"""Bundle host: create a late asset and send it to the joined client."""

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

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

PORT = 52436
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]


def create_asset():
    bpy.ops.mesh.primitive_uv_sphere_add(location=(3, 1, 0))
    obj = context.active_object
    obj.name = "BundleAsset"
    material = bpy.data.materials.new("BundleMat")
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.1, 0.8, 0.2, 1.0)
    image = bpy.data.images.new("BundleTex", 8, 8, alpha=True)
    image.pixels = [0.9, 0.2, 0.1, 1.0] * 64
    image.pack()
    texture = material.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    material.node_tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
    obj.data.materials.append(material)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    context.view_layer.objects.active = obj


def finish(reason):
    data = {
        "reason": reason,
        "objects": [item.name for item in context.scene.objects],
        "messages": [message.text for message in wm.bmp.messages][-16:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    joined = any(user.name == "testclient" for user in wm.bmp.users)
    if "created" not in globals() and joined and elapsed > 6.0:
        # Stop normal object sync so the bundle is the only way this asset can
        # arrive; otherwise the host would stream it before the offer.
        wm.bmp.sync_transforms = False
        wm.bmp.sync_mesh = False
        wm.bmp.sync_materials = False
        wm.bmp.sync_modifiers = False
        create_asset()
        globals()["created"] = elapsed
    if "created" in globals() and "sent" not in globals() and elapsed > globals()["created"] + 2.0:
        bpy.ops.bmp.bundle_send(target="testclient")
        globals()["sent"] = elapsed
    if "sent" in globals() and "pushed" not in globals() and elapsed > globals()["sent"] + 2.0:
        area = next((item for item in context.screen.areas if item.type == "VIEW_3D"), None)
        if area is not None and area.spaces.active is not None:
            region = area.spaces.active.region_3d
            region.view_location = (10.0, 20.0, 30.0)
            region.view_distance = 42.0
            region.view_perspective = "PERSP"
        bpy.ops.bmp.bookmark_save(name="E2EView")
        bpy.ops.bmp.bookmark_push(name="E2EView")
        globals()["pushed"] = elapsed
    if "sent" in globals() and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 45.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
