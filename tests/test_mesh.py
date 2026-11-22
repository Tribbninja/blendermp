"""Mesh transport: handshake, encryption, and connection dedupe."""

import asyncio

import pytest

from net import mesh

pytestmark = pytest.mark.skipif(not mesh.AVAILABLE, reason="cryptography is not installed")


def run_with_timeout(coro, timeout=8.0):
    async def wrapper():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(wrapper())


def test_peer_ids_are_distinct_hex():
    first = mesh.new_peer_id()
    second = mesh.new_peer_id()
    assert first != second
    assert len(first) == 32
    bytes.fromhex(first)


def test_frame_cipher_round_trip_uses_directional_keys():
    key_a = b"\x01" * 32
    key_b = b"\x02" * 32
    sender = mesh.FrameCipher(key_a, key_b)
    receiver = mesh.FrameCipher(key_b, key_a)
    packet = sender.encrypt(b"hello mesh")
    assert receiver.decrypt(packet) == b"hello mesh"


def test_frame_cipher_rejects_replay():
    sender = mesh.FrameCipher(b"\x01" * 32, b"\x02" * 32)
    receiver = mesh.FrameCipher(b"\x02" * 32, b"\x01" * 32)
    packet = sender.encrypt(b"once")
    assert receiver.decrypt(packet) == b"once"
    with pytest.raises(mesh.MeshError):
        receiver.decrypt(packet)


def test_frame_cipher_rejects_tampering():
    sender = mesh.FrameCipher(b"\x01" * 32, b"\x02" * 32)
    receiver = mesh.FrameCipher(b"\x02" * 32, b"\x01" * 32)
    packet = bytearray(sender.encrypt(b"secret"))
    packet[-1] ^= 0xFF
    with pytest.raises(mesh.MeshError):
        receiver.decrypt(bytes(packet))


def test_connect_and_send_between_two_managers():
    async def wait_for(manager, peer_id, timeout=2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if manager.peer(peer_id) is not None:
                return True
            await asyncio.sleep(0.02)
        return False

    async def scenario():
        host = mesh.MeshManager()
        guest = mesh.MeshManager()
        await host.start()
        await guest.start()
        try:
            ok = await guest.connect(host.peer_id, f"127.0.0.1:{host.bound_port}")
            assert ok
            assert await wait_for(host, guest.peer_id)
            assert guest.peer(host.peer_id) is not None
            await guest.send(host.peer_id, b"from guest")
            peer_id, payload = await asyncio.wait_for(host.receive(), 2)
            assert peer_id == guest.peer_id
            assert payload == b"from guest"
            await host.send(guest.peer_id, b"from host")
            peer_id, payload = await asyncio.wait_for(guest.receive(), 2)
            assert peer_id == host.peer_id
            assert payload == b"from host"
        finally:
            await guest.stop()
            await host.stop()

    run_with_timeout(scenario())


def test_bidirectional_dial_keeps_one_link():
    async def wait_for(manager, peer_id, timeout=2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if manager.peer(peer_id) is not None:
                return True
            await asyncio.sleep(0.02)
        return False

    async def scenario():
        first = mesh.MeshManager()
        second = mesh.MeshManager()
        await first.start()
        await second.start()
        try:
            results = await asyncio.gather(
                first.connect(second.peer_id, f"127.0.0.1:{second.bound_port}"),
                second.connect(first.peer_id, f"127.0.0.1:{first.bound_port}"),
            )
            assert any(results)
            assert await wait_for(first, second.peer_id)
            assert await wait_for(second, first.peer_id)
            await asyncio.sleep(0.2)
            assert len(first.peers()) == 1
            assert len(second.peers()) == 1
            # Both sides must agree on the same surviving link, so a message
            # still crosses.
            assert await first.send(second.peer_id, b"ping")
            peer_id, payload = await asyncio.wait_for(second.receive(), 2)
            assert peer_id == first.peer_id
            assert payload == b"ping"
        finally:
            await second.stop()
            await first.stop()

    run_with_timeout(scenario())


def test_connect_to_dead_address_fails_without_raising():
    async def scenario():
        manager = mesh.MeshManager()
        await manager.start()
        try:
            ok = await manager.connect("deadbeef" * 4, "127.0.0.1:1", timeout=0.3)
            assert ok is False
        finally:
            await manager.stop()

    run_with_timeout(scenario())
