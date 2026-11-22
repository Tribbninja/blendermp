"""Lock host: hold the default cube so the client must see it locked."""

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
wm.bmp.name = "lockhost"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

cube = bpy.data.objects.get("Cube")
if cube is None:
    bpy.ops.mesh.primitive_cube_add()
    cube = context.active_object
    cube.name = "Cube"
for obj in list(bpy.context.selected_objects):
    obj.select_set(False)
cube.select_set(True)
context.view_layer.objects.active = cube

PORT = 52468
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")


def tick():
    elapsed = time.monotonic() - start
    uid = cube.get("bmp_uuid")
    data = {
        "state": wm.bmp.state,
        "bootstrapping": bool(bmp.runtime.get("bootstrapping")),
        "owner": bmp.runtime["lease_owners"].get(uid) if uid else None,
        "location": [round(float(v), 3) for v in cube.location],
        "selected": bool(cube.select_get()),
        "hide_select": bool(cube.hide_select),
    }
    if elapsed > 8.0 and "deselected" not in globals():
        globals()["deselected"] = elapsed
        cube.select_set(False)
    if elapsed > 16.0:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
