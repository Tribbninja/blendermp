"""Scene checkpoints for disaster recovery.

Each checkpoint is a zip (``.bmpz``) containing a manifest and a JSON scene
snapshot. The addon also writes a ``.blend`` copy for cases where loading the
custom snapshot is not possible.
"""

from __future__ import annotations

import json
import os
import re
import time
import zipfile
from pathlib import Path

APP_DIR = "Blendermp"
MAX_BYTES = 1024 * 1024 * 1024
KEEP = 3
SAFE = re.compile(r"[^A-Za-z0-9_-]")


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def root_dir() -> Path:
    path = data_dir() / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir(code: str) -> Path:
    name = SAFE.sub("", str(code or "offline"))[:64] or "offline"
    path = root_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def create(code: str, scene: dict) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    stamp = f"{stamp}-{int(time.time() * 1000) % 1000:03d}"
    directory = session_dir(code)
    target = directory / f"{stamp}.bmpz"
    manifest = {
        "format": "bmpz1",
        "created": round(time.time(), 3),
        "code": str(code)[:64],
        "objects": len(scene.get("objects", [])),
        "addon": str(scene.get("addon", "")),
    }
    temp = target.with_suffix(".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        bundle.writestr("scene.json", json.dumps(scene, separators=(",", ":")))
    os.replace(temp, target)
    prune(code)
    return target


def read(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as bundle:
            names = set(bundle.namelist())
            if "scene.json" not in names:
                return None
            body = json.loads(bundle.read("scene.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def list_checkpoints(code: str) -> list[Path]:
    try:
        files = [path for path in session_dir(code).glob("*.bmpz") if path.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def latest(code: str) -> Path | None:
    files = list_checkpoints(code)
    return files[0] if files else None


def latest_any() -> Path | None:
    try:
        files = [path for path in root_dir().glob("*/*.bmpz") if path.is_file()]
    except OSError:
        return None
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


def prune(code: str, keep: int = KEEP) -> None:
    files = list_checkpoints(code)
    total = 0
    for index, path in enumerate(files):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        if index >= keep or total > MAX_BYTES:
            try:
                path.unlink()
            except OSError:
                pass
            try:
                path.with_suffix(".blend").unlink(missing_ok=True)
            except OSError:
                pass
