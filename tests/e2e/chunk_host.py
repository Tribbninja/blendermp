"""Chunked transfer host: create a large mesh after the client joins."""

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
import blender_multiplayer.session as session_module
from blender_multiplayer.core.roomcode import encode_room

bpy.ops.preferences.addon_enable(module="blender_multiplayer")
context = bpy.context
wm = context.window_manager
wm.bmp.name = "testhost"
wm.bmp.use_relay = False
wm.bmp.sync_mesh = True
wm.bmp.sync_transforms = True

# Force the chunk path without creating a 40 MB test mesh.
session_module.CHUNK_THRESHOLD = 512 * 1024

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52437
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]


def create_grid():
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=400, y_subdivisions=400, size=10.0)
    obj = context.active_object
    obj.name = "ChunkGrid"
    obj["bmp_uuid"] = str(__import__("uuid").uuid4())


def finish(reason):
    data = {
        "reason": reason,
        "objects": [item.name for item in context.scene.objects],
        "messages": [message.text for message in wm.bmp.messages][-16:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    joined = any(user.name == "testclient" for user in wm.bmp.users)
    if "created" not in globals() and joined and elapsed > 6.0:
        create_grid()
        globals()["created"] = elapsed
    if "created" in globals() and "sent" not in globals() and elapsed > globals()["created"] + 3.0:
        globals()["sent"] = elapsed
    if "sent" in globals() and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 45.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
