"""Scoped undo for shared edits.

Only local operations are recorded, and an undo is applied only when the
current value still matches the value this client produced. Remote edits are
never reverted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MAX_OPS = 200
WINDOW = 120.0


@dataclass(slots=True)
class UndoEntry:
    uid: str
    kind: str
    before: object
    after: object
    ts: float


class UndoLog:
    def __init__(self, max_ops: int = MAX_OPS, window: float = WINDOW):
        self.max_ops = max(1, max_ops)
        self.window = max(1.0, window)
        self._entries: list[UndoEntry] = []

    def push(self, uid: str, kind: str, before, after, now: float | None = None) -> None:
        if not uid or before == after:
            return
        self._entries.append(UndoEntry(uid, kind, before, after, time.monotonic() if now is None else now))
        self._trim(time.monotonic() if now is None else now)

    def pop(self, now: float | None = None) -> UndoEntry | None:
        moment = time.monotonic() if now is None else now
        self._trim(moment)
        return self._entries.pop() if self._entries else None

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def _trim(self, now: float) -> None:
        while self._entries and now - self._entries[0].ts > self.window:
            self._entries.pop(0)
        if len(self._entries) > self.max_ops:
            del self._entries[: len(self._entries) - self.max_ops]
