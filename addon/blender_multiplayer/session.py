"""Host-authoritative TLS session.

Topology is a star: every client connects to the host over TLS 1.3, and the host
validates and relays. Clients never talk to each other directly and never trust
another client's claim about who sent a message.

Security properties enforced here
---------------------------------
* Transport is TLS 1.3 using the stdlib ``ssl`` module. No custom ciphers.
* The client pins the host certificate fingerprint before sending a frame.
* The host issues a per-connection identity, validates every frame, enforces
  message size and rate limits, and owns object leases.
* Names and colours are set by the host from authenticated state, so a client
  cannot impersonate another participant.
* Malformed or abusive input closes the connection; it is never applied.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import queue
import re
import secrets
import struct
import threading
import time
import traceback
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from . import control
    from .core import image_payload
    from .core import material as material_payload
    from .core import object_payload, protocol, tls
    from .core.protocol import HEADER, HEADER_SIZE, MAGIC, PROTOCOL_VERSION, Channel, Frame, MessageType
    from .core.roomcode import FLAG_RELAY, decode_room, room_id
    from .net.relay import RelayClientAgent, RelayHostAgent
    from .sync.leases import LeaseManager
else:  # pragma: no cover - allows importing the module standalone (tests)
    import control
    from core import image_payload
    from core import material as material_payload
    from core import object_payload, protocol, tls
    from core.protocol import HEADER, HEADER_SIZE, MAGIC, PROTOCOL_VERSION, Channel, Frame, MessageType
    from core.roomcode import FLAG_RELAY, decode_room, room_id
    from net.relay import RelayClientAgent, RelayHostAgent
    from sync.leases import LeaseManager

MAX_CONTROL = 64 * 1024
MAX_MESH = 24 * 1024 * 1024
MAX_VERTICES = 5_000_000
MAX_CHAT = 2_000
MAX_NAME = 48
MAX_TEXT = 256
PING_INTERVAL = 2.0
PEER_TIMEOUT = 12.0
DISCONNECT_STRIKES = 6
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Violation(Exception):
    """The peer sent something it is not allowed to send."""


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = time.monotonic()

    def allow(self, cost: float = 1.0, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True


def clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return CONTROL_CHARS.sub("", value).strip()[:limit]


def clean_name(value: Any, fallback: str = "user") -> str:
    name = clean_text(value, MAX_NAME)
    return name or fallback


def clean_color(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return (0.4, 0.8, 1.0, 1.0)
    out = []
    for component in value[:4]:
        try:
            number = float(component)
        except (TypeError, ValueError):
            number = 0.5
        out.append(min(1.0, max(0.0, number if math.isfinite(number) else 0.5)))
    while len(out) < 4:
        out.append(1.0)
    return tuple(out)  # type: ignore[return-value]


def encode_transform(uid: str, matrix) -> bytes:
    values = [float(v) for v in matrix]
    if len(values) != 16 or not all(math.isfinite(v) and abs(v) < 1e12 for v in values):
        raise ValueError("invalid transform")
    return uuidlib.UUID(str(uid)).bytes + struct.pack("<16f", *values)


def decode_transform(payload: bytes) -> tuple[str, list[float]]:
    if len(payload) != 80:
        raise Violation("transform frame must be 80 bytes")
    uid = str(uuidlib.UUID(bytes=payload[:16]))
    values = list(struct.unpack_from("<16f", payload, 16))
    if not all(math.isfinite(v) and abs(v) < 1e12 for v in values):
        raise Violation("transform contains non-finite values")
    return uid, values


def encode_mesh(uid: str, rev: int, payload: bytes) -> bytes:
    if not 0 <= rev < 2**32:
        raise ValueError("mesh revision out of range")
    return uuidlib.UUID(str(uid)).bytes + struct.pack("<I", rev) + payload


def decode_mesh(payload: bytes) -> tuple[str, int, bytes]:
    if len(payload) < 20:
        raise Violation("mesh frame is truncated")
    if len(payload) > MAX_MESH:
        raise Violation("mesh frame is too large")
    try:
        uid = str(uuidlib.UUID(bytes=payload[:16]))
    except ValueError as exc:
        raise Violation("invalid mesh identifier") from exc
    (rev,) = struct.unpack_from("<I", payload, 16)
    return uid, rev, payload[20:]


@dataclass
class Peer:
    peer_id: str
    writer: asyncio.StreamWriter
    address: str
    name: str = "user"
    color: tuple = (0.4, 0.8, 1.0, 1.0)
    authenticated: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    strikes: int = 0
    rtt_ms: float = 0.0
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    writer_task: asyncio.Task | None = None
    closed: bool = False
    sent: int = 0
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(240.0, 480.0))

    def enqueue(self, data: bytes, critical: bool = False) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            if critical:
                raise Violation("peer is not keeping up")
            # Non-critical frames (transforms, presence) are dropped rather than
            # queueing stale state.

    async def drain_writes(self) -> None:
        try:
            while not self.closed:
                data = await self.queue.get()
                if self.closed:
                    break
                self.writer.write(data)
                await self.writer.drain()
                self.sent += len(data)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            pass


class SessionService:
    def __init__(
        self,
        inbox: queue.Queue,
        host: bool,
        code: str,
        name: str,
        color,
        port: int = 0,
        fingerprint: str | None = None,
        pin: str = "",
        relay: tuple[str, int] | None = None,
    ):
        self.inbox = inbox
        self.is_host = host
        self.code = code.strip().upper()
        self.name = clean_name(name)
        self.color = clean_color(color)
        self.port = int(port or 0)
        self.expected_fingerprint = fingerprint
        self.pin = pin[:64]

        self.relay_addr: tuple[str, int] | None = None
        self.relay_mode = False
        try:
            info = decode_room(self.code)
        except Exception:  # noqa: BLE001 - validated later
            info = None
        if relay is not None:
            self.relay_addr = relay
            self.relay_mode = True
        elif info is not None and (info.flags & FLAG_RELAY):
            self.relay_addr = (info.ip, info.port)
            self.relay_mode = True
        self._relay_host: RelayHostAgent | None = None
        self._relay_client: RelayClientAgent | None = None

        self.failed = False
        self.failure_reason = ""
        self.fingerprint = ""
        self.bound_port = 0
        self.state = "offline"

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        self._outbox: queue.Queue = queue.Queue()
        self._server: asyncio.AbstractServer | None = None
        self._peers: dict[str, Peer] = {}
        self._peer_id: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ping_task: asyncio.Task | None = None
        self._leases = LeaseManager(ttl=120.0, grace=20.0)
        self._bytes_in = 0
        self._bytes_out = 0
        self._rtt_ms = 0.0
        self._last_ping = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, name="bmp-session", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _main(self) -> None:
        self._stop_event = asyncio.Event()
        self._wake = asyncio.Event()
        tasks: list[asyncio.Task] = []
        try:
            if self.is_host:
                await self._start_host()
            else:
                if self.relay_mode and self.relay_addr:
                    self._relay_client = RelayClientAgent(self.relay_addr, room_id(self.code))
                    await self._relay_client.start()
                tasks.append(asyncio.create_task(self._client_loop()))
            tasks.append(asyncio.create_task(self._tick_loop()))
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._relay_client:
            try:
                await self._relay_client.stop()
            except Exception:  # noqa: BLE001
                pass
            self._relay_client = None
        if self._relay_host:
            try:
                await self._relay_host.stop()
            except Exception:  # noqa: BLE001
                pass
            self._relay_host = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        for peer in list(self._peers.values()):
            await self._close_peer(peer)
        self._peers.clear()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        self._set_state("offline")

    def stop(self) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._request_stop)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def _request_stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

    # ------------------------------------------------------------------ helpers
    def _emit(self, kind: str, body: dict[str, Any]) -> None:
        self.inbox.put((kind, body))

    def _set_state(self, value: str) -> None:
        self.state = value
        self._emit("state", {"state": value})

    def _fail(self, reason: str) -> None:
        self.failed = True
        self.failure_reason = reason[:200]
        self._emit("error", {"message": self.failure_reason})
        self._request_stop()

    def _enqueue(self, frame: Frame) -> None:
        loop = self._loop
        if loop and loop.is_running():
            self._outbox.put(frame)
            loop.call_soon_threadsafe(self._wake.set)

    # ------------------------------------------------------------------ host
    async def _start_host(self) -> None:
        try:
            certificate, key, fingerprint = tls.load_or_create_certificate(control.certs_dir())
            context = tls.server_context(certificate, key)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"tls setup failed: {exc}")
            return

        self.fingerprint = fingerprint
        if self.relay_mode:
            bind_host = "127.0.0.1"
            port = 0
        else:
            bind_host = "0.0.0.0"
            port = self.port
            if not port:
                try:
                    port = decode_room(self.code).port
                except Exception:  # noqa: BLE001
                    self._fail("invalid room code")
                    return
        try:
            self._server = await asyncio.start_server(
                self._on_client,
                host=bind_host,
                port=port,
                ssl=context,
                backlog=16,
                limit=MAX_MESH + HEADER_SIZE,
            )
        except OSError as exc:
            self._fail(f"cannot listen on port {port}: {exc}")
            return

        sockets = self._server.sockets or []
        self.bound_port = sockets[0].getsockname()[1] if sockets else port
        if self.relay_mode and self.relay_addr:
            self._relay_host = RelayHostAgent(
                self.relay_addr,
                room_id(self.code),
                self.bound_port,
                on_event=lambda kind, text: self._emit(kind, {"message": text}),
            )
            await self._relay_host.start()

        self._set_state("hosting")
        self._emit(
            "host_ready",
            {
                "code": self.code,
                "port": self.bound_port,
                "fingerprint": fingerprint,
                "short": tls.short_fingerprint(fingerprint),
                "relay": self.relay_mode,
            },
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        address = "%s:%s" % writer.get_extra_info("peername", ("?", 0))[:2]
        peer = Peer(peer_id=secrets.token_hex(8), writer=writer, address=address)
        peer.writer_task = asyncio.create_task(peer.drain_writes())
        try:
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break
                await self._handle_host_frame(peer, frame)
        except (Violation, asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            await self._drop_peer(peer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_frame(self, reader: asyncio.StreamReader) -> Frame | None:
        try:
            header = await reader.readexactly(HEADER_SIZE)
        except asyncio.IncompleteReadError:
            return None
        magic, version, _type, _channel, _flags, _seq, length, _crc = HEADER.unpack(header)
        if magic != MAGIC or version != PROTOCOL_VERSION:
            raise Violation("bad frame header")
        if length > MAX_MESH + HEADER_SIZE:
            raise Violation("frame exceeds the size limit")
        payload = await reader.readexactly(length)
        frame = protocol.decode(header + payload)
        if frame.type not in MessageType._value2member_map_:
            raise Violation("unknown message type")
        self._bytes_in += HEADER_SIZE + length
        return frame

    async def _handle_host_frame(self, peer: Peer, frame: Frame) -> None:
        now = time.monotonic()
        cost = 1.0 + len(frame.payload) / 4096.0
        if not peer.bucket.allow(cost, now):
            peer.strikes += 1
            if peer.strikes >= DISCONNECT_STRIKES:
                raise Violation("rate limit exceeded")
            return
        peer.last_seen = now

        if frame.type == MessageType.HELLO:
            await self._accept_hello(peer, frame)
            return
        if not peer.authenticated:
            raise Violation("frame before authentication")

        if frame.type == MessageType.PING:
            if len(frame.payload) > 64:
                raise Violation("ping payload too large")
            peer.enqueue(protocol.encode(Frame(type=MessageType.PONG, payload=frame.payload)), critical=True)
            return

        if frame.type == MessageType.BYE:
            raise Violation("peer left")

        if frame.type == MessageType.PRESENCE:
            body = self._json(frame)
            presence = {
                "name": peer.name,
                "color": list(peer.color),
                "scene": clean_text(body.get("scene"), 64),
                "frame": int(body.get("frame", 0)) if isinstance(body.get("frame"), (int, float)) else 0,
                "mode": clean_text(body.get("mode"), 32),
                "active": clean_text(body.get("active"), 64),
                "selected": clean_text(body.get("selected"), 512),
            }
            view = body.get("view")
            if isinstance(view, list) and len(view) == 16:
                try:
                    values = [float(value) for value in view]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                    presence["view"] = values
            aspect = body.get("aspect")
            if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
                presence["aspect"] = max(0.1, min(10.0, float(aspect)))
            persp = clean_text(body.get("persp"), 16)
            if persp:
                presence["persp"] = persp
            distance = body.get("dist")
            if isinstance(distance, (int, float)) and not isinstance(distance, bool):
                presence["dist"] = max(0.01, min(100000.0, float(distance)))
            self._broadcast(protocol.json_frame(MessageType.PRESENCE, presence), exclude=peer.peer_id)
            self._emit("presence", presence)
            return

        if frame.type == MessageType.CHAT:
            body = self._json(frame)
            text = clean_text(body.get("text"), MAX_CHAT)
            if text:
                message = {"name": peer.name, "color": list(peer.color), "text": text}
                self._broadcast(protocol.json_frame(MessageType.CHAT, message), exclude=peer.peer_id)
                self._emit("chat", message)
            return

        if frame.type == MessageType.TRANSFORM:
            uid, values = decode_transform(frame.payload)
            if not self._may_edit(peer, uid, now):
                return
            self._broadcast(frame, exclude=peer.peer_id, critical=False)
            self._emit("transform", {"uuid": uid, "matrix": values})
            return

        if frame.type in (MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT):
            uid, rev, payload = decode_mesh(frame.payload)
            if not self._may_edit(peer, uid, now):
                return
            self._validate_mesh(frame.type, payload)
            self._broadcast(frame, exclude=peer.peer_id, critical=True)
            kind = "mesh_delta" if frame.type == MessageType.MESH_DELTA else "mesh_snapshot"
            self._emit(kind, {"uuid": uid, "rev": rev, "payload": payload})
            return

        if frame.type == MessageType.LEASE:
            self._handle_lease(peer, frame, now)
            return

        if frame.type == MessageType.PING_MARK:
            body = self._json(frame, 4096)
            position = body.get("pos")
            if not isinstance(position, list) or len(position) != 3:
                raise Violation("ping marker needs a position")
            values = [float(v) for v in position]
            if not all(math.isfinite(v) and abs(v) < 1e7 for v in values):
                raise Violation("ping marker position is invalid")
            message = {"name": peer.name, "color": list(peer.color), "pos": values}
            self._broadcast(protocol.json_frame(MessageType.PING_MARK, message), exclude=peer.peer_id)
            self._emit("ping_mark", message)
            return

        if frame.type == MessageType.OBJECT_CREATE:
            try:
                body = object_payload.decode_create(frame.payload)
            except object_payload.ObjectPayloadError as exc:
                raise Violation(str(exc)) from exc
            meta = body["meta"]
            matrix = meta.get("matrix")
            if not isinstance(matrix, list) or len(matrix) != 16:
                raise Violation("object create needs a transform")
            values = [float(v) for v in matrix]
            if not all(math.isfinite(v) and abs(v) < 1e12 for v in values):
                raise Violation("object transform is invalid")
            meta["matrix"] = values
            meta["name"] = clean_name(meta.get("name"), "Object")
            geometry = body["geometry"]
            if body["kind"] == object_payload.KIND_MESH and geometry:
                from .core import chunker

                try:
                    chunker.validate_geometry(geometry, MAX_VERTICES)
                except ValueError as exc:
                    raise Violation(str(exc)) from exc
            elif geometry:
                raise Violation("only mesh objects may carry geometry")
            self._broadcast(frame, exclude=peer.peer_id)
            self._emit("object_create", body)
            return

        if frame.type == MessageType.OBJECT_DELETE:
            try:
                uid = object_payload.decode_delete(frame.payload)
            except object_payload.ObjectPayloadError as exc:
                raise Violation(str(exc)) from exc
            self._broadcast(frame, exclude=peer.peer_id)
            self._emit("object_delete", {"uuid": uid})
            return

        if frame.type == MessageType.MATERIAL_UPDATE:
            body = self._json(frame, 16384)
            try:
                uid = str(uuidlib.UUID(str(body.get("uuid"))))
            except ValueError as exc:
                raise Violation("invalid material object id") from exc
            slot = body.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 255:
                raise Violation("invalid material slot")
            payload = material_payload.sanitize(body.get("material"))
            if payload is None:
                raise Violation("invalid material payload")
            message = {"uuid": uid, "slot": slot, "material": payload}
            self._broadcast(protocol.json_frame(MessageType.MATERIAL_UPDATE, message), exclude=peer.peer_id)
            self._emit("material_update", message)
            return

        if frame.type == MessageType.MODIFIER_STACK:
            body = self._json(frame, 64 * 1024)
            message = self._sanitize_modifiers(body)
            if message is None:
                raise Violation("invalid modifier stack")
            self._broadcast(protocol.json_frame(MessageType.MODIFIER_STACK, message), exclude=peer.peer_id)
            self._emit("modifier_stack", message)
            return

        if frame.type == MessageType.IMAGE_ASSET:
            try:
                body = image_payload.decode(frame.payload)
            except image_payload.ImagePayloadError as exc:
                raise Violation(str(exc)) from exc
            body["name"] = clean_name(body.get("name"), "Image")
            self._broadcast(frame, exclude=peer.peer_id, critical=True)
            self._emit("image_asset", body)
            return

        raise Violation(f"message type {frame.type} is not accepted by the host")

    async def _accept_hello(self, peer: Peer, frame: Frame) -> None:
        if peer.authenticated:
            raise Violation("duplicate hello")
        body = self._json(frame, 4096)
        if self.pin:
            supplied = body.get("pin")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied[:64], self.pin):
                # Write directly: the peer is dropped immediately afterwards,
                # so a queued frame could be discarded before it is sent.
                try:
                    peer.writer.write(
                        protocol.encode(
                            protocol.json_frame(MessageType.ERROR, {"message": "invalid server password"})
                        )
                    )
                    await peer.writer.drain()
                except Exception:  # noqa: BLE001
                    pass
                raise Violation("invalid pin")
        peer.name = clean_name(body.get("name"))
        # Names are identity for leases, so the host makes them unique.
        existing = {other.name for other in self._peers.values() if other.authenticated} | {self.name}
        if peer.name in existing:
            peer.name = f"{peer.name[:40]}-{peer.peer_id[:4]}"
        peer.color = clean_color(body.get("color"))
        peer.authenticated = True
        self._peers[peer.peer_id] = peer

        peers = [
            {"name": other.name, "color": list(other.color)}
            for other in self._peers.values()
            if other.authenticated and other.peer_id != peer.peer_id
        ]
        welcome = {
            "peer_id": peer.peer_id,
            "name": self.name,
            "you": peer.name,
            "color": list(self.color),
            "peers": peers,
            "fingerprint": self.fingerprint,
            "code": self.code,
            "leases": self._leases.snapshot(time.monotonic()),
        }
        peer.enqueue(protocol.encode(protocol.json_frame(MessageType.WELCOME, welcome)), critical=True)
        self._broadcast(
            protocol.json_frame(
                MessageType.PRESENCE,
                {"name": peer.name, "color": list(peer.color), "joined": True, "scene": "", "frame": 0, "mode": ""},
            ),
            exclude=peer.peer_id,
        )
        self._emit("peer_join", {"name": peer.name, "color": list(peer.color)})

    def _handle_lease(self, peer: Peer, frame: Frame, now: float) -> None:
        body = self._json(frame, 4096)
        key = body.get("key")
        action = body.get("action")
        if not isinstance(key, str):
            raise Violation("lease without an object key")
        try:
            key = str(uuidlib.UUID(key))
        except ValueError as exc:
            raise Violation("invalid lease key") from exc
        if action == "acquire":
            if not self._leases.acquire(key, peer.name, now):
                owner = self._leases.owner_of(key, now)
                peer.enqueue(
                    protocol.encode(
                        protocol.json_frame(
                            MessageType.LEASE,
                            {"key": key, "owner": owner, "action": "denied"},
                        )
                    ),
                    critical=True,
                )
                self._leases.request(key, peer.name, now)
                if owner:
                    for other in self._peers.values():
                        if other.name == owner:
                            other.enqueue(
                                protocol.encode(
                                    protocol.json_frame(
                                        MessageType.LEASE,
                                        {"key": key, "owner": owner, "action": "requested", "requester": peer.name},
                                    )
                                ),
                                critical=True,
                            )
                            break
                return
        elif action == "grant":
            requester = clean_name(body.get("requester"), "")
            if not requester or self._leases.owner_of(key, now) != peer.name:
                raise Violation("only the lease owner can grant it")
            if not self._leases.resolve_request(key, requester, grant=True, now=now):
                self._leases.force(key, requester, now)
            peer_name = requester
            snapshot = self._leases.snapshot(now)
            self._broadcast(
                protocol.json_frame(
                    MessageType.LEASE, {"key": key, "owner": requester, "action": "grant", "leases": snapshot}
                )
            )
            return
        elif action == "release":
            self._leases.release(key, peer.name)
        elif action == "force":
            # Admin actions are not exposed to clients in this build.
            raise Violation("force leases are not permitted")
        else:
            raise Violation("unknown lease action")
        snapshot = self._leases.snapshot(now)
        body = {"key": key, "action": action, "leases": snapshot}
        if action != "release":
            body["owner"] = peer.name
        self._broadcast(protocol.json_frame(MessageType.LEASE, body))

    def _may_edit(self, peer: Peer, key: str, now: float) -> bool:
        owner = self._leases.owner_of(key, now)
        if owner is None:
            self._leases.acquire(key, peer.name, now)
            self._broadcast(
                protocol.json_frame(
                    MessageType.LEASE,
                    {"key": key, "owner": peer.name, "action": "acquire", "leases": self._leases.snapshot(now)},
                )
            )
            return True
        if owner == peer.name:
            self._leases.renew(key, peer.name, now)
            return True
        peer.strikes += 1
        if peer.strikes >= DISCONNECT_STRIKES:
            raise Violation("repeated edits to a leased object")
        peer.enqueue(
            protocol.encode(protocol.json_frame(MessageType.LEASE, {"key": key, "owner": owner, "action": "held"})),
            critical=True,
        )
        return False

    def _validate_mesh(self, type_: int, payload: bytes) -> None:
        from .core import chunker

        try:
            if type_ == MessageType.MESH_DELTA:
                chunker.validate_delta(payload, MAX_VERTICES)
            elif payload[:4] == chunker.GEO_MAGIC:
                chunker.validate_geometry(payload, MAX_VERTICES)
            else:
                chunker.validate_positions(payload, MAX_VERTICES)
        except ValueError as exc:
            raise Violation(str(exc)) from exc

    def _sanitize_modifiers(self, body: dict) -> dict[str, Any] | None:
        try:
            uid = str(uuidlib.UUID(str(body.get("uuid"))))
        except ValueError:
            return None
        modifiers = body.get("modifiers")
        if not isinstance(modifiers, list) or len(modifiers) > 64:
            return None
        result = []
        for item in modifiers:
            if not isinstance(item, dict):
                return None
            type_ = clean_text(item.get("type"), 32)
            name = clean_text(item.get("name"), 64) or type_
            if not type_:
                return None
            props_in = item.get("props") or {}
            if not isinstance(props_in, dict) or len(props_in) > 32:
                return None
            props: dict[str, Any] = {}
            for key, value in props_in.items():
                if not isinstance(key, str) or len(key) > 32:
                    return None
                if isinstance(value, bool):
                    props[key] = value
                elif isinstance(value, int):
                    props[key] = max(-1_000_000_000, min(1_000_000_000, value))
                elif isinstance(value, float):
                    if not math.isfinite(value):
                        return None
                    props[key] = value
                elif isinstance(value, str):
                    props[key] = value[:64]
                elif isinstance(value, list) and len(value) <= 16:
                    items: list[Any] = []
                    for entry in value:
                        if isinstance(entry, bool) or isinstance(entry, int):
                            items.append(entry)
                        elif isinstance(entry, float) and math.isfinite(entry):
                            items.append(entry)
                        else:
                            return None
                    props[key] = items
                else:
                    return None
            result.append(
                {
                    "type": type_,
                    "name": name,
                    "show_viewport": bool(item.get("show_viewport", True)),
                    "show_render": bool(item.get("show_render", True)),
                    "props": props,
                }
            )
        return {"uuid": uid, "modifiers": result}

    def _json(self, frame: Frame, limit: int = MAX_CONTROL) -> dict[str, Any]:
        if len(frame.payload) > limit:
            raise Violation("control payload too large")
        try:
            body = json.loads(frame.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Violation("invalid JSON payload") from exc
        if not isinstance(body, dict):
            raise Violation("payload must be an object")
        return body

    def _broadcast(self, frame: Frame, exclude: str | None = None, critical: bool = True) -> None:
        data = protocol.encode(frame)
        for peer in list(self._peers.values()):
            if peer.peer_id == exclude or not peer.authenticated:
                continue
            try:
                peer.enqueue(data, critical=critical)
            except Violation:
                asyncio.create_task(self._drop_peer(peer))

    def kick(self, name: str) -> None:
        """Ask the host loop to disconnect a peer by its assigned name."""
        loop = self._loop
        if self.is_host and loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._kick_peer(name)))

    async def _kick_peer(self, name: str) -> None:
        target = next(
            (peer for peer in self._peers.values() if peer.authenticated and peer.name == name),
            None,
        )
        if target is None:
            return
        try:
            target.writer.write(
                protocol.encode(protocol.json_frame(MessageType.KICK, {"message": "Kicked by host"}))
            )
            await target.writer.drain()
        except Exception:  # noqa: BLE001
            pass
        await self._drop_peer(target)
        try:
            target.writer.close()
            await target.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def _drop_peer(self, peer: Peer) -> None:
        was_authenticated = peer.authenticated
        peer.authenticated = False
        peer.closed = True
        if peer.writer_task:
            peer.writer_task.cancel()
        self._peers.pop(peer.peer_id, None)
        if was_authenticated:
            now = time.monotonic()
            released = False
            for key in self._leases.held_by(peer.name, now):
                if self._leases.release(key, peer.name):
                    released = True
            if released:
                self._broadcast(
                    protocol.json_frame(
                        MessageType.LEASE,
                        {"action": "release", "leases": self._leases.snapshot(now)},
                    )
                )
            self._emit("peer_leave", {"name": peer.name})
            self._broadcast(protocol.json_frame(MessageType.BYE, {"name": peer.name}))
        try:
            await peer.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ client
    async def _client_loop(self) -> None:
        delay = 1.0
        while self._stop_event and not self._stop_event.is_set():
            try:
                await self._connect_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.failed:
                    return
                self._emit("error", {"message": str(exc)[:200]})
            if self._stop_event.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 10.0)

    async def _connect_once(self) -> None:
        try:
            info = decode_room(self.code)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"invalid room code: {exc}")
            return

        self._set_state("joining")
        context = tls.client_context()
        if self.relay_mode and self._relay_client is not None:
            host, port = "127.0.0.1", self._relay_client.port
        else:
            host, port = info.ip, info.port
        try:
            reader, writer = await asyncio.open_connection(
                host,
                port,
                ssl=context,
                server_hostname="blendermp",
                limit=MAX_MESH + HEADER_SIZE,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise RuntimeError(f"cannot reach host: {exc}") from exc

        actual = tls.peer_fingerprint(writer)
        expected = self.expected_fingerprint or tls.known_fingerprint(control.data_dir(), self.code)
        if expected:
            if not tls.verify_fingerprint(actual, expected):
                writer.close()
                self._fail("host certificate changed; refusing to connect")
                return
        elif actual:
            tls.remember_fingerprint(control.data_dir(), self.code, actual)
            self._emit("host_key", {"fingerprint": actual, "short": tls.short_fingerprint(actual)})
        else:
            writer.close()
            self._fail("host did not present a certificate")
            return

        self._fingerprint = actual or ""
        self.fingerprint = actual or ""
        self._reader = reader
        self._writer = writer
        writer.write(
            protocol.encode(
                protocol.json_frame(
                    MessageType.HELLO,
                    {"name": self.name, "color": list(self.color), "pin": self.pin},
                )
            )
        )
        await writer.drain()

        frame = await asyncio.wait_for(self._read_frame(reader), timeout=8.0)
        if frame is not None and frame.type == MessageType.ERROR:
            body = self._json(frame, 1024)
            message = clean_text(body.get("message"), 200) or "host rejected the join"
            self.failed = True
            self.failure_reason = message
            self._emit("error", {"message": message})
            self._request_stop()
            return
        if frame is None or frame.type != MessageType.WELCOME:
            raise RuntimeError("host rejected the join")
        body = self._json(frame, 4096)
        self._peer_id = str(body.get("peer_id", ""))[:32]
        self._set_state("connected")
        self._emit("welcome", body)
        if self._ping_task:
            self._ping_task.cancel()
        self._ping_task = asyncio.create_task(self._ping_loop())

        try:
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break
                self._handle_client_frame(frame)
        finally:
            if self._ping_task:
                self._ping_task.cancel()
                self._ping_task = None
            self._writer = None
            self._reader = None
        if not (self._stop_event and self._stop_event.is_set()):
            raise ConnectionError("host closed the connection")

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            self._enqueue(protocol.json_frame(MessageType.PING, {"t": time.time()}))

    def _handle_client_frame(self, frame: Frame) -> None:
        self._bytes_in += len(frame.payload)
        if frame.type == MessageType.PONG:
            try:
                body = json.loads(frame.payload.decode("utf-8"))
                sent = float(body.get("t", 0.0))
            except (ValueError, json.JSONDecodeError):
                return
            if sent:
                self._rtt_ms = max(0.0, min(5000.0, (time.time() - sent) * 1000.0))
                self._emit("rtt", {"ms": self._rtt_ms})
            return

        if frame.type == MessageType.PRESENCE:
            body = self._json(frame, 8192)
            presence = {
                "name": clean_text(body.get("name"), MAX_NAME),
                "color": clean_color(body.get("color")),
                "scene": clean_text(body.get("scene"), 64),
                "frame": int(body.get("frame", 0)) if isinstance(body.get("frame"), (int, float)) else 0,
                "mode": clean_text(body.get("mode"), 32),
                "active": clean_text(body.get("active"), 64),
                "selected": clean_text(body.get("selected"), 512),
            }
            view = body.get("view")
            if isinstance(view, list) and len(view) == 16:
                try:
                    values = [float(value) for value in view]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                    presence["view"] = values
            aspect = body.get("aspect")
            if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
                presence["aspect"] = max(0.1, min(10.0, float(aspect)))
            persp = clean_text(body.get("persp"), 16)
            if persp:
                presence["persp"] = persp
            distance = body.get("dist")
            if isinstance(distance, (int, float)) and not isinstance(distance, bool):
                presence["dist"] = max(0.01, min(100000.0, float(distance)))
            self._emit("presence", presence)
            return

        if frame.type == MessageType.CHAT:
            body = self._json(frame, MAX_CHAT + 512)
            self._emit("chat", {"name": clean_text(body.get("name"), MAX_NAME), "text": clean_text(body.get("text"), MAX_CHAT)})
            return

        if frame.type == MessageType.BYE:
            body = self._json(frame, 1024)
            self._emit("peer_leave", {"name": clean_text(body.get("name"), MAX_NAME)})
            return

        if frame.type == MessageType.KICK:
            body = self._json(frame, 1024)
            self.failed = True
            self.failure_reason = clean_text(body.get("message"), 200) or "Kicked by host"
            self._emit("kicked", {"message": self.failure_reason})
            self._request_stop()
            return

        if frame.type == MessageType.TRANSFORM:
            uid, values = decode_transform(frame.payload)
            self._emit("transform", {"uuid": uid, "matrix": values})
            return

        if frame.type in (MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT):
            uid, rev, payload = decode_mesh(frame.payload)
            kind = "mesh_delta" if frame.type == MessageType.MESH_DELTA else "mesh_snapshot"
            self._emit(kind, {"uuid": uid, "rev": rev, "payload": payload})
            return

        if frame.type == MessageType.LEASE:
            body = self._json(frame, 8192)
            self._emit("lease", body)
            return

        if frame.type == MessageType.PING_MARK:
            body = self._json(frame, 4096)
            position = body.get("pos")
            if isinstance(position, list) and len(position) == 3:
                self._emit(
                    "ping_mark",
                    {
                        "name": clean_text(body.get("name"), MAX_NAME),
                        "color": clean_color(body.get("color")),
                        "pos": [float(v) for v in position],
                    },
                )
            return

        if frame.type == MessageType.OBJECT_CREATE:
            try:
                self._emit("object_create", object_payload.decode_create(frame.payload))
            except object_payload.ObjectPayloadError:
                pass
            return

        if frame.type == MessageType.OBJECT_DELETE:
            try:
                self._emit("object_delete", {"uuid": object_payload.decode_delete(frame.payload)})
            except object_payload.ObjectPayloadError:
                pass
            return

        if frame.type == MessageType.MATERIAL_UPDATE:
            body = self._json(frame, 16384)
            payload = material_payload.sanitize(body.get("material"))
            slot = body.get("slot")
            if payload is not None and isinstance(slot, int) and 0 <= slot <= 255:
                self._emit("material_update", {"uuid": body.get("uuid"), "slot": slot, "material": payload})
            return

        if frame.type == MessageType.MODIFIER_STACK:
            message = self._sanitize_modifiers(self._json(frame, 64 * 1024))
            if message is not None:
                self._emit("modifier_stack", message)
            return

        if frame.type == MessageType.IMAGE_ASSET:
            try:
                self._emit("image_asset", image_payload.decode(frame.payload))
            except image_payload.ImagePayloadError:
                pass
            return

        if frame.type in (MessageType.SCENE_RESET, MessageType.SCENE_READY):
            body = self._json(frame, 512)
            self._emit("scene_reset" if frame.type == MessageType.SCENE_RESET else "scene_ready", body)
            return

        if frame.type == MessageType.ERROR:
            body = self._json(frame, 1024)
            self._emit("error", {"message": clean_text(body.get("message"), 200)})

    # ------------------------------------------------------------------ tick
    async def _tick_loop(self) -> None:
        assert self._wake is not None
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

            while True:
                try:
                    frame = self._outbox.get_nowait()
                except queue.Empty:
                    break
                try:
                    if self.is_host:
                        self._broadcast(frame, critical=True)
                    elif self._writer:
                        data = protocol.encode(frame)
                        if self._writer.transport.get_write_buffer_size() > 4 * 1024 * 1024:
                            continue  # drop rather than buffer unboundedly
                        self._writer.write(data)
                        await self._writer.drain()
                        self._bytes_out += len(data)
                except ConnectionError:
                    self._writer = None
                except asyncio.CancelledError:
                    return

            if self.is_host:
                now = time.monotonic()
                for peer in list(self._peers.values()):
                    if now - peer.last_seen > PEER_TIMEOUT:
                        asyncio.create_task(self._drop_peer(peer))
                self._leases.sweep(now)

    # ------------------------------------------------------------------ public API
    def send_scene_reset(self, target: str, extra: dict | None = None) -> None:
        body = {"target": target}
        if isinstance(extra, dict):
            body.update(extra)
        self._enqueue(protocol.json_frame(MessageType.SCENE_RESET, body))

    def send_scene_ready(self, target: str, extra: dict | None = None) -> None:
        body = {"target": target}
        if isinstance(extra, dict):
            body.update(extra)
        self._enqueue(protocol.json_frame(MessageType.SCENE_READY, body))

    def send_presence(self, body: dict[str, Any]) -> None:
        payload = {
            "scene": clean_text(body.get("scene"), 64),
            "frame": int(body.get("frame", 0)) if isinstance(body.get("frame"), (int, float)) else 0,
            "mode": clean_text(body.get("mode"), 32),
            "active": clean_text(body.get("active"), 64),
            "selected": clean_text(body.get("selected"), 512),
        }
        view = body.get("view")
        if isinstance(view, list) and len(view) == 16:
            try:
                values = [float(value) for value in view]
            except (TypeError, ValueError):
                values = []
            if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                payload["view"] = values
        aspect = body.get("aspect")
        if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
            payload["aspect"] = max(0.1, min(10.0, float(aspect)))
        persp = clean_text(body.get("persp"), 16)
        if persp:
            payload["persp"] = persp
        distance = body.get("dist")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            payload["dist"] = max(0.01, min(100000.0, float(distance)))
        if self.is_host:
            payload.update({"name": self.name, "color": list(self.color)})
        self._enqueue(protocol.json_frame(MessageType.PRESENCE, payload))

    def send_chat(self, text: str) -> None:
        text = clean_text(text, MAX_CHAT)
        if text:
            self._enqueue(protocol.json_frame(MessageType.CHAT, {"text": text, "name": self.name}))

    def send_transform(self, uid: str, matrix) -> None:
        try:
            payload = encode_transform(uid, matrix)
        except Exception:  # noqa: BLE001
            return
        self._enqueue(Frame(type=MessageType.TRANSFORM, channel=Channel.TRANSFORM, payload=payload))

    def send_mesh(self, obj, kind: str, payload: bytes, rev: int = 0) -> None:
        uid = obj.get("bmp_uuid")
        if not uid:
            return
        if len(payload) > MAX_MESH:
            return
        try:
            body = encode_mesh(uid, int(rev), payload)
        except Exception:  # noqa: BLE001
            return
        type_ = MessageType.MESH_DELTA if kind == "delta" else MessageType.MESH_SNAPSHOT
        self._enqueue(Frame(type=type_, channel=Channel.MESH, payload=body))

    def send_ping(self) -> None:
        self._enqueue(protocol.json_frame(MessageType.PING, {"t": time.time()}))

    def send_ping_mark(self, position) -> None:
        try:
            values = [float(v) for v in position]
        except (TypeError, ValueError):
            return
        if len(values) != 3 or not all(math.isfinite(v) and abs(v) < 1e7 for v in values):
            return
        self._enqueue(protocol.json_frame(MessageType.PING_MARK, {"pos": values}))

    def send_object_create(self, uid: str, kind: int, meta: dict, geometry: bytes = b"") -> None:
        try:
            payload = object_payload.encode_create(uid, kind, meta, geometry)
        except object_payload.ObjectPayloadError:
            return
        if len(payload) > MAX_MESH:
            return
        self._enqueue(Frame(type=MessageType.OBJECT_CREATE, channel=Channel.MESH, payload=payload))

    def send_object_delete(self, uid: str) -> None:
        try:
            payload = object_payload.encode_delete(uid)
        except object_payload.ObjectPayloadError:
            return
        self._enqueue(Frame(type=MessageType.OBJECT_DELETE, channel=Channel.CONTROL, payload=payload))

    def send_material(self, uid: str, slot: int, material: dict) -> None:
        payload = material_payload.sanitize(material)
        if payload is None or not 0 <= int(slot) <= 255:
            return
        self._enqueue(
            protocol.json_frame(MessageType.MATERIAL_UPDATE, {"uuid": uid, "slot": int(slot), "material": payload})
        )

    def send_modifier_stack(self, uid: str, modifiers: list) -> None:
        message = self._sanitize_modifiers({"uuid": uid, "modifiers": modifiers})
        if message is None:
            return
        self._enqueue(protocol.json_frame(MessageType.MODIFIER_STACK, message))

    def send_image(self, name: str, format_hint: str, data: bytes) -> None:
        try:
            payload = image_payload.encode(name, format_hint, data)
        except image_payload.ImagePayloadError:
            return
        if len(payload) > MAX_MESH:
            return
        self._enqueue(Frame(type=MessageType.IMAGE_ASSET, channel=Channel.BULK, payload=payload))

    def request_lease(self, key: str, action: str = "acquire") -> None:
        self._enqueue(protocol.json_frame(MessageType.LEASE, {"key": key, "action": action}))

    def send_lease(self, key: str, action: str, requester: str | None = None) -> None:
        body = {"key": key, "action": action}
        if requester:
            body["requester"] = requester
        self._enqueue(protocol.json_frame(MessageType.LEASE, body))

    def local_lease(self, key: str, action: str) -> bool:
        """Acquire/release a lease from the host's own Blender."""
        if not self.is_host:
            self.send_lease(key, action)
            return True
        now = time.monotonic()
        if action == "acquire":
            if not self._leases.acquire(key, self.name, now):
                owner = self._leases.owner_of(key, now)
                self._emit("lease", {"key": key, "owner": owner, "action": "held"})
                return False
        elif action == "release":
            self._leases.release(key, self.name)
        elif action == "force":
            self._leases.force(key, self.name, now)
        else:
            return False
        body = {
            "key": key,
            "action": action,
            "leases": self._leases.snapshot(now),
        }
        if action != "release":
            body["owner"] = self.name
        self._broadcast(protocol.json_frame(MessageType.LEASE, body))
        self._emit("lease", body)
        return True

    def stats(self) -> dict[str, Any]:
        peers = []
        bytes_out = self._bytes_out
        if self.is_host:
            peers = [{"name": peer.name, "address": peer.address} for peer in self._peers.values() if peer.authenticated]
            bytes_out += sum(peer.sent for peer in self._peers.values())
        else:
            peers = [{"name": "host", "address": self.code}]
        return {
            "state": self.state,
            "rtt_ms": round(self._rtt_ms, 1),
            "bytes_in": self._bytes_in,
            "bytes_out": bytes_out,
            "peers": peers,
            "fingerprint": self.fingerprint,
            "port": self.bound_port,
            "relay": self.relay_mode,
        }
