"""Lock client: the host's cube must be non-selectable and a move must revert."""

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
wm.bmp.name = "lockclient"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

PORT = 52468
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
checks = {}
result_path = os.path.join(TEMP, "client-result.json")


def cube():
    return bpy.data.objects.get("Cube")


def finish(reason):
    checks["reason"] = reason
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(checks, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    obj = cube()
    texts = [message.text for message in wm.bmp.messages]
    if obj is not None and "loaded" not in checks and any(
        text == "Host scene loaded" for text in texts
    ):
        checks["loaded"] = round(elapsed, 2)
    if "loaded" in checks and "snapshot" not in checks and elapsed > checks["loaded"] + 1.0:
        uid = obj.get("bmp_uuid")
        checks["snapshot"] = round(elapsed, 2)
        checks["state"] = wm.bmp.state
        checks["bootstrapping"] = bool(bmp.runtime.get("bootstrapping"))
        checks["owner"] = bmp.runtime["lease_owners"].get(uid) if uid else None
        checks["owned"] = uid in bmp.runtime.get("owned_leases", set()) if uid else None
        checks["hide_select"] = bool(obj.hide_select)
        checks["selected"] = bool(obj.select_get())
        checks["hidden"] = uid in bmp.runtime.get("hidden_objects", set()) if uid else None
        checks["before"] = [round(float(v), 3) for v in obj.location]
        checks["remote_selected"] = {
            user.name: str(user.selected or "")
            for user in wm.bmp.users
            if user.name != wm.bmp.name
        }
        checks["outline_segments"] = bmp.runtime.get("remote_debug", {}).get("segments", 0)
        obj.location.x = 5.0
        checks["after_move"] = [round(float(v), 3) for v in obj.location]
    if "snapshot" in checks and "settled" not in checks and elapsed > checks["snapshot"] + 2.5:
        uid = obj.get("bmp_uuid") if obj is not None else None
        checks["settled"] = [round(float(v), 3) for v in obj.location]
        checks["hide_select_after"] = bool(obj.hide_select)
        checks["selected_after"] = bool(obj.select_get())
        checks["owner_after"] = bmp.runtime["lease_owners"].get(uid) if uid else None
    if "settled" in checks and "outline_after" not in checks and elapsed > 11.0:
        checks["remote_selected_after"] = {
            user.name: str(user.selected or "")
            for user in wm.bmp.users
            if user.name != wm.bmp.name
        }
        checks["outline_segments_after"] = bmp.runtime.get("remote_debug", {}).get("segments", 0)
        checks["outline_after"] = round(elapsed, 2)
        finish("done")
        return None
    if elapsed > 24.0:
        finish("timeout")
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
