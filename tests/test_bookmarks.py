import json

import pytest

from core import bookmarks


def entry(name="Front", **overrides):
    value = {
        "name": name,
        "matrix": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 2.0, 3.0, 1.0],
        "aspect": 1.6,
        "persp": "PERSP",
        "distance": 7.0,
    }
    value.update(overrides)
    return value


def test_sanitize_accepts_a_valid_bookmark():
    clean = bookmarks.sanitize(entry())
    assert clean["name"] == "Front"
    assert clean["distance"] == 7.0
    assert clean["persp"] == "PERSP"


def test_sanitize_rejects_bad_values():
    assert bookmarks.sanitize(None) is None
    assert bookmarks.sanitize(entry(name="")) is None
    assert bookmarks.sanitize(entry(matrix=[1.0] * 15)) is None
    assert bookmarks.sanitize(entry(matrix=[float("nan")] * 16)) is None
    assert bookmarks.sanitize(entry(persp="SIDEWAYS"))["persp"] == "PERSP"
    assert bookmarks.sanitize(entry(aspect=999))["aspect"] == 1.6


def test_merge_replaces_case_insensitively_and_caps():
    entries = [bookmarks.sanitize(entry("Front"))]
    merged = bookmarks.merge(entries, entry("front", distance=12.0))
    assert len(merged) == 1
    assert merged[0]["distance"] == 12.0
    for index in range(bookmarks.MAX_BOOKMARKS + 5):
        merged = bookmarks.merge(merged, entry(f"View {index}"))
    assert len(merged) == bookmarks.MAX_BOOKMARKS


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "bookmarks.json"
    bookmarks.save(path, [entry("Front"), entry("Top")])
    loaded = bookmarks.load(path)
    assert [item["name"] for item in loaded] == ["Front", "Top"]
    assert bookmarks.load(tmp_path / "missing.json") == []


def test_load_ignores_malformed_entries(tmp_path):
    path = tmp_path / "bookmarks.json"
    path.write_text(json.dumps({"bookmarks": [entry("Good"), {"name": "Bad"}]}), encoding="utf-8")
    loaded = bookmarks.load(path)
    assert [item["name"] for item in loaded] == ["Good"]
    path.write_text("not json", encoding="utf-8")
    assert bookmarks.load(path) == []


def test_save_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bookmarks, "MAX_FILE", 32)
    with pytest.raises(ValueError):
        bookmarks.save(tmp_path / "bookmarks.json", [entry("Front")])
