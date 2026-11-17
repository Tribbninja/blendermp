import numpy as np
import pytest

from core import chunker, object_payload


def geometry():
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    edges = np.array([[0, 1], [1, 2], [2, 0]], dtype=np.uint32)
    loops = np.array([0, 1, 2], dtype=np.uint32)
    starts = np.array([0], dtype=np.uint32)
    totals = np.array([3], dtype=np.uint32)
    smooth = np.array([1], dtype=np.uint8)
    materials = np.array([0], dtype=np.uint32)
    return chunker.encode_geometry(positions, edges, loops, starts, totals, smooth, materials)


def test_create_roundtrip():
    uid = "12345678-1234-5678-1234-567812345678"
    meta = {"name": "Cube", "matrix": [1.0] * 16, "parent": None}
    payload = object_payload.encode_create(uid, object_payload.KIND_MESH, meta, geometry())
    body = object_payload.decode_create(payload)
    assert body["uuid"] == uid
    assert body["kind"] == object_payload.KIND_MESH
    assert body["meta"] == meta
    assert chunker.validate_geometry(body["geometry"]) == (3, 3, 3, 1)


def test_empty_and_light_create():
    payload = object_payload.encode_create(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        object_payload.KIND_LIGHT,
        {"name": "Sun", "matrix": [0.0] * 16, "light_type": "SUN", "energy": 3.0},
    )
    body = object_payload.decode_create(payload)
    assert body["kind"] == object_payload.KIND_LIGHT
    assert body["geometry"] == b""
    assert body["meta"]["light_type"] == "SUN"


def test_create_rejects_bad_input():
    uid = "12345678-1234-5678-1234-567812345678"
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.encode_create(uid, 99, {"name": "x"})
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.encode_create("not-a-uuid", object_payload.KIND_EMPTY, {})
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.encode_create(uid, object_payload.KIND_EMPTY, {"pad": "x" * (object_payload.MAX_META + 1)})
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.encode_create(uid, object_payload.KIND_MESH, {}, b"x" * (object_payload.MAX_GEOMETRY + 1))


def test_decode_truncated_and_trailing():
    uid = "12345678-1234-5678-1234-567812345678"
    payload = object_payload.encode_create(uid, object_payload.KIND_EMPTY, {"name": "e"})
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.decode_create(payload[:-1])
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.decode_create(b"")
    bad = bytearray(payload)
    bad[16] = 200
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.decode_create(bytes(bad))


def test_delete_roundtrip():
    uid = "12345678-1234-5678-1234-567812345678"
    payload = object_payload.encode_delete(uid)
    assert len(payload) == 16
    assert object_payload.decode_delete(payload) == uid
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.decode_delete(b"short")
    with pytest.raises(object_payload.ObjectPayloadError):
        object_payload.encode_delete("nope")
