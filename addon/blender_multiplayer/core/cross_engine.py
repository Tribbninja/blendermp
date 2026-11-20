"""Cross-engine sync contracts for Blendermp.

Blendermp is host-authoritative over Blender data. A cross-engine link exports
the same scene graph to another engine (Unity/Unreal) as a one-way, lossless
stream of snapshots and deltas. The engine side never edits the Blender scene;
it only renders it.

These classes are the stable contract. Concrete exporters live next to them and
are registered with ``EngineRegistry``. Nothing here imports ``bpy`` or an
engine SDK, so the module is testable and importable in plain Python.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class ExportedObject:
    uid: str
    name: str
    kind: str
    matrix: list[float]
    bounds: tuple[float, float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportedMaterial:
    name: str
    base_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    metadata: dict[str, Any] = field(default_factory=dict)


class EngineExporter(ABC):
    """Base class for every cross-engine exporter."""

    name: str = "engine"

    @abstractmethod
    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        ...

    @abstractmethod
    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        ...

    def export_scene(self, objects: Iterable[ExportedObject], materials: Iterable[ExportedMaterial] | None = None) -> dict[str, Any]:
        return {
            "engine": self.name,
            "objects": [self.export_object(obj) for obj in objects],
            "materials": [self.export_material(mat) for mat in (materials or [])],
        }


class UnityExporter(EngineExporter):
    name = "unity"

    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        return {"prefab": obj.name, "position": obj.matrix[12:15], "metadata": obj.metadata}

    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        return {"shader": "Standard", "color": list(material.base_color)}


class UnrealExporter(EngineExporter):
    name = "unreal"

    def export_object(self, obj: ExportedObject) -> dict[str, Any]:
        return {"actor": obj.name, "transform": obj.matrix, "metadata": obj.metadata}

    def export_material(self, material: ExportedMaterial) -> dict[str, Any]:
        return {"material": material.name, "baseColor": list(material.base_color)}


class EngineRegistry:
    def __init__(self):
        self._exporters: dict[str, EngineExporter] = {}

    def register(self, exporter: EngineExporter) -> None:
        self._exporters[exporter.name] = exporter

    def get(self, name: str) -> EngineExporter | None:
        return self._exporters.get(name)

    def names(self) -> list[str]:
        return sorted(self._exporters)


class SceneStream:
    """A thin wrapper around an exporter plus a sink for incremental updates."""

    def __init__(self, exporter: EngineExporter, sink: Callable[[dict[str, Any]], None]):
        self.exporter = exporter
        self.sink = sink

    def push_object(self, obj: ExportedObject) -> None:
        self.sink({"type": "object", "data": self.exporter.export_object(obj)})

    def push_scene(self, objects: Iterable[ExportedObject], materials: Iterable[ExportedMaterial] | None = None) -> None:
        self.sink({"type": "scene", "data": self.exporter.export_scene(objects, materials)})
