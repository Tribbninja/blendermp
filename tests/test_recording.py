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


def test_recorder_drops_oversized_bodies(tmp_path):
    path = tmp_path / "big.jsonl"
    recorder = recording.Recorder(str(path), max_body_bytes=200)
    recorder.start()
    recorder.record("mesh_snapshot", {"uuid": "u", "payload": "x" * 500})
    recorder.stop()
    assert recorder.dropped_count == 1
    replay = recording.Replay(str(path))
    assert replay.events[0].body["dropped"] is True


def test_recorder_subscribers_see_events(tmp_path):
    path = tmp_path / "sub.jsonl"
    recorder = recording.Recorder(str(path))
    seen = []
    recorder.subscribe(lambda kind, body: seen.append((kind, body)))
    recorder.start()
    recorder.record("chat", {"text": "hello"})
    recorder.stop()
    assert seen == [("chat", {"text": "hello"})]


def test_replay_summary_and_kinds(tmp_path):
    path = tmp_path / "summary.jsonl"
    recorder = recording.Recorder(str(path))
    recorder.start()
    recorder.record("chat", {"text": "a"})
    recorder.record("transform", {"uuid": "u"})
    recorder.record("chat", {"text": "b"})
    recorder.stop()
    replay = recording.Replay(str(path))
    summary = replay.summary()
    assert summary["events"] == 3
    assert summary["kinds"]["chat"] == 2
    assert summary["duration"] >= 0.0


def test_list_recordings(tmp_path):
    for index in range(3):
        path = tmp_path / f"session-{index}.jsonl"
        recorder = recording.Recorder(str(path))
        recorder.start()
        recorder.record("chat", {"text": str(index)})
        recorder.stop()
    items = recording.list_recordings(tmp_path, limit=2)
    assert len(items) == 2
    assert all(item.events == 1 for item in items)
    assert items[0].as_dict()["name"].startswith("session-")


def test_replay_missing_file_is_empty(tmp_path):
    replay = recording.Replay(str(tmp_path / "missing.jsonl"))
    assert replay.events == []
    assert replay.play(lambda _k, _b: None) == 0
    assert replay.summary()["events"] == 0
