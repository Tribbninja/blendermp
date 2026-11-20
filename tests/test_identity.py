from core import identity


def test_identity_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    first = identity.load_or_create()
    assert first is not None
    assert identity.identity_path().is_file()
    descriptor = identity.descriptor("CODE", "0.3.0", "0.3.0", 2, "a" * 64, 7)
    signature = identity.sign(first, descriptor)
    assert signature
    assert identity.verify(first["public"], descriptor, signature)
    tampered = dict(descriptor)
    tampered["code"] = "OTHER"
    assert not identity.verify(first["public"], tampered, signature)
    again = identity.load_or_create()
    assert again["public"] == first["public"]
