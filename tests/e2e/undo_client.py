"""Scoped undo client."""

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

PORT = 52405
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def cube():
    obj = bpy.data.objects.get("UndoCube")
    return obj


def finish(reason):
    obj = cube()
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["cube"] = [round(float(value), 3) for value in obj.location] if obj else None
    checks["messages"] = [message.text for message in wm.bmp.messages][-16:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    texts = [message.text for message in wm.bmp.messages]
    obj = cube()
    if "loaded" not in checks and any(text == "Host scene loaded" for text in texts):
        checks["loaded"] = round(elapsed, 2)
    if "loaded" in checks and "selected" not in checks and elapsed > checks["loaded"] + 0.5:
        obj.select_set(True)
        context.view_layer.objects.active = obj
        checks["selected"] = round(elapsed, 2)
    if "selected" in checks and "moved" not in checks and elapsed > checks["selected"] + 0.6:
        obj.location = (2.0, 0.0, 0.0)
        checks["moved"] = round(elapsed, 2)
        checks["moved_cube"] = [round(float(value), 3) for value in obj.location]
    if "moved" in checks and "undone" not in checks and elapsed > checks["moved"] + 1.0:
        bpy.ops.bmp.undo_shared()
        checks["undone"] = round(elapsed, 2)
        checks["undone_cube"] = [round(float(value), 3) for value in obj.location]
    if "undone" in checks and elapsed > checks["undone"] + 0.5:
        finish("undone")
        return
    if elapsed > 38.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
