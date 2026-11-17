import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
from blender_multiplayer.sync.mesh import MeshMirror

verts = [(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1)]
faces = [(0,1,2,3),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
src = bpy.data.meshes.new("src")
src.from_pydata(verts, [], faces)
src.update()
layer = src.uv_layers.new(name="UVMap")
layer.data.foreach_set("uv", [0.0] * (len(src.loops) * 2))
for poly in src.polygons:
    poly.use_smooth = True

mirror = MeshMirror()
payload = mirror.full_snapshot(src)
print("payload", len(payload), "counts", mirror.counts)

dst = bpy.data.meshes.new("dst")
target = MeshMirror()
target.apply_snapshot(dst, payload)
print("dst counts", (len(dst.vertices), len(dst.edges), len(dst.loops), len(dst.polygons)))
print("uv", len(dst.uv_layers), "smooth", dst.polygons[0].use_smooth)
print("match", (len(dst.vertices) == len(src.vertices) and len(dst.polygons) == len(src.polygons)))
print("BMP_MESH_OK")
