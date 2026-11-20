import json
import os
import time

from core import heartbeat


def _write(tmp_path, pid, state):
    path = tmp_path / "Blendermp" / "heartbeats" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "pid": pid,
        "stamp": time.time(),
        "state": state,
        "session": "",
        "blender": "",
        "addon": "",
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_offline_forced_exit_is_not_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = _write(tmp_path, 999_999, "offline")
    assert heartbeat.unclean_exits() == []
    assert not path.exists()


def test_active_session_exit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write(tmp_path, 999_999, "hosting")
    entries = heartbeat.unclean_exits()
    assert len(entries) == 1
    assert entries[0]["state"] == "hosting"


def test_live_process_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write(tmp_path, os.getpid(), "hosting")
    assert heartbeat.unclean_exits() == []
