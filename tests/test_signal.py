"""Signalling client: URL construction and message guards."""

import asyncio

import pytest

from net import signal


def test_signal_url_appends_peer_and_room():
    url = signal.signal_url("wss://hub.example.workers.dev", "abc123", "peer-1")
    assert url == "wss://hub.example.workers.dev/signal/abc123?peer=peer-1"


def test_signal_url_strips_a_trailing_relay_path():
    url = signal.signal_url("wss://hub.example.workers.dev/relay/", "abc123", "peer-1")
    assert url == "wss://hub.example.workers.dev/signal/abc123?peer=peer-1"


def test_signal_client_rejects_a_non_websocket_url():
    with pytest.raises(ValueError):
        signal.SignalClient("https://hub.example.workers.dev", "abc123", "peer-1")


def test_send_without_a_connection_raises():
    client = signal.SignalClient("ws://127.0.0.1:1", "abc123", "peer-1")
    with pytest.raises(signal.SignalError):
        asyncio.run(client.send({"type": "hello"}))


def test_send_rejects_an_oversized_message():
    class DummySocket:
        async def send_bytes(self, payload):
            raise AssertionError("should not send")

    client = signal.SignalClient("ws://127.0.0.1:1", "abc123", "peer-1")
    client.websocket = DummySocket()
    with pytest.raises(signal.SignalError):
        asyncio.run(client.send({"type": "hello", "address": "x" * signal.MAX_MESSAGE}))
