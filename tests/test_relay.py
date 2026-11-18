import asyncio
import queue
import threading
import time

import pytest

import session as session_mod
from core.roomcode import FLAG_RELAY, encode_room
from net.relay import CLOSE, DATA, HEADER, OPEN
from server.relay import RelayServer


def wait_for(predicate, timeout=12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


async def read_frame(reader):
    header = await asyncio.wait_for(reader.readexactly(HEADER.size), 5)
    type_, stream, length = HEADER.unpack(header)
    payload = await reader.readexactly(length) if length else b""
    return type_, stream, payload


def test_relay_multiplexes_two_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    async def scenario():
        relay = RelayServer("127.0.0.1", 0)
        port = await relay.start()
        room = "0123456789abcdef"
        try:
            host_reader, host_writer = await asyncio.open_connection("127.0.0.1", port)
            host_writer.write(f"BMPRELAY 1 HOST {room}\n".encode())
            await host_writer.drain()
            assert await host_reader.readline() == b"OK\n"

            client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
            client_writer.write(f"BMPRELAY 1 JOIN {room}\n".encode())
            await client_writer.drain()
            assert await client_reader.readline() == b"OK\n"

            type_, stream, _ = await read_frame(host_reader)
            assert type_ == OPEN

            client_writer.write(b"hello")
            await client_writer.drain()
            type_, stream, payload = await read_frame(host_reader)
            assert (type_, payload) == (DATA, b"hello")

            host_writer.write(HEADER.pack(DATA, stream, 5) + b"world")
            await host_writer.drain()
            assert await asyncio.wait_for(client_reader.readexactly(5), 5) == b"world"

            client_writer.close()
            await client_writer.wait_closed()
            host_writer.close()
            await host_writer.wait_closed()
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_session_chat_over_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    relay = RelayServer("127.0.0.1", 0)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    relay_port = asyncio.run_coroutine_threadsafe(relay.start(), loop).result(timeout=10)
    code = encode_room("127.0.0.1", relay_port, FLAG_RELAY)

    host_events = queue.Queue()
    client_events = queue.Queue()
    host = session_mod.SessionService(
        host_events,
        host=True,
        code=code,
        name="relayhost",
        color=(0.4, 0.9, 1.0, 1.0),
        port=0,
        relay=("127.0.0.1", relay_port),
    )
    host.start()
    try:
        assert wait_for(lambda: host.state == "hosting"), "relay host did not start"
        client = session_mod.SessionService(
            client_events, host=False, code=code, name="relayclient", color=(0.9, 0.4, 1.0, 1.0)
        )
        client.start()
        try:
            assert wait_for(lambda: client.state == "connected"), client.failure_reason
            client.send_chat("through the relay")
            deadline = time.monotonic() + 8
            found = False
            while time.monotonic() < deadline and not found:
                try:
                    kind, body = host_events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "chat" and body.get("text") == "through the relay":
                    found = True
            assert found, "chat did not traverse the relay"
        finally:
            client.stop()
    finally:
        host.stop()
        asyncio.run_coroutine_threadsafe(relay.stop(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)


def test_relay_rejects_unknown_room(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    async def scenario():
        relay = RelayServer("127.0.0.1", 0)
        port = await relay.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"BMPRELAY 1 JOIN 0123456789abcdef\n")
            await writer.drain()
            line = await reader.readline()
            assert line.startswith(b"ERR")
            writer.close()
            await writer.wait_closed()
        finally:
            await relay.stop()

    asyncio.run(scenario())

def test_relay_password_and_kick(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    relay = RelayServer("127.0.0.1", 0)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    relay_port = asyncio.run_coroutine_threadsafe(relay.start(), loop).result(timeout=10)
    code = encode_room("127.0.0.1", relay_port, FLAG_RELAY)

    host_events = queue.Queue()
    client_events = queue.Queue()
    bad_events = queue.Queue()
    host = session_mod.SessionService(
        host_events,
        host=True,
        code=code,
        name="relayhost",
        color=(0.4, 0.9, 1.0, 1.0),
        port=0,
        relay=("127.0.0.1", relay_port),
        pin="secret",
    )
    host.start()
    try:
        assert wait_for(lambda: host.state == "hosting"), "relay host did not start"

        bad = session_mod.SessionService(
            bad_events, host=False, code=code, name="eve", color=(1, 0.4, 0.4, 1.0), pin="wrong"
        )
        bad.start()
        assert wait_for(lambda: bad.failed), "wrong password was not rejected"
        bad.stop()

        client = session_mod.SessionService(
            client_events, host=False, code=code, name="ada", color=(0.9, 0.4, 1.0, 1.0), pin="secret"
        )
        client.start()
        try:
            assert wait_for(lambda: client.state == "connected"), client.failure_reason
            host.kick("ada")
            deadline = time.monotonic() + 10
            kicked = False
            while time.monotonic() < deadline and not kicked:
                try:
                    kind, body = client_events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "kicked":
                    kicked = True
            assert kicked, "kick did not travel through the relay"
            assert wait_for(lambda: client.failed), "client did not stop after the kick"
        finally:
            client.stop()
    finally:
        host.stop()
        asyncio.run_coroutine_threadsafe(relay.stop(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
