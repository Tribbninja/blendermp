import pytest

from core import image_payload


def test_image_roundtrip():
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
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
