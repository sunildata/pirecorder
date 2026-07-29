"""REST API blueprint.

Error contract: every endpoint returns JSON. Failures carry an ``error``
string and a meaningful status code — 400 for bad input, 401 unauthenticated,
404 missing, 409 for a state conflict (e.g. already recording), 500 otherwise.
"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request, send_file

from . import audio_devices, db, storage, system, wifi
from .auth import check_credentials, login_required, login_session, logout_session, set_password
from .config import RECORDINGS_DIR, config
from .postprocess import ffmpeg_available, processor
from .recorder import RecorderError
from .storage import StorageError

api = Blueprint("api", __name__, url_prefix="/api")

# Injected by create_app so handlers can reach the singletons.
_ctx: dict = {}


def bind(recorder, meter, hardware=None) -> None:
    _ctx["recorder"] = recorder
    _ctx["meter"] = meter
    _ctx["hardware"] = hardware


def _recorder():
    return _ctx["recorder"]


def _meter():
    return _ctx["meter"]


def _body() -> dict:
    return request.get_json(silent=True) or {}


@api.errorhandler(RecorderError)
def _recorder_error(exc):
    return jsonify({"error": str(exc)}), 409


@api.errorhandler(StorageError)
def _storage_error(exc):
    return jsonify({"error": str(exc)}), 400


# ── Auth ─────────────────────────────────────────────────────────────────────

@api.post("/login")
def login():
    password = _body().get("password", "")
    if not check_credentials(password):
        time.sleep(0.5)  # blunt the obvious brute-force
        db.log_event("login_failed", {"ip": request.remote_addr})
        return jsonify({"error": "Incorrect password"}), 401
    login_session()
    db.log_event("login_ok", {"ip": request.remote_addr})
    return jsonify({"ok": True})


@api.post("/logout")
def logout():
    logout_session()
    return jsonify({"ok": True})


@api.post("/password")
@login_required
def change_password():
    data = _body()
    try:
        set_password(data.get("new_password", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


# ── Recording control ────────────────────────────────────────────────────────

@api.get("/status")
@login_required
def status():
    """Poll fallback for clients whose WebSocket dropped."""
    rec = _recorder()
    return jsonify(
        {
            **rec.status(),
            "levels": _meter().read(),
            "server_time": time.time(),
        }
    )


@api.post("/record/start")
@login_required
def record_start():
    data = _body()
    result = _recorder().start(
        label=str(data.get("label", ""))[:60],
        notes=str(data.get("notes", ""))[:2000],
    )
    _meter().reset_clip()
    return jsonify(result)


@api.post("/record/stop")
@login_required
def record_stop():
    data = _body()
    if config.get("recording_lock") and not data.get("confirm"):
        return jsonify(
            {
                "error": "Recording is locked",
                "requires_confirmation": True,
                "hint": "Resend with {\"confirm\": true}",
            }
        ), 409

    session = _recorder().stop()
    db.save_session(session)

    # Queue optional MP3 / DSP renders; the master WAV is already safe.
    queued = []
    wants_mp3 = "mp3" in config.get("output_format")
    wants_dsp = int(config.get("post_highpass_hz") or 0) > 0 or any(
        config.get(k) for k in ("post_limiter", "post_compressor", "post_noise_gate")
    )
    folder = RECORDINGS_DIR / session["folder"]
    for seg in session.get("segments", []):
        path = folder / seg["filename"]
        if not path.exists():
            continue
        if wants_mp3:
            queued.append(processor.convert_to_mp3(path).to_dict())
        if wants_dsp:
            queued.append(processor.apply_dsp(path).to_dict())

    return jsonify({"session": session, "jobs": queued})


@api.post("/record/pause")
@login_required
def record_pause():
    return jsonify(_recorder().pause())


@api.post("/record/resume")
@login_required
def record_resume():
    return jsonify(_recorder().resume())


@api.post("/record/marker")
@login_required
def record_marker():
    label = str(_body().get("label", ""))[:80]
    return jsonify(_recorder().add_marker(label))


@api.post("/record/notes")
@login_required
def record_notes():
    notes = str(_body().get("notes", ""))[:2000]
    return jsonify(_recorder().set_notes(notes))


@api.get("/levels")
@login_required
def levels():
    return jsonify(_meter().read())


@api.post("/levels/reset-clip")
@login_required
def reset_clip():
    _meter().reset_clip()
    return jsonify({"ok": True})


def _gain_payload(control, supported: bool) -> dict:
    """Uniform gain response. `percent` is authoritative; `db` is informational
    and only present when the hardware reports it."""
    if control is None:
        return {"supported": supported, "percent": 0, "db": None, "control": None}
    return {
        "supported": True,
        "percent": control.percent,
        "db": control.db,
        "control": control.name,
    }


@api.get("/gain")
@login_required
def get_gain():
    # probe=False: fast card lookup — the arecord format tests aren't needed
    # for mixer access and made every slider move take seconds.
    dev = audio_devices.select_device(config.get("audio_device"), probe=False)
    if not dev:
        return jsonify(_gain_payload(None, False))
    return jsonify(_gain_payload(audio_devices.get_capture_gain(dev), False))


@api.post("/gain")
@login_required
def set_gain():
    percent = _body().get("percent")
    if percent is None:
        return jsonify({"error": "percent is required"}), 400
    try:
        percent = int(percent)
    except (ValueError, TypeError):
        return jsonify({"error": "percent must be an integer"}), 400
    percent = max(0, min(100, percent))

    dev = audio_devices.select_device(config.get("audio_device"), probe=False)
    if not dev:
        return jsonify({"error": "No audio device found"}), 404

    control = audio_devices.set_capture_gain(dev, percent)
    if control is None:
        return jsonify({
            **_gain_payload(None, False),
            "ok": False,
            "reason": "This interface has no software capture volume — "
                      "its input gain is analogue only.",
        })

    payload = _gain_payload(control, True)
    # The device may quantise to its own step size, so treat "close enough"
    # as success rather than reporting a failure the user cannot act on.
    payload["ok"] = abs(control.percent - percent) <= 5
    payload["requested"] = percent
    return jsonify(payload)


@api.post("/gain/zero_db")
@login_required
def set_gain_zero_db():
    """Find and apply the percent value that puts the capture gain closest to 0 dB."""
    dev = audio_devices.select_device(config.get("audio_device"), probe=False)
    if not dev:
        return jsonify({"error": "No audio device found"}), 404

    control = audio_devices.find_zero_db_percent(dev)
    if control is None:
        return jsonify({
            **_gain_payload(None, False),
            "ok": False,
            "reason": "This interface does not report dB values — "
                      "0 dB reset is not available.",
        })

    payload = _gain_payload(control, True)
    payload["ok"] = True
    return jsonify(payload)


@api.get("/gain/controls")
@login_required
def gain_controls():
    """Diagnostics: every capture volume control amixer reports for the device."""
    dev = audio_devices.select_device(config.get("audio_device"), probe=False)
    if not dev:
        return jsonify({"error": "No audio device found", "controls": []}), 404
    return jsonify({
        "device": dev.name,
        "card": dev.card,
        "controls": [c.to_dict() for c in audio_devices.list_capture_controls(dev)],
    })


# ── Files ────────────────────────────────────────────────────────────────────

@api.get("/recordings")
@login_required
def list_recordings():
    return jsonify(
        {
            "files": storage.list_recordings(
                search=request.args.get("search", ""),
                folder=request.args.get("folder", ""),
                sort=request.args.get("sort", "date"),
                order=request.args.get("order", "desc"),
            ),
            "stats": storage.stats(),
        }
    )


@api.get("/folders")
@login_required
def list_folders():
    return jsonify(storage.list_folders())


@api.get("/recordings/<folder>/<filename>/download")
@login_required
def download(folder: str, filename: str):
    path = storage.get_path(folder, filename)
    return send_file(str(path), as_attachment=True, download_name=filename)


@api.get("/recordings/<folder>/<filename>/stream")
@login_required
def stream(folder: str, filename: str):
    """Inline playback. `conditional` gives us HTTP range support so the
    browser can seek without pulling a multi-gigabyte file."""
    path = storage.get_path(folder, filename)
    mime = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
    return send_file(str(path), mimetype=mime, as_attachment=False, conditional=True)


@api.delete("/recordings/<folder>/<filename>")
@login_required
def delete_recording(folder: str, filename: str):
    return jsonify(storage.delete(folder, filename))


@api.post("/recordings/delete-many")
@login_required
def delete_many():
    items = _body().get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    return jsonify(storage.delete_many(items))


@api.post("/recordings/<folder>/<filename>/rename")
@login_required
def rename_recording(folder: str, filename: str):
    new_name = str(_body().get("new_name", ""))
    return jsonify(storage.rename(folder, filename, new_name))


@api.post("/recordings/export")
@login_required
def export_zip():
    items = _body().get("items", [])
    if not items:
        return jsonify({"error": "No files selected"}), 400
    buf = storage.export_zip(items)
    name = f"recordings-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)


@api.get("/folders/<folder>/export")
@login_required
def export_folder(folder: str):
    buf = storage.export_folder_zip(folder)
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"{folder}.zip",
    )


@api.post("/storage/cleanup")
@login_required
def cleanup():
    return jsonify(storage.run_cleanup(force=bool(_body().get("force"))))


# ── Sessions ─────────────────────────────────────────────────────────────────

@api.get("/sessions")
@login_required
def sessions():
    return jsonify(db.list_sessions())


@api.get("/sessions/<session_id>")
@login_required
def session_detail(session_id: str):
    data = db.get_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(data)


@api.post("/sessions/<session_id>")
@login_required
def update_session(session_id: str):
    data = _body()
    ok = db.update_session_meta(
        session_id,
        event_name=data.get("event_name"),
        notes=data.get("notes"),
    )
    if not ok:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"ok": True})


# ── System ───────────────────────────────────────────────────────────────────

@api.get("/system")
@login_required
def system_info():
    return jsonify(system.snapshot())


@api.get("/system/events")
@login_required
def system_events():
    return jsonify(db.recent_events(limit=int(request.args.get("limit", 100))))


@api.get("/devices")
@login_required
def devices():
    found = audio_devices.list_devices(probe=True)
    active = audio_devices.select_device(config.get("audio_device"))
    return jsonify(
        {
            "devices": [d.to_dict() for d in found],
            "active": active.to_dict() if active else None,
        }
    )


@api.get("/jobs")
@login_required
def jobs():
    return jsonify(processor.jobs())


@api.get("/health")
def health():
    """Unauthenticated so systemd and uptime monitors can probe it."""
    rec = _ctx.get("recorder")
    return jsonify(
        {
            "ok": True,
            "recording": bool(rec and rec.is_recording),
            "uptime": system.uptime(),
            "storage_free_mb": system.storage()["free_mb"],
        }
    )


# ── Settings ─────────────────────────────────────────────────────────────────

@api.get("/settings")
@login_required
def get_settings():
    active = audio_devices.select_device(config.get("audio_device"))
    return jsonify(
        {
            "settings": config.as_dict(),
            "capabilities": {
                "rates": active.supported_rates if active else [48000],
                "depths": active.supported_depths if active else [16],
                "max_channels": active.max_channels if active else 2,
                "ffmpeg": ffmpeg_available(),
            },
        }
    )


@api.post("/settings")
@login_required
def update_settings():
    data = _body()
    data.pop("password", None)  # only changeable via /api/password
    if _recorder().is_recording:
        locked = {"sample_rate", "bit_depth", "channels", "audio_device"}
        blocked = locked & set(data)
        if blocked:
            return jsonify(
                {
                    "error": "Cannot change audio format while recording",
                    "blocked": sorted(blocked),
                }
            ), 409
    return jsonify(config.update(data))


# ── Wi-Fi ────────────────────────────────────────────────────────────────────

@api.get("/wifi/status")
@login_required
def wifi_status():
    return jsonify(wifi.status())


@api.get("/wifi/scan")
@login_required
def wifi_scan():
    return jsonify(wifi.scan(rescan=request.args.get("rescan", "1") != "0"))


@api.get("/wifi/networks")
@login_required
def wifi_networks():
    return jsonify(db.list_networks())


@api.post("/wifi/connect")
@login_required
def wifi_connect():
    data = _body()
    ssid = str(data.get("ssid", "")).strip()
    if not ssid:
        return jsonify({"error": "ssid is required"}), 400
    result = wifi.connect(
        ssid,
        str(data.get("password", "")),
        is_hotspot=bool(data.get("is_hotspot")),
        priority=int(data.get("priority", 0)),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@api.post("/wifi/ap/start")
@login_required
def wifi_ap_start():
    data = _body()
    result = wifi.start_ap(
        ssid=str(data.get("ssid", "")),
        password=str(data.get("password", "")),
        channel=int(data.get("channel", 0)),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@api.post("/wifi/ap/stop")
@login_required
def wifi_ap_stop():
    return jsonify(wifi.stop_ap())


@api.post("/wifi/auto")
@login_required
def wifi_auto():
    return jsonify(wifi.auto_connect())


@api.delete("/wifi/networks/<ssid>")
@login_required
def wifi_forget(ssid: str):
    return jsonify(wifi.forget(ssid))


# ── Hardware ─────────────────────────────────────────────────────────────────

@api.get("/hardware")
@login_required
def hardware_status():
    hw = _ctx.get("hardware")
    return jsonify(hw.status() if hw else {"enabled": False})
