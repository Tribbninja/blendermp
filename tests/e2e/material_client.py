"""Material/texture parity end-to-end joining client."""

import bpy
import json
import os
import sys
import time

TEMP = os.environ["BMP_TEST_DATA"]
VENDOR = os.environ["BMP_VENDOR"]
ADDON = os.environ["BMP_ADDON"]
SYNC = os.environ["BMP_SYNC"]
os.makedirs(TEMP, exist_ok=True)
os.environ["LOCALAPPDATA"] = TEMP
sys.path.insert(0, VENDOR)
sys.path.insert(0, ADDON)

import blender_multiplayer as bmp
from blender_multiplayer.core.roomcode import encode_room

bpy.ops.preferences.addon_enable(module="blender_multiplayer")
context = bpy.context
wm = context.window_manager
wm.bmp.name = "testclient"
wm.bmp.use_relay = False
wm.bmp.sync_mesh = True
wm.bmp.sync_transforms = True
wm.bmp.sync_materials = True
wm.bmp.sync_modifiers = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52355
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def summary():
    obj = bpy.data.objects.get("MatCube")
    if obj is None or not obj.material_slots or obj.material_slots[0].material is None:
        return None
    material = obj.material_slots[0].material
    tree = material.node_tree
    if tree is None:
        return None
    texture = tree.nodes.get("Brick Texture")
    mapping = tree.nodes.get("Tile Mapping")
    bump = tree.nodes.get("Surface Bump")
    uv = tree.nodes.get("UV Source")
    bsdf = next((node for node in tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    return {
        "material": material.name,
        "nodes": len(tree.nodes),
        "node_types": sorted(node.bl_idname for node in tree.nodes),
        "links": len(tree.links),
        "image": texture.image.name if texture is not None and texture.image else None,
        "image_size": list(texture.image.size) if texture is not None and texture.image else None,
        "image_packed": bool(texture.image.packed_file) if texture is not None and texture.image else False,
        "colorspace": texture.image.colorspace_settings.name if texture is not None and texture.image else None,
        "interpolation": texture.interpolation if texture is not None else None,
        "mapping": list(mapping.inputs["Scale"].default_value) if mapping is not None else None,
        "uv_map": uv.uv_map if uv is not None else None,
        "bump_strength": round(float(bump.inputs["Strength"].default_value), 3) if bump is not None else None,
        "base_linked": bool(bsdf.inputs["Base Color"].is_linked) if bsdf is not None else False,
        "normal_linked": bool(bsdf.inputs["Normal"].is_linked) if bsdf is not None else False,
        "roughness": round(float(bsdf.inputs["Roughness"].default_value), 3) if bsdf is not None else None,
        "emission": round(float(bsdf.inputs["Emission Strength"].default_value), 3) if bsdf is not None else None,
    }


def close_to(values, expected, tolerance=0.001):
    return isinstance(values, list) and len(values) == len(expected) and all(
        abs(float(value) - expected[index]) <= tolerance for index, value in enumerate(values)
    )


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["summary"] = summary()
    checks["messages"] = [message.text for message in wm.bmp.messages][-16:]
    checks["received_images"] = len(bmp.runtime.get("received_images", {}))
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    data = summary()
    if data is not None and "initial" not in checks:
        checks["initial"] = data
        checks["initial_at"] = round(elapsed, 2)
    if data is not None and data.get("image") and "image_seen" not in checks:
        checks["image_seen"] = round(elapsed, 2)
        checks["image_seen_size"] = data.get("image_size")
    if data is not None and "changed" not in checks:
        if (
            close_to(data.get("mapping"), [4.0, 5.0, 1.0])
            and data.get("interpolation") == "Closest"
            and data.get("emission") == 2.5
            and data.get("roughness") == 0.21
        ):
            checks["changed"] = data
            checks["changed_at"] = round(elapsed, 2)
    if "changed" in checks:
        checks["settled"] = summary()
        finish("changed")
        return
    if elapsed > 28.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25, persistent=True)
