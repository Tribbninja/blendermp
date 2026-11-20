"""Sculpt host: stream position deltas from Sculpt Mode."""

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

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, location=(0, 0, 0))
sphere = context.active_object
sphere.name = "SculptSphere"
bpy.ops.object.select_all(action="DESELECT")

PORT = 52425
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
        "phase": phase,
        "messages": [message.text for message in wm.bmp.messages][-12:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    if "client" not in phase and any(user.name == "testclient" for user in wm.bmp.users):
        phase["client"] = round(elapsed, 2)
        publish()
    if "client" in phase and "sculpting" not in phase and elapsed > 5.0:
        context.view_layer.objects.active = sphere
        sphere.select_set(True)
        bpy.ops.object.mode_set(mode="SCULPT")
        phase["sculpting"] = round(elapsed, 2)
        publish()
    if "sculpting" in phase and "moved" not in phase and elapsed > 7.0:
        vertex = sphere.data.vertices[0]
        vertex.co.x += 0.5
        vertex.co.y += 0.25
        vertex.co.z += 0.1
        sphere.data.update()
        phase["moved"] = round(elapsed, 2)
        publish()
    if "moved" in phase and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 30.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
