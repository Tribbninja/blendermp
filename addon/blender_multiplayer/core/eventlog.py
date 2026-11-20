"""Small structured event log shared by support bundles.

Events are JSON Lines in ``%LOCALAPPDATA%\\Blendermp\\events.log`` and are also
echoed on stdout with a ``BMP_EVENT`` prefix, so they end up in the app's
``blender.log`` capture too. Never pass secrets, keys, PINs or file contents
here; diagnostics additionally redacts absolute paths.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

APP_DIR = "Blendermp"
MAX_BYTES = 512 * 1024
PREFIX = "BMP_EVENT "


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return data_dir() / "events.log"


def _safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(key)[:64]: _safe(item) for key, item in list(value.items())[:32]}
    return str(value)[:200]


def log(level: str, code: str, message: str, **fields) -> None:
    body = {
        "ts": round(time.time(), 3),
        "level": str(level).upper()[:16],
        "code": str(code)[:48],
        "message": str(message)[:500],
    }
    for key, value in list(fields.items())[:32]:
        body[str(key)[:48]] = _safe(value)
    line = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
    path = log_path()
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            try:
                os.replace(path, path.with_suffix(".log.1"))
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        print(PREFIX + line, flush=True)
    except Exception:  # noqa: BLE001
        pass
