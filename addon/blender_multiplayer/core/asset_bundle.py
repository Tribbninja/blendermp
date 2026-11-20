"""Content-addressed asset bundles.

A bundle is a one-shot offer: one peer packages selected objects (geometry and
metadata), their materials and the images those materials use, and sends it to
another peer through the host. The receiving side previews the summary and
imports only after the user accepts.

The wire format is a tiny header plus zlib-compressed JSON. Geometry and image
bytes are base64 inside that JSON because JSON keeps the manifest inspectable
and dependency-free. Both ends enforce the same caps; the host decodes and
re-encodes before relaying so a malformed or oversized bundle never reaches a
client.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
import uuid as uuidlib
import zlib

from . import image_payload, material as material_payload, object_payload
from . import chunker

MAGIC = b"BMB1"
HEADER = struct.Struct("<4sI")
VERSION = 1
MAX_RAW = 96 * 1024 * 1024
MAX_WIRE = 56 * 1024 * 1024
MAX_OBJECTS = 64
MAX_IMAGES = 64
MAX_LABEL = 64
MAX_GEOMETRY = object_payload.MAX_GEOMETRY


class AssetBundleError(ValueError):
    pass


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value, limit: int) -> bytes:
    if not isinstance(value, str):
        raise AssetBundleError("asset data must be base64 text")
    if len(value) > (limit * 4) // 3 + 8:
        raise AssetBundleError("asset data is too large")
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AssetBundleError("invalid base64 asset data") from exc
    if len(data) > limit:
        raise AssetBundleError("asset data is too large")
    return data


def _clean_meta(meta: dict) -> dict:
    """Sanitize material slots and drop the bundle-only identity fields."""
    if not isinstance(meta, dict):
        raise AssetBundleError("object metadata must be an object")
    clean = dict(meta)
    clean.pop("bmp_uuid", None)
    materials = clean.get("materials")
    if isinstance(materials, list):
        slots = []
        for item in materials[:16]:
            if not isinstance(item, dict):
                continue
            slot = item.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 255:
                continue
            data = material_payload.sanitize(item)
            if data is None:
                continue
            data["slot"] = slot
            slots.append(data)
        if slots:
            clean["materials"] = slots
        else:
            clean.pop("materials", None)
    return clean


def _clean_object(item: dict) -> dict:
    if not isinstance(item, dict):
        raise AssetBundleError("bundle object must be an object")
    try:
        uid = str(uuidlib.UUID(str(item.get("uid"))))
    except (ValueError, TypeError) as exc:
        raise AssetBundleError("invalid bundle object id") from exc
    kind = item.get("kind")
    if not isinstance(kind, int) or isinstance(kind, bool) or kind not in object_payload.KINDS:
        raise AssetBundleError("invalid bundle object kind")
    meta = _clean_meta(item.get("meta") or {})
    matrix = meta.get("matrix")
    if not isinstance(matrix, list) or len(matrix) != 16:
        raise AssetBundleError("bundle object needs a transform")
    try:
        matrix = [float(value) for value in matrix]
    except (TypeError, ValueError) as exc:
        raise AssetBundleError("invalid bundle transform") from exc
    if not all(math.isfinite(value) and abs(value) < 1e12 for value in matrix):
        raise AssetBundleError("invalid bundle transform")
    meta["matrix"] = matrix
    geometry = item.get("geometry", b"")
    if not isinstance(geometry, (bytes, bytearray)):
        raise AssetBundleError("bundle geometry must be bytes")
    geometry = bytes(geometry)
    if len(geometry) > MAX_GEOMETRY:
        raise AssetBundleError("bundle geometry is too large")
    if kind == object_payload.KIND_MESH and geometry:
        try:
            chunker.validate_geometry(geometry)
        except ValueError as exc:
            raise AssetBundleError("invalid bundle geometry") from exc
    elif kind != object_payload.KIND_MESH and geometry:
        raise AssetBundleError("only mesh objects may carry geometry")
    # Reuse the object frame validator for the metadata and size limits.
    try:
        object_payload.encode_create(uid, kind, meta, geometry)
    except object_payload.ObjectPayloadError as exc:
        raise AssetBundleError(str(exc)) from exc
    return {"uid": uid, "kind": kind, "meta": meta, "geometry": geometry}


def _clean_image(item: dict) -> dict:
    if not isinstance(item, dict):
        raise AssetBundleError("bundle image must be an object")
    name = str(item.get("name", "Image"))[: image_payload.MAX_NAME] or "Image"
    format_hint = str(item.get("format", "png")).lower()
    if format_hint not in image_payload.ALLOWED:
        raise AssetBundleError("unsupported bundle image format")
    data = item.get("data", b"")
    if not isinstance(data, (bytes, bytearray)):
        raise AssetBundleError("bundle image data must be bytes")
    data = bytes(data)
    if not data or len(data) > image_payload.MAX_IMAGE:
        raise AssetBundleError("bundle image is empty or too large")
    digest = hashlib.sha1(data).hexdigest()
    claimed = item.get("sha1")
    if claimed is not None and (not isinstance(claimed, str) or claimed.lower() != digest):
        raise AssetBundleError("bundle image digest mismatch")
    return {"name": name, "format": format_hint, "sha1": digest, "data": data}


def _clean(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise AssetBundleError("bundle must be an object")
    objects = manifest.get("objects")
    images = manifest.get("images", [])
    if not isinstance(objects, list) or not objects or len(objects) > MAX_OBJECTS:
        raise AssetBundleError("bundle has no objects or too many")
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise AssetBundleError("bundle has too many images")
    clean_objects = [_clean_object(item) for item in objects]
    clean_images = [_clean_image(item) for item in images]
    label = str(manifest.get("label", ""))
    label = "".join(ch for ch in label if ord(ch) >= 32)[:MAX_LABEL]
    target = manifest.get("target", "")
    if not isinstance(target, str) or len(target) > 48 or any(ord(ch) < 32 for ch in target):
        target = ""
    sender = manifest.get("sender", "")
    if not isinstance(sender, str) or len(sender) > 48 or any(ord(ch) < 32 for ch in sender):
        sender = ""
    color = manifest.get("sender_color")
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        color = []
    else:
        try:
            color = [max(0.0, min(1.0, float(value))) for value in color[:4]]
        except (TypeError, ValueError):
            color = []
        if len(color) == 3:
            color.append(1.0)
    return {
        "objects": clean_objects,
        "images": clean_images,
        "label": label,
        "target": target,
        "sender": sender,
        "sender_color": color,
    }


def _content(manifest: dict) -> dict:
    return {
        "v": VERSION,
        "label": manifest.get("label", ""),
        "objects": [
            {
                "uid": item["uid"],
                "kind": item["kind"],
                "meta": item["meta"],
                "geometry": _b64(item["geometry"]),
            }
            for item in manifest["objects"]
        ],
        "images": [
            {
                "name": item["name"],
                "format": item["format"],
                "sha1": item["sha1"],
                "data": _b64(item["data"]),
            }
            for item in manifest["images"]
        ],
    }


def content_id(manifest: dict) -> str:
    body = _content(_clean(manifest))
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def pack(manifest: dict) -> bytes:
    clean = _clean(manifest)
    clean["id"] = content_id(clean)
    body = _content(clean)
    body["id"] = clean["id"]
    body["target"] = clean.get("target", "")
    body["sender"] = clean.get("sender", "")
    body["sender_color"] = clean.get("sender_color", [])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RAW:
        raise AssetBundleError("bundle exceeds the uncompressed limit")
    wire = HEADER.pack(MAGIC, len(raw)) + zlib.compress(raw, 9)
    if len(wire) > MAX_WIRE:
        raise AssetBundleError("bundle exceeds the transfer limit")
    return wire


def decode(payload: bytes) -> dict:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < HEADER.size:
        raise AssetBundleError("bundle frame is truncated")
    if len(payload) > MAX_WIRE:
        raise AssetBundleError("bundle frame is too large")
    magic, raw_len = HEADER.unpack_from(payload, 0)
    if magic != MAGIC:
        raise AssetBundleError("bad bundle magic")
    if raw_len <= 0 or raw_len > MAX_RAW:
        raise AssetBundleError("bundle size is invalid")
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(payload[HEADER.size :], raw_len + 1)
    except zlib.error as exc:
        raise AssetBundleError("invalid compressed bundle") from exc
    if len(raw) != raw_len or decompressor.unconsumed_tail or decompressor.unused_data or not decompressor.eof:
        raise AssetBundleError("bundle length mismatch")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssetBundleError("invalid bundle JSON") from exc
    if not isinstance(body, dict) or body.get("v") != VERSION:
        raise AssetBundleError("unsupported bundle version")
    objects = body.get("objects")
    images = body.get("images", [])
    if not isinstance(objects, list):
        raise AssetBundleError("bundle objects must be a list")
    if not isinstance(images, list):
        raise AssetBundleError("bundle images must be a list")
    decoded = {
        "label": body.get("label", ""),
        "target": body.get("target", ""),
        "sender": body.get("sender", ""),
        "sender_color": body.get("sender_color", []),
        "objects": [],
        "images": [],
    }
    for item in objects:
        if not isinstance(item, dict):
            raise AssetBundleError("invalid bundle object")
        decoded["objects"].append(
            {
                "uid": item.get("uid"),
                "kind": item.get("kind"),
                "meta": item.get("meta"),
                "geometry": _unb64(item.get("geometry", ""), MAX_GEOMETRY),
            }
        )
    for item in images:
        if not isinstance(item, dict):
            raise AssetBundleError("invalid bundle image")
        decoded["images"].append(
            {
                "name": item.get("name"),
                "format": item.get("format"),
                "sha1": item.get("sha1"),
                "data": _unb64(item.get("data", ""), image_payload.MAX_IMAGE),
            }
        )
    clean = _clean(decoded)
    expected = str(body.get("id", ""))
    actual = content_id(clean)
    if expected and expected != actual:
        raise AssetBundleError("bundle content hash does not match")
    clean["id"] = actual
    return clean


def summary(manifest: dict) -> dict:
    objects = manifest.get("objects") or []
    images = manifest.get("images") or []
    bundle_id = str(manifest.get("id", ""))[:32]
    if not bundle_id:
        try:
            bundle_id = content_id(manifest)
        except AssetBundleError:
            bundle_id = ""
    meshes = sum(1 for item in objects if item.get("kind") == object_payload.KIND_MESH)
    vertices = 0
    for item in objects:
        geometry = item.get("geometry") or b""
        if not geometry:
            continue
        try:
            vertices += int(chunker.validate_geometry(geometry)[0])
        except (ValueError, TypeError):
            continue
    names = [str((item.get("meta") or {}).get("name", "Object"))[:48] for item in objects[:8]]
    return {
        "id": bundle_id,
        "label": str(manifest.get("label", ""))[:MAX_LABEL],
        "objects": len(objects),
        "meshes": meshes,
        "vertices": vertices,
        "images": len(images),
        "names": names,
    }
