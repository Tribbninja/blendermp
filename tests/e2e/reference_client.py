"""Reference image client: join and look for an image no material uses."""

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
wm.bmp.name = "refclient"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

PORT = 52474
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")


def tick():
    elapsed = time.monotonic() - start
    image = bpy.data.images.get("ReferenceBoard")
    data = {
        "state": wm.bmp.state,
        "found": image is not None,
        "size": list(image.size) if image is not None else None,
        "loaded": any(message.text == "Host scene loaded" for message in wm.bmp.messages),
        "messages": [message.text for message in wm.bmp.messages][-8:],
    }
    if elapsed > 18.0:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
