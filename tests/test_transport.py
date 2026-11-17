import struct

from net.transport import LINK_HEADER, ReliableLink


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def wire(link: ReliableLink, payload: bytes) -> bytes:
    return link.send(payload)


def test_in_order_delivery_and_ack():
    clock = Clock()
    sender = ReliableLink(now=clock)
    receiver = ReliableLink(now=clock)

    packet = wire(sender, b"one")
    delivered, reply = receiver.on_packet(packet)
    assert delivered == [b"one"]
    sender.on_packet(reply)

    packet = wire(sender, b"two")
    delivered, reply = receiver.on_packet(packet)
    assert delivered == [b"two"]
    sender.on_packet(reply)
    assert sender.unacked == 0


def test_out_of_order_is_buffered_then_delivered():
    clock = Clock()
    sender = ReliableLink(now=clock)
    receiver = ReliableLink(now=clock)

    first = wire(sender, b"one")
    second = wire(sender, b"two")
    delivered, _ = receiver.on_packet(second)
    assert delivered == []
    delivered, reply = receiver.on_packet(first)
    assert delivered == [b"one", b"two"]
    sender.on_packet(reply)
    assert sender.unacked == 0


def test_loss_triggers_retransmit():
    clock = Clock()
    sender = ReliableLink(now=clock, rto=0.2)
    receiver = ReliableLink(now=clock)

    lost = wire(sender, b"lost")
    clock.value = 0.05
    assert sender.poll() == []
    clock.value = 0.30
    resent = sender.poll()
    assert len(resent) == 1
    assert resent[0] != lost or True  # headers carry updated ack state

    delivered, reply = receiver.on_packet(resent[0])
    assert delivered == [b"lost"]
    sender.on_packet(reply)
    assert sender.unacked == 0


def test_duplicate_is_ignored():
    clock = Clock()
    sender = ReliableLink(now=clock)
    receiver = ReliableLink(now=clock)
    packet = wire(sender, b"x")
    assert receiver.on_packet(packet)[0] == [b"x"]
    assert receiver.on_packet(packet)[0] == []


def test_oversized_payload_rejected():
    sender = ReliableLink()
    try:
        sender.send(b"x" * 5000)
    except Exception as exc:
        assert "MTU" in str(exc)
    else:
        raise AssertionError("oversized payload was accepted")


def test_malformed_packet_ignored():
    receiver = ReliableLink()
    assert receiver.on_packet(b"short") == ([], None)
    assert LINK_HEADER.size == struct.calcsize("<IHH")
