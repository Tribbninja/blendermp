"""Session-based distributed render job.

Unlike :mod:`renderfarm` (a LAN HTTP server), this module tracks a render job
that travels over the existing host-authoritative session. The host creates one
job at a time, asks peers to join, assigns frames in small batches, and collects
the rendered images.

The module is pure standard library and thread-safe, so the addon can drive it
from Blender's main thread while the session thread receives frames.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_BATCH = 5
CLAIM_TIMEOUT = 180.0
CONSENT_TIMEOUT = 45.0


@dataclass
class RenderFrame:
    frame: int
    status: str = "pending"  # pending | claimed | done | failed
    worker: str = ""
    claimed_at: float = 0.0
    path: str = ""
    error: str = ""


@dataclass
class RenderJob:
    id: str
    name: str
    start: int
    end: int
    step: int
    width: int
    height: int
    format: str
    engine: str
    output_dir: str
    fps: int = 24
    frames: dict[int, RenderFrame] = field(default_factory=dict)
    accepted: set[str] = field(default_factory=set)
    declined: set[str] = field(default_factory=set)
    asked: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    started: bool = False
    finished: bool = False
    stopped: bool = False
    error: str = ""


class RenderManager:
    """Host-side render job state."""

    def __init__(self, max_batch: int = MAX_BATCH):
        self.max_batch = max(1, min(32, int(max_batch)))
        self._lock = threading.Lock()
        self._job: RenderJob | None = None
        self._last: dict | None = None

    def active(self) -> bool:
        with self._lock:
            return self._job is not None and not self._job.finished

    def job(self) -> RenderJob | None:
        with self._lock:
            return self._job

    def create(
        self,
        name: str,
        start: int,
        end: int,
        step: int,
        width: int,
        height: int,
        format_hint: str,
        engine: str,
        output_dir: str,
        fps: int = 24,
    ) -> RenderJob | None:
        with self._lock:
            if self._job is not None and not self._job.finished:
                return None
            lo, hi = (int(start), int(end)) if int(start) <= int(end) else (int(end), int(start))
            stride = max(1, int(step or 1))
            job = RenderJob(
                id=secrets.token_hex(8),
                name=str(name or "render")[:80],
                start=lo,
                end=hi,
                step=stride,
                width=max(0, int(width)),
                height=max(0, int(height)),
                format=str(format_hint or "PNG")[:8].upper(),
                engine=str(engine or "")[:32],
                output_dir=str(output_dir),
                fps=max(1, min(240, int(fps or 24))),
            )
            for frame in range(lo, hi + 1, stride):
                job.frames[frame] = RenderFrame(frame)
            self._job = job
            return job

    def ask(self, worker: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.asked.add(str(worker)[:48])

    def accept(self, worker: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.accepted.add(str(worker)[:48])
                self._job.declined.discard(str(worker)[:48])

    def decline(self, worker: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.declined.add(str(worker)[:48])
                self._job.accepted.discard(str(worker)[:48])

    def start(self) -> bool:
        with self._lock:
            if self._job is None or self._job.started or self._job.finished:
                return False
            self._job.started = True
            self._job.started_at = time.time()
            return True

    def claim(self, worker: str, count: int | None = None) -> list[int]:
        now = time.time()
        limit = max(1, min(32, int(count or self.max_batch)))
        with self._lock:
            job = self._job
            if job is None or not job.started or job.finished:
                return []
            assigned: list[int] = []
            for chunk in job.frames.values():
                if len(assigned) >= limit:
                    break
                stale = chunk.status == "claimed" and now - chunk.claimed_at > CLAIM_TIMEOUT
                if chunk.status == "pending" or stale:
                    chunk.status = "claimed"
                    chunk.worker = str(worker)[:48]
                    chunk.claimed_at = now
                    chunk.error = ""
                    assigned.append(chunk.frame)
            return assigned

    def complete(self, frame: int, worker: str, path: str) -> bool:
        with self._lock:
            job = self._job
            if job is None:
                return False
            chunk = job.frames.get(int(frame))
            if chunk is None:
                return False
            chunk.status = "done"
            chunk.worker = str(worker or chunk.worker)[:48]
            chunk.path = str(path)[:512]
            chunk.error = ""
            self._maybe_finish(job)
            return True

    def fail(self, frame: int, worker: str, error: str) -> bool:
        with self._lock:
            job = self._job
            if job is None:
                return False
            chunk = job.frames.get(int(frame))
            if chunk is None:
                return False
            chunk.status = "failed"
            chunk.worker = str(worker or chunk.worker)[:48]
            chunk.error = str(error)[:300]
            self._maybe_finish(job)
            return True

    def release(self, worker: str) -> None:
        """Return a disconnected worker's claimed frames to the queue."""
        with self._lock:
            job = self._job
            if job is None:
                return
            for chunk in job.frames.values():
                if chunk.status == "claimed" and chunk.worker == str(worker)[:48]:
                    chunk.status = "pending"
                    chunk.worker = ""
                    chunk.claimed_at = 0.0

    def cancel(self) -> None:
        with self._lock:
            if self._job is not None:
                self._job.finished = True

    def stop(self) -> bool:
        """Stop the job now and keep the frames already written to disk."""
        with self._lock:
            job = self._job
            if job is None or job.finished:
                return False
            job.finished = True
            job.stopped = True
            return True

    def clear(self) -> None:
        """Retire the finished job and keep its summary for the UI."""
        with self._lock:
            if self._job is not None:
                self._last = self._status_locked(self._job)
            self._job = None

    def _maybe_finish(self, job: RenderJob) -> None:
        if job.finished:
            return
        if all(chunk.status in {"done", "failed"} for chunk in job.frames.values()):
            job.finished = True

    def all_done(self) -> bool:
        with self._lock:
            job = self._job
            if job is None:
                return False
            return all(chunk.status == "done" for chunk in job.frames.values())

    def missing(self) -> list[int]:
        with self._lock:
            job = self._job
            if job is None:
                return []
            return [chunk.frame for chunk in job.frames.values() if chunk.status != "done"]

    def retry_failed(self) -> int:
        with self._lock:
            job = self._job
            if job is None:
                return 0
            count = 0
            for chunk in job.frames.values():
                if chunk.status == "failed":
                    chunk.status = "pending"
                    chunk.error = ""
                    count += 1
            if count:
                job.finished = False
            return count

    def status(self) -> dict:
        with self._lock:
            if self._job is not None:
                return self._status_locked(self._job)
            if self._last is not None:
                return dict(self._last)
            return {"active": False}

    def _status_locked(self, job: RenderJob) -> dict:
        total = len(job.frames)
        done = sum(1 for chunk in job.frames.values() if chunk.status == "done")
        failed = sum(1 for chunk in job.frames.values() if chunk.status == "failed")
        claimed = sum(1 for chunk in job.frames.values() if chunk.status == "claimed")
        pending = total - done - failed - claimed
        return {
            "active": not job.finished,
            "id": job.id,
            "name": job.name,
            "start": job.start,
            "end": job.end,
            "step": job.step,
            "width": job.width,
            "height": job.height,
            "format": job.format,
            "engine": job.engine,
            "fps": job.fps,
            "output_dir": job.output_dir,
            "total": total,
            "done": done,
            "failed": failed,
            "claimed": claimed,
            "pending": pending,
            "percent": round(done * 100.0 / max(1, total), 1),
            "workers": sorted(job.accepted),
            "declined": sorted(job.declined),
            "started": job.started,
            "finished": job.finished,
            "stopped": job.stopped,
            "error": job.error,
        }


def frame_filename(frame: int, format_hint: str) -> str:
    extension = "png" if str(format_hint).upper() == "PNG" else str(format_hint).lower()
    return f"frame_{int(frame):05d}.{extension}"


def write_frame(output_dir: str, frame: int, format_hint: str, data: bytes) -> str:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / frame_filename(frame, format_hint)
    temp = final.with_suffix(final.suffix + ".tmp")
    temp.write_bytes(data)
    temp.replace(final)
    return str(final)
