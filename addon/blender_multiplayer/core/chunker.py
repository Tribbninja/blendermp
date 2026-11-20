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
import zlib
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

# --------------------------------------------------------------------------- geometry
GEO_MAGIC = b"GEO1"
GEOZ_MAGIC = b"GZ01"
GEO_HEADER = struct.Struct("<4s6I")
MAX_GEO_BYTES = 128 * 1024 * 1024


def is_geometry(payload: bytes) -> bool:
    return payload[:4] in (GEO_MAGIC, GEOZ_MAGIC)


def pack_geometry(payload: bytes) -> bytes:
    """Compress a full geometry snapshot when it is worth the CPU."""
    if payload[:4] != GEO_MAGIC or len(payload) < 4096:
        return payload
    # Large uploads get the slower/higher ratio pass; small edits stay cheap.
    level = 9 if len(payload) > 10 * 1024 * 1024 else 6
    return GEOZ_MAGIC + zlib.compress(payload, level)


def unpack_geometry(payload: bytes) -> bytes:
    if payload[:4] != GEOZ_MAGIC:
        return payload
    try:
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(payload[4:], MAX_GEO_BYTES + 1)
    except zlib.error as exc:
        raise ValueError("invalid compressed geometry") from exc
    if len(data) > MAX_GEO_BYTES or decompressor.unconsumed_tail:
        raise ValueError("compressed geometry is too large")
    return data


def encode_geometry(
    positions,
    edges,
    loops,
    loop_starts,
    loop_totals,
    smooth,
    materials,
    uv_name: str | None = None,
    uvs=None,
) -> bytes:
    """Full mesh snapshot: positions plus connectivity, enough to rebuild."""
    positions = as_positions(positions)
    edges = np.asarray(edges, dtype=np.uint32).ravel()
    loops = np.asarray(loops, dtype=np.uint32).ravel()
    starts = np.asarray(loop_starts, dtype=np.uint32).ravel()
    totals = np.asarray(loop_totals, dtype=np.uint32).ravel()
    smooth = np.asarray(smooth, dtype=np.uint8).ravel()
    materials = np.asarray(materials, dtype=np.uint32).ravel()

    nv, ne, nl, npoly = positions.shape[0], edges.size // 2, loops.size, starts.size
    if totals.size != npoly or smooth.size != npoly or materials.size != npoly:
        raise ValueError("polygon arrays must have the same length")
    if edges.size % 2:
        raise ValueError("edge array must contain pairs")
    if edges.size and int(edges.max()) >= nv:
        raise ValueError("edge references a vertex outside the mesh")
    if loops.size and int(loops.max()) >= nv:
        raise ValueError("loop references a vertex outside the mesh")
    if int(totals.sum()) != nl:
        raise ValueError("polygon loop totals do not match the loop count")

    name = (uv_name or "").encode("utf-8")[:64]
    has_uv = 1 if uvs is not None else 0
    if has_uv:
        uvs = np.asarray(uvs, dtype=np.float32)
        if uvs.size != nl * 2:
            raise ValueError("UV array must have two values per loop")
        uvs = uvs.ravel()
    else:
        uvs = np.zeros(0, dtype=np.float32)

    header = GEO_HEADER.pack(GEO_MAGIC, nv, ne, nl, npoly, npoly, has_uv)
    payload = bytearray(header)
    payload += struct.pack("<H", len(name)) + name
    payload += np.asarray(positions, dtype="<f4").tobytes()
    payload += edges.astype("<u4").tobytes()
    payload += loops.astype("<u4").tobytes()
    payload += starts.astype("<u4").tobytes()
    payload += totals.astype("<u4").tobytes()
    payload += smooth.astype("u1").tobytes()
    payload += materials.astype("<u4").tobytes()
    if has_uv:
        payload += uvs.astype("<f4").tobytes()
    return bytes(payload)


def decode_geometry(payload: bytes) -> dict:
    payload = unpack_geometry(payload)
    if len(payload) < GEO_HEADER.size:
        raise ValueError("geometry payload is truncated")
    magic, nv, ne, nl, npoly, npoly2, has_uv = GEO_HEADER.unpack_from(payload, 0)
    if magic != GEO_MAGIC or npoly != npoly2:
        raise ValueError("invalid geometry header")
    offset = GEO_HEADER.size
    if len(payload) < offset + 2:
        raise ValueError("geometry payload is missing the UV name")
    (name_len,) = struct.unpack_from("<H", payload, offset)
    offset += 2
    if name_len > 64 or len(payload) < offset + name_len:
        raise ValueError("invalid UV layer name")
    uv_name = payload[offset : offset + name_len].decode("utf-8", "replace")
    offset += name_len

    def take(count, dtype):
        nonlocal offset
        size = np.dtype(dtype).itemsize * count
        if len(payload) < offset + size:
            raise ValueError("geometry payload is truncated")
        value = np.frombuffer(payload, dtype=dtype, count=count, offset=offset).copy()
        offset += size
        return value

    positions = take(nv * 3, "<f4").reshape(-1, 3)
    edges = take(ne * 2, "<u4").reshape(-1, 2)
    loops = take(nl, "<u4")
    starts = take(npoly, "<u4")
    totals = take(npoly, "<u4")
    smooth = take(npoly, "u1")
    materials = take(npoly, "<u4")
    uvs = take(nl * 2, "<f4") if has_uv else np.zeros(0, dtype=np.float32)
    if offset != len(payload):
        raise ValueError("geometry payload has trailing bytes")
    return {
        "positions": positions,
        "edges": edges,
        "loops": loops,
        "loop_starts": starts,
        "loop_totals": totals,
        "smooth": smooth,
        "materials": materials,
        "uv_name": uv_name,
        "uvs": uvs.reshape(-1, 2),
    }


def validate_geometry(payload: bytes, max_vertices: int = 5_000_000) -> tuple[int, int, int, int]:
    geometry = decode_geometry(payload)
    nv = geometry["positions"].shape[0]
    if nv > max_vertices:
        raise ValueError("geometry exceeds the vertex limit")
    loops = geometry["loops"]
    if loops.size and int(loops.max()) >= nv:
        raise ValueError("loop references a vertex outside the mesh")
    edges = geometry["edges"]
    if edges.size and int(edges.max()) >= nv:
        raise ValueError("edge references a vertex outside the mesh")
    return nv, edges.shape[0], loops.size, geometry["loop_starts"].size
