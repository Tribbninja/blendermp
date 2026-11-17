"""Client-side agents for the optional self-hosted relay.

The relay forwards bytes, not trust: the TLS session remains between the client
and the host. The host keeps one outbound tunnel to the relay, so no port
forwarding or NAT traversal is needed on either side.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable

OPEN = 1
DATA = 2
CLOSE = 3
HEADER = struct.Struct("<BII")
MAX_FRAME = 256 * 1024
HANDSHAKE_TIMEOUT = 8.0
RETRY_DELAY = 2.0


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, int, bytes] | None:
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    type_, stream, length = HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ValueError("relay frame too large")
    payload = await reader.readexactly(length) if length else b""
    return type_, stream, payload


class _TunnelWriter:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.lock = asyncio.Lock()
        self.closed = False

    async def send(self, type_: int, stream: int = 0, payload: bytes = b"") -> None:
        if self.closed:
            return
        if len(payload) > MAX_FRAME:
            raise ValueError("relay frame too large")
        data = HEADER.pack(type_, stream, len(payload)) + payload
        async with self.lock:
            self.writer.write(data)
            await self.writer.drain()

    def close(self) -> None:
        self.closed = True
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class _LocalPipe:
    def __init__(self, stream_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tunnel: _TunnelWriter):
        self.stream_id = stream_id
        self.reader = reader
        self.writer = writer
        self.tunnel = tunnel
        self.closed = False
        self.task: asyncio.Task | None = None

    async def feed(self, data: bytes) -> None:
        if self.closed:
            return
        self.writer.write(data)
        await self.writer.drain()

    async def pump(self) -> None:
        try:
            while not self.closed:
                data = await self.reader.read(65536)
                if not data:
                    break
                await self.tunnel.send(DATA, self.stream_id, data)
        except (ConnectionError, ValueError):
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.tunnel.send(CLOSE, self.stream_id)
        except (ConnectionError, ValueError):
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


class RelayHostAgent:
    """Keeps the host tunnel registered and bridges streams to the local TLS port."""

    def __init__(self, relay: tuple[str, int], room: str, tls_port: int, on_event: Callable[[str, str], None] | None = None):
        self.relay = relay
        self.room = room
        self.tls_port = tls_port
        self.on_event = on_event or (lambda kind, text: None)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._writer: _TunnelWriter | None = None
        self._pipes: dict[int, _LocalPipe] = {}
        self._pending: dict[int, asyncio.Queue] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._writer:
            self._writer.close()
        for pipe in list(self._pipes.values()):
            await pipe.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(*self.relay)
                writer.write(f"BMPRELAY 1 HOST {self.room}\n".encode("ascii"))
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
                if not line.startswith(b"OK"):
                    raise ConnectionError(line.decode("ascii", "replace").strip() or "relay rejected the tunnel")
                tunnel = _TunnelWriter(writer)
                self._writer = tunnel
                self.on_event("relay", f"tunnel to {self.relay[0]}:{self.relay[1]} up")
                await self._serve(reader, tunnel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.on_event("relay_error", str(exc)[:200])
            finally:
                if self._writer:
                    self._writer.close()
                    self._writer = None
                for pipe in list(self._pipes.values()):
                    await pipe.close()
                self._pipes.clear()
                self._pending.clear()
            if not self._stop.is_set():
                await asyncio.sleep(RETRY_DELAY)

    async def _serve(self, reader: asyncio.StreamReader, tunnel: _TunnelWriter) -> None:
        while not self._stop.is_set():
            frame = await _read_frame(reader)
            if frame is None:
                return
            type_, stream_id, payload = frame
            if type_ == OPEN:
                if stream_id not in self._pending and stream_id not in self._pipes:
                    queue: asyncio.Queue = asyncio.Queue()
                    self._pending[stream_id] = queue
                    asyncio.create_task(self._connect_local(stream_id, tunnel, queue))
            elif type_ == DATA:
                pipe = self._pipes.get(stream_id)
                if pipe is not None:
                    await pipe.feed(payload)
                else:
                    queue = self._pending.get(stream_id)
                    if queue is not None:
                        queue.put_nowait(payload)
            elif type_ == CLOSE:
                pipe = self._pipes.pop(stream_id, None)
                if pipe is not None:
                    await pipe.close()
                self._pending.pop(stream_id, None)

    async def _connect_local(self, stream_id: int, tunnel: _TunnelWriter, queue: asyncio.Queue) -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.tls_port)
        except OSError as exc:
            self._pending.pop(stream_id, None)
            self.on_event("relay_error", f"local TLS connect failed: {exc}")
            try:
                await tunnel.send(CLOSE, stream_id)
            except Exception:  # noqa: BLE001
                pass
            return
        pipe = _LocalPipe(stream_id, reader, writer, tunnel)
        self._pending.pop(stream_id, None)
        self._pipes[stream_id] = pipe
        try:
            while not queue.empty():
                await pipe.feed(queue.get_nowait())
        except (ConnectionError, ValueError):
            await pipe.close()
            return
        pipe.task = asyncio.create_task(pipe.pump())


class RelayClientAgent:
    """Local TCP endpoint that forwards bytes to the host through the relay."""

    def __init__(self, relay: tuple[str, int], room: str):
        self.relay = relay
        self.room = room
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1] if sockets else 0
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _accept(self, local_reader: asyncio.StreamReader, local_writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        writer = None
        try:
            reader, writer = await asyncio.open_connection(*self.relay)
            writer.write(f"BMPRELAY 1 JOIN {self.room}\n".encode("ascii"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
            if not line.startswith(b"OK"):
                raise ConnectionError(line.decode("ascii", "replace").strip() or "relay has no host")
            await asyncio.gather(
                self._local_to_relay(local_reader, writer),
                self._relay_to_local(reader, local_writer),
            )
        except Exception:  # noqa: BLE001
            pass
        finally:
            for stream in (local_writer, writer):
                if stream is not None:
                    try:
                        stream.close()
                        await stream.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass
            if task is not None:
                self._tasks.discard(task)

    @staticmethod
    async def _local_to_relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _relay_to_local(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
