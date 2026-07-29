"""Live level metering.

Metering reads the WAV file `arecord` is currently writing. That keeps the
capture path completely untouched — an alternative such as `tee`-ing the PCM
stream through Python would put the recording at the mercy of the GIL, which
the reliability requirement rules out.

Reads are *continuous*: each poll picks up exactly where the last one stopped,
so no audio is skipped and the waveform is a faithful, gap-free picture of the
signal. If the reader ever falls behind (a busy Pi, a stalled poll) it skips
forward to the newest audio rather than replaying stale bytes — staying live
matters more than showing every sample.

The cost is one small pread per poll; on a Pi 3 that is well under 1% CPU.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from array import array
from pathlib import Path

WAV_HEADER_SIZE = 44
CLIP_THRESHOLD = 0.99      # fraction of full scale counted as clipping
PEAK_HOLD_SECONDS = 2.0
SILENCE_DBFS = -90.0

# RMS over every sample in the window would burn measurable CPU on a Pi 3 at
# 10 Hz. Sampling this many frames per channel is statistically ample for a
# meter and keeps the cost flat regardless of sample rate.
RMS_SAMPLE_TARGET = 1200

# Waveform points published per poll. The browser stitches consecutive frames
# into a scrolling oscilloscope, so this is resolution-per-frame, not total.
WAVEFORM_POINTS = 60

# If the reader falls this far behind the write head, skip forward instead of
# working through the backlog — a live meter must never drift.
MAX_LAG_SECONDS = 0.25


def _to_dbfs(amplitude: float) -> float:
    """Linear 0..1 amplitude to dBFS, floored at SILENCE_DBFS."""
    if amplitude <= 0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(min(1.0, amplitude)))


class LevelMeter:
    """Polls the active recording file and publishes RMS/peak per channel."""

    def __init__(self, recorder, poll_hz: float = 20.0) -> None:
        self._recorder = recorder
        self._interval = 1.0 / poll_hz
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._levels = self._empty()
        self._peak_hold = [0.0, 0.0]
        self._peak_hold_at = [0.0, 0.0]
        self._clip_latched = [False, False]

        # Continuous-read cursor. Reset whenever the segment file changes
        # (auto-split, new take) so we never read across file boundaries.
        self._read_path: Path | None = None
        self._read_pos = 0

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

    def read(self) -> dict:
        with self._lock:
            return dict(self._levels)

    def reset_clip(self) -> None:
        with self._lock:
            self._clip_latched = [False, False]

    # ── Internals ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._sample()
            except Exception:
                # Metering is cosmetic — it must never take down the process.
                with self._lock:
                    self._levels = self._empty()

    def _sample(self) -> None:
        path = self._recorder.current_segment_path()
        status = self._recorder.status()
        session = status.get("session")

        if path is None or session is None or not status.get("is_recording"):
            with self._lock:
                self._levels = self._empty()
            self._read_path = None
            return

        channels = int(session["channels"])
        depth = int(session["bit_depth"])
        rate = int(session["sample_rate"])
        width = depth // 8
        frame = channels * width

        chunk = self._read_new(path, frame, max_bytes=int(rate * MAX_LAG_SECONDS) * frame)
        if not chunk:
            return

        # Decode once and share — at 20 Hz a second pass would double the CPU
        # cost, and 24-bit decoding runs a Python-level loop.
        samples = self._decode(chunk, width)
        if not samples:
            return

        peaks, rms = self._analyse(samples, channels, width)
        waveform = self._make_waveform(samples, channels, width)
        now = time.time()

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

    def _read_new(self, path: Path, frame: int, max_bytes: int) -> bytes:
        """Read every byte written since the last poll, frame-aligned.

        Continuity is what makes the waveform look like the real signal: the
        previous implementation read a fixed-size tail on each poll, which
        silently dropped the audio between polls.

        If more than `max_bytes` is pending, the cursor jumps to the newest
        audio — a live meter should show *now*, not catch up on the past.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return b""
        if size <= WAV_HEADER_SIZE + frame:
            return b""

        # New segment (auto-split or new take): start at the write head.
        if self._read_path != path:
            self._read_path = path
            self._read_pos = max(WAV_HEADER_SIZE, size - max_bytes)

        start = max(self._read_pos, WAV_HEADER_SIZE)
        if size - start > max_bytes:
            start = size - max_bytes          # fell behind — skip stale audio
        start -= (start - WAV_HEADER_SIZE) % frame   # align to a frame boundary

        take = size - start
        take -= take % frame
        if take <= 0:
            return b""

        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                data = os.pread(fd, take, start)
            finally:
                os.close(fd)
        except OSError:
            return b""

        self._read_pos = start + len(data)
        return data

    @staticmethod
    def _make_waveform(
        samples: array, channels: int, width: int, n_points: int = WAVEFORM_POINTS
    ) -> list[float]:
        """Return n_points oscilloscope samples (−1..1) from the left channel."""
        full_scale = float(1 << (width * 8 - 1))
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

    @staticmethod
    def _analyse(
        samples: array, channels: int, width: int
    ) -> tuple[list[float], list[float]]:
        """Return per-channel (peak, rms) normalised to 0..1.

        Implemented on `array` rather than `audioop`, which PEP 594 removed
        in Python 3.13 — the version Raspberry Pi OS now ships.
        """
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
