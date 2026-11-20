import json
import os
import time

from core import heartbeat


def test_unclean_exit_is_reported_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = heartbeat.heartbeat_dir() / "999999.json"
    path.write_text(
        json.dumps({"pid": 999999, "stamp": time.time(), "state": "hosting"}),
        encoding="utf-8",
    )
    entries = heartbeat.unclean_exits()
    assert len(entries) == 1
    assert entries[0]["state"] == "hosting"
    assert not path.exists()


def test_live_or_stale_heartbeat_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    live = heartbeat.heartbeat_dir() / "live.json"
    live.write_text(
        json.dumps({"pid": os.getpid(), "stamp": time.time(), "state": "hosting"}),
        encoding="utf-8",
    )
    stale = heartbeat.heartbeat_dir() / "stale.json"
    stale.write_text(
        json.dumps({"pid": 999998, "stamp": 0, "state": "hosting"}),
        encoding="utf-8",
    )
    assert heartbeat.unclean_exits() == []
    assert live.exists()
    assert not stale.exists()


def test_clean_marker_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    heartbeat.write("hosting", "room")
    assert heartbeat.current_path().exists()
    heartbeat.mark_clean()
    assert not heartbeat.current_path().exists()
