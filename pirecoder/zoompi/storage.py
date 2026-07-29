"""Recording library: listing, search, rename, delete, export, cleanup."""

from __future__ import annotations

import io
import re
import shutil
import struct
import time
import zipfile
from pathlib import Path

from .config import RECORDINGS_DIR, config

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}$")
AUDIO_SUFFIXES = (".wav", ".flac", ".mp3")


class StorageError(RuntimeError):
    pass


def _resolve(folder: str, filename: str) -> Path:
    """Resolve a user-supplied path, refusing anything outside RECORDINGS_DIR.

    Both components arrive from HTTP, so traversal has to be blocked before
    any filesystem call.
    """
    if not folder or not filename:
        raise StorageError("Folder and filename are required")
    if any(part in ("..", "", ".") for part in (folder, filename)):
        raise StorageError("Invalid path")
    if "/" in filename or "\\" in filename:
        raise StorageError("Invalid filename")

    root = RECORDINGS_DIR.resolve()
    path = (root / folder / filename).resolve()
    # is_relative_to compares path components, so it is not fooled by a
    # separator mismatch or by a sibling directory sharing a name prefix.
    if not path.is_relative_to(root) or path == root:
        raise StorageError("Path escapes recordings directory")
    return path


def wav_duration(path: Path) -> float:
    """Duration from the WAV header, falling back to file size.

    Reads only the header rather than opening via `wave`, so it still works
    on a file whose header was rebuilt after a crash.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(44)
        if len(header) < 44 or header[:4] != b"RIFF":
            return 0.0
        channels, rate = struct.unpack("<HI", header[22:28])
        depth = struct.unpack("<H", header[34:36])[0]
        byte_rate = rate * channels * max(1, depth // 8)
        data_bytes = path.stat().st_size - 44
        return round(data_bytes / byte_rate, 2) if byte_rate else 0.0
    except (OSError, struct.error, ZeroDivisionError):
        return 0.0


def describe(path: Path) -> dict:
    st = path.stat()
    return {
        "filename": path.name,
        "folder": path.parent.name,
        "size_bytes": st.st_size,
        "size_mb": round(st.st_size / (1024 * 1024), 2),
        "modified": st.st_mtime,
        "duration": wav_duration(path) if path.suffix.lower() == ".wav" else 0.0,
        "format": path.suffix.lstrip(".").lower(),
    }


def list_recordings(
    search: str = "",
    folder: str = "",
    sort: str = "date",
    order: str = "desc",
) -> list[dict]:
    """Flat listing across day folders, with search and sort."""
    if not RECORDINGS_DIR.exists():
        return []

    items: list[dict] = []
    needle = search.lower().strip()

    for day_dir in RECORDINGS_DIR.iterdir():
        if not day_dir.is_dir() or day_dir.name == "safety":
            continue
        if folder and day_dir.name != folder:
            continue
        for f in day_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if f.name.startswith("."):
                continue
            if needle and needle not in f.name.lower():
                continue
            items.append(describe(f))

    keys = {
        "date": lambda i: i["modified"],
        "name": lambda i: i["filename"].lower(),
        "size": lambda i: i["size_bytes"],
        "duration": lambda i: i["duration"],
    }
    items.sort(key=keys.get(sort, keys["date"]), reverse=(order != "asc"))
    return items


def list_folders() -> list[dict]:
    """Day folders with aggregate counts, newest first."""
    if not RECORDINGS_DIR.exists():
        return []
    folders = []
    for d in RECORDINGS_DIR.iterdir():
        if not d.is_dir() or d.name == "safety":
            continue
        files = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES]
        if not files:
            continue
        folders.append(
            {
                "name": d.name,
                "count": len(files),
                "size_mb": round(sum(f.stat().st_size for f in files) / (1024 * 1024), 2),
                "modified": max(f.stat().st_mtime for f in files),
            }
        )
    folders.sort(key=lambda f: f["name"], reverse=True)
    return folders


def get_path(folder: str, filename: str) -> Path:
    path = _resolve(folder, filename)
    if not path.exists() or not path.is_file():
        raise StorageError("File not found")
    return path


def delete(folder: str, filename: str) -> dict:
    path = get_path(folder, filename)
    size = path.stat().st_size
    path.unlink()
    # Drop the day folder once its last recording is gone.
    try:
        if not any(path.parent.iterdir()):
            path.parent.rmdir()
    except OSError:
        pass
    return {"deleted": filename, "freed_bytes": size}


def delete_many(items: list[dict]) -> dict:
    deleted, failed = [], []
    for item in items:
        try:
            delete(item.get("folder", ""), item.get("filename", ""))
            deleted.append(item.get("filename"))
        except (StorageError, OSError) as exc:
            failed.append({"filename": item.get("filename"), "error": str(exc)})
    return {"deleted": deleted, "failed": failed}


def rename(folder: str, filename: str, new_name: str) -> dict:
    path = get_path(folder, filename)
    new_name = new_name.strip()
    if not new_name:
        raise StorageError("New name is required")

    suffix = path.suffix
    if new_name.lower().endswith(suffix.lower()):
        new_name = new_name[: -len(suffix)]
    if not SAFE_NAME.match(new_name):
        raise StorageError("Name may only contain letters, numbers, space, dot, dash, underscore")

    target = path.parent / f"{new_name}{suffix}"
    if target.exists():
        raise StorageError("A file with that name already exists")
    path.rename(target)
    return {"renamed_to": target.name}


def export_zip(items: list[dict]) -> io.BytesIO:
    """Bundle selected recordings into an in-memory ZIP.

    Stored (not deflated) because WAV barely compresses and the Pi 3 would
    spend minutes of CPU for a few percent.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for item in items:
            try:
                path = get_path(item.get("folder", ""), item.get("filename", ""))
            except StorageError:
                continue
            zf.write(path, arcname=f"{path.parent.name}/{path.name}")
    buf.seek(0)
    return buf


