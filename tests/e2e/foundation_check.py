"""Headless check for the 0.9.0 collaboration foundation.

Covers the AI tool handlers, mesh export, the cross-engine bridge, the live
broadcast server and the recording/replay tap. Writes a JSON result and exits
non-zero on failure.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.request

import bpy

sys.path.insert(0, os.environ["BMP_ADDON"])
import blender_multiplayer as bm  # noqa: E402
from blender_multiplayer.core import recording  # noqa: E402

RESULT = {"ok": True, "errors": [], "details": {}}
DATA = os.environ.get("BMP_TEST_DATA", os.environ.get("TEMP", "."))


def check(name, condition, detail=None):
    if condition:
        return
    RESULT["ok"] = False
    RESULT["errors"].append(f"{name}: {detail}")


bm.register()
try:
    bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    cube.name = "AI_Cube"
    bpy.ops.mesh.primitive_cube_add(location=(2.0, 0.0, 0.0))
    cutter = bpy.context.active_object
    cutter.name = "AI_Cutter"

    handlers = bm._ai_handlers()
    context = bpy.context

    result = handlers["move_object"]({"name": "AI_Cube", "location": [1.0, 2.0, 3.0]}, context)
    check("move_object", result.get("ok") and abs(cube.location.x - 1.0) < 1e-6, result)
    result = handlers["rotate_object"]({"name": "AI_Cube", "rotation": [0.0, 0.0, 45.0]}, context)
    check("rotate_object", result.get("ok"), result)
    result = handlers["scale_object"]({"name": "AI_Cube", "scale": [2.0, 2.0, 2.0]}, context)
    check("scale_object", result.get("ok") and abs(cube.scale.x - 2.0) < 1e-6, result)

    result = handlers["create_material"](
        {"name": "AI_Mat", "color": [1.0, 0.0, 0.0], "metallic": 0.2, "roughness": 0.3},
        context,
    )
    material = bpy.data.materials.get("AI_Mat")
    check("create_material", result.get("ok") and material is not None, result)
    result = handlers["assign_material"](
        {"object": "AI_Cube", "material": "AI_Mat"}, context
    )
    check(
        "assign_material",
        result.get("ok") and cube.material_slots[0].material is material,
        result,
    )
    result = handlers["set_material_property"](
        {"material": "AI_Mat", "metallic": 0.9}, context
    )
    check("set_material_property", result.get("ok"), result)
    result = handlers["set_material_color"](
        {"material": "AI_Mat", "color": [0.0, 1.0, 0.0]}, context
    )
    check("set_material_color", result.get("ok"), result)

    result = handlers["add_modifier"](
        {"name": "AI_Cube", "modifier": "BEVEL", "width": 0.05}, context
    )
    check("add_modifier", result.get("ok") and len(cube.modifiers) >= 1, result)
    result = handlers["apply_boolean"](
        {
            "name": "AI_Cube",
            "operand": "AI_Cutter",
            "operation": "DIFFERENCE",
            "apply": False,
        },
        context,
    )
    check(
        "apply_boolean",
        result.get("ok")
        and any(modifier.type == "BOOLEAN" for modifier in cube.modifiers),
        result,
    )
    boolean_snapshot = next(
        (item for item in bm.modifier_snapshot(cube) if item.get("type") == "BOOLEAN"),
        None,
    )
    check(
        "boolean_snapshot",
        bool(
            boolean_snapshot
            and boolean_snapshot["props"].get("operation") == "DIFFERENCE"
            and boolean_snapshot["props"].get("object_uid")
        ),
        boolean_snapshot,
    )

    result = handlers["select_object"]({"name": "AI_Cube"}, context)
    check("select_object", result.get("ok") and cube.select_get(), result)
    result = handlers["rename_object"](
        {"name": "AI_Cube", "new_name": "AI_Renamed"}, context
    )
    renamed = bpy.data.objects.get("AI_Renamed")
    check("rename_object", result.get("ok") and renamed is not None, result)
    result = handlers["get_object_modifiers"]({"name": "AI_Renamed"}, context)
    check("get_object_modifiers", isinstance(result.get("modifiers"), list), result)
    result = handlers["set_scene_setting"]({"frame": 12}, context)
    check("set_scene_setting", result.get("ok") and context.scene.frame_current == 12, result)

    exported = bm._exported_object(renamed, include_mesh=True)
    check(
        "export_mesh",
        exported.mesh is not None and len(exported.mesh.get("indices", [])) >= 3,
        {"mesh": bool(exported.mesh)},
    )
    check("export_transform", len(exported.matrix) == 16, exported.matrix)
    result = handlers["set_object_visibility"](
        {"name": "AI_Renamed", "visible": False}, context
    )
    check(
        "set_object_visibility",
        result.get("ok") and bpy.data.objects["AI_Renamed"].hide_viewport,
        result,
    )

    settings = bpy.context.window_manager.bmp
    settings.bridge_engine = "unity"
    settings.bridge_port = 17203
    settings.bridge_meshes = True
    bridge = bm._start_bridge(settings, context)
    check("bridge_start", bridge is not None, settings.bridge_url)
    messages = []

    def read_bridge():
        with socket.create_connection(("127.0.0.1", settings.bridge_port), timeout=4) as sock:
            stream = sock.makefile("r", encoding="utf-8")
            for _ in range(3):
                line = stream.readline()
                if not line:
                    break
                messages.append(json.loads(line))

    reader = threading.Thread(target=read_bridge)
    reader.start()
    reader.join(5)
    kinds = [message.get("type") for message in messages]
    check("bridge_hello", "hello" in kinds, kinds)
    check("bridge_scene", "scene" in kinds, kinds)
    scene = next((message for message in messages if message.get("type") == "scene"), None)
    if scene:
        check("bridge_meshes", any(obj.get("mesh") for obj in scene.get("objects", [])), None)
    bm._stop_bridge(settings)

    settings.broadcast_port = 17202
    broadcast = bm._start_broadcast(settings)
    check("broadcast_start", broadcast is not None, settings.broadcast_url)
    if broadcast is not None:
        bm._push_message(context, "system", "foundation check")
        base = settings.broadcast_url.split("?", 1)[0]
        with urllib.request.urlopen(
            f"{base}state?token={broadcast.token}", timeout=4
        ) as response:
            state = json.loads(response.read().decode("utf-8"))
        check("broadcast_event", state.get("seq", 0) >= 1, state.get("seq"))
        RESULT["details"]["broadcast_url"] = settings.broadcast_url
    bm._stop_broadcast(settings)

    path = os.path.join(DATA, "foundation.jsonl")
    recorder = recording.Recorder(path)
    recorder.start()
    bm.runtime["recording"] = recorder
    bm._record_local("chat", {"name": "tester", "text": "hello"})
    recorder.stop()
    bm.runtime["recording"] = None
    replay = recording.Replay(path)
    check(
        "recording_tap",
        len(replay.events) == 1 and replay.events[0].body.get("text") == "hello",
        replay.summary(),
    )
    RESULT["details"]["recording"] = replay.summary()

    # Idle leases must release themselves even while the object stays selected.
    class FakeSession:
        is_host = True
        state = "hosting"

        def __init__(self):
            self.actions = []

        def local_lease(self, key, action):
            self.actions.append((key, action))
            return True

        def send_lease(self, key, action):
            self.actions.append((key, action))

    target = bpy.data.objects.get("AI_Renamed") or bpy.data.objects[0]
    target.hide_viewport = False
    try:
        target.hide_set(False)
    except Exception:  # noqa: BLE001
        pass
    uid = target.get("bmp_uuid") or "66666666-6666-6666-6666-666666666666"
    target["bmp_uuid"] = uid
    for item in bpy.context.selected_objects:
        item.select_set(False)
    target.select_set(True)
    bpy.context.view_layer.update()
    fake = FakeSession()
    bm.runtime["session"] = fake
    bm.runtime["lease_owners"].clear()
    bm.runtime["owned_leases"].clear()
    bm.runtime["lease_activity"].clear()
    bm.runtime["hidden_objects"].clear()
    bm.runtime["selection"].clear()
    bm.runtime["last_selection"] = 0.0
    bm.runtime["lease_renew"] = 0.0
    old_idle, old_renew, old_poll = bm.LEASE_IDLE, bm.LEASE_RENEW, bm.SELECTION_POLL
    bm.LEASE_IDLE, bm.LEASE_RENEW, bm.SELECTION_POLL = 0.8, 0.3, 0.0
    try:
        bm._sync_selection(bpy.context)
        released = False
        for _ in range(20):
            time.sleep(0.15)
            bm._sync_selection(bpy.context)
            if any(action == "release" for _key, action in fake.actions):
                released = True
                break
        check("lease_idle_release", released, fake.actions)
    finally:
        bm.LEASE_IDLE, bm.LEASE_RENEW, bm.SELECTION_POLL = old_idle, old_renew, old_poll
        bm.runtime["session"] = None
finally:
    bm.unregister()

with open(os.path.join(DATA, "foundation-result.json"), "w", encoding="utf-8") as handle:
    json.dump(RESULT, handle)
print(json.dumps(RESULT))
if not RESULT["ok"]:
    raise SystemExit(1)
