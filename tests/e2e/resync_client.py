"""Auto-resync client: delete a synced object locally and wait for it to return."""

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
wm.bmp.auto_resync = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52478
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")
checks = {}


def finish(reason):
    cube = bpy.data.objects.get("ResyncCube")
    data = {
        "reason": reason,
        "state": wm.bmp.state,
        "diverged": bool(checks.get("diverged")),
        "healed": bool(checks.get("healed")),
        "heal_seconds": checks.get("heal_seconds"),
        "location": [round(float(value), 3) for value in cube.location] if cube is not None else None,
        "materials": [material.name for material in cube.data.materials] if cube is not None else [],
        "messages": [message.text for message in wm.bmp.messages][-20:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    cube = bpy.data.objects.get("ResyncCube")
    if cube is not None and "seen" not in checks:
        checks["seen"] = elapsed
    if cube is not None and "diverged" not in checks and elapsed > checks.get("seen", 0.0) + 2.0:
        # Stop sending local edits, then delete the object. The host's scene
        # fingerprint no longer matches, so auto-resync must recreate it.
        wm.bmp.paused = True
        try:
            context.view_layer.objects.active = None
        except Exception:  # noqa: BLE001
            pass
        bpy.data.objects.remove(cube, do_unlink=True)
        checks["diverged"] = True
        checks["diverged_at"] = elapsed
    if checks.get("diverged") and "healed" not in checks:
        cube = bpy.data.objects.get("ResyncCube")
        if cube is not None:
            checks["healed"] = True
            checks["healed_at"] = elapsed
            checks["heal_seconds"] = round(elapsed - checks["diverged_at"], 2)
    if checks.get("healed") and elapsed > checks["healed_at"] + 1.0:
        finish("healed")
        return None
    if elapsed > 40.0:
        finish("timeout")
        return None
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
