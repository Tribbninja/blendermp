"""Object details end-to-end host: parenting, modifiers, collections, flags."""

import bpy
import json
import os
import sys
import time
from mathutils import Matrix

TEMP = os.environ["BMP_TEST_DATA"]
VENDOR = os.environ["BMP_VENDOR"]
ADDON = os.environ["BMP_ADDON"]
SYNC = os.environ["BMP_SYNC"]
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
wm.bmp.sync_mesh = True
wm.bmp.sync_transforms = True
wm.bmp.sync_materials = True
wm.bmp.sync_modifiers = True

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

root = bpy.data.objects.new("RigRoot", None)
context.scene.collection.objects.link(root)
root.location = (2.0, 0.0, 0.0)
root.empty_display_type = "PLAIN_AXES"
root.empty_display_size = 0.4

bpy.ops.mesh.primitive_cube_add(location=(3.0, 0.0, 0.0))
child = context.active_object
child.name = "ChildCube"
child.parent = root
child.matrix_parent_inverse = root.matrix_world.inverted()
# Keep the child's world transform; this is what Blender's "keep transform"
# parenting does and exercises matrix_parent_inverse on the receiver.
child.matrix_world = Matrix.Translation((3.0, 0.0, 0.0))
child.color = (0.2, 0.8, 0.4, 1.0)
child.hide_render = True

subsurf = child.modifiers.new("Subdivision", "SUBSURF")
subsurf.levels = 2
subsurf.render_levels = 2
bevel = child.modifiers.new("Edge Bevel", "BEVEL")
bevel.width = 0.1
bevel.segments = 2
bpy.ops.mesh.primitive_cube_add(location=(4.0, 0.0, 0.0))
cutter = context.active_object
cutter.name = "Cutter"
boolean = child.modifiers.new("Cutter Boolean", "BOOLEAN")
boolean.operation = "DIFFERENCE"
boolean.object = cutter
constraint = child.constraints.new("COPY_ROTATION")
constraint.name = "Follow Root"
constraint.target = root
constraint.influence = 0.5

collection = bpy.data.collections.new("Props")
context.scene.collection.children.link(collection)
collection.hide_render = True
for existing in list(child.users_collection):
    existing.objects.unlink(child)
collection.objects.link(child)

PORT = 52365
code = encode_room("127.0.0.1", PORT)
bmp.start_host(context, code=code, port=PORT)
start = time.monotonic()
phase = {}
result_path = os.path.join(TEMP, "host-result.json")
client_result = os.environ["BMP_CLIENT_RESULT"]


def publish():
    temp = SYNC + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(phase, handle)
    os.replace(temp, SYNC)


def summary():
    obj = bpy.data.objects.get("ChildCube")
    if obj is None:
        return None
    modifiers = {mod.type: mod.name for mod in obj.modifiers}
    boolean = next((mod for mod in obj.modifiers if mod.type == "BOOLEAN"), None)
    root = bpy.data.objects.get("RigRoot")
    return {
        "root_location": [round(float(v), 3) for v in root.location] if root else None,
        "parent": obj.parent.name if obj.parent else None,
        "world": [round(value, 3) for value in obj.matrix_world.translation],
        "local": [round(float(value), 3) for value in obj.matrix_basis.translation],
        "parent_inverse": [round(float(value), 3) for value in obj.matrix_parent_inverse.translation],
        "modifiers": sorted(modifiers),
        "boolean": (
            {
                "operation": boolean.operation,
                "object": boolean.object.name if boolean.object is not None else None,
            }
            if boolean is not None
            else None
        ),
        "constraints": sorted(item.name for item in child.constraints),
        "subsurf_levels": int(subsurf.levels),
        "color": [round(float(value), 3) for value in obj.color],
        "hide_render": bool(obj.hide_render),
        "collections": sorted(item.name for item in obj.users_collection),
        "collection_hide_render": bool(collection.hide_render),
        "collection_hide_viewport": bool(collection.hide_viewport),
    }


def finish(reason):
    session = bmp.runtime.get("session")
    data = {
        "state": getattr(session, "state", None),
        "reason": reason,
        "summary": summary(),
        "leases": dict(bmp.runtime.get("lease_owners", {})),
        "owned": sorted(bmp.runtime.get("owned_leases", set())),
        "phase": phase,
        "messages": [message.text for message in wm.bmp.messages][-12:],
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    bpy.ops.wm.quit_blender()


def tick():
    elapsed = time.monotonic() - start
    if "client" not in phase and any(user.name == "testclient" for user in wm.bmp.users):
        phase["client"] = round(elapsed, 2)
        phase["summary_at_join"] = summary()
        publish()
    if "client" in phase and "changed" not in phase and elapsed > 6.0:
        root.location = (5.0, 0.0, 0.0)
        subsurf.levels = 1
        child.color = (0.9, 0.1, 0.2, 1.0)
        collection.hide_render = False
        collection.hide_viewport = True
        phase["changed"] = round(elapsed, 2)
        phase["summary_changed"] = summary()
        publish()
    if elapsed > 8.0 and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 40.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25)
