"""STUN client: request encoding and XOR-MAPPED-ADDRESS decoding."""

import struct

import pytest

from net import stun


def _binding_response(tid: bytes, ip: str, port: int, xor: bool = True) -> bytes:
    parts = [int(p) for p in ip.split(".")]
    if xor:
        cookie = struct.pack("!I", stun.MAGIC_COOKIE)
        raw_ip = bytes(a ^ b for a, b in zip(bytes(parts), cookie))
        x_port = port ^ (stun.MAGIC_COOKIE >> 16)
        attr_type = stun.ATTR_XOR_MAPPED_ADDRESS
    else:
        raw_ip = bytes(parts)
        x_port = port
        attr_type = stun.ATTR_MAPPED_ADDRESS
    value = bytes([0x00, stun.FAMILY_IPV4]) + struct.pack("!H", x_port) + raw_ip
    attr = struct.pack("!HH", attr_type, len(value)) + value
    attr += b"\x00" * ((4 - len(value) % 4) % 4)
    return struct.pack("!HHI12s", stun.BINDING_RESPONSE, len(attr), stun.MAGIC_COOKIE, tid) + attr


def test_build_request_has_magic_cookie_and_transaction_id():
    packet, tid = stun.build_request()
    assert len(tid) == 12
    message_type, length, cookie, parsed_tid = stun.HEADER.unpack_from(packet, 0)
    assert message_type == stun.BINDING_REQUEST
    assert length == 0
    assert cookie == stun.MAGIC_COOKIE
    assert parsed_tid == tid


def test_parse_xor_mapped_address():
    packet, tid = stun.build_request()
    response = _binding_response(tid, "203.0.113.7", 51820)
    assert stun.parse_response(response, tid) == ("203.0.113.7", 51820)


def test_parse_plain_mapped_address_fallback():
    packet, tid = stun.build_request()
    response = _binding_response(tid, "198.51.100.9", 40000, xor=False)
    assert stun.parse_response(response, tid) == ("198.51.100.9", 40000)


def test_parse_rejects_wrong_transaction_id():
    packet, tid = stun.build_request()
    response = _binding_response(b"\x00" * 12, "203.0.113.7", 51820)
    with pytest.raises(stun.StunError):
        stun.parse_response(response, tid)


def test_parse_rejects_wrong_magic_cookie():
    packet, tid = stun.build_request()
    response = bytearray(_binding_response(tid, "203.0.113.7", 51820))
    response[4:8] = b"\x00\x00\x00\x00"
    with pytest.raises(stun.StunError):
        stun.parse_response(bytes(response), tid)


def test_parse_rejects_missing_address():
    packet, tid = stun.build_request()
    response = struct.pack("!HHI12s", stun.BINDING_RESPONSE, 0, stun.MAGIC_COOKIE, tid)
    with pytest.raises(stun.StunError):
        stun.parse_response(response, tid)
