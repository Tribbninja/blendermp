"""Relay + mesh host: host through the Worker, then move the cube."""

import bpy
import json
import os
import sys
import time

TEMP = os.environ["BMP_TEST_DATA"]
VENDOR = os.environ["BMP_VENDOR"]
ADDON = os.environ["BMP_ADDON"]
RELAY = os.environ["BMP_RELAY_URL"]
RELAY_PORT = int(os.environ.get("BMP_RELAY_PORT", "7100"))
os.makedirs(TEMP, exist_ok=True)
os.environ["LOCALAPPDATA"] = TEMP
sys.path.insert(0, VENDOR)
sys.path.insert(0, ADDON)

import blender_multiplayer as bmp
from blender_multiplayer.net import stun


async def fake_discover(servers=None, timeout=3.0, local_port=0):
    return ("127.0.0.1", 9)


stun.discover = fake_discover

bpy.ops.preferences.addon_enable(module="blender_multiplayer")
context = bpy.context
wm = context.window_manager
wm.bmp.name = "meshhost"
wm.bmp.use_relay = True
wm.bmp.relay_host = "127.0.0.1"
wm.bmp.relay_port = RELAY_PORT
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

cube = bpy.data.objects.get("Cube")
if cube is None:
    bpy.ops.mesh.primitive_cube_add()
    cube = context.active_object
    cube.name = "Cube"
for obj in list(bpy.context.selected_objects):
    obj.select_set(False)
cube.select_set(True)
context.view_layer.objects.active = cube

bmp.start_host(context, mesh_url=RELAY)
code = wm.bmp.code
with open(os.path.join(TEMP, "host-code.txt"), "w", encoding="utf-8") as handle:
    handle.write(code)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
moved = {"at": 0.0}
written = {"done": False}


def tick():
    elapsed = time.monotonic() - start
    joined = [user.name for user in wm.bmp.users if user.name != wm.bmp.name]
    if joined and not moved["at"] and elapsed > 6.0:
        moved["at"] = elapsed
        cube.location.x = 4.0
    data = {
        "state": wm.bmp.state,
        "code": wm.bmp.code,
        "joined": joined,
        "mesh": bmp.runtime.get("session").mesh_status() if bmp.runtime.get("session") else {},
        "location": [round(float(v), 3) for v in cube.location],
        "messages": [message.text for message in wm.bmp.messages][-8:],
    }
    if elapsed > 18.0 and not written["done"]:
        written["done"] = True
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    if elapsed > 28.0:
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
