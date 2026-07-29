"""Persistent configuration with atomic writes.

Settings live in a JSON file next to the recordings so a single SD card
carries both the audio and the device configuration.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ZOOMPI_DATA", BASE_DIR / "data"))
RECORDINGS_DIR = Path(os.environ.get("ZOOMPI_RECORDINGS", BASE_DIR / "recordings"))
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "zoompi.db"
LOG_DIR = DATA_DIR / "logs"

DEFAULTS: dict[str, Any] = {
    # ── Identity ────────────────────────────────────────────────────────────
    "device_name": "ZoomPi",
    "password": "zoompi",          # replaced by a hash on first save
    "auth_enabled": True,
    # ── Audio ───────────────────────────────────────────────────────────────
    "sample_rate": 48000,          # negotiated down if the interface can't do it
    "bit_depth": 16,               # 16 | 24 | 32, subject to hardware support
    "channels": 2,                 # 1 = mono, 2 = stereo
    "audio_device": "auto",        # "auto" or an ALSA id such as "hw:1,0"
    # Input gain as a percentage of the ALSA capture control's own range. ALSA
    # forgets mixer levels across reboots, so this is the source of truth and
    # gets pushed back to the hardware on startup and before every take.
    "capture_gain_percent": 75,
    # ── Recording behaviour ─────────────────────────────────────────────────
    "auto_split_mb": 2048,         # 0 disables size-based splitting
    "auto_split_minutes": 0,       # 0 disables time-based splitting
    "recording_lock": False,       # requires confirmation before stopping
    "dual_recording": False,       # simultaneous -12 dB safety take
    # The master WAV is always written; these add renders alongside it.
    "output_format": "wav",        # wav | wav+flac | wav+mp3 | wav+flac+mp3
    "mp3_bitrate": "192k",
    "flac_compression": 5,         # 0-12; 5 is FLAC's own default
    # ── Storage ─────────────────────────────────────────────────────────────
    "auto_cleanup": False,
    "cleanup_threshold_pct": 90,   # start deleting oldest above this usage
    "min_free_mb": 500,            # refuse to start a take below this
    # ── Post-processing (never applied to the master take) ──────────────────
    "post_highpass_hz": 0,         # 0 disables
    "post_limiter": False,
    "post_compressor": False,
    "post_noise_gate": False,
    # ── Network ─────────────────────────────────────────────────────────────
    "wifi_mode": "auto",           # auto | ap | client
    "ap_ssid": "ZoomPi",
    "ap_password": "zoompi12345",
    "ap_channel": 7,
    # ── Hardware add-ons ────────────────────────────────────────────────────
    "hardware_enabled": False,
    "gpio_record_button": 17,
    "gpio_stop_button": 27,
    "gpio_status_led": 22,
    "oled_enabled": False,
}


class Config:
    """Thread-safe settings store backed by an atomically written JSON file."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._ensure_dirs()
        self.load()

    @staticmethod
    def _ensure_dirs() -> None:
        for d in (DATA_DIR, RECORDINGS_DIR, LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self.save()
                return
            try:
                stored = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A corrupt config must not brick the recorder — fall back to
                # defaults and keep the bad file for inspection.
                try:
                    self._path.rename(self._path.with_suffix(".json.corrupt"))
                except OSError:
                    pass
                self.save()
                return
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
            self._data = merged

    def save(self) -> None:
        """Write via temp file + fsync + rename so a power cut can't truncate it."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key not in DEFAULTS:
                raise KeyError(f"Unknown setting: {key}")
            self._data[key] = value
            self.save()

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Apply several settings at once. Unknown keys are reported, not raised."""
        applied, rejected = {}, []
        with self._lock:
            for key, value in values.items():
                if key in DEFAULTS:
                    self._data[key] = value
                    applied[key] = value
                else:
                    rejected.append(key)
            self.save()
        return {"applied": applied, "rejected": rejected}

    def as_dict(self, redact: bool = True) -> dict[str, Any]:
        with self._lock:
            data = dict(self._data)
        if redact:
            data.pop("password", None)
            data.pop("ap_password", None)
        return data


config = Config()
