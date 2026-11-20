import numpy as np
import pytest

from core import chunker


def sample():
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
    edges = np.array([[0, 1], [1, 2], [2, 0], [0, 3]], dtype=np.uint32)
    loops = np.array([0, 1, 2, 0, 1, 3], dtype=np.uint32)
    starts = np.array([0, 3], dtype=np.uint32)
    totals = np.array([3, 3], dtype=np.uint32)
    smooth = np.array([0, 1], dtype=np.uint8)
    materials = np.array([0, 1], dtype=np.uint32)
    uvs = np.array([[0, 0], [1, 0], [0, 1], [0, 0], [1, 0], [0, 1]], dtype=np.float32)
    return positions, edges, loops, starts, totals, smooth, materials, uvs


def test_geometry_roundtrip():
    positions, edges, loops, starts, totals, smooth, materials, uvs = sample()
    payload = chunker.encode_geometry(
        positions, edges, loops, starts, totals, smooth, materials, "UVMap", uvs
    )
    geometry = chunker.decode_geometry(payload)
    assert np.array_equal(geometry["positions"], positions)
    assert np.array_equal(geometry["edges"], edges)
    assert np.array_equal(geometry["loops"], loops)
    assert np.array_equal(geometry["loop_starts"], starts)
    assert np.array_equal(geometry["loop_totals"], totals)
    assert np.array_equal(geometry["smooth"], smooth)
    assert np.array_equal(geometry["materials"], materials)
    assert np.array_equal(geometry["uvs"], uvs)
    assert geometry["uv_name"] == "UVMap"


def test_geometry_without_uvs():
    positions, edges, loops, starts, totals, smooth, materials, _ = sample()
    payload = chunker.encode_geometry(positions, edges, loops, starts, totals, smooth, materials)
    geometry = chunker.decode_geometry(payload)
    assert geometry["uvs"].size == 0
    assert geometry["uv_name"] == ""


def test_geometry_validation():
    positions, edges, loops, starts, totals, smooth, materials, _ = sample()
    payload = chunker.encode_geometry(positions, edges, loops, starts, totals, smooth, materials)
    assert chunker.validate_geometry(payload) == (4, 4, 6, 2)

    bad_loops = loops.copy()
    bad_loops[0] = 99
    with pytest.raises(ValueError):
        chunker.encode_geometry(positions, edges, bad_loops, starts, totals, smooth, materials)

    with pytest.raises(ValueError):
        chunker.validate_geometry(payload + b"x")
    with pytest.raises(ValueError):
        chunker.validate_geometry(b"")
    with pytest.raises(ValueError):
        chunker.validate_geometry(payload, max_vertices=1)


def test_geometry_rejects_inconsistent_shapes():
    positions, edges, loops, starts, totals, smooth, materials, _ = sample()
    with pytest.raises(ValueError):
        chunker.encode_geometry(positions, edges, loops, starts, totals, smooth, materials[:-1])
    bad_edges = edges.copy()
    bad_edges[0, 0] = 99
    with pytest.raises(ValueError):
        chunker.encode_geometry(positions, bad_edges, loops, starts, totals, smooth, materials)
    with pytest.raises(ValueError):
        chunker.encode_geometry(positions, edges, loops, starts, totals + np.array([1, 0], dtype=np.uint32), smooth, materials)
    with pytest.raises(ValueError):
        chunker.encode_geometry(
            positions, edges, loops, starts, totals, smooth, materials, "UVMap", np.zeros((2, 2), np.float32)
        )


def test_multi_uv_geometry_roundtrip():
    positions, edges, loops, starts, totals, smooth, materials, uvs = sample()
    second = uvs * 0.5
    payload = chunker.encode_geometry_layers(
        positions,
        edges,
        loops,
        starts,
        totals,
        smooth,
        materials,
        [("UVMap", uvs), ("Second", second)],
        active_uv=1,
    )
    geometry = chunker.decode_geometry(payload)
    assert geometry["uv_active"] == 1
    assert [name for name, _values in geometry["uv_layers"]] == ["UVMap", "Second"]
    assert np.array_equal(geometry["uv_layers"][0][1], uvs)
    assert np.array_equal(geometry["uv_layers"][1][1], second)
    assert np.array_equal(geometry["uvs"], second)
    assert geometry["uv_name"] == "Second"
    packed = chunker.pack_geometry(payload)
    assert packed[:4] in (chunker.GEO2_MAGIC, chunker.GEOZ2_MAGIC)
    assert chunker.validate_geometry(packed) == (4, 4, 6, 2)
