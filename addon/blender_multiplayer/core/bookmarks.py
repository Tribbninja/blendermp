"""Named viewport bookmarks.

Bookmarks are local by default. A host or admin can push one to everyone in the
room, which only moves camera views; it never mutates scene objects. Both the
saved file and incoming push payloads go through the same strict sanitizer.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

MAX_BOOKMARKS = 64
MAX_NAME = 48
MAX_FILE = 128 * 1024
PERSPECTIVES = {"PERSP", "ORTHO", "CAMERA"}


def sanitize(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()[:MAX_NAME]
    if not name or any(ord(ch) < 32 for ch in name):
        return None
    matrix = value.get("matrix")
    if not isinstance(matrix, list) or len(matrix) != 16:
        return None
    try:
        matrix = [float(item) for item in matrix]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and abs(item) < 1e12 for item in matrix):
        return None
    try:
        aspect = float(value.get("aspect", 1.6))
        distance = float(value.get("distance", 7.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(aspect) or not 0.1 <= aspect <= 10.0:
        aspect = 1.6
    if not math.isfinite(distance) or not 0.05 <= distance <= 100000.0:
        distance = 7.0
    persp = str(value.get("persp", "PERSP")).upper()[:16]
    if persp not in PERSPECTIVES:
        persp = "PERSP"
    return {
        "name": name,
        "matrix": matrix,
        "aspect": aspect,
        "persp": persp,
        "distance": distance,
    }


def merge(entries: list[dict], bookmark: dict) -> list[dict]:
    """Add or replace by case-insensitive name, keeping newest last."""
    clean = sanitize(bookmark)
    if clean is None:
        return entries[:MAX_BOOKMARKS]
    result = [
        item
        for item in entries
        if str(item.get("name", "")).strip().lower() != clean["name"].lower()
    ]
    result.append(clean)
    return result[-MAX_BOOKMARKS:]


def load(path: Path) -> list[dict]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            return []
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = body.get("bookmarks") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw[:MAX_BOOKMARKS]:
        clean = sanitize(item)
        if clean is not None:
            result.append(clean)
    return result


def save(path: Path, entries: list[dict]) -> None:
    clean = []
    for item in list(entries)[:MAX_BOOKMARKS]:
        value = sanitize(item)
        if value is not None:
            clean.append(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"bookmarks": clean}, separators=(",", ":"))
    if len(payload) > MAX_FILE:
        raise ValueError("bookmark file is too large")
    descriptor, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".bookmarks-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass
