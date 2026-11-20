"""Live topology end-to-end client."""

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

PORT = 52375
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def summary():
    obj = bpy.data.objects.get("TopoCube")
    if obj is None:
        return None
    return {
        "counts": [len(obj.data.vertices), len(obj.data.edges), len(obj.data.polygons)],
        "mode": context.mode,
        "mesh_objects": len([item for item in context.scene.objects if item.type == "MESH"]),
        "names": sorted(item.name for item in context.scene.objects),
        "finite": all(
            all(abs(value) < 1e7 for value in vertex.co) for vertex in obj.data.vertices
        ),
    }


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["summary"] = summary()
    checks["messages"] = [message.text for message in wm.bmp.messages][-12:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    data = summary()
    if data is not None and "first" not in checks and data["counts"][0] > 8:
        checks["first"] = data
        checks["first_at"] = round(elapsed, 2)
    if data is not None and "first" in checks and "second" not in checks:
        if data["counts"][0] > checks["first"]["counts"][0]:
            checks["second"] = data
            checks["second_at"] = round(elapsed, 2)
    if "second" in checks:
        checks["settled"] = summary()
        finish("second")
        return
    if elapsed > 30.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
