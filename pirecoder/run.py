#!/usr/bin/env python3
"""ZoomPi entry point.

Started by systemd as:
    /usr/bin/python3 /home/pi/zoompi/run.py
"""

from __future__ import annotations

import sys

from zoompi.app import create_app

HOST = "0.0.0.0"
PORT = 5000


def main() -> int:
    app, socketio = create_app()
    # allow_unsafe_werkzeug: threading mode serves via Werkzeug, and
    # Flask-SocketIO refuses to start it without this acknowledgement.
    # Traffic here is LAN-only and single-digit clients, so it is appropriate.
    socketio.run(
        app,
        host=HOST,
        port=PORT,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
