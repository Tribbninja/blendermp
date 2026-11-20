"""Lease host: record who owns the cube while the client selects/deselects."""

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
wm.bmp.name = "testhost"
wm.bmp.use_relay = False
wm.bmp.sync_mesh = True
wm.bmp.sync_transforms = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
cube = context.active_object
cube.name = "LeaseCube"
bpy.ops.object.select_all(action="DESELECT")

PORT = 52445
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]
owners_seen = set()


def finish(reason):
    uid = cube.get("bmp_uuid")
    data = {
        "reason": reason,
        "owners_seen": sorted(value for value in owners_seen if value),
        "final_owner": (bmp.runtime["lease_owners"].get(uid) if uid else None),
        "messages": [message.text for message in wm.bmp.messages][-12:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    uid = cube.get("bmp_uuid")
    if uid:
        owners_seen.add(bmp.runtime["lease_owners"].get(uid))
    if elapsed > 8.0 and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 45.0:
        finish("timeout")
        return
    return 0.2


bpy.app.timers.register(tick, first_interval=0.2)
