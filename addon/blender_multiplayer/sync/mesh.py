"""Blender mesh mirror.

The mirror reads positions with ``foreach_get``, hashes them in chunks, and sends
only changed vertices. Application is the fast path: write the shadow buffer,
``foreach_set`` once, ``mesh.update()``. No ``clear_geometry``, no rebuilding.

This module imports ``bpy`` lazily so the rest of the package (protocol, tests)
works outside Blender.
"""

from __future__ import annotations

import numpy as np

from ..core import chunker


def _require_bpy():
    try:
        import bpy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - only outside Blender
        raise RuntimeError("bpy is only available inside Blender") from exc


class MeshMirror:
    def __init__(self) -> None:
        self.shadow: np.ndarray | None = None
        self.hashes: np.ndarray | None = None
        self.counts: tuple[int, int, int, int] | None = None
        self.rev = 0

    @staticmethod
    def counts_of(mesh) -> tuple[int, int, int, int]:
        return (len(mesh.vertices), len(mesh.edges), len(mesh.loops), len(mesh.polygons))

    def matches(self, mesh) -> bool:
        if self.shadow is None:
            return False
        return self.counts == self.counts_of(mesh) and self.shadow.shape[0] == len(mesh.vertices)

    @staticmethod
    def read_positions(mesh) -> np.ndarray:
        count = len(mesh.vertices)
        buffer = np.empty(count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", buffer)
        return buffer.reshape(-1, 3)

    def full_snapshot(self, mesh) -> bytes:
        positions = self.read_positions(mesh)
        self.shadow = positions.copy()
        self.counts = self.counts_of(mesh)
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = 0
        return chunker.encode_positions(positions)

    def poll(self, mesh, epsilon: float = 1e-5) -> tuple[int, bytes] | None:
        """Return ``(rev, delta)`` when positions changed, else ``None``."""
        if not self.matches(mesh):
            return None
        positions = self.read_positions(mesh)
        scan = chunker.diff_positions(
            self.shadow,
            positions,
            epsilon=epsilon,
            previous_hashes=self.hashes,
        )
        self.hashes = scan.hashes
        if scan.count == 0:
            self.shadow = positions
            return None
        self.shadow = positions
        self.rev = (self.rev + 1) & 0xFFFFFFFF
        return self.rev, chunker.encode_delta(self.rev, scan.indices, scan.values)

    def apply_delta(self, mesh, payload: bytes) -> None:
        _require_bpy()
        if not self.matches(mesh):
            return
        rev, indices, values = chunker.decode_delta(payload)
        if self.shadow is None:
            return
        if indices.size and int(indices.max()) >= self.shadow.shape[0]:
            raise ValueError("delta index outside the local mesh")
        chunker.apply_positions(self.shadow, indices, values)
        mesh.vertices.foreach_set("co", self.shadow.ravel())
        mesh.update()
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = rev

    def apply_snapshot(self, mesh, payload: bytes) -> None:
        _require_bpy()
        positions = chunker.decode_positions(payload)
        if positions.shape[0] != len(mesh.vertices):
            # Topology differs. A full geometry rebuild is a later milestone;
            # refusing is safer than writing a mismatched buffer.
            return
        self.shadow = positions.copy()
        self.counts = self.counts_of(mesh)
        mesh.vertices.foreach_set("co", self.shadow.ravel())
        mesh.update()
        self.hashes = chunker.chunk_hashes(self.shadow)

    def remember(self, mesh) -> None:
        """Adopt the current mesh state without sending anything."""
        self.shadow = self.read_positions(mesh)
        self.counts = self.counts_of(mesh)
        self.hashes = chunker.chunk_hashes(self.shadow)
