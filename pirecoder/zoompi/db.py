"""SQLite metadata store.

The filesystem remains the source of truth for audio — a recording is valid
even if this database is deleted. SQLite only holds the things a WAV file
cannot carry: notes, markers, event names, and an audit log.

WAL mode is enabled so the recorder can write markers while the web layer
reads, without either blocking the other.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    base_name      TEXT NOT NULL,
    folder         TEXT NOT NULL,
    event_name     TEXT DEFAULT '',
    notes          TEXT DEFAULT '',
    device         TEXT DEFAULT '',
    sample_rate    INTEGER,
    bit_depth      INTEGER,
    channels       INTEGER,
    started_at     REAL NOT NULL,
    stopped_at     REAL,
    duration       REAL DEFAULT 0,
    size_bytes     INTEGER DEFAULT 0,
    segment_count  INTEGER DEFAULT 1,
    recovered      INTEGER DEFAULT 0,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS markers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    offset_seconds  REAL NOT NULL,
    label           TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wifi_networks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ssid        TEXT NOT NULL UNIQUE,
    psk         TEXT DEFAULT '',
    priority    INTEGER DEFAULT 0,
    is_hotspot  INTEGER DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_markers_session  ON markers(session_id);
CREATE INDEX IF NOT EXISTS idx_events_created   ON events(created_at DESC);
"""

_lock = threading.RLock()


@contextmanager
def connect(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


# ── Sessions ─────────────────────────────────────────────────────────────────

def save_session(session: dict, recovered: bool = False) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, base_name, folder, event_name, notes, device,
                sample_rate, bit_depth, channels, started_at, stopped_at,
                duration, size_bytes, segment_count, recovered, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                notes=excluded.notes,
                stopped_at=excluded.stopped_at,
                duration=excluded.duration,
                size_bytes=excluded.size_bytes,
                segment_count=excluded.segment_count
            """,
            (
                session.get("session_id"),
                session.get("base_name", ""),
                session.get("folder", ""),
                session.get("event_name", ""),
                session.get("notes", ""),
                session.get("device_name", ""),
                session.get("sample_rate"),
                session.get("bit_depth"),
                session.get("channels"),
                session.get("started_at", time.time()),
                session.get("stopped_at"),
                session.get("duration", 0),
                session.get("size_bytes", 0),
                len(session.get("segments", [])) or 1,
                1 if recovered else 0,
                time.time(),
            ),
        )
        for marker in session.get("markers", []):
            conn.execute(
                "INSERT INTO markers (session_id, offset_seconds, label, created_at)"
                " VALUES (?,?,?,?)",
                (
                    session.get("session_id"),
                    marker.get("offset_seconds", 0),
                    marker.get("label", ""),
                    marker.get("created_at", time.time()),
                ),
            )


def get_session(session_id: str) -> dict | None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["markers"] = [
            dict(m)
            for m in conn.execute(
                "SELECT offset_seconds, label, created_at FROM markers"
                " WHERE session_id=? ORDER BY offset_seconds",
                (session_id,),
            ).fetchall()
        ]
        return data


def list_sessions(limit: int = 200) -> list[dict]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_session_meta(session_id: str, event_name: str | None = None,
                        notes: str | None = None) -> bool:
    sets, params = [], []
    if event_name is not None:
        sets.append("event_name=?")
        params.append(event_name)
    if notes is not None:
        sets.append("notes=?")
        params.append(notes)
    if not sets:
        return False
    params.append(session_id)
    with _lock, connect() as conn:
        cur = conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", params
        )
        return cur.rowcount > 0


def delete_session(session_id: str) -> bool:
    with _lock, connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        return cur.rowcount > 0


# ── Events (health / audit log) ──────────────────────────────────────────────

def log_event(kind: str, detail: dict | str = "") -> None:
    payload = json.dumps(detail) if isinstance(detail, dict) else str(detail)
    try:
        with _lock, connect() as conn:
            conn.execute(
                "INSERT INTO events (kind, detail, created_at) VALUES (?,?,?)",
                (kind, payload[:2000], time.time()),
            )
            # Keep the log bounded so it never fills the card.
            conn.execute(
                "DELETE FROM events WHERE id NOT IN"
                " (SELECT id FROM events ORDER BY created_at DESC LIMIT 2000)"
            )
    except sqlite3.Error:
        pass  # logging must never break a recording


def recent_events(limit: int = 100) -> list[dict]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT kind, detail, created_at FROM events"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Wi-Fi networks ───────────────────────────────────────────────────────────

def save_network(ssid: str, psk: str = "", priority: int = 0,
                 is_hotspot: bool = False) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            INSERT INTO wifi_networks (ssid, psk, priority, is_hotspot, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ssid) DO UPDATE SET
                psk=excluded.psk,
                priority=excluded.priority,
                is_hotspot=excluded.is_hotspot
            """,
            (ssid, psk, priority, 1 if is_hotspot else 0, time.time()),
        )


def list_networks() -> list[dict]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT id, ssid, priority, is_hotspot, created_at FROM wifi_networks"
            " ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_network_psk(ssid: str) -> str | None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT psk FROM wifi_networks WHERE ssid=?", (ssid,)
        ).fetchone()
        return row["psk"] if row else None


def delete_network(ssid: str) -> bool:
    with _lock, connect() as conn:
        cur = conn.execute("DELETE FROM wifi_networks WHERE ssid=?", (ssid,))
        return cur.rowcount > 0
