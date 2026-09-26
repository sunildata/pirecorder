"""Wi-Fi mode manager built on NetworkManager.

Raspberry Pi OS Bookworm ships NetworkManager, whose `nmcli` handles both
client connections and AP mode. That matters here because the Pi 3's single
radio cannot run a reliable access point and a station link at the same time
— so the three requested modes are implemented as a priority chain rather
than concurrently:

    1. Try saved networks (highest priority first)
    2. Try the phone hotspot entry
    3. Fall back to hosting our own AP

Every operation is deliberately isolated from the recorder. Switching Wi-Fi
never touches `arecord`, so a network change mid-take cannot interrupt audio.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from . import db
from .config import config

AP_CONNECTION = "SPstudio-ap"
_lock = threading.RLock()


PERMISSION_HINT = (
    "NetworkManager denied the request. The service runs as a systemd unit "
    "with no login session, so polkit blocks network changes by default. "
    "Run: sudo bash install.sh  (installs the required polkit rule)"
)


def _looks_like_permission_error(text: str) -> bool:
    lowered = text.lower()
    return any(
        s in lowered
        for s in ("not authorized", "insufficient privileges", "permission denied",
                  "access denied", "authorization")
    )


def _nmcli(args: list[str], timeout: int = 25) -> tuple[int, str, str]:
    """Run nmcli, retrying through sudo if polkit refuses.

    A systemd service has no active local session, which is exactly the
    condition NetworkManager's default polkit policy refuses. The installed
    polkit rule normally handles this; the sudo retry is a fallback for
    systems where that rule is missing.
    """
    try:
        p = subprocess.run(
            ["nmcli"] + args, capture_output=True, text=True, timeout=timeout
        )
        rc, out, err = p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "nmcli not found (NetworkManager not installed)"
    except subprocess.TimeoutExpired:
        return 124, "", "nmcli timed out"
    except OSError as exc:
        return 1, "", str(exc)

    if rc != 0 and _looks_like_permission_error(err):
        try:
            p = subprocess.run(
                ["sudo", "-n", "nmcli"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if p.returncode == 0:
                return 0, p.stdout.strip(), ""
            return rc, out, f"{err}\n{PERMISSION_HINT}"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return rc, out, f"{err}\n{PERMISSION_HINT}"

    return rc, out, err


def available() -> bool:
    rc, _, _ = _nmcli(["--version"], timeout=5)
    return rc == 0


def wifi_interface() -> str | None:
    rc, out, _ = _nmcli(["-t", "-f", "DEVICE,TYPE", "device"])
    if rc != 0:
        return None
    for line in out.splitlines():
        dev, _, dtype = line.partition(":")
        if dtype == "wifi":
            return dev
    return None


def status() -> dict:
    """Current connection state, mode, and signal."""
    iface = wifi_interface()
    info = {
        "available": available(),
        "interface": iface,
        "mode": "unknown",
        "ssid": None,
        "signal": None,
        "ip": None,
        "connected": False,
    }
    if not iface:
        return info

    rc, out, _ = _nmcli(
        ["-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", iface]
    )
    if rc == 0:
        profile = None
        state_ok = False
        for line in out.splitlines():
            key, _, value = line.partition(":")
            if key == "GENERAL.STATE":
                # nmcli reports e.g. "100 (connected)". Anything lower means
                # disconnected, connecting, or a failing retry loop -- all of
                # which must count as "not connected" so fallback can run.
                state_ok = value.strip().startswith("100")
            elif key == "GENERAL.CONNECTION" and value not in ("", "--"):
                profile = value
            elif key.startswith("IP4.ADDRESS"):
                info["ip"] = value.split("/")[0]

        info["ssid"] = profile
        info["connected"] = state_ok and profile is not None

    rc, out, _ = _nmcli(["-t", "-f", "ACTIVE,SSID,SIGNAL,MODE", "device", "wifi", "list"])
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == "yes":
                info["ssid"] = parts[1] or info["ssid"]
                info["signal"] = int(parts[2]) if parts[2].isdigit() else None
                info["mode"] = "ap" if parts[3].lower() == "ap" else "client"

    if info["ssid"] == AP_CONNECTION or info["ssid"] == config.get("ap_ssid"):
        info["mode"] = "ap"
    return info


def scan(rescan: bool = True) -> list[dict]:
    """Nearby networks, strongest first, deduplicated by SSID."""
    if rescan:
        _nmcli(["device", "wifi", "rescan"], timeout=20)
        time.sleep(1.5)

    rc, out, _ = _nmcli(
        ["-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"]
    )
    if rc != 0:
        return []

    seen: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3 or not parts[0]:
            continue
        ssid = parts[0]
        signal = int(parts[1]) if parts[1].isdigit() else 0
        entry = {
            "ssid": ssid,
            "signal": signal,
            "security": parts[2] or "open",
            "in_use": len(parts) > 3 and parts[3].strip() == "*",
        }
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = entry

    known = {n["ssid"] for n in db.list_networks()}
    results = list(seen.values())
    for r in results:
        r["saved"] = r["ssid"] in known
    results.sort(key=lambda r: r["signal"], reverse=True)
    return results


def connect(ssid: str, password: str = "", save: bool = True,
            is_hotspot: bool = False, priority: int = 0) -> dict:
    with _lock:
        iface = wifi_interface()
        if not iface:
            return {"ok": False, "error": "No Wi-Fi interface found"}

        args = ["device", "wifi", "connect", ssid, "ifname", iface]
        if password:
            args += ["password", password]

        rc, out, err = _nmcli(args, timeout=45)
        if rc != 0:
            return {"ok": False, "error": err or out or "Connection failed"}

        if save:
            db.save_network(ssid, password, priority=priority, is_hotspot=is_hotspot)
        db.log_event("wifi_connected", {"ssid": ssid})
        return {"ok": True, "ssid": ssid, "status": status()}


def start_ap(ssid: str = "", password: str = "", channel: int = 0) -> dict:
    """Host an access point so a phone can reach the recorder with no router."""
    with _lock:
        iface = wifi_interface()
        if not iface:
            return {"ok": False, "error": "No Wi-Fi interface found"}

        ssid = ssid or config.get("ap_ssid")
        password = password or config.get("ap_password")
        channel = channel or int(config.get("ap_channel"))

        if len(password) < 8:
            return {"ok": False, "error": "AP password must be at least 8 characters"}

        _nmcli(["connection", "delete", AP_CONNECTION], timeout=10)

        rc, _, err = _nmcli(
            [
                "connection", "add",
                "type", "wifi",
                "ifname", iface,
                "con-name", AP_CONNECTION,
                "autoconnect", "no",
                "ssid", ssid,
                "802-11-wireless.mode", "ap",
                "802-11-wireless.band", "bg",
                "802-11-wireless.channel", str(channel),
                "ipv4.method", "shared",
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", password,
            ],
            timeout=20,
        )
        if rc != 0:
            return {"ok": False, "error": err or "Failed to define AP"}

        rc, _, err = _nmcli(["connection", "up", AP_CONNECTION], timeout=30)
        if rc != 0:
            return {"ok": False, "error": err or "Failed to start AP"}

        db.log_event("wifi_ap_started", {"ssid": ssid})
        return {"ok": True, "mode": "ap", "ssid": ssid, "status": status()}


def stop_ap() -> dict:
    with _lock:
        _nmcli(["connection", "down", AP_CONNECTION], timeout=15)
        return {"ok": True, "status": status()}


def _link_alive() -> bool:
    """Return True only when the Wi-Fi link has a real routable IP.

    nmcli can report GENERAL.STATE=100 (connected) for up to ~90 seconds
    after a hotspot disappears because NetworkManager hasn't declared the
    link dead yet. Checking for a non-empty IP4 address is a reliable proxy:
    if DHCP lost its lease the address field goes blank immediately.
    """
    iface = wifi_interface()
    if not iface:
        return False
    rc, out, _ = _nmcli(
        ["-t", "-f", "GENERAL.STATE,IP4.ADDRESS", "device", "show", iface]
    )
    if rc != 0:
        return False
    state_ok = False
    has_ip = False
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key == "GENERAL.STATE":
            state_ok = value.strip().startswith("100")
        elif key.startswith("IP4.ADDRESS") and value.strip():
            has_ip = True
    return state_ok and has_ip


def auto_connect() -> dict:
    """Priority chain: saved networks, then hotspot entries, then own AP.

    Called at boot and whenever the link is lost. Runs on its own thread so a
    slow scan never stalls the web server.

    When no saved network is visible on the first scan we wait up to
    _SCAN_RETRY_SECS and try again before giving up and starting the AP.
    This handles the common case where a mobile hotspot takes 15-30 s to
    start broadcasting after it is switched on.
    """
    _SCAN_RETRY_SECS = 30   # how long to keep retrying before AP fallback
    _SCAN_RETRY_INT  = 10   # seconds between retry scans

    with _lock:
        mode = config.get("wifi_mode")
        if mode == "ap":
            return start_ap()

        current = status()
        if current["connected"] and current["mode"] == "client" and _link_alive():
            return {"ok": True, "reason": "already connected", "status": current}

        saved = db.list_networks()
        if not saved:
            if mode == "client":
                return {"ok": False, "error": "No saved networks"}
            db.log_event("wifi_fallback_ap", {"reason": "no saved networks"})
            return start_ap()

        # Non-hotspot saved networks first, then phone hotspots.
        ordered = sorted(saved, key=lambda n: (n["is_hotspot"], -n["priority"]))

        # Retry scanning for up to _SCAN_RETRY_SECS so a hotspot that is
        # still warming up gets a second (and third) chance to appear.
        deadline = time.monotonic() + _SCAN_RETRY_SECS
        attempt = 0
        while True:
            attempt += 1
            visible = {n["ssid"] for n in scan(rescan=True)}

            for net in ordered:
                if net["ssid"] not in visible:
                    continue
                psk = db.get_network_psk(net["ssid"]) or ""
                result = connect(net["ssid"], psk, save=False)
                if result.get("ok"):
                    return result

            # All saved networks tried and none connected — check if we still
            # have time for another scan pass before declaring defeat.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            db.log_event("wifi_scan_retry", {"attempt": attempt, "remaining_s": int(remaining)})
            # Release the lock while we wait so other callers aren't blocked.
            _lock.release()
            try:
                time.sleep(min(_SCAN_RETRY_INT, remaining))
            finally:
                _lock.acquire()

        if mode == "client":
            return {"ok": False, "error": "No saved network in range"}

        db.log_event("wifi_fallback_ap", {"reason": "no known network in range"})
        return start_ap()


def forget(ssid: str) -> dict:
    with _lock:
        _nmcli(["connection", "delete", ssid], timeout=15)
        db.delete_network(ssid)
        return {"ok": True, "forgotten": ssid}


class WifiWatchdog:
    """Brings the network up at boot, then restores it whenever it drops.

    Recording is never consulted or interrupted — this only restores the
    control channel so the phone can reconnect and see live status.

    Interval is 15 s (down from 45 s) so a dropped hotspot is detected and
    recovered within ~15 s rather than up to a minute. The real link check
    (_link_alive) catches a dead hotspot as soon as DHCP loses its lease,
    rather than waiting for NetworkManager's own slow timeout.
    """

    def __init__(self, interval: float = 15.0, startup_delay: float = 10.0) -> None:
        self._interval = interval
        # NetworkManager needs a moment after boot to finish its own attempt
        # at the saved networks; checking instantly would race it.
        self._startup_delay = startup_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wifi-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Run the first check shortly after boot rather than one full interval
        # later. Without this, a Pi with no known network sat unreachable for
        # a full interval because nothing had started the fallback AP yet.
        if self._stop.wait(self._startup_delay):
            return
        self._check(first=True)

        while not self._stop.wait(self._interval):
            self._check()

    def _check(self, first: bool = False) -> None:
        try:
            if not available():
                return
            st = status()

            # Use _link_alive() instead of st["connected"] so we catch the
            # common case where nmcli still says "connected" after a hotspot
            # disappears but DHCP has already lost the IP address.
            if st["connected"] and st["mode"] == "client" and _link_alive():
                if first:
                    db.log_event("wifi_ready", {"ssid": st["ssid"], "mode": st["mode"]})
                return

            # If we're already in AP mode and the user configured ap-only, leave it.
            if st["mode"] == "ap" and config.get("wifi_mode") == "ap":
                return

            db.log_event(
                "wifi_reconnecting",
                {"reason": "boot" if first else "link lost"},
            )
            result = auto_connect()
            if not result.get("ok"):
                db.log_event("wifi_reconnect_failed", {"error": result.get("error", "")[:300]})
        except Exception as exc:
            db.log_event("wifi_watchdog_error", {"error": str(exc)[:200]})
