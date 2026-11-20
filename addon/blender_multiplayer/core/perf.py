"""Performance budgets and measurement helpers for Blendermp.

The addon records timings in ``runtime["apply_times"]``. This module formalises
the budgets from the roadmap, keeps a rolling p95 for each hot path, and
produces the small ``perf`` block that is published in ``status.json`` and
shown in the dashboard.

Budgets are deliberately conservative; the goal is to catch regressions, not to
make a slow machine look broken.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BUDGETS: dict[str, float] = {
    "tick": 8.0,
    "apply": 8.0,
    "mesh_apply": 8.0,
    "topology_read": 350.0,
    "presence": 4.0,
    "bridge_pump": 3.0,
    "ui_draw": 16.0,
}


@dataclass
class Budget:
    name: str
    max_ms: float
    samples: list[float] = field(default_factory=list)
    window: int = 240

    def record(self, value_ms: float) -> None:
        self.samples.append(float(value_ms))
        if len(self.samples) > self.window:
            del self.samples[: len(self.samples) - self.window]

    def p50(self) -> float:
        return self._percentile(0.50)

    def p95(self) -> float:
        return self._percentile(0.95)

    def p99(self) -> float:
        return self._percentile(0.99)

    def _percentile(self, fraction: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = max(0, min(len(ordered) - 1, int(fraction * (len(ordered) - 1))))
        return ordered[index]

    def ok(self) -> bool:
        return self.p95() <= self.max_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_ms": self.max_ms,
            "p50_ms": round(self.p50(), 3),
            "p95_ms": round(self.p95(), 3),
            "p99_ms": round(self.p99(), 3),
            "samples": len(self.samples),
            "ok": self.ok(),
        }


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
    return {name: budget.as_dict() for name, budget in budgets.items()}


class PerfRegistry:
    """Named budgets with a stable JSON representation."""

    def __init__(self, budgets: dict[str, float] | None = None):
        self.budgets: dict[str, Budget] = {
            name: Budget(name, max_ms)
            for name, max_ms in (budgets or DEFAULT_BUDGETS).items()
        }

    def budget(self, name: str) -> Budget:
        if name not in self.budgets:
            self.budgets[name] = Budget(name, 16.0)
        return self.budgets[name]

    def record(self, name: str, value_ms: float) -> None:
        self.budget(name).record(value_ms)

    @contextmanager
    def measure(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - start) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        data = report(self.budgets)
        violations = [name for name, item in data.items() if not item["ok"]]
        return {
            "budgets": data,
            "violations": violations,
        }

    def reset(self) -> None:
        for budget in self.budgets.values():
            budget.samples.clear()
