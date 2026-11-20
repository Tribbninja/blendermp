"""Minimal asyncio WebSocket client (RFC 6455).

Blender does not ship the ``websockets`` package, so the Worker relay uses this
small client. It handles binary frames, ping/pong, close and fragmented
messages. No extensions or compression are requested.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 8 * 1024 * 1024


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def send_bytes(self, payload: bytes) -> None:
        if self._closed:
            raise WebSocketError("websocket is closed")
        if len(payload) > MAX_FRAME:
            raise WebSocketError("frame too large")
        length = len(payload)
        header = bytearray([0x82])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        async with self._send_lock:
            self.writer.write(bytes(header) + masked)
            await self.writer.drain()

    async def _control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        header.extend(mask)
        body = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        async with self._send_lock:
            self.writer.write(bytes(header) + body)
            await self.writer.drain()

    async def recv_bytes(self) -> bytes:
        fragments = bytearray()
        while True:
            try:
                first, second = await self.reader.readexactly(2)
            except asyncio.IncompleteReadError as exc:
                self._closed = True
                raise WebSocketError("connection closed") from exc
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length > MAX_FRAME:
                raise WebSocketError("frame too large")
            mask = await self.reader.readexactly(4) if masked else b""
            payload = await self.reader.readexactly(length)
            if masked:
                payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))

            if opcode == 0x9:  # ping
                await self._control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                try:
                    await self._control(0x8, payload[:125])
                except Exception:  # noqa: BLE001
                    pass
                self._closed = True
                raise WebSocketError("connection closed")
            if opcode == 0x1:
                if not final:
                    raise WebSocketError("fragmented text is not supported")
                raise WebSocketError("unexpected text frame")
            if opcode == 0x2:
                if final:
                    return bytes(payload)
                fragments.extend(payload)
                continue
            if opcode == 0x0:
                fragments.extend(payload)
                if final:
                    return bytes(fragments)
                continue
            raise WebSocketError(f"unsupported opcode {opcode}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._control(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def connect(url: str, timeout: float = 10.0) -> WebSocket:
    parts = urlsplit(url)
    if parts.scheme not in {"ws", "wss"}:
        raise WebSocketError("relay URL must start with ws:// or wss://")
    host = parts.hostname
    if not host:
        raise WebSocketError("relay URL has no host")
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    context = ssl.create_default_context() if parts.scheme == "wss" else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=host if context else None),
            timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise WebSocketError(f"cannot reach hub: {exc}") from exc

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    host_header = host if port in (80, 443) else f"{host}:{port}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {'https' if context else 'http'}://{host_header}\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    try:
        header_blob = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except Exception as exc:  # noqa: BLE001
        writer.close()
        raise WebSocketError(f"hub handshake failed: {exc}") from exc

    lines = header_blob.decode("utf-8", "replace").split("\r\n")
    if not lines or " 101 " not in f" {lines[0]} ":
        writer.close()
        raise WebSocketError(f"hub rejected the websocket: {lines[0] if lines else 'no response'}")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        writer.close()
        raise WebSocketError("hub websocket accept key did not match")
    return WebSocket(reader, writer)
