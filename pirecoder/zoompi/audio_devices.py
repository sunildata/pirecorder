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


# Common capture control names, ordered most-specific first.
_CAPTURE_CONTROLS = [
    "Mic", "Mic Boost", "Mic Gain", "Mic Volume",
    "Capture", "Capture Volume",
    "Input", "Input Volume",
    "ADC", "ADC Level",
    "Line In", "Line",
    "PCM Capture Source",
    "Digital",
]


def _amixer_sset(card: int, control: str, value: str) -> bool:
    """amixer sset — the correct command for named SimpleControls."""
    rc, _, _ = _run(["amixer", "-c", str(card), "sset", control, value])
    return rc == 0


def discover_capture_controls(device: AudioDevice) -> list[dict]:
    """Return all mixer controls on the card that have capture capability.

    Each entry: {"name": str, "db_value": int|None, "pct_value": int|None}
    """
    rc, out, _ = _run(["amixer", "-c", str(device.card)])
    if rc != 0:
        return []

    controls: list[dict] = []
    current: dict = {}

    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"Simple mixer control '([^']+)',\d+", line)
        if m:
            if current.get("has_capture"):
                controls.append({
                    "name": current["name"],
                    "db_value":  current.get("db_value"),
                    "pct_value": current.get("pct_value"),
                })
            current = {"name": m.group(1), "has_capture": False}
        elif current:
            if "cvolume" in line or "cswitch" in line:
                current["has_capture"] = True
            if current.get("has_capture"):
                db_m = re.search(r"\[([+-]?\d+(?:\.\d+)?)dB\]", line)
                if db_m and "db_value" not in current:
                    current["db_value"] = round(float(db_m.group(1)))
                pct_m = re.search(r"\[(\d+)%\]", line)
                if pct_m and "pct_value" not in current:
                    current["pct_value"] = int(pct_m.group(1))

    if current.get("has_capture"):
        controls.append({
            "name": current["name"],
            "db_value":  current.get("db_value"),
            "pct_value": current.get("pct_value"),
        })

    return controls


def get_capture_gain(device: AudioDevice) -> int | None:
    """Read current capture gain in dB.  Returns None if no control found."""
    controls = discover_capture_controls(device)
    for c in controls:
        if c.get("db_value") is not None:
            return c["db_value"]
    # Percentage-only controls: convert 0-100 % → roughly -10..+40 dB scale
    for c in controls:
        if c.get("pct_value") is not None:
            return round(c["pct_value"] / 100 * 50 - 10)
    return None


def set_capture_gain(device: AudioDevice, gain_db: int) -> bool:
    """Set capture gain via amixer sset.

    Tries every discovered capture control, then the known-name fallback list.
    For each control tries the dB string first, then a percentage equivalent
    (some devices only accept one format).
    """
    pct = max(0, min(100, round((gain_db + 10) / 50 * 100)))

    # Prefer controls the device actually reports as having capture capability.
    discovered = [c["name"] for c in discover_capture_controls(device)]
    all_controls = discovered + [n for n in _CAPTURE_CONTROLS if n not in discovered]

    for name in all_controls:
        if _amixer_sset(device.card, name, f"{gain_db}dB"):
            return True
        if _amixer_sset(device.card, name, f"{pct}%"):
            return True

    return False
