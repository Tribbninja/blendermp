"""Headless mesh/material performance harness.

Run inside Blender:

    blender --background --factory-startup --python bench/bench_mesh.py -- --write-baseline

Options:
  --sizes 10000,100000,1000000   vertex counts
  --repeat 3                    median of N runs for codec timings
  --write-baseline              replace bench/baseline.json
  --compare                     fail when a metric regresses >20%
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import bpy
import bmesh
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))

from blender_multiplayer.core import chunker  # noqa: E402
from blender_multiplayer.sync.mesh import MeshMirror, snapshot_bmesh  # noqa: E402

BENCH = ROOT / "bench"
BASELINE = BENCH / "baseline.json"
RESULTS = BENCH / "results.json"


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="10000,100000,1000000")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--compare", action="store_true")
    return parser.parse_args(argv)


def median_ms(function, repeat: int):
    samples = []
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - start) * 1000.0)
    return round(statistics.median(samples), 3), result


def positions_for(count: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((count, 3)) * 0.01).astype(np.float32)


def read_ms(count: int) -> float:
    mesh = bpy.data.meshes.new(f"BenchRead{count}")
    mesh.vertices.add(count)
    mesh.vertices.foreach_set("co", positions_for(count).ravel())
    mesh.update()
    mirror = MeshMirror()
    elapsed, _ = median_ms(lambda: mirror.read_positions(mesh), 3)
    bpy.data.meshes.remove(mesh)
    return elapsed


def delta_metrics(count: int, repeat: int):
    positions = positions_for(count)
    indices = np.arange(0, count, max(1, count // 200), dtype=np.int64)
    values = positions[indices] + np.float32(0.001)
    encode_ms, payload = median_ms(lambda: chunker.encode_delta(1, indices, values), repeat)
    decode_ms, decoded = median_ms(lambda: chunker.decode_delta(payload), repeat)
    shadow = positions.copy()
    apply_ms, _ = median_ms(lambda: chunker.apply_positions(shadow, decoded[1], decoded[2]), repeat)
    encoded = payload
    return {
        "changed_vertices": int(indices.size),
        "encoded_bytes": len(encoded),
        "encode_ms": encode_ms,
        "decode_ms": decode_ms,
        "apply_ms": apply_ms,
    }


def geometry_payload(count: int):
    rows = max(2, int(count**0.5))
    cells = rows - 1
    coordinates = np.empty((rows * rows, 3), dtype=np.float32)
    xs, ys = np.meshgrid(np.arange(rows), np.arange(rows))
    coordinates[:, 0] = xs.ravel() * 0.1
    coordinates[:, 1] = ys.ravel() * 0.1
    coordinates[:, 2] = 0.0

    index = np.arange(cells * cells, dtype=np.uint32)
    row = index // cells
    column = index % cells
    first = row * rows + column
    loops = np.stack((first, first + 1, first + rows + 1, first + rows), axis=1).astype(np.uint32)
    starts = (index * 4).astype(np.uint32)
    totals = np.full(cells * cells, 4, dtype=np.uint32)
    smooth = np.zeros(cells * cells, dtype=np.uint8)
    materials = np.zeros(cells * cells, dtype=np.uint32)

    grid_rows, grid_columns = np.meshgrid(np.arange(rows), np.arange(rows - 1), indexing="ij")
    horizontal_first = (grid_rows * rows + grid_columns).ravel().astype(np.uint32)
    horizontal = np.stack((horizontal_first, horizontal_first + 1), axis=1)
    grid_rows, grid_columns = np.meshgrid(np.arange(rows - 1), np.arange(rows), indexing="ij")
    vertical_first = (grid_rows * rows + grid_columns).ravel().astype(np.uint32)
    vertical = np.stack((vertical_first, vertical_first + rows), axis=1)
    edges = np.concatenate((horizontal, vertical), axis=0)

    payload = chunker.pack_geometry(
        chunker.encode_geometry(
            coordinates,
            edges,
            loops.ravel(),
            starts,
            totals,
            smooth,
            materials,
        )
    )
    return payload, coordinates


def topology_metrics(count: int):
    payload, coordinates = geometry_payload(count)
    mesh = bpy.data.meshes.new(f"BenchTopo{count}")
    mirror = MeshMirror()
    apply_ms, _ = median_ms(lambda: mirror.apply_snapshot(mesh, payload), 2)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    snapshot_ms, _ = median_ms(lambda: snapshot_bmesh(bm), 2)
    bm.free()
    bpy.data.meshes.remove(mesh)
    return {
        "decoded_vertices": int(coordinates.shape[0]),
        "payload_bytes": len(payload),
        "apply_ms": apply_ms,
        "snapshot_ms": snapshot_ms,
    }


def material_metrics():
    from blender_multiplayer import _apply_material_graph, material_graph
    from blender_multiplayer.core import material as material_payload

    image = bpy.data.images.new("BenchTexture", 16, 16, alpha=True)
    image.pixels.foreach_set([0.5, 0.25, 0.1, 1.0] * (16 * 16))
    image.pack()
    material = bpy.data.materials.new("BenchGraph")
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(node for node in tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    mapping = tree.nodes.new("ShaderNodeMapping")
    uv = tree.nodes.new("ShaderNodeUVMap")
    bump = tree.nodes.new("ShaderNodeBump")
    tree.links.new(uv.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
    tree.links.new(texture.outputs["Color"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    encode_ms, data = median_ms(lambda: material_graph(material), 3)
    sanitize_ms, clean = median_ms(lambda: material_payload.sanitize({"name": material.name, **{}, "nodes": data[0], "links": data[1]}), 3)
    copy = bpy.data.materials.new("BenchGraphCopy")
    copy.use_nodes = True
    apply_ms, _ = median_ms(lambda: _apply_material_graph(copy, clean), 3)
    bpy.data.materials.remove(copy)
    bpy.data.materials.remove(material)
    bpy.data.images.remove(image)
    return {
        "nodes": len(data[0]),
        "links": len(data[1]),
        "graph_ms": encode_ms,
        "sanitize_ms": sanitize_ms,
        "apply_ms": apply_ms,
    }


def machine() -> dict:
    return {
        "platform": platform.platform(),
        "processor": platform.processor()[:200],
        "blender": bpy.app.version_string,
        "numpy": np.__version__,
        "python": platform.python_version(),
    }


def main():
    args = parse_args()
    sizes = [int(item) for item in args.sizes.split(",") if item.strip()]
    result = {"machine": machine(), "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sizes": {}}
    print("Blendermp bench", result["machine"], flush=True)
    for count in sizes:
        entry = {"read_ms": read_ms(count)}
        entry.update(delta_metrics(count, args.repeat))
        if count <= 250_000:
            entry["topology"] = topology_metrics(count)
        result["sizes"][str(count)] = entry
        print(
            f"{count:>9} verts  read={entry['read_ms']}ms  "
            f"delta enc={entry['encode_ms']}ms dec={entry['decode_ms']}ms apply={entry['apply_ms']}ms",
            flush=True,
        )
    result["material"] = material_metrics()
    print("material", result["material"], flush=True)
    RESULTS.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if args.write_baseline:
        BASELINE.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"baseline written: {BASELINE}", flush=True)

    if args.compare and BASELINE.is_file():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        failures = []

        def compare(path, current, previous, tolerance=1.2):
            if isinstance(previous, dict) and isinstance(current, dict):
                for key in previous:
                    if key in current:
                        compare(f"{path}.{key}", current[key], previous[key], tolerance)
            elif isinstance(previous, (int, float)) and isinstance(current, (int, float)):
                if previous > 0 and current > previous * tolerance:
                    failures.append(f"{path}: {current:.3f} > {previous * tolerance:.3f}")

        compare("sizes", result.get("sizes", {}), baseline.get("sizes", {}))
        compare("material", result.get("material", {}), baseline.get("material", {}))
        if failures:
            print("PERF REGRESSION", flush=True)
            for failure in failures:
                print(" ", failure, flush=True)
            bpy.ops.wm.quit_blender()
            raise SystemExit(1)

    bpy.ops.wm.quit_blender()


if __name__ == "__main__":
    main()
