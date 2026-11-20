"""Animation/rig host: armature, weights, shape keys, action and pose."""

import bpy
import json
import math
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

bpy.ops.object.armature_add(enter_editmode=True, location=(0, 0, 0))
rig = context.active_object
rig.name = "Rig"
edit_bones = rig.data.edit_bones
edit_bones[0].name = "Bone1"
edit_bones[0].head = (0, 0, 0)
edit_bones[0].tail = (0, 0, 1)
bone2 = edit_bones.new("Bone2")
bone2.head = (0, 0, 1)
bone2.tail = (0, 0, 2)
bone2.parent = edit_bones["Bone1"]
bone2.use_connect = True
bpy.ops.object.mode_set(mode="OBJECT")

bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
mesh_obj = context.active_object
mesh_obj.name = "RigMesh"
mesh_obj.parent = rig
modifier = mesh_obj.modifiers.new("Armature", "ARMATURE")
modifier.object = rig
group1 = mesh_obj.vertex_groups.new(name="Bone1")
group2 = mesh_obj.vertex_groups.new(name="Bone2")
group1.add(list(range(4)), 1.0, "REPLACE")
group2.add(list(range(4, 8)), 1.0, "REPLACE")

basis = mesh_obj.shape_key_add(name="Basis", from_mix=False)
smile = mesh_obj.shape_key_add(name="Smile", from_mix=False)
for index, point in enumerate(smile.data):
    point.co.x += 0.25
smile.value = 0.25

rig.animation_data_create()
action = bpy.data.actions.new("RigAction")
rig.animation_data.action = action
slot = action.slots.new(id_type="OBJECT", name=rig.name)
layer = action.layers.new("Layer")
strip = layer.strips.new(type="KEYFRAME")
channelbag = strip.channelbag(slot, ensure=True)
curve = channelbag.fcurves.new(data_path='pose.bones["Bone2"].rotation_quaternion', index=0)
curve.keyframe_points.add(2)
curve.keyframe_points[0].co = (1.0, 0.0)
curve.keyframe_points[1].co = (24.0, 0.5)
try:
    rig.animation_data.action_slot = slot
except Exception:
    pass

bpy.ops.object.select_all(action="DESELECT")

PORT = 52415
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
    if "client" in phase and "changed" not in phase and elapsed > 6.0:
        rig.pose.bones["Bone2"].rotation_mode = "QUATERNION"
        rig.pose.bones["Bone2"].rotation_quaternion = (math.cos(0.4), 0.0, 0.0, math.sin(0.4))
        smile.value = 0.75
        phase["changed"] = round(elapsed, 2)
        publish()
    if "changed" in phase and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 30.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
