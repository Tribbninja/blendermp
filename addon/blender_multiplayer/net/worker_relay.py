"""Bridge between the existing local relay protocol and a Worker WebSocket hub.

The host side speaks the normal BMPRELAY frame protocol to this bridge on
127.0.0.1 and the bridge forwards whole frames over one WebSocket. Client
bridges carry raw TLS bytes in both directions; the Worker wraps them in DATA
frames for the host.
"""

from __future__ import annotations

import asyncio
import struct
import threading
from typing import Callable

from .websocket import WebSocketError, connect

HEADER = struct.Struct("<BII")
MAX_FRAME = 8 * 1024 * 1024


class WorkerBridge:
    def __init__(self, role: str, base_url: str, room: str, token: str | None = None):
        if role not in {"host", "join"}:
            raise ValueError("role must be host or join")
        if not base_url.startswith(("ws://", "wss://")):
            raise ValueError("relay URL must start with ws:// or wss://")
        self.role = role
        self.base_url = base_url.rstrip("/")
        self.room = room
        self.token = (token or "").strip()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.error: Exception | None = None
        self._ready = threading.Event()
        self._writers: set[asyncio.StreamWriter] = set()

    def start(self, timeout: float = 8.0) -> int:
        self.thread = threading.Thread(target=self._run, name="bmp-worker-relay", daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("worker relay bridge did not start")
        if self.error is not None:
            raise RuntimeError(f"worker relay bridge failed: {self.error}")
        return self.port

    def stop(self) -> None:
        loop = self.loop
        if loop is None or not loop.is_running():
            return

        async def close() -> None:
            if self.server is not None:
                self.server.close()
                try:
                    await self.server.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
            for writer in list(self._writers):
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            future = asyncio.run_coroutine_threadsafe(close(), loop)
            future.result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:  # noqa: BLE001
            pass
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
            self._ready.set()
            self.loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve(self) -> None:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        sockets = self.server.sockets or []
        if not sockets:
            raise RuntimeError("no local socket")
        self.port = sockets[0].getsockname()[1]

    def _url(self) -> str:
        base = self.base_url
        if base.endswith("/relay"):
            base = base[: -len("/relay")]
        return f"{base}/relay/{self.room}?role={self.role}"

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        websocket = None
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
            except Exception:
                return
            if not line.startswith(b"BMPRELAY 1 "):
                return
            try:
                headers = {"X-BMP-Host-Token": self.token} if self.role == "host" and self.token else None
                if self.role == "host":
                    print(f"BMP relay host connecting room={self.room}", flush=True)
                websocket = await self._connect_with_retry(headers)
            except WebSocketError as exc:
                print(f"BMP relay {self.role} failed: {exc}", flush=True)
                writer.write(f"ERR {str(exc)[:150]}\n".encode("utf-8", "replace"))
                await writer.drain()
                return
            writer.write(b"OK\n")
            await writer.drain()
            if self.role == "host":
                await self._pump_host(reader, writer, websocket)
            else:
                await self._pump_join(reader, writer, websocket)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if websocket is not None:
                await websocket.close()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writers.discard(writer)

    async def _connect_with_retry(self, headers):
        """Retry a host connection while the room token is being announced.

        The desktop app announces the host token just after launching Blender.
        The relay can be reachable a fraction of a second earlier, so a 403
        here usually means "the token is not in the Room yet", not "bad token".
        """
        last_error = None
        attempts = 12 if self.role == "host" else 1
        for attempt in range(attempts):
            try:
                return await connect(self._url(), headers=headers)
            except WebSocketError as exc:
                last_error = exc
                text = str(exc)
                if "403" not in text or attempt == attempts - 1:
                    raise
                await asyncio.sleep(0.5)
        raise last_error or WebSocketError("relay connection failed")

    async def _pump_host(self, reader, writer, websocket) -> None:
        async def local_to_hub() -> None:
            while True:
                header = await reader.readexactly(HEADER.size)
                type_, stream, length = HEADER.unpack(header)
                if length > MAX_FRAME:
                    raise WebSocketError("relay frame too large")
                payload = await reader.readexactly(length) if length else b""
                await websocket.send_bytes(header + payload)

        async def hub_to_local() -> None:
            while True:
                data = await websocket.recv_bytes()
                writer.write(data)
                await writer.drain()

        async def keepalive() -> None:
            # Cloudflare closes idle WebSockets. A tiny ignored frame keeps the
            # host's room alive between joins, so a friend does not land during
            # a reconnect window and get dropped.
            while True:
                await asyncio.sleep(20.0)
                await websocket.send_bytes(HEADER.pack(0, 0, 0))

        await self._race(local_to_hub(), hub_to_local(), keepalive())

    async def _pump_join(self, reader, writer, websocket) -> None:
        async def local_to_hub() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await websocket.send_bytes(data)

        async def hub_to_local() -> None:
            while True:
                data = await websocket.recv_bytes()
                writer.write(data)
                await writer.drain()

        await self._race(local_to_hub(), hub_to_local())

    async def _race(self, *coros) -> None:
        tasks = [asyncio.create_task(coro) for coro in coros]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    task.result()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
