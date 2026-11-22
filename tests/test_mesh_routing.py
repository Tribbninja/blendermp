"""Mesh data routing: peer transport choice and mesh identity exchange."""

import asyncio
import json

import pytest

import session as session_mod
from core import protocol
from core.protocol import Frame, MessageType


class FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class FakeMesh:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send(self, peer_id, payload):
        if not self.ok:
            return False
        self.sent.append((peer_id, payload))
        return True


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_peer_drain_writes_prefers_the_mesh():
    async def scenario():
        writer = FakeWriter()
        mesh = FakeMesh()
        peer = session_mod.Peer(
            peer_id="p1",
            writer=writer,
            address="127.0.0.1:1",
            mesh=mesh,
            mesh_id="m" * 32,
            bootstrap_done=True,
        )
        await peer.queue.put(b"frame-a")
        await peer.queue.put(b"frame-b")
        task = asyncio.create_task(peer.drain_writes())
        await asyncio.sleep(0.1)
        peer.closed = True
        await peer.queue.put(b"stop")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert mesh.sent == [("m" * 32, b"frame-a"), ("m" * 32, b"frame-b")]
        assert writer.data == b""

    _run(scenario())


def test_peer_drain_writes_falls_back_to_tls_when_the_mesh_fails():
    async def scenario():
        writer = FakeWriter()
        mesh = FakeMesh(ok=False)
        peer = session_mod.Peer(
            peer_id="p1",
            writer=writer,
            address="127.0.0.1:1",
            mesh=mesh,
            mesh_id="m" * 32,
        )
        await peer.queue.put(b"frame-a")
        task = asyncio.create_task(peer.drain_writes())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert writer.data == b"frame-a"

    _run(scenario())


def test_host_mesh_frame_records_the_peer_id():
    async def scenario():
        service = session_mod.SessionService(
            inbox=__import__("queue").Queue(),
            host=True,
            code="0407Y-00006-GWSSR",
            name="host",
            color=(0.4, 0.8, 1.0, 1.0),
        )
        service._mesh = FakeMesh()
        service._mesh.manager = type("M", (), {"peer_id": "h" * 32})()
        writer = FakeWriter()
        peer = session_mod.Peer(peer_id="p1", writer=writer, address="127.0.0.1:1")
        peer.authenticated = True
        frame = protocol.json_frame(MessageType.MESH, {"peer_id": "c" * 32})
        await service._handle_host_frame(peer, frame)
        assert peer.mesh_id == "c" * 32
        # The host acknowledges with its own id and the client's id.
        data = peer.queue.get_nowait()
        decoded = protocol.decode(data)
        assert decoded.type == MessageType.MESH
        body = json.loads(decoded.payload)
        assert body["peer_id"] == "h" * 32
        assert body["ack"] == "c" * 32

    _run(scenario())


def test_client_mesh_frame_sets_host_id_and_readiness():
    service = session_mod.SessionService(
        inbox=__import__("queue").Queue(),
        host=False,
        code="0407Y-00006-GWSSR",
        name="client",
        color=(0.4, 0.8, 1.0, 1.0),
    )
    service._mesh = FakeMesh()
    service._mesh.manager = type("M", (), {"peer_id": "c" * 32})()
    frame = protocol.json_frame(MessageType.MESH, {"peer_id": "h" * 32, "ack": "c" * 32})
    service._handle_client_frame(frame)
    assert service._host_mesh_id == "h" * 32
    assert service._mesh_ready is True


def test_client_mesh_frame_without_matching_ack_stays_unready():
    service = session_mod.SessionService(
        inbox=__import__("queue").Queue(),
        host=False,
        code="0407Y-00006-GWSSR",
        name="client",
        color=(0.4, 0.8, 1.0, 1.0),
    )
    service._mesh = FakeMesh()
    service._mesh.manager = type("M", (), {"peer_id": "c" * 32})()
    frame = protocol.json_frame(MessageType.MESH, {"peer_id": "h" * 32, "ack": "x" * 32})
    service._handle_client_frame(frame)
    assert service._host_mesh_id == "h" * 32
    assert service._mesh_ready is False
