"""Animation/rig client."""

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
wm.bmp.sync_animation_values = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52415
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def summary():
    rig = bpy.data.objects.get("Rig")
    mesh_obj = bpy.data.objects.get("RigMesh")
    if rig is None or rig.type != "ARMATURE" or mesh_obj is None:
        return None
    modifier = next((mod for mod in mesh_obj.modifiers if mod.type == "ARMATURE"), None)
    keys = mesh_obj.data.shape_keys
    smile = keys.key_blocks.get("Smile") if keys else None
    action = rig.animation_data.action if rig.animation_data else None
    curves = bmp._action_fcurves(action) if action else []
    bone = rig.pose.bones.get("Bone2")
    driver_empty = bpy.data.objects.get("DriverEmpty")
    drivers = []
    if driver_empty is not None and driver_empty.animation_data is not None:
        drivers = [
            (curve.data_path, curve.array_index, curve.driver.expression)
            for curve in driver_empty.animation_data.drivers
        ]
    nla = []
    if rig.animation_data is not None:
        nla = [
            (
                track.name,
                [
                    (strip.name, strip.action.name if strip.action else None)
                    for strip in track.strips
                ],
            )
            for track in rig.animation_data.nla_tracks
        ]
    return {
        "bones": sorted(bone.name for bone in rig.data.bones),
        "modifier_target": modifier.object.name if modifier and modifier.object else None,
        "groups": sorted(group.name for group in mesh_obj.vertex_groups),
        "shape_keys": sorted(key.name for key in keys.key_blocks) if keys else [],
        "smile": round(float(smile.value), 3) if smile else None,
        "action": action.name if action else None,
        "curves": len(curves),
        "points": len(curves[0].keyframe_points) if curves else 0,
        "quat": [round(float(value), 3) for value in bone.rotation_quaternion] if bone else None,
        "uv_layers": [layer.name for layer in mesh_obj.data.uv_layers],
        "uv_active": mesh_obj.data.uv_layers.active_index,
        "drivers": drivers,
        "nla": nla,
        "mode": context.mode,
    }


def finish(reason):
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["summary"] = summary()
    checks["messages"] = [message.text for message in wm.bmp.messages][-16:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    data = summary()
    if data is not None and "initial" not in checks:
        checks["initial"] = data
        checks["initial_at"] = round(elapsed, 2)
    if data is not None and "changed" not in checks:
        if (
            data["bones"] == ["Bone1", "Bone2"]
            and data["modifier_target"] == "Rig"
            and "Smile" in data["shape_keys"]
            and abs(data["smile"] - 0.75) <= 0.01
            and data["quat"][0] < 0.95
        ):
            checks["changed"] = data
            checks["changed_at"] = round(elapsed, 2)
    if "changed" in checks:
        finish("changed")
        return
    if elapsed > 36.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
