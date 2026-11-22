"""Blender mesh mirror.

Positions follow the fast path: chunked hashing, changed-vertex deltas and a
single ``foreach_set``. Structural edits fall back to a full geometry snapshot
that rebuilds vertices, edges, loops and polygons, including UVs and material
indices. That rebuild only runs when topology actually changed, not per tick.
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

    @staticmethod
    def read_geometry(mesh) -> dict:
        positions = MeshMirror.read_positions(mesh)
        edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        if edges.size:
            mesh.edges.foreach_get("vertices", edges)
        loops = np.empty(len(mesh.loops), dtype=np.int32)
        if loops.size:
            mesh.loops.foreach_get("vertex_index", loops)
        starts = np.empty(len(mesh.polygons), dtype=np.int32)
        totals = np.empty(len(mesh.polygons), dtype=np.int32)
        smooth = np.empty(len(mesh.polygons), dtype=bool)
        materials = np.empty(len(mesh.polygons), dtype=np.int32)
        if len(mesh.polygons):
            mesh.polygons.foreach_get("loop_start", starts)
            mesh.polygons.foreach_get("loop_total", totals)
            mesh.polygons.foreach_get("use_smooth", smooth)
            mesh.polygons.foreach_get("material_index", materials)

        uv_name = ""
        uvs = np.zeros(0, dtype=np.float32)
        uv_layers = []
        for layer in mesh.uv_layers:
            count = len(layer.data)
            if not count:
                uv_layers.append((layer.name, np.zeros((0, 2), dtype=np.float32)))
                continue
            values = np.empty(count * 2, dtype=np.float32)
            try:
                layer.data.foreach_get("uv", values)
            except (TypeError, ValueError):
                # A UV layer can briefly disagree with the loop count while the
                # mesh is being edited or rebuilt. Skip it instead of aborting
                # the whole scene bootstrap.
                uv_layers.append((layer.name, np.zeros((0, 2), dtype=np.float32)))
                continue
            uv_layers.append((layer.name, values.reshape(-1, 2)))
        try:
            active_uv = int(mesh.uv_layers.active_index)
        except (AttributeError, TypeError, ValueError):
            active_uv = 0
        if uv_layers:
            active_uv = max(0, min(len(uv_layers) - 1, active_uv))
            uv_name = uv_layers[active_uv][0]
            uvs = uv_layers[active_uv][1].ravel()

        return {
            "positions": positions,
            "edges": edges.reshape(-1, 2),
            "loops": loops,
            "loop_starts": starts,
            "loop_totals": totals,
            "smooth": smooth.astype(np.uint8),
            "materials": materials.astype(np.uint32),
            "uv_name": uv_name,
            "uvs": uvs.reshape(-1, 2),
            "uv_layers": uv_layers,
            "uv_active": active_uv,
        }

    def full_snapshot(self, mesh) -> bytes:
        geometry = self.read_geometry(mesh)
        self.shadow = geometry["positions"].copy()
        self.counts = self.counts_of(mesh)
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = 0
        if len(geometry["uv_layers"]) > 1 or (geometry["uv_layers"] and geometry["uv_active"] != 0):
            encoded = chunker.encode_geometry_layers(
                geometry["positions"],
                geometry["edges"],
                geometry["loops"],
                geometry["loop_starts"],
                geometry["loop_totals"],
                geometry["smooth"],
                geometry["materials"],
                geometry["uv_layers"],
                geometry["uv_active"],
            )
        else:
            encoded = chunker.encode_geometry(
                geometry["positions"],
                geometry["edges"],
                geometry["loops"],
                geometry["loop_starts"],
                geometry["loop_totals"],
                geometry["smooth"],
                geometry["materials"],
                geometry["uv_name"] or None,
                geometry["uvs"] if geometry["uvs"].size else None,
            )
        return chunker.pack_geometry(encoded)

    def poll(self, mesh, epsilon: float = 1e-5) -> tuple[int, bytes] | None:
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
        if indices.size and indices.size < max(64, self.shadow.shape[0] // 4):
            # A small drag: touching only the changed vertices avoids copying
            # the whole position buffer through foreach_set.
            for index, value in zip(indices, values):
                mesh.vertices[int(index)].co = (
                    float(value[0]),
                    float(value[1]),
                    float(value[2]),
                )
        else:
            mesh.vertices.foreach_set("co", self.shadow.ravel())
        mesh.update()
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = rev

    def apply_snapshot(self, mesh, payload: bytes) -> None:
        _require_bpy()
        if chunker.is_geometry(payload):
            self._apply_geometry(mesh, payload)
            return
        positions = chunker.decode_positions(payload)
        if positions.shape[0] != len(mesh.vertices):
            return
        self.shadow = positions.copy()
        self.counts = self.counts_of(mesh)
        mesh.vertices.foreach_set("co", self.shadow.ravel())
        mesh.update()
        self.hashes = chunker.chunk_hashes(self.shadow)

    def _apply_geometry(self, mesh, payload: bytes) -> None:
        geometry = chunker.decode_geometry(payload)
        positions = geometry["positions"]
        edges = geometry["edges"]
        loops = geometry["loops"]
        starts = geometry["loop_starts"]
        totals = geometry["loop_totals"]
        polygons = starts.size

        mesh.clear_geometry()
        mesh.vertices.add(positions.shape[0])
        mesh.edges.add(edges.shape[0])
        mesh.loops.add(loops.size)
        mesh.polygons.add(polygons)
        if positions.size:
            mesh.vertices.foreach_set("co", positions.ravel())
        if edges.size:
            mesh.edges.foreach_set("vertices", edges.ravel())
        if loops.size:
            mesh.loops.foreach_set("vertex_index", loops)
        if polygons:
            mesh.polygons.foreach_set("loop_start", starts)
            mesh.polygons.foreach_set("loop_total", totals)
            mesh.polygons.foreach_set("use_smooth", geometry["smooth"].astype(bool))
            mesh.polygons.foreach_set("material_index", geometry["materials"].astype(np.int32))
        layers = geometry.get("uv_layers") or []
        if layers:
            for layer in list(mesh.uv_layers):
                mesh.uv_layers.remove(layer)
            created_layers = []
            for name, layer_uvs in layers[:16]:
                layer = mesh.uv_layers.new(name=name or "UVMap")
                layer.data.foreach_set("uv", np.asarray(layer_uvs, dtype=np.float32).ravel())
                created_layers.append(layer)
            if created_layers:
                active = max(0, min(len(created_layers) - 1, int(geometry.get("uv_active", 0))))
                try:
                    mesh.uv_layers.active_index = active
                except (AttributeError, TypeError, ValueError):
                    pass
        elif geometry["uvs"].size:
            layer = mesh.uv_layers.active
            if layer is None:
                layer = mesh.uv_layers.new(name=geometry["uv_name"] or "UVMap")
            layer.data.foreach_set("uv", geometry["uvs"].ravel())

        self.shadow = positions.copy()
        self.counts = self.counts_of(mesh)
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = 0
        # Building corners directly leaves their edge indices unset, which
        # makes Blender's Bevel modifier read out of bounds. Validate once
        # here to rebuild the corner/edge links before modifiers run.
        try:
            mesh.validate(clean_customdata=False)
        except TypeError:
            mesh.validate()
        mesh.update()

    def remember(self, mesh) -> None:
        self.shadow = self.read_positions(mesh)
        self.counts = self.counts_of(mesh)
        self.hashes = chunker.chunk_hashes(self.shadow)


def _snapshot_arrays(bm):
    """Read a BMesh's geometry as numpy arrays.

    The fast path converts the BMesh to a temporary ``Mesh`` in C and uses
    ``foreach_get`` for every array. The old pure-Python loops cost roughly a
    second on a 100k-vertex mesh; this path is a few tens of milliseconds. The
    slow path is kept as a fallback for Blender builds where ``to_mesh`` or a
    ``foreach_get`` attribute is unavailable.
    """
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    active_name = ""
    try:
        active_layer = bm.loops.layers.uv.active
        if active_layer is not None:
            active_name = active_layer.name
    except (AttributeError, TypeError, RuntimeError):
        active_name = ""

    _require_bpy()
    import bpy

    tmp = bpy.data.meshes.new("__bmp_topology_tmp__")
    try:
        bm.to_mesh(tmp)
        nv, ne, nl, npoly = (
            len(tmp.vertices),
            len(tmp.edges),
            len(tmp.loops),
            len(tmp.polygons),
        )
        positions = np.empty(nv * 3, dtype=np.float32)
        tmp.vertices.foreach_get("co", positions)
        edges = np.empty(ne * 2, dtype=np.uint32)
        if ne:
            tmp.edges.foreach_get("vertices", edges)
        loops = np.empty(nl, dtype=np.uint32)
        if nl:
            tmp.loops.foreach_get("vertex_index", loops)
        starts = np.empty(npoly, dtype=np.uint32)
        totals = np.empty(npoly, dtype=np.uint32)
        materials = np.empty(npoly, dtype=np.uint32)
        smooth_bool = np.empty(npoly, dtype=bool)
        if npoly:
            tmp.polygons.foreach_get("loop_start", starts)
            tmp.polygons.foreach_get("loop_total", totals)
            tmp.polygons.foreach_get("material_index", materials)
            tmp.polygons.foreach_get("use_smooth", smooth_bool)
        smooth = smooth_bool.astype(np.uint8)
        uv_layers = []
        active_uv = 0
        for layer_index, layer in enumerate(tmp.uv_layers):
            if layer.name == active_name:
                active_uv = layer_index
            values = np.empty(nl * 2, dtype=np.float32)
            if nl:
                layer.data.foreach_get("uv", values)
            uv_layers.append((layer.name, values))
        return (
            positions.reshape(-1, 3),
            edges,
            loops,
            starts,
            totals,
            smooth,
            materials,
            uv_layers,
            active_uv,
        )
    finally:
        try:
            bpy.data.meshes.remove(tmp)
        except (ReferenceError, RuntimeError):
            pass


def _snapshot_arrays_slow(bm):
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    verts = len(bm.verts)
    positions = np.empty(verts * 3, dtype=np.float32)
    try:
        bm.verts.foreach_get("co", positions)
    except (AttributeError, TypeError, RuntimeError):
        for index, vert in enumerate(bm.verts):
            positions[index * 3 : index * 3 + 3] = (vert.co.x, vert.co.y, vert.co.z)
    edges = np.empty(len(bm.edges) * 2, dtype=np.uint32)
    for index, edge in enumerate(bm.edges):
        edges[index * 2] = edge.verts[0].index
        edges[index * 2 + 1] = edge.verts[1].index
    loops: list[int] = []
    starts: list[int] = []
    totals: list[int] = []
    smooth: list[int] = []
    materials: list[int] = []
    for face in bm.faces:
        start = len(loops)
        face_loops = list(face.loops)
        loops.extend(loop.vert.index for loop in face_loops)
        starts.append(start)
        totals.append(len(face_loops))
        smooth.append(1 if face.smooth else 0)
        materials.append(int(face.material_index))
    uv_layers = []
    active_uv = 0
    try:
        active_layer = bm.loops.layers.uv.active
        for layer_index, layer in enumerate(bm.loops.layers.uv):
            if layer == active_layer:
                active_uv = layer_index
            values = []
            for face in bm.faces:
                for loop in face.loops:
                    uv = loop[layer].uv
                    values.extend((uv.x, uv.y))
            if len(values) == len(loops) * 2:
                uv_layers.append((layer.name, np.asarray(values, dtype=np.float32)))
    except (AttributeError, TypeError, RuntimeError):
        uv_layers = []
        active_uv = 0
    return (
        positions.reshape(-1, 3),
        edges,
        np.asarray(loops, dtype=np.uint32),
        np.asarray(starts, dtype=np.uint32),
        np.asarray(totals, dtype=np.uint32),
        np.asarray(smooth, dtype=np.uint8),
        np.asarray(materials, dtype=np.uint32),
        uv_layers,
        active_uv,
    )


def snapshot_bmesh(bm) -> bytes:
    """Encode the live Edit Mode BMesh as a full compressed geometry snapshot."""
    try:
        arrays = _snapshot_arrays(bm)
    except Exception:  # noqa: BLE001 - fall back to the portable path
        arrays = _snapshot_arrays_slow(bm)
    (
        positions,
        edges,
        loops,
        starts,
        totals,
        smooth,
        materials,
        uv_layers,
        active_uv,
    ) = arrays
    if uv_layers:
        geometry = chunker.encode_geometry_layers(
            positions,
            edges,
            loops,
            starts,
            totals,
            smooth,
            materials,
            uv_layers,
            active_uv,
        )
    else:
        geometry = chunker.encode_geometry(
            positions,
            edges,
            loops,
            starts,
            totals,
            smooth,
            materials,
        )
    return chunker.pack_geometry(geometry)


class EditMirror:
    """Position mirror for a mesh that is currently in Edit Mode.

    Blender does not write edit-mode vertex positions into ``mesh.vertices``
    until the user leaves Edit Mode; they live in the BMesh. This class reads and
    writes that BMesh directly so vertex dragging can be streamed live.
    """

    def __init__(self) -> None:
        self.shadow: np.ndarray | None = None
        self.hashes: np.ndarray | None = None
        self.count = 0
        self.rev = 0

    @staticmethod
    def read(bm) -> np.ndarray:
        bm.verts.ensure_lookup_table()
        count = len(bm.verts)
        flat = np.empty(count * 3, dtype=np.float32)
        # Blender 5.x has no foreach_get on BMesh sequences; keep the fast path
        # for builds that do and fall back to a Python loop.
        try:
            bm.verts.foreach_get("co", flat)
            return flat.reshape(-1, 3)
        except (AttributeError, TypeError, RuntimeError):
            pass
        for index, vert in enumerate(bm.verts):
            flat[index * 3] = vert.co.x
            flat[index * 3 + 1] = vert.co.y
            flat[index * 3 + 2] = vert.co.z
        return flat.reshape(-1, 3)

    def matches(self, bm) -> bool:
        return self.shadow is not None and len(bm.verts) == self.shadow.shape[0]

    def remember(self, bm) -> None:
        self.shadow = self.read(bm)
        self.count = self.shadow.shape[0]
        self.hashes = chunker.chunk_hashes(self.shadow)

    def poll(self, bm, epsilon: float = 1e-5) -> tuple[int, bytes] | None:
        if not self.matches(bm):
            return None
        positions = self.read(bm)
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

    def apply_delta(self, bm, payload: bytes) -> None:
        if not self.matches(bm):
            self.remember(bm)
        rev, indices, values = chunker.decode_delta(payload)
        if self.shadow is None:
            return
        if indices.size and int(indices.max()) >= self.shadow.shape[0]:
            raise ValueError("delta index outside the local edit mesh")
        chunker.apply_positions(self.shadow, indices, values)
        bm.verts.ensure_lookup_table()
        bm_verts = bm.verts
        for index, value in zip(indices, values):
            bm_verts[int(index)].co = (float(value[0]), float(value[1]), float(value[2]))
        bm.normal_update()
        self.hashes = chunker.chunk_hashes(self.shadow)
        self.rev = rev
