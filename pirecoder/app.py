import os
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import pyaudio
from flask import Flask, jsonify, render_template, send_file, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "audio-recorder-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

RECORDINGS_DIR = Path("recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

# Audio settings
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100

# Recording state
state = {
    "is_recording": False,
    "current_file": None,
    "start_time": None,
    "duration": 0,
}

_audio = None
_stream = None
_frames = []
_record_thread = None
_stop_event = threading.Event()


def _recording_worker():
    global _frames, _audio, _stream
    _frames = []
    _audio = pyaudio.PyAudio()
    _stream = _audio.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    while not _stop_event.is_set():
        data = _stream.read(CHUNK, exception_on_overflow=False)
        _frames.append(data)
        elapsed = time.time() - state["start_time"]
        state["duration"] = int(elapsed)
        socketio.emit("status", _build_status())


def _save_recording():
    global _audio, _stream
    if not _frames or not state["current_file"]:
        return
    filepath = RECORDINGS_DIR / state["current_file"]
    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(_audio.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b"".join(_frames))
    _stream.stop_stream()
    _stream.close()
    _audio.terminate()


def _build_status():
    return {
        "is_recording": state["is_recording"],
        "current_file": state["current_file"],
        "duration": state["duration"],
    }


def _list_recordings():
    files = []
    for f in sorted(RECORDINGS_DIR.glob("*.wav"), reverse=True):
        size_kb = round(f.stat().st_size / 1024, 1)
        files.append({"name": f.name, "size_kb": size_kb})
    return files


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/api/start", methods=["POST"])
def api_start():
    global _record_thread, _stop_event
    if state["is_recording"]:
        return jsonify({"error": "Already recording"}), 400

    label = request.json.get("label", "").strip() if request.is_json else ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{label}.wav" if label else f"{timestamp}.wav"

    state["is_recording"] = True
    state["current_file"] = filename
    state["start_time"] = time.time()
    state["duration"] = 0

    _stop_event = threading.Event()
    _record_thread = threading.Thread(target=_recording_worker, daemon=True)
    _record_thread.start()

    socketio.emit("status", _build_status())
    return jsonify({"message": "Recording started", "file": filename})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _record_thread
    if not state["is_recording"]:
        return jsonify({"error": "Not recording"}), 400

    _stop_event.set()
    _record_thread.join(timeout=5)

    _save_recording()

    saved_file = state["current_file"]
    state["is_recording"] = False
    state["current_file"] = None
    state["start_time"] = None
    state["duration"] = 0

    socketio.emit("status", _build_status())
    return jsonify({"message": "Recording stopped", "file": saved_file})


@app.route("/api/recordings")
def api_recordings():
    return jsonify(_list_recordings())


@app.route("/api/download/<filename>")
def api_download(filename):
    filepath = RECORDINGS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    return send_file(str(filepath), as_attachment=True)


@app.route("/api/delete/<filename>", methods=["DELETE"])
def api_delete(filename):
    filepath = RECORDINGS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    filepath.unlink()
    return jsonify({"message": "Deleted"})


# ── WebSocket ────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("status", _build_status())


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
