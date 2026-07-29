"""Offline post-processing via FFmpeg.

DSP runs *after* a take completes, never during capture. Real-time limiting
and compression at 48 kHz stereo on a Pi 3 would compete with the recording
process for CPU, and the spec's "zero dropped samples" requirement makes that
trade unacceptable. The master WAV is always preserved untouched; processed
output is written alongside it.

Jobs run one at a time on a background worker so a batch of conversions
cannot saturate the CPU while a new recording is in progress.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

from . import db
from .config import config


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    """Which optional audio encoders this FFmpeg build carries.

    Distributions ship trimmed builds — Debian's `ffmpeg` has both of these,
    but a self-compiled or `-free` variant may lack libmp3lame. Checking up
    front lets the settings page grey out a format instead of letting the user
    pick one that fails silently an hour into a session.
    """
    if not ffmpeg_available():
        return frozenset()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    found = set()
    for name in ("libmp3lame", "flac"):
        if re.search(rf"^\s*\S*A\S*\s+{re.escape(name)}\s", proc.stdout, re.MULTILINE):
            found.add(name)
    return frozenset(found)


def build_filter_chain() -> str:
    """Assemble an FFmpeg -af chain from the enabled post-processing options."""
    stages: list[str] = []

    hp = int(config.get("post_highpass_hz") or 0)
    if hp > 0:
        stages.append(f"highpass=f={hp}")

    if config.get("post_noise_gate"):
        # Gentle downward expansion — enough to suppress room hiss between
        # cues without audibly pumping on speech.
        stages.append("agate=threshold=0.005:ratio=2:attack=10:release=250")

    if config.get("post_compressor"):
        stages.append(
            "acompressor=threshold=-18dB:ratio=3:attack=20:release=250:makeup=2"
        )

    if config.get("post_limiter"):
        stages.append("alimiter=limit=0.97:attack=5:release=50")

    return ",".join(stages)


class Job:
    __slots__ = ("id", "kind", "source", "target", "status", "error",
                 "created_at", "finished_at", "progress")

    def __init__(self, kind: str, source: Path, target: Path) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.source = source
        self.target = target
        self.status = "queued"
        self.error: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.progress = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source.name,
            "target": self.target.name,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class PostProcessor:
    """Single-worker FFmpeg queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Job] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._recorder = None  # set by the app so we can yield during takes

    def attach_recorder(self, recorder) -> None:
        self._recorder = recorder

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="postprocess", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── Submission ───────────────────────────────────────────────────────────

    def convert_to_mp3(self, source: Path) -> Job:
        target = source.with_suffix(".mp3")
        return self._submit(Job("mp3", source, target))

    def convert_to_flac(self, source: Path) -> Job:
        target = source.with_suffix(".flac")
        return self._submit(Job("flac", source, target))

    def apply_dsp(self, source: Path) -> Job:
        target = source.with_name(f"{source.stem}_processed{source.suffix}")
        return self._submit(Job("dsp", source, target))

    def _submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def jobs(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )][:50]

    # ── Worker ───────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Defer heavy CPU work while a take is live.
            while self._recorder is not None and self._recorder.is_recording:
                if self._stop.wait(5.0):
                    return

            self._execute(job)
            self._queue.task_done()

    def _execute(self, job: Job) -> None:
        job.status = "running"
        if not ffmpeg_available():
            job.status = "failed"
            job.error = "ffmpeg not installed"
            job.finished_at = time.time()
            return
        if not job.source.exists():
            job.status = "failed"
            job.error = "source file missing"
            job.finished_at = time.time()
            return

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(job.source)]

        if job.kind == "mp3":
            cmd += ["-codec:a", "libmp3lame", "-b:a", str(config.get("mp3_bitrate"))]
        elif job.kind == "flac":
            # Lossless, so bit depth and sample rate carry over untouched.
            # Levels above ~5 buy very little size for a lot of Pi 3 CPU time.
            level = max(0, min(12, int(config.get("flac_compression") or 5)))
            cmd += ["-codec:a", "flac", "-compression_level", str(level)]
        else:
            chain = build_filter_chain()
            if not chain:
                job.status = "skipped"
                job.error = "no post-processing options enabled"
                job.finished_at = time.time()
                return
            cmd += ["-af", chain, "-c:a", "pcm_s16le"]

        # `nice` keeps encoding off the recorder's back if one starts mid-job.
        cmd = ["nice", "-n", "15"] + cmd + [str(job.target)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode == 0:
                job.status = "done"
                db.log_event("postprocess_done", {"job": job.kind, "file": job.target.name})
            else:
                job.status = "failed"
                job.error = (proc.stderr or "ffmpeg failed").strip()[:300]
                job.target.unlink(missing_ok=True)
        except subprocess.TimeoutExpired:
            job.status = "failed"
            job.error = "timed out after 1 hour"
            job.target.unlink(missing_ok=True)
        except OSError as exc:
            job.status = "failed"
            job.error = str(exc)[:300]
        finally:
            job.finished_at = time.time()


processor = PostProcessor()
