"""Sculpt client."""

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

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52425
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def vertex0():
    obj = bpy.data.objects.get("SculptSphere")
    if obj is None or not obj.data.vertices:
        return None
    return [round(float(value), 3) for value in obj.data.vertices[0].co]


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["vertex0"] = vertex0()
    checks["messages"] = [message.text for message in wm.bmp.messages][-12:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    data = vertex0()
    if data is not None and "initial" not in checks:
        checks["initial"] = data
    if "initial" in checks and "moved" not in checks and data is not None:
        if abs(data[0] - checks["initial"][0]) > 0.1:
            checks["moved"] = data
            checks["moved_at"] = round(elapsed, 2)
    if "moved" in checks and elapsed > checks["moved_at"] + 0.6:
        finish("moved")
        return
    if elapsed > 28.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
