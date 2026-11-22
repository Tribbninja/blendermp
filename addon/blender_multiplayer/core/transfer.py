"""Chunked one-shot transfers.

Object creation frames can be much larger than a comfortable single frame once
geometry, rig data and multiple UV layers are included. This module splits one
logical payload into bounded chunks and reassembles it on the other side.

The transfer id is derived from the payload hash, so retrying the same object
after a reconnect reuses the same id. Receivers keep partial transfers for a
few minutes and ignore duplicate chunks, which makes retries resume rather than
restart.
"""

from __future__ import annotations

import hashlib
import struct
import time

MAGIC = b"BMT1"
HEADER = struct.Struct("<4s16sBBHII")
KIND_OBJECT = 0
KIND_IMAGE = 1
KINDS = {KIND_OBJECT, KIND_IMAGE}
CHUNK_SIZE = 900 * 1024  # ~900 KB; stays under Cloudflare's ~1 MiB WebSocket cap
MAX_TRANSFER = 256 * 1024 * 1024
MAX_CHUNKS = 8192
TTL = 300.0


class TransferError(ValueError):
    pass


def transfer_id(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()[:16]


def split(payload: bytes, kind: int = KIND_OBJECT) -> tuple[bytes, list[bytes]]:
    if not isinstance(payload, (bytes, bytearray)):
        raise TransferError("transfer payload must be bytes")
    if kind not in KINDS:
        raise TransferError("unknown transfer kind")
    payload = bytes(payload)
    if not payload:
        raise TransferError("transfer payload is empty")
    if len(payload) > MAX_TRANSFER:
        raise TransferError("transfer payload is too large")
    total = (len(payload) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if total > MAX_CHUNKS:
        raise TransferError("transfer has too many chunks")
    identity = transfer_id(payload)
    chunks = []
    for index in range(total):
        start = index * CHUNK_SIZE
        data = payload[start : start + CHUNK_SIZE]
        chunks.append(
            HEADER.pack(MAGIC, identity, kind, 0, index, total, len(payload)) + data
        )
    return identity, chunks


def decode_chunk(payload: bytes) -> dict:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < HEADER.size:
        raise TransferError("chunk is truncated")
    magic, identity, kind, flags, index, total, total_size = HEADER.unpack_from(payload, 0)
    if magic != MAGIC:
        raise TransferError("bad chunk magic")
    if kind not in KINDS:
        raise TransferError("unknown transfer kind")
    if flags != 0:
        raise TransferError("unknown chunk flags")
    if total <= 0 or total > MAX_CHUNKS or index >= total:
        raise TransferError("invalid chunk index")
    if total_size <= 0 or total_size > MAX_TRANSFER:
        raise TransferError("invalid transfer size")
    expected = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    if total != expected:
        raise TransferError("chunk count does not match transfer size")
    data = payload[HEADER.size :]
    if not data:
        raise TransferError("chunk has no data")
    if index < total - 1 and len(data) != CHUNK_SIZE:
        raise TransferError("non-final chunk has the wrong size")
    if index == total - 1:
        expected_size = total_size - index * CHUNK_SIZE
        if len(data) != expected_size:
            raise TransferError("final chunk has the wrong size")
    return {
        "id": identity,
        "kind": kind,
        "index": index,
        "total": total,
        "total_size": total_size,
        "data": data,
    }


class Reassembler:
    """Partial transfers keyed by sender and content hash."""

    def __init__(self, ttl: float = TTL):
        self.ttl = ttl
        self._entries: dict[tuple[str, bytes], dict] = {}

    def add(self, sender: str, payload: bytes, now: float | None = None) -> tuple[dict | None, dict]:
        now = time.monotonic() if now is None else now
        chunk = decode_chunk(payload)
        key = (str(sender)[:48], chunk["id"])
        entry = self._entries.get(key)
        if entry is None:
            if chunk["index"] != 0:
                raise TransferError("transfer did not start at chunk zero")
            entry = {
                "kind": chunk["kind"],
                "total": chunk["total"],
                "total_size": chunk["total_size"],
                "chunks": [None] * chunk["total"],
                "received": 0,
                "bytes": 0,
                "updated": now,
            }
            self._entries[key] = entry
        else:
            if (
                entry["kind"] != chunk["kind"]
                or entry["total"] != chunk["total"]
                or entry["total_size"] != chunk["total_size"]
            ):
                raise TransferError("chunk does not match its transfer")
        index = chunk["index"]
        existing = entry["chunks"][index]
        if existing is None:
            entry["chunks"][index] = chunk["data"]
            entry["received"] += 1
            entry["bytes"] += len(chunk["data"])
        elif existing != chunk["data"]:
            raise TransferError("duplicate chunk has different data")
        entry["updated"] = now
        progress = {
            "id": chunk["id"].hex(),
            "kind": chunk["kind"],
            "received": entry["received"],
            "total": entry["total"],
            "bytes": entry["bytes"],
            "total_bytes": entry["total_size"],
            "percent": round(entry["bytes"] * 100.0 / max(1, entry["total_size"]), 1),
        }
        if entry["received"] < entry["total"]:
            return None, progress
        data = b"".join(entry["chunks"])
        if len(data) != entry["total_size"]:
            self._entries.pop(key, None)
            raise TransferError("assembled transfer has the wrong size")
        self._entries.pop(key, None)
        return {"kind": entry["kind"], "data": data, "id": chunk["id"]}, progress

    def sweep(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        expired = [
            key
            for key, entry in self._entries.items()
            if now - float(entry.get("updated", now)) > self.ttl
        ]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def pending(self) -> int:
        return len(self._entries)
