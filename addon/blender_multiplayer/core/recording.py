"""Session recording and replay for Blendermp.

A recorder captures the stream of events that a session produces (transforms,
mesh deltas, presence, chat, leases, ...) into a JSONL file. A replayer feeds
them back through a callback at the original rate, or as fast as the host
allows. Recordings are version-tagged; unsupported event kinds are skipped
rather than failing the whole replay.

Nothing here imports ``bpy``; it is testable with plain Python.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

RECORDING_VERSION = 1


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
            {"v": RECORDING_VERSION, "kind": self.kind, "stamp": self.stamp, "body": _make_serializable(self.body)},
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
        if not isinstance(kind, str) or not isinstance(stamp, (int, float)) or not isinstance(body, dict):
            raise ValueError("malformed recording event")
        return cls(kind=kind, stamp=float(stamp), body=_restore(body))


class Recorder:
    """Append-only recorder writing one JSON object per line."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None
        self._events = 0

    @property
    def is_active(self) -> bool:
        return self._handle is not None

    @property
    def event_count(self) -> int:
        return self._events

    def start(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._events = 0

    def record(self, kind: str, body: Any) -> None:
        if self._handle is None:
            return
        if not isinstance(kind, str) or not kind:
            return
        if not isinstance(body, dict):
            body = {"value": body}
        self._handle.write(Event(kind=kind, stamp=time.time(), body=body).to_line() + "\n")
        self._handle.flush()
        self._events += 1

    def stop(self) -> None:
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

    def play(self, sink: Callable[[str, dict], None], speed: float = 1.0, max_events: int | None = None) -> int:
        """Replay events into ``sink(kind, body)``. Returns the number delivered."""
        if speed <= 0:
            speed = 1.0
        delivered = 0
        start = self.events[0].stamp if self.events else time.time()
        for event in self.events:
            if max_events is not None and delivered >= max_events:
                break
            if delivered > 0:
                delay = (event.stamp - self.events[delivered - 1].stamp) / speed
                if delay > 0:
                    time.sleep(min(delay, 1.0))
            try:
                sink(event.kind, event.body)
            except Exception:  # noqa: BLE001
                pass
            delivered += 1
        return delivered
