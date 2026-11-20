import numpy as np
import pytest

from core import asset_bundle, chunker, object_payload


def geometry():
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    edges = np.array([[0, 1], [1, 2], [2, 0]], dtype=np.uint32)
    loops = np.array([0, 1, 2], dtype=np.uint32)
    starts = np.array([0], dtype=np.uint32)
    totals = np.array([3], dtype=np.uint32)
    smooth = np.array([1], dtype=np.uint8)
    materials = np.array([0], dtype=np.uint32)
    return chunker.pack_geometry(
        chunker.encode_geometry(positions, edges, loops, starts, totals, smooth, materials)
    )


def manifest(target="bob"):
    uid = "12345678-1234-5678-1234-567812345678"
    image = b"\x89PNG\r\n\x1a\n" + b"x" * 128
    return {
        "label": "Selection",
        "target": target,
        "objects": [
            {
                "uid": uid,
                "kind": object_payload.KIND_MESH,
                "meta": {
                    "name": "Cube",
                    "matrix": [1.0] * 16,
                    "parent": None,
                    "materials": [
                        {
                            "name": "Red",
                            "base_color": [0.8, 0.1, 0.1, 1.0],
                            "metallic": 0.0,
                            "roughness": 0.4,
                            "emission_color": [0.0, 0.0, 0.0],
                            "emission_strength": 0.0,
                            "slot": 0,
                        }
                    ],
                },
                "geometry": geometry(),
            }
        ],
        "images": [
            {
                "name": "Texture",
                "format": "png",
                "data": image,
            }
        ],
    }


def test_bundle_roundtrip():
    wire = asset_bundle.pack(manifest())
    body = asset_bundle.decode(wire)
    assert body["id"]
    assert body["target"] == "bob"
    assert body["label"] == "Selection"
    assert len(body["objects"]) == 1
    assert body["objects"][0]["meta"]["name"] == "Cube"
    assert body["images"][0]["data"].startswith(b"\x89PNG")
    summary = asset_bundle.summary(body)
    assert summary["objects"] == 1
    assert summary["meshes"] == 1
    assert summary["images"] == 1
    assert summary["vertices"] == 3


def test_bundle_raw_roundtrip_without_compression():
    wire = asset_bundle.pack(manifest(), compress=False)
    assert wire[:4] == asset_bundle.MAGIC_RAW
    body = asset_bundle.decode(wire)
    assert body["objects"][0]["meta"]["name"] == "Cube"
    assert body["images"][0]["data"].startswith(b"\x89PNG")
    assert body["id"] == asset_bundle.decode(asset_bundle.pack(manifest()))["id"]


def test_bundle_id_is_content_addressed_and_target_independent():
    first = asset_bundle.decode(asset_bundle.pack(manifest("bob")))
    second = asset_bundle.decode(asset_bundle.pack(manifest("carol")))
    assert first["id"] == second["id"]
    assert first["target"] != second["target"]


def test_bundle_rejects_bad_magic_and_truncation():
    wire = asset_bundle.pack(manifest())
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.decode(b"BMB1")
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.decode(wire[:-4])
    bad = bytearray(wire)
    bad[0] = ord("X")
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.decode(bytes(bad))


def test_bundle_rejects_bad_geometry_and_digest():
    body = manifest()
    body["objects"][0]["geometry"] = b"not geometry"
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.pack(body)
    body = manifest()
    body["images"][0]["sha1"] = "0" * 40
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.pack(body)


def test_bundle_rejects_too_many_objects():
    body = manifest()
    body["objects"] = body["objects"] * (asset_bundle.MAX_OBJECTS + 1)
    with pytest.raises(asset_bundle.AssetBundleError):
        asset_bundle.pack(body)
