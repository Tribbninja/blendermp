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
