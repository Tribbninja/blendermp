"""Reconnect/resume client: force a socket drop, then verify the scene is kept."""

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

PORT = 52385
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def messages():
    return [message.text for message in wm.bmp.messages]


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["objects"] = sorted(obj.name for obj in context.scene.objects)
    cube = bpy.data.objects.get("ResumeCube")
    checks["cube"] = [round(float(value), 3) for value in cube.location] if cube else None
    checks["messages"] = messages()[-20:]
    checks["replacing"] = sum(1 for text in messages() if text.startswith("Replacing local scene"))
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    texts = messages()
    if "loaded" not in checks and any(text == "Host scene loaded" for text in texts):
        checks["loaded"] = round(elapsed, 2)
    if "loaded" in checks and "dropped" not in checks and elapsed > checks["loaded"] + 2.0:
        session = bmp.runtime.get("session")
        writer = getattr(session, "_writer", None) if session else None
        loop = getattr(session, "_loop", None) if session else None
        if writer is not None and loop is not None:
            loop.call_soon_threadsafe(writer.close)
            offline_cube = bpy.data.objects.get("JournalCube")
            if offline_cube is not None:
                offline_cube.location = (2.0, 3.0, 4.0)
            checks["dropped"] = round(elapsed, 2)
    if "dropped" in checks and "reconnected" not in checks:
        if any(text == "Reconnected - scene kept" for text in texts):
            checks["reconnected"] = round(elapsed, 2)
            cube = bpy.data.objects.get("ResumeCube")
            checks["kept"] = cube is not None and len(context.scene.objects) > 0
    if "reconnected" in checks and "conflict" not in checks:
        if any("conflict with host changes" in text for text in texts):
            checks["conflict"] = round(elapsed, 2)
            bpy.ops.bmp.conflict_keep_mine()
    if "reconnected" in checks:
        cube = bpy.data.objects.get("ResumeCube")
        if cube is not None and abs(float(cube.location.x) - 3.0) < 0.01:
            checks["moved"] = [round(float(value), 3) for value in cube.location]
            finish("resumed")
            return
    if elapsed > 38.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
