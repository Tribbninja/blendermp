"""Session render job state and frame payloads."""

from pathlib import Path

import pytest

from core import render_payload, renderjob


def test_create_builds_frames_for_the_range():
    manager = renderjob.RenderManager(max_batch=5)
    job = manager.create("Test", 1, 10, 2, 1920, 1080, "PNG", "BLENDER_EEVEE_NEXT", "out")
    assert job is not None
    assert sorted({frame for frame, _tile in job.chunks}) == [1, 3, 5, 7, 9]
    assert job.tile_count == 1
    assert manager.active()


def test_create_builds_a_tile_grid():
    manager = renderjob.RenderManager()
    job = manager.create("Tiles", 1, 3, 1, 1920, 1080, "PNG", "CYCLES", "out", tiles_x=2, tiles_y=2)
    assert job is not None
    assert job.tile_count == 4
    assert len(job.chunks) == 12
    assert (2, 3) in job.chunks
    assert sorted({frame for frame, _tile in job.chunks}) == [1, 2, 3]


def test_only_one_job_at_a_time():
    manager = renderjob.RenderManager()
    assert manager.create("A", 1, 2, 1, 0, 0, "PNG", "", "out") is not None
    assert manager.create("B", 1, 2, 1, 0, 0, "PNG", "", "out") is None


def test_claim_returns_at_most_the_batch_size():
    manager = renderjob.RenderManager(max_batch=5)
    manager.create("Test", 1, 20, 1, 0, 0, "PNG", "", "out")
    manager.start()
    frames = manager.claim("worker-a", 5)
    assert frames == [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]
    more = manager.claim("worker-b", 5)
    assert more == [(6, 0), (7, 0), (8, 0), (9, 0), (10, 0)]
    assert manager.claim("worker-c", 5) == [(11, 0), (12, 0), (13, 0), (14, 0), (15, 0)]


def test_complete_and_finish():
    manager = renderjob.RenderManager()
    job = manager.create("Test", 1, 3, 1, 0, 0, "PNG", "", "out")
    manager.start()
    for frame in (1, 2, 3):
        assert manager.complete(frame, 0, "w", f"frame_{frame}.png")
    assert job.finished
    assert manager.all_done()
    assert manager.status()["percent"] == 100.0


def test_frame_completes_only_when_every_tile_is_done():
    manager = renderjob.RenderManager()
    job = manager.create("Tiles", 1, 1, 1, 64, 64, "PNG", "", "out", tiles_x=2, tiles_y=2)
    manager.start()
    claimed = manager.claim("w", 4)
    assert len(claimed) == 4
    for index, (frame, tile) in enumerate(claimed):
        done = manager.complete(frame, tile, "w", f"tile_{tile}.png")
        assert done is (index == 3)
    assert job.frame_complete(1)
    assert job.finished
    assert manager.status()["frames_done"] == 1


def test_release_returns_claimed_frames():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 5, 1, 0, 0, "PNG", "", "out")
    manager.start()
    claimed = manager.claim("worker", 5)
    assert len(claimed) == 5
    manager.release("worker")
    again = manager.claim("other", 5)
    assert again == [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]


def test_fail_and_retry():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("worker", 2)
    manager.fail(1, 0, "worker", "boom")
    assert manager.status()["failed"] == 1
    assert manager.retry_failed() == 1
    assert manager.status()["failed"] == 0
    assert manager.status()["pending"] == 1
    assert manager.active()


def test_fail_frame_marks_every_tile_failed():
    manager = renderjob.RenderManager()
    manager.create("Tiles", 1, 1, 1, 64, 64, "PNG", "", "out", tiles_x=2, tiles_y=2)
    manager.start()
    manager.claim("w", 4)
    assert manager.fail_frame(1, "w", "boom") == 4
    status = manager.status()
    assert status["failed"] == 4
    assert status["finished"] is True
    assert manager.retry_failed() == 4
    assert manager.status()["pending"] == 4
    assert manager.status()["finished"] is False


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
    assert body["tile"] == 0
    assert (body["tiles_x"], body["tiles_y"]) == (1, 1)
    assert body["data"] == data


def test_render_payload_carries_tile_metadata():
    data = b"\x89PNG\r\n\x1a\n" + b"t" * 64
    payload = render_payload.encode(
        "job9",
        12,
        "png",
        1920,
        1080,
        data,
        tile=5,
        tiles_x=3,
        tiles_y=2,
        tile_x=1280,
        tile_y=540,
        tile_w=640,
        tile_h=540,
    )
    body = render_payload.decode(payload)
    assert body["tile"] == 5
    assert (body["tiles_x"], body["tiles_y"]) == (3, 2)
    assert (body["tile_x"], body["tile_y"], body["tile_w"], body["tile_h"]) == (1280, 540, 640, 540)
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


def test_tile_geometry_covers_the_frame_without_gaps():
    assert renderjob.tile_geometry(1920, 1080, 1, 1, 0) == (0, 0, 1920, 1080)
    bounds = [renderjob.tile_geometry(1920, 1080, 3, 2, tile) for tile in range(6)]
    assert bounds[0] == (0, 0, 640, 540)
    assert bounds[1] == (640, 0, 1280, 540)
    assert bounds[2] == (1280, 0, 1920, 540)
    assert bounds[3] == (0, 540, 640, 1080)
    assert bounds[5] == (1280, 540, 1920, 1080)
    for row in range(2):
        row_bounds = bounds[row * 3 : (row + 1) * 3]
        assert row_bounds[0][0] == 0
        assert row_bounds[-1][2] == 1920
        for left, right in zip(row_bounds, row_bounds[1:]):
            assert left[2] == right[0]
    assert bounds[0][3] == bounds[3][1]


def test_tile_geometry_handles_uneven_division():
    bounds = [renderjob.tile_geometry(101, 100, 3, 1, tile) for tile in range(3)]
    assert bounds[0][0] == 0
    assert bounds[-1][2] == 101
    for left, right in zip(bounds, bounds[1:]):
        assert left[2] == right[0]


def test_write_tile_uses_the_tiles_directory(tmp_path):
    path = renderjob.write_tile(str(tmp_path), 7, 2, b"png")
    assert Path(path).name == "frame_00007_tile_002.png"
    assert Path(path).parent.name == "_tiles"
    assert Path(path).read_bytes() == b"png"


def test_clear_retires_the_job_and_keeps_the_summary():
    manager = renderjob.RenderManager()
    job = manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("w", 2)
    manager.complete(1, 0, "w", "a.png")
    manager.complete(2, 0, "w", "b.png")
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
    manager.complete(1, 0, "w", "a.png")
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


def test_status_reports_eta_and_worker_detail():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 10, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("ada", 3)
    manager.complete(1, 0, "ada", "a.png")
    status = manager.status()
    assert status["percent"] == 10.0
    assert status["worker_detail"]["ada"]["done"] == 1
    assert status["worker_detail"]["ada"]["claimed"] == 2


def test_retry_after_failure_resets_notified():
    manager = renderjob.RenderManager()
    manager.create("Test", 1, 2, 1, 0, 0, "PNG", "", "out")
    manager.start()
    manager.claim("w", 2)
    manager.complete(1, 0, "w", "a.png")
    manager.fail(2, 0, "w", "boom")
    status = manager.status()
    assert status["finished"] is True
    assert status["failed"] == 1
    assert status["percent"] == 100.0
    assert manager.retry_failed() == 1
    assert manager.job().notified is False
    assert manager.status()["finished"] is False
