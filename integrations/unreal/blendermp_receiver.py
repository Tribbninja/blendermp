"""Blendermp cross-engine receiver for Unreal Engine.

Run this from Unreal's Output Log (Python) or as an Editor Utility:

    py "C:/path/to/blendermp_receiver.py"

It connects to the Blendermp bridge (Blender side: N-panel > Cross-engine
Bridge > Start), reads newline-delimited JSON on a background thread, and
applies messages on the editor tick. Mesh objects are represented by scaled
engine cubes; the raw triangle arrays are available in ``msg["mesh"]`` for a
project-specific procedural-mesh adapter.
"""

import json
import queue
import socket
import threading
import time

import unreal

HOST = "127.0.0.1"
PORT = 7103

_queue = queue.Queue()
_actors = {}
_running = True

_CUBE = "/Engine/BasicShapes/Cube.Cube"
_BASIC_MATERIAL = "/Engine/BasicShapes/BasicShapeMaterial.BasicShapeMaterial"


def _reader():
    while _running:
        try:
            with socket.create_connection((HOST, PORT), timeout=3.0) as sock:
                stream = sock.makefile("r", encoding="utf-8")
                for line in stream:
                    if not _running:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        _queue.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            time.sleep(1.0)


def _spawn(uid, name):
    actor = _actors.get(uid)
    if actor is not None:
        return actor
    location = unreal.Vector(0.0, 0.0, 0.0)
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, location
    )
    if actor is None:
        return None
    component = actor.static_mesh_component
    mesh = unreal.load_asset(_CUBE)
    if mesh is not None:
        component.set_static_mesh(mesh)
    actor.set_actor_label(name or uid)
    actor.set_mobility(unreal.ComponentMobility.MOVABLE)
    _actors[uid] = actor
    return actor


def _apply_object(msg):
    uid = msg.get("uid")
    if not uid:
        return
    actor = _spawn(uid, msg.get("name"))
    if actor is None:
        return
    actor.set_actor_hidden_in_game(not msg.get("visible", True))
    position = msg.get("position") or [0.0, 0.0, 0.0]
    actor.set_actor_location(
        unreal.Vector(float(position[0]), float(position[1]), float(position[2])),
        False,
        False,
    )
    rotation = msg.get("rotation") or [0.0, 0.0, 0.0]
    actor.set_actor_rotation(
        unreal.Rotator(
            float(rotation[0]), float(rotation[1]), float(rotation[2])
        ),
        False,
    )
    scale = msg.get("scale") or [1.0, 1.0, 1.0]
    bounds = msg.get("bounds") or [0, 0, 0, 1, 1, 1]
    # Blender dimensions are full extents; the engine cube is 100 uu wide.
    size = [max(0.0001, float(value)) for value in bounds[3:6]]
    actor.set_actor_scale3d(
        unreal.Vector(
            size[0] * float(scale[0]) * 0.5,
            size[1] * float(scale[1]) * 0.5,
            size[2] * float(scale[2]) * 0.5,
        )
    )


def _apply(message):
    kind = message.get("type")
    if kind == "scene":
        for item in message.get("objects") or []:
            _apply_object(item)
    elif kind == "object":
        _apply_object(message)
    elif kind == "remove":
        actor = _actors.pop(message.get("uid"), None)
        if actor is not None:
            unreal.EditorLevelLibrary.destroy_actor(actor)


def _tick(_delta_seconds):
    while True:
        try:
            message = _queue.get_nowait()
        except queue.Empty:
            break
        try:
            _apply(message)
        except Exception as error:  # noqa: BLE001
            unreal.log_warning(f"[Blendermp] apply failed: {error}")


def start():
    global _running
    _running = True
    threading.Thread(target=_reader, name="BlendermpReader", daemon=True).start()
    unreal.register_slate_post_tick_callback(_tick)
    unreal.log("[Blendermp] receiver started")


def stop():
    global _running
    _running = False
    try:
        unreal.unregister_slate_post_tick_callback(_tick)
    except Exception:  # noqa: BLE001
        pass
    unreal.log("[Blendermp] receiver stopped")


start()
