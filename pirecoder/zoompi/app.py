"""Application factory and background service wiring."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from flask import Blueprint, Flask, redirect, render_template, request, url_for
from flask_socketio import SocketIO

from . import db, storage, system, wifi
from .api import api, bind
from .auth import check_credentials, is_authenticated, login_required, login_session, logout_session
from .config import BASE_DIR, LOG_DIR, config
from .hardware import HardwareController
from .levels import LevelMeter
from .postprocess import processor
from .recorder import Recorder, recover_orphans

socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=20,
    ping_interval=10,
)

web = Blueprint("web", __name__)


# ── Page routes ──────────────────────────────────────────────────────────────

@web.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("web.dashboard"))
    error = None
    if request.method == "POST":
        if check_credentials(request.form.get("password", "")):
            login_session()
            return redirect(request.args.get("next") or url_for("web.dashboard"))
        error = "Incorrect password"
        time.sleep(0.5)
    return render_template("login.html", error=error, device_name=config.get("device_name"))


@web.get("/logout")
def logout():
    logout_session()
    return redirect(url_for("web.login"))


@web.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", device_name=config.get("device_name"))


@web.get("/files")
@login_required
def files():
    return render_template("files.html", device_name=config.get("device_name"))


@web.get("/settings")
@login_required
def settings():
    return render_template("settings.html", device_name=config.get("device_name"))


# ── Background broadcaster ───────────────────────────────────────────────────

class Broadcaster:
    """Pushes status + levels to connected clients.

    Runs at two rates: levels at 10 Hz while recording (the VU meter needs to
    feel live), and the heavier system snapshot every 5 seconds. Clients that
    miss frames simply render the next one — nothing here is stateful, which
    is what lets a phone reconnect mid-take and immediately show the truth.
    """

    def __init__(self, recorder: Recorder, meter: LevelMeter) -> None:
        self._recorder = recorder
        self._meter = meter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="broadcaster", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_system = 0.0
        while not self._stop.wait(0.1):
            try:
                status = self._recorder.status()
                recording = status.get("is_recording")

                if recording:
                    socketio.emit(
                        "levels", {"levels": self._meter.read(), "status": status}
                    )
                    time.sleep(0.0)  # yield; the 0.1 s wait paces us at ~10 Hz
                else:
                    socketio.emit("status", status)
                    self._stop.wait(0.4)  # idle clients don't need 10 Hz

                now = time.time()
                if now - last_system >= 5.0:
                    last_system = now
                    socketio.emit("system", system.snapshot())
                    if config.get("auto_cleanup"):
                        storage.run_cleanup()
            except Exception:
                continue


# ── Factory ──────────────────────────────────────────────────────────────────

def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "zoompi.log", maxBytes=2_000_000, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)


def create_app() -> tuple[Flask, SocketIO]:
    _configure_logging()
    log = logging.getLogger("zoompi")

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "web" / "templates"),
        static_folder=str(BASE_DIR / "web" / "static"),
    )
    app.config.update(
        SECRET_KEY=_persistent_secret(),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=0,
        JSON_SORT_KEYS=False,
    )

    db.init_db()

    # Repair anything a power cut left behind before accepting new takes.
    recovered = recover_orphans()
    for item in recovered:
        db.log_event("recovered_recording", item)
        log.warning("Recovered interrupted recording: %s", item.get("session_id"))

    def on_event(kind: str, payload: dict) -> None:
        db.log_event(kind, payload if isinstance(payload, dict) else {})
        try:
            socketio.emit(kind, payload)
        except Exception:
            pass

    recorder = Recorder(on_event=on_event)
    meter = LevelMeter(recorder)
    hardware = HardwareController(recorder, system)

    processor.attach_recorder(recorder)
    bind(recorder, meter, hardware)

    app.register_blueprint(web)
    app.register_blueprint(api)

    @app.after_request
    def _no_store(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(404)
    def _not_found(_):
        if request.path.startswith("/api/"):
            return {"error": "Not found"}, 404
        return redirect(url_for("web.dashboard"))

    @app.errorhandler(500)
    def _server_error(exc):
        log.exception("Unhandled error: %s", exc)
        return {"error": "Internal server error"}, 500

    socketio.init_app(app)

    @socketio.on("connect")
    def _on_connect():
        # A phone that just reconnected gets the full picture immediately.
        socketio.emit("status", recorder.status())
        socketio.emit("system", system.snapshot())

    # Background services.
    meter.start()
    processor.start()
    Broadcaster(recorder, meter).start()

    if config.get("hardware_enabled"):
        try:
            hardware.start()
            log.info("Hardware controls: %s", hardware.status())
        except Exception as exc:
            log.warning("Hardware init failed: %s", exc)

    # The watchdog runs in every mode. `auto_connect()` already respects
    # wifi_mode (client mode never starts an AP), and skipping the watchdog
    # in client mode meant a dropped link was never re-established either.
    if wifi.available():
        wifi.WifiWatchdog().start()
        log.info("Wi-Fi watchdog started (mode=%s)", config.get("wifi_mode"))
    else:
        log.warning("NetworkManager unavailable — Wi-Fi management disabled")

    db.log_event("service_started", {"version": _version(), "recovered": len(recovered)})
    log.info("ZoomPi ready on http://%s:5000", system.primary_ip())
    return app, socketio


def _persistent_secret() -> str:
    """Stable across restarts so a service restart doesn't log everyone out."""
    path = LOG_DIR.parent / "secret.key"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        secret = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        path.chmod(0o600)
        return secret
    except OSError:
        return secrets.token_hex(32)


def _version() -> str:
    from . import __version__
    return __version__
