"""Mesh coordinator: ties signalling, STUN and the mesh transport together.

One coordinator runs per session. It:

1. starts a local mesh listener;
2. discovers its public address with STUN;
3. connects to the Worker signalling socket and announces that address;
4. dials every peer it hears about and keeps the direct links alive.

If a direct link cannot be made (symmetric NAT, CGNAT, a closed port) the peer
simply has no mesh link and the session falls back to the relay for that peer.
The coordinator never blocks the session: everything it does is best-effort.
"""

from __future__ import annotations

import asyncio
import time

from . import stun
from .mesh import MeshError, MeshManager
from .signal import SignalClient, SignalError

RECONNECT_DELAY = 2.0
DIAL_TIMEOUT = 4.0
HELLO_INTERVAL = 15.0


class MeshCoordinator:
    def __init__(
        self,
        base_url: str,
        room: str,
        peer_id: str | None = None,
        listen_port: int = 0,
    ):
        self.base_url = base_url
        self.room = room
        self.peer_id = peer_id
        self.listen_port = listen_port
        self.manager: MeshManager | None = None
        self.signal: SignalClient | None = None
        self.public_address = ""
        self.error = ""
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._hellos: dict[str, float] = {}

    async def start(self, timeout: float = 10.0) -> bool:
        try:
            self.manager = MeshManager(self.peer_id, self.listen_port)
        except MeshError as exc:
            self.error = str(exc)
            return False
        await self.manager.start()
        try:
            endpoint = await stun.discover()
        except Exception:  # noqa: BLE001
            endpoint = None
        if endpoint is not None:
            # STUN reports the UDP mapping. The mesh listens on TCP, so pair the
            # public IP with the local TCP port. Cone NATs usually preserve the
            # port; when they do not, the dial fails and the relay is used.
            self.public_address = f"{endpoint[0]}:{self.manager.bound_port}"
        self.manager.public_address = self.public_address
        self.signal = SignalClient(self.base_url, self.room, self.manager.peer_id)
        try:
            await self.signal.connect(timeout=timeout)
        except (SignalError, OSError) as exc:
            self.error = str(exc)
            await self.manager.stop()
            self.manager = None
            return False
        await self._announce()
        self._tasks.append(asyncio.create_task(self._receive_loop()))
        self._tasks.append(asyncio.create_task(self._hello_loop()))
        return True

    async def stop(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self.signal is not None:
            try:
                await self.signal.close()
            except Exception:  # noqa: BLE001
                pass
            self.signal = None
        if self.manager is not None:
            await self.manager.stop()
            self.manager = None

    # -------------------------------------------------------------- signalling
    async def _announce(self) -> None:
        if self.signal is None or self.manager is None:
            return
        await self.signal.send(self.manager.hello())

    async def _hello_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(HELLO_INTERVAL)
                await self._announce()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                return

    async def _receive_loop(self) -> None:
        while not self._closed:
            try:
                message = await self.signal.receive()
            except asyncio.CancelledError:
                return
            except (SignalError, OSError):
                return
            try:
                await self._handle(message)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                continue

    async def _handle(self, message: dict) -> None:
        if self.manager is None:
            return
        kind = message.get("type")
        peer = message.get("peer")
        if not isinstance(peer, str) or peer == self.manager.peer_id:
            return
        if kind == "leave":
            self._hellos.pop(peer, None)
            return
        if kind == "hello":
            self.manager.apply_hello(peer, message)
            self._hellos[peer] = time.monotonic()
            await self._dial(peer)
        elif kind == "join":
            # Wait for the peer's hello before dialling; it carries the address.
            self._hellos.setdefault(peer, 0.0)

    async def _dial(self, peer: str) -> None:
        if self.manager is None:
            return
        target = self.manager.peer(peer)
        if target is None or not target.address:
            return
        if target.ready:
            return
        await self.manager.connect(peer, target.address, timeout=DIAL_TIMEOUT)

    # ------------------------------------------------------------------- data
    async def send(self, peer: str, payload: bytes) -> bool:
        if self.manager is None:
            return False
        return await self.manager.send(peer, payload)

    async def broadcast(self, payload: bytes) -> int:
        if self.manager is None:
            return 0
        return await self.manager.broadcast(payload)

    async def receive(self) -> tuple[str, bytes]:
        if self.manager is None:
            raise MeshError("mesh coordinator is not running")
        return await self.manager.receive()

    def peer_ids(self) -> list[str]:
        if self.manager is None:
            return []
        return [peer.peer_id for peer in self.manager.peers()]

    def status(self) -> dict:
        return {
            "peer_id": self.manager.peer_id if self.manager else "",
            "public_address": self.public_address,
            "peers": self.peer_ids(),
            "error": self.error,
        }
