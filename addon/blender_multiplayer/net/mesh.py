"""Direct peer-to-peer mesh transport.

The signalling server (the Worker's Room object) only introduces peers: it
exchanges public addresses and X25519 public keys. This module turns those
introductions into encrypted TCP links between every pair of peers.

Security
--------
* Each peer owns an ephemeral X25519 key pair for the session.
* Peers exchange public keys through the signalling server and derive a shared
  secret with X25519, then HKDF-SHA256 with both handshake nonces as salt.
* Every frame after the handshake is ChaCha20-Poly1305 with a monotonic nonce.
* The signalling server never sees a private key or any plaintext.

NAT traversal is best-effort by design. A direct link is attempted when a peer
advertises a reachable address; if the connection fails within the timeout the
caller falls back to the relay. Peers with public addresses (a VPS, a forwarded
port) connect directly and reliably; peers behind symmetric NAT or CGNAT fall
back to the relay.

This module is transport only. It does not know about objects, leases or the
session protocol; it moves opaque bytes between named peers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import time
from dataclasses import dataclass, field

try:  # pragma: no cover - exercised through the optional dependency
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    AVAILABLE = True
except Exception:  # noqa: BLE001
    X25519PrivateKey = None  # type: ignore[assignment]
    X25519PublicKey = None  # type: ignore[assignment]
    ChaCha20Poly1305 = None  # type: ignore[assignment]
    HKDF = None  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]
    serialization = None  # type: ignore[assignment]
    AVAILABLE = False

MAGIC = b"BMPM"
VERSION = 1
PEER_ID_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12
HANDSHAKE_NONCE = 16
HEADER = struct.Struct("!4sB16s32s16s")
HEADER_SIZE = HEADER.size
FRAME_HEADER = struct.Struct("!I")
MAX_FRAME = 16 * 1024 * 1024
MAX_PEERS = 64
CONNECT_TIMEOUT = 4.0
HANDSHAKE_TIMEOUT = 5.0


class MeshError(Exception):
    pass


def new_peer_id() -> str:
    return os.urandom(PEER_ID_SIZE).hex()


def _derive_keys(shared: bytes, nonce_a: bytes, nonce_b: bytes) -> tuple[bytes, bytes]:
    """Derive two directional keys so the two proof frames never reuse a nonce."""
    salt = b"".join(sorted((nonce_a, nonce_b)))
    initiator = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=b"bmp-mesh-v1-init"
    ).derive(shared)
    responder = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=b"bmp-mesh-v1-resp"
    ).derive(shared)
    return initiator, responder


class FrameCipher:
    """Encrypts and decrypts length-prefixed frames with rolling nonces.

    Send and receive use separate directional keys, so the two sides never
    encrypt different plaintexts under the same key and nonce.
    """

    def __init__(self, send_key: bytes, recv_key: bytes):
        if not AVAILABLE:
            raise MeshError("mesh encryption needs the 'cryptography' package")
        self._send_aead = ChaCha20Poly1305(send_key)
        self._recv_aead = ChaCha20Poly1305(recv_key)
        self._send = 0
        self._recv = 0

    def encrypt(self, payload: bytes) -> bytes:
        if len(payload) > MAX_FRAME:
            raise MeshError("mesh frame is too large")
        nonce = self._send.to_bytes(NONCE_SIZE, "big")
        self._send += 1
        return nonce + self._send_aead.encrypt(nonce, payload, None)

    def decrypt(self, packet: bytes) -> bytes:
        if len(packet) < NONCE_SIZE + 16:
            raise MeshError("mesh packet is truncated")
        nonce = packet[:NONCE_SIZE]
        expected = self._recv.to_bytes(NONCE_SIZE, "big")
        if nonce != expected:
            raise MeshError("mesh packet is out of order")
        self._recv += 1
        try:
            return self._recv_aead.decrypt(nonce, packet[NONCE_SIZE:], None)
        except Exception as exc:  # noqa: BLE001
            raise MeshError("mesh packet failed authentication") from exc


@dataclass
class MeshPeer:
    peer_id: str
    address: str = ""
    public_key: bytes = b""
    initiator_id: str = ""
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    cipher: FrameCipher | None = None
    connected: bool = False
    direct: bool = False
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def ready(self) -> bool:
        return self.connected and self.cipher is not None and self.writer is not None


class MeshManager:
    """Owns the local listening socket and every direct peer link."""

    def __init__(self, peer_id: str | None = None, listen_port: int = 0):
        if not AVAILABLE:
            raise MeshError("mesh transport needs the 'cryptography' package")
        self.peer_id = peer_id or new_peer_id()
        self.listen_port = int(listen_port or 0)
        self.bound_port = 0
        self.public_address = ""
        self._private = X25519PrivateKey.generate()
        self.public_key = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self._server: asyncio.AbstractServer | None = None
        self._peers: dict[str, MeshPeer] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._readers: dict[str, asyncio.Task] = {}
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._closed = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "0.0.0.0", self.listen_port)
        sockets = self._server.sockets or []
        if not sockets:
            raise MeshError("mesh listener did not bind")
        self.bound_port = sockets[0].getsockname()[1]
        return self.bound_port

    async def stop(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
        for task in list(self._pending.values()):
            task.cancel()
        self._pending.clear()
        readers = list(self._readers.values())
        for task in readers:
            task.cancel()
        self._readers.clear()
        for peer in list(self._peers.values()):
            await self._drop(peer)
        self._peers.clear()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if readers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True), 2.0
                )
            except Exception:  # noqa: BLE001
                pass

    def peers(self) -> list[MeshPeer]:
        return [peer for peer in self._peers.values() if peer.ready]

    def peer(self, peer_id: str) -> MeshPeer | None:
        return self._peers.get(peer_id)

    # ---------------------------------------------------------------- incoming
    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.close()
            return
        try:
            peer = await self._handshake(reader, writer, initiator=False)
        except Exception:  # noqa: BLE001
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        if peer is None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        await self._adopt(peer, reader, writer, initiator=False)

    # ---------------------------------------------------------------- outgoing
    async def connect(self, peer_id: str, address: str, timeout: float = CONNECT_TIMEOUT) -> bool:
        """Attempt a direct link to ``address`` (``host:port``)."""
        if self._closed or not address:
            return False
        existing = self._peers.get(peer_id)
        if existing is not None and existing.ready:
            return True
        task = self._pending.get(peer_id)
        if task is not None and not task.done():
            return False
        task = asyncio.create_task(self._connect(peer_id, address, timeout))
        self._pending[peer_id] = task
        try:
            return await task
        finally:
            self._pending.pop(peer_id, None)

    async def _connect(self, peer_id: str, address: str, timeout: float) -> bool:
        host, _, port_text = address.rpartition(":")
        if not host or not port_text.isdigit():
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port_text)), timeout
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            peer = await self._handshake(reader, writer, initiator=True, expected=peer_id)
        except Exception:  # noqa: BLE001
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return False
        if peer is None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return False
        await self._adopt(peer, reader, writer, initiator=True)
        return True

    # --------------------------------------------------------------- handshake
    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initiator: bool,
        expected: str | None = None,
    ) -> MeshPeer | None:
        nonce = os.urandom(HANDSHAKE_NONCE)
        own = HEADER.pack(MAGIC, VERSION, bytes.fromhex(self.peer_id), self.public_key, nonce)
        try:
            if initiator:
                writer.write(own)
                await writer.drain()
                blob = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), HANDSHAKE_TIMEOUT)
            else:
                blob = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), HANDSHAKE_TIMEOUT)
                writer.write(own)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            return None
        if len(blob) != HEADER_SIZE:
            return None
        magic, version, raw_id, raw_key, remote_nonce = HEADER.unpack(blob)
        if magic != MAGIC or version != VERSION:
            return None
        if len(raw_id) != PEER_ID_SIZE or len(raw_key) != KEY_SIZE:
            return None
        peer_id = raw_id.hex()
        if expected is not None and peer_id != expected:
            return None
        try:
            remote = X25519PublicKey.from_public_bytes(raw_key)
            shared = self._private.exchange(remote)
        except Exception:  # noqa: BLE001
            return None
        key_init, key_resp = _derive_keys(shared, nonce, remote_nonce)
        if initiator:
            cipher = FrameCipher(key_init, key_resp)
        else:
            cipher = FrameCipher(key_resp, key_init)
        # Exchange a proof frame so both sides know the key is live before the
        # link is advertised as ready.
        proof = cipher.encrypt(b"BMPM-OK")
        try:
            writer.write(FRAME_HEADER.pack(len(proof)) + proof)
            await writer.drain()
            length = FRAME_HEADER.unpack(
                await asyncio.wait_for(reader.readexactly(FRAME_HEADER.size), HANDSHAKE_TIMEOUT)
            )[0]
            if length > MAX_FRAME:
                return None
            packet = await asyncio.wait_for(reader.readexactly(length), HANDSHAKE_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            return None
        try:
            if cipher.decrypt(packet) != b"BMPM-OK":
                return None
        except MeshError:
            return None
        return MeshPeer(peer_id=peer_id, public_key=raw_key, cipher=cipher)

    async def _adopt(
        self,
        peer: MeshPeer,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initiator: bool,
    ) -> None:
        # The connection whose initiator has the lower peer id wins. This rule is
        # symmetric, so both sides keep the same physical TCP connection when
        # both of them dialled at once.
        peer.initiator_id = self.peer_id if initiator else peer.peer_id
        existing = self._peers.get(peer.peer_id)
        if existing is not None and existing.ready:
            if existing.initiator_id <= peer.initiator_id:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            old_task = self._readers.pop(peer.peer_id, None)
            if old_task is not None and old_task is not asyncio.current_task():
                old_task.cancel()
            await self._drop(existing)
        peer.reader = reader
        peer.writer = writer
        peer.connected = True
        peer.direct = True
        self._peers[peer.peer_id] = peer
        self._readers[peer.peer_id] = asyncio.create_task(self._read_loop(peer))

    async def _read_loop(self, peer: MeshPeer) -> None:
        assert peer.reader is not None and peer.cipher is not None
        try:
            while not self._closed:
                header = await peer.reader.readexactly(FRAME_HEADER.size)
                length = FRAME_HEADER.unpack(header)[0]
                if length > MAX_FRAME:
                    break
                packet = await peer.reader.readexactly(length)
                payload = peer.cipher.decrypt(packet)
                peer.last_seen = time.monotonic()
                await self._incoming.put((peer.peer_id, payload))
        except (asyncio.IncompleteReadError, MeshError, OSError):
            pass
        finally:
            if self._peers.get(peer.peer_id) is peer:
                self._peers.pop(peer.peer_id, None)
            if self._readers.get(peer.peer_id) is asyncio.current_task():
                self._readers.pop(peer.peer_id, None)
            await self._drop(peer)

    async def _drop(self, peer: MeshPeer) -> None:
        peer.connected = False
        if peer.writer is not None:
            try:
                peer.writer.close()
            except Exception:  # noqa: BLE001
                pass
        peer.writer = None
        peer.reader = None

    # ------------------------------------------------------------------- data
    async def send(self, peer_id: str, payload: bytes) -> bool:
        peer = self._peers.get(peer_id)
        if peer is None or not peer.ready:
            return False
        if peer.cipher is None or peer.writer is None:
            return False
        try:
            packet = peer.cipher.encrypt(payload)
            peer.writer.write(FRAME_HEADER.pack(len(packet)) + packet)
            await peer.writer.drain()
            return True
        except (MeshError, OSError):
            await self._drop(peer)
            self._peers.pop(peer_id, None)
            return False

    async def broadcast(self, payload: bytes) -> int:
        sent = 0
        for peer_id in list(self._peers):
            if await self.send(peer_id, payload):
                sent += 1
        return sent

    async def receive(self) -> tuple[str, bytes]:
        return await self._incoming.get()

    # ------------------------------------------------------------------ signalling
    def hello(self) -> dict:
        return {
            "type": "hello",
            "address": self.public_address,
            "port": self.bound_port,
            "key": self.public_key.hex(),
        }

    def apply_hello(self, peer_id: str, message: dict) -> None:
        peer = self._peers.get(peer_id)
        if peer is None:
            peer = MeshPeer(peer_id=peer_id)
            self._peers[peer_id] = peer
        address = message.get("address")
        if isinstance(address, str) and 0 < len(address) <= 128:
            peer.address = address
        key = message.get("key")
        if isinstance(key, str) and len(key) == 64:
            try:
                peer.public_key = bytes.fromhex(key)
            except ValueError:
                peer.public_key = b""
