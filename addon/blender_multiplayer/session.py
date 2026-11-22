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
import hashlib
import hmac
import ipaddress
import json
import math
import queue
import re
import secrets
import socket
import struct
import threading
import time
import traceback
import uuid as uuidlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from . import control
    from .core import image_payload
    from .core import asset_bundle
    from .core import bookmarks
    from .core import eventlog
    from .core import identity
    from .core import material as material_payload
    from .core import object_payload, protocol, tls
    from .core import compression
    from .core import transfer
    from .core.protocol import HEADER, HEADER_SIZE, MAGIC, PROTOCOL_VERSION, Channel, ErrorCode, Frame, MessageType
    from .core.roomcode import FLAG_RELAY, decode_room, encode_room, normalize, room_id
    from .core.version import ADDON_VERSION, compatible
    from .net.relay import RelayClientAgent, RelayHostAgent
    from .sync.leases import LeaseManager, Role, can_edit, can_manage
else:  # pragma: no cover - allows importing the module standalone (tests)
    import control
    from core import image_payload
    from core import asset_bundle
    from core import bookmarks
    from core import eventlog
    from core import identity
    from core import material as material_payload
    from core import object_payload, protocol, tls
    from core import compression
    from core import transfer
    from core.protocol import HEADER, HEADER_SIZE, MAGIC, PROTOCOL_VERSION, Channel, ErrorCode, Frame, MessageType
    from core.roomcode import FLAG_RELAY, decode_room, encode_room, normalize, room_id
    from core.version import ADDON_VERSION, compatible
    from net.relay import RelayClientAgent, RelayHostAgent
    from sync.leases import LeaseManager, Role, can_edit, can_manage

RESUME_TTL = 30.0
MAX_CONTROL = 64 * 1024
MAX_MESH = 64 * 1024 * 1024
CHUNK_THRESHOLD = 900 * 1024  # ~900 KB; stays under Cloudflare's ~1 MiB WebSocket cap\nRELAY_SAFE_FRAME = 900 * 1024
MAX_VERTICES = 20_000_000
MAX_CHAT = 2_000
MAX_NAME = 48
MAX_TEXT = 256
PING_INTERVAL = 5.0
PEER_TIMEOUT = 20.0
DISCONNECT_STRIKES = 6
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MUTATING_TYPES = {
    MessageType.TRANSFORM,
    MessageType.POSE,
    MessageType.ACTION,
    MessageType.SHAPE_KEYS,
    MessageType.MESH_DELTA,
    MessageType.MESH_SNAPSHOT,
    MessageType.OBJECT_CREATE,
    MessageType.OBJECT_DELETE,
    MessageType.MATERIAL_UPDATE,
    MessageType.MODIFIER_STACK,
    MessageType.IMAGE_ASSET,
    MessageType.CHUNK,
}
BULK_TYPES = {
    MessageType.OBJECT_CREATE,
    MessageType.MESH_SNAPSHOT,
    MessageType.IMAGE_ASSET,
    MessageType.ACTION,
    MessageType.BULK,
    MessageType.ASSET_BUNDLE,
    MessageType.CHUNK,
}


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


