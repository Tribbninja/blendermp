"""Session-based distributed render job.

Unlike :mod:`renderfarm` (a LAN HTTP server), this module tracks a render job
that travels over the existing host-authoritative session. The host creates one
job at a time, asks peers to join, assigns work in small batches, and collects
the rendered images.

A job is a grid of tiles per frame. With a 1x1 grid every frame is one chunk
(the original behaviour). With a larger grid a heavy frame is split across
workers, which each render one border region and upload only that tile; the host
composites the tiles into the final frame.

The module is pure standard library and thread-safe, so the addon can drive it
from Blender's main thread while the session thread receives frames.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_BATCH = 5
CLAIM_TIMEOUT = 60.0
CONSENT_TIMEOUT = 45.0
MAX_TILES = 16


@dataclass
class RenderChunk:
    frame: int
    tile: int = 0
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
    tiles_x: int = 1
    tiles_y: int = 1
    chunks: dict[tuple[int, int], RenderChunk] = field(default_factory=dict)
    accepted: set[str] = field(default_factory=set)
    declined: set[str] = field(default_factory=set)
    asked: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    started: bool = False
    finished: bool = False
    stopped: bool = False
    notified: bool = False
    error: str = ""

    @property
    def tile_count(self) -> int:
        return max(1, self.tiles_x) * max(1, self.tiles_y)

    def frame_complete(self, frame: int) -> bool:
        for tile in range(self.tile_count):
            chunk = self.chunks.get((frame, tile))
            if chunk is None or chunk.status != "done":
                return False
        return True


class RenderManager:
    """Host-side render job state."""

    def __init__(self, max_batch: int = MAX_BATCH, persist_path: str | Path | None = None):
        self.max_batch = max(1, min(32, int(max_batch)))
        self._lock = threading.Lock()
        self._job: RenderJob | None = None
        self._last: dict | None = None
        self._persist_path = Path(persist_path) if persist_path else None
        self._last_save = 0.0

    def active(self) -> bool:
        with self._lock:
            return self._job is not None and not self._job.finished

    def job(self) -> RenderJob | None:
        with self._lock:
            return self._job

    # --------------------------------------------------------------- persistence
    def _snapshot_locked(self, job: RenderJob) -> dict:
        return {
            "version": 1,
            "id": job.id,
            "name": job.name,
            "start": job.start,
            "end": job.end,
            "step": job.step,
            "width": job.width,
            "height": job.height,
            "format": job.format,
            "engine": job.engine,
            "output_dir": job.output_dir,
            "fps": job.fps,
            "tiles_x": job.tiles_x,
            "tiles_y": job.tiles_y,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "started": job.started,
            "finished": job.finished,
            "stopped": job.stopped,
            "error": job.error,
            "chunks": [
                {
                    "frame": chunk.frame,
                    "tile": chunk.tile,
                    "status": chunk.status,
                    "worker": chunk.worker,
                    "path": chunk.path,
                    "error": chunk.error,
                }
                for chunk in job.chunks.values()
                if chunk.status in {"done", "failed"}
            ],
        }

    def save(self, force: bool = False) -> None:
        """Write the active job so a crash can resume it."""
        if self._persist_path is None:
            return
        now = time.time()
        with self._lock:
            if not force and now - self._last_save < 2.0:
                return
            self._last_save = now
            job = self._job
            if job is None:
                return
            data = self._snapshot_locked(job)
        try:
            path = self._persist_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)
        except OSError:
            pass

    def load(self) -> bool:
        """Restore an unfinished job. Claimed chunks become pending again."""
        if self._persist_path is None:
            return False
        path = self._persist_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or int(data.get("version", 0) or 0) != 1:
            return False
        if data.get("finished") or data.get("stopped"):
            self._delete()
            return False
        try:
            start = int(data.get("start", 0))
            end = int(data.get("end", 0))
            step = max(1, int(data.get("step", 1) or 1))
            width = max(0, int(data.get("width", 0) or 0))
            height = max(0, int(data.get("height", 0) or 0))
            output_dir = str(data.get("output_dir", ""))[:512]
            if not output_dir:
                return False
            if end < start:
                start, end = end, start
            cols = max(1, min(MAX_TILES, int(data.get("tiles_x", 1) or 1)))
            rows = max(1, min(MAX_TILES, int(data.get("tiles_y", 1) or 1)))
            job = RenderJob(
                id=str(data.get("id", ""))[:32] or secrets.token_hex(8),
                name=str(data.get("name", "render"))[:80],
                start=start,
                end=end,
                step=step,
                width=width,
                height=height,
                format=str(data.get("format", "PNG"))[:8].upper(),
                engine=str(data.get("engine", ""))[:32],
                output_dir=output_dir,
                fps=max(1, min(240, int(data.get("fps", 24) or 24))),
                tiles_x=cols,
                tiles_y=rows,
                started=bool(data.get("started")),
            )
        except (TypeError, ValueError):
            return False
        for frame in range(start, end + 1, step):
            for tile in range(cols * rows):
                job.chunks[(frame, tile)] = RenderChunk(frame, tile)
        for item in data.get("chunks", []):
            if not isinstance(item, dict):
                continue
            try:
                key = (int(item.get("frame", 0)), int(item.get("tile", 0)))
            except (TypeError, ValueError):
                continue
            chunk = job.chunks.get(key)
            if chunk is None:
                continue
            status = str(item.get("status", ""))
            if status not in {"done", "failed"}:
                continue
            chunk.status = status
            chunk.worker = str(item.get("worker", ""))[:48]
            chunk.path = str(item.get("path", ""))[:512]
            chunk.error = str(item.get("error", ""))[:300]
        job.accepted.clear()
        job.declined.clear()
        job.asked.clear()
        job.notified = False
        job.created_at = time.time()
        job.started_at = time.time() if job.started else 0.0
        job.finished = False
        job.stopped = False
        job.error = ""
        if all(chunk.status in {"done", "failed"} for chunk in job.chunks.values()):
            self._delete()
            return False
        with self._lock:
            if self._job is not None and not self._job.finished:
                return False
            self._job = job
        return True

    def _delete(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.unlink()
        except OSError:
            pass

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
        tiles_x: int = 1,
        tiles_y: int = 1,
    ) -> RenderJob | None:
        with self._lock:
            if self._job is not None and not self._job.finished:
                return None
            lo, hi = (int(start), int(end)) if int(start) <= int(end) else (int(end), int(start))
            stride = max(1, int(step or 1))
            cols = max(1, min(MAX_TILES, int(tiles_x or 1)))
            rows = max(1, min(MAX_TILES, int(tiles_y or 1)))
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
                tiles_x=cols,
                tiles_y=rows,
            )
            for frame in range(lo, hi + 1, stride):
                for tile in range(cols * rows):
                    job.chunks[(frame, tile)] = RenderChunk(frame, tile)
            self._job = job
        self.save(force=True)
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
        self.save(force=True)
        return True

    def claim(self, worker: str, count: int | None = None) -> list[tuple[int, int]]:
        now = time.time()
        limit = max(1, min(32, int(count or self.max_batch)))
        with self._lock:
            job = self._job
            if job is None or not job.started or job.finished:
                return []
            assigned: list[tuple[int, int]] = []
            for chunk in job.chunks.values():
                if len(assigned) >= limit:
                    break
                stale = chunk.status == "claimed" and now - chunk.claimed_at > CLAIM_TIMEOUT
                if chunk.status == "pending" or stale:
                    chunk.status = "claimed"
                    chunk.worker = str(worker)[:48]
                    chunk.claimed_at = now
                    chunk.error = ""
                    assigned.append((chunk.frame, chunk.tile))
            return assigned

    def complete(self, frame: int, tile: int, worker: str, path: str) -> bool:
        """Mark a chunk done. Returns True when its whole frame is ready."""
        ready = False
        with self._lock:
            job = self._job
            if job is None:
                return False
            chunk = job.chunks.get((int(frame), int(tile)))
            if chunk is None:
                return False
            chunk.status = "done"
            chunk.worker = str(worker or chunk.worker)[:48]
            chunk.path = str(path)[:512]
            chunk.error = ""
            self._maybe_finish(job)
            ready = job.frame_complete(int(frame))
        self.save()
        return ready

    def fail(self, frame: int, tile: int, worker: str, error: str) -> bool:
        with self._lock:
            job = self._job
            if job is None:
                return False
            chunk = job.chunks.get((int(frame), int(tile)))
            if chunk is None:
                return False
            chunk.status = "failed"
            chunk.worker = str(worker or chunk.worker)[:48]
            chunk.error = str(error)[:300]
            self._maybe_finish(job)
        self.save()
        return True

    def fail_frame(self, frame: int, worker: str, error: str) -> int:
        """Mark every tile of a frame failed so Retry re-renders it."""
        with self._lock:
            job = self._job
            if job is None:
                return 0
            count = 0
            for (chunk_frame, _tile), chunk in job.chunks.items():
                if chunk_frame != int(frame):
                    continue
                chunk.status = "failed"
                chunk.worker = str(worker or chunk.worker)[:48]
                chunk.error = str(error)[:300]
                count += 1
            if count:
                job.finished = False
                job.notified = False
                self._maybe_finish(job)
        self.save()
        return count

    def release(self, worker: str) -> None:
        """Return a disconnected worker's claimed chunks to the queue."""
        with self._lock:
            job = self._job
            if job is None:
                return
            for chunk in job.chunks.values():
                if chunk.status == "claimed" and chunk.worker == str(worker)[:48]:
                    chunk.status = "pending"
                    chunk.worker = ""
                    chunk.claimed_at = 0.0

    def cancel(self) -> None:
        with self._lock:
            if self._job is not None:
                self._job.finished = True
        self._delete()

    def stop(self) -> bool:
        """Stop the job now and keep the frames already written to disk."""
        with self._lock:
            job = self._job
            if job is None or job.finished:
                return False
            job.finished = True
            job.stopped = True
        self.save(force=True)
        return True

    def clear(self) -> None:
        """Retire the finished job and keep its summary for the UI."""
        with self._lock:
            if self._job is not None:
                self._last = self._status_locked(self._job)
            self._job = None
        self._delete()

    def _maybe_finish(self, job: RenderJob) -> None:
        if job.finished:
            return
        if all(chunk.status in {"done", "failed"} for chunk in job.chunks.values()):
            job.finished = True

    def all_done(self) -> bool:
        with self._lock:
            job = self._job
            if job is None:
                return False
            return all(chunk.status == "done" for chunk in job.chunks.values())

    def missing(self) -> list[tuple[int, int]]:
        with self._lock:
            job = self._job
            if job is None:
                return []
            return [key for key, chunk in job.chunks.items() if chunk.status != "done"]

    def retry_failed(self) -> int:
        with self._lock:
            job = self._job
            if job is None:
                return 0
            count = 0
            for chunk in job.chunks.values():
                if chunk.status == "failed":
                    chunk.status = "pending"
                    chunk.error = ""
                    count += 1
            if count:
                job.finished = False
                job.notified = False
        if count:
            self.save(force=True)
        return count

    def status(self) -> dict:
        with self._lock:
            if self._job is not None:
                return self._status_locked(self._job)
            if self._last is not None:
                return dict(self._last)
            return {"active": False}

    def _status_locked(self, job: RenderJob) -> dict:
        total = len(job.chunks)
        done = sum(1 for chunk in job.chunks.values() if chunk.status == "done")
        failed = sum(1 for chunk in job.chunks.values() if chunk.status == "failed")
        claimed = sum(1 for chunk in job.chunks.values() if chunk.status == "claimed")
        pending = total - done - failed - claimed
        processed = done + failed
        elapsed = max(0.0, time.time() - job.started_at) if job.started_at else 0.0
        rate = processed / elapsed if elapsed > 0 and processed > 0 else 0.0
        remaining = (total - processed) / rate if rate > 0 else 0.0
        frames = list(range(job.start, job.end + 1, job.step))
        frames_done = sum(1 for frame in frames if job.frame_complete(frame))
        workers: dict[str, dict] = {}
        for chunk in job.chunks.values():
            if not chunk.worker:
                continue
            entry = workers.setdefault(chunk.worker, {"done": 0, "failed": 0, "claimed": 0})
            if chunk.status in entry:
                entry[chunk.status] += 1
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
            "tiles_x": job.tiles_x,
            "tiles_y": job.tiles_y,
            "output_dir": job.output_dir,
            "frames": len(frames),
            "frames_done": frames_done,
            "total": total,
            "done": done,
            "failed": failed,
            "claimed": claimed,
            "pending": pending,
            "percent": round(processed * 100.0 / max(1, total), 1),
            "elapsed": round(elapsed, 1),
            "eta_seconds": round(remaining, 1),
            "workers": sorted(job.accepted),
            "worker_detail": workers,
            "declined": sorted(job.declined),
            "started": job.started,
            "finished": job.finished,
            "stopped": job.stopped,
            "error": job.error,
        }


def tile_geometry(width: int, height: int, tiles_x: int, tiles_y: int, tile: int) -> tuple[int, int, int, int]:
    """Pixel bounds of one tile in the full frame (bottom-left origin).

    Tile 0 is the bottom-left tile; indices run left to right, then bottom to
    top. The host and every worker use this same function so the stitched
    result lines up exactly.
    """
    fw = max(1, int(width))
    fh = max(1, int(height))
    cols = max(1, int(tiles_x or 1))
    rows = max(1, int(tiles_y or 1))
    index = max(0, min(cols * rows - 1, int(tile or 0)))
    col = index % cols
    row = index // cols
    x0 = round(col * fw / cols)
    x1 = round((col + 1) * fw / cols)
    y0 = round(row * fh / rows)
    y1 = round((row + 1) * fh / rows)
    return x0, y0, x1, y1


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


def write_tile(output_dir: str, frame: int, tile: int, data: bytes) -> str:
    directory = Path(output_dir) / "_tiles"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"frame_{int(frame):05d}_tile_{int(tile):03d}.png"
    temp = final.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(final)
    return str(final)
