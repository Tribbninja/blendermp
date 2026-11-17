"""BMP/1 wire protocol.

Every message is a single frame::

    magic     4s   b"BMP1"
    version   u8   protocol version (1)
    type      u8   MessageType
    channel   u8   Channel
    flags     u8
    seq       u32  sender-local sequence number
    length    u32  payload length
    crc       u32  crc32 over type..length and payload
    payload   ...

Frames are cheap to parse and never allocate intermediate dicts, which is the
whole point: control messages are JSON, but geometry and transforms are binary.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

MAGIC = b"BMP1"
PROTOCOL_VERSION = 1
HEADER = struct.Struct("<4sBBBBIII")
HEADER_SIZE = HEADER.size  # 20 bytes
MAX_PAYLOAD = 32 * 1024 * 1024


class MessageType(IntEnum):
    HELLO = 1
    WELCOME = 2
    JOIN = 3
    PRESENCE = 4
    CHAT = 5
    PING = 6
    PONG = 7
    TRANSFORM = 8
    MESH_DELTA = 9
    MESH_SNAPSHOT = 10
    LEASE = 11
    ACK = 12
    BULK = 13
    ERROR = 14
    AUTH = 15
    BYE = 16
    PING_MARK = 17
    OBJECT_CREATE = 18
    OBJECT_DELETE = 19
    MATERIAL_UPDATE = 20


class Channel(IntEnum):
    CONTROL = 0
    TRANSFORM = 1
    MESH = 2
    BULK = 3
    PRESENCE = 4


@dataclass(slots=True)
class Frame:
    type: int
    channel: int = Channel.CONTROL
    flags: int = 0
    seq: int = 0
    payload: bytes = b""

    def json(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        return json.loads(self.payload.decode("utf-8"))

    def json_body(self, body: dict[str, Any]) -> None:
        self.payload = json.dumps(body, separators=(",", ":")).encode("utf-8")


def encode(frame: Frame) -> bytes:
    payload = frame.payload
    length = len(payload)
    if length > MAX_PAYLOAD:
        raise ValueError(f"payload of {length} bytes exceeds {MAX_PAYLOAD}")
    prefix = struct.pack("<BBBBII", PROTOCOL_VERSION, int(frame.type), int(frame.channel), int(frame.flags), int(frame.seq), length)
    crc = zlib.crc32(prefix)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, int(frame.type), int(frame.channel), int(frame.flags), int(frame.seq), length, crc) + payload


def decode(data: bytes) -> Frame:
    if len(data) < HEADER_SIZE:
        raise ValueError("frame is shorter than its header")
    magic, version, type_, channel, flags, seq, length, crc = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("bad frame magic")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version {version}")
    if length > MAX_PAYLOAD:
        raise ValueError("declared payload is too large")
    end = HEADER_SIZE + length
    if len(data) < end:
        raise ValueError("truncated frame payload")
    payload = data[HEADER_SIZE:end]
    check = zlib.crc32(struct.pack("<BBBBII", version, type_, channel, flags, seq, length))
    check = zlib.crc32(payload, check) & 0xFFFFFFFF
    if check != crc:
        raise ValueError("frame crc mismatch")
    return Frame(type=type_, channel=channel, flags=flags, seq=seq, payload=payload)


class FrameStream:
    """Incremental frame splitter for stream transports (TCP, files)."""

    def __init__(self, max_buffer: int = MAX_PAYLOAD + HEADER_SIZE):
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while len(self._buffer) >= HEADER_SIZE:
            magic, version, type_, channel, flags, seq, length, _crc = HEADER.unpack_from(self._buffer)
            if magic != MAGIC:
                raise ValueError("bad frame magic in stream")
            if version != PROTOCOL_VERSION:
                raise ValueError(f"unsupported protocol version {version}")
            total = HEADER_SIZE + length
            if total > self._max_buffer:
                raise ValueError("declared frame exceeds max buffer")
            if len(self._buffer) < total:
                break
            frames.append(decode(bytes(self._buffer[:total])))
            del self._buffer[:total]
        return frames

    def reset(self) -> None:
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


def json_frame(type_: MessageType, body: dict[str, Any], channel: Channel = Channel.CONTROL, seq: int = 0) -> Frame:
    frame = Frame(type=type_, channel=channel, seq=seq)
    frame.json_body(body)
    return frame
