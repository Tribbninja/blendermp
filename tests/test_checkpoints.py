import time

from core import checkpoints


def test_checkpoint_roundtrip_and_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    scene = {"objects": [{"uuid": "a", "meta": {"name": "Cube"}}], "addon": "test"}
    first = checkpoints.create("ROOM", scene)
    assert first.is_file()
    assert checkpoints.read(first)["objects"][0]["meta"]["name"] == "Cube"
    time.sleep(0.01)
    second = checkpoints.create("ROOM", scene)
    time.sleep(0.01)
    third = checkpoints.create("ROOM", scene)
    assert checkpoints.latest("ROOM") == third
    assert len(checkpoints.list_checkpoints("ROOM")) == 3
    checkpoints.prune("ROOM", keep=1)
    assert checkpoints.list_checkpoints("ROOM") == [third]
    assert second.exists() is False or first.exists() is False
