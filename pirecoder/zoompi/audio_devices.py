"""USB audio interface discovery and capability probing.

The spec asks for 24-bit/48 kHz, but common interfaces (UCA202/UCA222) are
16-bit only. Rather than hardcode a format that may fail to open, every
candidate format is probed against the real hardware and the best supported
one is selected.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, asdict, field

# arecord's format names, ordered best-first.
FORMAT_BY_DEPTH = {32: "S32_LE", 24: "S24_3LE", 16: "S16_LE"}
CANDIDATE_DEPTHS = (24, 16)
CANDIDATE_RATES = (96000, 48000, 44100)

_CARD_RE = re.compile(
    r"^card (?P<card>\d+): (?P<cid>[^\[]+)\[(?P<cname>[^\]]+)\], "
    r"device (?P<dev>\d+): (?P<did>[^\[]+)\[(?P<dname>[^\]]+)\]"
)


@dataclass
class AudioDevice:
    card: int
    device: int
    card_id: str
    name: str
    alsa_id: str
    is_usb: bool = False
    supported_rates: list[int] = field(default_factory=list)
    supported_depths: list[int] = field(default_factory=list)
    max_channels: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


def _probe(alsa_id: str, rate: int, depth: int, channels: int) -> bool:
    """Ask ALSA to open the device with this exact format for a moment.

    `-d 1` with `--test-position` would still record; instead we use a
    zero-duration capture to /dev/null which fails fast if the format is
    unsupported.
    """
    fmt = FORMAT_BY_DEPTH.get(depth)
    if not fmt:
        return False
    rc, _, err = _run(
        [
            "arecord", "-D", alsa_id, "-f", fmt,
            "-r", str(rate), "-c", str(channels),
            "-d", "1", "--duration=0", "-t", "raw", "/dev/null",
        ],
        timeout=4,
    )
    if rc == 0:
        return True
    # A busy device means the format is fine but something else holds it.
    return "Device or resource busy" in err


def list_devices(probe: bool = True) -> list[AudioDevice]:
    """Enumerate capture devices, optionally probing each for real capabilities."""
    rc, out, _ = _run(["arecord", "-l"])
    if rc != 0:
        return []

    devices: list[AudioDevice] = []
    for line in out.splitlines():
        m = _CARD_RE.match(line.strip())
        if not m:
            continue
        card = int(m.group("card"))
        dev = int(m.group("dev"))
        card_id = m.group("cid").strip()
        name = m.group("cname").strip()
        alsa_id = f"hw:{card},{dev}"
        d = AudioDevice(
            card=card,
            device=dev,
            card_id=card_id,
            name=name,
            alsa_id=alsa_id,
            is_usb="usb" in (card_id + name).lower(),
        )
        if probe:
            d.supported_rates = [r for r in CANDIDATE_RATES if _probe(alsa_id, r, 16, 2)]
            base_rate = d.supported_rates[0] if d.supported_rates else 48000
            d.supported_depths = [
                b for b in CANDIDATE_DEPTHS if _probe(alsa_id, base_rate, b, 2)
            ]
            d.max_channels = 2 if _probe(alsa_id, base_rate, 16, 2) else 1
        devices.append(d)
    return devices


def select_device(preferred: str = "auto", probe: bool = True) -> AudioDevice | None:
    """Resolve the configured device, preferring a USB interface over onboard.

    Set probe=False for fast lookups (e.g. gain control) that don't need
    capability data — avoids the several-second arecord probe round-trip.
    """
    devices = list_devices(probe=probe)
    if not devices:
        return None
    if preferred and preferred != "auto":
        for d in devices:
            if d.alsa_id == preferred or d.card_id == preferred:
                return d
    for d in devices:
        if d.is_usb:
            return d
    return devices[0]


def negotiate_format(
    device: AudioDevice, want_rate: int, want_depth: int, want_channels: int
) -> tuple[int, int, int]:
    """Return (rate, depth, channels) the hardware will actually accept.

    Falls back gracefully instead of letting arecord fail at record time.
    """
    rates = device.supported_rates or [48000]
    depths = device.supported_depths or [16]

    rate = want_rate if want_rate in rates else min(rates, key=lambda r: abs(r - want_rate))
    depth = want_depth if want_depth in depths else max(depths)
    channels = min(want_channels, max(1, device.max_channels))
    return rate, depth, channels


# ── Input gain (ALSA capture volume) ─────────────────────────────────────────
#
# Gain is driven as a *percentage* of the control's own range, never as a dB
# string. Every ALSA volume control accepts "70%", while dB is only supported
# by some — and a negative argument such as "-10dB" is liable to be parsed as
# a command-line option. The dB figure the device reports is read back and
# shown to the user, but it is never used to command a change.
#
# Only controls advertising `cvolume` (a capture volume) qualify. Blindly
# poking likely-sounding names is what made the previous version report
# success while nothing changed: a name such as "Digital" often resolves to a
# playback control, which accepts the value happily and leaves input gain
# untouched.

_CONTROL_NAME_RE = re.compile(r"Simple mixer control '([^']+)',\d+")
_CAPABILITIES_RE = re.compile(r"Capabilities:(.*)")
_CAPTURE_LIMITS_RE = re.compile(r"Capture (\d+) - (\d+)")
# Anchored on "Capture" so a control exposing both playback and capture on one
# line (e.g. "Playback 20 [64%] ... Capture 15 [48%] ...") yields the capture
# figures rather than the playback ones.
_CAPTURE_VALUE_RE = re.compile(
    r"Capture (\d+) \[(\d+)%\](?:\s*\[([+-]?\d+(?:\.\d+)?)dB\])?"
)

# Name fragments that suggest an input gain, best first.
_GAIN_NAME_HINTS = ("mic", "capture", "input", "gain", "adc", "line")


@dataclass
class CaptureControl:
    """An ALSA capture volume control and its current state."""

    name: str
    raw: int
    raw_min: int
    raw_max: int
    percent: int
    db: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def list_capture_controls(device: AudioDevice) -> list[CaptureControl]:
    """Every control on the card that can act as an input gain."""
    rc, out, _ = _run(["amixer", "-c", str(device.card)])
    if rc != 0:
        return []

    controls: list[CaptureControl] = []
    # Split on the control header so each block is one control.
    for block in re.split(r"(?=Simple mixer control )", out):
        name_m = _CONTROL_NAME_RE.search(block)
        if not name_m:
            continue
        caps = _CAPABILITIES_RE.search(block)
        if not caps or "cvolume" not in caps.group(1):
            continue  # no capture volume — cannot serve as a gain
        limits = _CAPTURE_LIMITS_RE.search(block)
        value = _CAPTURE_VALUE_RE.search(block)
        if not limits or not value:
            continue
        controls.append(
            CaptureControl(
                name=name_m.group(1),
                raw=int(value.group(1)),
                raw_min=int(limits.group(1)),
                raw_max=int(limits.group(2)),
                percent=int(value.group(2)),
                db=float(value.group(3)) if value.group(3) else None,
            )
        )
    return controls


def _gain_preference(control: CaptureControl) -> int:
    low = control.name.lower()
    for i, hint in enumerate(_GAIN_NAME_HINTS):
        if hint in low:
            return i
    return len(_GAIN_NAME_HINTS)


def find_capture_control(device: AudioDevice) -> CaptureControl | None:
    """The most likely input-gain control, or None if the device has none."""
    controls = list_capture_controls(device)
    if not controls:
        return None
    return sorted(controls, key=_gain_preference)[0]


def get_capture_gain(device: AudioDevice) -> CaptureControl | None:
    """Current input gain state, or None when gain is analogue-only."""
    return find_capture_control(device)


def set_capture_gain(device: AudioDevice, percent: int) -> CaptureControl | None:
    """Set input gain to `percent` of the control's range.

    Returns the control re-read from the hardware afterwards — the only
    trustworthy confirmation that anything actually moved. None means the
    device exposes no capture volume at all.
    """
    control = find_capture_control(device)
    if control is None:
        return None

    percent = max(0, min(100, int(percent)))
    _run(["amixer", "-c", str(device.card), "sset", control.name, f"{percent}%"])

    for updated in list_capture_controls(device):
        if updated.name == control.name:
            return updated
    return None
