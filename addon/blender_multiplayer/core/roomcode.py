"""Room codes: a short, typo-resistant encoding of a host endpoint.

The code itself is the invitation. It carries the host's IPv4 address and UDP
port plus a checksum, encoded in Crockford base32 so it survives being read out
loud or retyped::

    K7M2Q-X9ABC-DEFGH

For IPv6 or relay candidates the UI shares a full invite (QR or link) instead;
the short code stays human-sized.
"""

from __future__ import annotations

import hashlib
import ipaddress
import struct
import zlib
from dataclasses import dataclass

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
DECODE = {ch: i for i, ch in enumerate(ALPHABET)}
DECODE.update({"O": 0, "I": 1, "L": 1})
CODE_LEN = 15
PAYLOAD = struct.Struct("<BB4sHB")  # version, flags, ipv4, port, checksum
PAYLOAD_SIZE = PAYLOAD.size  # 9 bytes
VERSION = 1
FLAG_PIN = 1 << 0
FLAG_RELAY = 1 << 1


class RoomCodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoomInfo:
    ip: str
    port: int
    flags: int = 0
    version: int = VERSION

    @property
    def needs_pin(self) -> bool:
        return bool(self.flags & FLAG_PIN)

    @property
    def endpoint(self) -> str:
        return f"{self.ip}:{self.port}"


def _to_base32(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    total = len(data) * 8
    chars = []
    for shift in range(total - 5, -1, -5):
        chars.append(ALPHABET[(bits >> shift) & 31])
    # pad the final partial group on the right with zero bits
    if total % 5:
        chars.append(ALPHABET[(bits << (5 - total % 5)) & 31])
    return "".join(chars)


def _from_base32(text: str) -> bytes:
    bits = 0
    for ch in text:
        value = DECODE.get(ch)
        if value is None:
            raise RoomCodeError(f"invalid room code character: {ch!r}")
        bits = (bits << 5) | value
    leftover = len(text) * 5 - PAYLOAD_SIZE * 8
    if leftover > 0:
        if bits & ((1 << leftover) - 1):
            raise RoomCodeError("room code has non-zero padding bits")
        bits >>= leftover
    elif leftover < 0:
        raise RoomCodeError("room code is too short")
    return bits.to_bytes(PAYLOAD_SIZE, "big")


def room_id(code: str) -> str:
    """Stable relay room key derived from the room code (not a secret)."""
    return hashlib.sha256(normalize(code).encode("ascii")).hexdigest()[:16]


def normalize(code: str) -> str:
    cleaned = []
    for ch in code.strip().upper():
        if ch.isalnum():
            cleaned.append("1" if ch in "IL" else "0" if ch == "O" else ch)
    return "".join(cleaned)


def format_code(code: str) -> str:
    text = normalize(code).replace("-", "").replace(" ", "")
    if len(text) != CODE_LEN:
        raise RoomCodeError(f"room code must be {CODE_LEN} characters")
    return f"{text[:5]}-{text[5:10]}-{text[10:]}"


def encode_room(ip: str, port: int, flags: int = 0) -> str:
    if not 0 < port < 65536:
        raise RoomCodeError("port out of range")
    try:
        packed = ipaddress.IPv4Address(ip).packed
    except ipaddress.AddressValueError as exc:
        raise RoomCodeError(f"not an IPv4 address: {ip}") from exc
    body = struct.pack("<BB4sH", VERSION, flags & 0xFF, packed, port)
    checksum = zlib.crc32(body) & 0xFF
    text = _to_base32(body + bytes([checksum]))
    return f"{text[:5]}-{text[5:10]}-{text[10:]}"


def decode_room(code: str) -> RoomInfo:
    text = normalize(code).replace("-", "")
    if len(text) != CODE_LEN:
        raise RoomCodeError(f"room code must be {CODE_LEN} characters, got {len(text)}")
    raw = _from_base32(text)
    version, flags, packed, port, checksum = PAYLOAD.unpack(raw)
    if version != VERSION:
        raise RoomCodeError(f"unsupported room code version {version}")
    expected = zlib.crc32(struct.pack("<BB4sH", version, flags, packed, port)) & 0xFF
    if checksum != expected:
        raise RoomCodeError("room code checksum mismatch")
    if not 0 < port < 65536:
        raise RoomCodeError("room code port out of range")
    return RoomInfo(ip=str(ipaddress.IPv4Address(packed)), port=port, flags=flags, version=version)
