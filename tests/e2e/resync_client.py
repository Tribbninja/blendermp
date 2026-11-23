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

PORT = 52479
code = encode_room("127.0.0.1", PORT)
bmp.start_join(context, code)
start = time.monotonic()
result_path = os.path.join(TEMP, "client-result.json")
checks = {}


def finish(reason):
    cube = bpy.data.objects.get("ResyncCube")
    camera = bpy.data.objects.get("ResyncCamera")
    light = bpy.data.objects.get("ResyncLight")
    geo_value = None
    if cube is not None:
        geo = cube.modifiers.get("Geo")
        if geo is not None:
            try:
                geo_value = round(float(geo.properties.inputs.Socket_0.value), 3)
            except Exception:  # noqa: BLE001
                geo_value = None
    world = context.scene.world
    background = None
    if world is not None and world.use_nodes and world.node_tree is not None:
        background = world.node_tree.nodes.get("Background")
    color = (
        [round(float(value), 3) for value in background.inputs["Color"].default_value[:3]]
        if background is not None
        else None
    )
    data = {
        "reason": reason,
        "state": wm.bmp.state,
        "diverged": bool(checks.get("diverged")),
        "healed": bool(checks.get("healed")),
        "heal_seconds": checks.get("heal_seconds"),
        "location": [round(float(value), 3) for value in cube.location] if cube is not None else None,
        "materials": [material.name for material in cube.data.materials] if cube is not None else [],
        "resolution_x": int(context.scene.render.resolution_x),
        "world_color": color,
        "camera_lens": round(float(camera.data.lens), 3) if camera is not None else None,
        "light_energy": round(float(light.data.energy), 3) if light is not None else None,
        "geo_value": geo_value,
        "prop_value": cube.get("ResyncProp") if cube is not None else None,
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
        # Stop sending local edits, then delete the object and change scene
        # settings. The host's scene fingerprint no longer matches, so
        # auto-resync must recreate the object and refresh world/render/camera/light.
        wm.bmp.paused = True
        try:
            context.view_layer.objects.active = None
        except Exception:  # noqa: BLE001
            pass
        group = bpy.data.node_groups.get("ResyncGeo")
        if group is None:
            group = bpy.data.node_groups.new("ResyncGeo", "GeometryNodeTree")
            group.interface.new_socket("Value", in_out="INPUT", socket_type="NodeSocketFloat")
        geo = cube.modifiers.get("Geo") or cube.modifiers.new("Geo", "NODES")
        try:
            geo.node_group = group
            geo.properties.inputs.Socket_0.value = 0.1
        except Exception:  # noqa: BLE001
            pass
        cube["ResyncProp"] = 9.0
        bpy.data.objects.remove(cube, do_unlink=True)
        context.scene.render.resolution_x = 32
        world = context.scene.world
        if world is not None and world.use_nodes and world.node_tree is not None:
            background = world.node_tree.nodes.get("Background")
            if background is not None:
                background.inputs["Color"].default_value = (0.9, 0.1, 0.1, 1.0)
        camera = bpy.data.objects.get("ResyncCamera")
        if camera is not None:
            camera.data.lens = 80.0
        light = bpy.data.objects.get("ResyncLight")
        if light is not None:
            light.data.energy = 10.0
        checks["diverged"] = True
        checks["diverged_at"] = elapsed
    if checks.get("diverged") and "healed" not in checks:
        cube = bpy.data.objects.get("ResyncCube")
        if cube is not None:
            checks["healed"] = True
            checks["healed_at"] = elapsed
            checks["heal_seconds"] = round(elapsed - checks["diverged_at"], 2)
    if checks.get("healed") and elapsed > checks["healed_at"] + 2.0:
        finish("healed")
        return None
    if elapsed > 40.0:
        finish("timeout")
        return None
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
