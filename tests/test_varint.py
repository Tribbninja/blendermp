import pytest

from core.varint import decode_svarint, decode_uvarint, decode_uvarints, encode_svarint, encode_uvarint, encode_uvarints


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, 16383, 16384, 2**32, 2**63, 2**64 - 1])
def test_uvarint_roundtrip(value):
    encoded = encode_uvarint(value)
    decoded, offset = decode_uvarint(encoded)
    assert decoded == value
    assert offset == len(encoded)


@pytest.mark.parametrize("value", [-2**62, -1000, -1, 0, 1, 1000, 2**62])
def test_svarint_roundtrip(value):
    decoded, offset = decode_svarint(encode_svarint(value))
    assert decoded == value
    assert offset == len(encode_svarint(value))


def test_varint_rejects_negative_and_oversized():
    with pytest.raises(ValueError):
        encode_uvarint(-1)
    with pytest.raises(ValueError):
        encode_uvarint(2**64)


def test_truncated_input():
    with pytest.raises(ValueError):
        decode_uvarint(b"\x80")
    with pytest.raises(ValueError):
        decode_uvarint(b"")


def test_bulk_roundtrip():
    values = [0, 5, 300, 70000, 2**40]
    decoded, offset = decode_uvarints(encode_uvarints(values), len(values))
    assert decoded == values
    assert offset == len(encode_uvarints(values))
