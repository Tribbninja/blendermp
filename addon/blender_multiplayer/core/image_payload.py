"""Image asset payloads.

A single frame carries an encoded image (PNG/JPEG/EXR bytes) plus a name and
format hint. Images are user-supplied binaries, so both ends cap the size and
only accept a conservative format list.
"""

from __future__ import annotations

import hashlib
import struct
import re
import zlib

from . import compression

HEADER = struct.Struct("<HBBI")
DIGEST_SIZE = 20
MAX_IMAGE = 32 * 1024 * 1024
COMPRESS_THRESHOLD = 10 * 1024 * 1024
FLAG_ZLIB = 1
MAX_NAME = 64
MAX_FORMAT = 12
FORMAT_RE = re.compile(r"^[a-z0-9]{2,8}$")
ALLOWED = {
    "png", "jpg", "jpeg", "bmp", "tga", "tif", "tiff", "webp", "exr", "hdr", "gif", "psd",
}


class ImagePayloadError(ValueError):
    pass


def encode(name: str, format_hint: str, data: bytes, compress: bool | None = None) -> bytes:
    if compress is None:
        compress = compression.enabled()
    name = (name or "Image")[:MAX_NAME]
    format_hint = (format_hint or "png").lower().lstrip(".")
    if not FORMAT_RE.match(format_hint) or format_hint not in ALLOWED:
        raise ImagePayloadError("unsupported image format")
    if not data or len(data) > MAX_IMAGE:
        raise ImagePayloadError("image data is empty or too large")
    encoded_name = name.encode("utf-8", "replace")
    encoded_format = format_hint.encode("ascii")
    digest = hashlib.sha1(data).digest()
    flags = 0
    wire = data
    if compress and len(data) > COMPRESS_THRESHOLD:
        packed = zlib.compress(data, 6)
        if len(packed) < len(data):
            wire = packed
            flags |= FLAG_ZLIB
    return (
        HEADER.pack(len(encoded_name), len(encoded_format), flags, len(wire))
        + digest
        + encoded_name
        + encoded_format
        + wire
    )


def decode(payload: bytes) -> dict:
    if len(payload) < HEADER.size + DIGEST_SIZE:
        raise ImagePayloadError("image frame is truncated")
    name_len, format_len, flags, data_len = HEADER.unpack_from(payload, 0)
    if name_len > MAX_NAME or format_len > MAX_FORMAT or data_len > MAX_IMAGE * 2:
        raise ImagePayloadError("image frame exceeds its limits")
    offset = HEADER.size
    digest = payload[offset : offset + DIGEST_SIZE]
    offset += DIGEST_SIZE
    end = offset + name_len + format_len + data_len
    if len(payload) != end:
        raise ImagePayloadError("image frame length mismatch")
    name = payload[offset : offset + name_len].decode("utf-8", "replace") or "Image"
    offset += name_len
    format_hint = payload[offset : offset + format_len].decode("ascii", "replace").lower()
    offset += format_len
    if not FORMAT_RE.match(format_hint) or format_hint not in ALLOWED:
        raise ImagePayloadError("unsupported image format")
    wire = payload[offset:]
    if flags & FLAG_ZLIB:
        try:
            decompressor = zlib.decompressobj()
            data = decompressor.decompress(wire, MAX_IMAGE + 1)
        except zlib.error as exc:
            raise ImagePayloadError("invalid compressed image") from exc
        if len(data) > MAX_IMAGE or decompressor.unconsumed_tail:
            raise ImagePayloadError("compressed image is too large")
    else:
        data = wire
    if hashlib.sha1(data).digest() != digest:
        raise ImagePayloadError("image digest does not match its data")
    return {"name": name, "format": format_hint, "sha1": digest.hex(), "data": data, "compressed": bool(flags & FLAG_ZLIB)}
