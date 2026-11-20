"""Checkpoint client: observe a host restore with the session still connected."""

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

PORT = 52395
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def messages():
    return [message.text for message in wm.bmp.messages]


def cube():
    obj = bpy.data.objects.get("CkptCube")
    return [round(float(value), 3) for value in obj.location] if obj else None


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["cube"] = cube()
    checks["objects"] = sorted(obj.name for obj in context.scene.objects)
    checks["messages"] = messages()[-20:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    texts = messages()
    if "loaded" not in checks and any(text == "Host scene loaded" for text in texts):
        checks["loaded"] = round(elapsed, 2)
        checks["initial"] = cube()
    if "loaded" in checks and "moved" not in checks and cube() and abs(cube()[0] - 7.0) < 0.01:
        checks["moved"] = round(elapsed, 2)
        checks["moved_cube"] = cube()
    if "moved" in checks and "restored" not in checks:
        if cube() and abs(cube()[0] - 1.0) < 0.01 and sum(1 for text in texts if text.startswith("Replacing local scene")) >= 2:
            checks["restored"] = round(elapsed, 2)
            checks["restored_cube"] = cube()
            checks["mesh_objects"] = len([obj for obj in context.scene.objects if obj.type == "MESH"])
    if "restored" in checks:
        finish("restored")
        return
    if elapsed > 32.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
