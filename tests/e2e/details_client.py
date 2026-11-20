"""Object details end-to-end client."""

import bpy
import json
import os
import sys
import time

TEMP = os.environ["BMP_TEST_DATA"]
VENDOR = os.environ["BMP_VENDOR"]
ADDON = os.environ["BMP_ADDON"]
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

PORT = 52365
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def summary():
    bpy.context.view_layer.update()
    obj = bpy.data.objects.get("ChildCube")
    if obj is None:
        return None
    subsurf = next((mod for mod in obj.modifiers if mod.type == "SUBSURF"), None)
    collection = bpy.data.collections.get("Props")
    root = bpy.data.objects.get("RigRoot")
    return {
        "root_location": [round(float(v), 3) for v in root.location] if root else None,
        "root_world": [round(float(v), 3) for v in root.matrix_world.translation] if root else None,
        "parent": obj.parent.name if obj.parent else None,
        "world": [round(float(value), 3) for value in obj.matrix_world.translation],
        "local": [round(float(value), 3) for value in obj.matrix_basis.translation],
        "parent_inverse": [round(float(value), 3) for value in obj.matrix_parent_inverse.translation],
        "modifiers": sorted(mod.type for mod in obj.modifiers),
        "modifier_names": sorted(mod.name for mod in obj.modifiers),
        "subsurf_levels": int(subsurf.levels) if subsurf is not None else None,
        "color": [round(float(value), 3) for value in obj.color],
        "hide_render": bool(obj.hide_render),
        "collections": sorted(item.name for item in obj.users_collection),
        "collection_hide_render": bool(collection.hide_render) if collection is not None else None,
        "collection_hide_viewport": bool(collection.hide_viewport) if collection is not None else None,
    }


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["summary"] = summary()
    checks["leases"] = dict(bmp.runtime.get("lease_owners", {}))
    checks["owned"] = sorted(bmp.runtime.get("owned_leases", set()))
    checks["messages"] = [message.text for message in wm.bmp.messages][-12:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    data = summary()
    if data is not None and "initial" not in checks:
        checks["initial"] = data
        checks["initial_at"] = round(elapsed, 2)
    if data is not None and "changed" not in checks:
        if (
            abs(data["world"][0] - 6.0) <= 0.01
            and data["subsurf_levels"] == 1
            and abs(data["color"][0] - 0.9) <= 0.01
            and data["collection_hide_render"] is False
            and data["collection_hide_viewport"] is True
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


bpy.app.timers.register(tick, first_interval=0.25)
