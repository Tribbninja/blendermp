"""Session render job state and frame payloads."""

import pytest

from core import render_payload, renderjob


def test_create_builds_frames_for_the_range():
    manager = renderjob.RenderManager(max_batch=5)
    job = manager.create("Test", 1, 10, 2, 1920, 1080, "PNG", "BLENDER_EEVEE_NEXT", "out")
    assert job is not None
    assert sorted(job.frames) == [1, 3, 5, 7, 9]
    assert manager.active()


def test_only_one_job_at_a_time():
    manager = renderjob.RenderManager()
    assert manager.create("A", 1, 2, 1, 0, 0, "PNG", "", "out") is not None
    assert manager.create("B", 1, 2, 1, 0, 0, "PNG", "", "out") is None


def test_claim_returns_at_most_the_batch_size():
    manager = renderjob.RenderManager(max_batch=5)
    job = manager.create("Test", 1, 20, 1, 0, 0, "PNG", "", "out")
    manager.start()
    frames = manager.claim("worker-a", 5)
    assert frames == [1, 2, 3, 4, 5]
    more = manager.claim("worker-b", 5)
    assert more == [6, 7, 8, 9, 10]
    assert manager.claim("worker-c", 5) == [11, 12, 13, 14, 15]


def test_complete_and_finish():
    manager = renderjob.RenderManager()
    job = manager.create("Test", 1, 3, 1, 0, 0, "PNG", "", "out")
    manager.start()
    for frame in (1, 2, 3):
        assert manager.complete(frame, "w", f"frame_{frame}.png")
    assert job.finished
    assert manager.all_done()
    assert manager.status()["percent"] == 100.0


def test_release_returns_claimed_frames():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 5, 1, 0, 0, "PNG", "", "out")
    manager.start()
    claimed = manager.claim("worker", 5)
    assert len(claimed) == 5
    manager.release("worker")
    again = manager.claim("other", 5)
    assert again == [1, 2, 3, 4, 5]


def test_fail_and_retry():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("worker", 2)
    manager.fail(1, "worker", "boom")
    assert manager.status()["failed"] == 1
    assert manager.retry_failed() == 1
    assert manager.status()["failed"] == 0
    assert manager.status()["pending"] == 1
    assert manager.active()


def test_accept_and_decline_are_tracked():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.ask("a")
    manager.ask("b")
    manager.accept("a")
    manager.decline("b")
    status = manager.status()
    assert status["workers"] == ["a"]
    assert status["declined"] == ["b"]


def test_render_payload_roundtrip():
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    payload = render_payload.encode("job123", 7, "png", 640, 480, data)
    body = render_payload.decode(payload)
    assert body["job"] == "job123"
    assert body["frame"] == 7
    assert body["format"] == "png"
    assert body["width"] == 640
    assert body["height"] == 480
    assert body["data"] == data


def test_render_payload_lossless_compression():
    data = b"B" * 200_000
    raw = render_payload.encode("j", 1, "png", 0, 0, data, compress=False)
    packed = render_payload.encode("j", 1, "png", 0, 0, data, compress=True)
    assert len(packed) < len(raw)
    assert render_payload.decode(packed)["data"] == data


def test_render_payload_rejects_bad_input():
    with pytest.raises(render_payload.RenderPayloadError):
        render_payload.encode("j", 1, "png", 0, 0, b"")
    with pytest.raises(render_payload.RenderPayloadError):
        render_payload.decode(b"")
    payload = bytearray(render_payload.encode("j", 1, "png", 0, 0, b"data"))
    payload[-1] ^= 0xFF
    # A single flipped byte in the image still decodes structurally; the
    # transport CRC is what rejects corruption. Ensure the header path is safe.
    assert render_payload.decode(bytes(payload))["frame"] == 1


def test_clear_retires_the_job_and_keeps_the_summary():
    manager = renderjob.RenderManager()
    job = manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("w", 2)
    manager.complete(1, "w", "a.png")
    manager.complete(2, "w", "b.png")
    assert job.finished
    manager.clear()
    assert manager.job() is None
    assert not manager.active()
    status = manager.status()
    assert status["finished"] is True
    assert status["done"] == 2
    assert status["percent"] == 100.0
    # A second clear is harmless and a new job is allowed.
    manager.clear()
    assert manager.create("Next", 1, 1, 1, 0, 0, "PNG", "", "out") is not None


def test_stop_keeps_frames_and_marks_stopped():
    manager = renderjob.RenderManager()
    job = manager.create("Test", 1, 5, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("w", 2)
    manager.complete(1, "w", "a.png")
    assert manager.stop() is True
    assert job.finished and job.stopped
    status = manager.status()
    assert status["stopped"] is True
    assert status["done"] == 1
    manager.clear()
    assert manager.status()["stopped"] is True
    assert manager.status()["done"] == 1
    # Stopping a finished job does nothing.
    assert manager.stop() is False
