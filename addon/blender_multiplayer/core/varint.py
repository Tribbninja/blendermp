"""Varint primitives shared by the wire protocol."""

from __future__ import annotations

from typing import Iterable

MAX_U64 = (1 << 64) - 1


def encode_uvarint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    if value > MAX_U64:
        raise ValueError("uvarint exceeds 64 bits")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_uvarint(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint is too long")


def zigzag(value: int) -> int:
    return (value << 1) if value >= 0 else ((-value << 1) - 1)


def unzigzag(value: int) -> int:
    return (value >> 1) if not value & 1 else -((value + 1) >> 1)


def encode_svarint(value: int) -> bytes:
    return encode_uvarint(zigzag(value))


def decode_svarint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value, offset = decode_uvarint(data, offset)
    return unzigzag(value), offset


def encode_uvarints(values: Iterable[int]) -> bytes:
    return b"".join(encode_uvarint(v) for v in values)


def decode_uvarints(data: bytes, count: int, offset: int = 0) -> tuple[list[int], int]:
    values = []
    for _ in range(count):
        value, offset = decode_uvarint(data, offset)
        values.append(value)
    return values, offset
