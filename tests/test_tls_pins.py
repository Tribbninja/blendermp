import json

from core import tls


def test_remember_and_forget_round_trip(tmp_path):
    code = "041C1-A24CM-GWPHR"
    value = "ab" * 32
    assert tls.known_fingerprint(tmp_path, code) is None
    tls.remember_fingerprint(tmp_path, code, value)
    assert tls.known_fingerprint(tmp_path, code) == value
    # Codes are normalised, so the dashed and undashed forms share a pin.
    assert tls.known_fingerprint(tmp_path, code.replace("-", "")) == value
    tls.forget_fingerprint(tmp_path, code)
    assert tls.known_fingerprint(tmp_path, code) is None


def test_known_fingerprint_rejects_invalid_entries(tmp_path):
    path = tmp_path / "known-hosts.json"
    path.write_text(json.dumps({"ROOM": "too-short"}), encoding="utf-8")
    assert tls.known_fingerprint(tmp_path, "ROOM") is None


def test_known_fingerprint_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "known-hosts.json"
    path.write_text("{not json", encoding="utf-8")
    assert tls.known_fingerprint(tmp_path, "ROOM") is None


def test_verify_fingerprint_is_case_and_colon_insensitive():
    value = "cd" * 32
    assert tls.verify_fingerprint(value.upper(), value)
    assert tls.verify_fingerprint(value, ":".join(value[i : i + 2] for i in range(0, 64, 2)).upper())
    assert not tls.verify_fingerprint(value, "ef" * 32)


def test_short_fingerprint_is_grouped():
    assert tls.short_fingerprint("40904e7c2e53bb5b") == "4090 4E7C 2E53 BB5B"
