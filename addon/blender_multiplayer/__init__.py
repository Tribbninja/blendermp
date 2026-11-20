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

import hashlib
import json
import math
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
from .core import chunker
from .core import image_payload
from .core import material as material_payload
from .core import object_payload
from .core.version import ADDON_VERSION
from .session import MAX_MESH, SessionService
from .sync import objects as obj_sync
from .sync.mesh import EditMirror, MeshMirror, snapshot_bmesh

bl_info = {
    "name": "Blendermp",
    "author": "Blendermp",
    "version": (0, 1, 13),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Blendermp",
    "description": "Low-latency collaborative Blender sessions",
    "category": "Network",
}

TICK = 1.0 / 60.0
TOPOLOGY_POLL = 0.2
TOPOLOGY_DEBOUNCE = 0.15
CONTROL_POLL = 0.5
STATUS_WRITE = 0.5
MESH_POLL = 1.0 / 20.0
EDIT_POLL = 1.0 / 30.0
SELECTION_POLL = 0.2
LEASE_RENEW = 30.0
PRESENCE_POLL = 1.0 / 20.0
REMOTE_POLL = 1.0 / 30.0
TRANSFORM_SMOOTH = 0.02
MESH_SMOOTH = 0.03
VIEW_SMOOTH = 0.05
DEFAULT_COLOR = (0.35, 0.8, 0.95, 1.0)
USER_PALETTE = (
    (0.35, 0.8, 0.95, 1.0),
    (0.98, 0.65, 0.25, 1.0),
    (0.55, 0.85, 0.45, 1.0),
    (0.85, 0.5, 0.9, 1.0),
    (0.95, 0.45, 0.45, 1.0),
    (0.95, 0.85, 0.35, 1.0),
    (0.45, 0.7, 1.0, 1.0),
    (0.65, 0.9, 0.8, 1.0),
)


ROLE_NAMES = {0: "viewer", 1: "commenter", 2: "editor", 3: "admin", 4: "owner"}
ROLE_VALUES = {name: value for value, name in ROLE_NAMES.items()}


def _role_name(value) -> str:
    if isinstance(value, str) and value in ROLE_VALUES:
        return value
    try:
        return ROLE_NAMES.get(int(value), "editor")
    except (TypeError, ValueError):
        return "editor"


def effective_color(name: str, color) -> tuple:
    try:
        values = tuple(float(value) for value in color)[:4]
    except (TypeError, ValueError):
        values = DEFAULT_COLOR
    if len(values) < 4:
        values = values + (1.0,) * (4 - len(values))
    if all(abs(values[index] - DEFAULT_COLOR[index]) < 0.02 for index in range(3)):
        digest = hashlib.sha256(str(name).encode("utf-8")).digest()
        return USER_PALETTE[digest[0] % len(USER_PALETTE)]
    return values

runtime: dict = {
    "session": None,
    "inbox": queue.Queue(),
    "mirrors": {},
    "edit_mirrors": {},
    "object_shadow": {},
    "lease_owners": {},
    "local_transform": {},
    "edit_lease_uid": None,
    "selection": set(),
    "hidden_objects": set(),
    "deselected": {},
    "owned_leases": set(),
    "last_selection": 0.0,
    "remote_batches": [],
    "remote_point_batches": [],
    "remote_labels": [],
    "remote_shader": None,
    "last_remote": 0.0,
    "view_smoothers": {},
    "remote_debug": {},
    "remote_selections": {},
    "remote_points": [],
    "lasers": {},
    "last_transform": 0.0,
    "fast_mode": True,
    "transform_interval": 1.0 / 60.0,
    "presence_interval": 1.0 / 30.0,
    "transform_smooth": TRANSFORM_SMOOTH,
    "mesh_smooth": MESH_SMOOTH,
    "view_smooth": VIEW_SMOOTH,
    "selection_digest": {},
    "last_edit_sel": 0.0,
    "last_selection_uid": "",
    "last_selection_count": 0,
    "topology_sig": {},
    "topology_due": {},
    "last_topology": 0.0,
    "create_errors": set(),
    "draw_lines": None,
    "draw_labels": None,
    "transform_smoothers": {},
    "mesh_smoothers": {},
    "lease_renew": 0.0,
    "bootstrapping": False,
    "bootstrap_deadline": 0.0,
    "bootstraps": {},
    "tracked": set(),
    "last_control_nonce": None,
    "last_status": 0.0,
    "last_mesh": 0.0,
    "last_edit": 0.0,
    "mesh_cursor": 0,
    "pings": [],
    "known_objects": set(),
    "applying_frame": False,
    "material_shadow": {},
    "modifier_shadow": {},
    "last_modifier": 0.0,
    "lease_request": None,
    "lease_requests": {},
    "last_material": 0.0,
    "material_cursor": 0,
    "last_presence": 0.0,
    "control_checked": 0.0,
    "started": 0.0,
    "session_started": 0.0,
    "relay_bridge": None,
}

addon_keymaps: list = []


# --------------------------------------------------------------------------- props
class BMPUser(PropertyGroup):
    name: StringProperty()
    color: FloatVectorProperty(size=4, subtype="COLOR", min=0.0, max=1.0, default=(0.4, 0.8, 1.0, 1.0))
    ping_ms: IntProperty(default=0)
    scene: StringProperty()
    frame: IntProperty()
    mode: StringProperty()
    active: StringProperty()
    view: FloatVectorProperty(size=16)
    has_view: BoolProperty()
    view_aspect: FloatProperty(default=1.6)
    view_persp: StringProperty()
    view_distance: FloatProperty(default=7.0)
    selected: StringProperty()
    host: BoolProperty()
    role: StringProperty(default="editor")
    cursor: FloatVectorProperty(size=3)
    has_cursor: BoolProperty()


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
    pin: StringProperty(name="Server password", subtype="PASSWORD", maxlen=64)
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
    follow_user: StringProperty()
    show_cameras: BoolProperty(name="Show cameras", default=True)
    show_selections: BoolProperty(name="Show selections", default=True)
    show_labels: BoolProperty(name="Show name tags", default=True)
    host_name: StringProperty()
    sync_transforms: BoolProperty(name="Sync object transforms", default=True)
    users: CollectionProperty(type=BMPUser)
    messages: CollectionProperty(type=BMPMessage)
    show_users: BoolProperty(default=True)
    show_chat: BoolProperty(default=True)
    lease_info: StringProperty()
    lease_requester: StringProperty()
    role: StringProperty(default="editor")
    overlay_info: StringProperty()
    direct_code: StringProperty()


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


def _upsert_user(context, name: str, color, host: bool = False, role: str | None = None) -> None:
    users = context.window_manager.bmp.users
    for user in users:
        if user.name == name:
            user.color = color
            user.host = host
            if role:
                user.role = role
            return
    user = users.add()
    user.name = name
    user.color = color
    user.host = host
    user.role = role or "editor"


def _remove_user(context, name: str) -> None:
    runtime["view_smoothers"].pop(name, None)
    runtime["remote_selections"].pop(name, None)
    runtime["lasers"].pop(name, None)
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
def start_host(
    context,
    code: str | None = None,
    port: int | None = None,
    relay_url: str | None = None,
    relay_token: str | None = None,
) -> None:
    settings = context.window_manager.bmp
    existing = runtime.get("session")
    if (
        existing is not None
        and getattr(existing, "is_host", False)
        and existing.is_alive()
        and getattr(existing, "state", "") == "hosting"
        and code is None
    ):
        print("BMP host already running; ignoring duplicate start", flush=True)
        return
    # Keep the previous session's late events out of this one. They used to land
    # in a shared inbox and overwrite the fresh state with "offline".
    stop_session(context, publish=False)
    inbox = queue.Queue()
    runtime["inbox"] = inbox
    from .core.roomcode import FLAG_PIN, FLAG_RELAY, encode_room, room_id

    flags = FLAG_PIN if settings.pin else 0
    relay: tuple[str, int] | None = None
    bridge = None
    if code is None and settings.use_relay:
        if relay_url:
            endpoint_port = port or settings.port
            code = encode_room(_local_ip(), endpoint_port, flags | FLAG_RELAY)
            from .net.worker_relay import WorkerBridge

            settings.code = code
            settings.is_host = True
            settings.state = "hosting"
            settings.role = "owner"
            settings.last_error = ""
            bridge = WorkerBridge("host", relay_url, room_id(code), relay_token)
            relay = ("127.0.0.1", bridge.start())
            port = 0
        else:
            host = settings.relay_host.strip()
            if not host:
                raise ValueError("Relay host is not set")
            try:
                import ipaddress

                ipaddress.IPv4Address(host)
            except ValueError as exc:
                raise ValueError("Relay host must be an IPv4 address") from exc
            relay = (host, settings.relay_port)
            code = encode_room(host, settings.relay_port, flags | FLAG_RELAY)
            port = 0
    else:
        port = port or settings.port
        if not code:
            code = encode_room(_local_ip(), port, flags)
    settings.code = code
    settings.is_host = True
    settings.state = "hosting"
    settings.role = "owner"
    settings.show_cameras = True
    settings.show_selections = True
    settings.show_labels = True
    settings.last_error = ""
    runtime["session_started"] = time.monotonic()
    runtime["bootstrapping"] = False
    runtime["bootstraps"] = {}
    name = settings.name or f"user-{uuid.uuid4().hex[:4]}"
    settings.name = name
    print(f"BMP host code={code} relay={relay_url or relay}", flush=True)
    try:
        session = SessionService(
            inbox=inbox,
            host=True,
            code=code,
            name=name,
            color=tuple(settings.color),
            port=port,
            pin=settings.pin,
            relay=relay,
        )
        session.start()
    except Exception:
        if bridge is not None:
            bridge.stop()
        raise
    runtime["relay_bridge"] = bridge
    runtime["session"] = session
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    _prime_objects(context)
    runtime["last_selection"] = 0.0
    _sync_selection(context)
    _upsert_user(context, name, tuple(settings.color), host=True)
    _push_message(context, "system", f"Hosting room {code}")
    _ensure_timer()


