"""Material/texture parity end-to-end host.

Run two Blender instances with ``run_material.ps1``. The host creates a
textured, mapped, procedural-ish graph, then edits it mid-session so the
joining instance has to update both the graph and the texture binding.
"""

import bpy
import json
import os
import sys
import time

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
bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
cube = context.active_object
cube.name = "MatCube"

image = bpy.data.images.new("BMPBrick", 32, 32, alpha=True)
image.pixels.foreach_set([0.65, 0.25, 0.1, 1.0] * (32 * 32))
image.pack()

material = bpy.data.materials.new("MatGraph")
material.use_nodes = True
tree = material.node_tree
bsdf = next(node for node in tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
output = next(node for node in tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial")
uv = tree.nodes.new("ShaderNodeUVMap")
uv.name = "UV Source"
uv.uv_map = "UVMap"
uv.location = (-700.0, 0.0)
mapping = tree.nodes.new("ShaderNodeMapping")
mapping.name = "Tile Mapping"
mapping.location = (-500.0, 0.0)
mapping.inputs["Scale"].default_value = (2.0, 3.0, 1.0)
texture = tree.nodes.new("ShaderNodeTexImage")
texture.name = "Brick Texture"
texture.location = (-280.0, 0.0)
texture.image = image
texture.interpolation = "Linear"
bump = tree.nodes.new("ShaderNodeBump")
bump.name = "Surface Bump"
bump.location = (-60.0, -260.0)
bump.inputs["Strength"].default_value = 0.35
tree.links.new(uv.outputs["UV"], mapping.inputs["Vector"])
tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
tree.links.new(texture.outputs["Color"], bump.inputs["Height"])
tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
bsdf.inputs["Metallic"].default_value = 0.15
bsdf.inputs["Roughness"].default_value = 0.42
cube.data.materials.append(material)

PORT = 52355
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
    obj = bpy.data.objects.get("MatCube")
    if obj is None or not obj.material_slots or obj.material_slots[0].material is None:
        return None
    material = obj.material_slots[0].material
    tree = material.node_tree
    texture = tree.nodes.get("Brick Texture") if tree else None
    mapping = tree.nodes.get("Tile Mapping") if tree else None
    bsdf = next((node for node in tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None) if tree else None
    return {
        "nodes": len(tree.nodes) if tree else 0,
        "links": len(tree.links) if tree else 0,
        "image": texture.image.name if texture is not None and texture.image else None,
        "image_size": list(texture.image.size) if texture is not None and texture.image else None,
        "image_packed": bool(texture.image.packed_file) if texture is not None and texture.image else False,
        "colorspace": texture.image.colorspace_settings.name if texture is not None and texture.image else None,
        "interpolation": texture.interpolation if texture is not None else None,
        "mapping": list(mapping.inputs["Scale"].default_value) if mapping is not None else None,
        "base_linked": bool(bsdf.inputs["Base Color"].is_linked) if bsdf is not None else False,
        "normal_linked": bool(bsdf.inputs["Normal"].is_linked) if bsdf is not None else False,
        "roughness": round(float(bsdf.inputs["Roughness"].default_value), 3) if bsdf is not None else None,
        "emission": round(float(bsdf.inputs["Emission Strength"].default_value), 3) if bsdf is not None else None,
    }


def finish(reason):
    session = bmp.runtime.get("session")
    data = {
        "state": getattr(session, "state", None),
        "reason": reason,
        "objects": sorted(obj.name for obj in context.scene.objects),
        "summary": summary(),
        "phase": phase,
        "shared_images": len(bmp.runtime.get("shared_images", set())),
        "messages": [message.text for message in wm.bmp.messages][-16:],
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
        mapping.inputs["Scale"].default_value = (4.0, 5.0, 1.0)
        texture.interpolation = "Closest"
        bsdf.inputs["Emission Strength"].default_value = 2.5
        bsdf.inputs["Roughness"].default_value = 0.21
        phase["changed"] = round(elapsed, 2)
        publish()
    if elapsed > 8.0 and os.path.exists(client_result):
        finish("client")
        return
    if elapsed > 30.0:
        finish("timeout")
        return
    return 0.25


bpy.app.timers.register(tick, first_interval=0.25, persistent=True)