def export_folder_zip(folder: str) -> io.BytesIO:
    files = list_recordings(folder=folder)
    return export_zip([{"folder": f["folder"], "filename": f["filename"]} for f in files])


def run_cleanup(force: bool = False) -> dict:
    """Delete oldest recordings once usage passes the configured threshold.

    Only ever runs when explicitly enabled — silently deleting a client's
    audio would be worse than running out of space.
    """
    if not force and not config.get("auto_cleanup"):
        return {"ran": False, "reason": "auto_cleanup disabled"}

    usage = shutil.disk_usage(str(RECORDINGS_DIR))
    percent = 100.0 * (usage.total - usage.free) / usage.total if usage.total else 0.0
    threshold = float(config.get("cleanup_threshold_pct"))

    if percent < threshold:
        return {"ran": False, "reason": f"usage {percent:.1f}% below {threshold}%"}

    candidates = list_recordings(sort="date", order="asc")
    removed, freed = [], 0
    for item in candidates:
        if percent < threshold - 5:  # clear some headroom, not just the line
            break
        try:
            result = delete(item["folder"], item["filename"])
            removed.append(item["filename"])
            freed += result["freed_bytes"]
            usage = shutil.disk_usage(str(RECORDINGS_DIR))
            percent = 100.0 * (usage.total - usage.free) / usage.total if usage.total else 0.0
        except (StorageError, OSError):
            continue

    return {
        "ran": True,
        "deleted": removed,
        "freed_mb": round(freed / (1024 * 1024), 2),
        "usage_percent": round(percent, 1),
    }


def stats() -> dict:
    files = list_recordings()
    return {
        "total_files": len(files),
        "total_size_mb": round(sum(f["size_mb"] for f in files), 2),
        "total_duration_hours": round(sum(f["duration"] for f in files) / 3600, 2),
        "folders": len(list_folders()),
        "newest": files[0]["filename"] if files else None,
    }
