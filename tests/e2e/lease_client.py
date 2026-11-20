"""Lease client: select, deselect, reselect and prove the lock is released."""

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

PORT = 52445
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def cube():
    return bpy.data.objects.get("LeaseCube")


def owner():
    obj = cube()
    uid = obj.get("bmp_uuid") if obj is not None else None
    if not uid:
        return None, False
    return bmp.runtime["lease_owners"].get(uid), uid in bmp.runtime["owned_leases"]


def finish(reason):
    current, owned = owner()
    checks["reason"] = reason
    checks["state"] = wm.bmp.state
    checks["final_owner"] = current
    checks["final_owned"] = owned
    checks["messages"] = [message.text for message in wm.bmp.messages][-16:]
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    texts = [message.text for message in wm.bmp.messages]
    obj = cube()
    if "loaded" not in checks and any(text == "Host scene loaded" for text in texts):
        checks["loaded"] = round(elapsed, 2)
    if "loaded" in checks and "selected" not in checks and elapsed > checks["loaded"] + 0.6:
        obj.select_set(True)
        context.view_layer.objects.active = obj
        checks["selected"] = round(elapsed, 2)
    if "selected" in checks and "after_select" not in checks and elapsed > checks["selected"] + 0.9:
        current, owned = owner()
        checks["after_select"] = round(elapsed, 2)
        checks["owner_after_select"] = current
        checks["owned_after_select"] = owned
    if "after_select" in checks and "deselected" not in checks and elapsed > checks["after_select"] + 0.3:
        obj.select_set(False)
        checks["deselected"] = round(elapsed, 2)
    if "deselected" in checks and "after_deselect" not in checks and elapsed > checks["deselected"] + 1.0:
        current, owned = owner()
        checks["after_deselect"] = round(elapsed, 2)
        checks["owner_after_deselect"] = current
        checks["owned_after_deselect"] = owned
    if "after_deselect" in checks and "reselected" not in checks and elapsed > checks["after_deselect"] + 0.3:
        obj.select_set(True)
        context.view_layer.objects.active = obj
        checks["reselected"] = round(elapsed, 2)
    if "reselected" in checks and "after_reselect" not in checks and elapsed > checks["reselected"] + 0.9:
        current, owned = owner()
        checks["after_reselect"] = round(elapsed, 2)
        checks["owner_after_reselect"] = current
        checks["owned_after_reselect"] = owned
        obj.select_set(False)
    if "after_reselect" in checks and elapsed > checks["after_reselect"] + 1.0:
        finish("done")
        return
    if elapsed > 40.0:
        finish("timeout")
        return
    return 0.2


bpy.app.timers.register(tick, first_interval=0.2)
