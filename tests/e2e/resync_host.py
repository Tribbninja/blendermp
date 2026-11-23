"""Auto-resync host: keep one cube authoritative while the client diverges."""

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
wm.bmp.name = "testhost"
wm.bmp.use_relay = False
wm.bmp.sync_transforms = True
wm.bmp.sync_mesh = True
wm.bmp.sync_materials = True
wm.bmp.sync_modifiers = True
wm.bmp.auto_resync = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
cube = context.active_object
cube.name = "ResyncCube"
material = bpy.data.materials.new("ResyncMat")
material.use_nodes = True
bsdf = material.node_tree.nodes.get("Principled BSDF")
if bsdf is not None:
    bsdf.inputs["Base Color"].default_value = (0.8, 0.1, 0.1, 1.0)
cube.data.materials.append(material)

PORT = 52478
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]


def finish(reason):
    manager = bmp.runtime.get("render_manager")
    status = manager.status() if manager is not None else {}
    data = {
        "reason": reason,
        "state": wm.bmp.state,
        "objects": [item.name for item in context.scene.objects],
        "auto_resync": bool(wm.bmp.auto_resync),
        "resync_interval": bmp.runtime.get("auto_resync_interval"),
        "deep_interval": bmp.runtime.get("deep_resync_interval"),
        "messages": [message.text for message in wm.bmp.messages][-16:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    if os.path.exists(client_result):
        finish("client")
        return None
    if elapsed > 45.0:
        finish("timeout")
        return None
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
