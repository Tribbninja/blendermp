"""Chunked transfer client: wait for a large mesh assembled from chunks."""

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

PORT = 52437
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")


def finish(reason):
    obj = bpy.data.objects.get("ChunkGrid")
    data = {
        "reason": reason,
        "state": wm.bmp.state,
        "vertices": len(obj.data.vertices) if obj is not None and obj.type == "MESH" else 0,
        "edges": len(obj.data.edges) if obj is not None and obj.type == "MESH" else 0,
        "objects": [item.name for item in context.scene.objects],
        "messages": [message.text for message in wm.bmp.messages][-20:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    obj = bpy.data.objects.get("ChunkGrid")
    if "arrived" not in globals() and obj is not None and obj.type == "MESH" and len(obj.data.vertices) > 100000:
        globals()["arrived"] = elapsed
    if "arrived" in globals() and "client_sent" not in globals():
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=300, y_subdivisions=300, size=8.0)
        client_obj = context.active_object
        client_obj.name = "ClientChunkGrid"
        client_obj["bmp_uuid"] = str(__import__("uuid").uuid4())
        globals()["client_sent"] = elapsed
    if "client_sent" in globals() and elapsed > globals()["client_sent"] + 3.0:
        finish("arrived")
        return
    if elapsed > 40.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
