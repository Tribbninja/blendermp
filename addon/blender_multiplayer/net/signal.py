"""Signalling client for the Worker mesh rendezvous.

The Worker's Room object exposes ``/signal/{room}?peer=<id>`` as a WebSocket
that only forwards small JSON messages between peers. It never carries
collaboration data. This client speaks that protocol and is intentionally thin:
the mesh manager decides what to send.

Messages are UTF-8 JSON in binary WebSocket frames so the same frame reader as
the relay can be reused.
"""

from __future__ import annotations

import asyncio
import json

from .websocket import WebSocket, WebSocketError, connect

MAX_MESSAGE = 2048


class SignalError(Exception):
    pass


def signal_url(base_url: str, room: str, peer: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/relay"):
        base = base[: -len("/relay")]
    return f"{base}/signal/{room}?peer={peer}"


class SignalClient:
    def __init__(self, base_url: str, room: str, peer: str):
        if not base_url.startswith(("ws://", "wss://")):
            raise ValueError("relay URL must start with ws:// or wss://")
        self.base_url = base_url
        self.room = room
        self.peer = peer
        self.websocket: WebSocket | None = None

    async def connect(self, timeout: float = 10.0) -> dict:
        """Open the signalling socket and return the welcome message."""
        self.websocket = await connect(
            signal_url(self.base_url, self.room, self.peer), timeout=timeout
        )
        message = await self.receive(timeout=timeout)
        if message.get("type") != "welcome":
            raise SignalError("signalling server did not send a welcome")
        return message

    async def send(self, message: dict) -> None:
        if self.websocket is None:
            raise SignalError("signalling socket is not connected")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE:
            raise SignalError("signalling message is too large")
        await self.websocket.send_bytes(payload)

    async def receive(self, timeout: float | None = None) -> dict:
        if self.websocket is None:
            raise SignalError("signalling socket is not connected")
        try:
            if timeout is None:
                data = await self.websocket.recv_bytes()
            else:
                data = await asyncio.wait_for(self.websocket.recv_bytes(), timeout)
        except WebSocketError as exc:
            raise SignalError(str(exc)) from exc
        if len(data) > MAX_MESSAGE:
            raise SignalError("signalling message is too large")
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignalError("signalling message is not JSON") from exc
        if not isinstance(message, dict):
            raise SignalError("signalling message is not an object")
        return message

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
