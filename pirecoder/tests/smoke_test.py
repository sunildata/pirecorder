#!/usr/bin/env python3
"""Smoke test — exercises the app without any audio hardware.

Verifies that the application boots, every route is reachable, auth is
enforced, the level analyser produces correct numbers, and the WAV repair
path actually fixes a truncated header.

    python tests/smoke_test.py
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point at a scratch directory so a real library is never touched.
_TMP = tempfile.mkdtemp(prefix="zoompi-test-")
os.environ["ZOOMPI_DATA"] = str(Path(_TMP) / "data")
os.environ["ZOOMPI_RECORDINGS"] = str(Path(_TMP) / "recordings")

PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 58)


# ── Level analyser ───────────────────────────────────────────────────────────

def test_levels() -> None:
    section("Level analyser (audioop-free)")
    from zoompi.levels import LevelMeter, _to_dbfs

    # Full-scale sine on L, silence on R.
    frames = 4800
    data = bytearray()
    for i in range(frames):
        left = int(32767 * math.sin(2 * math.pi * 440 * i / 48000))
        data += struct.pack("<hh", left, 0)

    peaks, rms = LevelMeter._analyse(bytes(data), channels=2, width=2)

    check("L peak near full scale", peaks[0] > 0.98, f"got {peaks[0]:.3f}")
    check("R peak is silent", peaks[1] == 0.0, f"got {peaks[1]:.3f}")
    # RMS of a sine is amplitude / sqrt(2), about 0.707.
    check("L RMS approx 0.707", abs(rms[0] - 0.707) < 0.02, f"got {rms[0]:.3f}")
    check("R RMS is zero", rms[1] == 0.0, f"got {rms[1]:.3f}")

    check("0 dBFS maps to 0", abs(_to_dbfs(1.0)) < 0.01)
    check("half scale approx -6 dB", abs(_to_dbfs(0.5) + 6.02) < 0.1)
    check("silence floors at -90", _to_dbfs(0.0) == -90.0)

    # Mono input must drive both meters.
    mono = b"".join(struct.pack("<h", 16000) for _ in range(1000))
    mp, _ = LevelMeter._analyse(mono, channels=1, width=2)
    check("mono feeds both meters", mp[0] == mp[1] and mp[0] > 0.4)

    # 24-bit decode path.
    tri = bytearray()
    for _ in range(300):
        for _ch in range(2):
            tri += (0x7FFFFF).to_bytes(3, "little")
    p24, _ = LevelMeter._analyse(bytes(tri), channels=2, width=3)
    check("24-bit decodes to full scale", p24[0] > 0.99, f"got {p24[0]:.3f}")


# ── WAV repair ───────────────────────────────────────────────────────────────

def test_wav_repair() -> None:
    section("Crash recovery - WAV header repair")
    from zoompi.recorder import _wav_header, repair_wav
    from zoompi.storage import wav_duration

    path = Path(_TMP) / "broken.wav"
    audio = b"\x00\x01" * 48000  # 0.5 s of stereo 16-bit at 48 kHz

    # Simulate a power cut: arecord never patched the length fields, so the
    # header claims zero bytes even though the samples are all on disk.
    path.write_bytes(_wav_header(48000, 2, 16, 0) + audio)

    stale = struct.unpack("<I", path.read_bytes()[40:44])[0]
    check("header starts stale at 0", stale == 0, f"got {stale}")
    # Our own listing measures from file size, so it survives a bad header --
    # but external players trust the header, which is why repair matters.
    check("listing tolerates bad header", abs(wav_duration(path) - 0.5) < 0.01)

    check("repair succeeds", repair_wav(path, 48000, 2, 16))

    header = path.read_bytes()[:44]
    size = struct.unpack("<I", header[40:44])[0]
    check("data chunk size rebuilt", size == len(audio), f"got {size}")
    riff = struct.unpack("<I", header[4:8])[0]
    check("RIFF size rebuilt", riff == len(audio) + 36, f"got {riff}")
    check("duration reads 0.5 s", abs(wav_duration(path) - 0.5) < 0.01)

    check("repair on missing file is safe", not repair_wav(Path(_TMP) / "nope.wav", 48000, 2, 16))


# ── Config ───────────────────────────────────────────────────────────────────

def test_config() -> None:
    section("Configuration")
    from zoompi.config import Config

    path = Path(_TMP) / "cfg.json"
    cfg = Config(path)

    check("defaults load", cfg.get("sample_rate") == 48000)
    cfg.set("sample_rate", 44100)
    check("value persists", Config(path).get("sample_rate") == 44100)

    try:
        cfg.set("not_a_setting", 1)
        check("unknown key rejected", False)
    except KeyError:
        check("unknown key rejected", True)

    result = cfg.update({"channels": 1, "bogus": True})
    check("update applies known keys", result["applied"] == {"channels": 1})
    check("update reports unknown keys", result["rejected"] == ["bogus"])

    check("secrets redacted", "password" not in cfg.as_dict())

    # A corrupt file must fall back to defaults rather than crash.
    path.write_text("{ this is not json", encoding="utf-8")
    check("corrupt config recovers", Config(path).get("sample_rate") == 48000)


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_auth() -> None:
    section("Authentication")
    from zoompi.auth import hash_password, verify_password

    stored = hash_password("hunter2")
    check("correct password verifies", verify_password("hunter2", stored))
    check("wrong password rejected", not verify_password("hunter3", stored))
    check("hash is salted", hash_password("x") != hash_password("x"))
    check("plaintext fallback works", verify_password("zoompi", "zoompi"))
    check("empty stored value rejected", not verify_password("x", ""))


# ── Storage path safety ──────────────────────────────────────────────────────

def test_storage_safety() -> None:
    section("Storage path traversal")
    from zoompi.storage import StorageError, _resolve

    for folder, filename in [
        ("..", "passwd"),
        ("2026-01-01", "../../../etc/passwd"),
        ("2026-01-01", "sub/dir.wav"),
        ("", "x.wav"),
    ]:
        try:
            _resolve(folder, filename)
            check(f"blocks {folder!r}/{filename!r}", False)
        except StorageError:
            check(f"blocks {folder!r}/{filename!r}", True)

    try:
        _resolve("2026-01-01", "take.wav")
        check("allows legitimate path", True)
    except StorageError as exc:
        check("allows legitimate path", False, str(exc))


# ── HTTP surface ─────────────────────────────────────────────────────────────

def test_http() -> None:
    section("HTTP routes")
    from zoompi.app import create_app
    from zoompi.config import config

    config.set("auth_enabled", True)
    config.set("password", "testpw")

    app, _ = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    r = client.get("/api/health")
    check("health is public", r.status_code == 200, f"got {r.status_code}")
    check("health returns JSON", r.get_json().get("ok") is True)

    r = client.get("/api/status")
    check("status requires auth", r.status_code == 401, f"got {r.status_code}")

    r = client.post("/api/record/start", json={})
    check("record/start requires auth", r.status_code == 401)

    r = client.get("/")
    check("dashboard redirects to login", r.status_code == 302)

    r = client.post("/api/login", json={"password": "wrong"})
    check("bad password rejected", r.status_code == 401)

    r = client.post("/api/login", json={"password": "testpw"})
    check("good password accepted", r.status_code == 200, f"got {r.status_code}")

    for path in ("/api/status", "/api/settings", "/api/recordings",
                 "/api/system", "/api/devices", "/api/folders", "/api/jobs",
                 "/api/sessions", "/api/wifi/status", "/api/hardware"):
        r = client.get(path)
        check(f"GET {path}", r.status_code == 200, f"got {r.status_code}")

    for path in ("/", "/files", "/settings"):
        r = client.get(path)
        check(f"page {path}", r.status_code == 200, f"got {r.status_code}")

    # Stopping when idle is a state conflict, not a crash.
    r = client.post("/api/record/stop", json={})
    check("stop while idle returns 409", r.status_code == 409, f"got {r.status_code}")

    r = client.post("/api/settings", json={"device_name": "TestPi"})
    check("settings update accepted", r.status_code == 200)
    check("setting took effect", client.get("/api/settings")
          .get_json()["settings"]["device_name"] == "TestPi")

    r = client.get("/api/recordings/..%2F..%2Fetc/passwd/download")
    check("traversal blocked over HTTP", r.status_code in (400, 404), f"got {r.status_code}")

    r = client.get("/api/nonexistent")
    check("unknown API path returns JSON 404", r.status_code == 404)


# ── Database ─────────────────────────────────────────────────────────────────

def test_db() -> None:
    section("Database")
    from zoompi import db

    db.init_db()
    db.save_session({
        "session_id": "2026-07-26_10-00-00",
        "base_name": "test-take",
        "folder": "2026-07-26",
        "sample_rate": 48000, "bit_depth": 16, "channels": 2,
        "started_at": 1000.0, "stopped_at": 1060.0,
        "duration": 60.0, "size_bytes": 11520000,
        "markers": [{"offset_seconds": 12.5, "label": "chorus"}],
        "segments": [{"filename": "a.wav"}],
    })

    s = db.get_session("2026-07-26_10-00-00")
    check("session round-trips", s is not None and s["duration"] == 60.0)
    check("marker stored", s and len(s["markers"]) == 1)
    check("marker offset correct", s and s["markers"][0]["offset_seconds"] == 12.5)

    check("session listed", any(
        x["session_id"] == "2026-07-26_10-00-00" for x in db.list_sessions()))

    db.update_session_meta("2026-07-26_10-00-00", event_name="Sunday Service")
    check("metadata updates",
          db.get_session("2026-07-26_10-00-00")["event_name"] == "Sunday Service")

    db.log_event("test_event", {"k": "v"})
    check("event logged", any(e["kind"] == "test_event" for e in db.recent_events()))

    db.save_network("TestNet", "secret123", priority=5)
    check("network saved", any(n["ssid"] == "TestNet" for n in db.list_networks()))
    check("psk retrievable", db.get_network_psk("TestNet") == "secret123")
    check("psk not exposed in list", "psk" not in db.list_networks()[0])


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 58)
    print("  ZoomPi smoke test")
    print(f"  scratch dir: {_TMP}")
    print("=" * 58)

    for fn in (test_levels, test_wav_repair, test_config, test_auth,
               test_storage_safety, test_db, test_http):
        try:
            fn()
        except Exception as exc:
            global FAILED
            FAILED += 1
            print(f"  ERROR in {fn.__name__}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 58)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 58)

    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
