"""UDP transport plus a small reliable layer.

Control traffic must arrive in order; transforms and presence must not queue
behind a lost packet. ``ReliableLink`` gives us selective acks with cumulative
ordering for the first, while callers of the datagram transport can send the
second fire-and-forget.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

LINK_HEADER = struct.Struct("<IHH")
MAX_PACKET = 1200  # stay below a typical LAN/WAN MTU
MAX_WINDOW = 1024


class TransportError(RuntimeError):
    pass


@dataclass(slots=True)
class _Pending:
    seq: int
    payload: bytes
    first_sent: float
    last_sent: float
    tries: int


class ReliableLink:
    """Reliable, ordered delivery over an unreliable datagram sender."""

    def __init__(self, now: Callable[[], float] = time.monotonic, rto: float = 0.2, max_tries: int = 12):
        self._now = now
        self.rto = rto
        self.max_tries = max_tries
        self._next_seq = 0
        self._pending: dict[int, _Pending] = {}
        self._recv_next = 0
        self._seen: set[int] = set()
        self._buffered: dict[int, bytes] = {}
        self._ready: list[bytes] = []

    # -- sending ---------------------------------------------------------
    def send(self, payload: bytes) -> bytes:
        if len(payload) + LINK_HEADER.size > MAX_PACKET:
            raise TransportError("payload exceeds the reliable link MTU")
        seq = self._next_seq
        self._next_seq += 1
        now = self._now()
        packet = self._packet(seq, payload)
        self._pending[seq] = _Pending(seq, payload, now, now, 1)
        return packet

    def _packet(self, seq: int, payload: bytes) -> bytes:
        bits = 0
        for i in range(16):
            if self._recv_next + 1 + i in self._seen:
                bits |= 1 << i
        return LINK_HEADER.pack(seq, self._recv_next, bits) + payload

    def _acknowledge(self, ack: int, bits: int) -> None:
        for seq in [s for s in self._pending if s < ack]:
            del self._pending[seq]
        for i in range(16):
            if bits & (1 << i):
                self._pending.pop(ack + 1 + i, None)

    # -- receiving -------------------------------------------------------
    def on_packet(self, data: bytes) -> tuple[list[bytes], bytes | None]:
        """Feed a packet; return newly delivered payloads and an ack to send."""
        if len(data) < LINK_HEADER.size:
            return [], None
        seq, ack, bits = LINK_HEADER.unpack_from(data)
        self._acknowledge(ack, bits)
        payload = data[LINK_HEADER.size:]

        if payload:
            if seq < self._recv_next:
                pass  # duplicate
            elif seq > self._recv_next + MAX_WINDOW:
                pass  # too far ahead; let the peer retransmit
            else:
                self._seen.add(seq)
                if seq == self._recv_next:
                    self._ready.append(payload)
                    self._recv_next += 1
                    while self._recv_next in self._buffered:
                        self._ready.append(self._buffered.pop(self._recv_next))
                        self._seen.discard(self._recv_next)
                        self._recv_next += 1
                elif seq > self._recv_next:
                    self._buffered[seq] = payload

        reply = self._packet(self._next_seq, b"")
        return self._take_ready(), reply

    def _take_ready(self) -> list[bytes]:
        ready = self._ready
        self._ready = []
        return ready

    # -- timers ----------------------------------------------------------
    def poll(self, now: float | None = None) -> list[bytes]:
        """Return packets that need retransmitting."""
        now = self._now() if now is None else now
        out: list[bytes] = []
        for seq, pending in list(self._pending.items()):
            backoff = self.rto * (1.0 + 0.35 * pending.tries)
            if now - pending.last_sent < backoff:
                continue
            if pending.tries >= self.max_tries:
                del self._pending[seq]
                continue
            pending.tries += 1
            pending.last_sent = now
            out.append(self._packet(seq, pending.payload))
        return out

    @property
    def unacked(self) -> int:
        return len(self._pending)

    @property
    def receive_next(self) -> int:
        return self._recv_next


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data, addr):  # type: ignore[override]
        self.queue.put_nowait((data, addr))

    def error_received(self, exc):  # type: ignore[override]
        self.queue.put_nowait((exc, None))


class UdpEndpoint:
    """Thin asyncio wrapper with an awaitable receive queue."""

    def __init__(self, transport: asyncio.DatagramTransport, queue: asyncio.Queue, protocol: _DatagramProtocol):
        self._transport = transport
        self._queue = queue
        self._protocol = protocol

    @classmethod
    async def create(cls, host: str = "0.0.0.0", port: int = 0, allow_broadcast: bool = False) -> "UdpEndpoint":
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(queue),
            local_addr=(host, port),
            allow_broadcast=allow_broadcast,
        )
        return cls(transport, queue, protocol)

    @property
    def local_address(self) -> tuple[str, int]:
        sock = self._transport.get_extra_info("socket")
        return sock.getsockname()[:2]

    def send(self, data: bytes, addr: tuple[str, int]) -> None:
        self._transport.sendto(data, addr)

    async def recv(self) -> tuple[bytes, tuple[str, int]]:
        data, addr = await self._queue.get()
        if isinstance(data, Exception):
            raise TransportError(str(data))
        return data, addr

    def close(self) -> None:
        self._transport.close()


async def run_until(coro: Awaitable, timeout: float) -> None:
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        pass
