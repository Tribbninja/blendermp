"""Cross-engine sync for Blendermp.

Blendermp is host-authoritative over Blender data. A cross-engine link exports
the same scene graph to another engine (Unity, Unreal, Godot) as a one-way,
lossless stream of snapshots and deltas. The engine side never edits the
Blender scene; it only renders it.

Transport
---------
``BridgeServer`` is a small TCP server speaking newline-delimited JSON. It binds
to ``127.0.0.1`` by default. Each connected engine receives:

* a ``hello`` message with the protocol version and engine name;
* ``object`` upserts whenever a transform/visibility/material changes;
* ``remove`` when an object disappears;
* a full ``scene`` snapshot on request or on connect.

The reference receivers live in ``integrations/`` (Unity C#, Unreal Python,
Godot GDScript). They are intentionally small so they can be dropped into a
project and adapted.

Nothing here imports ``bpy`` or an engine SDK, so the module is testable and
importable in plain Python.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

BRIDGE_PROTOCOL = 1
DEFAULT_BRIDGE_PORT = 7103
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_CLIENTS = 8


# --------------------------------------------------------------------------- math
def _matmul3(a: list[float], b: list[float]) -> list[float]:
    return [
        a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
        a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
        a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
        a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
        a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
        a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
        a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
        a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
        a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
    ]


def _rotation3(matrix: list[float]) -> list[float]:
    return [
        matrix[0],
        matrix[1],
        matrix[2],
        matrix[4],
        matrix[5],
        matrix[6],
        matrix[8],
        matrix[9],
        matrix[10],
    ]


def quat_from_matrix3(m: list[float]) -> list[float]:
    """Return [x, y, z, w] for a row-major 3x3 rotation matrix."""
    trace = m[0] + m[4] + m[8]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (m[7] - m[5]) / scale
        y = (m[2] - m[6]) / scale
        z = (m[3] - m[1]) / scale
    elif m[0] > m[4] and m[0] > m[8]:
        scale = math.sqrt(1.0 + m[0] - m[4] - m[8]) * 2.0
        w = (m[7] - m[5]) / scale
        x = 0.25 * scale
        y = (m[1] + m[3]) / scale
        z = (m[2] + m[6]) / scale
    elif m[4] > m[8]:
        scale = math.sqrt(1.0 + m[4] - m[0] - m[8]) * 2.0
        w = (m[2] - m[6]) / scale
        x = (m[1] + m[3]) / scale
        y = 0.25 * scale
        z = (m[5] + m[7]) / scale
    else:
        scale = math.sqrt(1.0 + m[8] - m[0] - m[4]) * 2.0
        w = (m[3] - m[1]) / scale
        x = (m[2] + m[6]) / scale
        y = (m[5] + m[7]) / scale
        z = 0.25 * scale
    return [round(x, 6), round(y, 6), round(z, 6), round(w, 6)]


# C maps Blender (x, y, z) to a Y-up engine: (x, z, -y).
_Y_UP = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, -1.0, 0.0]
_Y_UP_INV = [1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0]


def y_up_quat(matrix: list[float]) -> list[float]:
    """Blender world matrix -> Y-up quaternion (Unity/Godot convention)."""
    rotated = _matmul3(_matmul3(_Y_UP, _rotation3(matrix)), _Y_UP_INV)
    return quat_from_matrix3(rotated)


def y_up_position(matrix: list[float]) -> list[float]:
    return [round(matrix[12], 6), round(matrix[14], 6), round(-matrix[13], 6)]


def y_up_scale(matrix: list[float]) -> list[float]:
    sx = math.sqrt(matrix[0] ** 2 + matrix[1] ** 2 + matrix[2] ** 2)
    sy = math.sqrt(matrix[4] ** 2 + matrix[5] ** 2 + matrix[6] ** 2)
    sz = math.sqrt(matrix[8] ** 2 + matrix[9] ** 2 + matrix[10] ** 2)
    return [round(sx, 6), round(sy, 6), round(sz, 6)]


# ----------------------------------------------------------------------- datatypes
@dataclass
class ExportedObject:
    uid: str
    name: str
    kind: str
    matrix: list[float]
    bounds: tuple[float, float, float, float, float, float] = (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    metadata: dict[str, Any] = field(default_factory=dict)
    visible: bool = True
    mesh: dict[str, Any] | None = None


@dataclass
class ExportedMaterial:
    name: str
    base_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    metallic: float = 0.0
    roughness: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


class EngineExporter(ABC):
    """Base class for every cross-engine exporter."""

    name: str = "engine"

    def common_object(self, obj: ExportedObject) -> dict[str, Any]:
        data = {
            "uid": obj.uid,
            "name": obj.name[:64],
            "kind": obj.kind,
            "matrix": [round(float(value), 6) for value in obj.matrix[:16]],
            "bounds": [round(float(value), 6) for value in obj.bounds[:6]],
            "visible": bool(obj.visible),
            "metadata": obj.metadata,
        }
        if obj.mesh is not None:
            data["mesh"] = obj.mesh
        return data

    @abstractmethod
    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        ...

    @abstractmethod
    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        ...

    def export_scene(
        self,
        objects: Iterable[ExportedObject],
        materials: Iterable[ExportedMaterial] | None = None,
    ) -> dict[str, Any]:
        return {
            "engine": self.name,
            "objects": [self.export_object(obj) for obj in objects],
            "materials": [self.export_material(mat) for mat in (materials or [])],
        }


class UnityExporter(EngineExporter):
    name = "unity"

    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        data = self.common_object(obj)
        data.update(
            {
                "op": "upsert",
                "position": y_up_position(obj.matrix),
                "rotation": y_up_quat(obj.matrix),
                "scale": y_up_scale(obj.matrix),
            }
        )
        return data

    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        return {
            "name": material.name[:64],
            "shader": "Standard",
            "color": [round(float(value), 6) for value in material.base_color],
            "metallic": round(float(material.metallic), 4),
            "roughness": round(float(material.roughness), 4),
            "metadata": material.metadata,
        }


class UnrealExporter(EngineExporter):
    name = "unreal"

    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        data = self.common_object(obj)
        # Unreal is X-forward, Y-right, Z-up, left-handed.
        data.update(
            {
                "op": "upsert",
                "actor": obj.name[:64],
                "position": [
                    round(obj.matrix[12], 6),
                    round(-obj.matrix[13], 6),
                    round(obj.matrix[14], 6),
                ],
                "rotation": [round(float(value), 6) for value in obj.metadata.get("euler", [0, 0, 0])],
                "scale": y_up_scale(obj.matrix),
            }
        )
        return data

    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        return {
            "name": material.name[:64],
            "material": material.name[:64],
            "baseColor": [round(float(value), 6) for value in material.base_color],
            "metallic": round(float(material.metallic), 4),
            "roughness": round(float(material.roughness), 4),
            "metadata": material.metadata,
        }


class GodotExporter(EngineExporter):
    name = "godot"

    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        data = self.common_object(obj)
        data.update(
            {
                "op": "upsert",
                "position": y_up_position(obj.matrix),
                "rotation": y_up_quat(obj.matrix),
                "scale": y_up_scale(obj.matrix),
            }
        )
        return data

    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        return {
            "name": material.name[:64],
            "albedo": [round(float(value), 6) for value in material.base_color],
            "metallic": round(float(material.metallic), 4),
            "roughness": round(float(material.roughness), 4),
            "metadata": material.metadata,
        }


class EngineRegistry:
    def __init__(self):
        self._exporters: dict[str, EngineExporter] = {}

    def register(self, exporter: EngineExporter) -> None:
        self._exporters[exporter.name] = exporter

    def get(self, name: str) -> EngineExporter | None:
        return self._exporters.get(str(name).lower())

    def names(self) -> list[str]:
        return sorted(self._exporters)

    @classmethod
    def default(cls) -> "EngineRegistry":
        registry = cls()
        registry.register(UnityExporter())
        registry.register(UnrealExporter())
        registry.register(GodotExporter())
        return registry


# ------------------------------------------------------------------------- bridge
class BridgeServer:
    """Newline-delimited JSON TCP server for engine receivers."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_BRIDGE_PORT,
        engine: str = "unity",
    ):
        self.host = host
        self.port = int(port)
        self.engine = str(engine or "unity")
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._seq = 0
        self._sent = 0
        self._snapshot: dict[str, Any] | None = None
        self.last_error = ""

    # -- lifecycle -----------------------------------------------------
    def start(self) -> str:
        if self._socket is not None:
            return self.url
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(MAX_CLIENTS)
        sock.settimeout(0.5)
        self._socket = sock
        self.port = int(sock.getsockname()[1])
        self._thread = threading.Thread(target=self._accept, name="bmp-bridge", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        self._thread = None

    @property
    def is_active(self) -> bool:
        return self._socket is not None

    @property
    def url(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def sent_count(self) -> int:
        return self._sent

    # -- connection handling -------------------------------------------
    def _accept(self) -> None:
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                client, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.settimeout(None)
            with self._lock:
                if len(self._clients) >= MAX_CLIENTS:
                    try:
                        client.close()
                    except OSError:
                        pass
                    continue
                self._clients.append(client)
            try:
                self._send_to(
                    client,
                    {
                        "type": "hello",
                        "protocol": BRIDGE_PROTOCOL,
                        "engine": self.engine,
                        "app": "Blendermp",
                    },
                )
                snapshot = self._snapshot
                if snapshot is not None:
                    welcome = {"seq": 0, "t": round(time.time(), 3)}
                    welcome.update(snapshot)
                    self._send_to(client, welcome)
            except OSError:
                self._drop(client)
                continue
            threading.Thread(
                target=self._read_client, args=(client,), name="bmp-bridge-client", daemon=True
            ).start()

    def _read_client(self, client: socket.socket) -> None:
        buffer = b""
        try:
            while not self._stop.is_set():
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE_BYTES:
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict) and message.get("type") == "ping":
                        self._send_to(client, {"type": "pong", "t": round(time.time(), 3)})
        except OSError:
            pass
        finally:
            self._drop(client)

    def _drop(self, client: socket.socket) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        try:
            client.close()
        except OSError:
            pass

    def _send_to(self, client: socket.socket, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        client.sendall(payload)

    # -- publishing ----------------------------------------------------
    def publish(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        with self._send_lock:
            self._seq += 1
            envelope = {"seq": self._seq, "t": round(time.time(), 3)}
            envelope.update(message)
            with self._lock:
                clients = list(self._clients)
            for client in clients:
                try:
                    self._send_to(client, envelope)
                    self._sent += 1
                except OSError:
                    self._drop(client)

    def publish_object(self, exporter: EngineExporter, obj: ExportedObject) -> None:
        message = exporter.export_object(obj)
        message["type"] = "object"
        self.publish(message)

    def publish_remove(self, uid: str) -> None:
        self.publish({"type": "remove", "uid": str(uid)[:64]})

    def publish_scene(
        self,
        exporter: EngineExporter,
        objects: Iterable[ExportedObject],
        materials: Iterable[ExportedMaterial] | None = None,
    ) -> None:
        message = exporter.export_scene(objects, materials)
        message["type"] = "scene"
        self._snapshot = message
        self.publish(message)

    def publish_materials(
        self, exporter: EngineExporter, materials: Iterable[ExportedMaterial]
    ) -> None:
        self.publish(
            {
                "type": "materials",
                "materials": [
                    exporter.export_material(material) for material in materials
                ],
            }
        )


class SceneStream:
    """A thin wrapper around an exporter plus a sink for incremental updates."""

    def __init__(self, exporter: EngineExporter, sink: Callable[[dict[str, Any]], None]):
        self.exporter = exporter
        self.sink = sink

    def push_object(self, obj: ExportedObject) -> None:
        self.sink({"type": "object", "data": self.exporter.export_object(obj)})

    def push_scene(
        self,
        objects: Iterable[ExportedObject],
        materials: Iterable[ExportedMaterial] | None = None,
    ) -> None:
        self.sink(
            {
                "type": "scene",
                "data": self.exporter.export_scene(objects, materials),
            }
        )