def start_join(context, code: str, relay_url: str | None = None) -> None:
    settings = context.window_manager.bmp
    stop_session(context, publish=False)
    inbox = queue.Queue()
    runtime["inbox"] = inbox
    name = settings.name or f"user-{uuid.uuid4().hex[:4]}"
    settings.name = name
    settings.code = code.strip().upper()
    settings.is_host = False
    settings.state = "joining"
    settings.role = "editor"
    settings.show_cameras = True
    settings.show_selections = True
    settings.show_labels = True
    settings.last_error = ""
    runtime["session_started"] = time.monotonic()
    runtime["bootstrapping"] = True
    runtime["bootstrap_deadline"] = time.monotonic() + 20.0
    runtime["bootstraps"] = {}
    bridge = None
    relay: tuple[str, int] | None = None
    print(f"BMP join code={settings.code} relay={relay_url}", flush=True)
    try:
        if relay_url:
            from .core.roomcode import room_id
            from .net.worker_relay import WorkerBridge

            bridge = WorkerBridge("join", relay_url, room_id(settings.code))
            relay = ("127.0.0.1", bridge.start())
        session = SessionService(
            inbox=inbox,
            host=False,
            code=settings.code,
            name=name,
            color=tuple(settings.color),
            port=settings.port,
            fingerprint=settings.fingerprint or None,
            pin=settings.pin,
            relay=relay,
        )
        session.start()
    except Exception:
        if bridge is not None:
            bridge.stop()
        raise
    runtime["relay_bridge"] = bridge
    runtime["session"] = session
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    _prime_objects(context)
    _upsert_user(context, name, tuple(settings.color), host=False)
    _push_message(context, "system", f"Joining room {settings.code}")
    _ensure_timer()


def _ensure_timer() -> None:
    # register() already owns the timer. Registering it again from a session
    # start raised "already registered" and left the start half-finished.
    try:
        if not bpy.app.timers.is_registered(bmp_tick):
            bpy.app.timers.register(bmp_tick, first_interval=0.05, persistent=True)
    except (ValueError, AttributeError):
        bpy.app.timers.register(bmp_tick, first_interval=0.05, persistent=True)



