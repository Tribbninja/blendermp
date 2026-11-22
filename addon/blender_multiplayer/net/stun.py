"""Minimal STUN client (RFC 5389) for discovering the public UDP endpoint.

Only the binding request / XOR-MAPPED-ADDRESS path is implemented, which is all
the mesh needs: a peer asks a public STUN server "what address do you see me
coming from?" and uses the answer as a hole-punching candidate.

Pure stdlib. A query is one UDP datagram out and one back, so it is cheap enough
to run on every join and again before each connection attempt.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import struct

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_SOFTWARE = 0x8022
HEADER = struct.Struct("!HHI12s")
HEADER_SIZE = HEADER.size
FAMILY_IPV4 = 0x01
FAMILY_IPV6 = 0x02

DEFAULT_SERVERS = (
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
)


class StunError(Exception):
    pass


def build_request(transaction_id: bytes | None = None) -> tuple[bytes, bytes]:
    """Return ``(packet, transaction_id)`` for a binding request."""
    tid = transaction_id or os.urandom(12)
    if len(tid) != 12:
        raise StunError("transaction id must be 12 bytes")
    return HEADER.pack(BINDING_REQUEST, 0, MAGIC_COOKIE, tid), tid


def _decode_address(value: bytes, xor: bool) -> tuple[str, int]:
    if len(value) < 4:
        raise StunError("address attribute is truncated")
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    if xor:
        port ^= (MAGIC_COOKIE >> 16) & 0xFFFF
    raw = value[4:]
    if family == FAMILY_IPV4:
        if len(raw) != 4:
            raise StunError("bad IPv4 address length")
        if xor:
            cookie = struct.pack("!I", MAGIC_COOKIE)
            raw = bytes(a ^ b for a, b in zip(raw, cookie))
        return str(ipaddress.IPv4Address(raw)), port
    if family == FAMILY_IPV6:
        if len(raw) != 16:
            raise StunError("bad IPv6 address length")
        if xor:
            cookie = struct.pack("!I", MAGIC_COOKIE) + (b"\x00" * 12)
            raw = bytes(a ^ b for a, b in zip(raw, cookie))
        return str(ipaddress.IPv6Address(raw)), port
    raise StunError("unsupported address family")


def parse_response(data: bytes, transaction_id: bytes) -> tuple[str, int]:
    """Extract the public endpoint from a binding success response."""
    if len(data) < HEADER_SIZE:
        raise StunError("response is truncated")
    message_type, length, cookie, tid = HEADER.unpack_from(data, 0)
    if cookie != MAGIC_COOKIE:
        raise StunError("bad magic cookie")
    if tid != transaction_id:
        raise StunError("transaction id mismatch")
    if message_type != BINDING_RESPONSE:
        raise StunError(f"unexpected message type 0x{message_type:04x}")
    if len(data) < HEADER_SIZE + length:
        raise StunError("response length is invalid")
    offset = HEADER_SIZE
    end = HEADER_SIZE + length
    mapped = None
    xor_mapped = None
    while offset + 4 <= end:
        attr_type, attr_len = struct.unpack_from("!HH", data, offset)
        offset += 4
        if offset + attr_len > end:
            raise StunError("attribute overruns the message")
        value = data[offset : offset + attr_len]
        offset += attr_len + ((4 - attr_len % 4) % 4)
        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            xor_mapped = value
        elif attr_type == ATTR_MAPPED_ADDRESS:
            mapped = value
    if xor_mapped is not None:
        return _decode_address(xor_mapped, xor=True)
    if mapped is not None:
        return _decode_address(mapped, xor=False)
    raise StunError("response has no mapped address")


async def query(
    server: tuple[str, int] = DEFAULT_SERVERS[0],
    timeout: float = 3.0,
    local_port: int = 0,
) -> tuple[str, int]:
    """Ask one STUN server for our public endpoint.

    ``local_port`` lets a caller reuse the socket it will later punch from, so
    the mapping STUN observes is the one the peer will actually use.
    """
    host, port = server
    loop = asyncio.get_running_loop()
    try:
        address = socket_gethostbyname(host)
    except OSError as exc:
        raise StunError(f"cannot resolve STUN server: {exc}") from exc
    sock = socket_udp(local_port)
    try:
        packet, tid = build_request()
        await loop.sock_sendto(sock, packet, (address, port))
        try:
            data, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout)
        except asyncio.TimeoutError as exc:
            raise StunError("STUN server did not answer") from exc
        return parse_response(data, tid)
    finally:
        sock.close()


def socket_udp(local_port: int = 0):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    # Windows rejects sendto on an unbound datagram socket, so always bind to an
    # ephemeral port (or the requested one).
    sock.bind(("0.0.0.0", int(local_port or 0)))
    return sock


def socket_gethostbyname(host: str) -> str:
    import socket

    return socket.gethostbyname(host)


async def discover(
    servers=DEFAULT_SERVERS,
    timeout: float = 3.0,
    local_port: int = 0,
) -> tuple[str, int] | None:
    """Try each server in order and return the first public endpoint."""
    for server in servers:
        try:
            return await query(server, timeout=timeout, local_port=local_port)
        except (StunError, OSError, asyncio.TimeoutError):
            continue
    return None
