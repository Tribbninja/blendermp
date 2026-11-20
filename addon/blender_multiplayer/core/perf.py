"""Performance budgets and measurement helpers for Blendermp.

The addon already records apply timings in ``runtime["apply_times"]``. This
module formalises the budgets from the roadmap and gives a small context
manager that any hot path can use.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Budget:
    name: str
    max_ms: float
    samples: list[float] = field(default_factory=list)

    def record(self, value_ms: float) -> None:
        self.samples.append(float(value_ms))
        if len(self.samples) > 240:
            del self.samples[: len(self.samples) - 240]

    def p95(self) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = max(0, min(len(ordered) - 1, int(0.95 * (len(ordered) - 1))))
        return ordered[index]

    def ok(self) -> bool:
        return self.p95() <= self.max_ms


@contextmanager
def measure(budget: Budget | None = None):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000.0
        if budget is not None:
            budget.record(elapsed)


def report(budgets: dict[str, Budget]) -> dict[str, Any]:
    return {
        name: {"max_ms": budget.max_ms, "p95_ms": round(budget.p95(), 3), "ok": budget.ok()}
        for name, budget in budgets.items()
    }
