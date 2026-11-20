"""Offline edit journal.

Entries are JSON Lines under ``%LOCALAPPDATA%\\Blendermp\\journal``. The addon
coalesces transforms per object before appending, so a long drag produces one
entry. Corrupt or oversized files are truncated rather than trusted.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

APP_DIR = "Blendermp"
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 5000
SAFE = re.compile(r"[^A-Za-z0-9_-]")


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def journal_dir() -> Path:
    path = data_dir() / "journal"
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(session: str) -> Path:
    name = SAFE.sub("", str(session or "session"))[:64] or "session"
    return journal_dir() / f"{name}.jsonl"


def append(session: str, entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    path = path_for(session)
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            try:
                os.replace(path, path.with_suffix(".jsonl.1"))
            except OSError:
                path.write_text("", encoding="utf-8")
        line = json.dumps(entry, ensure_ascii=True, separators=(",", ":"), default=str)
        if len(line) > 8 * 1024 * 1024:
            return False
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except OSError:
        return False


def read(session: str) -> tuple[list[dict], int]:
    path = path_for(session)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return [], 0
    entries = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(entries) >= MAX_ENTRIES:
                    skipped += 1
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    body = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(body, dict):
                    entries.append(body)
                else:
                    skipped += 1
    except OSError:
        return [], 0
    return entries, skipped


def clear(session: str) -> None:
    for path in (path_for(session), path_for(session).with_suffix(".jsonl.1")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def pending() -> list[tuple[str, Path]]:
    try:
        files = [path for path in journal_dir().glob("*.jsonl") if path.is_file() and path.stat().st_size > 0]
    except OSError:
        return []
    return sorted(
        ((path.stem, path) for path in files),
        key=lambda item: item[1].stat().st_mtime,
        reverse=True,
    )


def stamp() -> float:
    return round(time.time(), 3)
