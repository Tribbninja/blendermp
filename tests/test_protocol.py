import pytest

from core.protocol import (
    Channel,
    Frame,
    FrameStream,
    HEADER_SIZE,
    MessageType,
    decode,
    encode,
    json_frame,
)


def test_frame_roundtrip():
    frame = Frame(type=MessageType.CHAT, channel=Channel.CONTROL, seq=42)
    frame.json_body({"text": "hello", "name": "ada"})
    raw = encode(frame)
    decoded = decode(raw)
    assert decoded.type == MessageType.CHAT
    assert decoded.seq == 42
    assert decoded.json() == {"text": "hello", "name": "ada"}


def test_empty_payload():
    decoded = decode(encode(Frame(type=MessageType.PING)))
    assert decoded.payload == b""
    assert decoded.json() == {}


def test_crc_detects_tampering():
    raw = bytearray(encode(json_frame(MessageType.PRESENCE, {"name": "x"})))
    raw[-1] ^= 0xFF
    with pytest.raises(ValueError):
        decode(bytes(raw))


def test_short_and_bad_magic():
    with pytest.raises(ValueError):
        decode(b"x")
    raw = bytearray(encode(Frame(type=MessageType.PING)))
    raw[0] = ord("Z")
    with pytest.raises(ValueError):
        decode(bytes(raw))


def test_declared_length_too_large():
    raw = bytearray(encode(Frame(type=MessageType.PING)))
    raw[16:20] = (10_000_000).to_bytes(4, "little")
    with pytest.raises(ValueError):
        decode(bytes(raw))


def test_frame_stream_splits_partial_input():
    a = encode(json_frame(MessageType.PING, {"t": 1}))
    b = encode(json_frame(MessageType.PONG, {"t": 1}))
    stream = FrameStream()
    assert stream.feed(a[: HEADER_SIZE + 3]) == []
    frames = stream.feed(a[HEADER_SIZE + 3 :] + b)
    assert [frame.type for frame in frames] == [MessageType.PING, MessageType.PONG]
    assert len(stream) == 0


def test_unknown_type_is_still_decodable():
    raw = encode(Frame(type=200, payload=b"x"))
    assert decode(raw).type == 200
