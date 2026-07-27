"""Live level metering.

Metering reads the tail of the WAV file `arecord` is currently writing.
That keeps the capture path completely untouched — an alternative such as
`tee`-ing the PCM stream through Python would put the recording at the mercy
of the GIL, which the reliability requirement rules out.

The cost is roughly one small pread per poll; on a Pi 3 that is well under
1% CPU at 10 Hz.

In idle mode (not recording) an optional _MonitorCapture can run a separate
short-lived `arecord` so that VU meters and the waveform stay live for gain
setting and sound-check without writing to the recordings folder.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

WAV_HEADER_SIZE = 44
CLIP_THRESHOLD = 0.99      # fraction of full scale counted as clipping
PEAK_HOLD_SECONDS = 2.0
SILENCE_DBFS = -90.0

# RMS over every sample in the window would burn measurable CPU on a Pi 3 at
# 10 Hz. Sampling this many frames per channel is statistically ample for a
# meter and keeps the cost flat regardless of sample rate.
RMS_SAMPLE_TARGET = 1200

# Monitor capture defaults (used when the interface hasn't been probed yet).
_MON_RATE     = 48000
_MON_DEPTH    = 16
_MON_CHANNELS = 2
_MON_FMT      = "S16_LE"


def _to_dbfs(amplitude: float) -> float:
    """Linear 0..1 amplitude to dBFS, floored at SILENCE_DBFS."""
    if amplitude <= 0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(min(1.0, amplitude)))


class _MonitorCapture:
    """A short-lived `arecord` subprocess that writes to a temp WAV file.

    The LevelMeter reads its tail exactly like a real recording segment, so
    VU meters and the waveform stay live while the recorder is idle.

    The temp file is deleted when stop() is called.  arecord appends
    indefinitely; we never need to seek to the front so the WAV header
    being stale is fine — _read_tail() only touches the last few KB.
    """

    def __init__(self, alsa_id: str, rate: int, depth: int, channels: int) -> None:
        self.rate     = rate
        self.depth    = depth
        self.channels = channels
        fmt = {16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}.get(depth, "S16_LE")
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="zoompi_mon_")
        os.close(fd)
        self._path = Path(tmp)
        try:
            self._proc: subprocess.Popen | None = subprocess.Popen(
                ["arecord", "-D", alsa_id, "-f", fmt,
                 "-r", str(rate), "-c", str(channels), str(self._path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.warning("Monitor capture failed to start: %s", exc)
            self._proc = None
            self._path.unlink(missing_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._path.unlink(missing_ok=True)


class LevelMeter:
    """Polls the active recording file (or a monitor capture) and publishes
    RMS/peak per channel plus a waveform snapshot."""

    def __init__(self, recorder, poll_hz: float = 10.0) -> None:
        self._recorder = recorder
        self._interval = 1.0 / poll_hz
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._levels = self._empty()
        self._peak_hold = [0.0, 0.0]
        self._peak_hold_at = [0.0, 0.0]
        self._clip_latched = [False, False]

        self._monitor: _MonitorCapture | None = None
        self._monitoring_enabled = False   # set by start_monitor() / stop_monitor()

    @staticmethod
    def _empty() -> dict:
        return {
            "rms_db": [SILENCE_DBFS, SILENCE_DBFS],
            "peak_db": [SILENCE_DBFS, SILENCE_DBFS],
            "peak_hold_db": [SILENCE_DBFS, SILENCE_DBFS],
            "clip": [False, False],
            "active": False,
            "waveform": [],
        }

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="level-meter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_monitor()

    def read(self) -> dict:
        with self._lock:
            return dict(self._levels)

    def reset_clip(self) -> None:
        with self._lock:
            self._clip_latched = [False, False]

    def start_monitor(self, alsa_id: str,
                      rate: int = _MON_RATE,
                      depth: int = _MON_DEPTH,
                      channels: int = _MON_CHANNELS) -> None:
        """Enable idle monitoring.  The capture starts on the next _sample() poll
        so we are certain no recording arecord is competing for the device."""
        self._kill_monitor()
        self._monitor_params = (alsa_id, rate, depth, channels)
        self._monitoring_enabled = True

    def stop_monitor(self) -> None:
        """Disable idle monitoring and tear down any running capture."""
        self._monitoring_enabled = False
        self._kill_monitor()
        with self._lock:
            self._levels = self._empty()

    # ── Internals ────────────────────────────────────────────────────────────

    def _kill_monitor(self) -> None:
        if self._monitor:
            self._monitor.stop()
            self._monitor = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._sample()
            except Exception:
                # Metering is cosmetic — it must never take down the process.
                with self._lock:
                    self._levels = self._empty()

    def _sample(self) -> None:
        status   = self._recorder.status()
        is_rec   = status.get("is_recording")
        session  = status.get("session")

        if is_rec and session:
            # ── Recording path ──────────────────────────────────────────────
            # Kill any monitor immediately so the device is free.
            if self._monitor:
                self._kill_monitor()

            path     = self._recorder.current_segment_path()
            channels = int(session["channels"])
            depth    = int(session["bit_depth"])
            rate     = int(session["sample_rate"])

        elif self._monitoring_enabled:
            # ── Idle monitor path ───────────────────────────────────────────
            if self._monitor is None or not self._monitor.alive:
                params = getattr(self, "_monitor_params",
                                 ("default", _MON_RATE, _MON_DEPTH, _MON_CHANNELS))
                self._monitor = _MonitorCapture(*params)
                # Give arecord a moment to write the WAV header.
                time.sleep(0.15)

            if not self._monitor.alive:
                with self._lock:
                    self._levels = self._empty()
                return

            path     = self._monitor.path
            channels = self._monitor.channels
            depth    = self._monitor.depth
            rate     = self._monitor.rate

        else:
            # ── Truly idle — no monitoring requested ────────────────────────
            self._kill_monitor()
            with self._lock:
                self._levels = self._empty()
            return

        if path is None:
            with self._lock:
                self._levels = self._empty()
            return

        width  = depth // 8
        frame  = channels * width
        window = max(frame, int(rate * 0.05) * frame)

        chunk = self._read_tail(path, window, frame)
        if not chunk:
            return

        peaks, rms = self._analyse(chunk, channels, width)
        waveform   = self._make_waveform(chunk, channels, width)
        now        = time.time()

        with self._lock:
            for ch in range(2):
                p = peaks[ch] if ch < len(peaks) else 0.0
                if p >= self._peak_hold[ch] or now - self._peak_hold_at[ch] > PEAK_HOLD_SECONDS:
                    self._peak_hold[ch] = p
                    self._peak_hold_at[ch] = now
                if p >= CLIP_THRESHOLD:
                    self._clip_latched[ch] = True

            self._levels = {
                "rms_db": [
                    round(_to_dbfs(rms[i] if i < len(rms) else 0.0), 1) for i in range(2)
                ],
                "peak_db": [
                    round(_to_dbfs(peaks[i] if i < len(peaks) else 0.0), 1) for i in range(2)
                ],
                "peak_hold_db": [round(_to_dbfs(v), 1) for v in self._peak_hold],
                "clip": list(self._clip_latched),
                "active": True,
                "waveform": waveform,
            }

    @staticmethod
    def _read_tail(path: Path, window: int, frame: int) -> bytes:
        """Read the last `window` bytes, snapped to a frame boundary."""
        try:
            size = path.stat().st_size
        except OSError:
            return b""
        if size <= WAV_HEADER_SIZE + frame:
            return b""

        available = size - WAV_HEADER_SIZE
        take = min(window, available)
        take -= take % frame
        if take <= 0:
            return b""

        offset = size - take
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                return os.pread(fd, take, offset)
            finally:
                os.close(fd)
        except OSError:
            return b""

    @classmethod
    def _make_waveform(cls, chunk: bytes, channels: int, width: int, n_points: int = 80) -> list[float]:
        """Return n_points oscilloscope samples (−1..1) from the left channel for display."""
        samples = cls._decode(chunk, width)
        if not samples:
            return []
        full_scale = float(1 << (width * 8 - 1))
        # Use left channel (or mono)
        stride = channels if channels >= 2 else 1
        mono = samples[0::stride]
        if not mono:
            return []
        bucket = max(1, len(mono) // n_points)
        result: list[float] = []
        for i in range(n_points):
            start = i * bucket
            end = min(start + bucket, len(mono))
            if start >= len(mono):
                result.append(0.0)
            else:
                # Peak in bucket preserves waveform shape for oscilloscope display
                peak = max(mono[start:end], key=abs)
                result.append(round(float(peak) / full_scale, 3))
        return result

    @classmethod
    def _analyse(cls, chunk: bytes, channels: int, width: int) -> tuple[list[float], list[float]]:
        """Return per-channel (peak, rms) normalised to 0..1.

        Implemented on `array` rather than `audioop`, which PEP 594 removed
        in Python 3.13 — the version Raspberry Pi OS now ships.
        """
        samples = cls._decode(chunk, width)
        if not samples:
            return [0.0, 0.0], [0.0, 0.0]

        full_scale = float(1 << (width * 8 - 1))
        peaks: list[float] = []
        rms: list[float] = []

        for ch in range(2):
            # Mono input feeds both meters; stereo de-interleaves by stride.
            offset = ch if channels >= 2 else 0
            stride = channels if channels >= 2 else 1
            chan = samples[offset::stride]
            if not chan:
                peaks.append(0.0)
                rms.append(0.0)
                continue

            # max/min are C-speed over an array; abs() of the extremes gives
            # the true peak without a Python-level loop.
            peak = max(abs(max(chan)), abs(min(chan)))
            peaks.append(min(1.0, peak / full_scale))

            step = max(1, len(chan) // RMS_SAMPLE_TARGET)
            window = chan[::step]
            total = sum(float(s) * s for s in window)
            rms.append(min(1.0, math.sqrt(total / len(window)) / full_scale))

        return peaks, rms

    @staticmethod
    def _decode(chunk: bytes, width: int) -> array:
        """Bytes to a signed integer array, handling 16/24/32-bit input."""
        if width == 2:
            a = array("h")
            a.frombytes(chunk[: len(chunk) - len(chunk) % 2])
            if sys.byteorder == "big":
                a.byteswap()  # WAV is always little-endian
            return a

        if width == 4:
            a = array("i") if array("i").itemsize == 4 else array("l")
            a.frombytes(chunk[: len(chunk) - len(chunk) % 4])
            if sys.byteorder == "big":
                a.byteswap()
            return a

        if width == 3:
            # No native 24-bit type; sign-extend each triplet into 32-bit.
            out = array("i")
            usable = len(chunk) - len(chunk) % 3
            for i in range(0, usable, 3):
                v = chunk[i] | (chunk[i + 1] << 8) | (chunk[i + 2] << 16)
                out.append(v - 0x1000000 if v & 0x800000 else v)
            return out

        return array("i")
