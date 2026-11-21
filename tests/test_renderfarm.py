import json
import urllib.request

from core import renderfarm


def test_queue_claims_and_completes():
    queue = renderfarm.RenderFarmQueue()
    job = queue.create_job("test", 1, 4, 1, "out")
    assert len(job.chunks) == 4
    claimed = queue.claim("alice")
    assert claimed is not None
    claimed_job, chunk = claimed
    assert claimed_job.id == job.id
    assert chunk.frame == 1
    assert queue.complete(job.id, 1, "alice", "frame_0001.png")
    status = queue.status()
    assert status["jobs"][0]["done"] == 1
    assert status["jobs"][0]["pending"] == 3
    assert status["worker_count"] == 1


def test_queue_fails_and_reclaims_stale():
    queue = renderfarm.RenderFarmQueue()
    job = queue.create_job("test", 1, 2, 1, "out")
    queue.claim("alice")
    assert queue.fail(job.id, 1, "alice", "boom")
    status = queue.status()
    assert status["jobs"][0]["failed"] == 1
    # A stale claim is handed to a new worker.
    queue.claim("alice")
    job2 = queue.create_job("test2", 10, 10, 1, "out")
    claimed = queue.claim("bob", stale_after=0.0)
    assert claimed is not None
    assert claimed[0].id in {job.id, job2.id}


def test_server_and_client_roundtrip(tmp_path):
    server = renderfarm.FarmServer(port=0, output_dir=str(tmp_path))
    server.start()
    try:
        job = server.queue.create_job("e2e", 7, 7, 1, str(tmp_path))
        client = renderfarm.FarmClient(
            f"http://127.0.0.1:{server.port}/", server.token, "worker-1"
        )
        work = client.claim()
        assert work is not None
        assert work.frame == 7
        assert work.job_id == job.id
        png = renderfarm.PNG_MAGIC + b"fake png bytes"
        assert client.submit(work, png)
        status = server.queue.status()
        assert status["jobs"][0]["done"] == 1
        assert (tmp_path / "frame_00007.png").read_bytes() == png
    finally:
        server.stop()


def test_server_rejects_bad_token(tmp_path):
    server = renderfarm.FarmServer(port=0, output_dir=str(tmp_path))
    server.start()
    try:
        server.queue.create_job("e2e", 1, 1, 1, str(tmp_path))
        client = renderfarm.FarmClient(
            f"http://127.0.0.1:{server.port}/", "wrong", "worker"
        )
        assert client.claim() is None
        assert "403" in client.last_error or client.last_error
    finally:
        server.stop()


def test_server_rejects_non_png(tmp_path):
    server = renderfarm.FarmServer(port=0, output_dir=str(tmp_path))
    server.start()
    try:
        server.queue.create_job("e2e", 1, 1, 1, str(tmp_path))
        client = renderfarm.FarmClient(
            f"http://127.0.0.1:{server.port}/", server.token, "worker"
        )
        work = client.claim()
        assert work is not None
        assert not client.submit(work, b"not a png")
        assert server.queue.status()["jobs"][0]["done"] == 0
    finally:
        server.stop()