def _is_lan_ip(value: str) -> bool:
    """True for a private LAN address, not loopback or a public internet IP.

    Relay-bridge peers arrive from 127.0.0.1, so they stay on the bandwidth
    profile. Direct LAN peers get the unthrottled fast path.
    """
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return False
    return bool(address.is_private and not address.is_loopback)


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _local_ips() -> list[str]:
    """Every usable IPv4 address, default-route address first.

    A host can be multi-homed (for example Wi-Fi plus a Windows ICS Ethernet
    link to the other PC). Advertising only the default-route address makes the
    client try an unreachable subnet, so publish all of them.
    """
    addresses: list[str] = []
    try:
        _name, _aliases, resolved = socket.gethostbyname_ex(socket.gethostname())
        for value in resolved:
            if value and not value.startswith(("127.", "169.254.")) and value not in addresses:
                addresses.append(value)
    except OSError:
        pass
    default = _local_ip()
    if default and not default.startswith(("127.", "169.254.")) and default not in addresses:
        addresses.insert(0, default)
    return addresses[:4]


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
    app: str = ""
    addon: str = ""
    protocol: int = 0
    caps: int = 0
    role: int = int(Role.EDITOR)
    authenticated: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    strikes: int = 0
    rtt_ms: float = 0.0
    ping_sent: float = 0.0
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    writer_task: asyncio.Task | None = None
    closed: bool = False
    kicked: bool = False
    resume_token: str = ""
    sent: int = 0
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(240.0, 480.0))
    subscriptions: dict[str, str] = field(default_factory=dict)
    priorities: dict[str, int] = field(default_factory=dict)
    last_sent: dict[tuple[str, int], float] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    quality: int = 0
    lan: bool = False
    byte_window: deque = field(default_factory=deque)
    pending: dict[tuple[str, int], tuple[bytes, float, bool]] = field(default_factory=dict)
    dropped: bool = False

    def enqueue(self, data: bytes, critical: bool = False) -> None:
        now = time.monotonic()
        while self.byte_window and now - self.byte_window[0][0] > 1.0:
            self.byte_window.popleft()
        used = sum(size for _stamp, size in self.byte_window)
        if not critical and used + len(data) > 1024 * 1024:
            self.dropped = True
            return
        try:
            self.queue.put_nowait(data)
            self.byte_window.append((now, len(data)))
        except asyncio.QueueFull:
            self.dropped = True
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
        direct_code: str | None = None,
        direct_first: bool = False,
        version: str | None = None,
        mesh_url: str | None = None,
    ):
        self.inbox = inbox
        self.is_host = host
        self.event_sink = None
        self.code = code.strip().upper()
        self.name = clean_name(name)
        self.color = clean_color(color)
        self.version = clean_text(version or ADDON_VERSION, 32) or ADDON_VERSION
        self.remote_app = ""
        self.remote_caps = 0
        self.role = int(Role.OWNER if host else Role.EDITOR)
        self.port = int(port or 0)
        self.expected_fingerprint = fingerprint
        self.pin = pin[:64]
        self._pin_salt = secrets.token_bytes(16)
        self._pin_hash = (
            hashlib.scrypt(self.pin.encode("utf-8"), salt=self._pin_salt, n=2**14, r=8, p=1, dklen=32)
            if self.pin
            else b""
        )
        self._pin_attempts: dict[str, list[float]] = {}
        self.identity = None
        self.identity_public = ""
        self.identity_signature = ""
        self.identity_descriptor: dict = {}

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
        # Direct LAN: raw payloads. Relay/cross-network: lossless zlib.
        self.compress_enabled = bool(self.relay_mode)
        compression.set_enabled(self.compress_enabled)
        self.direct_first = bool(direct_first)
        self.direct_code = (direct_code or "").strip().upper()
        self.direct_codes: list[str] = []
        self._direct_failed = False
        self._using_relay = bool(self.relay_mode)
        self._relay_host: RelayHostAgent | None = None
        self._relay_client: RelayClientAgent | None = None
        self.mesh_url = (mesh_url or "").strip()
        self._mesh = None
        self._mesh_task: asyncio.Task | None = None

        self.failed = False
        self.failure_reason = ""
        self.failure_code = ""
        self.session_id = secrets.token_hex(16) if host else ""
        self.resume_token = ""
        self._ever_connected = False
        self._recent: dict[str, dict] = {}
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
        self._lan_peer_count = 0
        self._peer_id: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ping_task: asyncio.Task | None = None
        self._leases = LeaseManager(ttl=60.0, grace=15.0)
        self._expiry_notified: set[str] = set()
        self._bytes_in = 0
        self._bytes_out = 0
        self._revs: dict[str, int] = {}
        self._rtt_ms = 0.0
        self._last_ping = 0.0
        self._last_host_ping = 0.0
        self._incoming = transfer.Reassembler()

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
            if self.mesh_url:
                self._mesh_task = asyncio.create_task(self._start_mesh())
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

    async def _start_mesh(self) -> None:
        """Start the direct mesh alongside the session, best-effort."""
        try:
            from .net.meshcoord import MeshCoordinator

            coordinator = MeshCoordinator(self.mesh_url, room_id(self.code))
            started = await asyncio.wait_for(coordinator.start(), 12.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return
        if not started:
            await coordinator.stop()
            return
        self._mesh = coordinator

    def mesh_status(self) -> dict:
        if self._mesh is None:
            return {"active": False, "peers": [], "public_address": "", "error": ""}
        status = self._mesh.status()
        return {
            "active": True,
            "peer_id": status.get("peer_id", ""),
            "peers": status.get("peers", []),
            "public_address": status.get("public_address", ""),
            "error": status.get("error", ""),
        }

    async def _shutdown(self) -> None:
        if self._mesh_task is not None:
            self._mesh_task.cancel()
            try:
                await self._mesh_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._mesh_task = None
        if self._mesh is not None:
            try:
                await self._mesh.stop()
            except Exception:  # noqa: BLE001
                pass
            self._mesh = None
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
        self._lan_peer_count = 0
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
            if not self.is_host and self._writer is not None:
                loop.call_soon_threadsafe(self._send_bye)
            else:
                loop.call_soon_threadsafe(self._request_stop)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def _send_bye(self) -> None:
        writer = self._writer
        if writer is not None:
            try:
                writer.write(protocol.encode(protocol.json_frame(MessageType.BYE, {})))
            except Exception:  # noqa: BLE001
                pass
        self._request_stop()

    def _request_stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

    # ------------------------------------------------------------------ helpers
    def _emit(self, kind: str, body: dict[str, Any]) -> None:
        self.inbox.put((kind, body))

    def _emit_leases(self, extra: dict | None = None) -> None:
        """Mirror the authoritative lease map into the host's own addon.

        ``_broadcast`` only reaches peers, so without this the host's UI and
        sync loops never learned that a client owned an object.
        """
        body: dict[str, Any] = {
            "action": "sync",
            "leases": self._leases.snapshot(time.monotonic()),
        }
        if extra:
            body.update(extra)
        self._emit("lease", body)

    def force_release_all(self) -> None:
        """Host escape hatch: clear every lease and tell all peers."""
        loop = self._loop
        if self.is_host and loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._force_release_all()))

    async def _force_release_all(self) -> None:
        now = time.monotonic()
        for key in list(self._leases.snapshot(now)):
            self._leases.release(key)
        self._expiry_notified.clear()
        self._broadcast(
            protocol.json_frame(
                MessageType.LEASE,
                {"action": "released", "leases": self._leases.snapshot(now)},
            )
        )
        self._emit_leases()

    def _identity_body(self) -> dict:
        if not self.identity_public or not self.identity_signature:
            return {}
        return {
            "public_key": self.identity_public,
            "signature": self.identity_signature,
            "descriptor": self.identity_descriptor,
        }

    def _set_state(self, value: str) -> None:
        self.state = value
        self._emit("state", {"state": value})

    def _fail(self, reason: str) -> None:
        self.failed = True
        self.failure_reason = reason[:200]
        self.failure_code = ErrorCode.INTERNAL.value
        self._emit("error", {"message": self.failure_reason, "code": self.failure_code})
        self._request_stop()

    def _enqueue(self, frame: Frame) -> None:
        loop = self._loop
        if loop and loop.is_running():
            self._outbox.put(frame)
            loop.call_soon_threadsafe(self._wake.set)

    def _tap(self, kind: str, body: dict) -> None:
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink(kind, body)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ host
    async def _start_host(self) -> None:
        try:
            certificate, key, fingerprint = tls.load_or_create_certificate(control.certs_dir())
            context = tls.server_context(certificate, key)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"tls setup failed: {exc}")
            return

        self.fingerprint = fingerprint
        self.identity = identity.load_or_create()
        if self.identity:
            self.identity_public = str(self.identity.get("public", ""))
            self.identity_descriptor = identity.descriptor(
                self.code,
                self.version,
                self.version,
                PROTOCOL_VERSION,
                fingerprint,
                protocol.DEFAULT_CAPABILITIES,
            )
            self.identity_signature = identity.sign(self.identity, self.identity_descriptor)
        if self.relay_mode:
            bind_host = "0.0.0.0"
            port = self.port or 0
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
        codes: list[str] = []
        for address in _local_ips():
            try:
                codes.append(encode_room(address, self.bound_port))
            except Exception:  # noqa: BLE001
                continue
        self.direct_codes = codes[:4]
        self.direct_code = codes[0] if codes else ""
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
                "direct_code": self.direct_code,
                "direct_codes": self.direct_codes,
                "fingerprint": fingerprint,
                "short": tls.short_fingerprint(fingerprint),
                "relay": self._using_relay,
            },
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        address = "%s:%s" % peername[:2]
        peer = Peer(
            peer_id=secrets.token_hex(8),
            writer=writer,
            address=address,
            lan=_is_lan_ip(str(peername[0])),
        )
        peer.writer_task = asyncio.create_task(peer.drain_writes())
        try:
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break
                await self._handle_host_frame(peer, frame)
        except Violation as exc:
            message = str(exc)
            if message != "peer left":
                lowered = message.lower()
                if "rate limit" in lowered:
                    code = ErrorCode.RATE_LIMIT
                elif "too large" in lowered or "exceeds" in lowered:
                    code = ErrorCode.ASSET_TOO_LARGE
                else:
                    code = ErrorCode.PROTOCOL
                try:
                    peer.enqueue(
                        protocol.encode(
                            protocol.json_frame(
                                MessageType.ERROR,
                                {"message": message[:200], "code": code.value},
                            )
                        ),
                        critical=True,
                    )
                except Violation:
                    pass
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
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
        # Large one-shot structural payloads (models, mesh snapshots, images)
        # must not be charged per 4 KB or they exceed the burst bucket and are
        # silently dropped. Keep a modest cap so a flood is still rate-limited.
        if frame.type == MessageType.CHUNK:
            cost = 4.0
        elif frame.type in BULK_TYPES:
            cost = min(64.0, 1.0 + len(frame.payload) / 1_000_000.0)
        else:
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

        if frame.type in MUTATING_TYPES and not can_edit(peer.role):
            peer.enqueue(
                protocol.encode(
                    protocol.json_frame(
                        MessageType.ERROR,
                        {"message": "your role does not allow edits", "code": ErrorCode.ROLE_DENIED.value},
                    )
                ),
                critical=True,
            )
            return

        if frame.type == MessageType.PING:
            if len(frame.payload) > 64:
                raise Violation("ping payload too large")
            peer.enqueue(protocol.encode(Frame(type=MessageType.PONG, payload=frame.payload)), critical=True)
            return

        if frame.type == MessageType.PONG:
            try:
                body = json.loads(frame.payload.decode("utf-8"))
                sent = float(body.get("t", 0.0))
            except (ValueError, json.JSONDecodeError):
                return
            if sent:
                peer.rtt_ms = max(0.0, min(5000.0, (time.time() - sent) * 1000.0))
            return

        if frame.type == MessageType.BYE:
            peer.resume_token = ""
            raise Violation("peer left")

        if frame.type == MessageType.PRESENCE:
            body = self._json(frame)
            presence: dict[str, Any] = {
                "name": peer.name,
                "color": list(peer.color),
                "role": int(peer.role),
                "ping_ms": (
                    max(0.0, min(5000.0, float(body["ping_ms"])))
                    if isinstance(body.get("ping_ms"), (int, float)) and not isinstance(body.get("ping_ms"), bool)
                    else round(max(0.0, float(peer.rtt_ms)), 1)
                ),
            }
            for key, limit in (
                ("scene", 64),
                ("mode", 32),
                ("active", 64),
                ("selected", 512),
                ("scene_hash", 32),
            ):
                if key in body:
                    presence[key] = clean_text(body.get(key), limit)
            if "frame" in body and isinstance(body.get("frame"), (int, float)) and not isinstance(body.get("frame"), bool):
                presence["frame"] = int(body["frame"])
            if "playing" in body:
                presence["playing"] = bool(body.get("playing"))
            if "fps" in body and isinstance(body.get("fps"), (int, float)) and not isinstance(body.get("fps"), bool):
                presence["fps"] = max(1, min(240, int(body["fps"])))
            cursor = body.get("cursor")
            if isinstance(cursor, list) and len(cursor) == 3:
                try:
                    values = [float(value) for value in cursor]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 3 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    presence["cursor"] = values
            view = body.get("view")
            if isinstance(view, list) and len(view) == 16:
                try:
                    values = [float(value) for value in view]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                    presence["view"] = values
            corners = body.get("view_corners")
            if isinstance(corners, list) and len(corners) == 21:
                try:
                    values = [float(value) for value in corners]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 21 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    presence["view_corners"] = values
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

        if frame.type == MessageType.SUBSCRIPTION:
            body = self._json(frame, 1024 * 1024)
            uids = body.get("uids")
            modes = body.get("modes")
            priorities = body.get("priorities")
            if not isinstance(uids, list) or len(uids) > 20000:
                raise Violation("invalid subscription list")
            if modes is not None and not isinstance(modes, dict):
                raise Violation("invalid subscription modes")
            if priorities is not None and not isinstance(priorities, dict):
                raise Violation("invalid subscription priorities")
            subscriptions = {}
            for uid in uids:
                if not isinstance(uid, str):
                    continue
                try:
                    uid = str(uuidlib.UUID(uid))
                except ValueError:
                    continue
                mode = modes.get(uid, "full") if isinstance(modes, dict) else "full"
                if mode not in {"full", "key", "mute"}:
                    mode = "full"
                subscriptions[uid] = mode
            peer.subscriptions = subscriptions
            clean_priorities = {}
            if isinstance(priorities, dict):
                for uid, value in list(priorities.items())[:20000]:
                    if not isinstance(uid, str) or not isinstance(value, int) or isinstance(value, bool):
                        continue
                    try:
                        clean_priorities[str(uuidlib.UUID(uid))] = max(0, min(2, value))
                    except ValueError:
                        continue
            peer.priorities = clean_priorities
            return

        if frame.type == MessageType.CLIENT_STATS:
            body = self._json(frame, 4096)
            stats = {}
            for key in ("apply_ms_p95", "fps", "rx_kbps", "tx_kbps", "queue_depth"):
                value = body.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                    stats[key] = max(0.0, min(1e9, float(value)))
            peer.stats = stats
            apply_ms = float(stats.get("apply_ms_p95", 0.0))
            fps = float(stats.get("fps", 60.0))
            queue_depth = float(stats.get("queue_depth", 0.0))
            quality = 0
            if apply_ms > 40.0 or fps < 20.0 or queue_depth > 300:
                quality = 2
            elif apply_ms > 20.0 or fps < 35.0 or queue_depth > 120:
                quality = 1
            if quality < peer.quality:
                quality = max(quality, peer.quality - 1)
            peer.quality = quality
            self._emit("client_stats", {"name": peer.name, **stats, "quality": quality})
            return

        if frame.type == MessageType.VIEW_BOOKMARK:
            body = self._json(frame, 8192)
            bookmark = bookmarks.sanitize(body.get("bookmark"))
            if bookmark is None:
                raise Violation("invalid view bookmark")
            if not can_manage(peer.role):
                peer.enqueue(
                    protocol.encode(
                        protocol.json_frame(
                            MessageType.ERROR,
                            {"message": "only an admin can move everyone's view", "code": ErrorCode.ROLE_DENIED.value},
                        )
                    ),
                    critical=True,
                )
                return
            message = {"name": peer.name, "bookmark": bookmark}
            self._broadcast(protocol.json_frame(MessageType.VIEW_BOOKMARK, message), exclude=peer.peer_id)
            self._emit("view_bookmark", message)
            return

        if frame.type == MessageType.ASSET_BUNDLE:
            if not can_edit(peer.role):
                peer.enqueue(
                    protocol.encode(
                        protocol.json_frame(
                            MessageType.ERROR,
                            {"message": "your role does not allow sending assets", "code": ErrorCode.ROLE_DENIED.value},
                        )
                    ),
                    critical=True,
                )
                return
            try:
                manifest = asset_bundle.decode(frame.payload)
            except asset_bundle.AssetBundleError as exc:
                raise Violation(str(exc)) from exc
            target = manifest.get("target")
            if not target or target == peer.name:
                raise Violation("asset bundle needs another target")
            manifest["sender"] = peer.name
            manifest["target"] = target
            manifest["sender_color"] = list(peer.color)
            if target == self.name:
                self._emit("asset_bundle", manifest)
                return
            target_peer = next(
                (item for item in self._peers.values() if item.authenticated and item.name == target),
                None,
            )
            if target_peer is None:
                raise Violation("asset bundle target is not in the room")
            try:
                wire = asset_bundle.pack(manifest)
            except asset_bundle.AssetBundleError as exc:
                raise Violation(str(exc)) from exc
            target_peer.enqueue(
                protocol.encode(Frame(type=MessageType.ASSET_BUNDLE, channel=Channel.BULK, payload=wire)),
                critical=True,
            )
            self._emit("asset_bundle_sent", {"sender": peer.name, "target": target, **asset_bundle.summary(manifest)})
            eventlog.log(
                "info",
                "ASSET_BUNDLE_RELAY",
                "asset bundle relayed",
                sender=peer.name,
                target=target,
                objects=len(manifest.get("objects", [])),
                images=len(manifest.get("images", [])),
            )
            return

        if frame.type == MessageType.TRANSFORM:
            uid, values = decode_transform(frame.payload)
            if not self._may_edit(peer, uid, now):
                return
            self.bump_rev(uid)
            self._broadcast(frame, exclude=peer.peer_id, critical=False)
            self._emit("transform", {"uuid": uid, "matrix": values})
            return

        if frame.type == MessageType.POSE:
            body = self._json(frame, 512 * 1024)
            try:
                uid = str(uuidlib.UUID(str(body.get("uuid"))))
            except ValueError as exc:
                raise Violation("invalid armature id") from exc
            bones = body.get("bones")
            if not isinstance(bones, list) or len(bones) > 256:
                raise Violation("invalid pose payload")
            clean = []
            for item in bones:
                if not isinstance(item, dict):
                    raise Violation("invalid pose bone")
                name = clean_text(item.get("name"), 64)
                loc = item.get("l")
                quat = item.get("q")
                scale = item.get("s")
                if not name or not isinstance(loc, list) or not isinstance(quat, list) or not isinstance(scale, list):
                    raise Violation("invalid pose bone")
                if len(loc) != 3 or len(quat) != 4 or len(scale) != 3:
                    raise Violation("invalid pose bone")
                values = [float(v) for v in (*loc, *quat, *scale)]
                if not all(math.isfinite(v) and abs(v) < 1e6 for v in values):
                    raise Violation("pose value is invalid")
                clean.append({"name": name, "l": values[:3], "q": values[3:7], "s": values[7:10]})
            if not self._may_edit(peer, uid, now):
                return
            self.bump_rev(uid)
            message = {"uuid": uid, "bones": clean}
            self._broadcast(protocol.json_frame(MessageType.POSE, message), exclude=peer.peer_id, critical=False)
            self._emit("pose", message)
            return

        if frame.type == MessageType.SHAPE_KEYS:
            body = self._json(frame, 128 * 1024)
            try:
                uid = str(uuidlib.UUID(str(body.get("uuid"))))
            except ValueError as exc:
                raise Violation("invalid shape-key object id") from exc
            values = body.get("values")
            if not isinstance(values, dict) or len(values) > 128:
                raise Violation("invalid shape-key values")
            clean = {}
            for key, value in values.items():
                if not isinstance(key, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise Violation("invalid shape-key value")
                number = float(value)
                if not math.isfinite(number) or not -1000.0 <= number <= 1000.0:
                    raise Violation("shape-key value is out of range")
                clean[key[:64]] = number
            if not self._may_edit(peer, uid, now):
                return
            self.bump_rev(uid)
            message = {"uuid": uid, "values": clean}
            self._broadcast(protocol.json_frame(MessageType.SHAPE_KEYS, message), exclude=peer.peer_id)
            self._emit("shape_keys", message)
            return

        if frame.type == MessageType.ACTION:
            body = self._json(frame, 16 * 1024 * 1024)
            try:
                uid = str(uuidlib.UUID(str(body.get("uuid"))))
            except ValueError as exc:
                raise Violation("invalid action object id") from exc
            action = body.get("action")
            nla = body.get("nla")
            if action is not None and not isinstance(action, dict):
                raise Violation("invalid action payload")
            if nla is not None and not isinstance(nla, dict):
                raise Violation("invalid NLA payload")
            if not self._may_edit(peer, uid, now):
                return
            self.bump_rev(uid)
            message = {"uuid": uid, "action": action}
            if "nla" in body:
                message["nla"] = nla
            self._broadcast(protocol.json_frame(MessageType.ACTION, message), exclude=peer.peer_id)
            self._emit("action", message)
            return

        if frame.type in (MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT):
            uid, rev, payload = decode_mesh(frame.payload)
            if not self._may_edit(peer, uid, now):
                return
            self._validate_mesh(frame.type, payload)
            self.bump_rev(uid)
            self._broadcast(frame, exclude=peer.peer_id, critical=True)
            kind = "mesh_delta" if frame.type == MessageType.MESH_DELTA else "mesh_snapshot"
            self._emit(kind, {"uuid": uid, "rev": rev, "payload": payload})
            return

        if frame.type == MessageType.LEASE:
            self._handle_lease(peer, frame, now)
            return

        if frame.type == MessageType.LASER:
            payload = frame.payload
            if len(payload) < 11:
                raise Violation("laser frame is truncated")
            name_len = payload[0]
            if name_len == 0 or name_len > MAX_NAME:
                raise Violation("invalid laser name")
            offset = 1 + name_len
            if len(payload) < offset + 2:
                raise Violation("laser frame is truncated")
            (count,) = struct.unpack_from("<H", payload, offset)
            if count == 0 or count > 64 or len(payload) != offset + 2 + count * 12:
                raise Violation("invalid laser point count")
            points = []
            for index in range(count):
                values = list(struct.unpack_from("<3f", payload, offset + 2 + index * 12))
                if not all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    raise Violation("laser point is not finite")
                points.append(values)
            name = peer.name.encode("utf-8")[:MAX_NAME]
            forwarded = bytes([len(name)]) + name + payload[offset:]
            self._broadcast(
                Frame(type=MessageType.LASER, channel=Channel.PRESENCE, payload=forwarded),
                exclude=peer.peer_id,
                critical=False,
            )
            self._emit("laser", {"name": peer.name, "points": points})
            return

        if frame.type == MessageType.SELECTION:
            payload = frame.payload
            if len(payload) < 27:
                raise Violation("selection frame is truncated")
            name_len = payload[0]
            if name_len == 0 or name_len > MAX_NAME:
                raise Violation("invalid selection name")
            offset = 1 + name_len
            if len(payload) < offset + 26:
                raise Violation("selection frame is truncated")
            uid = str(uuidlib.UUID(bytes=payload[offset : offset + 16]))
            kind = payload[offset + 16]
            rev, count = struct.unpack_from("<II", payload, offset + 18)
            if kind > 2 or count > 200_000:
                raise Violation("invalid selection")
            expected = (count + 7) // 8
            mask = payload[offset + 26 :]
            if len(mask) != expected:
                raise Violation("selection mask length mismatch")
            name = peer.name.encode("utf-8")[:MAX_NAME]
            forwarded = bytes([len(name)]) + name + payload[offset:]
            self._broadcast(
                Frame(type=MessageType.SELECTION, channel=Channel.PRESENCE, payload=forwarded),
                exclude=peer.peer_id,
                critical=False,
            )
            self._emit(
                "selection",
                {
                    "name": peer.name,
                    "uuid": uid,
                    "kind": kind,
                    "rev": rev,
                    "count": count,
                    "mask": mask,
                },
            )
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

        if frame.type == MessageType.CHUNK:
            try:
                result, progress = self._incoming.add(peer.name, frame.payload)
            except transfer.TransferError as exc:
                raise Violation(str(exc)) from exc
            self._emit("transfer_progress", {**progress, "direction": "receive", "sender": peer.name})
            if result is None:
                return
            if result.get("kind") != transfer.KIND_OBJECT and result.get("kind") != transfer.KIND_IMAGE:
                raise Violation("unsupported chunked transfer kind")
            if result.get("kind") == transfer.KIND_IMAGE:
                try:
                    body = image_payload.decode(result["data"])
                except image_payload.ImagePayloadError as exc:
                    raise Violation(str(exc)) from exc
                body["name"] = clean_name(body.get("name"), "Image")
                self._emit("transfer_done", {"direction": "receive", "sender": peer.name, "id": progress.get("id")})
                try:
                    _identity, chunks = transfer.split(result["data"], transfer.KIND_IMAGE)
                except transfer.TransferError as exc:
                    raise Violation(str(exc)) from exc
                for chunk in chunks:
                    self._broadcast(
                        Frame(type=MessageType.CHUNK, channel=Channel.BULK, payload=chunk),
                        exclude=peer.peer_id,
                    )
                self._emit("image_asset", body)
                return
            try:
                body = object_payload.decode_create(result["data"])
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
            self._emit("transfer_done", {"direction": "receive", "sender": peer.name, "id": progress.get("id")})
            try:
                _identity, chunks = transfer.split(result["data"], transfer.KIND_OBJECT)
            except transfer.TransferError as exc:
                raise Violation(str(exc)) from exc
            for chunk in chunks:
                self._broadcast(
                    Frame(type=MessageType.CHUNK, channel=Channel.BULK, payload=chunk),
                    exclude=peer.peer_id,
                )
            self._emit("object_create", body)
            eventlog.log(
                "info",
                "CHUNKED_OBJECT",
                "chunked object relayed",
                sender=peer.name,
                bytes=len(result["data"]),
                chunks=len(chunks),
            )
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
            self._revs[uid] = self._revs.get(uid, 0) + 1
            self._broadcast(frame, exclude=peer.peer_id)
            self._emit("object_delete", {"uuid": uid})
            return

        if frame.type == MessageType.OBJECT_RENAME:
            body = self._json(frame, 1024)
            uid = body.get("uuid")
            name = clean_text(body.get("name"), MAX_NAME)
            if not isinstance(uid, str) or not name:
                raise Violation("invalid object rename")
            if not self._may_edit(peer, uid, now):
                return
            self.bump_rev(uid)
            message = {"uuid": uid, "name": name}
            self._broadcast(
                protocol.json_frame(MessageType.OBJECT_RENAME, message),
                exclude=peer.peer_id,
            )
            self._emit("object_rename", message)
            return

        if frame.type == MessageType.MATERIAL_UPDATE:
            body = self._json(frame, 64 * 1024)
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
            self.bump_rev(uid)
            self._broadcast(protocol.json_frame(MessageType.MATERIAL_UPDATE, message), exclude=peer.peer_id)
            self._emit("material_update", message)
            return

        if frame.type == MessageType.MODIFIER_STACK:
            body = self._json(frame, 64 * 1024)
            message = self._sanitize_modifiers(body)
            if message is None:
                raise Violation("invalid modifier stack")
            self.bump_rev(message["uuid"])
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

    async def _reject_hello(self, peer: Peer, message: str, code: str = ErrorCode.UPDATE_REQUIRED.value) -> None:
        """Write a rejection before the peer is dropped.

        A queued frame may not flush before the connection closes, so write
        directly. The client shows this message instead of retrying blindly.
        """
        try:
            peer.writer.write(
                protocol.encode(
                    protocol.json_frame(
                        MessageType.ERROR,
                        {"message": message[:200], "code": code},
                    )
                )
            )
            await peer.writer.drain()
        except Exception:  # noqa: BLE001
            pass

    async def _accept_hello(self, peer: Peer, frame: Frame) -> None:
        if peer.authenticated:
            raise Violation("duplicate hello")
        body = self._json(frame, 4096)
        if self.pin:
            ip = peer.address.rsplit(":", 1)[0] if ":" in peer.address else peer.address
            now = time.monotonic()
            attempts = [stamp for stamp in self._pin_attempts.get(ip, []) if now - stamp < 60.0]
            if len(attempts) >= 5:
                self._pin_attempts[ip] = attempts
                await self._reject_hello(peer, "too many password attempts; wait a minute", ErrorCode.AUTH_FAILED.value)
                raise Violation("pin rate limited")
            supplied = body.get("pin")
            candidate = (
                hashlib.scrypt(str(supplied)[:64].encode("utf-8"), salt=self._pin_salt, n=2**14, r=8, p=1, dklen=32)
                if isinstance(supplied, str)
                else b""
            )
            if not candidate or not hmac.compare_digest(candidate, self._pin_hash):
                attempts.append(now)
                self._pin_attempts[ip] = attempts
                await self._reject_hello(peer, "invalid server password", ErrorCode.AUTH_FAILED.value)
                raise Violation("invalid pin")
            self._pin_attempts.pop(ip, None)
        peer.app = clean_text(body.get("app"), 32)
        peer.addon = clean_text(body.get("addon"), 32)
        protocol_value = body.get("protocol")
        if isinstance(protocol_value, int) and not isinstance(protocol_value, bool):
            peer.protocol = max(0, min(65535, protocol_value))
        caps_value = body.get("caps")
        if isinstance(caps_value, int) and not isinstance(caps_value, bool):
            peer.caps = max(0, caps_value)
        if peer.protocol == 0 or not peer.app:
            await self._reject_hello(
                peer,
                f"update Blendermp: reinstall the latest {self.version} build to join",
                ErrorCode.UPDATE_REQUIRED.value,
            )
            raise Violation("legacy client")
        if peer.protocol != PROTOCOL_VERSION:
            await self._reject_hello(
                peer,
                f"protocol mismatch: host uses {PROTOCOL_VERSION}, client uses {peer.protocol}",
                ErrorCode.PROTOCOL.value,
            )
            raise Violation("protocol mismatch")
        if not compatible(self.version, peer.app):
            await self._reject_hello(
                peer,
                f"update Blendermp: host is {self.version}, client is {peer.app}",
                ErrorCode.UPDATE_REQUIRED.value,
            )
            raise Violation("version mismatch")
        existing = {other.name for other in self._peers.values() if other.authenticated} | {self.name}
        now = time.monotonic()
        resume = body.get("resume")
        if isinstance(resume, dict) and self.session_id:
            token = clean_text(resume.get("token"), 64)
            entry = self._recent.get(token) if token else None
            if (
                entry is not None
                and resume.get("session_id") == self.session_id
                and now - float(entry.get("last_seen", 0.0)) <= RESUME_TTL
                and entry.get("name") not in existing
            ):
                peer.name = str(entry["name"])
                peer.color = tuple(entry.get("color", peer.color))
                peer.role = int(entry.get("role", int(Role.EDITOR)))
                peer.resume_token = token
                peer.authenticated = True
                if peer.lan:
                    self._lan_peer_count += 1
                self._peers[peer.peer_id] = peer
                entry["last_seen"] = now
                entry["peer_id"] = peer.peer_id
                self._recent[token] = entry
                peers = [
                    {"name": other.name, "color": list(other.color), "role": int(other.role)}
                    for other in self._peers.values()
                    if other.authenticated and other.peer_id != peer.peer_id
                ]
                welcome = {
                    "peer_id": peer.peer_id,
                    "name": self.name,
                    "you": peer.name,
                    "color": list(self.color),
                    "role": int(peer.role),
                    "peers": peers,
                    "fingerprint": self.fingerprint,
                    "code": self.code,
                    "leases": self._leases.snapshot(now),
                    "app": self.version,
                    "addon": self.version,
                    "protocol": PROTOCOL_VERSION,
                    "caps": protocol.DEFAULT_CAPABILITIES,
                    "revs": dict(self._revs),
                    "resumed": True,
                    "session_id": self.session_id,
                    "resume_token": token,
                    "identity": self._identity_body(),
                }
                peer.enqueue(protocol.encode(protocol.json_frame(MessageType.WELCOME, welcome)), critical=True)
                self._broadcast(
                    protocol.json_frame(
                        MessageType.PRESENCE,
                        {"name": peer.name, "color": list(peer.color), "joined": True, "role": int(peer.role), "scene": "", "frame": 0, "mode": ""},
                    ),
                    exclude=peer.peer_id,
                )
                self._emit("peer_resume", {"name": peer.name, "peer_id": peer.peer_id})
                return
        peer.name = clean_name(body.get("name"))
        # Names are identity for leases, so the host makes them unique.
        existing = {other.name for other in self._peers.values() if other.authenticated} | {self.name}
        if peer.name in existing:
            peer.name = f"{peer.name[:40]}-{peer.peer_id[:4]}"
        peer.color = clean_color(body.get("color"))
        peer.authenticated = True
        if peer.lan:
            self._lan_peer_count += 1
        self._peers[peer.peer_id] = peer
        peer.resume_token = secrets.token_urlsafe(24)
        self._recent[peer.resume_token] = {
            "name": peer.name,
            "color": list(peer.color),
            "role": int(peer.role),
            "peer_id": peer.peer_id,
            "last_seen": now,
            "leases": [],
        }

        peers = [
            {"name": other.name, "color": list(other.color), "role": int(other.role)}
            for other in self._peers.values()
            if other.authenticated and other.peer_id != peer.peer_id
        ]
        welcome = {
            "peer_id": peer.peer_id,
            "name": self.name,
            "you": peer.name,
            "color": list(self.color),
            "role": int(peer.role),
            "peers": peers,
            "fingerprint": self.fingerprint,
            "code": self.code,
            "leases": self._leases.snapshot(time.monotonic()),
            "app": self.version,
            "addon": self.version,
            "protocol": PROTOCOL_VERSION,
            "caps": protocol.DEFAULT_CAPABILITIES,
            "revs": dict(self._revs),
            "resumed": False,
            "session_id": self.session_id,
            "resume_token": peer.resume_token,
            "identity": self._identity_body(),
        }
        peer.enqueue(protocol.encode(protocol.json_frame(MessageType.WELCOME, welcome)), critical=True)
        self._broadcast(
            protocol.json_frame(
                MessageType.PRESENCE,
                {"name": peer.name, "color": list(peer.color), "joined": True, "role": int(peer.role), "scene": "", "frame": 0, "mode": ""},
            ),
            exclude=peer.peer_id,
        )
        self._emit("peer_join", {"name": peer.name, "color": list(peer.color)})

    def _handle_lease(self, peer: Peer, frame: Frame, now: float) -> None:
        body = self._json(frame, 4096)
        key = body.get("key")
        action = body.get("action")
        if action in {"acquire", "grant", "deny"} and not can_edit(peer.role):
            peer.enqueue(
                protocol.encode(
                    protocol.json_frame(
                        MessageType.ERROR,
                        {"message": "your role does not allow editing", "code": ErrorCode.ROLE_DENIED.value},
                    )
                ),
                critical=True,
            )
            return
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
                    requested = {
                        "key": key,
                        "owner": owner,
                        "action": "requested",
                        "requester": peer.name,
                        "leases": self._leases.snapshot(now),
                    }
                    if owner == self.name:
                        self._emit("lease", requested)
                    for other in self._peers.values():
                        if other.name == owner:
                            other.enqueue(
                                protocol.encode(
                                    protocol.json_frame(MessageType.LEASE, requested)
                                ),
                                critical=True,
                            )
                            break
                return
            self._expiry_notified.discard(key)
        elif action == "grant":
            requester = clean_name(body.get("requester"), "")
            if not requester or self._leases.owner_of(key, now) != peer.name:
                raise Violation("only the lease owner can grant it")
            if not self._leases.resolve_request(key, requester, grant=True, now=now):
                self._leases.force(key, requester, now)
            self._expiry_notified.discard(key)
            snapshot = self._leases.snapshot(now)
            self._broadcast(
                protocol.json_frame(
                    MessageType.LEASE, {"key": key, "owner": requester, "action": "grant", "leases": snapshot}
                )
            )
            self._emit_leases()
            return
        elif action == "deny":
            requester = clean_name(body.get("requester"), "")
            if not requester or self._leases.owner_of(key, now) != peer.name:
                raise Violation("only the lease owner can deny a request")
            self._leases.resolve_request(key, requester, grant=False, now=now)
            self._broadcast(
                protocol.json_frame(
                    MessageType.LEASE,
                    {
                        "key": key,
                        "owner": peer.name,
                        "requester": requester,
                        "action": "denied_request",
                        "leases": self._leases.snapshot(now),
                    },
                )
            )
            self._emit_leases()
            return
        elif action == "release":
            self._leases.release(key, peer.name)
            self._expiry_notified.discard(key)
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
        self._emit_leases()

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
            self._emit_leases()
            return True
        if owner == peer.name:
            self._leases.renew(key, peer.name, now)
            self._expiry_notified.discard(key)
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
            elif chunker.is_geometry(payload):
                chunker.validate_geometry(payload, MAX_VERTICES)
            else:
                chunker.validate_positions(payload, MAX_VERTICES)
        except ValueError as exc:
            raise Violation(str(exc)) from exc

    def _sanitize_object_details(self, value) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            return None
        details: dict[str, Any] = {}
        for key in ("hide_viewport", "hide_render", "show_in_front"):
            details[key] = bool(value.get(key, False))
        color = value.get("color")
        if color is not None:
            if not isinstance(color, list) or len(color) != 4:
                return None
            try:
                details["color"] = [max(0.0, min(1.0, float(component))) for component in color]
            except (TypeError, ValueError):
                return None
        parent = value.get("parent")
        if parent is not None:
            try:
                details["parent"] = str(uuidlib.UUID(str(parent)))
            except ValueError:
                return None
        inverse = value.get("matrix_parent_inverse")
        if inverse is not None:
            if not isinstance(inverse, list) or len(inverse) != 16:
                return None
            try:
                values = [float(component) for component in inverse]
            except (TypeError, ValueError):
                return None
            if not all(math.isfinite(component) and abs(component) <= 1e12 for component in values):
                return None
            details["matrix_parent_inverse"] = values
        collections = value.get("collections")
        if collections is not None:
            if not isinstance(collections, list) or len(collections) > 16:
                return None
            items = []
            for item in collections:
                if not isinstance(item, dict):
                    return None
                name = clean_text(item.get("name"), 64)
                if not name:
                    return None
                items.append(
                    {
                        "name": name,
                        "hide_viewport": bool(item.get("hide_viewport", False)),
                        "hide_render": bool(item.get("hide_render", False)),
                        "hide_select": bool(item.get("hide_select", False)),
                    }
                )
            details["collections"] = items
        constraints = value.get("constraints")
        if constraints is not None:
            if not isinstance(constraints, list) or len(constraints) > 32:
                return None
            items = []
            for item in constraints:
                if not isinstance(item, dict):
                    return None
                type_ = clean_text(item.get("type"), 32)
                name = clean_text(item.get("name"), 64)
                if not type_ or not name:
                    return None
                entry = {
                    "type": type_,
                    "name": name,
                    "influence": max(0.0, min(1.0, float(item.get("influence", 1.0)))),
                    "mute": bool(item.get("mute", False)),
                }
                target = item.get("target")
                if target is not None:
                    try:
                        entry["target"] = str(uuidlib.UUID(str(target)))
                    except ValueError:
                        return None
                subtarget = clean_text(item.get("subtarget"), 64)
                if subtarget:
                    entry["subtarget"] = subtarget
                items.append(entry)
            details["constraints"] = items
        return details

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
        message = {"uuid": uid, "modifiers": result}
        raw_details = body.get("details")
        details = self._sanitize_object_details(raw_details)
        if raw_details is not None and details is None:
            return None
        if details is not None:
            message["details"] = details
        return message

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

    def _frame_uid(self, frame: Frame) -> str | None:
        if frame.type in (MessageType.TRANSFORM, MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT, MessageType.OBJECT_CREATE):
            if len(frame.payload) < 16:
                return None
            try:
                return str(uuidlib.UUID(bytes=frame.payload[:16]))
            except ValueError:
                return None
        if frame.type in (
            MessageType.MATERIAL_UPDATE,
            MessageType.MODIFIER_STACK,
            MessageType.POSE,
            MessageType.SHAPE_KEYS,
            MessageType.ACTION,
        ):
            try:
                body = json.loads(frame.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            uid = body.get("uuid") if isinstance(body, dict) else None
            return uid if isinstance(uid, str) else None
        return None

    def _peer_accepts(self, peer: Peer, frame: Frame, now: float) -> bool:
        if frame.type == MessageType.PRESENCE:
            # Presence has no object UID. LAN peers get every delta. Relay
            # peers get camera-only deltas at 5 Hz, but state changes (frame,
            # selection, active object, cursor, mode) go through immediately so
            # a real update is never dropped by the throttle.
            if peer.lan:
                return True
            try:
                body = json.loads(frame.payload.decode("utf-8"))
                sender = str(body.get("name", ""))[:48]
            except (UnicodeDecodeError, json.JSONDecodeError):
                sender = ""
                body = {}
            if any(
                key in body
                for key in (
                    "frame",
                    "playing",
                    "selected",
                    "active",
                    "mode",
                    "scene",
                    "scene_hash",
                    "cursor",
                )
            ):
                return True
            key = ("presence", sender)
            if now - peer.last_sent.get(key, 0.0) < 0.1:
                return False
            peer.last_sent[key] = now
            return True
        uid = self._frame_uid(frame)
        if uid is None:
            return True
        if peer.subscriptions.get(uid) == "mute":
            return False
        if peer.lan:
            # Direct LAN: never rate-limit by interest or quality. Send every
            # transform/mesh frame the sender produces, as fast as the socket
            # allows.
            return True
        if frame.type in (MessageType.OBJECT_CREATE, MessageType.OBJECT_DELETE, MessageType.SCENE_RESET):
            return True
        priority = int(peer.priorities.get(uid, 0))
        if priority < 0:
            priority = 0
        if priority > 2:
            priority = 2
        if frame.type == MessageType.TRANSFORM:
            base = (1.0 / 30.0, 1.0 / 15.0, 1.0 / 8.0)[priority]
            scale = (1.0, 1.2, 1.5)[peer.quality]
        elif frame.type in (MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT):
            base = (1.0 / 20.0, 1.0 / 5.0, 1.0)[priority]
            scale = (1.0, 2.0, 4.0)[peer.quality]
        else:
            base = (1.0 / 30.0, 1.0 / 10.0, 1.0 / 2.0)[priority]
            scale = (1.0, 2.0, 4.0)[peer.quality]
        key = (uid, int(frame.type))
        if now - peer.last_sent.get(key, 0.0) < base * scale:
            return False
        peer.last_sent[key] = now
        return True

    def _broadcast(self, frame: Frame, exclude: str | None = None, critical: bool = True) -> None:
        data = protocol.encode(frame)
        now = time.monotonic()
        for peer in list(self._peers.values()):
            if peer.peer_id == exclude or not peer.authenticated:
                continue
            if not self._peer_accepts(peer, frame, now):
                uid = self._frame_uid(frame)
                if uid is not None and peer.subscriptions.get(uid) != "mute":
                    key = (uid, int(frame.type))
                    delay = 0.15 if frame.type in (MessageType.TRANSFORM, MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT) else 0.5
                    peer.pending[key] = (data, now + delay, critical)
                continue
            try:
                peer.enqueue(data, critical=critical)
            except Violation:
                asyncio.create_task(self._drop_peer(peer))

    def set_role(self, name: str, role: int) -> None:
        """Ask the host loop to set a peer's role."""
        loop = self._loop
        if self.is_host and loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._set_role(name, int(role))))

    async def _set_role(self, name: str, role: int) -> None:
        allowed = {
            int(Role.VIEWER),
            int(Role.COMMENTER),
            int(Role.EDITOR),
            int(Role.ADMIN),
        }
        if role not in allowed:
            return
        target = next(
            (peer for peer in self._peers.values() if peer.authenticated and peer.name == name),
            None,
        )
        if target is None:
            return
        target.role = role
        if not can_edit(role):
            now = time.monotonic()
            released = False
            for key in self._leases.held_by(name, now):
                if self._leases.release(key, name):
                    self._expiry_notified.discard(key)
                    released = True
            if released:
                self._broadcast(
                    protocol.json_frame(
                        MessageType.LEASE,
                        {"action": "release", "leases": self._leases.snapshot(now)},
                    )
                )
                self._emit_leases()
        self._broadcast(
            protocol.json_frame(MessageType.ROLE, {"name": name, "role": role})
        )
        self._emit("role", {"name": name, "role": role})
        eventlog.log("info", "ROLE_CHANGE", "role changed", target=name, role=int(role))

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
        target.kicked = True
        self._recent.pop(target.resume_token, None)
        eventlog.log("warn", "KICK", "peer kicked", target=name)
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
        if was_authenticated and peer.lan:
            self._lan_peer_count = max(0, self._lan_peer_count - 1)
        peer.authenticated = False
        peer.closed = True
        if peer.writer_task:
            peer.writer_task.cancel()
        self._peers.pop(peer.peer_id, None)
        if was_authenticated:
            now = time.monotonic()
            held = self._leases.held_by(peer.name, now)
            retain = bool(peer.resume_token) and not peer.kicked
            if retain:
                entry = self._recent.get(peer.resume_token)
                if entry is not None:
                    entry["last_seen"] = now
                    entry["peer_id"] = peer.peer_id
                    entry["role"] = int(peer.role)
                    entry["color"] = list(peer.color)
                    entry["leases"] = held
                    self._recent[peer.resume_token] = entry
            if not retain:
                self._recent.pop(peer.resume_token, None)
                released = False
                for key in held:
                    if self._leases.release(key, peer.name):
                        self._expiry_notified.discard(key)
                        released = True
                if released:
                    self._broadcast(
                        protocol.json_frame(
                            MessageType.LEASE,
                            {"action": "release", "leases": self._leases.snapshot(now)},
                        )
                    )
                    self._emit_leases()
            self._emit("peer_leave", {"name": peer.name, "temporary": retain})
            self._broadcast(protocol.json_frame(MessageType.BYE, {"name": peer.name}))
        try:
            await peer.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ client
    async def _client_loop(self) -> None:
        delay = 0.5
        while self._stop_event and not self._stop_event.is_set():
            try:
                await self._connect_once()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.failed:
                    return
                if not self._ever_connected:
                    self._emit("error", {"message": str(exc)[:200], "code": ErrorCode.PROTOCOL.value})
            if self._stop_event.is_set():
                return
            if self._ever_connected:
                self._set_state("reconnecting")
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def _connect_once(self) -> None:
        try:
            info = decode_room(self.code)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"invalid room code: {exc}")
            return
        direct_info = None
        if self.direct_code:
            try:
                direct_info = decode_room(self.direct_code)
            except Exception:  # noqa: BLE001
                direct_info = None

        self._set_state("joining")
        context = tls.client_context()
        use_relay = bool(
            self.relay_mode
            and self._relay_client is not None
            and (not self.direct_first or self._direct_failed)
        )
        if use_relay:
            host, port = "127.0.0.1", self._relay_client.port
        elif self.direct_first and direct_info is not None:
            host, port = direct_info.ip, direct_info.port
        else:
            host, port = info.ip, info.port
        try:
            if self.direct_first and not use_relay:
                # LAN is preferred; give it a short window before falling back
                # to the relay so a blocked direct port cannot hang the join.
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host,
                        port,
                        ssl=context,
                        server_hostname="blendermp",
                        limit=MAX_MESH + HEADER_SIZE,
                    ),
                    3.0,
                )
            else:
                reader, writer = await asyncio.open_connection(
                    host,
                    port,
                    ssl=context,
                    server_hostname="blendermp",
                    limit=MAX_MESH + HEADER_SIZE,
                )
        except (OSError, asyncio.TimeoutError) as exc:
            if self.direct_first and not use_relay:
                self._direct_failed = True
            raise RuntimeError(f"cannot reach host: {exc}") from exc
        self._using_relay = use_relay
        self.compress_enabled = use_relay
        compression.set_enabled(use_relay)

        actual = tls.peer_fingerprint(writer)
        expected = self.expected_fingerprint or tls.known_fingerprint(control.data_dir(), self.code)
        if expected:
            if not tls.verify_fingerprint(actual, expected):
                writer.close()
                self._emit(
                    "cert_changed",
                    {
                        "fingerprint": actual or "",
                        "expected": expected,
                        "short": tls.short_fingerprint(actual or ""),
                    },
                )
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
        hello = {
            "name": self.name,
            "color": list(self.color),
            "pin": self.pin,
            "app": self.version,
            "addon": self.version,
            "protocol": PROTOCOL_VERSION,
            "caps": protocol.DEFAULT_CAPABILITIES,
        }
        if self.session_id and self.resume_token:
            hello["resume"] = {"session_id": self.session_id, "token": self.resume_token}
        writer.write(protocol.encode(protocol.json_frame(MessageType.HELLO, hello)))
        await writer.drain()

        frame = await asyncio.wait_for(self._read_frame(reader), timeout=8.0)
        if frame is not None and frame.type == MessageType.ERROR:
            body = self._json(frame, 1024)
            message = clean_text(body.get("message"), 200) or "host rejected the join"
            code = clean_text(body.get("code"), 48) or ErrorCode.UPDATE_REQUIRED.value
            self.failed = True
            self.failure_reason = message
            self.failure_code = code
            self._emit("error", {"message": message, "code": code})
            self._request_stop()
            return
        if frame is None or frame.type != MessageType.WELCOME:
            raise RuntimeError("host rejected the join")
        body = self._json(frame, 4096)
        host_app = clean_text(body.get("app"), 32)
        host_protocol = body.get("protocol")
        if not host_app or not isinstance(host_protocol, int) or isinstance(host_protocol, bool) or host_protocol == 0:
            writer.close()
            self._fail("host is running an older Blendermp build; the host must reinstall the latest setup")
            return
        if host_protocol != PROTOCOL_VERSION:
            writer.close()
            self._fail(
                f"host uses Blendermp protocol {host_protocol}; update Blendermp to {self.version}"
            )
            return
        if not compatible(self.version, host_app):
            writer.close()
            self._fail(
                f"host is on Blendermp {host_app}; update to {self.version} to join"
            )
            return
        identity_body = body.get("identity")
        if not isinstance(identity_body, dict):
            writer.close()
            self._fail("host did not present a signed identity; update Blendermp on the host")
            return
        public_key = clean_text(identity_body.get("public_key"), 64)
        signature = clean_text(identity_body.get("signature"), 256)
        descriptor = identity_body.get("descriptor")
        if not public_key or not signature or not isinstance(descriptor, dict):
            writer.close()
            self._fail("host identity is incomplete")
            return
        if not identity.verify(public_key, descriptor, signature):
            writer.close()
            self._fail("host identity signature is invalid")
            return
        if (
            normalize(str(descriptor.get("code", ""))) != normalize(self.code)
            or descriptor.get("cert_fingerprint") != (actual or "")
            or descriptor.get("protocol") != PROTOCOL_VERSION
            or not compatible(self.version, str(descriptor.get("app", "")))
        ):
            writer.close()
            self._fail("host identity does not match this session")
            return
        self.identity_public = public_key
        self.remote_app = host_app
        caps_value = body.get("caps")
        if isinstance(caps_value, int) and not isinstance(caps_value, bool):
            self.remote_caps = max(0, caps_value)
        role_value = body.get("role")
        if isinstance(role_value, int) and not isinstance(role_value, bool) and 0 <= role_value <= 4:
            self.role = role_value
            self._emit("role", {"name": self.name, "role": role_value})
        self.session_id = clean_text(body.get("session_id"), 64) or self.session_id
        self.resume_token = clean_text(body.get("resume_token"), 64) or self.resume_token
        resumed = bool(body.get("resumed")) and self._ever_connected
        self._ever_connected = True
        if resumed:
            body["resumed"] = True
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
            presence: dict[str, Any] = {
                "name": clean_text(body.get("name"), MAX_NAME),
                "color": clean_color(body.get("color")),
            }
            for key, limit in (
                ("scene", 64),
                ("mode", 32),
                ("active", 64),
                ("selected", 512),
                ("scene_hash", 32),
            ):
                if key in body:
                    presence[key] = clean_text(body.get(key), limit)
            if "frame" in body and isinstance(body.get("frame"), (int, float)) and not isinstance(body.get("frame"), bool):
                presence["frame"] = int(body["frame"])
            if "playing" in body:
                presence["playing"] = bool(body.get("playing"))
            if "fps" in body and isinstance(body.get("fps"), (int, float)) and not isinstance(body.get("fps"), bool):
                presence["fps"] = max(1, min(240, int(body["fps"])))
            role_value = body.get("role")
            if isinstance(role_value, int) and not isinstance(role_value, bool) and 0 <= role_value <= 4:
                presence["role"] = role_value
            ping_value = body.get("ping_ms")
            if isinstance(ping_value, (int, float)) and not isinstance(ping_value, bool):
                presence["ping_ms"] = max(0.0, min(5000.0, float(ping_value)))
            cursor = body.get("cursor")
            if isinstance(cursor, list) and len(cursor) == 3:
                try:
                    values = [float(value) for value in cursor]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 3 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    presence["cursor"] = values
            view = body.get("view")
            if isinstance(view, list) and len(view) == 16:
                try:
                    values = [float(value) for value in view]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                    presence["view"] = values
            corners = body.get("view_corners")
            if isinstance(corners, list) and len(corners) == 21:
                try:
                    values = [float(value) for value in corners]
                except (TypeError, ValueError):
                    values = []
                if len(values) == 21 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    presence["view_corners"] = values
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

        if frame.type == MessageType.POSE:
            body = self._json(frame, 512 * 1024)
            self._emit("pose", body)
            return

        if frame.type == MessageType.SHAPE_KEYS:
            body = self._json(frame, 128 * 1024)
            self._emit("shape_keys", body)
            return

        if frame.type == MessageType.ACTION:
            body = self._json(frame, 16 * 1024 * 1024)
            self._emit("action", body)
            return

        if frame.type in (MessageType.MESH_DELTA, MessageType.MESH_SNAPSHOT):
            uid, rev, payload = decode_mesh(frame.payload)
            kind = "mesh_delta" if frame.type == MessageType.MESH_DELTA else "mesh_snapshot"
            self._emit(kind, {"uuid": uid, "rev": rev, "payload": payload})
            return

        if frame.type == MessageType.ROLE:
            body = self._json(frame, 1024)
            role = body.get("role")
            if isinstance(role, int) and not isinstance(role, bool) and 0 <= role <= 4:
                self._emit(
                    "role",
                    {
                        "name": clean_text(body.get("name"), MAX_NAME),
                        "role": role,
                    },
                )
            return

        if frame.type == MessageType.LASER:
            payload = frame.payload
            if len(payload) < 11:
                return
            name_len = payload[0]
            if name_len == 0 or name_len > MAX_NAME:
                return
            offset = 1 + name_len
            if len(payload) < offset + 2:
                return
            (count,) = struct.unpack_from("<H", payload, offset)
            if count == 0 or count > 64 or len(payload) != offset + 2 + count * 12:
                return
            points = []
            for index in range(count):
                values = list(struct.unpack_from("<3f", payload, offset + 2 + index * 12))
                if not all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    return
                points.append(values)
            self._emit(
                "laser",
                {
                    "name": clean_text(payload[1:offset].decode("utf-8", "ignore"), MAX_NAME),
                    "points": points,
                },
            )
            return

        if frame.type == MessageType.SELECTION:
            payload = frame.payload
            if len(payload) < 27:
                return
            name_len = payload[0]
            if name_len == 0 or name_len > MAX_NAME:
                return
            offset = 1 + name_len
            if len(payload) < offset + 26:
                return
            try:
                uid = str(uuidlib.UUID(bytes=payload[offset : offset + 16]))
            except ValueError:
                return
            kind = payload[offset + 16]
            rev, count = struct.unpack_from("<II", payload, offset + 18)
            mask = payload[offset + 26 :]
            if kind > 2 or count > 200_000 or len(mask) != (count + 7) // 8:
                return
            self._emit(
                "selection",
                {
                    "name": clean_text(payload[1:offset].decode("utf-8", "ignore"), MAX_NAME),
                    "uuid": uid,
                    "kind": kind,
                    "rev": rev,
                    "count": count,
                    "mask": mask,
                },
            )
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

        if frame.type == MessageType.CHUNK:
            try:
                result, progress = self._incoming.add("host", frame.payload)
            except transfer.TransferError:
                return
            self._emit("transfer_progress", {**progress, "direction": "receive", "sender": "host"})
            if result is None:
                return
            if result.get("kind") == transfer.KIND_IMAGE:
                try:
                    body = image_payload.decode(result["data"])
                except image_payload.ImagePayloadError:
                    return
                self._emit("transfer_done", {"direction": "receive", "sender": "host", "id": progress.get("id")})
                self._emit("image_asset", body)
                return
            if result.get("kind") != transfer.KIND_OBJECT:
                return
            try:
                body = object_payload.decode_create(result["data"])
            except object_payload.ObjectPayloadError:
                return
            self._emit("transfer_done", {"direction": "receive", "sender": "host", "id": progress.get("id")})
            self._emit("object_create", body)
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

        if frame.type == MessageType.OBJECT_RENAME:
            body = self._json(frame, 1024)
            uid = body.get("uuid")
            name = clean_text(body.get("name"), MAX_NAME)
            if isinstance(uid, str) and name:
                self._emit("object_rename", {"uuid": uid, "name": name})
            return

        if frame.type == MessageType.MATERIAL_UPDATE:
            body = self._json(frame, 64 * 1024)
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

        if frame.type == MessageType.VIEW_BOOKMARK:
            body = self._json(frame, 8192)
            bookmark = bookmarks.sanitize(body.get("bookmark"))
            if bookmark is not None:
                self._emit(
                    "view_bookmark",
                    {
                        "name": clean_text(body.get("name"), MAX_NAME),
                        "bookmark": bookmark,
                    },
                )
            return

        if frame.type == MessageType.ASSET_BUNDLE:
            try:
                manifest = asset_bundle.decode(frame.payload)
            except asset_bundle.AssetBundleError:
                return
            self._emit("asset_bundle", manifest)
            return

        if frame.type in (MessageType.SCENE_RESET, MessageType.SCENE_READY):
            body = self._json(frame, 512)
            self._emit("scene_reset" if frame.type == MessageType.SCENE_RESET else "scene_ready", body)
            return

        if frame.type == MessageType.RESUME:
            body = self._json(frame, 16 * 1024 * 1024)
            objects = body.get("objects")
            if isinstance(objects, list):
                self._emit("resume_state", {"objects": [str(item) for item in objects[:100000]]})
            return

        if frame.type == MessageType.ERROR:
            body = self._json(frame, 1024)
            self._emit(
                "error",
                {
                    "message": clean_text(body.get("message"), 200),
                    "code": clean_text(body.get("code"), 48) or ErrorCode.PROTOCOL.value,
                },
            )

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
                            if frame.type not in {
                                MessageType.CHUNK,
                                MessageType.OBJECT_CREATE,
                                MessageType.MESH_SNAPSHOT,
                                MessageType.IMAGE_ASSET,
                                MessageType.ASSET_BUNDLE,
                                MessageType.ACTION,
                            }:
                                continue  # drop stale non-structural frames
                            await self._writer.drain()
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
                    for key, (data, due, critical) in list(peer.pending.items()):
                        if now >= due:
                            try:
                                peer.enqueue(data, critical=critical)
                            except Violation:
                                asyncio.create_task(self._drop_peer(peer))
                            peer.pending.pop(key, None)
                result = self._leases.sweep(now)
                expired_tokens = []
                for token, entry in list(self._recent.items()):
                    if now - float(entry.get("last_seen", 0.0)) <= RESUME_TTL:
                        continue
                    for key in entry.get("leases", []) or []:
                        self._leases.release(key, entry.get("name"))
                        self._expiry_notified.discard(key)
                    expired_tokens.append(token)
                if expired_tokens:
                    for token in expired_tokens:
                        self._recent.pop(token, None)
                    self._broadcast(
                        protocol.json_frame(
                            MessageType.LEASE,
                            {"action": "released", "leases": self._leases.snapshot(now)},
                        )
                    )
                    self._emit_leases()
                snapshot = self._leases.snapshot(now)
                if result.released:
                    self._expiry_notified.difference_update(result.released)
                    self._broadcast(
                        protocol.json_frame(
                            MessageType.LEASE,
                            {
                                "action": "released",
                                "keys": list(result.released),
                                "leases": snapshot,
                            },
                        )
                    )
                    self._emit_leases()
                for key in result.expiring:
                    if key in self._expiry_notified:
                        continue
                    self._expiry_notified.add(key)
                    self._broadcast(
                        protocol.json_frame(
                            MessageType.LEASE,
                            {
                                "action": "expiring",
                                "keys": [key],
                                "leases": snapshot,
                            },
                        )
                    )

            expired = self._incoming.sweep(time.monotonic())
            if expired:
                self._emit("transfer_expired", {"count": expired})

    # ------------------------------------------------------------------ public API
    def _send_targeted(self, name: str, frame: Frame, manifest: dict | None = None) -> None:
        peer = next(
            (item for item in self._peers.values() if item.authenticated and item.name == name),
            None,
        )
        if peer is None:
            self._emit("error", {"message": f"{name} is no longer in the room", "code": ErrorCode.PROTOCOL.value})
            return
        try:
            peer.enqueue(protocol.encode(frame), critical=True)
        except Violation:
            asyncio.create_task(self._drop_peer(peer))
            return
        if manifest is not None:
            self._emit("asset_bundle_sent", {"sender": self.name, "target": name, **asset_bundle.summary(manifest)})

    def send_asset_bundle(self, target: str, manifest: dict) -> bool:
        if not isinstance(target, str) or not target:
            return False
        manifest = dict(manifest)
        manifest["target"] = target
        manifest["sender"] = self.name
        manifest["sender_color"] = list(self.color)
        try:
            wire = asset_bundle.pack(manifest)
        except asset_bundle.AssetBundleError:
            return False
        if len(wire) > MAX_MESH:
            return False
        frame = Frame(type=MessageType.ASSET_BUNDLE, channel=Channel.BULK, payload=wire)
        if self.is_host:
            loop = self._loop
            if loop is None or not loop.is_running():
                return False
            loop.call_soon_threadsafe(lambda: self._send_targeted(target, frame, manifest))
            return True
        self._enqueue(frame)
        return True

    def send_view_bookmark(self, bookmark: dict) -> bool:
        clean = bookmarks.sanitize(bookmark)
        if clean is None:
            return False
        body = {"name": self.name, "bookmark": clean}
        frame = protocol.json_frame(MessageType.VIEW_BOOKMARK, body)
        if self.is_host:
            loop = self._loop
            if loop is None or not loop.is_running():
                return False
            loop.call_soon_threadsafe(lambda: self._broadcast(frame))
            return True
        self._enqueue(frame)
        return True

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

    def send_resume(self, uids) -> None:
        objects = [str(uid) for uid in list(uids)[:100000]]
        self._enqueue(protocol.json_frame(MessageType.RESUME, {"objects": objects}))

    def send_subscription(self, uids, modes: dict, priorities: dict) -> None:
        body = {
            "uids": [str(uid) for uid in list(uids)[:20000]],
            "modes": {str(key): str(value)[:8] for key, value in list(modes.items())[:20000]},
            "priorities": {str(key): int(value) for key, value in list(priorities.items())[:20000]},
        }
        self._enqueue(protocol.json_frame(MessageType.SUBSCRIPTION, body))

    def send_client_stats(self, stats: dict) -> None:
        self._enqueue(protocol.json_frame(MessageType.CLIENT_STATS, stats))

    def peer_names(self) -> list[str]:
        return [peer.name for peer in self._peers.values() if peer.authenticated]

    def reset_leases(self) -> None:
        loop = self._loop
        if self.is_host and loop and loop.is_running():
            loop.call_soon_threadsafe(self._reset_leases)

    def _reset_leases(self) -> None:
        now = time.monotonic()
        for key in list(self._leases.snapshot(now)):
            self._leases.release(key)
        self._broadcast(
            protocol.json_frame(MessageType.LEASE, {"action": "released", "leases": {}})
        )
        self._emit_leases()

    def bump_rev(self, uid: str) -> int:
        if not uid:
            return 0
        self._revs[uid] = self._revs.get(uid, 0) + 1
        return self._revs[uid]

    def send_presence(self, body: dict[str, Any]) -> None:
        # Presence is delta-encoded by the addon. Only copy the keys that were
        # actually supplied; filling defaults here would make every small
        # update a full snapshot and waste internet bandwidth.
        payload: dict[str, Any] = {}
        for key, limit in (
            ("scene", 64),
            ("mode", 32),
            ("active", 64),
            ("selected", 512),
            ("scene_hash", 32),
        ):
            if key in body:
                payload[key] = clean_text(body.get(key), limit)
        if "frame" in body and isinstance(body.get("frame"), (int, float)) and not isinstance(body.get("frame"), bool):
            payload["frame"] = int(body["frame"])
        if "playing" in body:
            payload["playing"] = bool(body.get("playing"))
        if "fps" in body and isinstance(body.get("fps"), (int, float)) and not isinstance(body.get("fps"), bool):
            payload["fps"] = max(1, min(240, int(body["fps"])))
        view = body.get("view")
        if isinstance(view, list) and len(view) == 16:
            try:
                values = [float(value) for value in view]
            except (TypeError, ValueError):
                values = []
            if len(values) == 16 and all(math.isfinite(value) and abs(value) < 1e12 for value in values):
                payload["view"] = values
        corners = body.get("view_corners")
        if isinstance(corners, list) and len(corners) == 21:
            try:
                values = [float(value) for value in corners]
            except (TypeError, ValueError):
                values = []
            if len(values) == 21 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                payload["view_corners"] = values
        aspect = body.get("aspect")
        if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
            payload["aspect"] = max(0.1, min(10.0, float(aspect)))
        persp = clean_text(body.get("persp"), 16)
        if persp:
            payload["persp"] = persp
        cursor = body.get("cursor")
        if isinstance(cursor, list) and len(cursor) == 3:
            try:
                values = [float(value) for value in cursor]
            except (TypeError, ValueError):
                values = []
            if len(values) == 3 and all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                payload["cursor"] = values
        distance = body.get("dist")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            payload["dist"] = max(0.01, min(100000.0, float(distance)))
        if self.is_host:
            payload.update(
                {
                    "name": self.name,
                    "color": list(self.color),
                    "role": int(self.role),
                }
            )
        if payload:
            self._enqueue(protocol.json_frame(MessageType.PRESENCE, payload))

    def send_selection(self, uid: str, mask: bytes, count: int, kind: int = 0) -> None:
        if not isinstance(mask, (bytes, bytearray)) or not 0 <= count <= 200_000:
            return
        if len(mask) != (count + 7) // 8 or kind not in (0, 1, 2):
            return
        name = self.name.encode("utf-8")[:MAX_NAME]
        payload = (
            bytes([len(name)])
            + name
            + uuidlib.UUID(str(uid)).bytes
            + struct.pack("<BBII", kind, 0, 0, count)
            + bytes(mask)
        )
        self._enqueue(Frame(type=MessageType.SELECTION, channel=Channel.PRESENCE, payload=payload))

    def send_laser(self, points) -> None:
        if not points:
            return
        clean = []
        for point in list(points)[:64]:
            try:
                values = [float(point[0]), float(point[1]), float(point[2])]
            except (TypeError, ValueError, IndexError):
                continue
            if all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                clean.append(values)
        if not clean:
            return
        name = self.name.encode("utf-8")[:MAX_NAME]
        flat = [value for point in clean for value in point]
        payload = (
            bytes([len(name)])
            + name
            + struct.pack("<H", len(clean))
            + struct.pack("<%df" % len(flat), *flat)
        )
        self._enqueue(Frame(type=MessageType.LASER, channel=Channel.PRESENCE, payload=payload))

    def send_chat(self, text: str) -> None:
        text = clean_text(text, MAX_CHAT)
        if text:
            self._enqueue(protocol.json_frame(MessageType.CHAT, {"text": text, "name": self.name}))
            self._tap("chat", {"name": self.name, "text": text})

    def send_transform(self, uid: str, matrix) -> None:
        try:
            payload = encode_transform(uid, matrix)
        except Exception:  # noqa: BLE001
            return
        self._enqueue(Frame(type=MessageType.TRANSFORM, channel=Channel.TRANSFORM, payload=payload))
        self._tap("transform", {"uuid": uid, "matrix": [float(value) for value in matrix]})

    def send_pose(self, uid: str, bones: list) -> None:
        try:
            body = protocol.json_frame(MessageType.POSE, {"uuid": uid, "bones": bones})
        except Exception:  # noqa: BLE001
            return
        self._enqueue(Frame(type=MessageType.POSE, channel=Channel.TRANSFORM, payload=body.payload))
        self._tap("pose", {"uuid": uid, "bones": bones})

    def send_shape_keys(self, uid: str, values: dict) -> None:
        try:
            frame = protocol.json_frame(MessageType.SHAPE_KEYS, {"uuid": uid, "values": values})
        except Exception:  # noqa: BLE001
            return
        self._enqueue(frame)
        self._tap("shape_keys", {"uuid": uid, "values": values})

    def send_action(self, uid: str, action: dict | None, nla: dict | None = None) -> None:
        try:
            body = {"uuid": uid, "action": action, "nla": nla}
            frame = protocol.json_frame(MessageType.ACTION, body)
        except Exception:  # noqa: BLE001
            return
        if len(frame.payload) > 16 * 1024 * 1024:
            return
        self._enqueue(Frame(type=MessageType.ACTION, channel=Channel.BULK, payload=frame.payload))
        self._tap("action", body)

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
        self._tap(
            "mesh_delta" if kind == "delta" else "mesh_snapshot",
            {"uuid": uid, "payload": payload, "rev": int(rev)},
        )

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

    def send_object_create(self, uid: str, kind: int, meta: dict, geometry: bytes = b"") -> bool:
        try:
            payload = object_payload.encode_create(uid, kind, meta, geometry)
        except object_payload.ObjectPayloadError:
            return False
        if len(payload) > transfer.MAX_TRANSFER:
            return False
        if len(payload) > CHUNK_THRESHOLD:
            try:
                identity, chunks = transfer.split(payload, transfer.KIND_OBJECT)
            except transfer.TransferError:
                return False
            total = len(chunks)
            for index, chunk in enumerate(chunks):
                self._enqueue(Frame(type=MessageType.CHUNK, channel=Channel.BULK, payload=chunk))
                self._emit(
                    "transfer_progress",
                    {
                        "id": identity.hex(),
                        "kind": transfer.KIND_OBJECT,
                        "direction": "send",
                        "received": index + 1,
                        "total": total,
                        "bytes": min(len(payload), (index + 1) * transfer.CHUNK_SIZE),
                        "total_bytes": len(payload),
                        "percent": round((index + 1) * 100.0 / max(1, total), 1),
                    },
                )
            self._emit("transfer_done", {"direction": "send", "id": identity.hex()})
            self._tap(
                "object_create",
                {"uuid": uid, "kind": kind, "meta": meta, "geometry": geometry},
            )
            return True
        self._enqueue(Frame(type=MessageType.OBJECT_CREATE, channel=Channel.MESH, payload=payload))
        self._tap(
            "object_create",
            {"uuid": uid, "kind": kind, "meta": meta, "geometry": geometry},
        )
        return True

    def send_object_delete(self, uid: str) -> None:
        try:
            payload = object_payload.encode_delete(uid)
        except object_payload.ObjectPayloadError:
            return
        self._enqueue(Frame(type=MessageType.OBJECT_DELETE, channel=Channel.CONTROL, payload=payload))
        self._tap("object_delete", {"uuid": uid})

    def send_object_rename(self, uid: str, name: str) -> bool:
        if not isinstance(uid, str) or not isinstance(name, str) or not name.strip():
            return False
        clean = name.strip()[:48]
        self._enqueue(
            protocol.json_frame(
                MessageType.OBJECT_RENAME, {"uuid": uid, "name": clean}
            )
        )
        self._tap("object_rename", {"uuid": uid, "name": clean})
        return True

    def send_material(self, uid: str, slot: int, material: dict) -> None:
        payload = material_payload.sanitize(material)
        if payload is None or not 0 <= int(slot) <= 255:
            return
        self._enqueue(
            protocol.json_frame(MessageType.MATERIAL_UPDATE, {"uuid": uid, "slot": int(slot), "material": payload})
        )
        self._tap(
            "material_update",
            {"uuid": uid, "slot": int(slot), "material": payload},
        )

    def send_modifier_stack(self, uid: str, modifiers: list, details: dict | None = None) -> None:
        body = {"uuid": uid, "modifiers": modifiers}
        if details is not None:
            body["details"] = details
        message = self._sanitize_modifiers(body)
        if message is None:
            return
        self._enqueue(protocol.json_frame(MessageType.MODIFIER_STACK, message))
        self._tap("modifier_stack", message)

    def send_image(self, name: str, format_hint: str, data: bytes) -> int:
        try:
            payload = image_payload.encode(name, format_hint, data)
        except image_payload.ImagePayloadError:
            return 0
        if len(payload) > MAX_MESH:
            return 0
        if len(payload) > CHUNK_THRESHOLD:
            try:
                identity, chunks = transfer.split(payload, transfer.KIND_IMAGE)
            except transfer.TransferError:
                return 0
            total = len(chunks)
            for index, chunk in enumerate(chunks):
                self._enqueue(Frame(type=MessageType.CHUNK, channel=Channel.BULK, payload=chunk))
                self._emit(
                    "transfer_progress",
                    {
                        "id": identity.hex(),
                        "kind": transfer.KIND_IMAGE,
                        "direction": "send",
                        "received": index + 1,
                        "total": total,
                        "bytes": min(len(payload), (index + 1) * transfer.CHUNK_SIZE),
                        "total_bytes": len(payload),
                        "percent": round((index + 1) * 100.0 / max(1, total), 1),
                    },
                )
            self._emit("transfer_done", {"direction": "send", "id": identity.hex()})
            return len(payload)
        self._enqueue(Frame(type=MessageType.IMAGE_ASSET, channel=Channel.BULK, payload=payload))
        return len(payload)

    def request_lease(self, key: str, action: str = "acquire") -> None:
        self._enqueue(protocol.json_frame(MessageType.LEASE, {"key": key, "action": action}))

    def send_lease(self, key: str, action: str, requester: str | None = None) -> None:
        if self.is_host and action in {"grant", "deny"}:
            self._local_resolve_lease(key, action, requester)
            return
        body = {"key": key, "action": action}
        if requester:
            body["requester"] = requester
        self._enqueue(protocol.json_frame(MessageType.LEASE, body))

    def _local_resolve_lease(self, key: str, action: str, requester: str | None) -> None:
        """Grant or deny a request from the host's own Blender."""
        now = time.monotonic()
        owner = self._leases.owner_of(key, now)
        if not requester or owner != self.name:
            self._emit("lease", {"key": key, "owner": owner, "action": "held"})
            return
        grant = action == "grant"
        resolved = self._leases.resolve_request(key, requester, grant=grant, now=now)
        if grant and not resolved:
            self._leases.force(key, requester, now)
        elif not grant and not resolved:
            self._emit("lease", {"key": key, "owner": self.name, "action": "held"})
            return
        self._expiry_notified.discard(key)
        if grant:
            body = {
                "key": key,
                "owner": requester,
                "action": "grant",
                "leases": self._leases.snapshot(now),
            }
        else:
            body = {
                "key": key,
                "owner": self.name,
                "requester": requester,
                "action": "denied_request",
                "leases": self._leases.snapshot(now),
            }
        self._broadcast(protocol.json_frame(MessageType.LEASE, body))
        self._emit("lease", body)

    def force_lease(self, key: str) -> None:
        """Host takes an object's lease so its own edit is authoritative."""
        loop = self._loop
        if self.is_host and loop and loop.is_running():
            loop.call_soon_threadsafe(self._force_lease, key)

    def _force_lease(self, key: str) -> None:
        now = time.monotonic()
        self._leases.force(key, self.name, now)
        self._expiry_notified.discard(key)
        body = {
            "key": key,
            "owner": self.name,
            "action": "force",
            "leases": self._leases.snapshot(now),
        }
        self._broadcast(protocol.json_frame(MessageType.LEASE, body))
        self._emit("lease", body)

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
            self._expiry_notified.discard(key)
        elif action == "release":
            self._leases.release(key, self.name)
            self._expiry_notified.discard(key)
        elif action == "force":
            self._leases.force(key, self.name, now)
            self._expiry_notified.discard(key)
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

    def has_lan_peer(self) -> bool:
        """True when at least one authenticated peer arrived over a LAN IP."""
        return self._lan_peer_count > 0

    def stats(self) -> dict[str, Any]:
        peers = []
        bytes_out = self._bytes_out
        if self.is_host:
            peers = [
                {
                    "name": peer.name,
                    "address": peer.address,
                    "app": peer.app,
                    "addon": peer.addon,
                    "protocol": peer.protocol,
                    "caps": peer.caps,
                    "role": int(peer.role),
                    "rtt_ms": round(max(0.0, float(peer.rtt_ms)), 1),
                    "queue": int(peer.queue.qsize()),
                    "pending": len(peer.pending),
                    "quality": int(peer.quality),
                }
                for peer in self._peers.values()
                if peer.authenticated
            ]
            bytes_out += sum(peer.sent for peer in self._peers.values())
            measured = [float(peer.rtt_ms) for peer in self._peers.values() if peer.authenticated and peer.rtt_ms > 0]
            rtt_ms = round(sum(measured) / len(measured), 1) if measured else 0.0
        else:
            peers = [{"name": "host", "address": self.code, "app": self.remote_app, "caps": self.remote_caps, "role": int(Role.OWNER)}]
            rtt_ms = round(self._rtt_ms, 1)
        return {
            "state": self.state,
            "rtt_ms": rtt_ms,
            "bytes_in": self._bytes_in,
            "bytes_out": bytes_out,
            "peers": peers,
            "fingerprint": self.fingerprint,
            "port": self.bound_port,
            "relay": self._using_relay,
        }
