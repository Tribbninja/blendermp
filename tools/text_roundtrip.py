import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
import blender_multiplayer as bmp
from blender_multiplayer.core import object_payload

uid = "33333333-3333-3333-3333-333333333333"
bmp._apply_object_create(bpy.context, {"uuid": uid, "kind": object_payload.KIND_FONT, "meta": {"name": "RemoteText", "matrix": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], "body": "Hello", "size": 2.0, "align_x": "CENTER", "align_y": "CENTER"}, "geometry": b""})
obj = bpy.data.objects.get("RemoteText")
print("text", bool(obj), obj.type if obj else None, obj.data.body if obj else None, obj.data.size if obj else None)
bmp._apply_object_delete(bpy.context, {"uuid": uid})
print("BMP_TEXT_OK")