def stop_session(context, publish: bool = True) -> None:
    session = runtime.get("session")
    if session:
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    runtime["session"] = None
    bridge = runtime.pop("relay_bridge", None)
    if bridge is not None:
        try:
            bridge.stop()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    runtime["mirrors"].clear()
    runtime["edit_mirrors"].clear()
    runtime["object_shadow"].clear()
    runtime["lease_owners"].clear()
    runtime["owned_leases"].clear()
    runtime["lease_requests"].clear()
    runtime["lease_request"] = None
    runtime["local_transform"].clear()
    runtime["transform_smoothers"].clear()
    runtime["mesh_smoothers"].clear()
    runtime["view_smoothers"].clear()
    runtime["remote_debug"] = {}
    runtime["remote_selections"].clear()
    runtime["remote_points"] = []
    runtime["selection_digest"].clear()
    runtime["last_edit_sel"] = 0.0
    runtime["selection"].clear()
    runtime["hidden_objects"].clear()
    runtime["deselected"].clear()
    runtime["last_selection"] = 0.0
    runtime["lease_renew"] = 0.0
    runtime["bootstraps"].clear()
    runtime["bootstrapping"] = False
    runtime["bootstrap_deadline"] = 0.0
    runtime["edit_lease_uid"] = None
    runtime["last_edit"] = 0.0
    settings = context.window_manager.bmp
    settings.rtt_ms = 0
    settings.users.clear()
    if runtime["inbox"] is not None:
        while not runtime["inbox"].empty():
            try:
                runtime["inbox"].get_nowait()
            except queue.Empty:
                break
    if not publish:
        # A new session is about to start in the same control tick. Writing
        # "offline" here made the desktop app detach Blender and remove the
        # public listing for a moment every time a session began.
        return
    settings.state = "offline"
    settings.is_host = False
    # Publish the offline state immediately; otherwise the app keeps showing
    # the last connected status until another session starts.
    runtime["last_status"] = 0.0
    try:
        _write_status(context, force=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


# --------------------------------------------------------------------------- inbox
def _handle_event(context, kind: str, body: dict) -> None:
    settings = context.window_manager.bmp
    if kind == "state":
        value = body.get("state")
        if value in {"offline", "hosting", "joining", "connected"}:
            settings.state = value
    elif kind == "host_ready":
        settings.code = body.get("code", settings.code)
        settings.direct_code = body.get("direct_code", "")
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
        if body.get("you"):
            settings.name = body["you"]
        runtime["bootstrapping"] = True
        runtime["bootstrap_deadline"] = time.monotonic() + 20.0
        leases = body.get("leases")
        if isinstance(leases, dict):
            runtime["lease_owners"] = {
                key: info["owner"]
                for key, info in leases.items()
                if isinstance(key, str) and isinstance(info, dict) and isinstance(info.get("owner"), str)
            }
        _push_message(context, "system", "Connected - loading host scene")
        _upsert_user(context, body.get("name", "host"), body.get("color", (1, 1, 1, 1)), host=True)
        for peer in body.get("peers", []):
            _upsert_user(context, peer.get("name", "?"), peer.get("color", (1, 1, 1, 1)))
        # The host now streams its scene; the client does not upload its own.
    elif kind == "relay":
        settings.last_error = ""
        _push_message(context, "system", body.get("message", "Relay connected"))
    elif kind == "relay_error":
        settings.last_error = str(body.get("message", "relay error"))[:180]
    elif kind == "peer_join":
        name = body.get("name", "?")
        _upsert_user(context, name, body.get("color", (1, 1, 1, 1)))
        _push_message(context, "system", f"{name} joined")
        if settings.is_host:
            _queue_scene_bootstrap(context, name)
    elif kind == "scene_reset":
        target = body.get("target")
        if target and target != settings.name:
            return
        _clear_scene(context, body)
        runtime["bootstrapping"] = True
        runtime["bootstrap_deadline"] = time.monotonic() + 30.0
        _push_message(context, "system", "Replacing local scene with the host's")
    elif kind == "scene_ready":
        target = body.get("target")
        if target and target != settings.name:
            return
        _finish_scene_bootstrap(context, body)
    elif kind == "peer_leave":
        _remove_user(context, body.get("name", "?"))
        _push_message(context, "system", f"{body.get('name', 'someone')} left")
    elif kind == "role":
        name = str(body.get("name", ""))[:48]
        role = _role_name(body.get("role"))
        for user in settings.users:
            if user.name == name:
                user.role = role
                break
        if name == settings.name:
            settings.role = role
        if name:
            _push_message(context, "system", f"{name} is now {role}")
    elif kind == "kicked":
        settings.last_error = str(body.get("message", "Kicked by host"))[:180]
        _push_message(context, "system", settings.last_error)
    elif kind == "presence":
        role = _role_name(body.get("role")) if "role" in body else None
        _upsert_user(context, body.get("name", "?"), body.get("color", (1, 1, 1, 1)), role=role)
        for user in settings.users:
            if user.name == body.get("name"):
                user.scene = body.get("scene", "")
                user.frame = int(body.get("frame", 0))
                user.mode = body.get("mode", "")
                user.active = str(body.get("active", ""))[:64]
                if role:
                    user.role = role
                    if body.get("name") == settings.name:
                        settings.role = role
                view = body.get("view")
                if isinstance(view, list) and len(view) == 16:
                    try:
                        values = [float(value) for value in view]
                    except (TypeError, ValueError):
                        values = []
                    if len(values) == 16:
                        user.view = values
                        user.has_view = True
                    else:
                        user.has_view = False
                else:
                    user.has_view = False
                aspect = body.get("aspect")
                if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
                    user.view_aspect = max(0.1, min(10.0, float(aspect)))
                user.view_persp = str(body.get("persp", ""))[:16]
                distance = body.get("dist")
                if isinstance(distance, (int, float)) and not isinstance(distance, bool):
                    user.view_distance = max(0.05, min(100000.0, float(distance)))
                cursor = body.get("cursor")
                if isinstance(cursor, list) and len(cursor) == 3:
                    try:
                        values = [float(value) for value in cursor]
                    except (TypeError, ValueError):
                        values = []
                    if len(values) == 3:
                        user.cursor = values
                        user.has_cursor = True
                    else:
                        user.has_cursor = False
                else:
                    user.has_cursor = False
                user.selected = str(body.get("selected", ""))[:512]
                ping_value = body.get("ping_ms")
                if isinstance(ping_value, (int, float)) and not isinstance(ping_value, bool):
                    user.ping_ms = max(0, min(5000, int(round(float(ping_value)))))
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
        leases = body.get("leases")
        if isinstance(leases, dict):
            owners = {}
            requests = {}
            for key, info in leases.items():
                if not isinstance(key, str) or not isinstance(info, dict):
                    continue
                lease_owner = info.get("owner")
                if isinstance(lease_owner, str):
                    owners[key] = lease_owner
                names = info.get("requests")
                if isinstance(names, list):
                    requests[key] = [str(name)[:48] for name in names if isinstance(name, str)][:8]
            runtime["lease_owners"] = owners
            runtime["lease_requests"] = requests
        key = body.get("key")
        owner = body.get("owner")
        action = body.get("action")
        if (
            action not in {"release", "released", "expiring", "denied_request"}
            and isinstance(key, str)
            and isinstance(owner, str)
        ):
            runtime["lease_owners"][key] = owner
        runtime["owned_leases"] = {
            item
            for item, lease_owner in runtime["lease_owners"].items()
            if lease_owner == settings.name
        }
        pending = None
        for lease_key, names in runtime["lease_requests"].items():
            if names and runtime["lease_owners"].get(lease_key) == settings.name:
                pending = (lease_key, names[0])
                break
        runtime["lease_request"] = (
            {"key": pending[0], "requester": pending[1]} if pending else None
        )
        settings.lease_requester = pending[1] if pending else ""
        if pending:
            obj = obj_sync.find(pending[0])
            settings.lease_info = f"{pending[1]} wants {obj.name if obj else 'an object'}"
        elif action == "expiring":
            settings.lease_info = "Edit lock expiring - edit the object to renew"
        else:
            settings.lease_info = ""
        if action in {"denied", "held"}:
            _push_message(context, "system", f"Locked by {body.get('owner', 'someone')}")
        elif action == "requested":
            _push_message(context, "system", f"{body.get('requester', 'someone')} requested edit")
        elif action == "grant":
            _push_message(context, "system", f"Lease granted to {body.get('owner', 'someone')}")
        elif action == "denied_request":
            _push_message(context, "system", f"{body.get('owner', 'owner')} denied the edit request")
        elif action == "expiring":
            keys = body.get("keys")
            if not isinstance(keys, list):
                keys = [key] if isinstance(key, str) else []
            mine = [item for item in keys if runtime["lease_owners"].get(item) == settings.name]
            if mine:
                _push_message(context, "system", "Your edit lock is expiring - edit the object to renew")
                session = runtime.get("session")
                if session is not None:
                    for item in mine:
                        _lease_request(session, item, "acquire")
    elif kind == "laser":
        name = str(body.get("name", ""))[:48]
        points = body.get("points")
        if name and isinstance(points, list):
            clean = []
            for item in points[:64]:
                if isinstance(item, (list, tuple)) and len(item) == 3:
                    try:
                        clean.append([float(item[0]), float(item[1]), float(item[2])])
                    except (TypeError, ValueError):
                        continue
            if clean:
                runtime["lasers"][name] = {"points": clean, "stamp": time.monotonic()}
    elif kind == "selection":
        name = str(body.get("name", ""))[:48]
        uid = body.get("uuid")
        count = body.get("count")
        mask = body.get("mask")
        if name and isinstance(uid, str) and isinstance(count, int) and isinstance(mask, (bytes, bytearray)):
            if len(mask) != (count + 7) // 8:
                return
            entry = runtime["remote_selections"].setdefault(name, {})
            if count == 0:
                entry.pop(uid, None)
            else:
                indices = []
                for index in range(min(count, 200_000)):
                    if mask[index >> 3] & (1 << (index & 7)):
                        indices.append(index)
                        if len(indices) >= 3000:
                            break
                entry[uid] = {
                    "mask": bytes(mask),
                    "count": count,
                    "kind": int(body.get("kind", 0)),
                    "rev": int(body.get("rev", 0)),
                    "indices": indices,
                }
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
        uid = body.get("uuid")
        values = obj_sync.normalize(body.get("matrix", []))
        if values is None or _should_ignore_remote_transform(context, uid):
            return
        obj = obj_sync.find(uid)
        if obj is None:
            return
        if not obj_sync.apply(uid, values):
            return
        # Exact application: no display interpolation. Remember the applied
        # matrix so the next local sync does not echo it back.
        runtime["object_shadow"][uid] = tuple(round(float(value), 6) for value in values)
    elif kind == "mesh_delta":
        _apply_mesh_delta(context, body)
    elif kind == "mesh_snapshot":
        _apply_mesh_snapshot(context, body)
    elif kind == "error":
        settings.last_error = str(body.get("message", "unknown error"))
    elif kind == "disconnected":
        settings.state = "offline"
        _push_message(context, "system", f"Disconnected: {body.get('reason', 'unknown')}")


def _should_ignore_remote_transform(context, uid: str) -> bool:
    """Do not move an object while the local user is moving/editing it."""
    now = time.monotonic()
    if now - runtime["local_transform"].get(uid, 0.0) < 0.35:
        return True
    obj = obj_sync.find(uid)
    if obj is not None and context.active_object is obj and context.mode in {"EDIT_MESH", "SCULPT"}:
        return True
    window = getattr(context, "window", None)
    if window is not None:
        try:
            if window.modal_operators:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _apply_mesh_delta(context, body: dict) -> None:
    uid = body.get("uuid")
    payload = body.get("payload", b"")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_MESH:
        return
    obj = obj_sync.find(uid)
    if obj is None or obj.type != "MESH":
        return
    if context.active_object == obj and context.mode == "EDIT_MESH":
        _apply_edit_delta(context, obj, uid, bytes(payload))
        return
    if context.active_object == obj and context.mode == "SCULPT":
        return  # sculpt data is not a BMesh; leave the local sculpt alone
    try:
        rev, indices, values = chunker.decode_delta(bytes(payload))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    if indices.size == 0:
        return
    mirror = runtime["mirrors"].get(uid)
    if mirror is None or not mirror.matches(obj.data):
        return
    try:
        mirror.apply_delta(obj.data, bytes(payload))
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _apply_edit_delta(context, obj, uid: str, payload: bytes) -> None:
    """Apply a vertex delta to an object that is currently in Edit Mode."""
    try:
        import bmesh
    except ImportError:  # pragma: no cover - Blender always has bmesh
        return
    try:
        bm = bmesh.from_edit_mesh(obj.data)
    except Exception:  # noqa: BLE001
        return
    mirror = runtime["edit_mirrors"].get(uid)
    if mirror is None:
        mirror = EditMirror()
        mirror.remember(bm)
        runtime["edit_mirrors"][uid] = mirror
    if not mirror.matches(bm):
        # Topology changed remotely. Rebuilding a live edit mesh is unsafe;
        # the editor's final snapshot applies once this user leaves Edit Mode.
        return
    try:
        mirror.apply_delta(bm, payload)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
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
    runtime["mesh_smoothers"].pop(uid, None)
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
    if session is None or settings.paused or not settings.sync_materials or runtime.get("bootstrapping"):
        return
    now = time.monotonic()
    if now - runtime["last_material"] < 0.25:
        return
    runtime["last_material"] = now
    candidates = [
        item
        for item in context.scene.objects
        if item.get("bmp_uuid") and getattr(item, "material_slots", None)
    ]
    if not candidates:
        return
    cursor = runtime.get("material_cursor", 0) % len(candidates)
    runtime["material_cursor"] = cursor + 4
    for obj in candidates[cursor : cursor + 4]:
        uid = obj.get("bmp_uuid")
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        for index, slot in enumerate(obj.material_slots[:16]):
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
    if session is None or settings.paused or not settings.sync_modifiers or runtime.get("bootstrapping"):
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
    owner = runtime["lease_owners"].get(uid)
    if owner is not None and owner != settings.name:
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
    # Re-enabling the addon (or a second module copy) must not replay the last
    # host/join request: doing that restarts the relay tunnel and kicks every
    # connected player.
    if nonce == control.read_ack():
        runtime["last_control_nonce"] = nonce
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
            start_host(
                context,
                request.get("code") or None,
                request.get("port"),
                request.get("relay_url"),
                request.get("relay_token"),
            )
        elif action == "join":
            code = request.get("code", "")
            if code:
                start_join(context, code, request.get("relay_url"))
        elif action == "chat":
            text = request.get("text", "")
            session = runtime.get("session")
            if session is None or not text:
                settings.last_error = "Not connected"
            else:
                session.send_chat(text)
                _push_message(context, settings.name, text)
        elif action == "kick":
            target = request.get("target")
            session = runtime.get("session")
            if session is None or not settings.is_host:
                settings.last_error = "Only the host can kick players"
            elif target:
                session.kick(target)
        elif action == "role":
            target = request.get("target")
            role = request.get("role")
            session = runtime.get("session")
            if session is None or not settings.is_host:
                settings.last_error = "Only the host can change roles"
            elif target and role:
                session.set_role(target, ROLE_VALUES[role])
        elif action == "leave":
            settings.last_error = ""
            stop_session(context)
    except Exception as exc:  # noqa: BLE001
        settings.last_error = str(exc)
        traceback.print_exc()
        if action in {"host", "join"}:
            try:
                stop_session(context)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    try:
        control.write_ack(nonce)
    except Exception:  # noqa: BLE001
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
        if obj.type == "MESH":
            runtime["tracked"].add(obj.name)
    runtime["known_objects"] = known


def _parent_depth(obj) -> int:
    depth = 0
    seen = 0
    while obj is not None and obj.parent is not None and seen < 64:
        depth += 1
        obj = obj.parent
        seen += 1
    return depth


def _send_object_details(session: SessionService, obj) -> None:
    uid = obj.get("bmp_uuid")
    if not uid:
        return
    for index, slot in enumerate(getattr(obj, "material_slots", []) or []):
        material = slot.material
        if material is None:
            continue
        data = material_values(material)
        session.send_material(uid, index, data)
        runtime["material_shadow"][(uid, index)] = _material_signature(data)
    if hasattr(obj, "modifiers"):
        snapshot = modifier_snapshot(obj)
        session.send_modifier_stack(uid, snapshot)
        runtime["modifier_shadow"][uid] = _modifier_signature(snapshot)


def _world_values(world):
    if world is None or not getattr(world, "use_nodes", False) or world.node_tree is None:
        return None
    for node in world.node_tree.nodes:
        if node.type != "BACKGROUND":
            continue
        try:
            color = [float(v) for v in node.inputs["Color"].default_value][:3]
            strength = float(node.inputs["Strength"].default_value)
        except (KeyError, TypeError, ValueError):
            return None
        return {"color": color, "strength": max(0.0, min(1000.0, strength))}
    return None


def _collection_snapshot(context) -> list:
    master = context.scene.collection.name
    result = []
    for collection in bpy.data.collections:
        if collection.name == master:
            continue
        parent = ""
        for other in bpy.data.collections:
            if other == collection:
                continue
            if other.children.get(collection.name) is not None:
                parent = other.name
                break
        result.append({"name": collection.name[:64], "parent": parent[:64]})
        if len(result) >= 256:
            break
    return result


def _render_snapshot(context) -> dict:
    render = context.scene.render
    return {
        "engine": str(render.engine)[:32],
        "resolution_x": int(render.resolution_x),
        "resolution_y": int(render.resolution_y),
        "resolution_percentage": int(render.resolution_percentage),
        "fps": int(render.fps),
    }


def _queue_scene_bootstrap(context, target: str) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None or not session.is_host:
        return
    objects = [obj for obj in context.scene.objects if not obj.get("bmp_ping")]
    objects.sort(key=_parent_depth)
    camera = context.scene.camera
    collections = _collection_snapshot(context)
    runtime["bootstraps"][target] = {
        "objects": [obj.name for obj in objects],
        "camera": camera.get("bmp_uuid") if camera is not None else None,
        "world": _world_values(context.scene.world),
        "collections": collections,
        "render": _render_snapshot(context),
    }
    session.send_scene_reset(target, {"collections": collections})
    _push_message(context, "system", f"Sending scene to {target}")


def _pump_scene_bootstrap(context) -> None:
    session: SessionService | None = runtime.get("session")
    bootstraps = runtime.get("bootstraps")
    if session is None or not session.is_host or not bootstraps:
        return
    for target in list(bootstraps):
        entry = bootstraps.get(target)
        if not isinstance(entry, dict):
            continue
        queue = entry.get("objects") or []
        sent = 0
        while queue and sent < 2:
            name = queue.pop(0)
            obj = bpy.data.objects.get(name)
            if obj is None or obj.get("bmp_ping"):
                continue
            _send_object_create(session, obj)
            _send_object_details(session, obj)
            if obj.type == "MESH":
                runtime["tracked"].add(obj.name)
            sent += 1
        if not queue:
            session.send_scene_ready(
                target,
                {
                    "camera": entry.get("camera"),
                    "world": entry.get("world"),
                    "render": entry.get("render"),
                },
            )
            del bootstraps[target]


def _clear_scene(context, body: dict | None = None) -> None:
    try:
        if context.mode != "OBJECT" and context.active_object is not None:
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:  # noqa: BLE001
        pass
    for obj in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:  # noqa: BLE001
            pass
    master = context.scene.collection
    for collection in list(bpy.data.collections):
        if collection == master:
            continue
        try:
            bpy.data.collections.remove(collection)
        except Exception:  # noqa: BLE001
            pass
    try:
        bpy.data.orphans_purge(do_recursive=True)
    except TypeError:
        try:
            bpy.data.orphans_purge()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    entries = (body or {}).get("collections")
    if isinstance(entries, list):
        for item in entries[:256]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name and name not in bpy.data.collections:
                try:
                    bpy.data.collections.new(name[:64])
                except Exception:  # noqa: BLE001
                    pass
        for item in entries[:256]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or name not in bpy.data.collections:
                continue
            parent = item.get("parent")
            target = bpy.data.collections.get(parent) if isinstance(parent, str) and parent else master
            collection = bpy.data.collections.get(name)
            if collection is not None and target is not None and target.children.get(collection.name) is None:
                try:
                    target.children.link(collection)
                except Exception:  # noqa: BLE001
                    pass
    runtime["mirrors"].clear()
    runtime["edit_mirrors"].clear()
    runtime["object_shadow"].clear()
    runtime["material_shadow"].clear()
    runtime["modifier_shadow"].clear()
    runtime["tracked"].clear()
    runtime["known_objects"].clear()
    runtime["edit_lease_uid"] = None
    runtime["selection"].clear()
    runtime["hidden_objects"].clear()
    runtime["deselected"].clear()
    runtime["owned_leases"].clear()
    runtime["lease_requests"].clear()
    runtime["lease_request"] = None
    runtime["transform_smoothers"].clear()
    runtime["mesh_smoothers"].clear()
    runtime["view_smoothers"].clear()
    runtime["remote_selections"].clear()
    runtime["remote_points"] = []
    runtime["selection_digest"].clear()
    runtime["last_selection"] = 0.0
    runtime["lease_renew"] = 0.0


def _finish_scene_bootstrap(context, body: dict | None = None) -> None:
    runtime["bootstrapping"] = False
    runtime["bootstrap_deadline"] = 0.0
    runtime["mirrors"].clear()
    runtime["edit_mirrors"].clear()
    runtime["object_shadow"].clear()
    runtime["material_shadow"].clear()
    runtime["modifier_shadow"].clear()
    runtime["tracked"].clear()
    runtime["known_objects"].clear()
    for obj in context.scene.objects:
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        runtime["known_objects"].add(uid)
        runtime["object_shadow"][uid] = tuple(round(float(v), 6) for row in obj.matrix_world for v in row)
        if obj.type == "MESH":
            mirror = MeshMirror()
            mirror.remember(obj.data)
            runtime["mirrors"][uid] = mirror
            runtime["tracked"].add(obj.name)
        for index, slot in enumerate(getattr(obj, "material_slots", []) or []):
            if slot.material:
                data = material_values(slot.material)
                runtime["material_shadow"][(uid, index)] = _material_signature(data)
        if hasattr(obj, "modifiers"):
            snapshot = modifier_snapshot(obj)
            runtime["modifier_shadow"][uid] = _modifier_signature(snapshot)
    if isinstance(body, dict):
        camera = body.get("camera")
        if isinstance(camera, str):
            camera_obj = obj_sync.find(camera)
            if camera_obj is not None:
                context.scene.camera = camera_obj
        world_values = body.get("world")
        world = context.scene.world
        if isinstance(world_values, dict) and world is not None:
            world.use_nodes = True
            tree = world.node_tree
            if tree is not None:
                for node in tree.nodes:
                    if node.type != "BACKGROUND":
                        continue
                    color = world_values.get("color")
                    if isinstance(color, list) and len(color) == 3:
                        node.inputs["Color"].default_value = tuple(float(v) for v in color) + (1.0,)
                    strength = world_values.get("strength")
                    if isinstance(strength, (int, float)):
                        node.inputs["Strength"].default_value = float(strength)
                    break
        render = body.get("render")
        if isinstance(render, dict):
            scene = context.scene
            engine = render.get("engine")
            if engine in {"BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH"}:
                try:
                    scene.render.engine = engine
                except Exception:  # noqa: BLE001
                    pass
            limits = {
                "resolution_x": (1, 16384),
                "resolution_y": (1, 16384),
                "resolution_percentage": (1, 1000),
                "fps": (1, 240),
            }
            for key, (low, high) in limits.items():
                value = render.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    try:
                        setattr(scene.render, key, max(low, min(high, int(value))))
                    except Exception:  # noqa: BLE001
                        pass
    settings = context.window_manager.bmp
    blocked = {
        uid
        for uid, owner in runtime["lease_owners"].items()
        if owner and owner != settings.name
    }
    hidden = runtime["hidden_objects"]
    for uid in blocked:
        obj = obj_sync.find(uid)
        if obj is not None:
            try:
                obj.hide_select = True
                hidden.add(uid)
            except Exception:  # noqa: BLE001
                pass
    runtime["owned_leases"] = {
        uid
        for uid, owner in runtime["lease_owners"].items()
        if owner == settings.name
    }
    _push_message(context, "system", "Host scene loaded")


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
    collections = [collection for collection in getattr(obj, "users_collection", []) if collection != bpy.context.scene.collection]
    if collections:
        meta["collection"] = collections[0].name[:64]
    materials = []
    for index, slot in enumerate(getattr(obj, "material_slots", [])[:16]):
        if slot.material is not None:
            data = material_values(slot.material)
            data["slot"] = index
            materials.append(data)
    if materials:
        meta["materials"] = materials
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
        sent = session.send_object_create(uid, kind, meta, geometry)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sent = False
    if not sent:
        if uid not in runtime["create_errors"]:
            runtime["create_errors"].add(uid)
            _push_message(
                context,
                "system",
                f"{obj.name[:40]} is too large to sync; reduce its geometry and re-import",
            )
    else:
        runtime["create_errors"].discard(uid)
    if kind == object_payload.KIND_MESH:
        runtime["tracked"].add(obj.name)


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
            geometry = body.get("geometry", b"")
            if geometry:
                mirror.apply_snapshot(mesh, geometry)
            else:
                mirror.remember(mesh)
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
    if obj.type == "MESH":
        runtime["tracked"].add(obj.name)
    obj_sync.apply(uid, meta.get("matrix", []))
    parent_uid = meta.get("parent")
    if parent_uid:
        parent = obj_sync.find(parent_uid)
        if parent is not None:
            obj.parent = parent
            obj_sync.apply(uid, meta.get("matrix", []))
    materials = meta.get("materials")
    if isinstance(materials, list):
        for item in materials[:16]:
            if not isinstance(item, dict):
                continue
            slot = item.get("slot")
            if isinstance(slot, int) and 0 <= slot <= 255:
                _apply_material(context, {"uuid": uid, "slot": slot, "material": item})


def _apply_object_delete(context, body: dict) -> None:
    uid = body.get("uuid")
    if not uid:
        return
    obj = obj_sync.find(uid)
    runtime["known_objects"].discard(uid)
    runtime["mirrors"].pop(uid, None)
    runtime["edit_mirrors"].pop(uid, None)
    runtime["object_shadow"].pop(uid, None)
    runtime["lease_owners"].pop(uid, None)
    runtime["transform_smoothers"].pop(uid, None)
    runtime["mesh_smoothers"].pop(uid, None)
    if obj is not None:
        runtime["tracked"].discard(obj.name)
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


def _lease_request(session: SessionService, uid: str, action: str) -> bool:
    if session.is_host:
        return session.local_lease(uid, action)
    session.send_lease(uid, action)
    return True


def _sync_selection(context) -> None:
    """Claim selected objects so only one person can select/edit them."""
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or runtime.get("bootstrapping"):
        return
    now = time.monotonic()
    if now - runtime["last_selection"] < SELECTION_POLL:
        return
    runtime["last_selection"] = now
    owners = runtime["lease_owners"]
    selected = {
        uid
        for obj in context.selected_objects
        if (uid := obj.get("bmp_uuid"))
    }
    previous = runtime["selection"]
    owned = runtime["owned_leases"]

    # Any lease we own that is no longer selected must be released. This also
    # covers a lease that was granted after the object was already deselected.
    for uid in owned - selected:
        runtime["deselected"].setdefault(uid, now)
    for uid in selected:
        runtime["deselected"].pop(uid, None)
    for uid, stamp in list(runtime["deselected"].items()):
        if now - stamp < 0.2:
            continue
        if uid in owned or owners.get(uid) == settings.name:
            _lease_request(session, uid, "release")
            owned.discard(uid)
            owners.pop(uid, None)
        runtime["deselected"].pop(uid, None)

    for uid in selected - previous:
        owner = owners.get(uid)
        if owner is None or owner == settings.name:
            granted = _lease_request(session, uid, "acquire")
            if granted and session.is_host:
                owners[uid] = settings.name
                owned.add(uid)
            owner = owners.get(uid)
        if owner not in (None, settings.name):
            obj = obj_sync.find(uid)
            if obj is not None:
                try:
                    obj.select_set(False)
                except Exception:  # noqa: BLE001
                    pass
                _push_message(context, "system", f"{obj.name} is being edited by {owner}")

    if now - runtime["lease_renew"] > LEASE_RENEW:
        runtime["lease_renew"] = now
        for uid in selected:
            if owners.get(uid) == settings.name:
                _lease_request(session, uid, "acquire")

    blocked = {uid for uid, owner in owners.items() if owner and owner != settings.name}
    hidden = runtime["hidden_objects"]
    for uid in list(hidden - blocked):
        obj = obj_sync.find(uid)
        if obj is not None:
            try:
                obj.hide_select = False
            except Exception:  # noqa: BLE001
                pass
        hidden.discard(uid)
    for uid in blocked:
        obj = obj_sync.find(uid)
        if obj is not None and not obj.hide_select:
            try:
                obj.hide_select = True
                hidden.add(uid)
            except Exception:  # noqa: BLE001
                pass

    runtime["selection"] = selected


def _sync_transforms(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_transforms or settings.paused or runtime.get("bootstrapping"):
        return
    now = time.monotonic()
    if now - runtime["last_transform"] < runtime["transform_interval"]:
        return
    runtime["last_transform"] = now
    known = runtime["known_objects"]
    current = set()
    assigned = set()
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        # Imported/duplicated objects can carry a stale or copied bmp_uuid.
        # If it is not a UID we have already announced this session, send a
        # create; duplicated UIDs get a fresh identity.
        if not uid or uid in assigned:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
        if uid not in known:
            _send_object_create(session, obj)
        assigned.add(uid)
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
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        if uid in runtime["transform_smoothers"]:
            continue
        matrix = obj.matrix_world
        current = tuple(round(v, 6) for row in matrix for v in row)
        previous = shadow.get(uid)
        if previous is None:
            shadow[uid] = current
            continue
        if current != previous:
            shadow[uid] = current
            runtime["transform_smoothers"].pop(uid, None)
            runtime["local_transform"][uid] = time.monotonic()
            session.send_transform(uid, current)
    for stale in list(shadow.keys() - seen):
        del shadow[stale]


def _sync_edit_topology(context, session: SessionService, obj, bm) -> None:
    """Debounced full snapshots when Edit Mode topology changes."""
    now = time.monotonic()
    if now - runtime["last_topology"] < TOPOLOGY_POLL:
        return
    runtime["last_topology"] = now
    uid = obj.get("bmp_uuid")
    if not uid:
        return
    count = len(bm.edges)
    sample = []
    if count:
        for index in list(range(min(128, count))) + list(range(max(0, count - 128), count)):
            edge = bm.edges[index]
            sample.append((edge.verts[0].index, edge.verts[1].index))
    signature = (len(bm.verts), count, len(bm.faces), hash(tuple(sample)))
    if signature != runtime["topology_sig"].get(uid):
        runtime["topology_sig"][uid] = signature
        runtime["topology_due"][uid] = now + TOPOLOGY_DEBOUNCE
    due = runtime["topology_due"].get(uid, 0.0)
    if not due or now < due:
        return
    runtime["topology_due"].pop(uid, None)
    try:
        payload = snapshot_bmesh(bm)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    if not payload:
        return
    if len(payload) > MAX_MESH:
        _push_message(context, "system", f"Topology for {obj.name[:40]} is too large to sync")
        return
    session.send_mesh(obj, "snapshot", payload, 0)
    # The position mirror no longer matches the new topology.
    runtime["edit_mirrors"].pop(uid, None)
    runtime["mirrors"].pop(uid, None)
    _push_message(context, "system", f"Topology synced for {obj.name[:40]}")


def _sync_edit_mesh(context, session: SessionService, obj) -> None:
    """Stream vertex moves while an object is in Edit Mode.

    Blender keeps edit-mode positions in a BMesh, so ``obj.data.vertices`` is
    stale until the user leaves the mode. This reads the BMesh and sends only
    changed vertices, at up to 30 Hz.
    """
    settings = context.window_manager.bmp
    if not settings.sync_mesh or settings.paused:
        return
    now = time.monotonic()
    count = len(obj.data.vertices)
    interval = EDIT_POLL if count <= 20000 else (1.0 / 15.0 if count <= 80000 else 1.0 / 10.0)
    if now - runtime["last_edit"] < interval:
        return
    runtime["last_edit"] = now

    uid = obj.get("bmp_uuid") or str(uuid.uuid4())
    obj["bmp_uuid"] = uid
    runtime["tracked"].add(obj.name)
    _flush_mesh_smoother(uid)
    owner = runtime["lease_owners"].get(uid)
    if owner is not None and owner != settings.name:
        if runtime.get("edit_lease_uid") != uid:
            runtime["edit_lease_uid"] = uid
            _push_message(context, "system", f"Live edit locked by {owner}")
        return
    if runtime.get("edit_lease_uid") != uid:
        runtime["edit_lease_uid"] = uid
        _lease_request(session, uid, "acquire")
        if session.is_host:
            runtime["lease_owners"][uid] = settings.name
            runtime["owned_leases"].add(uid)
    try:
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
    except Exception:  # noqa: BLE001
        return
    _sync_edit_topology(context, session, obj, bm)
    mirror = runtime["edit_mirrors"].get(uid)
    if mirror is None or not mirror.matches(bm):
        # Seed from the object's pre-edit mesh, not from the BMesh. If the user
        # entered Edit Mode and moved a vertex before this first tick, the BMesh
        # already contains the new position; obj.data still has the old one.
        mirror = EditMirror()
        try:
            positions = MeshMirror.read_positions(obj.data)
            if positions.shape[0] == len(bm.verts):
                mirror.shadow = positions
                mirror.count = positions.shape[0]
                mirror.hashes = chunker.chunk_hashes(positions)
            else:
                mirror.remember(bm)
        except Exception:  # noqa: BLE001
            mirror.remember(bm)
        runtime["edit_mirrors"][uid] = mirror
    try:
        delta = mirror.poll(bm)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    if delta is not None:
        rev, payload = delta
        session.send_mesh(obj, "delta", payload, rev)


def _sync_mesh(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_mesh or settings.paused or runtime.get("bootstrapping"):
        return
    active = context.active_object
    editing = active is not None and active.type == "MESH" and context.mode == "EDIT_MESH"
    if editing:
        _sync_edit_mesh(context, session, active)
    elif runtime["edit_mirrors"]:
        runtime["edit_mirrors"].clear()
        runtime["edit_lease_uid"] = None

    now = time.monotonic()
    if now - runtime["last_mesh"] < MESH_POLL:
        return
    runtime["last_mesh"] = now

    names = sorted(name for name in runtime["tracked"] if name in bpy.data.objects)
    if editing:
        # The edit fast path already streams this object.
        names = [name for name in names if name != active.name]
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
    if uid in runtime["mesh_smoothers"]:
        return
    owner = runtime["lease_owners"].get(uid)
    if owner is not None and owner != settings.name:
        return

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


def _rigid_view(matrix):
    """Split a view matrix into position + rotation, dropping viewport zoom.

    ``RegionView3D.view_matrix.inverted()`` carries the orthographic zoom (and
    possibly a camera object's scale) in its basis. Drawing that directly made
    remote frustums gigantic when the other user was in an orthographic view,
    which looked like a stray axis line across the scene.
    """
    try:
        location, rotation, _scale = matrix.decompose()
    except Exception:  # noqa: BLE001
        return None
    numbers = [location[0], location[1], location[2], rotation.w, rotation.x, rotation.y, rotation.z]
    if not all(math.isfinite(float(value)) and abs(float(value)) < 1e9 for value in numbers):
        return None
    rotation.normalize()
    return location.copy(), rotation.copy()


def _viewport_view(context):
    from mathutils import Matrix

    screen = getattr(context, "screen", None)
    if screen is None:
        return None
    area = next((item for item in screen.areas if item.type == "VIEW_3D"), None)
    if area is None or area.spaces.active is None:
        return None
    region_3d = area.spaces.active.region_3d
    if region_3d is None:
        return None
    try:
        rigid = _rigid_view(region_3d.view_matrix.inverted())
    except Exception:  # noqa: BLE001
        return None
    if rigid is None:
        return None
    location, rotation = rigid
    matrix = Matrix.Translation(location) @ rotation.to_matrix().to_4x4()
    try:
        distance = float(region_3d.view_distance)
    except Exception:  # noqa: BLE001
        distance = 7.0
    if not math.isfinite(distance) or distance <= 0.0:
        distance = 7.0
    return {
        "matrix": [float(value) for row in matrix for value in row],
        "aspect": float(area.width) / max(1.0, float(area.height)),
        "persp": str(region_3d.view_perspective)[:16],
        "distance": max(0.05, min(100000.0, distance)),
    }


def _selected_uids(context) -> list:
    objects = list(context.selected_objects)
    active = context.active_object
    if active is not None and active not in objects:
        objects.append(active)
    for obj in getattr(context, "objects_in_mode", []):
        if obj not in objects:
            objects.append(obj)
    uids = []
    seen = set()
    for obj in objects:
        uid = obj.get("bmp_uuid")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        uids.append(uid)
    return uids[:10]


def _camera_segments(matrix, aspect: float, distance: float = 7.0) -> list:
    """A camera-shaped wire pyramid that shows both position and gaze.

    The widget is sized from the remote orbit distance so it stays readable at
    any zoom level, and the centre ray makes the look direction obvious.
    """
    from mathutils import Vector

    try:
        orbit = float(distance)
    except (TypeError, ValueError):
        orbit = 7.0
    if not math.isfinite(orbit) or orbit <= 0.0:
        orbit = 7.0
    size = max(0.15, min(0.6, orbit * 0.06))
    forward = size
    height = size * 0.35
    width = height * max(0.6, min(2.6, float(aspect or 1.6)))
    apex = matrix @ Vector((0.0, 0.0, 0.0))
    corners = [
        matrix @ Vector((-width, -height, -forward)),
        matrix @ Vector((width, -height, -forward)),
        matrix @ Vector((width, height, -forward)),
        matrix @ Vector((-width, height, -forward)),
    ]
    segments = [(apex, corner) for corner in corners]
    segments.extend(
        (corners[index], corners[(index + 1) % 4]) for index in range(4)
    )
    return segments


def _object_segments(obj, max_edges: int = 2500) -> list:
    from mathutils import Vector

    matrix = obj.matrix_world
    corners = [matrix @ Vector(corner) for corner in obj.bound_box]
    max_length = 1.0
    if corners:
        spans = [
            max(point[index] for point in corners) - min(point[index] for point in corners)
            for index in range(3)
        ]
        max_length = max(1.0, max(spans) * 2.0)
    if obj.type == "MESH":
        mesh = obj.data
        count = len(mesh.vertices)
        # Only tiny meshes get a true edge outline. Dense imported models look
        # like a wireframe overlay otherwise, so use a clean bounding box.
        if count and 0 < len(mesh.edges) <= 120:
            segments = []
            for edge in mesh.edges:
                first, second = edge.vertices
                if first >= count or second >= count:
                    continue
                start = matrix @ mesh.vertices[first].co
                end = matrix @ mesh.vertices[second].co
                if not all(math.isfinite(float(value)) for value in (*start, *end)):
                    continue
                if (end - start).length > max_length:
                    continue
                segments.append((start, end))
            if segments:
                return segments
    pairs = (
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    )
    return [(corners[first], corners[second]) for first, second in pairs]


def _object_label(obj):
    from mathutils import Vector

    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not corners:
        return obj.matrix_world.translation
    top = max(corners, key=lambda point: point.z)
    return top + Vector((0.0, 0.0, 0.12))


def _build_remote_batches(lines: list) -> list:
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except ImportError:  # pragma: no cover - background Blender
        return []
    shader = runtime.get("remote_shader")
    if shader is None:
        try:
            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        except Exception:  # noqa: BLE001
            try:
                shader = gpu.shader.from_builtin("3D_UNIFORM_COLOR")
            except Exception:  # noqa: BLE001
                return []
        runtime["remote_shader"] = shader
    batches = []
    for color, segments in lines:
        if not segments:
            continue
        coords = []
        for first, second in segments:
            coords.extend((first.x, first.y, first.z, second.x, second.y, second.z))
        try:
            batch = batch_for_shader(shader, "LINES", {"pos": coords})
        except Exception:  # noqa: BLE001
            continue
        batches.append((color, batch))
    return batches


def _build_remote_point_batches(points: list) -> list:
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except ImportError:  # pragma: no cover - background Blender
        return []
    shader = runtime.get("remote_shader")
    if shader is None:
        try:
            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        except Exception:  # noqa: BLE001
            try:
                shader = gpu.shader.from_builtin("3D_UNIFORM_COLOR")
            except Exception:  # noqa: BLE001
                return []
        runtime["remote_shader"] = shader
    batches = []
    for color, coords in points:
        if not coords:
            continue
        flat = [(point.x, point.y, point.z) for point in coords]
        try:
            batch = batch_for_shader(shader, "POINTS", {"pos": flat})
        except Exception:  # noqa: BLE001
            continue
        batches.append((color, batch))
    return batches


def _smooth_view(name: str, values, now: float):
    """Return the remote camera pose exactly; no interpolation."""
    from mathutils import Matrix

    try:
        matrix = Matrix((values[0:4], values[4:8], values[8:12], values[12:16]))
    except Exception:  # noqa: BLE001
        return None
    rigid = _rigid_view(matrix)
    if rigid is None:
        return None
    location, rotation = rigid
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()

def _sync_remote_visuals(context) -> None:
    session: SessionService | None = runtime.get("session")
    now = time.monotonic()
    if now - runtime["last_remote"] < REMOTE_POLL:
        return
    runtime["last_remote"] = now
    if session is None:
        runtime["remote_batches"] = []
        runtime["remote_labels"] = []
        runtime["remote_points"] = []
        runtime["remote_point_batches"] = []
        runtime["view_smoothers"].clear()
        runtime["remote_debug"] = {}
        return
    settings = context.window_manager.bmp
    from mathutils import Vector

    show_cameras = bool(getattr(settings, "show_cameras", True))
    show_selections = bool(getattr(settings, "show_selections", True))

    lines = []
    labels = []
    points = []
    total = 0
    point_total = 0
    max_segment = 0.0
    seen_users = set()
    for user in settings.users:
        if user.name == settings.name:
            continue
        seen_users.add(user.name)
        color = effective_color(user.name, user.color)
        if show_cameras and user.has_view:
            matrix = _smooth_view(user.name, list(user.view), now)
            if matrix is not None:
                segments = _camera_segments(matrix, user.view_aspect, user.view_distance)
                lines.append((color, segments))
                total += len(segments)
                for first, second in segments:
                    max_segment = max(max_segment, float((second - first).length))
                origin = matrix.translation
                widget = max(0.15, min(0.6, float(user.view_distance) * 0.06))
                labels.append(
                    (origin + Vector((0.0, 0.0, widget * 0.5)), user.name, color)
                )
        if show_cameras and user.has_cursor:
            try:
                cursor = Vector((float(user.cursor[0]), float(user.cursor[1]), float(user.cursor[2])))
            except (TypeError, ValueError, IndexError):
                cursor = None
            if cursor is not None:
                size = max(0.05, min(0.6, float(user.view_distance or 7.0) * 0.01))
                lines.append(
                    (
                        color,
                        [
                            (cursor - Vector((size, 0.0, 0.0)), cursor + Vector((size, 0.0, 0.0))),
                            (cursor - Vector((0.0, size, 0.0)), cursor + Vector((0.0, size, 0.0))),
                            (cursor - Vector((0.0, 0.0, size)), cursor + Vector((0.0, 0.0, size))),
                        ],
                    )
                )
                total += 3
        uids = [uid for uid in str(user.selected or "").split(",") if uid]
        for uid in uids[:6]:
            obj = obj_sync.find(uid)
            if obj is None or obj.get("bmp_remote"):
                continue
            segments = _object_segments(obj)
            if not segments:
                continue
            lines.append((color, segments))
            labels.append((_object_label(obj), user.name, color))
            total += len(segments)
            for first, second in segments:
                max_segment = max(max_segment, float((second - first).length))
            if total > 30000:
                break
        if show_selections:
            selections = runtime["remote_selections"].get(user.name) or {}
            for uid, entry in list(selections.items())[:4]:
                obj = obj_sync.find(uid)
                if obj is None or obj.type != "MESH":
                    continue
                count = int(entry.get("count") or 0)
                indices = entry.get("indices") or []
                if count != len(obj.data.vertices) or not indices:
                    continue
                matrix = obj.matrix_world
                selected = [
                    matrix @ obj.data.vertices[index].co
                    for index in indices
                    if 0 <= index < len(obj.data.vertices)
                ]
                if selected:
                    points.append((color, selected))
                    point_total += len(selected)
                if point_total > 20000:
                    break
        if total > 30000 or point_total > 20000:
            break
    for name in list(runtime["view_smoothers"]):
        if name not in seen_users:
            runtime["view_smoothers"].pop(name, None)
    runtime["remote_labels"] = labels[:24]
    runtime["remote_batches"] = _build_remote_batches(lines)
    runtime["remote_points"] = points
    runtime["remote_point_batches"] = _build_remote_point_batches(points)
    runtime["remote_debug"] = {
        "max_segment": round(max_segment, 3),
        "segments": total,
        "points": point_total,
        "labels": len(labels),
        "users": sorted(seen_users),
    }
    try:
        settings.overlay_info = (
            f"{len(labels)} tags, {total} lines, {point_total} points"
        )
    except Exception:  # noqa: BLE001
        pass


def _draw_remote_lines() -> None:
    try:
        import gpu
        shader = runtime.get("remote_shader")
        batches = runtime.get("remote_batches") or []
        point_batches = runtime.get("remote_point_batches") or []
        if shader is None or (not batches and not point_batches):
            return
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("NONE")
        try:
            gpu.state.line_width_set(2.0)
        except Exception:  # noqa: BLE001
            pass
        for color, batch in batches:
            shader.uniform_float("color", color)
            batch.draw(shader)
        try:
            gpu.state.line_width_set(1.0)
        except Exception:  # noqa: BLE001
            pass
        if point_batches:
            try:
                gpu.state.point_size_set(4.0)
            except Exception:  # noqa: BLE001
                pass
            for color, batch in point_batches:
                try:
                    shader.uniform_float("color", color)
                    batch.draw(shader)
                except Exception:  # noqa: BLE001
                    break
            try:
                gpu.state.point_size_set(1.0)
            except Exception:  # noqa: BLE001
                pass
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.blend_set("NONE")
    except Exception:  # noqa: BLE001
        pass


def _draw_remote_labels() -> None:
    try:
        import blf
        from mathutils import Vector
    except ImportError:
        return
    context = bpy.context
    try:
        if not context.window_manager.bmp.show_labels:
            return
    except Exception:  # noqa: BLE001
        pass
    region = getattr(context, "region", None)
    region_data = getattr(context, "region_data", None)
    if region is None or region_data is None:
        return
    labels = runtime.get("remote_labels") or []
    if not labels:
        return
    width = float(region.width)
    height = float(region.height)
    matrix = region_data.perspective_matrix
    font = 0
    lift = 8
    try:
        blf.size(font, 12.0, 72.0)
    except Exception:  # noqa: BLE001
        try:
            blf.size(font, 12)
        except Exception:  # noqa: BLE001
            pass
    shadow_native = False
    try:
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0.0, 0.0, 0.0, 1.0)
        blf.shadow_offset(font, 1, -1)
        shadow_native = True
    except Exception:  # noqa: BLE001
        shadow_native = False
    drawn = []
    for position, name, color in labels:
        clip = matrix @ Vector((position[0], position[1], position[2], 1.0))
        if clip.w <= 0.0001:
            continue
        x = (clip.x / clip.w * 0.5 + 0.5) * width
        y = (clip.y / clip.w * 0.5 + 0.5) * height + lift
        if x < -160 or y < -160 or x > width + 160 or y > height + 160:
            continue
        if any(other == name and abs(x - ox) < 24 and abs(y - oy) < 24 for other, ox, oy in drawn):
            continue
        try:
            text_width, _text_height = blf.dimensions(font, name)
            left = round(x - text_width * 0.5)
            base = round(y)
            if not shadow_native:
                blf.color(font, 0.015, 0.015, 0.02, 0.95)
                blf.position(font, left + 1, base - 1, 0.0)
                blf.draw(font, name)
            blf.color(font, float(color[0]), float(color[1]), float(color[2]), 1.0)
            blf.position(font, left, base, 0.0)
            blf.draw(font, name)
            drawn.append((name, x, y))
        except Exception:  # noqa: BLE001
            pass
    if shadow_native:
        try:
            blf.disable(font, blf.SHADOW)
        except Exception:  # noqa: BLE001
            pass


def _register_remote_draw() -> None:
    try:
        from bpy.types import SpaceView3D

        if runtime.get("draw_lines") is None:
            runtime["draw_lines"] = SpaceView3D.draw_handler_add(
                _draw_remote_lines, (), "WINDOW", "POST_VIEW"
            )
        if runtime.get("draw_labels") is None:
            runtime["draw_labels"] = SpaceView3D.draw_handler_add(
                _draw_remote_labels, (), "WINDOW", "POST_PIXEL"
            )
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _unregister_remote_draw() -> None:
    for key in ("draw_lines", "draw_labels"):
        handle = runtime.pop(key, None)
        if handle is None:
            continue
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
        except Exception:  # noqa: BLE001
            pass
    runtime["remote_batches"] = []
    runtime["remote_point_batches"] = []
    runtime["remote_points"] = []
    runtime["remote_labels"] = []


def _send_presence(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    now = time.monotonic()
    if now - runtime["last_presence"] < runtime.get("presence_interval", PRESENCE_POLL):
        return
    runtime["last_presence"] = now
    body = {
        "scene": context.scene.name,
        "frame": context.scene.frame_current,
        "mode": context.mode,
        "active": context.active_object.name if context.active_object is not None else "",
        "selected": ",".join(_selected_uids(context)),
        "cursor": [float(value) for value in context.scene.cursor.location],
    }
    view = _viewport_view(context)
    if view:
        body["view"] = view["matrix"]
        body["aspect"] = view["aspect"]
        body["persp"] = view["persp"]
        body["dist"] = view["distance"]
    session.send_presence(body)


def _write_status(context, force: bool = False) -> None:
    session: SessionService | None = runtime.get("session")
    now = time.monotonic()
    if not force and now - runtime["last_status"] < STATUS_WRITE:
        return
    runtime["last_status"] = now
    settings = context.window_manager.bmp
    stats = session.stats() if session else {}
    # The session object is the source of truth. Settings are updated from
    # queued events and can lag (or be overwritten by a late event), which used
    # to publish "offline" while the socket was healthy.
    state = settings.state
    code = settings.code
    is_host = settings.is_host
    name = settings.name
    if session is not None:
        live_state = getattr(session, "state", None)
        if live_state in {"offline", "hosting", "joining", "connected"}:
            state = live_state
        code = getattr(session, "code", None) or code
        is_host = bool(getattr(session, "is_host", is_host))
        name = getattr(session, "name", None) or name
    local_active = context.active_object.name if context.active_object is not None else ""
    for user in settings.users:
        if user.name == settings.name:
            user.active = local_active
            break
    live_rtt = stats.get("rtt_ms", 0)
    if is_host and not live_rtt:
        pings = [user.ping_ms for user in settings.users if user.ping_ms > 0]
        live_rtt = round(sum(pings) / len(pings), 1) if pings else 0
    users = [
        {
            "name": user.name,
            "color": list(effective_color(user.name, user.color)),
            "ping_ms": user.ping_ms,
            "host": user.host,
            "active": user.active,
            "role": "owner" if (is_host and user.name == name) else user.role,
        }
        for user in settings.users
    ]
    messages = [
        {"author": item.author, "text": item.text, "stamp": item.stamp}
        for item in list(settings.messages)[-20:]
    ]
    control.write_status(
        {
            "state": state,
            "code": code,
            "is_host": is_host,
            "name": name,
            "role": "owner" if is_host else settings.role,
            "overlay_info": settings.overlay_info,
            "direct_code": settings.direct_code,
            "uptime": max(0, int(time.monotonic() - runtime.get("session_started", runtime["started"]))),
            "total_bytes": int(stats.get("bytes_in", 0)) + int(stats.get("bytes_out", 0)),
            "fast_mode": bool(runtime.get("fast_mode", True)),
            "fingerprint": settings.fingerprint,
            "verify_code": settings.verify_code,
            "last_error": settings.last_error,
            "relay": bool(stats.get("relay", False)),
            "rtt_ms": live_rtt,
            "bytes_in": stats.get("bytes_in", 0),
            "bytes_out": stats.get("bytes_out", 0),
            "peers": stats.get("peers", []),
            "users": users,
            "messages": messages,
            "blender": bpy.app.version_string,
            "addon": ADDON_VERSION,
            "pid": os.getpid(),
        }
    )


def _object_alive(obj) -> bool:
    try:
        return obj is not None and obj.name in bpy.data.objects
    except ReferenceError:
        return False


def _smooth_transforms(context) -> None:
    from mathutils import Matrix

    now = time.monotonic()
    for uid, item in list(runtime["transform_smoothers"].items()):
        obj = item.get("obj")
        if not _object_alive(obj):
            runtime["transform_smoothers"].pop(uid, None)
            continue
        duration = float(item.get("duration") or runtime.get("transform_smooth", TRANSFORM_SMOOTH))
        t = min(1.0, max(0.0, (now - item["t0"]) / max(0.001, duration)))
        start = item["start"]
        target = item["target"]
        values = [start[index] + (target[index] - start[index]) * t for index in range(16)]
        try:
            obj.matrix_world = Matrix(
                (values[0:4], values[4:8], values[8:12], values[12:16])
            )
            for child in obj.children_recursive:
                child_uid = child.get("bmp_uuid")
                if child_uid:
                    runtime["object_shadow"][child_uid] = tuple(
                        round(float(value), 6) for row in child.matrix_world for value in row
                    )
        except Exception:  # noqa: BLE001
            runtime["transform_smoothers"].pop(uid, None)
            continue
        if t >= 1.0:
            runtime["transform_smoothers"].pop(uid, None)


def _flush_mesh_smoother(uid: str) -> None:
    item = runtime["mesh_smoothers"].pop(uid, None)
    if not item:
        return
    obj = item.get("obj")
    if not _object_alive(obj):
        return
    mesh = obj.data
    for index, (_start, target, _t0) in item.get("entries", {}).items():
        if 0 <= index < len(mesh.vertices):
            try:
                mesh.vertices[index].co = target
            except Exception:  # noqa: BLE001
                pass
    try:
        mesh.update()
    except Exception:  # noqa: BLE001
        pass


def _smooth_meshes(context) -> None:
    now = time.monotonic()
    for uid, item in list(runtime["mesh_smoothers"].items()):
        obj = item.get("obj")
        if not _object_alive(obj) or obj.type != "MESH":
            runtime["mesh_smoothers"].pop(uid, None)
            continue
        if obj.mode != "OBJECT":
            _flush_mesh_smoother(uid)
            continue
        mesh = obj.data
        entries = item.get("entries") or {}
        duration = float(item.get("duration") or runtime.get("mesh_smooth", MESH_SMOOTH))
        changed = False
        for index, (start, target, t0) in list(entries.items()):
            t = min(1.0, max(0.0, (now - t0) / max(0.001, duration)))
            if index < 0 or index >= len(mesh.vertices):
                del entries[index]
                continue
            try:
                mesh.vertices[index].co = (
                    start[0] + (target[0] - start[0]) * t,
                    start[1] + (target[1] - start[1]) * t,
                    start[2] + (target[2] - start[2]) * t,
                )
            except Exception:  # noqa: BLE001
                del entries[index]
                continue
            changed = True
            if t >= 1.0:
                del entries[index]
        if changed:
            try:
                mesh.update()
            except Exception:  # noqa: BLE001
                pass
        if not entries:
            runtime["mesh_smoothers"].pop(uid, None)


def bmp_smooth_tick():
    context = bpy.context
    if context is None:
        return 1.0 / 60.0
    try:
        _smooth_transforms(context)
        _smooth_meshes(context)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return 1.0 / 60.0


def _sync_edit_selection(context) -> None:
    """Send the active Edit Mode vertex selection at up to ~8 Hz."""
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    now = time.monotonic()
    if now - runtime["last_edit_sel"] < 0.12:
        return
    runtime["last_edit_sel"] = now
    active = context.active_object
    editing = active is not None and active.type == "MESH" and context.mode == "EDIT_MESH"
    if not editing:
        uid = runtime.get("last_selection_uid")
        count = runtime.get("last_selection_count") or 0
        if uid and count:
            session.send_selection(uid, bytes((count + 7) // 8), count)
            runtime["last_selection_uid"] = ""
            runtime["last_selection_count"] = 0
            runtime["selection_digest"].pop(uid, None)
        return
    uid = active.get("bmp_uuid")
    count = len(active.data.vertices)
    if not uid or count <= 0 or count > 200_000:
        return
    try:
        import bmesh

        bm = bmesh.from_edit_mesh(active.data)
        bm.verts.ensure_lookup_table()
    except Exception:  # noqa: BLE001
        return
    mask = bytearray((count + 7) // 8)
    for vert in bm.verts:
        if vert.select:
            index = vert.index
            if 0 <= index < count:
                mask[index >> 3] |= 1 << (index & 7)
    digest = hashlib.blake2b(bytes(mask), digest_size=8).digest()
    if runtime["selection_digest"].get(uid) == digest:
        return
    runtime["selection_digest"][uid] = digest
    runtime["last_selection_uid"] = uid
    runtime["last_selection_count"] = count
    session.send_selection(uid, bytes(mask), count)


def _apply_follow(context) -> None:
    settings = context.window_manager.bmp
    name = settings.follow_user
    if not name or context.screen is None:
        return
    user = next((item for item in settings.users if item.name == name), None)
    if user is None or not user.has_view:
        settings.follow_user = ""
        return
    matrix = _smooth_view(name, list(user.view), time.monotonic())
    if matrix is None:
        return
    area = next((item for item in context.screen.areas if item.type == "VIEW_3D"), None)
    if area is None or area.spaces.active is None:
        return
    region = area.spaces.active.region_3d
    if region is None:
        return
    try:
        region.view_matrix = matrix.inverted()
        region.view_perspective = "PERSP"
        region.view_distance = max(0.1, float(user.view_distance))
    except Exception:  # noqa: BLE001
        pass


def _update_timing(session: SessionService | None) -> None:
    """Use the fastest rates and shortest smoothing when latency is low."""
    rtt = 0.0
    relay = False
    if session is not None:
        try:
            stats = session.stats()
            rtt = float(stats.get("rtt_ms") or 0.0)
            relay = bool(stats.get("relay"))
        except Exception:  # noqa: BLE001
            rtt = 0.0
            relay = False
    fast = not relay or rtt <= 0.0 or rtt <= 60.0
    runtime["fast_mode"] = fast
    runtime["transform_interval"] = 1.0 / 60.0 if fast else 1.0 / 30.0
    runtime["presence_interval"] = 1.0 / 30.0 if fast else 1.0 / 20.0
    runtime["transform_smooth"] = 0.018 if fast else 0.035
    runtime["mesh_smooth"] = 0.025 if fast else 0.045
    runtime["view_smooth"] = 0.045 if fast else 0.075


def bmp_tick():
    context = bpy.context
    if context is None or context.window_manager is None:
        return TICK
    try:
        _poll_control(context)
        session: SessionService | None = runtime.get("session")
        _update_timing(session)
        inbox = runtime["inbox"]
        _expire_pings(context)
        while True:
            try:
                kind, body = inbox.get_nowait()
            except queue.Empty:
                break
            _handle_event(context, kind, body)
        if session is not None:
            _sync_selection(context)
            _sync_transforms(context)
            _sync_mesh(context)
            _sync_edit_selection(context)
            _sync_materials(context)
            _sync_modifiers(context)
            _send_presence(context)
        _pump_scene_bootstrap(context)
        _sync_remote_visuals(context)
        _apply_follow(context)
        if (
            runtime.get("bootstrapping")
            and runtime.get("bootstrap_deadline")
            and time.monotonic() > runtime["bootstrap_deadline"]
        ):
            runtime["bootstrapping"] = False
            runtime["bootstrap_deadline"] = 0.0
            _push_message(context, "system", "Host scene timed out; using the local scene")
            _prime_objects(context)
        # Keep writing while offline too, so the desktop app always sees the
        # current state and never stays stuck on "connected".
        _write_status(context)
        if session is not None and not session.is_alive():
            settings = context.window_manager.bmp
            settings.last_error = session.failure_reason or "session stopped"
            stop_session(context)
        elif session is not None and session.failed:
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
        settings = context.window_manager.bmp
        settings.lease_info = ""
        settings.lease_requester = ""
        self.report({"INFO"}, f"Granted to {request.get('requester')}")
        return {"FINISHED"}


class BMP_OT_deny_edit(Operator):
    bl_idname = "bmp.deny_edit"
    bl_label = "Deny Edit"
    bl_description = "Deny the pending edit request"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        request = runtime.get("lease_request")
        if session is None or not request:
            self.report({"ERROR"}, "No pending request")
            return {"CANCELLED"}
        session.send_lease(request["key"], "deny", request.get("requester"))
        runtime["lease_request"] = None
        settings = context.window_manager.bmp
        settings.lease_info = ""
        settings.lease_requester = ""
        self.report({"INFO"}, f"Denied {request.get('requester')}")
        return {"FINISHED"}


class BMP_OT_follow_user(Operator):
    bl_idname = "bmp.follow_user"
    bl_label = "Follow User"
    bl_description = "Follow a user's viewport, or stop following them"

    name: StringProperty()

    def execute(self, context):
        settings = context.window_manager.bmp
        target = self.name.strip()
        if not target:
            return {"CANCELLED"}
        settings.follow_user = "" if settings.follow_user == target else target
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
    BMP_OT_follow_user,
    BMP_OT_share_images,
    BMP_OT_request_edit,
    BMP_OT_grant_edit,
    BMP_OT_deny_edit,
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
    _register_remote_draw()
    bpy.app.handlers.frame_change_post.append(on_frame_change)


def unregister() -> None:
    if not getattr(bpy, "_blendermp_active", False):
        return
    bpy._blendermp_active = False
    _unregister_remote_draw()
    try:
        bpy.app.timers.unregister(bmp_tick)
    except ValueError:
        pass
    session = runtime.get("session")
    if session:
        session.stop()
        runtime["session"] = None
    try:
        settings = bpy.context.window_manager.bmp
        settings.state = "offline"
        settings.users.clear()
        runtime["last_status"] = 0.0
        _write_status(bpy.context, force=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    ui.unregister()
    for keymap, item in addon_keymaps:
        try:
            keymap.keymap_items.remove(item)
        except Exception:  # noqa: BLE001
            pass
    addon_keymaps.clear()
    del bpy.types.WindowManager.bmp
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
