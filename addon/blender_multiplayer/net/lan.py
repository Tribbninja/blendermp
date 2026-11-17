"""Zero-configuration LAN discovery.

A host answers broadcast queries on port 5599 with a small JSON beacon that
contains its room code and endpoint. Joiners send a query and collect replies,
so only one machine ever binds the discovery port and it works on Windows.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Awaitable, Callable

from .transport import UdpEndpoint

LAN_PORT = 5599
MAGIC = "bmp-lan-1"


def encode_beacon(code: str, name: str, port: int, **extra: Any) -> bytes:
    body = {"magic": MAGIC, "op": "beacon", "code": code, "name": name, "port": port, **extra}
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def query_packet() -> bytes:
    return json.dumps({"magic": MAGIC, "op": "query"}, separators=(",", ":")).encode("utf-8")


def decode(data: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or body.get("magic") != MAGIC:
        return None
    return body


def broadcast_targets() -> list[str]:
    targets = {"255.255.255.255"}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        parts = address.split(".")
        if len(parts) == 4:
            targets.add(".".join(parts[:3] + ["255"]))
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(targets)


class LanResponder:
    """Answers LAN discovery queries for one session."""

    def __init__(self, beacon: Callable[[], dict[str, Any]] | Callable[[], Awaitable[dict[str, Any]]], port: int = LAN_PORT):
        self._beacon = beacon
        self._port = port
        self._endpoint: UdpEndpoint | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._endpoint = await UdpEndpoint.create("0.0.0.0", self._port, allow_broadcast=True)
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        assert self._endpoint
        while True:
            try:
                data, addr = await self._endpoint.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            body = decode(data)
            if not body or body.get("op") != "query":
                continue
            beacon = self._beacon()
            if asyncio.iscoroutine(beacon):
                beacon = await beacon
            self._endpoint.send(json.dumps(beacon, separators=(",", ":")).encode("utf-8"), addr)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._endpoint:
            self._endpoint.close()
            self._endpoint = None


async def probe(timeout: float = 1.4, port: int = LAN_PORT, repeats: int = 3) -> list[dict[str, Any]]:
    """Broadcast a query and collect unique beacons."""
    endpoint = await UdpEndpoint.create("0.0.0.0", 0, allow_broadcast=True)
    found: dict[tuple, dict[str, Any]] = {}
    request = query_packet()
    try:
        for _ in range(repeats):
            for target in broadcast_targets():
                endpoint.send(request, (target, port))
            await asyncio.sleep(0.12)

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                data, addr = await asyncio.wait_for(endpoint.recv(), remaining)
            except asyncio.TimeoutError:
                break
            body = decode(data)
            if not body or body.get("op") != "beacon":
                continue
            key = (body.get("code"), addr[0], body.get("port"))
            body["host"] = addr[0]
            found[key] = body
    finally:
        endpoint.close()
    return list(found.values())
