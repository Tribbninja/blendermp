"""Crash marker/heartbeat used to detect an unclean Blender exit.

Each Blender process owns ``heartbeats/<pid>.json``. The file is removed on a
clean shutdown. On startup, remaining files whose process is gone (or whose
timestamp is old) are reported as an unclean exit; the UI can then point at
recovery checkpoints.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

APP_DIR = "Blendermp"
STALE_SECONDS = 7 * 24 * 60 * 60
MAX_ENTRIES = 32


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def heartbeat_dir() -> Path:
    path = data_dir() / "heartbeats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_path(pid: int | None = None) -> Path:
    return heartbeat_dir() / f"{os.getpid() if pid is None else int(pid)}.json"


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def write(state: str, session: str = "", extra: dict | None = None) -> None:
    body = {
        "pid": os.getpid(),
        "stamp": round(time.time(), 3),
        "state": str(state)[:32],
        "session": str(session)[:64],
        "blender": extra.get("blender", "") if extra else "",
        "addon": extra.get("addon", "") if extra else "",
    }
    path = current_path()
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def mark_clean() -> None:
    try:
        current_path().unlink()
    except OSError:
        pass


def unclean_exits() -> list[dict]:
    """Return recent heartbeats that belong to dead Blender processes."""
    now = time.time()
    result = []
    for path in sorted(heartbeat_dir().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(result) >= MAX_ENTRIES:
            break
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        pid = body.get("pid")
        stamp = body.get("stamp")
        if not isinstance(pid, int) or not isinstance(stamp, (int, float)):
            continue
        if pid == os.getpid():
            continue
        try:
            if now - float(stamp) > STALE_SECONDS:
                path.unlink(missing_ok=True)
                continue
        except OSError:
            pass
        if process_alive(pid):
            continue
        result.append(body)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return result
