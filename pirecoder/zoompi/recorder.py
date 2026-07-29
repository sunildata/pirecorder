"""Crash-safe recording engine.

Design rules that follow directly from "audio loss is unacceptable":

1. Audio never passes through Python. `arecord` writes PCM straight to the
   SD card, so a Python exception, a GC pause, or a stalled HTTP request
   cannot drop samples.
2. No RAM buffering. A 12-hour stereo take is ~8 GB; holding that in memory
   is impossible on a Pi 3. Bytes hit the filesystem continuously.
3. The recorder owns no network state. Wi-Fi dropping, the browser closing,
   or the phone dying are all invisible to it — nothing but an explicit stop,
   power loss, or a full disk ends a take.
4. Every take is journalled before the first sample is written, so an
   unexpected shutdown can be detected and repaired on next boot.
5. WAV headers are rewritten on close; if that never happened (power cut),
   the repair pass reconstructs them from the actual file size.
"""

from __future__ import annotations

import os
import signal
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .audio_devices import (
    AudioDevice,
    FORMAT_BY_DEPTH,
    negotiate_format,
    select_device,
)
from .config import RECORDINGS_DIR, config

# arecord flushes on its own schedule; this is how much audio a hard power
# cut can cost. ALSA's period size keeps it near a second in practice.
WAV_HEADER_SIZE = 44


class RecorderError(RuntimeError):
    pass


@dataclass
class Segment:
    index: int
    path: Path
    started_at: float
    ended_at: float | None = None
    bytes_written: int = 0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "filename": self.path.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "size_bytes": self.size_bytes(),
        }

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return self.bytes_written


@dataclass
class Marker:
    offset_seconds: float
    label: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "offset_seconds": round(self.offset_seconds, 2),
            "label": self.label,
            "created_at": self.created_at,
        }


@dataclass
class Session:
    """One logical recording, possibly spanning several split files."""

    session_id: str
    folder: Path
    base_name: str
    device: AudioDevice
    sample_rate: int
    bit_depth: int
    channels: int
    started_at: float
    notes: str = ""
    segments: list[Segment] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    paused_seconds: float = 0.0
    paused_at: float | None = None
    stopped_at: float | None = None

    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * (self.bit_depth // 8)

    def duration(self) -> float:
        """Wall-clock duration minus time spent paused."""
        end = self.stopped_at or time.time()
        paused = self.paused_seconds
        if self.paused_at is not None:
            paused += time.time() - self.paused_at
        return max(0.0, end - self.started_at - paused)

    def total_bytes(self) -> int:
        return sum(s.size_bytes() for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "base_name": self.base_name,
            "folder": self.folder.name,
            "device": self.device.alsa_id,
            "device_name": self.device.name,
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "channels": self.channels,
            "started_at": self.started_at,
            "duration": round(self.duration(), 2),
            "size_bytes": self.total_bytes(),
            "notes": self.notes,
            "segments": [s.to_dict() for s in self.segments],
            "markers": [m.to_dict() for m in self.markers],
        }


def _wav_header(rate: int, channels: int, depth: int, data_bytes: int) -> bytes:
    """Build a canonical 44-byte PCM WAV header."""
    byte_rate = rate * channels * depth // 8
    block_align = channels * depth // 8
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, depth),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


def repair_wav(path: Path, rate: int, channels: int, depth: int) -> bool:
    """Rewrite a WAV header to match the file's real size.

    `arecord` writes a placeholder header and patches it on clean exit. After
    a power cut the header still claims zero (or stale) length, which makes
    players refuse the file even though every sample is on disk.
    """
    try:
        size = path.stat().st_size
        if size <= WAV_HEADER_SIZE:
            return False
        data_bytes = size - WAV_HEADER_SIZE
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(_wav_header(rate, channels, depth, data_bytes))
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


