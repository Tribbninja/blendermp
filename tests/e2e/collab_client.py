"""Bundle client: accept the host's late asset and verify it imported."""

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

PORT = 52436
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")


def view_distance():
    area = next((item for item in context.screen.areas if item.type == "VIEW_3D"), None)
    if area is None or area.spaces.active is None:
        return None
    region = area.spaces.active.region_3d
    return round(float(region.view_distance), 2) if region is not None else None


def finish(reason):
    asset = bpy.data.objects.get("BundleAsset")
    data = {
        "reason": reason,
        "state": wm.bmp.state,
        "bundle_count": wm.bmp.bundle_count,
        "bundle_sender": globals().get("sender", wm.bmp.bundle_sender),
        "bundle_info": globals().get("info", wm.bmp.bundle_info),
        "asset": asset.name if asset is not None else None,
        "materials": [material.name for material in bpy.data.materials if material.name.startswith("Bundle")],
        "images": [image.name for image in bpy.data.images if image.name.startswith("Bundle")],
        "bookmark_distance": globals().get("bookmark_distance"),
        "messages": [message.text for message in wm.bmp.messages][-20:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    texts = [message.text for message in wm.bmp.messages]
    if "loaded" not in globals() and any(text == "Host scene loaded" for text in texts):
        globals()["loaded"] = elapsed
    if "loaded" in globals() and "accepted" not in globals() and wm.bmp.bundle_count > 0:
        globals()["sender"] = wm.bmp.bundle_sender
        globals()["info"] = wm.bmp.bundle_info
        bpy.ops.bmp.bundle_accept(bundle=wm.bmp.bundle_id)
        globals()["accepted"] = elapsed
    if "accepted" in globals() and "imported" not in globals() and bpy.data.objects.get("BundleAsset") is not None:
        globals()["imported"] = elapsed
    if "imported" in globals() and "bookmark_distance" not in globals() and any("moved everyone" in text for text in texts):
        globals()["bookmark_distance"] = view_distance()
        globals()["bookmarked"] = elapsed
    if "bookmarked" in globals() and elapsed > globals()["bookmarked"] + 1.0:
        finish("imported")
        return
    if elapsed > 40.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
