"""Session recording and replay for Blendermp.

A recorder captures the stream of events that a session produces (transforms,
mesh deltas, presence, chat, leases, ...) into a JSONL file. A replayer feeds
them back through a callback at the original rate, or as fast as the host
allows. Recordings are version-tagged; unsupported event kinds are skipped
rather than failing the whole replay.

The recorder also supports live subscribers, which is how the broadcast page
and a debug overlay can watch the same event stream without reading the file.

Nothing here imports ``bpy``; it is testable with plain Python.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

RECORDING_VERSION = 1
MAX_RECORD_BODY_BYTES = 64 * 1024 * 1024


def _make_serializable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_make_serializable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _make_serializable(item) for key, item in value.items()}
    return value


def _restore(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1 and "__bytes__" in value:
        try:
            return base64.b64decode(value["__bytes__"])
        except Exception:  # noqa: BLE001
            return value
    if isinstance(value, list):
        return [_restore(item) for item in value]
    if isinstance(value, dict):
        return {key: _restore(item) for key, item in value.items()}
    return value


@dataclass
class Event:
    kind: str
    stamp: float
    body: dict[str, Any]

    def to_line(self) -> str:
        return json.dumps(
            {
                "v": RECORDING_VERSION,
                "kind": self.kind,
                "stamp": self.stamp,
                "body": _make_serializable(self.body),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_line(cls, line: str) -> "Event":
        payload = json.loads(line)
        if not isinstance(payload, dict) or payload.get("v") != RECORDING_VERSION:
            raise ValueError("unsupported recording version")
        kind = payload.get("kind")
        stamp = payload.get("stamp")
        body = payload.get("body")
        if (
            not isinstance(kind, str)
            or not isinstance(stamp, (int, float))
            or not isinstance(body, dict)
        ):
            raise ValueError("malformed recording event")
        return cls(kind=kind, stamp=float(stamp), body=_restore(body))


@dataclass
class RecordingInfo:
    path: Path
    size: int
    modified: float
    events: int = 0
    duration: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "modified": self.modified,
            "events": self.events,
            "duration": round(self.duration, 2),
        }


class Recorder:
    """Append-only recorder writing one JSON object per line."""

    def __init__(self, path: str | Path, max_body_bytes: int = MAX_RECORD_BODY_BYTES):
        self.path = Path(path)
        self.max_body_bytes = int(max_body_bytes)
        self._handle = None
        self._events = 0
        self._dropped = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[str, dict[str, Any]], None]] = []
        self._started = 0.0

    @property
    def is_active(self) -> bool:
        return self._handle is not None

    @property
    def event_count(self) -> int:
        return self._events

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def duration(self) -> float:
        return max(0.0, time.time() - self._started) if self._started else 0.0

    def subscribe(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def start(self) -> None:
        with self._lock:
            if self._handle is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
            self._events = 0
            self._dropped = 0
            self._started = time.time()

    def record(self, kind: str, body: Any) -> None:
        if not isinstance(kind, str) or not kind:
            return
        if not isinstance(body, dict):
            body = {"value": body}
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            try:
                line = Event(kind=kind, stamp=time.time(), body=body).to_line()
            except (TypeError, ValueError):
                line = Event(
                    kind=kind, stamp=time.time(), body={"value": repr(body)[:2000]}
                ).to_line()
            if len(line) > self.max_body_bytes:
                self._dropped += 1
                line = Event(
                    kind=kind,
                    stamp=time.time(),
                    body={"dropped": True, "size": len(line)},
                ).to_line()
            try:
                handle.write(line + "\n")
                handle.flush()
            except OSError:
                return
            self._events += 1
        for callback in list(self._subscribers):
            try:
                callback(kind, body)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


class Replay:
    """Load a recording and feed its events to a sink."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.events: list[Event] = []
        self._cancel = threading.Event()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.events.append(Event.from_line(line))
                except ValueError:
                    continue
        self.events.sort(key=lambda event: event.stamp)

    @property
    def duration(self) -> float:
        if len(self.events) < 2:
            return 0.0
        return max(0.0, self.events[-1].stamp - self.events[0].stamp)

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.path.name,
            "events": len(self.events),
            "duration": round(self.duration, 2),
            "kinds": self.kinds(),
        }

    def cancel(self) -> None:
        self._cancel.set()

    def play(
        self,
        sink: Callable[[str, dict], None],
        speed: float = 1.0,
        max_events: int | None = None,
    ) -> int:
        """Replay events into ``sink(kind, body)``. Returns the number delivered."""
        if speed <= 0:
            speed = 1.0
        self._cancel.clear()
        delivered = 0
        previous_stamp = self.events[0].stamp if self.events else time.time()
        for event in self.events:
            if self._cancel.is_set():
                break
            if max_events is not None and delivered >= max_events:
                break
            delay = (event.stamp - previous_stamp) / speed
            if delay > 0:
                # Sleep in slices so cancel() is responsive.
                remaining = min(delay, 2.0)
                while remaining > 0 and not self._cancel.is_set():
                    slice_time = min(remaining, 0.05)
                    time.sleep(slice_time)
                    remaining -= slice_time
            previous_stamp = event.stamp
            try:
                sink(event.kind, event.body)
            except Exception:  # noqa: BLE001
                pass
            delivered += 1
        return delivered


def list_recordings(directory: str | Path, limit: int = 20) -> list[RecordingInfo]:
    """Return recent recordings, newest first, with event counts."""
    root = Path(directory)
    if not root.exists():
        return []
    paths = sorted(
        root.glob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[: max(1, int(limit))]
    result: list[RecordingInfo] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        info = RecordingInfo(path=path, size=stat.st_size, modified=stat.st_mtime)
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = 0.0
                last = 0.0
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stamp = payload.get("stamp")
                    if not isinstance(stamp, (int, float)):
                        continue
                    if not first:
                        first = float(stamp)
                    last = float(stamp)
                    info.events += 1
                if first and last:
                    info.duration = max(0.0, last - first)
        except OSError:
            pass
        result.append(info)
    return result
