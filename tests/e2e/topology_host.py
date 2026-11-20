"""Live topology end-to-end host: structural edits while a client watches."""

import bmesh
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
cube.name = "TopoCube"
context.view_layer.objects.active = cube
cube.select_set(True)

PORT = 52375
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


def counts():
    obj = bpy.data.objects.get("TopoCube")
    if obj is None:
        return None
    return [len(obj.data.vertices), len(obj.data.edges), len(obj.data.polygons)]


def finish(reason):
    session = bmp.runtime.get("session")
    data = {
        "state": getattr(session, "state", None),
        "reason": reason,
        "counts": counts(),
        "phase": phase,
        "messages": [message.text for message in wm.bmp.messages][-12:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def edit(mode):
    context.view_layer.objects.active = cube
    cube.select_set(True)
    bpy.ops.object.mode_set(mode=mode)


def tick():
    elapsed = time.monotonic() - start
    if "client" not in phase and any(user.name == "testclient" for user in wm.bmp.users):
        phase["client"] = round(elapsed, 2)
        publish()
    if "client" in phase and "first_edit" not in phase and elapsed > 5.0:
        edit("EDIT")
        bm = bmesh.from_edit_mesh(cube.data)
        for vert in bm.verts:
            vert.select = True
        bmesh.update_edit_mesh(cube.data)
        bpy.ops.mesh.subdivide(number_cuts=1)
        phase["first_edit"] = round(elapsed, 2)
        print("PHASE first_edit", phase["first_edit"], counts(), flush=True)
        publish()
    if "first_edit" in phase and "first_exit" not in phase and elapsed > phase["first_edit"] + 1.2:
        edit("OBJECT")
        phase["first_exit"] = round(elapsed, 2)
        print("PHASE first_exit", phase["first_exit"], counts(), flush=True)
        phase["first_counts"] = counts()
        publish()
    if "first_exit" in phase and "second_edit" not in phase and elapsed > phase["first_exit"] + 2.0:
        edit("EDIT")
        bm = bmesh.from_edit_mesh(cube.data)
        for vert in bm.verts:
            vert.select = True
        bmesh.update_edit_mesh(cube.data)
        bpy.ops.mesh.subdivide(number_cuts=1)
        phase["second_edit"] = round(elapsed, 2)
        print("PHASE second_edit", phase["second_edit"], counts(), flush=True)
        publish()
    if "second_edit" in phase and "second_exit" not in phase and elapsed > phase["second_edit"] + 1.2:
        edit("OBJECT")
        phase["second_exit"] = round(elapsed, 2)
        print("PHASE second_exit", phase["second_exit"], counts(), flush=True)
        phase["second_counts"] = counts()
        publish()
    if "second_exit" in phase and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 35.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
