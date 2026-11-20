import hashlib

import pytest

from core import image_payload


def test_image_roundtrip():
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 1000
    payload = image_payload.encode("Texture", "png", data)
    body = image_payload.decode(payload)
    assert body["name"] == "Texture"
    assert body["format"] == "png"
    assert body["data"] == data


def test_image_rejects_bad_input():
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.encode("x", "exe", b"data")
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.encode("x", "png", b"")
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.encode("x", "png", b"x" * (image_payload.MAX_IMAGE + 1))


def test_image_decode_truncated():
    payload = image_payload.encode("x", "png", b"data")
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.decode(payload[:-1])
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.decode(payload + b"extra")
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.decode(b"")


def test_image_name_is_bounded():
    body = image_payload.decode(image_payload.encode("n" * 500, "jpg", b"abc"))
    assert len(body["name"]) <= image_payload.MAX_NAME


def test_image_digest_roundtrip_and_integrity():
    data = b"\x89PNG\r\n\x1a\n" + b"y" * 64
    body = image_payload.decode(image_payload.encode("Texture", "png", data))
    assert body["sha1"] == hashlib.sha1(data).hexdigest()
    payload = bytearray(image_payload.encode("Texture", "png", data))
    payload[-1] ^= 0xFF
    with pytest.raises(image_payload.ImagePayloadError):
        image_payload.decode(bytes(payload))


def test_large_image_is_compressed_before_transfer():
    data = b"B" * (image_payload.COMPRESS_THRESHOLD + 1024 * 1024)
    payload = image_payload.encode("Large", "png", data)
    assert len(payload) < len(data)
    body = image_payload.decode(payload)
    assert body["data"] == data
    assert body["compressed"] is True


def test_small_image_stays_uncompressed():
    data = b"C" * 1024
    body = image_payload.decode(image_payload.encode("Small", "png", data))
    assert body["data"] == data
    assert body["compressed"] is False


