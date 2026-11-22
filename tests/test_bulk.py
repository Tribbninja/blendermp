"""Per-peer bulk encoding: LAN gets large frames, relay gets chunks."""

import asyncio
import os
import queue

import pytest

import session as session_mod
from core import protocol
from core.protocol import MessageType


class FakeWriter:
    def write(self, data):
        pass

    async def drain(self):
        return None

    def close(self):
        pass

    async def wait_closed(self):
        return None


def _host():
    return session_mod.SessionService(
        inbox=queue.Queue(),
        host=True,
        code="0407Y-00006-GWSSR",
        name="host",
        color=(0.4, 0.8, 1.0, 1.0),
    )


def _peer(peer_id, lan):
    peer = session_mod.Peer(peer_id=peer_id, writer=FakeWriter(), address="127.0.0.1:1", lan=lan)
    peer.authenticated = True
    return peer


def _drain(peer):
    frames = []
    while True:
        try:
            data = peer.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        frames.append(protocol.decode(data))
    return frames


def test_lan_peer_gets_one_frame_and_relay_peer_gets_chunks():
    service = _host()
    lan = _peer("lan", True)
    relay = _peer("relay", False)
    service._peers = {"lan": lan, "relay": relay}
    data = os.urandom(2_000_000)
    sent = service.send_image("tex", "png", data)
    assert sent > 0
    lan_frames = _drain(lan)
    relay_frames = _drain(relay)
    assert len(lan_frames) == 1
    assert lan_frames[0].type == MessageType.IMAGE_ASSET
    assert len(lan_frames[0].payload) > 900 * 1024
    assert relay_frames
    assert all(frame.type == MessageType.CHUNK for frame in relay_frames)
    assert len(relay_frames) >= 2


def test_client_on_lan_sends_one_image_frame():
    service = session_mod.SessionService(
        inbox=queue.Queue(),
        host=False,
        code="0407Y-00006-GWSSR",
        name="client",
        color=(0.4, 0.8, 1.0, 1.0),
    )
    service._using_relay = False
    service.send_image("tex", "png", os.urandom(2_000_000))
    frames = []
    while True:
        try:
            frames.append(service._outbox.get_nowait())
        except queue.Empty:
            break
    assert len(frames) == 1
    assert frames[0].type == MessageType.IMAGE_ASSET


def test_client_on_relay_chunks_a_large_image():
    service = session_mod.SessionService(
        inbox=queue.Queue(),
        host=False,
        code="0407Y-00006-GWSSR",
        name="client",
        color=(0.4, 0.8, 1.0, 1.0),
    )
    service._using_relay = True
    service.send_image("tex", "png", os.urandom(2_000_000))
    frames = []
    while True:
        try:
            frames.append(service._outbox.get_nowait())
        except queue.Empty:
            break
    assert frames
    assert all(frame.type == MessageType.CHUNK for frame in frames)


def test_lan_peer_gets_a_large_object_create_in_one_frame():
    service = _host()
    lan = _peer("lan", True)
    relay = _peer("relay", False)
    service._peers = {"lan": lan, "relay": relay}
    geometry = os.urandom(2_000_000)
    assert service.send_object_create("11111111-1111-1111-1111-111111111111", 1, {"name": "Cube"}, geometry)
    lan_frames = _drain(lan)
    relay_frames = _drain(relay)
    assert len(lan_frames) == 1
    assert lan_frames[0].type == MessageType.OBJECT_CREATE
    assert all(frame.type == MessageType.CHUNK for frame in relay_frames)
