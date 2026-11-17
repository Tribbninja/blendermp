"""Self-hosted relay for cross-network sessions.

The relay is a dumb, multiplexed TCP pipe. Clients keep a TLS 1.3 connection to
the host through it, so the relay never sees plaintext and does not need to be
trusted with scene data.

Protocol
--------
A connection starts with one text line::

    BMPRELAY 1 HOST <room-id>\\n
    BMPRELAY 1 JOIN <room-id>\\n

The server replies ``OK\\n`` and then switches to binary frames::

    u8   type      1=OPEN 2=DATA 3=CLOSE
    u32  stream
    u32  length
    ...  payload (DATA only)

The host keeps one tunnel connection with the relay. Each joining client gets a
new stream id; OPEN and CLOSE bracket the stream, DATA carries bytes in both
directions.

Run it::

    python -m server.relay --host 0.0.0.0 --port 7100
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import time

log = logging.getLogger("blendermp.relay")

OPEN = 1
DATA = 2
CLOSE = 3
HEADER = struct.Struct("<BII")
MAX_FRAME = 256 * 1024
MAX_STREAMS = 32
MAX_ROOMS = 512
HANDSHAKE_TIMEOUT = 8.0
IDLE_TIMEOUT = 300.0
ROOM_LEN = 16


class RelayError(Exception):
    pass


def valid_room(value: str) -> bool:
    return len(value) == ROOM_LEN and all(ch in "0123456789abcdef" for ch in value)


class Tunnel:
    """The persistent host side of a room."""

    def __init__(self, room: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.room = room
        self.reader = reader
        self.writer = writer
        self.streams: dict[int, Stream] = {}
        self._lock = asyncio.Lock()
        self._next = 1
        self.closed = False

    async def send(self, type_: int, stream: int = 0, payload: bytes = b"") -> None:
        if len(payload) > MAX_FRAME:
            raise RelayError("frame too large")
        data = HEADER.pack(type_, stream, len(payload)) + payload
        async with self._lock:
            self.writer.write(data)
            await self.writer.drain()

    def open_stream(self, session: "Stream") -> None:
        if len(self.streams) >= MAX_STREAMS:
            raise RelayError("too many streams")
        stream_id = self._next
        self._next = (self._next % 0xFFFFFFFF) + 1
        session.stream_id = stream_id
        self.streams[stream_id] = session

    async def read_loop(self) -> None:
        try:
            while not self.closed:
                header = await self.reader.readexactly(HEADER.size)
                type_, stream_id, length = HEADER.unpack(header)
                if length > MAX_FRAME:
                    raise RelayError("frame too large")
                payload = await self.reader.readexactly(length) if length else b""
                if type_ == DATA:
                    session = self.streams.get(stream_id)
                    if session is not None:
                        session.deliver(payload)
                elif type_ == CLOSE:
                    session = self.streams.pop(stream_id, None)
                    if session is not None:
                        await session.close(notify=False)
                # OPEN from a host is ignored; the relay owns stream creation.
        except (asyncio.IncompleteReadError, ConnectionError, RelayError):
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for session in list(self.streams.values()):
            await session.close(notify=False)
        self.streams.clear()
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 1.0)
        except Exception:  # noqa: BLE001
            pass


class Stream:
    """One joining client bridged to the host tunnel."""

    def __init__(self, tunnel: Tunnel, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.tunnel = tunnel
        self.reader = reader
        self.writer = writer
        self.stream_id = 0
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.closed = False
        self.created = time.monotonic()

    def deliver(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            asyncio.create_task(self.close(notify=True))

    async def run(self) -> None:
        tunnel = self.tunnel
        tunnel.open_stream(self)
        await tunnel.send(OPEN, self.stream_id, b"")
        tasks = [
            asyncio.create_task(self._client_to_host()),
            asyncio.create_task(self._host_to_client()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await self.close(notify=True)
            tunnel.streams.pop(self.stream_id, None)

    async def _client_to_host(self) -> None:
        try:
            while not self.closed:
                data = await self.reader.read(65536)
                if not data:
                    break
                await self.tunnel.send(DATA, self.stream_id, data)
        except (ConnectionError, RelayError):
            pass
        finally:
            await self.close(notify=True)

    async def _host_to_client(self) -> None:
        try:
            while not self.closed:
                data = await self.queue.get()
                if data is None:
                    break
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, RelayError):
            pass
        finally:
            await self.close(notify=True)

    async def close(self, notify: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(None)
        if notify:
            try:
                await self.tunnel.send(CLOSE, self.stream_id, b"")
            except (RelayError, ConnectionError):
                pass
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 1.0)
        except Exception:  # noqa: BLE001
            pass


class RelayServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 7100):
        self.host = host
        self.port = port
        self.hosts: dict[str, Tunnel] = {}
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for tunnel in list(self.hosts.values()):
            await tunnel.close()
        self.hosts.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
            parts = line.decode("ascii", "replace").strip().split()
            if len(parts) != 4 or parts[0] != "BMPRELAY" or parts[1] != "1":
                raise RelayError("bad handshake")
            role, room = parts[2], parts[3].lower()
            if role not in {"HOST", "JOIN"} or not valid_room(room):
                raise RelayError("bad role or room")

            if role == "HOST":
                if len(self.hosts) >= MAX_ROOMS and room not in self.hosts:
                    raise RelayError("too many rooms")
                old = self.hosts.get(room)
                if old:
                    await old.close()
                writer.write(b"OK\n")
                await writer.drain()
                tunnel = Tunnel(room, reader, writer)
                self.hosts[room] = tunnel
                log.info("host registered room %s from %s", room, peer)
                await tunnel.read_loop()
                if self.hosts.get(room) is tunnel:
                    del self.hosts[room]
                log.info("host left room %s", room)
                return

            tunnel = self.hosts.get(room)
            if tunnel is None or tunnel.closed:
                writer.write(b"ERR no-host\n")
                await writer.drain()
                raise RelayError("no host")

            writer.write(b"OK\n")
            await writer.drain()
            session = Stream(tunnel, reader, writer)
            log.info("join room %s from %s", room, peer)
            await session.run()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, RelayError, ConnectionError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("relay handler failed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def serve(host: str, port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = RelayServer(host, port)
    actual = await server.start()
    log.info("relay listening on %s:%s", host, actual)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blendermp self-hosted relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7100)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        pass