class Recorder:
    """Owns at most one active recording session.

    All public methods are safe to call from HTTP handlers, GPIO callbacks,
    or the watchdog thread simultaneously.
    """

    def __init__(self, on_event: Callable[[str, dict], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._proc: subprocess.Popen | None = None
        self._dual_proc: subprocess.Popen | None = None
        self._watchdog: threading.Thread | None = None
        self._stop_watchdog = threading.Event()
        self._on_event = on_event or (lambda *_: None)
        self._last_error: str | None = None

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.stopped_at is None

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._session and self._session.paused_at is not None)

    def status(self) -> dict:
        with self._lock:
            if not self._session:
                return {
                    "is_recording": False,
                    "is_paused": False,
                    "session": None,
                    "last_error": self._last_error,
                }
            s = self._session
            return {
                "is_recording": s.stopped_at is None,
                "is_paused": s.paused_at is not None,
                "session": s.to_dict(),
                "last_error": self._last_error,
            }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, label: str = "", notes: str = "") -> dict:
        with self._lock:
            if self.is_recording:
                raise RecorderError("Already recording")

            device = select_device(config.get("audio_device"))
            if device is None:
                raise RecorderError("No audio capture device found")

            rate, depth, channels = negotiate_format(
                device,
                config.get("sample_rate"),
                config.get("bit_depth"),
                config.get("channels"),
            )

            free_mb = _free_megabytes(RECORDINGS_DIR)
            if free_mb < config.get("min_free_mb"):
                raise RecorderError(
                    f"Only {free_mb} MB free — need at least "
                    f"{config.get('min_free_mb')} MB to start"
                )

            now = datetime.now()
            folder = RECORDINGS_DIR / now.strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)

            stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
            safe_label = _sanitize(label)
            base_name = f"{stamp}_{safe_label}" if safe_label else stamp

            session = Session(
                session_id=stamp,
                folder=folder,
                base_name=base_name,
                device=device,
                sample_rate=rate,
                bit_depth=depth,
                channels=channels,
                started_at=time.time(),
                notes=notes,
            )

            # Journal before the first sample so a crash is always detectable.
            _write_journal(session)

            self._session = session
            self._last_error = None
            self._spawn_segment(0)

            self._stop_watchdog.clear()
            self._watchdog = threading.Thread(
                target=self._watch, name="rec-watchdog", daemon=True
            )
            self._watchdog.start()

            self._on_event("recording_started", self.status())
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            if not self._session:
                raise RecorderError("Not recording")

            self._stop_watchdog.set()
            self._terminate_procs()

            session = self._session
            session.stopped_at = time.time()
            if session.segments:
                session.segments[-1].ended_at = session.stopped_at

            # arecord patches its own header on SIGINT, but repair anyway —
            # it is idempotent and covers a kill that arrived too late.
            for seg in session.segments:
                repair_wav(seg.path, session.sample_rate, session.channels, session.bit_depth)

            _clear_journal(session)
            result = session.to_dict()
            self._session = None
            self._on_event("recording_stopped", {"session": result})
            return result

    def pause(self) -> dict:
        """Close the current segment; resume opens a new one.

        Splitting rather than SIGSTOP means paused audio is already durable
        on disk instead of sitting in a suspended process's buffers.
        """
        with self._lock:
            if not self.is_recording:
                raise RecorderError("Not recording")
            if self.is_paused:
                raise RecorderError("Already paused")

            self._terminate_procs()
            session = self._session
            assert session is not None
            if session.segments:
                seg = session.segments[-1]
                seg.ended_at = time.time()
                repair_wav(seg.path, session.sample_rate, session.channels, session.bit_depth)
            session.paused_at = time.time()
            self._on_event("recording_paused", self.status())
            return self.status()

    def resume(self) -> dict:
        with self._lock:
            if not self._session:
                raise RecorderError("Not recording")
            if not self.is_paused:
                raise RecorderError("Not paused")
            session = self._session
            assert session.paused_at is not None
            session.paused_seconds += time.time() - session.paused_at
            session.paused_at = None
            self._spawn_segment(len(session.segments))
            self._on_event("recording_resumed", self.status())
            return self.status()

    def add_marker(self, label: str = "") -> dict:
        with self._lock:
            if not self._session:
                raise RecorderError("Not recording")
            marker = Marker(
                offset_seconds=self._session.duration(),
                label=label or f"Marker {len(self._session.markers) + 1}",
            )
            self._session.markers.append(marker)
            _write_journal(self._session)
            self._on_event("marker_added", {"marker": marker.to_dict()})
            return marker.to_dict()

    def set_notes(self, notes: str) -> dict:
        with self._lock:
            if not self._session:
                raise RecorderError("Not recording")
            self._session.notes = notes
            _write_journal(self._session)
            return {"notes": notes}

    def current_segment_path(self) -> Path | None:
        """Used by the level meter to sample the growing file."""
        with self._lock:
            if not self._session or not self._session.segments:
                return None
            if self.is_paused:
                return None
            return self._session.segments[-1].path

    # ── Internals ────────────────────────────────────────────────────────────

    def _segment_path(self, index: int) -> Path:
        session = self._session
        assert session is not None
        suffix = "" if index == 0 else f"_part{index + 1:03d}"
        return session.folder / f"{session.base_name}{suffix}.wav"

    def _spawn_segment(self, index: int) -> None:
        session = self._session
        assert session is not None

        path = self._segment_path(index)
        fmt = FORMAT_BY_DEPTH.get(session.bit_depth, "S16_LE")
        cmd = [
            "arecord",
            "-D", session.device.alsa_id,
            "-f", fmt,
            "-r", str(session.sample_rate),
            "-c", str(session.channels),
            "-t", "wav",
            # Buffer and period are separate concerns. The buffer is the safety
            # margin against scheduling hiccups; the period is how often bytes
            # actually reach the file. A large period is what made live metering
            # lag ~1 s behind, so keep a 1 s buffer but flush every 50 ms.
            # (Units are microseconds. The previous --buffer-size took *frames*,
            # so 192000 was 4 s at 48 kHz, not the 1 s intended.)
            "--buffer-time=1000000",
            "--period-time=50000",
            str(path),
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RecorderError("arecord not found — install alsa-utils") from exc

        # A bad ALSA format fails within milliseconds; surface it now rather
        # than letting the caller believe recording began.
        time.sleep(0.35)
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()
            raise RecorderError(f"arecord failed to start: {err or 'unknown error'}")

        self._proc = proc
        session.segments.append(Segment(index=index, path=path, started_at=time.time()))
        _write_journal(session)

        if config.get("dual_recording"):
            self._spawn_dual(index)

    def _spawn_dual(self, index: int) -> None:
        """Second take at -12 dB as insurance against clipping the master."""
        session = self._session
        assert session is not None
        safety_dir = session.folder / "safety"
        safety_dir.mkdir(exist_ok=True)
        path = safety_dir / self._segment_path(index).name
        fmt = FORMAT_BY_DEPTH.get(session.bit_depth, "S16_LE")
        try:
            self._dual_proc = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "alsa", "-acodec", "pcm_s16le",
                    "-ar", str(session.sample_rate),
                    "-ac", str(session.channels),
                    "-i", session.device.alsa_id,
                    "-af", "volume=-12dB",
                    "-c:a", fmt.lower().replace("_le", "le").replace("s", "pcm_s"),
                    str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError):
            self._dual_proc = None  # ffmpeg missing — master take is unaffected

    def _terminate_procs(self) -> None:
        for attr in ("_proc", "_dual_proc"):
            proc = getattr(self, attr)
            if proc is None:
                continue
            try:
                # SIGINT lets arecord finalise its WAV header cleanly.
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except (ProcessLookupError, OSError):
                pass
            setattr(self, attr, None)

    def _watch(self) -> None:
        """Enforce split limits, disk headroom, and restart a dead capture."""
        split_bytes = int(config.get("auto_split_mb")) * 1024 * 1024
        split_seconds = int(config.get("auto_split_minutes")) * 60

        while not self._stop_watchdog.wait(2.0):
            with self._lock:
                session = self._session
                if session is None or session.stopped_at is not None:
                    return
                if session.paused_at is not None:
                    continue

                seg = session.segments[-1] if session.segments else None
                if seg is None:
                    continue

                # A capture that died on its own is the one failure mode that
                # silently loses audio, so restart it immediately.
                if self._proc is not None and self._proc.poll() is not None:
                    err = ""
                    if self._proc.stderr:
                        err = (self._proc.stderr.read() or b"").decode(errors="replace")
                    self._last_error = f"capture restarted: {err.strip()[:200]}"
                    repair_wav(seg.path, session.sample_rate, session.channels, session.bit_depth)
                    seg.ended_at = time.time()
                    try:
                        self._spawn_segment(len(session.segments))
                        self._on_event("capture_restarted", {"error": self._last_error})
                    except RecorderError as exc:
                        self._last_error = str(exc)
                        self._on_event("capture_failed", {"error": str(exc)})
                        return
                    continue

                # Stop before the card fills rather than corrupting the tail.
                if _free_megabytes(RECORDINGS_DIR) < config.get("min_free_mb"):
                    self._last_error = "storage full — recording stopped"
                    self._on_event("storage_full", {"error": self._last_error})
                    try:
                        self.stop()
                    except RecorderError:
                        pass
                    return

                size = seg.size_bytes()
                elapsed = time.time() - seg.started_at
                needs_split = (split_bytes and size >= split_bytes) or (
                    split_seconds and elapsed >= split_seconds
                )
                if needs_split:
                    self._rotate_segment(seg)

    def _rotate_segment(self, seg: Segment) -> None:
        """Close the current file and open the next with minimal gap."""
        session = self._session
        assert session is not None
        self._terminate_procs()
        seg.ended_at = time.time()
        repair_wav(seg.path, session.sample_rate, session.channels, session.bit_depth)
        try:
            self._spawn_segment(len(session.segments))
            self._on_event("segment_split", {"segment": seg.to_dict()})
        except RecorderError as exc:
            self._last_error = str(exc)
            self._on_event("capture_failed", {"error": str(exc)})


