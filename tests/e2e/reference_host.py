"""Reference image host: create an image no material uses, then host."""

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
wm.bmp.name = "refhost"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

image = bpy.data.images.get("ReferenceBoard")
if image is None:
    image = bpy.data.images.new("ReferenceBoard", 64, 64)
    image.pixels = [0.9, 0.1, 0.1, 1.0] * (64 * 64)
image.pack()

PORT = 52474
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")


def tick():
    elapsed = time.monotonic() - start
    data = {
        "state": wm.bmp.state,
        "image": image.name,
        "size": list(image.size),
        "messages": [message.text for message in wm.bmp.messages][-8:],
    }
    if elapsed > 20.0:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
