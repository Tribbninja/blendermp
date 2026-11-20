from core import perf


def test_budget_records_and_reports():
    budget = perf.Budget("apply", 100.0)
    with perf.measure(budget):
        pass
    assert len(budget.samples) == 1
    assert budget.p95() >= 0.0
    assert budget.ok()
    report = perf.report({"apply": budget})
    assert report["apply"]["ok"] is True
    assert "p99_ms" in report["apply"]


def test_registry_snapshot_and_violations():
    registry = perf.PerfRegistry({"fast": 100.0, "slow": 0.0})
    registry.record("fast", 1.0)
    registry.record("slow", 5.0)
    snapshot = registry.snapshot()
    assert snapshot["budgets"]["fast"]["ok"] is True
    assert "slow" in snapshot["violations"]
    registry.reset()
    assert registry.budget("fast").samples == []
