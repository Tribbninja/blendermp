"""LAN-only distributed render farm for Blendermp.

The host splits a frame range into one chunk per frame. Peers on the same LAN
claim a frame, render it from their local (already synced) scene, and upload the
PNG back to the host. The host saves the frames into one output directory.

This is deliberately **LAN-only**:

* the server binds ``0.0.0.0`` so peers can reach it, but every request needs a
  random token;
* result uploads are capped and validated as PNG;
* there is no internet discovery, no relay and no remote code execution.

The module is pure standard library, so the queue and HTTP transport are tested
without Blender. The addon supplies the actual ``bpy.ops.render.render`` call on
Blender's main thread.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_RESULT_BYTES = 64 * 1024 * 1024
STALE_CLAIM_SECONDS = 600.0
WORKER_TTL = 90.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class RenderChunk:
    frame: int
    status: str = "pending"  # pending | claimed | done | failed
    worker: str = ""
    claimed_at: float = 0.0
    result: str = ""
    error: str = ""


@dataclass
class RenderJob:
    id: str
    name: str
    frame_start: int
    frame_end: int
    frame_step: int
    output_dir: str
    width: int = 0
    height: int = 0
    created_at: float = field(default_factory=time.time)
    chunks: list[RenderChunk] = field(default_factory=list)


@dataclass
class FarmWork:
    job_id: str
    job_name: str
    frame: int
    output_name: str
    width: int = 0
    height: int = 0


class RenderFarmQueue:
    """Thread-safe frame queue shared by the HTTP server and the host UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, RenderJob] = {}
        self._workers: dict[str, float] = {}

    def create_job(
        self,
        name: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        output_dir: str,
        width: int = 0,
        height: int = 0,
    ) -> RenderJob:
        start = int(frame_start)
        end = int(frame_end)
        if end < start:
            start, end = end, start
        step = max(1, int(frame_step or 1))
        job = RenderJob(
            id=secrets.token_hex(8),
            name=str(name or "render")[:80],
            frame_start=start,
            frame_end=end,
            frame_step=step,
            output_dir=str(output_dir),
            width=max(0, int(width)),
            height=max(0, int(height)),
            chunks=[RenderChunk(frame) for frame in range(start, end + 1, step)],
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> RenderJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def claim(self, worker: str, stale_after: float = STALE_CLAIM_SECONDS):
        now = time.time()
        worker = str(worker or "worker")[:48]
        with self._lock:
            self._workers[worker] = now
            for job in self._jobs.values():
                for chunk in job.chunks:
                    if chunk.status == "pending" or (
                        chunk.status == "claimed"
                        and now - chunk.claimed_at > stale_after
                    ):
                        chunk.status = "claimed"
                        chunk.worker = worker
                        chunk.claimed_at = now
                        return job, chunk
        return None

    def complete(self, job_id: str, frame: int, worker: str, result: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return False
            for chunk in job.chunks:
                if chunk.frame != int(frame):
                    continue
                chunk.status = "done"
                chunk.worker = str(worker or chunk.worker)[:48]
                chunk.result = str(result)[:512]
                chunk.error = ""
                return True
        return False

    def fail(self, job_id: str, frame: int, worker: str, error: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return False
            for chunk in job.chunks:
                if chunk.frame != int(frame):
                    continue
                chunk.status = "failed"
                chunk.worker = str(worker or chunk.worker)[:48]
                chunk.error = str(error)[:300]
                return True
        return False

    def worker_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for stamp in self._workers.values() if now - stamp <= WORKER_TTL)

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            jobs = []
            for job in self._jobs.values():
                done = sum(1 for chunk in job.chunks if chunk.status == "done")
                failed = sum(1 for chunk in job.chunks if chunk.status == "failed")
                claimed = sum(1 for chunk in job.chunks if chunk.status == "claimed")
                total = len(job.chunks)
                jobs.append(
                    {
                        "id": job.id,
                        "name": job.name,
                        "frames": total,
                        "done": done,
                        "failed": failed,
                        "claimed": claimed,
                        "pending": total - done - failed - claimed,
                        "percent": round(done * 100.0 / max(1, total), 1),
                        "output_dir": job.output_dir,
                    }
                )
            workers = [
                name
                for name, stamp in self._workers.items()
                if now - stamp <= WORKER_TTL
            ]
        return {"jobs": jobs, "workers": sorted(workers), "worker_count": len(workers)}

    def progress_text(self) -> str:
        status = self.status()
        if not status["jobs"]:
            return "no job"
        job = status["jobs"][-1]
        text = f"{job['done']}/{job['frames']} frames ({job['percent']:.0f}%)"
        if job["failed"]:
            text += f", {job['failed']} failed"
        text += f" · {status['worker_count']} worker(s)"
        return text


class FarmServer:
    """Host-side HTTP server for LAN workers."""

    def __init__(
        self,
        port: int = 7110,
        token: str | None = None,
        output_dir: str | None = None,
        queue: RenderFarmQueue | None = None,
    ):
        self.port = int(port)
        self.token = token or secrets.token_urlsafe(18)
        self.output_dir = str(output_dir or (Path.cwd() / "renderfarm"))
        self.queue = queue or RenderFarmQueue()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""

    def start(self) -> str:
        if self._server is not None:
            return self.url
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        server = ThreadingHTTPServer(("0.0.0.0", self.port), _make_handler(self))
        server.daemon_threads = True
        server.allow_reuse_address = True
        self._server = server
        self.port = int(server.server_address[1])
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="bmp-renderfarm", daemon=True
        )
        self._thread.start()
        return self.url

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.25)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:200]

    def stop(self) -> None:
        self._stop.set()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._thread = None

    @property
    def is_active(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{_lan_ip()}:{self.port}/"

    @property
    def worker_count(self) -> int:
        return self.queue.worker_count()


def _lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def _make_handler(app: FarmServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _json(self, status: int, body: dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(body, separators=(",", ":")).encode("utf-8"),
                "application/json",
            )

        def _authorised(self, query: dict[str, list[str]]) -> bool:
            token = (query.get("token") or [""])[0]
            return bool(token) and hmac.compare_digest(str(token), str(app.token))

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                self._json(403, {"error": "forbidden"})
                return
            if parsed.path == "/status":
                self._json(200, app.queue.status())
                return
            if parsed.path == "/work":
                worker = (query.get("worker") or ["worker"])[0]
                claimed = app.queue.claim(worker)
                if claimed is None:
                    self._json(200, {"job": None})
                    return
                job, chunk = claimed
                self._json(
                    200,
                    {
                        "job": {
                            "id": job.id,
                            "name": job.name,
                            "frame_start": job.frame_start,
                            "frame_end": job.frame_end,
                            "step": job.frame_step,
                            "width": job.width,
                            "height": job.height,
                        },
                        "frame": chunk.frame,
                        "output": f"frame_{chunk.frame:05d}.png",
                    },
                )
                return
            if parsed.path == "/":
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Blendermp render farm</title>"
                    "<body style='font:14px system-ui;background:#111;color:#eee;padding:20px'>"
                    f"<h1>Blendermp render farm</h1><pre>{json.dumps(app.queue.status(), indent=2)}</pre>"
                ).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                self._json(403, {"error": "forbidden"})
                return
            job_id = (query.get("job") or [""])[0]
            frame_text = (query.get("frame") or ["0"])[0]
            worker = (query.get("worker") or ["worker"])[0]
            try:
                frame = int(frame_text)
            except ValueError:
                self._json(400, {"error": "invalid frame"})
                return
            length_text = self.headers.get("Content-Length", "0")
            try:
                length = int(length_text)
            except ValueError:
                length = 0
            if parsed.path == "/fail":
                error = (query.get("error") or ["render failed"])[0]
                app.queue.fail(job_id, frame, worker, error)
                self._json(200, {"ok": True})
                return
            if parsed.path != "/result":
                self._json(404, {"error": "not found"})
                return
            if length <= 0 or length > MAX_RESULT_BYTES:
                self._json(413, {"error": "result is too large"})
                return
            body = self.rfile.read(length)
            if not body.startswith(PNG_MAGIC):
                self._json(400, {"error": "result is not a PNG"})
                return
            job = app.queue.get(job_id)
            if job is None:
                self._json(404, {"error": "unknown job"})
                return
            output = Path(job.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            name = f"frame_{frame:05d}.png"
            temp = output / (name + ".tmp")
            final = output / name
            try:
                temp.write_bytes(body)
                os.replace(temp, final)
            except OSError as exc:
                self._json(500, {"error": f"could not save frame: {exc}"})
                return
            app.queue.complete(job_id, frame, worker, str(final))
            self._json(200, {"ok": True, "saved": str(final)})

    return Handler


class FarmClient:
    """Worker-side client. Claims frames and uploads rendered PNGs."""

    def __init__(
        self,
        url: str,
        token: str,
        worker: str,
        poll_interval: float = 1.0,
        timeout: float = 8.0,
    ):
        self.url = str(url or "").rstrip("/") + "/"
        self.token = str(token or "")
        self.worker = str(worker or "worker")[:48]
        self.poll_interval = max(0.25, float(poll_interval))
        self.timeout = max(1.0, float(timeout))
        self._work: "queue.Queue[FarmWork]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""
        self.last_status: dict[str, Any] = {}

    def _request(self, path: str, params: dict[str, str] | None = None) -> bytes:
        query = {"token": self.token, "worker": self.worker}
        if params:
            query.update(params)
        encoded = urllib.parse.urlencode(query)
        url = f"{self.url}{path}?{encoded}"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            return response.read()

    def claim(self) -> FarmWork | None:
        try:
            payload = json.loads(self._request("work").decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.last_error = str(exc)[:200]
            return None
        job = payload.get("job")
        if not isinstance(job, dict):
            return None
        return FarmWork(
            job_id=str(job.get("id", "")),
            job_name=str(job.get("name", "")),
            frame=int(payload.get("frame", 0)),
            output_name=str(payload.get("output", "frame.png")),
            width=int(job.get("width", 0) or 0),
            height=int(job.get("height", 0) or 0),
        )

    def submit(self, work: FarmWork, data: bytes) -> bool:
        query = urllib.parse.urlencode(
            {
                "token": self.token,
                "worker": self.worker,
                "job": work.job_id,
                "frame": work.frame,
            }
        )
        request = urllib.request.Request(
            f"{self.url}result?{query}",
            data=bytes(data),
            headers={"Content-Type": "image/png"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)[:200]
            return False

    def fail(self, work: FarmWork, error: str) -> None:
        query = urllib.parse.urlencode(
            {
                "token": self.token,
                "worker": self.worker,
                "job": work.job_id,
                "frame": work.frame,
                "error": str(error)[:200],
            }
        )
        try:
            with urllib.request.urlopen(f"{self.url}fail?{query}", timeout=self.timeout):
                pass
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    def status(self) -> dict[str, Any]:
        try:
            self.last_status = json.loads(self._request("status").decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
        return self.last_status

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="bmp-farm-client", daemon=True
        )
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            if self._work.qsize() == 0:
                work = self.claim()
                if work is not None:
                    self._work.put(work)
            self._stop.wait(self.poll_interval)

    def poll(self) -> FarmWork | None:
        try:
            return self._work.get_nowait()
        except Exception:  # noqa: BLE001
            return None

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
