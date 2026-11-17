import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
import blender_multiplayer as bmp
from blender_multiplayer.core import object_payload
from blender_multiplayer.sync.mesh import MeshMirror

src = bpy.data.meshes.new("src")
src.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)], [], [(0,1,2,3)])
src.update()
payload = MeshMirror().full_snapshot(src)
uid = "12345678-1234-5678-1234-567812345678"
matrix = [1,0,0,0, 0,1,0,0, 0,0,1,0, 5,6,7,1]
bmp._apply_object_create(bpy.context, {"uuid": uid, "kind": object_payload.KIND_MESH, "meta": {"name": "RemoteCube", "matrix": matrix}, "geometry": payload})
obj = bpy.data.objects.get("RemoteCube")
print("created", bool(obj), obj.location[:] if obj else None, len(obj.data.vertices) if obj else 0)
bmp._apply_object_delete(bpy.context, {"uuid": uid})
print("deleted", bpy.data.objects.get("RemoteCube") is None)

light_uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
bmp._apply_object_create(bpy.context, {"uuid": light_uid, "kind": object_payload.KIND_LIGHT, "meta": {"name": "RemoteSun", "matrix": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], "light_type": "SUN", "energy": 4.0, "color": [1,0.8,0.6]}, "geometry": b""})
light = bpy.data.objects.get("RemoteSun")
print("light", bool(light), light.data.type if light else None, light.data.energy if light else None)
bmp._apply_object_delete(bpy.context, {"uuid": light_uid})
print("BMP_OBJECT_OK")
