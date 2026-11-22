"""Mesh coordinator: signalling handling and direct dialling."""

import asyncio

import pytest

from net import mesh, meshcoord

pytestmark = pytest.mark.skipif(not mesh.AVAILABLE, reason="cryptography is not installed")


class FakeSignal:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()

    async def connect(self, timeout=10.0):
        return {"type": "welcome", "peers": []}

    async def send(self, message):
        self.sent.append(message)

    async def receive(self, timeout=None):
        return await self.incoming.get()

    async def close(self):
        pass


def _coordinator(monkeypatch, endpoint="203.0.113.5:5000"):
    fake = FakeSignal()

    async def fake_discover(servers=None, timeout=3.0, local_port=0):
        host, _, port = endpoint.partition(":")
        return host, int(port)

    monkeypatch.setattr(meshcoord.stun, "discover", fake_discover)
    monkeypatch.setattr(meshcoord, "SignalClient", lambda *args, **kwargs: fake)
    return meshcoord.MeshCoordinator("ws://127.0.0.1:1", "room", "a" * 32), fake


def test_coordinator_announces_its_address(monkeypatch):
    async def scenario():
        coord, fake = _coordinator(monkeypatch)
        try:
            assert await coord.start()
            assert fake.sent
            hello = fake.sent[0]
            assert hello["type"] == "hello"
            assert hello["address"].startswith("203.0.113.5:")
            assert len(hello["key"]) == 64
        finally:
            await coord.stop()

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_coordinator_dials_a_peer_from_its_hello(monkeypatch):
    async def scenario():
        coord, fake = _coordinator(monkeypatch)
        other = mesh.MeshManager()
        await other.start()
        try:
            assert await coord.start()
            hello = {
                "type": "hello",
                "peer": other.peer_id,
                "address": f"127.0.0.1:{other.bound_port}",
                "key": other.public_key.hex(),
            }
            await coord._handle(hello)
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if other.peer(coord.manager.peer_id) is not None:
                    break
                await asyncio.sleep(0.02)
            assert coord.peer_ids() == [other.peer_id]
            assert await coord.send(other.peer_id, b"hi")
            peer_id, payload = await asyncio.wait_for(other.receive(), 2)
            assert peer_id == coord.manager.peer_id
            assert payload == b"hi"
        finally:
            await coord.stop()
            await other.stop()

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_coordinator_ignores_its_own_hello(monkeypatch):
    async def scenario():
        coord, _fake = _coordinator(monkeypatch)
        try:
            assert await coord.start()
            await coord._handle({"type": "hello", "peer": coord.manager.peer_id})
            assert coord.peer_ids() == []
        finally:
            await coord.stop()

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_coordinator_status_reports_the_listener(monkeypatch):
    async def scenario():
        coord, _fake = _coordinator(monkeypatch)
        try:
            assert await coord.start()
            status = coord.status()
            assert status["peer_id"] == coord.manager.peer_id
            assert status["public_address"].startswith("203.0.113.5:")
            assert status["error"] == ""
        finally:
            await coord.stop()

    asyncio.run(asyncio.wait_for(scenario(), 8))
