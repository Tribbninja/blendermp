import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
import blender_multiplayer as bmp

src = bpy.data.objects.new("Src", bpy.data.meshes.new("srcmesh"))
bpy.context.scene.collection.objects.link(src)
src["bmp_uuid"] = "11111111-1111-1111-1111-111111111111"
sub = src.modifiers.new("Subdivision", "SUBSURF"); sub.levels = 2
arr = src.modifiers.new("Array", "ARRAY"); arr.count = 4
snapshot = bmp.modifier_snapshot(src)
print("snapshot", [(m["type"], m["name"], m["props"].get("levels"), m["props"].get("count")) for m in snapshot])

dst = bpy.data.objects.new("Dst", bpy.data.meshes.new("dstmesh"))
bpy.context.scene.collection.objects.link(dst)
dst["bmp_uuid"] = "22222222-2222-2222-2222-222222222222"
bmp._apply_modifier_stack(bpy.context, {"uuid": dst["bmp_uuid"], "modifiers": snapshot})
print("dst", [(m.type, m.name, getattr(m, "levels", None), getattr(m, "count", None)) for m in dst.modifiers])
print("BMP_MODIFIER_OK")
