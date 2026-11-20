from core import recording


def test_recorder_roundtrip(tmp_path):
    path = tmp_path / "session.jsonl"
    recorder = recording.Recorder(str(path))
    recorder.start()
    assert recorder.is_active
    recorder.record("transform", {"uuid": "u", "matrix": [1.0] * 16})
    recorder.record("chat", {"text": "hi", "data": b"abc"})
    recorder.stop()
    assert not recorder.is_active
    assert recorder.event_count == 2

    replay = recording.Replay(str(path))
    assert len(replay.events) == 2
    assert replay.events[0].kind == "transform"
    assert replay.events[1].body["data"] == b"abc"

    seen = []
    replay.play(lambda kind, body: seen.append((kind, body)), speed=100.0)
    assert seen == [
        ("transform", {"uuid": "u", "matrix": [1.0] * 16}),
        ("chat", {"text": "hi", "data": b"abc"}),
    ]


def test_recorder_skips_non_dict_bodies(tmp_path):
    path = tmp_path / "r.jsonl"
    recorder = recording.Recorder(str(path))
    recorder.start()
    recorder.record("ping", "hello")
    recorder.stop()
    replay = recording.Replay(str(path))
    assert replay.events[0].body == {"value": "hello"}


def test_replay_missing_file_is_empty(tmp_path):
    replay = recording.Replay(str(tmp_path / "missing.jsonl"))
    assert replay.events == []
    assert replay.play(lambda _k, _b: None) == 0