# ── Journal + crash recovery ─────────────────────────────────────────────────

def _journal_path(session: Session) -> Path:
    return session.folder / f".{session.base_name}.journal.json"


def _write_journal(session: Session) -> None:
    import json

    payload = {
        "session_id": session.session_id,
        "base_name": session.base_name,
        "sample_rate": session.sample_rate,
        "bit_depth": session.bit_depth,
        "channels": session.channels,
        "started_at": session.started_at,
        "notes": session.notes,
        "segments": [s.path.name for s in session.segments],
        "markers": [m.to_dict() for m in session.markers],
    }
    path = _journal_path(session)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # journalling is best-effort; never block a take


def _clear_journal(session: Session) -> None:
    try:
        _journal_path(session).unlink(missing_ok=True)
    except OSError:
        pass


def recover_orphans() -> list[dict]:
    """Repair takes interrupted by power loss. Called once at startup.

    Any journal still on disk means the recorder never reached a clean stop,
    so the referenced WAV files have stale headers and need rebuilding.
    """
    import json

    recovered: list[dict] = []
    if not RECORDINGS_DIR.exists():
        return recovered

    for journal in RECORDINGS_DIR.glob("*/.*.journal.json"):
        try:
            meta = json.loads(journal.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            journal.unlink(missing_ok=True)
            continue

        folder = journal.parent
        rate = int(meta.get("sample_rate", 48000))
        channels = int(meta.get("channels", 2))
        depth = int(meta.get("bit_depth", 16))
        fixed: list[str] = []

        for name in meta.get("segments", []):
            path = folder / name
            if path.exists() and repair_wav(path, rate, channels, depth):
                fixed.append(name)

        if fixed:
            recovered.append(
                {
                    "session_id": meta.get("session_id"),
                    "folder": folder.name,
                    "repaired": fixed,
                    "markers": meta.get("markers", []),
                    "notes": meta.get("notes", ""),
                }
            )
        journal.unlink(missing_ok=True)

    return recovered


# ── Helpers ──────────────────────────────────────────────────────────────────

def _free_megabytes(path: Path) -> int:
    try:
        st = os.statvfs(str(path))
        return int(st.f_bavail * st.f_frsize / (1024 * 1024))
    except (OSError, AttributeError):
        return 10_000  # non-POSIX dev machine — don't block


def _sanitize(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip())
    return "-".join(filter(None, keep.split("-")))[:60]
