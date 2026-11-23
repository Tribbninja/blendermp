"""Session render host: start a tiny render and collect the frames."""

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
wm.bmp.name = "renderhost"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

scene = context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
scene.frame_start = 1
scene.frame_end = 3

PORT = int(os.environ.get("BMP_TEST_PORT", "52476"))
TILES = os.environ.get("BMP_TEST_TILES", "1x1")
TILES_X, TILES_Y = (max(1, int(part)) for part in (TILES.lower().split("x") + ["1", "1"])[:2])
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
output_dir = os.path.join(TEMP, "render-out")
started = {"at": 0.0}
finished = {"at": 0.0}


def png_size(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return [int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")]


def tick():
    elapsed = time.monotonic() - start
    manager = bmp.runtime.get("render_manager")
    status = manager.status() if manager is not None else {}
    joined = [user.name for user in wm.bmp.users if user.name != wm.bmp.name]
    if joined and not started["at"] and elapsed > 3.0:
        started["at"] = elapsed
        bmp._start_session_render(
            context,
            1,
            3,
            1,
            "MP4",
            output_dir,
            tiles_x=TILES_X,
            tiles_y=TILES_Y,
        )
    if status.get("finished") and not finished["at"]:
        finished["at"] = elapsed
    frames = sorted(os.listdir(output_dir)) if os.path.isdir(output_dir) else []
    tile_dir = os.path.join(output_dir, "_tiles")
    data = {
        "state": wm.bmp.state,
        "joined": joined,
        "started": bool(started["at"]),
        "render": status,
        "frames": frames,
        "tiles": sorted(os.listdir(tile_dir)) if os.path.isdir(tile_dir) else [],
        "frame_sizes": {
            name: png_size(os.path.join(output_dir, name))
            for name in frames
            if name.startswith("frame_") and name.endswith(".png")
        },
        "messages": [message.text for message in wm.bmp.messages][-10:],
    }
    if elapsed > 45.0:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
