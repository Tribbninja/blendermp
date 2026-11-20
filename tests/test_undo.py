from core.undo import UndoLog


def test_undo_skips_when_value_changed():
    log = UndoLog()
    log.push("obj", "transform", (0.0,), (1.0,), now=0.0)
    entry = log.pop(now=1.0)
    assert entry is not None
    assert entry.before == (0.0,)
    assert log.pop(now=1.0) is None


def test_undo_window_and_cap():
    log = UndoLog(max_ops=3, window=10.0)
    for index in range(5):
        log.push("obj", "transform", index, index + 1, now=float(index))
    assert len(log) == 3
    assert log.pop(now=4.0).after == 5
    assert log.pop(now=20.0) is None
    assert len(log) == 0


def test_undo_ignores_noop():
    log = UndoLog()
    log.push("obj", "transform", (1.0,), (1.0,), now=0.0)
    assert len(log) == 0
