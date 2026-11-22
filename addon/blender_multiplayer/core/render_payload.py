"""Render frame payloads.

A rendered frame is one small JSON header plus the encoded image bytes. It is
sent through the normal chunked transfer path, so the host receives it in order
and can save it without knowing the transport.
"""

from __future__ import annotations

import json
import struct

HEADER = struct.Struct("<I")
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
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(meta) > MAX_META:
        raise RenderPayloadError("render header is too large")
    return HEADER.pack(len(meta)) + meta + bytes(data)


def decode(payload: bytes) -> dict:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < HEADER.size:
        raise RenderPayloadError("render frame is truncated")
    (meta_len,) = HEADER.unpack_from(payload, 0)
    if meta_len <= 0 or meta_len > MAX_META:
        raise RenderPayloadError("render header length is invalid")
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
        "data": data,
    }
