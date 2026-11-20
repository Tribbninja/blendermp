"""Object creation payloads.

An object create frame carries the identity, a small JSON property bag and an
optional geometry snapshot. Deletes are a single UUID. Both are validated by the
host before relay and again by receivers before touching Blender.
"""

from __future__ import annotations

import json
import struct
import uuid as uuidlib

HEADER = struct.Struct("<16sBII")
MAX_META = 8 * 1024 * 1024
MAX_GEOMETRY = 48 * 1024 * 1024

KIND_EMPTY = 0
KIND_MESH = 1
KIND_CAMERA = 2
KIND_LIGHT = 3
KIND_CURVE = 4
KIND_FONT = 5
KIND_ARMATURE = 6
KINDS = {KIND_EMPTY, KIND_MESH, KIND_CAMERA, KIND_LIGHT, KIND_CURVE, KIND_FONT, KIND_ARMATURE}


class ObjectPayloadError(ValueError):
    pass


def encode_create(uid: str, kind: int, meta: dict, geometry: bytes = b"") -> bytes:
    if kind not in KINDS:
        raise ObjectPayloadError("unknown object kind")
    try:
        identity = uuidlib.UUID(str(uid)).bytes
    except ValueError as exc:
        raise ObjectPayloadError("invalid object id") from exc
    body = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_META:
        raise ObjectPayloadError("object metadata is too large")
    if len(geometry) > MAX_GEOMETRY:
        raise ObjectPayloadError("geometry is too large")
    return HEADER.pack(identity, kind, len(body), len(geometry)) + body + geometry


def decode_create(payload: bytes) -> dict:
    if len(payload) < HEADER.size:
        raise ObjectPayloadError("object frame is truncated")
    identity, kind, meta_len, geometry_len = HEADER.unpack_from(payload, 0)
    if kind not in KINDS:
        raise ObjectPayloadError("unknown object kind")
    if meta_len > MAX_META or geometry_len > MAX_GEOMETRY:
        raise ObjectPayloadError("object frame exceeds its limits")
    end = HEADER.size + meta_len
    if len(payload) < end + geometry_len:
        raise ObjectPayloadError("object frame is truncated")
    try:
        uid = str(uuidlib.UUID(bytes=identity))
    except ValueError as exc:
        raise ObjectPayloadError("invalid object id") from exc
    try:
        meta = json.loads(payload[HEADER.size : end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectPayloadError("invalid object metadata") from exc
    if not isinstance(meta, dict):
        raise ObjectPayloadError("object metadata must be an object")
    return {
        "uuid": uid,
        "kind": kind,
        "meta": meta,
        "geometry": payload[end : end + geometry_len],
    }


def encode_delete(uid: str) -> bytes:
    try:
        return uuidlib.UUID(str(uid)).bytes
    except ValueError as exc:
        raise ObjectPayloadError("invalid object id") from exc


def decode_delete(payload: bytes) -> str:
    if len(payload) != 16:
        raise ObjectPayloadError("delete frame must be 16 bytes")
    try:
        return str(uuidlib.UUID(bytes=payload))
    except ValueError as exc:
        raise ObjectPayloadError("invalid object id") from exc
