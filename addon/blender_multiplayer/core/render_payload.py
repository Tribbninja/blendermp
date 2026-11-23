"""Render frame payloads.

A rendered frame is one small JSON header plus the encoded image bytes. It is
sent through the normal chunked transfer path, so the host receives it in order
and can save it without knowing the transport.

LAN senders pass the image through untouched. Relay senders may zlib the image
bytes; the header records whether they did, and the decode is always lossless.
"""

from __future__ import annotations

import json
import struct
import zlib

HEADER = struct.Struct("<IB")  # meta length, flags
FLAG_ZLIB = 1
MAX_META = 4096
MAX_FRAME_BYTES = 256 * 1024 * 1024


class RenderPayloadError(ValueError):
    pass


def encode(
    job: str,
    frame: int,
    format_hint: str,
    width: int,
    height: int,
    data: bytes,
    compress: bool = False,
    tile: int = 0,
    tiles_x: int = 1,
    tiles_y: int = 1,
    tile_x: int = 0,
    tile_y: int = 0,
    tile_w: int = 0,
    tile_h: int = 0,
) -> bytes:
    if not data:
        raise RenderPayloadError("render frame is empty")
    if len(data) > MAX_FRAME_BYTES:
        raise RenderPayloadError("render frame is too large")
    meta = json.dumps(
        {
            "job": str(job)[:32],
            "frame": int(frame),
            "format": str(format_hint)[:8].lower(),
            "width": max(0, int(width)),
            "height": max(0, int(height)),
            "tile": max(0, int(tile)),
            "tiles_x": max(1, int(tiles_x)),
            "tiles_y": max(1, int(tiles_y)),
            "tile_x": max(0, int(tile_x)),
            "tile_y": max(0, int(tile_y)),
            "tile_w": max(0, int(tile_w)),
            "tile_h": max(0, int(tile_h)),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(meta) > MAX_META:
        raise RenderPayloadError("render header is too large")
    flags = 0
    wire = bytes(data)
    if compress:
        packed = zlib.compress(wire, 6)
        if len(packed) < len(wire):
            wire = packed
            flags |= FLAG_ZLIB
    return HEADER.pack(len(meta), flags) + meta + wire


def decode(payload: bytes) -> dict:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < HEADER.size:
        raise RenderPayloadError("render frame is truncated")
    meta_len, flags = HEADER.unpack_from(payload, 0)
    if meta_len <= 0 or meta_len > MAX_META:
        raise RenderPayloadError("render header length is invalid")
    if flags & ~FLAG_ZLIB:
        raise RenderPayloadError("render frame has unknown flags")
    end = HEADER.size + meta_len
    if len(payload) < end:
        raise RenderPayloadError("render frame is truncated")
    try:
        meta = json.loads(payload[HEADER.size : end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderPayloadError("render header is not JSON") from exc
    if not isinstance(meta, dict):
        raise RenderPayloadError("render header must be an object")
    data = payload[end:]
    if flags & FLAG_ZLIB:
        try:
            data = zlib.decompress(data)
        except zlib.error as exc:
            raise RenderPayloadError("render frame could not be decompressed") from exc
    if not data or len(data) > MAX_FRAME_BYTES:
        raise RenderPayloadError("render image is empty or too large")
    frame = meta.get("frame")
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise RenderPayloadError("render frame number is invalid")
    return {
        "job": str(meta.get("job", ""))[:32],
        "frame": frame,
        "format": str(meta.get("format", "png"))[:8],
        "width": int(meta.get("width", 0) or 0),
        "height": int(meta.get("height", 0) or 0),
        "tile": int(meta.get("tile", 0) or 0),
        "tiles_x": max(1, int(meta.get("tiles_x", 1) or 1)),
        "tiles_y": max(1, int(meta.get("tiles_y", 1) or 1)),
        "tile_x": int(meta.get("tile_x", 0) or 0),
        "tile_y": int(meta.get("tile_y", 0) or 0),
        "tile_w": int(meta.get("tile_w", 0) or 0),
        "tile_h": int(meta.get("tile_h", 0) or 0),
        "data": data,
    }
