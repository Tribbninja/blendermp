"""Chunked mesh differencing.

Blender exposes no per-vertex change events, so we read the position buffer and
hash it in fixed-size chunks. Only chunks whose hash changed are compared
vertex by vertex, which keeps a 1M-vertex sculpt tick in the low single-digit
milliseconds and makes the wire payload proportional to the brush, not the mesh.

Nothing here imports ``bpy``; it is testable with plain numpy and is reused by
both the addon and the headless tools.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .varint import decode_uvarint, encode_uvarint

CHUNK_VERTS = 8192
MODE_F32 = 0
MODE_I16 = 1
_QUANT_MAX = 65535


def as_positions(values) -> np.ndarray:
    """Return a contiguous (n, 3) float32 array."""
    array = np.asarray(values, dtype=np.float32)
    if array.ndim == 1:
        if array.size % 3:
            raise ValueError("flat position buffer is not a multiple of 3")
        array = array.reshape(-1, 3)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError(f"expected (n, 3) positions, got {array.shape}")
    return np.ascontiguousarray(array, dtype=np.float32)


def _chunk_count(n: int, chunk_verts: int) -> int:
    return 0 if n == 0 else (n + chunk_verts - 1) // chunk_verts


def chunk_hashes(positions, chunk_verts: int = CHUNK_VERTS) -> np.ndarray:
    """One uint64 hash per chunk, computed with a vectorised XOR reduction."""
    positions = as_positions(positions)
    n = positions.shape[0]
    chunks = _chunk_count(n, chunk_verts)
    if chunks == 0:
        return np.zeros(0, dtype=np.uint64)

    padded = np.zeros(chunks * chunk_verts * 3, dtype=np.float32)
    padded[: n * 3] = positions.ravel()
    words = padded.view(np.uint64).reshape(chunks, -1)
    hashes = np.bitwise_xor.reduce(words, axis=1)
    # Mix the vertex count into the first hash so growth/shrink cannot collide
    # with a chunk whose new tail happens to be zeroes.
    hashes[0] ^= np.uint64((n * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF)
    return hashes


@dataclass(slots=True)
class DeltaScan:
    indices: np.ndarray
    values: np.ndarray
    changed_chunks: np.ndarray
    hashes: np.ndarray

    @property
    def count(self) -> int:
        return int(self.indices.size)

    @property
    def changed_chunk_count(self) -> int:
        return int(self.changed_chunks.size)

    @property
    def changed_fraction(self) -> float:
        return float(self.indices.size)


def diff_positions(
    previous,
    current,
    chunk_verts: int = CHUNK_VERTS,
    epsilon: float = 1e-6,
    previous_hashes: np.ndarray | None = None,
) -> DeltaScan:
    """Compare two position buffers and return changed vertices.

    ``epsilon`` is a per-component threshold. Pass ``0`` for an exact diff.
    ``previous_hashes`` lets callers cache the previous tick's hashes.
    """
    previous = as_positions(previous)
    current = as_positions(current)
    if previous.shape != current.shape:
        raise ValueError("position buffers differ in size; topology changed")
    if previous.shape[0] == 0:
        empty = np.zeros(0, dtype=np.int32)
        return DeltaScan(empty, np.zeros((0, 3), np.float32), np.zeros(0, np.int64), np.zeros(0, np.uint64))

    hashes_before = chunk_hashes(previous, chunk_verts) if previous_hashes is None else previous_hashes
    hashes_after = chunk_hashes(current, chunk_verts)
    changed = np.flatnonzero(hashes_before != hashes_after)

    index_parts: list[np.ndarray] = []
    value_parts: list[np.ndarray] = []
    n = current.shape[0]
    for chunk in changed.tolist():
        start = chunk * chunk_verts
        end = min(n, start + chunk_verts)
        before = previous[start:end]
        after = current[start:end]
        if epsilon <= 0:
            mask = np.any(before != after, axis=1)
        else:
            mask = np.any(np.abs(before - after) > epsilon, axis=1)
        local = np.flatnonzero(mask)
        if local.size:
            index_parts.append((local + start).astype(np.int32))
            value_parts.append(after[local])

    if index_parts:
        indices = np.concatenate(index_parts)
        values = np.concatenate(value_parts)
    else:
        indices = np.zeros(0, dtype=np.int32)
        values = np.zeros((0, 3), dtype=np.float32)
    return DeltaScan(indices, values, changed.astype(np.int64), hashes_after)


def apply_positions(shadow, indices, values) -> None:
    """Write changed vertices into a writable (n, 3) float32 buffer."""
    target = np.asarray(shadow)
    if target.ndim != 2 or target.shape[1] != 3:
        raise ValueError("shadow buffer must be (n, 3)")
    idx = np.asarray(indices, dtype=np.int64)
    vals = as_positions(values)
    if idx.size != vals.shape[0]:
        raise ValueError("index and value counts differ")
    if idx.size and (idx.min() < 0 or idx.max() >= target.shape[0]):
        raise ValueError("position index out of range")
    target[idx] = vals


def encode_delta(rev: int, indices, values, mode: int = MODE_I16) -> bytes:
    """Serialise changed vertices: varint delta indices + float32 or int16 values."""
    idx = np.asarray(indices, dtype=np.int64).ravel()
    vals = as_positions(values)
    if idx.size != vals.shape[0]:
        raise ValueError("index and value counts differ")
    if idx.size > 1 and np.any(np.diff(idx) < 0):
        raise ValueError("indices must be ascending")

    header = bytearray(struct.pack("<IIB", rev, idx.size, mode))
    if vals.shape[0]:
        base = vals.min(axis=0)
        span = (vals.max(axis=0) - base).astype(np.float32)
    else:
        base = np.zeros(3, np.float32)
        span = np.zeros(3, np.float32)
    header += np.asarray(base, dtype="<f4").tobytes()
    header += np.asarray(span, dtype="<f4").tobytes()

    previous = 0
    for index in idx.tolist():
        header += encode_uvarint(index - previous)
        previous = index

    if mode == MODE_F32:
        header += np.asarray(vals, dtype="<f4").tobytes()
    elif mode == MODE_I16:
        safe = np.where(span > 0, span, 1.0).astype(np.float32)
        scaled = np.clip((vals - base) / safe, 0.0, 1.0) * _QUANT_MAX
        quantised = np.rint(scaled).astype("<u2")
        header += quantised.tobytes()
    else:
        raise ValueError(f"unknown delta mode {mode}")
    return bytes(header)


def decode_delta(payload: bytes) -> tuple[int, np.ndarray, np.ndarray]:
    """Return ``(rev, indices, values)`` from an encoded delta."""
    if len(payload) < 9:
        raise ValueError("delta payload is truncated")
    rev, count, mode = struct.unpack_from("<IIB", payload, 0)
    offset = 9
    if len(payload) < offset + 24:
        raise ValueError("delta payload is missing its base/span")
    base = np.frombuffer(payload, dtype="<f4", count=3, offset=offset).astype(np.float32)
    offset += 12
    span = np.frombuffer(payload, dtype="<f4", count=3, offset=offset).astype(np.float32)
    offset += 12

    indices = np.empty(count, dtype=np.int64)
    previous = 0
    for i in range(count):
        delta, offset = decode_uvarint(payload, offset)
        previous += delta
        indices[i] = previous

    if mode == MODE_F32:
        need = count * 12
        if len(payload) < offset + need:
            raise ValueError("delta payload is truncated (f32 values)")
        values = np.frombuffer(payload, dtype="<f4", count=count * 3, offset=offset).astype(np.float32).reshape(-1, 3)
    elif mode == MODE_I16:
        need = count * 6
        if len(payload) < offset + need:
            raise ValueError("delta payload is truncated (i16 values)")
        quantised = np.frombuffer(payload, dtype="<u2", count=count * 3, offset=offset).astype(np.float32).reshape(-1, 3)
        values = base + (quantised / _QUANT_MAX) * span
    else:
        raise ValueError(f"unknown delta mode {mode}")
    return rev, indices.astype(np.int32), values


def encode_positions(positions) -> bytes:
    positions = as_positions(positions)
    return struct.pack("<I", positions.shape[0]) + positions.astype("<f4").tobytes()


def decode_positions(payload: bytes) -> np.ndarray:
    if len(payload) < 4:
        raise ValueError("snapshot payload is truncated")
    (count,) = struct.unpack_from("<I", payload, 0)
    expected = 4 + count * 12
    if len(payload) < expected:
        raise ValueError("snapshot payload is truncated")
    values = np.frombuffer(payload, dtype="<f4", count=count * 3, offset=4)
    return np.array(values, dtype=np.float32).reshape(-1, 3)


def validate_delta(payload: bytes, max_vertices: int = 5_000_000) -> tuple[int, int, int]:
    """Structural validation without materialising the values.

    Used by the host on untrusted input so a malformed or abusive delta is
    rejected before it reaches Blender.
    """
    if len(payload) < 9:
        raise ValueError("delta payload is truncated")
    rev, count, mode = struct.unpack_from("<IIB", payload, 0)
    if count > max_vertices:
        raise ValueError("delta exceeds the vertex limit")
    if mode not in (MODE_F32, MODE_I16):
        raise ValueError("unknown delta mode")
    offset = 9 + 24
    if len(payload) < offset:
        raise ValueError("delta payload is missing its base/span")
    previous = 0
    for _ in range(count):
        delta, offset = decode_uvarint(payload, offset)
        if delta > max_vertices:
            raise ValueError("delta index jump is out of range")
        previous += delta
        if previous > max_vertices:
            raise ValueError("delta index exceeds the vertex limit")
    need = count * (12 if mode == MODE_F32 else 6)
    if len(payload) != offset + need:
        raise ValueError("delta payload length does not match its header")
    return rev, count, mode


def validate_positions(payload: bytes, max_vertices: int = 5_000_000) -> tuple[int]:
    if len(payload) < 4:
        raise ValueError("snapshot payload is truncated")
    (count,) = struct.unpack_from("<I", payload, 0)
    if count > max_vertices:
        raise ValueError("snapshot exceeds the vertex limit")
    if len(payload) != 4 + count * 12:
        raise ValueError("snapshot payload length does not match its header")
    return (count,)
