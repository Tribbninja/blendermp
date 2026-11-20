import numpy as np
import pytest

from core import chunker, compression


def make_positions(count: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(size=(count, 3)).astype(np.float32)


def test_chunk_hashes_change_only_for_dirty_chunks():
    positions = make_positions(20_000)
    hashes = chunker.chunk_hashes(positions, chunk_verts=4096)
    changed = positions.copy()
    changed[10] += 1.0
    new_hashes = chunker.chunk_hashes(changed, chunk_verts=4096)
    assert int((hashes != new_hashes).sum()) == 1


def test_diff_finds_only_changed_vertices():
    positions = make_positions(10_000)
    changed = positions.copy()
    indexes = [3, 4000, 9999]
    changed[indexes] += 0.5
    scan = chunker.diff_positions(positions, changed, chunk_verts=4096, epsilon=0.0)
    assert sorted(scan.indices.tolist()) == indexes
    assert np.allclose(scan.values, changed[indexes])


def test_diff_epsilon_ignores_subpixel_motion():
    positions = make_positions(1000)
    changed = positions.copy()
    changed[:] += 1e-9
    scan = chunker.diff_positions(positions, changed, epsilon=1e-6)
    assert scan.count == 0


def test_diff_rejects_size_mismatch():
    with pytest.raises(ValueError):
        chunker.diff_positions(make_positions(10), make_positions(11))


def test_apply_positions():
    shadow = make_positions(100)
    values = shadow[[5, 6]].copy()
    values += 2.0
    chunker.apply_positions(shadow, np.array([5, 6]), values)
    assert np.allclose(shadow[[5, 6]], values)
    with pytest.raises(ValueError):
        chunker.apply_positions(shadow, np.array([999]), np.zeros((1, 3)))


def test_delta_roundtrip_float32_is_exact():
    positions = make_positions(500)
    changed = positions.copy()
    changed[[1, 3, 250]] += 0.25
    scan = chunker.diff_positions(positions, changed, epsilon=0.0)
    payload = chunker.encode_delta(7, scan.indices, scan.values, mode=chunker.MODE_F32)
    rev, indices, values = chunker.decode_delta(payload)
    assert rev == 7
    assert indices.tolist() == scan.indices.tolist()
    assert np.array_equal(values, scan.values)


def test_delta_roundtrip_int16_is_close():
    positions = make_positions(500)
    changed = positions.copy()
    changed[[2, 4]] += 0.01
    scan = chunker.diff_positions(positions, changed, epsilon=0.0)
    payload = chunker.encode_delta(3, scan.indices, scan.values, mode=chunker.MODE_I16)
    rev, indices, values = chunker.decode_delta(payload)
    assert rev == 3
    assert indices.tolist() == [2, 4]
    span = np.maximum(np.ptp(scan.values, axis=0), 1e-9)
    assert np.all(np.abs(values - scan.values) <= span / 65535.0 + 1e-6)


def test_validate_delta_rejects_bad_payloads():
    previous = compression.enabled()
    compression.set_enabled(False)
    try:
        positions = make_positions(50)
        scan = chunker.diff_positions(positions, positions + 1.0, epsilon=0.0)
        payload = chunker.encode_delta(1, scan.indices, scan.values)
        assert chunker.validate_delta(payload) == (1, scan.count, chunker.MODE_F32)

        with pytest.raises(ValueError):
            chunker.validate_delta(payload[:-1])
        with pytest.raises(ValueError):
            chunker.validate_delta(b"")
        with pytest.raises(ValueError):
            chunker.validate_delta(payload + b"junk")
    finally:
        compression.set_enabled(previous)


def test_delta_is_lossless_in_both_compression_modes():
    positions = make_positions(200)
    changed = positions.copy()
    changed[list(range(20))] += 0.123456789
    scan = chunker.diff_positions(positions, changed, epsilon=0.0)
    previous = compression.enabled()
    try:
        compression.set_enabled(False)
        raw = chunker.encode_delta(4, scan.indices, scan.values)
        assert chunker.validate_delta(raw)[2] == chunker.MODE_F32
        _rev, _indices, raw_values = chunker.decode_delta(raw)
        assert np.array_equal(raw_values, scan.values)

        compression.set_enabled(True)
        packed = chunker.encode_delta(5, scan.indices, scan.values)
        assert chunker.validate_delta(packed)[2] == chunker.MODE_F32_ZLIB
        _rev, _indices, packed_values = chunker.decode_delta(packed)
        assert np.array_equal(packed_values, scan.values)
    finally:
        compression.set_enabled(previous)


def test_pack_geometry_respects_the_compression_policy():
    payload = chunker.GEO_MAGIC + b"x" * 8192
    previous = compression.enabled()
    try:
        compression.set_enabled(False)
        assert chunker.pack_geometry(payload) == payload
        compression.set_enabled(True)
        packed = chunker.pack_geometry(payload)
        assert packed[:4] == chunker.GEOZ_MAGIC
        assert chunker.unpack_geometry(packed) == payload
    finally:
        compression.set_enabled(previous)


def test_positions_snapshot_roundtrip_and_validation():
    positions = make_positions(321)
    payload = chunker.encode_positions(positions)
    assert np.array_equal(chunker.decode_positions(payload), positions)
    chunker.validate_positions(payload)
    with pytest.raises(ValueError):
        chunker.validate_positions(payload[:-4])
    with pytest.raises(ValueError):
        chunker.validate_positions(b"\xff\xff\xff\x7f")
