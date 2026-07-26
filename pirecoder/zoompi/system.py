"""Device telemetry: temperature, storage, power, network, uptime."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .config import RECORDINGS_DIR

_BOOT = time.time()


def _run(cmd: list[str], timeout: int = 4) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def cpu_temperature() -> float | None:
    """Degrees Celsius, or None off-Pi."""
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            return round(int(Path(path).read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    out = _run(["vcgencmd", "measure_temp"])
    m = re.search(r"([\d.]+)", out)
    return round(float(m.group(1)), 1) if m else None


def cpu_usage() -> float:
    """Percent busy over a 200 ms window."""
    def snapshot() -> tuple[int, int]:
        try:
            parts = Path("/proc/stat").read_text().split("\n")[0].split()[1:]
            vals = [int(v) for v in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
        except (OSError, ValueError, IndexError):
            return 0, 0

    t1, i1 = snapshot()
    time.sleep(0.2)
    t2, i2 = snapshot()
    dt, di = t2 - t1, i2 - i1
    return round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0


def memory_usage() -> dict:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0) // 1024
        available = info.get("MemAvailable", 0) // 1024
        used = total - available
        return {
            "total_mb": total,
            "used_mb": used,
            "percent": round(100.0 * used / total, 1) if total else 0.0,
        }
    except (OSError, ValueError, KeyError):
        return {"total_mb": 0, "used_mb": 0, "percent": 0.0}


def storage() -> dict:
    """Capacity of the volume holding the recordings."""
    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(RECORDINGS_DIR))
        total_mb = usage.total // (1024 * 1024)
        free_mb = usage.free // (1024 * 1024)
        used_mb = total_mb - free_mb
        percent = round(100.0 * used_mb / total_mb, 1) if total_mb else 0.0
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "free_mb": free_mb,
            "percent_used": percent,
            "recording_hours_left": _hours_remaining(free_mb),
        }
    except OSError:
        return {
            "total_mb": 0, "used_mb": 0, "free_mb": 0,
            "percent_used": 0.0, "recording_hours_left": 0.0,
        }


def _hours_remaining(free_mb: int, rate: int = 48000, channels: int = 2, depth: int = 16) -> float:
    """How much stereo WAV the remaining space holds."""
    mb_per_hour = (rate * channels * (depth // 8) * 3600) / (1024 * 1024)
    return round(free_mb / mb_per_hour, 1) if mb_per_hour else 0.0


def battery() -> dict:
    """Battery state if a UPS HAT exposes one.

    Supports the standard Linux power_supply class and the common INA219
    fuel gauges used by PiSugar / Waveshare UPS boards. Returns
    ``present: False`` when running on mains only.
    """
    base = Path("/sys/class/power_supply")
    if base.exists():
        for entry in base.iterdir():
            cap = entry / "capacity"
            if cap.exists():
                try:
                    pct = int(cap.read_text().strip())
                    st = (entry / "status")
                    return {
                        "present": True,
                        "percent": pct,
                        "status": st.read_text().strip() if st.exists() else "Unknown",
                        "source": entry.name,
                    }
                except (OSError, ValueError):
                    continue

    # PiSugar exposes a local socket rather than sysfs.
    try:
        with socket.create_connection(("127.0.0.1", 8423), timeout=0.5) as sock:
            sock.sendall(b"get battery\n")
            reply = sock.recv(128).decode(errors="replace")
            m = re.search(r"([\d.]+)", reply)
            if m:
                return {
                    "present": True,
                    "percent": int(float(m.group(1))),
                    "status": "Discharging",
                    "source": "pisugar",
                }
    except (OSError, ValueError):
        pass

    return {"present": False, "percent": None, "status": "AC", "source": None}


def throttled() -> dict:
    """Pi under-voltage / thermal throttle flags — a common cause of dropouts."""
    out = _run(["vcgencmd", "get_throttled"])
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return {"available": False}
    bits = int(m.group(1), 16)
    return {
        "available": True,
        "under_voltage_now": bool(bits & 0x1),
        "throttled_now": bool(bits & 0x4),
        "under_voltage_since_boot": bool(bits & 0x10000),
        "throttled_since_boot": bool(bits & 0x40000),
    }


def ip_addresses() -> list[dict]:
    out = _run(["ip", "-4", "-o", "addr", "show"])
    results = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] != "lo":
            results.append({"interface": parts[1], "address": parts[3].split("/")[0]})
    if not results:
        try:
            results.append({"interface": "unknown", "address": socket.gethostbyname(socket.gethostname())})
        except OSError:
            pass
    return results


def primary_ip() -> str:
    """The address a phone on the LAN would actually reach."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        addrs = ip_addresses()
        return addrs[0]["address"] if addrs else "127.0.0.1"


def uptime() -> dict:
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        secs = time.time() - _BOOT
    return {"seconds": int(secs), "human": _human_duration(secs)}


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def snapshot() -> dict:
    """Everything the dashboard header needs in one call."""
    return {
        "cpu_percent": cpu_usage(),
        "cpu_temp_c": cpu_temperature(),
        "memory": memory_usage(),
        "storage": storage(),
        "battery": battery(),
        "throttled": throttled(),
        "ip": primary_ip(),
        "interfaces": ip_addresses(),
        "uptime": uptime(),
        "hostname": socket.gethostname(),
        "timestamp": time.time(),
    }
