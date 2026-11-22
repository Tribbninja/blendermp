"""Relay + mesh client: join through the Worker and watch the host's edit."""

import bpy
import json
import os
import sys
import time

TEMP = os.environ["BMP_TEST_DATA"]
VENDOR = os.environ["BMP_VENDOR"]
ADDON = os.environ["BMP_ADDON"]
RELAY = os.environ["BMP_RELAY_URL"]
os.makedirs(TEMP, exist_ok=True)
os.environ["LOCALAPPDATA"] = TEMP
sys.path.insert(0, VENDOR)
sys.path.insert(0, ADDON)

import blender_multiplayer as bmp
from blender_multiplayer.net import stun


async def fake_discover(servers=None, timeout=3.0, local_port=0):
    return ("127.0.0.1", 9)


stun.discover = fake_discover

bpy.ops.preferences.addon_enable(module="blender_multiplayer")
context = bpy.context
wm = context.window_manager
wm.bmp.name = "meshclient"
wm.bmp.use_relay = True
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True

deadline = time.monotonic() + 15.0
code_path = os.environ.get("BMP_HOST_CODE") or os.path.join(TEMP, "host-code.txt")
while time.monotonic() < deadline and not os.path.exists(code_path):
    time.sleep(0.2)
code = open(code_path, encoding="utf-8").read().strip()
bmp.start_join(context, code, mesh_url=RELAY)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")
checks = {"code": code, "samples": []}


def tick():
    elapsed = time.monotonic() - start
    cube = bpy.data.objects.get("Cube")
    session = bmp.runtime.get("session")
    checks["state"] = wm.bmp.state
    checks["mesh"] = session.mesh_status() if session else {}
    checks["samples"].append(
        {
            "t": round(elapsed, 2),
            "x": round(float(cube.location.x), 3) if cube is not None else None,
            "loaded": any(message.text == "Host scene loaded" for message in wm.bmp.messages),
            "state": wm.bmp.state,
        }
    )
    if elapsed > 20.0:
        checks["messages"] = [message.text for message in wm.bmp.messages][-12:]
        checks["last_error"] = wm.bmp.last_error
        checks["error_code"] = wm.bmp.last_error_code
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(checks, handle)
        bpy.ops.wm.quit_blender()
        return None
    return 0.5


bpy.app.timers.register(tick, first_interval=0.5)
