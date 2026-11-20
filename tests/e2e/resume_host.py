"""Reconnect/resume host: drop the client socket mid-session and keep editing."""

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
wm.bmp.name = "testhost"
wm.bmp.use_relay = False
wm.bmp.sync_mesh = True
wm.bmp.sync_transforms = True
wm.bmp.sync_materials = True
wm.bmp.sync_modifiers = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
cube = context.active_object
cube.name = "ResumeCube"
bpy.ops.mesh.primitive_cube_add(location=(0.0, 1.0, 0.0))
journal_cube = context.active_object
journal_cube.name = "JournalCube"
bpy.ops.object.select_all(action="DESELECT")

PORT = 52385
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
phase = {}
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]


def publish():
    temp = SYNC + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(phase, handle)
    os.replace(temp, SYNC)


def finish(reason):
    session = bmp.runtime.get("session")
    data = {
        "state": getattr(session, "state", None),
        "reason": reason,
        "objects": sorted(obj.name for obj in context.scene.objects),
        "cube": [round(value, 3) for value in cube.location],
        "journal_cube": [round(value, 3) for value in journal_cube.location],
        "phase": phase,
        "messages": [message.text for message in wm.bmp.messages][-16:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    if "client" not in phase and any(user.name == "testclient" for user in wm.bmp.users):
        phase["client"] = round(elapsed, 2)
        publish()
    if "client" in phase and "offline_change" not in phase and "testclient left" in [message.text for message in wm.bmp.messages]:
        phase["offline_change"] = round(elapsed, 2)
        journal_cube.location = (9.0, 9.0, 9.0)
        publish()
    if "client" in phase and "journal_seen" not in phase and elapsed > 7.0:
        phase["journal_seen"] = round(elapsed, 2)
        phase["journal_cube"] = [round(float(value), 3) for value in journal_cube.location]
        publish()
    if "journal_seen" in phase and "moved" not in phase and elapsed > 8.0:
        cube.location = (3.0, 4.0, 5.0)
        phase["moved"] = round(elapsed, 2)
        publish()
    if "moved" in phase and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 40.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
