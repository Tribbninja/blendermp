"""Image asset payloads.

A single frame carries an encoded image (PNG/JPEG/EXR bytes) plus a name and
format hint. Images are user-supplied binaries, so both ends cap the size and
only accept a conservative format list.
"""

from __future__ import annotations

import struct
import re

HEADER = struct.Struct("<HBBI")
MAX_IMAGE = 16 * 1024 * 1024
MAX_NAME = 64
MAX_FORMAT = 12
FORMAT_RE = re.compile(r"^[a-z0-9]{2,8}$")
ALLOWED = {
    "png", "jpg", "jpeg", "bmp", "tga", "tif", "tiff", "webp", "exr", "hdr", "gif", "psd",
}


class ImagePayloadError(ValueError):
    pass


def encode(name: str, format_hint: str, data: bytes) -> bytes:
    name = (name or "Image")[:MAX_NAME]
    format_hint = (format_hint or "png").lower().lstrip(".")
    if not FORMAT_RE.match(format_hint) or format_hint not in ALLOWED:
        raise ImagePayloadError("unsupported image format")
    if not data or len(data) > MAX_IMAGE:
        raise ImagePayloadError("image data is empty or too large")
    encoded_name = name.encode("utf-8", "replace")
    encoded_format = format_hint.encode("ascii")
    return HEADER.pack(len(encoded_name), len(encoded_format), len(data), 0) + encoded_name + encoded_format + data


def decode(payload: bytes) -> dict:
    if len(payload) < HEADER.size:
        raise ImagePayloadError("image frame is truncated")
    name_len, format_len, data_len, _flags = HEADER.unpack_from(payload, 0)
    if name_len > MAX_NAME or format_len > MAX_FORMAT or data_len > MAX_IMAGE:
        raise ImagePayloadError("image frame exceeds its limits")
    offset = HEADER.size
    end = offset + name_len + format_len + data_len
    if len(payload) != end:
        raise ImagePayloadError("image frame length mismatch")
    name = payload[offset : offset + name_len].decode("utf-8", "replace") or "Image"
    offset += name_len
    format_hint = payload[offset : offset + format_len].decode("ascii", "replace").lower()
    offset += format_len
    if not FORMAT_RE.match(format_hint) or format_hint not in ALLOWED:
        raise ImagePayloadError("unsupported image format")
    return {"name": name, "format": format_hint, "data": payload[offset:]}
