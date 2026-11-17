"""Blendermp - collaborative Blender sessions.

The addon talks to the desktop app through two small JSON files in the user's
local data folder. That keeps the integration dependency-free and lets the app
start, stop and drive sessions without an RPC framework.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path as _Path

# Blender disables Python's user site, so TLS certificate generation needs a
# per-user vendor directory. The desktop app's bootstrap installs cryptography
# here; without it the addon still loads and reports a clear error on host.
_VENDOR = _Path(os.environ.get("LOCALAPPDATA", _Path.home() / ".local" / "share")) / "Blendermp" / "python"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import json
import queue
import socket
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper

from . import control, ui
from .core import image_payload
from .core import material as material_payload
from .core import object_payload
from .session import MAX_MESH, SessionService
from .sync import objects as obj_sync
from .sync.mesh import MeshMirror

bl_info = {
    "name": "Blendermp",
    "author": "Blendermp",
    "version": (0, 1, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Blendermp",
    "description": "Low-latency collaborative Blender sessions",
    "category": "Network",
}

ADDON_VERSION = "0.1.0"
TICK = 1.0 / 30.0
CONTROL_POLL = 0.5
STATUS_WRITE = 0.5
MESH_POLL = 1.0 / 12.0

runtime: dict = {
    "session": None,
    "inbox": queue.Queue(),
    "mirrors": {},
    "object_shadow": {},
    "tracked": set(),
    "last_control_nonce": None,
    "last_status": 0.0,
    "last_mesh": 0.0,
    "mesh_cursor": 0,
    "pings": [],
    "known_objects": set(),
    "applying_frame": False,
    "material_shadow": {},
    "modifier_shadow": {},
    "last_modifier": 0.0,
    "lease_request": None,
    "last_material": 0.0,
    "last_presence": 0.0,
    "control_checked": 0.0,
    "started": 0.0,
}


# --------------------------------------------------------------------------- props
class BMPUser(PropertyGroup):
    name: StringProperty()
    color: FloatVectorProperty(size=4, subtype="COLOR", min=0.0, max=1.0, default=(0.4, 0.8, 1.0, 1.0))
    ping_ms: IntProperty(default=0)
    scene: StringProperty()
    frame: IntProperty()
    mode: StringProperty()
    host: BoolProperty()


class BMPMessage(PropertyGroup):
    author: StringProperty()
    text: StringProperty()
    stamp: StringProperty()


class BMPState(PropertyGroup):
    state: StringProperty(default="offline")  # offline|hosting|joining|connected
    code: StringProperty()
    join_code: StringProperty()
    is_host: BoolProperty()
    name: StringProperty(default="")
    color: FloatVectorProperty(size=4, subtype="COLOR", min=0.0, max=1.0, default=(0.35, 0.8, 0.95, 1.0))
    port: IntProperty(name="Port", default=51999, min=1024, max=65535)
    pin: StringProperty(name="Room PIN", subtype="PASSWORD", maxlen=64)
    rtt_ms: IntProperty(default=0)
    bytes_in: IntProperty(default=0)
    bytes_out: IntProperty(default=0)
    last_error: StringProperty()
    fingerprint: StringProperty()
    verify_code: StringProperty()
    chat_text: StringProperty()
    sync_mesh: BoolProperty(name="Sync active mesh", default=True)
    sync_materials: BoolProperty(name="Sync materials", default=True)
    sync_modifiers: BoolProperty(name="Sync modifiers", default=True)
    use_relay: BoolProperty(name="Use relay", default=False)
    relay_host: StringProperty(name="Relay host", default="")
    relay_port: IntProperty(name="Relay port", default=7100, min=1024, max=65535)
    paused: BoolProperty(name="Pause sync", default=False)
    follow_host: BoolProperty(name="Follow host frame", default=False)
    host_name: StringProperty()
    sync_transforms: BoolProperty(name="Sync object transforms", default=True)
    users: CollectionProperty(type=BMPUser)
    messages: CollectionProperty(type=BMPMessage)
    show_users: BoolProperty(default=True)
    show_chat: BoolProperty(default=True)


def state(context) -> BMPState:
    return context.window_manager.bmp


def _push_message(context, author: str, text: str) -> None:
    messages = context.window_manager.bmp.messages
    item = messages.add()
    item.author = author
    item.text = text
    item.stamp = time.strftime("%H:%M")
    while len(messages) > 200:
        messages.remove(0)


def _upsert_user(context, name: str, color, host: bool = False) -> None:
    users = context.window_manager.bmp.users
    for user in users:
        if user.name == name:
            user.color = color
            user.host = host
            return
    user = users.add()
    user.name = name
    user.color = color
    user.host = host


def _remove_user(context, name: str) -> None:
    users = context.window_manager.bmp.users
    for index, user in enumerate(users):
        if user.name == name:
            users.remove(index)
            return


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


# --------------------------------------------------------------------------- session control
def start_host(context, code: str | None = None, port: int | None = None) -> None:
    stop_session(context)
    settings = context.window_manager.bmp
    from .core.roomcode import FLAG_RELAY, encode_room

    relay: tuple[str, int] | None = None
    if code is None and settings.use_relay:
        host = settings.relay_host.strip()
        if not host:
            raise ValueError("Relay host is not set")
        try:
            import ipaddress

            ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise ValueError("Relay host must be an IPv4 address") from exc
        relay = (host, settings.relay_port)
        code = encode_room(host, settings.relay_port, FLAG_RELAY)
        port = 0
    else:
        port = port or settings.port
        if not code:
            code = encode_room(_local_ip(), port)
    settings.code = code
    settings.is_host = True
    settings.state = "hosting"
    settings.last_error = ""
    name = settings.name or f"user-{uuid.uuid4().hex[:4]}"
    settings.name = name
    session = SessionService(
        inbox=runtime["inbox"],
        host=True,
        code=code,
        name=name,
        color=tuple(settings.color),
        port=port,
        pin=settings.pin,
        relay=relay,
    )
    session.start()
    runtime["session"] = session
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    _prime_objects(context)
    _upsert_user(context, name, tuple(settings.color), host=True)
    _push_message(context, "system", f"Hosting room {code}")
    bpy.app.timers.register(bmp_tick, first_interval=0.05, persistent=True)


def start_join(context, code: str) -> None:
    stop_session(context)
    settings = context.window_manager.bmp
    name = settings.name or f"user-{uuid.uuid4().hex[:4]}"
    settings.name = name
    settings.code = code.strip().upper()
    settings.is_host = False
    settings.state = "joining"
    settings.last_error = ""
    session = SessionService(
        inbox=runtime["inbox"],
        host=False,
        code=settings.code,
        name=name,
        color=tuple(settings.color),
        port=settings.port,
        fingerprint=settings.fingerprint or None,
        pin=settings.pin,
    )
    session.start()
    runtime["session"] = session
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    _prime_objects(context)
    _upsert_user(context, name, tuple(settings.color), host=False)
    _push_message(context, "system", f"Joining room {settings.code}")
    bpy.app.timers.register(bmp_tick, first_interval=0.05, persistent=True)


def stop_session(context) -> None:
    session = runtime.get("session")
    if session:
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    runtime["session"] = None
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    settings = context.window_manager.bmp
    settings.state = "offline"
    settings.rtt_ms = 0
    settings.users.clear()
    if runtime["inbox"] is not None:
        while not runtime["inbox"].empty():
            try:
                runtime["inbox"].get_nowait()
            except queue.Empty:
                break


# --------------------------------------------------------------------------- inbox
def _handle_event(context, kind: str, body: dict) -> None:
    settings = context.window_manager.bmp
    if kind == "state":
        value = body.get("state")
        if value in {"offline", "hosting", "joining", "connected"}:
            settings.state = value
    elif kind == "host_ready":
        settings.code = body.get("code", settings.code)
        settings.fingerprint = body.get("fingerprint", "")
        settings.verify_code = body.get("short", "")
        _push_message(context, "system", f"Room {settings.code} ready")
    elif kind == "host_key":
        settings.fingerprint = body.get("fingerprint", "")
        settings.verify_code = body.get("short", "")
        _push_message(context, "system", f"Verify host code: {settings.verify_code}")
    elif kind == "welcome":
        settings.state = "connected"
        settings.host_name = body.get("name", "")
        _push_message(context, "system", "Connected")
        _upsert_user(context, body.get("name", "host"), body.get("color", (1, 1, 1, 1)), host=True)
        for peer in body.get("peers", []):
            _upsert_user(context, peer.get("name", "?"), peer.get("color", (1, 1, 1, 1)))
        # send our full mesh for tracked objects so the newcomer sees them
        _send_snapshots(context)
    elif kind == "relay":
        _push_message(context, "system", body.get("message", "Relay connected"))
    elif kind == "relay_error":
        settings.last_error = str(body.get("message", "relay error"))[:180]
    elif kind == "peer_join":
        _upsert_user(context, body.get("name", "?"), body.get("color", (1, 1, 1, 1)))
        _push_message(context, "system", f"{body.get('name', 'someone')} joined")
    elif kind == "peer_leave":
        _remove_user(context, body.get("name", "?"))
        _push_message(context, "system", f"{body.get('name', 'someone')} left")
    elif kind == "presence":
        _upsert_user(context, body.get("name", "?"), body.get("color", (1, 1, 1, 1)))
        for user in settings.users:
            if user.name == body.get("name"):
                user.scene = body.get("scene", "")
                user.frame = int(body.get("frame", 0))
                user.mode = body.get("mode", "")
                if body.get("name") == settings.name:
                    user.ping_ms = settings.rtt_ms
        if (
            settings.follow_host
            and body.get("name")
            and body.get("name") == settings.host_name
            and isinstance(body.get("frame"), int)
        ):
            runtime["applying_frame"] = True
            try:
                context.scene.frame_current = int(body["frame"])
            finally:
                runtime["applying_frame"] = False
    elif kind == "lease":
        action = body.get("action")
        if action in {"denied", "held"}:
            _push_message(context, "system", f"Locked by {body.get('owner', 'someone')}")
        elif action == "requested":
            runtime["lease_request"] = {"key": body.get("key"), "requester": body.get("requester")}
            _push_message(context, "system", f"{body.get('requester', 'someone')} requested edit")
        elif action == "grant":
            _push_message(context, "system", f"Lease granted to {body.get('owner', 'someone')}")
    elif kind == "image_asset":
        _apply_image(context, body)
    elif kind == "modifier_stack":
        _apply_modifier_stack(context, body)
    elif kind == "material_update":
        _apply_material(context, body)
    elif kind == "object_create":
        _apply_object_create(context, body)
    elif kind == "object_delete":
        _apply_object_delete(context, body)
    elif kind == "ping_mark":
        body_pos = body.get("pos") or []
        name = body.get("name", "?")
        color = body.get("color") or (1.0, 0.5, 0.2, 1.0)
        if len(color) == 3:
            color = tuple(color) + (1.0,)
        _spawn_ping(context, body_pos, name, color)
    elif kind == "chat":
        _push_message(context, body.get("name", "?"), body.get("text", ""))
    elif kind == "rtt":
        settings.rtt_ms = int(body.get("ms", 0))
    elif kind == "transform":
        obj_sync.apply(body.get("uuid"), body.get("matrix", []))
    elif kind == "mesh_delta":
        _apply_mesh_delta(context, body)
    elif kind == "mesh_snapshot":
        _apply_mesh_snapshot(context, body)
    elif kind == "error":
        settings.last_error = str(body.get("message", "unknown error"))
    elif kind == "disconnected":
        settings.state = "offline"
        _push_message(context, "system", f"Disconnected: {body.get('reason', 'unknown')}")


def _apply_mesh_delta(context, body: dict) -> None:
    uid = body.get("uuid")
    payload = body.get("payload", b"")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_MESH:
        return
    obj = obj_sync.find(uid)
    if obj is None or obj.type != "MESH":
        return
    if context.active_object == obj and context.mode in {"EDIT_MESH", "SCULPT"}:
        return  # never fight the local sculpt
    mirror = runtime["mirrors"].setdefault(uid, MeshMirror())
    try:
        mirror.apply_delta(obj.data, bytes(payload))
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _apply_mesh_snapshot(context, body: dict) -> None:
    uid = body.get("uuid")
    payload = body.get("payload", b"")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_MESH:
        return
    obj = obj_sync.find(uid)
    if obj is None or obj.type != "MESH":
        return
    if context.active_object == obj and context.mode in {"EDIT_MESH", "SCULPT"}:
        return  # never rebuild the mesh the user is editing
    mirror = runtime["mirrors"].setdefault(uid, MeshMirror())
    try:
        mirror.apply_snapshot(obj.data, bytes(payload))
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _send_snapshots(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    for name in sorted(runtime["tracked"]):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            continue
        uid = obj.get("bmp_uuid") or str(uuid.uuid4())
        obj["bmp_uuid"] = uid
        mirror = MeshMirror()
        runtime["mirrors"][uid] = mirror
        try:
            payload = mirror.full_snapshot(obj.data)
            session.send_mesh(obj, "snapshot", payload, 0)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# --------------------------------------------------------------------------- pings
PING_LIFETIME = 3.0


def _ping_material(name: str, color):
    material = bpy.data.materials.get(f"BMP Ping {name}")
    if material is None:
        material = bpy.data.materials.new(f"BMP Ping {name}")
        material.use_nodes = True
        material.diffuse_color = tuple(color)
        bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
        if bsdf is not None:
            for key in ("Emission Color", "Emission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = tuple(color)
                    break
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = 6.0
    return material


def _spawn_ping(context, position, name: str, color) -> None:
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        return
    try:
        location = tuple(float(v) for v in position)
    except (TypeError, ValueError):
        return
    collection = bpy.data.collections.get("Blendermp Pings")
    if collection is None:
        collection = bpy.data.collections.new("Blendermp Pings")
        context.scene.collection.children.link(collection)
        collection.hide_render = True
        collection.hide_select = True

    radius = 0.12
    vertices = [(0, 0, radius), (0, 0, -radius), (radius, 0, 0), (-radius, 0, 0), (0, radius, 0), (0, -radius, 0)]
    faces = [(0, 2, 4), (4, 2, 1), (1, 2, 3), (3, 2, 0), (0, 4, 3), (4, 1, 3)]
    mesh = bpy.data.meshes.new(f"BMP Ping {name}")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    mesh.materials.append(_ping_material(name, color))

    obj = bpy.data.objects.new(f"Ping {name}", mesh)
    obj.location = location
    obj.color = tuple(color)
    obj.show_in_front = True
    obj["bmp_ping"] = True
    collection.objects.link(obj)
    runtime["pings"].append((obj, time.monotonic() + PING_LIFETIME))


def _expire_pings(context) -> None:
    now = time.monotonic()
    for item in list(runtime["pings"]):
        obj, expiry = item
        if now < expiry:
            continue
        runtime["pings"].remove(item)
        mesh = obj.data
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- materials
def material_values(material) -> dict:
    base = list(getattr(material, "diffuse_color", (0.8, 0.8, 0.8, 1.0)))[:4]
    data = {
        "name": material.name,
        "base_color": base,
        "metallic": 0.0,
        "roughness": 0.5,
        "emission_color": [0.0, 0.0, 0.0],
        "emission_strength": 0.0,
    }
    if not material.use_nodes or material.node_tree is None:
        return data
    for node in material.node_tree.nodes:
        if node.type != "BSDF_PRINCIPLED":
            continue
        inputs = node.inputs
        try:
            data["base_color"] = list(inputs["Base Color"].default_value)[:4]
            data["metallic"] = float(inputs["Metallic"].default_value)
            data["roughness"] = float(inputs["Roughness"].default_value)
            for key in ("Emission Color", "Emission"):
                if key in inputs:
                    data["emission_color"] = list(inputs[key].default_value)[:3]
                    break
            if "Emission Strength" in inputs:
                data["emission_strength"] = float(inputs["Emission Strength"].default_value)
        except (KeyError, TypeError, ValueError):
            pass
        break
    return data


def _material_signature(data: dict) -> tuple:
    return (
        data.get("name"),
        tuple(data.get("base_color", ())),
        data.get("metallic"),
        data.get("roughness"),
        tuple(data.get("emission_color", ())),
        data.get("emission_strength"),
    )


def _sync_materials(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or not settings.sync_materials:
        return
    now = time.monotonic()
    if now - runtime["last_material"] < 0.25:
        return
    runtime["last_material"] = now
    obj = context.active_object
    if obj is None or not getattr(obj, "material_slots", None):
        return
    uid = obj.get("bmp_uuid")
    if not uid:
        return
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        if material is None:
            continue
        data = material_values(material)
        signature = _material_signature(data)
        key = (uid, index)
        if runtime["material_shadow"].get(key) == signature:
            continue
        runtime["material_shadow"][key] = signature
        session.send_material(uid, index, data)


def _apply_material(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    if obj is None or getattr(obj, "material_slots", None) is None:
        return
    data = material_payload.sanitize(body.get("material"))
    slot_index = body.get("slot")
    if data is None or not isinstance(slot_index, int) or slot_index < 0:
        return
    while len(obj.material_slots) <= slot_index:
        if getattr(obj.data, "materials", None) is None:
            return
        obj.data.materials.append(None)
    slot = obj.material_slots[slot_index]
    material = slot.material
    if material is None:
        material = bpy.data.materials.new(data["name"])
        slot.material = material
    material.use_nodes = True
    material.diffuse_color = tuple(data["base_color"])
    if material.node_tree is not None:
        for node in material.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            inputs = node.inputs
            try:
                inputs["Base Color"].default_value = data["base_color"]
                inputs["Metallic"].default_value = data["metallic"]
                inputs["Roughness"].default_value = data["roughness"]
                for key in ("Emission Color", "Emission"):
                    if key in inputs:
                        inputs[key].default_value = data["emission_color"]
                        break
                if "Emission Strength" in inputs:
                    inputs["Emission Strength"].default_value = data["emission_strength"]
            except (KeyError, TypeError, ValueError):
                pass
            break
    runtime["material_shadow"][(body.get("uuid"), slot_index)] = _material_signature(data)


# --------------------------------------------------------------------------- modifiers
MODIFIER_PROPS = {
    "SUBSURF": ("levels", "render_levels", "subdivision_type"),
    "MIRROR": ("use_axis", "use_bisect_axis", "use_clip", "merge_threshold"),
    "ARRAY": ("count", "use_relative_offset", "relative_offset_displace", "use_constant_offset", "constant_offset_displace"),
    "SOLIDIFY": ("thickness", "offset", "use_even_offset"),
    "BEVEL": ("width", "segments", "limit_method", "angle_limit"),
    "DECIMATE": ("ratio", "decimate_type"),
    "TRIANGULATE": ("quad_method", "ngon_method", "min_vertices"),
    "SMOOTH": ("factor", "iterations"),
    "WELD": ("merge_threshold", "mode"),
    "SCREW": ("angle", "screw_offset", "steps", "render_steps"),
}


def modifier_snapshot(obj) -> list:
    snapshot = []
    for mod in list(obj.modifiers)[:32]:
        props = {}
        for key in MODIFIER_PROPS.get(mod.type, ()):
            try:
                value = getattr(mod, key)
            except AttributeError:
                continue
            if isinstance(value, (bool, int, float, str)):
                props[key] = value
            elif isinstance(value, (list, tuple)):
                props[key] = [item for item in value if isinstance(item, (bool, int, float, str))]
        if mod.type == "NODES":
            try:
                props["node_group"] = mod.node_group.name if mod.node_group else ""
            except AttributeError:
                props["node_group"] = ""
        snapshot.append(
            {
                "type": mod.type,
                "name": mod.name,
                "show_viewport": bool(mod.show_viewport),
                "show_render": bool(mod.show_render),
                "props": props,
            }
        )
    return snapshot


def _modifier_signature(snapshot: list) -> str:
    return json.dumps(snapshot, sort_keys=True, default=str)


def _sync_modifiers(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or not settings.sync_modifiers:
        return
    now = time.monotonic()
    if now - runtime["last_modifier"] < 0.5:
        return
    runtime["last_modifier"] = now
    obj = context.active_object
    if obj is None or not hasattr(obj, "modifiers"):
        return
    uid = obj.get("bmp_uuid")
    if not uid:
        return
    snapshot = modifier_snapshot(obj)
    signature = _modifier_signature(snapshot)
    if runtime["modifier_shadow"].get(uid) == signature:
        return
    runtime["modifier_shadow"][uid] = signature
    session.send_modifier_stack(uid, snapshot)


def _apply_modifier_stack(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    snapshot = body.get("modifiers")
    if obj is None or not hasattr(obj, "modifiers") or not isinstance(snapshot, list):
        return
    desired = {(str(item.get("name")), str(item.get("type"))) for item in snapshot if isinstance(item, dict)}
    for mod in list(obj.modifiers):
        if (mod.name, mod.type) not in desired:
            try:
                obj.modifiers.remove(mod)
            except Exception:  # noqa: BLE001
                pass
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        type_ = str(item.get("type", ""))
        name = str(item.get("name", type_))
        mod = next((m for m in obj.modifiers if m.name == name and m.type == type_), None)
        if mod is None:
            existing = obj.modifiers.get(name)
            if existing is not None:
                try:
                    obj.modifiers.remove(existing)
                except Exception:  # noqa: BLE001
                    pass
            try:
                mod = obj.modifiers.new(name, type_)
            except Exception:  # noqa: BLE001
                continue
        try:
            mod.show_viewport = bool(item.get("show_viewport", True))
            mod.show_render = bool(item.get("show_render", True))
        except Exception:  # noqa: BLE001
            pass
        for key, value in (item.get("props") or {}).items():
            if key == "node_group":
                group = bpy.data.node_groups.get(str(value))
                if group is not None:
                    try:
                        mod.node_group = group
                    except Exception:  # noqa: BLE001
                        pass
                continue
            try:
                setattr(mod, key, value)
            except Exception:  # noqa: BLE001
                pass
    runtime["modifier_shadow"][body.get("uuid")] = _modifier_signature(snapshot)


# --------------------------------------------------------------------------- images
IMAGE_FORMATS = {
    "PNG": "png",
    "JPEG": "jpg",
    "BMP": "bmp",
    "TARGA": "tga",
    "TIFF": "tif",
    "OPEN_EXR": "exr",
    "HDR": "hdr",
    "WEBP": "webp",
    "GIF": "gif",
    "PSD": "psd",
}


def _image_bytes(context, image):
    format_hint = IMAGE_FORMATS.get(image.file_format, "png")
    try:
        if image.packed_file is not None:
            return bytes(image.packed_file.data), format_hint
        if image.source == "FILE" and image.filepath:
            path = bpy.path.abspath(image.filepath)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    data = handle.read()
                extension = os.path.splitext(path)[1].lstrip(".").lower()
                if extension in image_payload.ALLOWED:
                    format_hint = extension
                return data, format_hint
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            image.save_render(path, scene=context.scene)
            with open(path, "rb") as reader:
                return reader.read(), "png"
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return b"", "png"


def _apply_image(context, body: dict) -> None:
    data = body.get("data")
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > image_payload.MAX_IMAGE:
        return
    format_hint = str(body.get("format", "png")).lower()
    if format_hint not in image_payload.ALLOWED:
        format_hint = "png"
    name = str(body.get("name", "Image"))[:64] or "Image"
    handle, path = tempfile.mkstemp(suffix="." + format_hint)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(bytes(data))
        image = bpy.data.images.load(path, check_existing=False)
        if bpy.data.images.get(name) is not None:
            index = 1
            while bpy.data.images.get(f"{name}.{index:03d}") is not None:
                index += 1
            name = f"{name}.{index:03d}"
        image.name = name
        image.pack()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def share_active_images(context) -> int:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return 0
    images = {}
    obj = context.active_object
    if obj is not None:
        for slot in getattr(obj, "material_slots", []) or []:
            material = slot.material
            if material is None or not material.use_nodes or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image is not None:
                    images[node.image.name] = node.image
    count = 0
    for image in images.values():
        data, format_hint = _image_bytes(context, image)
        if not data or len(data) > image_payload.MAX_IMAGE:
            continue
        session.send_image(image.name, format_hint, data)
        count += 1
    return count


# --------------------------------------------------------------------------- bpy timer
def _poll_control(context) -> None:
    now = time.monotonic()
    if now - runtime["control_checked"] < CONTROL_POLL:
        return
    runtime["control_checked"] = now
    request = control.read_session()
    if not request:
        return
    nonce = request.get("nonce")
    if not nonce or nonce == runtime["last_control_nonce"]:
        return
    runtime["last_control_nonce"] = nonce
    settings = context.window_manager.bmp
    if request.get("name"):
        settings.name = request["name"]
    if request.get("port"):
        settings.port = int(request["port"])
    if request.get("fingerprint"):
        settings.fingerprint = request["fingerprint"]
    if "pin" in request:
        settings.pin = request["pin"]
    if request.get("relay"):
        host, port_text = request["relay"].rsplit(":", 1)
        settings.relay_host = host
        settings.relay_port = int(port_text)
    if "use_relay" in request:
        settings.use_relay = bool(request["use_relay"])
    action = request.get("action")
    try:
        if action == "host":
            start_host(context, request.get("code") or None, request.get("port"))
        elif action == "join":
            code = request.get("code", "")
            if code:
                start_join(context, code)
        elif action == "chat":
            text = request.get("text", "")
            session = runtime.get("session")
            if session is None or not text:
                settings.last_error = "Not connected"
            else:
                session.send_chat(text)
                _push_message(context, settings.name, text)
        elif action == "leave":
            stop_session(context)
    except Exception as exc:  # noqa: BLE001
        settings.last_error = str(exc)
        traceback.print_exc()


# --------------------------------------------------------------------------- objects
def _object_kind(obj) -> int:
    if obj.type == "MESH":
        return object_payload.KIND_MESH
    if obj.type == "CAMERA":
        return object_payload.KIND_CAMERA
    if obj.type == "LIGHT":
        return object_payload.KIND_LIGHT
    return object_payload.KIND_EMPTY


def _prime_objects(context) -> None:
    known = set()
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
        known.add(uid)
    runtime["known_objects"] = known


def _send_object_create(session: SessionService, obj) -> None:
    uid = obj.get("bmp_uuid") or str(uuid.uuid4())
    obj["bmp_uuid"] = uid
    kind = _object_kind(obj)
    matrix = [float(value) for row in obj.matrix_world for value in row]
    meta = {
        "name": obj.name[:48],
        "matrix": matrix,
        "parent": obj.parent.get("bmp_uuid") if obj.parent else None,
    }
    geometry = b""
    if kind == object_payload.KIND_MESH:
        mirror = runtime["mirrors"].get(uid)
        if mirror is None:
            mirror = MeshMirror()
            runtime["mirrors"][uid] = mirror
        try:
            geometry = mirror.full_snapshot(obj.data)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            geometry = b""
    elif kind == object_payload.KIND_CAMERA:
        meta["lens"] = float(obj.data.lens)
    elif kind == object_payload.KIND_LIGHT:
        meta["light_type"] = obj.data.type
        meta["energy"] = float(obj.data.energy)
        meta["color"] = list(obj.data.color)[:3]
    try:
        session.send_object_create(uid, kind, meta, geometry)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _apply_object_create(context, body: dict) -> None:
    uid = body.get("uuid")
    if not uid or obj_sync.find(uid):
        return
    kind = body.get("kind")
    meta = body.get("meta") or {}
    name = str(meta.get("name", "Object"))[:48] or "Object"

    try:
        if kind == object_payload.KIND_MESH:
            mesh = bpy.data.meshes.new(name)
            obj = bpy.data.objects.new(name, mesh)
            mirror = MeshMirror()
            mirror.apply_snapshot(mesh, body.get("geometry", b""))
            runtime["mirrors"][uid] = mirror
        elif kind == object_payload.KIND_CAMERA:
            data = bpy.data.cameras.new(name)
            obj = bpy.data.objects.new(name, data)
            if isinstance(meta.get("lens"), (int, float)):
                data.lens = max(1.0, min(500.0, float(meta["lens"])))
        elif kind == object_payload.KIND_CURVE:
            data = bpy.data.curves.new(name, type="CURVE")
            dimension = meta.get("dimensions")
            if dimension in {"2D", "3D"}:
                data.dimensions = dimension
            obj = bpy.data.objects.new(name, data)
        elif kind == object_payload.KIND_FONT:
            data = bpy.data.curves.new(name, type="FONT")
            body_text = meta.get("body")
            if isinstance(body_text, str):
                data.body = body_text[:512]
            if isinstance(meta.get("size"), (int, float)):
                data.size = max(0.001, min(1000.0, float(meta["size"])))
            if meta.get("align_x") in {"LEFT", "CENTER", "RIGHT", "JUSTIFY", "FLUSH"}:
                data.align_x = meta["align_x"]
            if meta.get("align_y") in {"TOP_BASELINE", "TOP", "CENTER", "BOTTOM_BASELINE", "BOTTOM"}:
                data.align_y = meta["align_y"]
            obj = bpy.data.objects.new(name, data)
        elif kind == object_payload.KIND_LIGHT:
            light_type = meta.get("light_type")
            if light_type not in {"POINT", "SUN", "SPOT", "AREA"}:
                light_type = "POINT"
            data = bpy.data.lights.new(name, type=light_type)
            obj = bpy.data.objects.new(name, data)
            if isinstance(meta.get("energy"), (int, float)):
                data.energy = max(0.0, min(1e6, float(meta["energy"])))
            color = meta.get("color")
            if isinstance(color, list) and len(color) == 3:
                data.color = [max(0.0, min(1.0, float(v))) for v in color]
        else:
            obj = bpy.data.objects.new(name, None)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return

    target = context.scene.collection
    collection_name = meta.get("collection")
    if isinstance(collection_name, str) and collection_name:
        collection = bpy.data.collections.get(collection_name)
        if collection is not None:
            target = collection
    target.objects.link(obj)
    obj["bmp_uuid"] = uid
    runtime["known_objects"].add(uid)
    obj_sync.apply(uid, meta.get("matrix", []))
    parent_uid = meta.get("parent")
    if parent_uid:
        parent = obj_sync.find(parent_uid)
        if parent is not None:
            obj.parent = parent
            obj_sync.apply(uid, meta.get("matrix", []))


def _apply_object_delete(context, body: dict) -> None:
    uid = body.get("uuid")
    if not uid:
        return
    obj = obj_sync.find(uid)
    runtime["known_objects"].discard(uid)
    runtime["mirrors"].pop(uid, None)
    runtime["object_shadow"].pop(uid, None)
    if obj is None:
        return
    if context.active_object is obj and context.mode != "OBJECT":
        return
    mesh = obj.data if obj.type == "MESH" else None
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _sync_transforms(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_transforms or settings.paused:
        return
    known = runtime["known_objects"]
    current = set()
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
            _send_object_create(session, obj)
        current.add(uid)
    for uid in list(known - current):
        session.send_object_delete(uid)
    runtime["known_objects"] = current

    shadow: dict = runtime["object_shadow"]
    seen = set()
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
        seen.add(uid)
        matrix = obj.matrix_world
        current = tuple(round(v, 6) for row in matrix for v in row)
        previous = shadow.get(uid)
        if previous is None:
            shadow[uid] = current
            continue
        if current != previous:
            shadow[uid] = current
            session.send_transform(uid, current)
    for stale in list(shadow.keys() - seen):
        del shadow[stale]


def _sync_mesh(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_mesh or settings.paused:
        return
    now = time.monotonic()
    if now - runtime["last_mesh"] < MESH_POLL:
        return
    runtime["last_mesh"] = now

    active = context.active_object
    if active is not None and active.type == "MESH" and context.mode in {"EDIT_MESH", "SCULPT"}:
        runtime["tracked"].add(active.name)

    names = sorted(name for name in runtime["tracked"] if name in bpy.data.objects)
    if not names:
        return
    cursor = runtime.get("mesh_cursor", 0) % len(names)
    runtime["mesh_cursor"] = cursor + 1
    obj = bpy.data.objects.get(names[cursor])
    if obj is None or obj.type != "MESH":
        runtime["tracked"].discard(names[cursor])
        return

    uid = obj.get("bmp_uuid") or str(uuid.uuid4())
    obj["bmp_uuid"] = uid
    mirror = runtime["mirrors"].get(uid)
    if mirror is None or not mirror.matches(obj.data):
        mirror = MeshMirror()
        runtime["mirrors"][uid] = mirror
        try:
            snapshot = mirror.full_snapshot(obj.data)
            session.send_mesh(obj, "snapshot", snapshot, 0)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return
    try:
        delta = mirror.poll(obj.data)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    if delta is not None:
        rev, payload = delta
        session.send_mesh(obj, "delta", payload, rev)


def on_frame_change(scene) -> None:
    if runtime.get("applying_frame"):
        return
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    session.send_presence(
        {
            "scene": scene.name,
            "frame": scene.frame_current,
            "mode": bpy.context.mode if bpy.context else "OBJECT",
        }
    )


def _send_presence(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    now = time.monotonic()
    if now - runtime["last_presence"] < 1.0:
        return
    runtime["last_presence"] = now
    session.send_presence(
        {
            "scene": context.scene.name,
            "frame": context.scene.frame_current,
            "mode": context.mode,
        }
    )


def _write_status(context) -> None:
    session: SessionService | None = runtime.get("session")
    now = time.monotonic()
    if now - runtime["last_status"] < STATUS_WRITE:
        return
    runtime["last_status"] = now
    settings = context.window_manager.bmp
    stats = session.stats() if session else {}
    users = [
        {
            "name": user.name,
            "color": list(user.color),
            "ping_ms": user.ping_ms,
            "host": user.host,
        }
        for user in settings.users
    ]
    messages = [
        {"author": item.author, "text": item.text, "stamp": item.stamp}
        for item in list(settings.messages)[-20:]
    ]
    control.write_status(
        {
            "state": settings.state,
            "code": settings.code,
            "is_host": settings.is_host,
            "name": settings.name,
            "fingerprint": settings.fingerprint,
            "verify_code": settings.verify_code,
            "last_error": settings.last_error,
            "relay": bool(stats.get("relay", False)),
            "rtt_ms": stats.get("rtt_ms", 0),
            "bytes_in": stats.get("bytes_in", 0),
            "bytes_out": stats.get("bytes_out", 0),
            "peers": stats.get("peers", []),
            "users": users,
            "messages": messages,
            "blender": bpy.app.version_string,
            "addon": ADDON_VERSION,
        }
    )


def bmp_tick():
    context = bpy.context
    if context is None or context.window_manager is None:
        return TICK
    try:
        _poll_control(context)
        session: SessionService | None = runtime.get("session")
        inbox = runtime["inbox"]
        _expire_pings(context)
        while True:
            try:
                kind, body = inbox.get_nowait()
            except queue.Empty:
                break
            _handle_event(context, kind, body)
        if session is not None:
            _sync_transforms(context)
            _sync_mesh(context)
            _sync_materials(context)
            _sync_modifiers(context)
            _send_presence(context)
            _write_status(context)
            if session.failed:
                settings = context.window_manager.bmp
                settings.last_error = session.failure_reason or "connection failed"
                stop_session(context)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return TICK


# --------------------------------------------------------------------------- operators
class BMP_OT_host(Operator):
    bl_idname = "bmp.host"
    bl_label = "Host Session"
    bl_description = "Start hosting a session in this scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        start_host(context)
        return {"FINISHED"}


class BMP_OT_join(Operator):
    bl_idname = "bmp.join"
    bl_label = "Join Session"
    bl_description = "Join a session with a room code"
    bl_options = {"REGISTER"}

    code: StringProperty(name="Room code")

    def invoke(self, context, event):
        self.code = context.window_manager.bmp.join_code
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "code", text="Code")

    def execute(self, context):
        code = self.code.strip()
        if not code:
            self.report({"ERROR"}, "Enter a room code")
            return {"CANCELLED"}
        try:
            start_join(context, code)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class BMP_OT_leave(Operator):
    bl_idname = "bmp.leave"
    bl_label = "Leave Session"
    bl_description = "Disconnect from the current session"
    bl_options = {"REGISTER"}

    def execute(self, context):
        stop_session(context)
        return {"FINISHED"}


class BMP_OT_copy_code(Operator):
    bl_idname = "bmp.copy_code"
    bl_label = "Copy Room Code"
    bl_description = "Copy the room code to the clipboard"

    def execute(self, context):
        code = context.window_manager.bmp.code
        if code:
            context.window_manager.clipboard = code
            self.report({"INFO"}, f"Copied {code}")
        return {"FINISHED"}


class BMP_OT_send_chat(Operator):
    bl_idname = "bmp.send_chat"
    bl_label = "Send"
    bl_description = "Send a chat message"

    def execute(self, context):
        settings = context.window_manager.bmp
        text = settings.chat_text.strip()
        if not text:
            return {"CANCELLED"}
        session: SessionService | None = runtime.get("session")
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            command = command.lower()
            settings.chat_text = ""
            if command == "/help":
                _push_message(context, "system", "Commands: /who /ping /lock /grant <user> /leave /me <text>")
            elif command == "/who":
                names = ", ".join(user.name for user in settings.users) or "nobody"
                _push_message(context, "system", f"In the room: {names}")
            elif command == "/lock":
                obj = context.active_object
                uid = obj.get("bmp_uuid") if obj else None
                if session and uid:
                    session.send_lease(uid, "acquire")
                    _push_message(context, "system", "Edit requested")
                else:
                    _push_message(context, "system", "Select a tracked object first")
            elif command == "/grant":
                obj = context.active_object
                uid = obj.get("bmp_uuid") if obj else None
                requester = argument.strip()
                if session and uid and requester:
                    session.send_lease(uid, "grant", requester)
                else:
                    _push_message(context, "system", "Usage: /grant <user> with the object selected")
            elif command == "/ping":
                if session:
                    session.send_ping()
                    _push_message(context, "system", "Ping sent")
            elif command == "/leave":
                stop_session(context)
            elif command == "/me":
                if session and argument.strip():
                    session.send_chat(f"* {settings.name} {argument.strip()}")
                    _push_message(context, "*", f"{settings.name} {argument.strip()}")
            else:
                _push_message(context, "system", f"Unknown command: {command}")
            return {"FINISHED"}
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        session.send_chat(text)
        _push_message(context, settings.name, text)
        settings.chat_text = ""
        return {"FINISHED"}


class BMP_OT_track_mesh(Operator):
    bl_idname = "bmp.track_mesh"
    bl_label = "Track Meshes"
    bl_description = "Stream the selected meshes to the session"
    bl_options = {"REGISTER"}

    def execute(self, context):
        objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not objects and context.active_object and context.active_object.type == "MESH":
            objects = [context.active_object]
        if not objects:
            self.report({"ERROR"}, "Select one or more mesh objects")
            return {"CANCELLED"}
        session: SessionService | None = runtime.get("session")
        for obj in objects:
            runtime["tracked"].add(obj.name)
            uid = obj.get("bmp_uuid") or str(uuid.uuid4())
            obj["bmp_uuid"] = uid
            mirror = MeshMirror()
            runtime["mirrors"][uid] = mirror
            if session:
                try:
                    session.send_mesh(obj, "snapshot", mirror.full_snapshot(obj.data), 0)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        self.report({"INFO"}, f"Tracking {len(objects)} mesh(es)")
        return {"FINISHED"}


class BMP_OT_untrack_mesh(Operator):
    bl_idname = "bmp.untrack_mesh"
    bl_label = "Untrack Meshes"
    bl_description = "Stop streaming the selected meshes"
    bl_options = {"REGISTER"}

    def execute(self, context):
        objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not objects and context.active_object and context.active_object.type == "MESH":
            objects = [context.active_object]
        for obj in objects:
            runtime["tracked"].discard(obj.name)
            uid = obj.get("bmp_uuid")
            if uid:
                runtime["mirrors"].pop(uid, None)
        self.report({"INFO"}, f"Untracked {len(objects)} mesh(es)")
        return {"FINISHED"}


class BMP_OT_share_images(Operator):
    bl_idname = "bmp.share_images"
    bl_label = "Share Images"
    bl_description = "Send images used by the active object's materials to the room"

    def execute(self, context):
        count = share_active_images(context)
        if count == 0:
            self.report({"WARNING"}, "No shareable images on the active object")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Shared {count} image(s)")
        return {"FINISHED"}


class BMP_OT_request_edit(Operator):
    bl_idname = "bmp.request_edit"
    bl_label = "Request Edit"
    bl_description = "Ask the current owner for the edit lease on the active object"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        obj = context.active_object
        uid = obj.get("bmp_uuid") if obj else None
        if session is None or not uid:
            self.report({"ERROR"}, "Select a tracked object in a session")
            return {"CANCELLED"}
        session.send_lease(uid, "acquire")
        self.report({"INFO"}, "Edit requested")
        return {"FINISHED"}


class BMP_OT_grant_edit(Operator):
    bl_idname = "bmp.grant_edit"
    bl_label = "Grant Edit"
    bl_description = "Grant the pending edit request to its requester"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        request = runtime.get("lease_request")
        if session is None or not request:
            self.report({"ERROR"}, "No pending request")
            return {"CANCELLED"}
        session.send_lease(request["key"], "grant", request.get("requester"))
        runtime["lease_request"] = None
        self.report({"INFO"}, f"Granted to {request.get('requester')}")
        return {"FINISHED"}


class BMP_OT_ping(Operator):
    bl_idname = "bmp.ping"
    bl_label = "Ping"
    bl_description = "Measure round-trip latency to the host"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        if session is None:
            return {"CANCELLED"}
        session.send_ping()
        return {"FINISHED"}


class BMP_OT_ping_mark(Operator):
    bl_idname = "bmp.ping_mark"
    bl_label = "Ping Cursor"
    bl_description = "Drop a short-lived marker for everyone at the 3D cursor"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        position = context.scene.cursor.location
        session.send_ping_mark((position.x, position.y, position.z))
        _spawn_ping(context, (position.x, position.y, position.z), context.window_manager.bmp.name, tuple(context.window_manager.bmp.color))
        return {"FINISHED"}


class BMP_OT_export_log(Operator, ExportHelper):
    bl_idname = "bmp.export_log"
    bl_label = "Export diagnostics"
    bl_description = "Write session diagnostics to a text file"
    filename_ext = ".txt"
    filter_glob: StringProperty(default="*.txt", options={"HIDDEN"})

    def execute(self, context):
        settings = context.window_manager.bmp
        session: SessionService | None = runtime.get("session")
        stats = session.stats() if session else {}
        lines = [
            f"Blendermp addon {ADDON_VERSION}",
            f"Blender {bpy.app.version_string}",
            f"state={settings.state} host={settings.is_host} code={settings.code}",
            f"name={settings.name} port={settings.port}",
            f"rtt_ms={stats.get('rtt_ms')} bytes_in={stats.get('bytes_in')} bytes_out={stats.get('bytes_out')}",
            f"peers={stats.get('peers')}",
            f"error={settings.last_error}",
        ]
        with open(self.filepath, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return {"FINISHED"}


classes = (
    BMPUser,
    BMPMessage,
    BMPState,
    BMP_OT_host,
    BMP_OT_join,
    BMP_OT_leave,
    BMP_OT_copy_code,
    BMP_OT_send_chat,
    BMP_OT_track_mesh,
    BMP_OT_untrack_mesh,
    BMP_OT_ping,
    BMP_OT_ping_mark,
    BMP_OT_share_images,
    BMP_OT_request_edit,
    BMP_OT_grant_edit,
    BMP_OT_export_log,
)


def register() -> None:
    if getattr(bpy, "_blendermp_active", False):
        print("Blendermp: another copy is already active; skipping duplicate registration")
        return
    bpy._blendermp_active = True
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.bmp = bpy.props.PointerProperty(type=BMPState)
    ui.register()
    runtime["started"] = time.monotonic()
    bpy.app.timers.register(bmp_tick, first_interval=0.5, persistent=True)
    bpy.app.handlers.frame_change_post.append(on_frame_change)


def unregister() -> None:
    if not getattr(bpy, "_blendermp_active", False):
        return
    bpy._blendermp_active = False
    try:
        bpy.app.timers.unregister(bmp_tick)
    except ValueError:
        pass
    session = runtime.get("session")
    if session:
        session.stop()
        runtime["session"] = None
    ui.unregister()
    del bpy.types.WindowManager.bmp
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
