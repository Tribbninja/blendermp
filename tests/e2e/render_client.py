"""Session render client: accept the render and render assigned frames."""

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
wm.bmp.name = "renderclient"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

scene = context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100

PORT = int(os.environ.get("BMP_TEST_PORT", "52476"))
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")
accepted = {"job": ""}
rendered = {"count": 0}


def tick():
    elapsed = time.monotonic() - start
    session = bmp.runtime.get("session")
    request = bmp.runtime.get("render_request")
    if session is not None and isinstance(request, dict) and request.get("job"):
        job_id = str(request.get("job"))
        if accepted["job"] != job_id:
            accepted["job"] = job_id
            session.send_render_control("accept", {"job": job_id})
    data = {
        "state": wm.bmp.state,
        "accepted": accepted["job"],
        "active": bool(bmp.runtime.get("render_active")),
        "queue": len(bmp.runtime.get("render_queue") or []),
        "done": int(bmp.runtime.get("render_done", 0)),
        "messages": [message.text for message in wm.bmp.messages][-10:],
    }
    if elapsed > 42.0:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
