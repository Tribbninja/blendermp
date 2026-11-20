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

import atexit
import base64
import hashlib
import json
import math
import platform
import queue
import re
import socket
import tempfile
import time
import traceback
import uuid
import zipfile
import zlib

import numpy as np
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper

from . import control, ui
from .core import chunker
from .core import checkpoints
from .core import eventlog
from .core import heartbeat
from .core import image_payload
from .core import journal
from .core import undo as undo_log
from .core import material as material_payload
from .core import object_payload
from .core.version import ADDON_VERSION
from .session import MAX_MESH, SessionService
from .sync import objects as obj_sync
from .sync.mesh import EditMirror, MeshMirror, snapshot_bmesh

bl_info = {
    "name": "Blendermp",
    "author": "Blendermp",
    "version": (0, 5, 1),
    "blender": (4, 2, 0),
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
    "scene_stats": {},
    "last_scene_stats": 0.0,
    "heartbeat_last": 0.0,
    "crash_entries": [],
    "scene_revs": {},
    "journal_session": "",
    "journal_active": False,
    "journal_pending": {},
    "journal_last_flush": 0.0,
    "journal_conflicts": [],
    "journal_files": [],
    "last_journal_capture": 0.0,
    "undo": undo_log.UndoLog(),
    "last_checkpoint": 0.0,
    "pose_shadow": {},
    "shape_shadow": {},
    "action_shadow": {},
    "last_pose": 0.0,
    "last_shape": 0.0,
    "last_action": 0.0,
    "last_sculpt": 0.0,
    "sculpt_warned": set(),
    "muted": set(),
    "last_subscription": 0.0,
    "subscription_sig": None,
    "last_client_stats": 0.0,
    "stats_bytes": 0,
    "stats_in": 0,
    "stats_out": 0,
    "stats_time": 0.0,
    "apply_times": [],
    "last_mesh": 0.0,
    "last_edit": 0.0,
    "mesh_cursor": 0,
    "pings": [],
    "known_objects": set(),
    "applying_frame": False,
    "material_shadow": {},
    "modifier_shadow": {},
    "last_modifier": 0.0,
    "modifier_cursor": 0,
    "lease_request": None,
    "lease_requests": {},
    "last_material": 0.0,
    "material_cursor": 0,
    "image_queue": [],
    "image_queued": set(),
    "image_hash_cache": {},
    "shared_images": set(),
    "received_images": {},
    "last_image": 0.0,
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
    playing: BoolProperty()
    fps: IntProperty(default=24)


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
    last_error_code: StringProperty()
    fingerprint: StringProperty()
    verify_code: StringProperty()
    chat_text: StringProperty()
    sync_mesh: BoolProperty(name="Sync active mesh", default=True)
    sync_materials: BoolProperty(name="Sync materials", default=True)
    sync_modifiers: BoolProperty(name="Sync modifiers", default=True)
    sync_visible_only: BoolProperty(name="Only sync visible objects", default=False)
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
    crash_notice: StringProperty()
    recovery_available: BoolProperty(default=False)
    conflict_count: IntProperty(default=0, min=0)
    conflict_info: StringProperty()
    journal_available: BoolProperty(default=False)


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
    settings.last_error_code = ""
    settings.journal_available = False
    runtime["journal_active"] = False
    runtime["journal_files"] = []
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
    eventlog.log("info", "HOST_START", "hosting", room=code, relay=bool(relay))
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
    settings.last_error_code = ""
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
    eventlog.log("info", "JOIN_START", "joining", room=settings.code, relay=bool(relay_url))
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
            eventlog.log(
                "info",
                "SESSION_STOP",
                "session stopped",
                state=getattr(session, "state", ""),
                room=getattr(session, "code", ""),
            )
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
        if value in {"offline", "hosting", "joining", "connected", "reconnecting"}:
            settings.state = value
            if value == "reconnecting" and not settings.is_host:
                _start_journal(context)
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
        leases = body.get("leases")
        if isinstance(leases, dict):
            runtime["lease_owners"] = {
                key: info["owner"]
                for key, info in leases.items()
                if isinstance(key, str) and isinstance(info, dict) and isinstance(info.get("owner"), str)
            }
        if body.get("resumed"):
            runtime["bootstrapping"] = False
            runtime["bootstrap_deadline"] = 0.0
            _push_message(context, "system", "Reconnected - scene kept")
        else:
            runtime["bootstrapping"] = True
            runtime["bootstrap_deadline"] = time.monotonic() + 20.0
            runtime["journal_active"] = False
            _push_message(context, "system", "Connected - loading host scene")
        revs = body.get("revs")
        if isinstance(revs, dict):
            runtime["scene_revs"] = {
                str(key): int(value)
                for key, value in revs.items()
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            }
        if body.get("resumed"):
            _replay_journal(context, body)
        _upsert_user(context, body.get("name", "host"), body.get("color", (1, 1, 1, 1)), host=True)
        for peer in body.get("peers", []):
            _upsert_user(context, peer.get("name", "?"), peer.get("color", (1, 1, 1, 1)))
        # The host now streams its scene; the client does not upload its own.
    elif kind == "peer_resume":
        name = body.get("name", "?")
        _upsert_user(context, name, body.get("color", (1, 1, 1, 1)))
        _push_message(context, "system", f"{name} reconnected")
        if settings.is_host:
            _resume_peer(context, name)
    elif kind == "resume_state":
        allowed = {str(item) for item in body.get("objects", []) if isinstance(item, str)}
        for obj in list(context.scene.objects):
            if obj.get("bmp_ping"):
                continue
            uid = obj.get("bmp_uuid")
            if uid and uid not in allowed:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:  # noqa: BLE001
                    pass
        runtime["bootstrapping"] = False
        runtime["bootstrap_deadline"] = 0.0
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
            # A late joiner missed any image assets sent before it arrived.
            # Re-queue them with the scene bootstrap.
            runtime["shared_images"].clear()
            runtime["material_shadow"].clear()
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
                user.playing = bool(body.get("playing", False))
                user.fps = int(body.get("fps", 24) or 24)
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
    elif kind == "pose":
        _apply_pose(context, body)
    elif kind == "shape_keys":
        _apply_shape_keys(context, body)
    elif kind == "action":
        _apply_action(context, body)
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
        settings.last_error_code = str(body.get("code", ""))[:48]
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
GRAPH_PROPS = (
    "interpolation",
    "extension",
    "projection",
    "uv_map",
    "vector_type",
    "attribute_name",
    "attribute_type",
    "noise_dimensions",
    "feature",
    "distance",
    "voronoi_dimensions",
    "wave_type",
    "bands_direction",
    "wave_profile",
    "data_type",
    "blend_type",
    "clamp_factor",
    "clamp",
    "space",
    "sky_type",
    "altitude",
    "air_density",
    "dust_density",
    "ozone_density",
    "operation",
    "use_clamp",
)


def _socket_default(value):
    """Convert a Blender socket default into a sanitizable JSON value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and abs(number) <= 1.0e6 else None
    if isinstance(value, str):
        return value[:32]
    try:
        values = list(value)
    except TypeError:
        return None
    if not 1 <= len(values) <= 4:
        return None
    result = []
    for component in values:
        if isinstance(component, bool):
            result.append(float(component))
            continue
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or abs(number) > 1.0e6:
            return None
        result.append(number)
    return result


def _node_input_id(socket) -> str:
    return (getattr(socket, "identifier", None) or getattr(socket, "name", ""))[:64]


def _socket_by_id(sockets, key):
    for socket in sockets:
        if _node_input_id(socket) == key:
            return socket
    return None


def _node_props(node) -> dict:
    props = {}
    for name in GRAPH_PROPS:
        try:
            value = getattr(node, name)
        except (AttributeError, RuntimeError):
            continue
        encoded = _socket_default(value)
        if encoded is not None:
            props[name] = encoded
    if node.bl_idname == "ShaderNodeValToRGB":
        try:
            ramp = node.color_ramp
            elements = [[float(item.position), *(float(c) for c in item.color)] for item in ramp.elements]
            if 2 <= len(elements) <= 32:
                props["elements"] = elements
        except (AttributeError, RuntimeError, TypeError):
            pass
    return props


def material_graph(material) -> tuple[list, list]:
    """Serialize the active surface output's upstream node graph.

    Only allow-listed nodes are traversed. Unsupported branches (node groups,
    scripts, OSL) are omitted instead of being approximated.
    """
    tree = getattr(material, "node_tree", None)
    if not material.use_nodes or tree is None:
        return [], []
    nodes = list(tree.nodes)
    output = None
    for node in nodes:
        if node.bl_idname != "ShaderNodeOutputMaterial":
            continue
        if output is None or getattr(node, "is_active_output", False):
            output = node
    if output is None:
        return [], []
    order = []
    index = {}
    stack = [output]
    while stack and len(order) < material_payload.MAX_NODES:
        node = stack.pop()
        name = node.name
        if name in index:
            continue
        if node.bl_idname not in material_payload.ALLOWED_NODES:
            continue
        index[name] = len(order)
        order.append(node)
        for socket in node.inputs:
            try:
                links = socket.links
            except (AttributeError, RuntimeError):
                continue
            for link in links:
                source = link.from_node
                if source.name not in index and source.bl_idname in material_payload.ALLOWED_NODES:
                    stack.append(source)
    if output.name not in index:
        return [], []
    specs = []
    for node in order:
        spec = {
            "bl_idname": node.bl_idname,
            "name": node.name[:64],
            "location": [float(node.location[0]), float(node.location[1])],
        }
        inputs = {}
        for socket in node.inputs:
            if getattr(socket, "is_linked", False):
                continue
            value = _socket_default(getattr(socket, "default_value", None))
            if value is not None:
                inputs[_node_input_id(socket)] = value
        if inputs:
            spec["inputs"] = inputs
        props = _node_props(node)
        if props:
            spec["props"] = props
        image = getattr(node, "image", None)
        if image is not None:
            spec["image"] = image.name[:64]
            cached = runtime["image_hash_cache"].get(image.name)
            if cached:
                spec["image_hash"] = cached[1]
            try:
                spec["image_colorspace"] = image.colorspace_settings.name[:32]
                spec["image_alpha_mode"] = image.alpha_mode[:16]
            except (AttributeError, RuntimeError, TypeError):
                pass
        specs.append(spec)
    links = []
    for link in tree.links:
        source = index.get(link.from_node.name)
        target = index.get(link.to_node.name)
        if source is None or target is None or source == target:
            continue
        source_socket = _node_input_id(link.from_socket)
        target_socket = _node_input_id(link.to_socket)
        if not source_socket or not target_socket:
            continue
        links.append([source, source_socket, target, target_socket])
        if len(links) >= material_payload.MAX_LINKS:
            break
    return specs, links


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
    nodes, links = material_graph(material)
    if nodes:
        data["nodes"] = nodes
        data["links"] = links
    return data


def _material_signature(data: dict) -> str:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(data)


def _reset_basic_nodes(material) -> None:
    tree = material.node_tree
    if tree is None:
        return
    try:
        tree.nodes.clear()
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
        socket = bsdf.outputs.get("BSDF")
        surface = output.inputs.get("Surface")
        if socket is not None and surface is not None:
            tree.links.new(socket, surface)
        output.location = (300.0, 0.0)
        bsdf.location = (0.0, 0.0)
        tree.nodes.active = output
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _apply_material_graph(material, data: dict) -> bool:
    """Rebuild a whitelisted node graph. Returns False on any failure."""
    tree = getattr(material, "node_tree", None)
    specs = data.get("nodes") or []
    if tree is None or not specs:
        return False
    if not any(spec.get("bl_idname") == "ShaderNodeOutputMaterial" for spec in specs):
        return False
    created = []
    try:
        tree.nodes.clear()
        for spec in specs:
            node = tree.nodes.new(spec["bl_idname"])
            name = spec.get("name")
            if name:
                node.name = name[:64]
            location = spec.get("location")
            if isinstance(location, list) and len(location) == 2:
                node.location = (float(location[0]), float(location[1]))
            for key, value in (spec.get("inputs") or {}).items():
                socket = _socket_by_id(node.inputs, key)
                if socket is None:
                    continue
                try:
                    socket.default_value = tuple(value) if isinstance(value, list) else value
                except (TypeError, ValueError):
                    pass
            props = spec.get("props") or {}
            for key, value in props.items():
                if key == "elements":
                    continue
                try:
                    setattr(node, key, tuple(value) if isinstance(value, list) else value)
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    pass
            if node.bl_idname == "ShaderNodeValToRGB" and isinstance(props.get("elements"), list):
                try:
                    ramp = node.color_ramp
                    elements = props["elements"]
                    while len(ramp.elements) > len(elements) and len(ramp.elements) > 2:
                        ramp.elements.remove(ramp.elements[-1])
                    while len(ramp.elements) < len(elements):
                        ramp.elements.new(0.5)
                    for target, item in zip(ramp.elements, elements):
                        if len(item) != 5:
                            continue
                        target.position = float(item[0])
                        target.color = tuple(float(component) for component in item[1:5])
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    pass
            if node.bl_idname in {"ShaderNodeTexImage", "ShaderNodeTexEnvironment"}:
                image_name = spec.get("image")
                if image_name:
                    image = bpy.data.images.get(image_name)
                    if image is None:
                        digest = spec.get("image_hash")
                        if digest:
                            existing = runtime["received_images"].get(digest)
                            if existing:
                                image = bpy.data.images.get(existing)
                    if image is not None:
                        node.image = image
                        _apply_image_settings(
                            image,
                            spec.get("image_colorspace"),
                            spec.get("image_alpha_mode"),
                        )
                    else:
                        node["bmp_image"] = image_name[:64]
                        if spec.get("image_hash"):
                            node["bmp_image_hash"] = spec["image_hash"]
                        if spec.get("image_colorspace"):
                            node["bmp_image_colorspace"] = spec["image_colorspace"][:32]
                        if spec.get("image_alpha_mode"):
                            node["bmp_image_alpha_mode"] = spec["image_alpha_mode"][:16]
            created.append(node)
        for link in data.get("links") or []:
            if not isinstance(link, list) or len(link) != 4:
                continue
            source, source_key, target, target_key = link
            if not 0 <= int(source) < len(created) or not 0 <= int(target) < len(created):
                continue
            from_socket = _socket_by_id(created[int(source)].outputs, source_key)
            to_socket = _socket_by_id(created[int(target)].inputs, target_key)
            if from_socket is None or to_socket is None:
                continue
            tree.links.new(from_socket, to_socket)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False
    for node in created:
        if node.bl_idname == "ShaderNodeOutputMaterial":
            try:
                node.is_active_output = True
                tree.nodes.active = node
            except (AttributeError, RuntimeError):
                pass
            break
    return True


def _action_fcurves(action) -> list:
    try:
        if hasattr(action, "fcurves"):
            return list(action.fcurves)
    except (AttributeError, RuntimeError):
        pass
    curves = []
    try:
        for layer in action.layers:
            for strip in layer.strips:
                if getattr(strip, "type", "") != "KEYFRAME":
                    continue
                for channelbag in strip.channelbags:
                    curves.extend(list(channelbag.fcurves))
    except (AttributeError, RuntimeError):
        pass
    return curves


def _action_channelbag(action, obj):
    """Return a channelbag for Blender 5's layered actions, or None."""
    if not hasattr(action, "slots") or not hasattr(action, "layers"):
        return None
    try:
        slot = action.slots[0] if len(action.slots) else action.slots.new(id_type="OBJECT", name=obj.name[:64])
        layer = action.layers[0] if len(action.layers) else action.layers.new("Layer")
        strip = next((item for item in layer.strips if getattr(item, "type", "") == "KEYFRAME"), None)
        if strip is None:
            strip = layer.strips.new(type="KEYFRAME")
        return strip.channelbag(slot, ensure=True)
    except (AttributeError, TypeError, RuntimeError):
        return None


def _clear_action_curves(action, obj) -> None:
    channelbag = _action_channelbag(action, obj)
    if channelbag is not None:
        for curve in list(channelbag.fcurves):
            channelbag.fcurves.remove(curve)
        return
    try:
        for curve in list(action.fcurves):
            action.fcurves.remove(curve)
    except (AttributeError, RuntimeError):
        pass


def _action_snapshot(obj) -> dict | None:
    animation = getattr(obj, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return None
    curves = []
    try:
        for curve in _action_fcurves(action)[:128]:
            points = []
            for point in list(curve.keyframe_points)[:512]:
                points.append(
                    [
                        float(point.co[0]),
                        float(point.co[1]),
                        float(point.handle_left[0]),
                        float(point.handle_left[1]),
                        float(point.handle_right[0]),
                        float(point.handle_right[1]),
                        str(point.interpolation)[:16],
                    ]
                )
            curves.append(
                {
                    "path": curve.data_path[:256],
                    "index": int(curve.array_index),
                    "extrapolation": str(curve.extrapolation)[:16],
                    "points": points,
                }
            )
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    return {
        "name": action.name[:64],
        "range": [float(value) for value in action.frame_range],
        "curves": curves,
    }


def _sync_pose(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or runtime.get("bootstrapping"):
        return
    if not settings.is_host and session.state != "connected":
        return
    now = time.monotonic()
    if now - runtime.get("last_pose", 0.0) < 1.0 / 30.0:
        return
    runtime["last_pose"] = now
    for obj in context.scene.objects:
        if obj.type != "ARMATURE" or not (uid := obj.get("bmp_uuid")):
            continue
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        shadow = runtime["pose_shadow"].setdefault(uid, {})
        changes = []
        for bone in list(obj.pose.bones)[:256]:
            state = (
                tuple(round(float(value), 5) for value in bone.location),
                tuple(round(float(value), 5) for value in bone.rotation_quaternion),
                tuple(round(float(value), 5) for value in bone.scale),
                str(bone.rotation_mode)[:16],
            )
            if shadow.get(bone.name) == state:
                continue
            shadow[bone.name] = state
            changes.append(
                {
                    "name": bone.name[:64],
                    "l": list(state[0]),
                    "q": list(state[1]),
                    "s": list(state[2]),
                    "m": state[3],
                }
            )
        if changes:
            _note_rev(uid)
            session.send_pose(uid, changes)


def _apply_pose(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    bones = body.get("bones")
    if obj is None or obj.type != "ARMATURE" or not isinstance(bones, list):
        return
    uid = obj.get("bmp_uuid")
    shadow = runtime["pose_shadow"].setdefault(uid, {}) if uid else {}
    for item in bones[:256]:
        if not isinstance(item, dict):
            continue
        bone = obj.pose.bones.get(str(item.get("name", "")))
        if bone is None:
            continue
        try:
            mode = str(item.get("m", "QUATERNION"))[:16]
            bone.rotation_mode = mode
            bone.location = [float(value) for value in item.get("l", ())][:3]
            quaternion = [float(value) for value in item.get("q", ())][:4]
            if mode == "QUATERNION":
                bone.rotation_quaternion = quaternion
            else:
                from mathutils import Quaternion

                bone.rotation_euler = Quaternion(quaternion).to_euler(mode)
            bone.scale = [float(value) for value in item.get("s", ())][:3]
            shadow[bone.name] = (
                tuple(round(float(value), 5) for value in bone.location),
                tuple(round(float(value), 5) for value in bone.rotation_quaternion),
                tuple(round(float(value), 5) for value in bone.scale),
                mode,
            )
        except (TypeError, ValueError, RuntimeError):
            pass


def _sync_shape_keys(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or runtime.get("bootstrapping"):
        return
    if not settings.is_host and session.state != "connected":
        return
    now = time.monotonic()
    if now - runtime.get("last_shape", 0.0) < 1.0 / 30.0:
        return
    runtime["last_shape"] = now
    for obj in context.scene.objects:
        if obj.type != "MESH" or not (uid := obj.get("bmp_uuid")):
            continue
        mesh = obj.data
        if mesh.shape_keys is None:
            continue
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        shadow = runtime["shape_shadow"].setdefault(uid, {})
        changes = {}
        for key in list(mesh.shape_keys.key_blocks)[:128]:
            value = round(float(key.value), 4)
            if shadow.get(key.name) == value:
                continue
            shadow[key.name] = value
            changes[key.name[:64]] = value
        if changes:
            _note_rev(uid)
            session.send_shape_keys(uid, changes)


def _apply_shape_keys(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    values = body.get("values")
    if obj is None or obj.type != "MESH" or obj.data.shape_keys is None or not isinstance(values, dict):
        return
    uid = obj.get("bmp_uuid")
    shadow = runtime["shape_shadow"].setdefault(uid, {}) if uid else {}
    for name, value in list(values.items())[:128]:
        key = obj.data.shape_keys.key_blocks.get(str(name))
        if key is None or not isinstance(value, (int, float)):
            continue
        key.value = max(float(key.slider_min), min(float(key.slider_max), float(value)))
        shadow[key.name] = round(float(key.value), 4)


def _sync_actions(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or runtime.get("bootstrapping"):
        return
    if not settings.is_host and session.state != "connected":
        return
    now = time.monotonic()
    if now - runtime.get("last_action", 0.0) < 0.5:
        return
    runtime["last_action"] = now
    for obj in context.scene.objects:
        if obj.type != "ARMATURE" or not (uid := obj.get("bmp_uuid")):
            continue
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        snapshot = _action_snapshot(obj)
        signature = json.dumps(snapshot, sort_keys=True, default=str)
        if runtime["action_shadow"].get(uid) == signature:
            continue
        runtime["action_shadow"][uid] = signature
        _note_rev(uid)
        session.send_action(uid, snapshot)


def _apply_action(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    snapshot = body.get("action")
    if obj is None or obj.type != "ARMATURE":
        return
    uid = obj.get("bmp_uuid")
    if snapshot is None:
        animation = getattr(obj, "animation_data", None)
        if animation is not None:
            animation.action = None
        if uid:
            runtime["action_shadow"][uid] = "null"
        return
    if not isinstance(snapshot, dict):
        return
    name = str(snapshot.get("name", "Action"))[:64] or "Action"
    action = bpy.data.actions.get(name)
    if action is None:
        action = bpy.data.actions.new(name)
    try:
        _clear_action_curves(action, obj)
        channelbag = _action_channelbag(action, obj)
        for curve_spec in (snapshot.get("curves") or [])[:128]:
            if not isinstance(curve_spec, dict):
                continue
            if channelbag is not None:
                curve = channelbag.fcurves.new(
                    data_path=str(curve_spec.get("path", ""))[:256],
                    index=int(curve_spec.get("index", 0)),
                )
            else:
                curve = action.fcurves.new(
                    data_path=str(curve_spec.get("path", ""))[:256],
                    index=int(curve_spec.get("index", 0)),
                )
            curve.extrapolation = str(curve_spec.get("extrapolation", "CONSTANT"))[:16]
            points = [item for item in (curve_spec.get("points") or [])[:512] if isinstance(item, list) and len(item) == 7]
            if points:
                curve.keyframe_points.add(len(points))
                for target, item in zip(curve.keyframe_points, points):
                    target.co = (float(item[0]), float(item[1]))
                    target.handle_left = (float(item[2]), float(item[3]))
                    target.handle_right = (float(item[4]), float(item[5]))
                    target.interpolation = str(item[6])[:16]
        animation = obj.animation_data_create()
        animation.action = action
        if hasattr(animation, "action_slot") and len(action.slots):
            try:
                animation.action_slot = action.slots[0]
            except (AttributeError, TypeError, RuntimeError):
                pass
        if uid:
            runtime["action_shadow"][uid] = json.dumps(snapshot, sort_keys=True, default=str)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        traceback.print_exc()


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
        if not settings.is_host and uid not in runtime["owned_leases"]:
            for index, slot in enumerate(obj.material_slots[:16]):
                if slot.material is not None:
                    data = material_values(slot.material)
                    runtime["material_shadow"][(uid, index)] = _material_signature(data)
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
            _note_rev(uid)
            session.send_material(uid, index, data)
            _queue_material_images(material)


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
    applied = False
    if data.get("nodes"):
        applied = _apply_material_graph(material, data)
        if not applied:
            _reset_basic_nodes(material)
    if not applied and material.node_tree is not None:
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
    try:
        runtime["material_shadow"][(body.get("uuid"), slot_index)] = _material_signature(material_values(material))
    except Exception:  # noqa: BLE001
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
        if mod.type in {"ARMATURE", "LATTICE", "CURVE", "HOOK", "SURFACE_DEFORM", "MESH_DEFORM"}:
            target = getattr(mod, "object", None)
            if target is not None and target.get("bmp_uuid"):
                props["object_uid"] = target.get("bmp_uuid")
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


def object_details(obj) -> dict:
    """Small object-level properties that are not transforms or mesh data."""
    details = {
        "hide_viewport": bool(getattr(obj, "hide_viewport", False)),
        "hide_render": bool(getattr(obj, "hide_render", False)),
        "show_in_front": bool(getattr(obj, "show_in_front", False)),
        "color": [float(value) for value in getattr(obj, "color", (1.0, 1.0, 1.0, 1.0))][:4],
    }
    parent = getattr(obj, "parent", None)
    details["parent"] = parent.get("bmp_uuid") if parent is not None else None
    if parent is not None:
        try:
            details["matrix_parent_inverse"] = [
                float(value) for row in obj.matrix_parent_inverse for value in row
            ]
        except (AttributeError, TypeError, ValueError):
            pass
    collections = []
    scene_collection = getattr(bpy.context.scene, "collection", None)
    for collection in list(getattr(obj, "users_collection", []))[:16]:
        if collection == scene_collection:
            continue
        collections.append(
            {
                "name": collection.name[:64],
                "hide_viewport": bool(collection.hide_viewport),
                "hide_render": bool(collection.hide_render),
                "hide_select": bool(collection.hide_select),
            }
        )
    if collections:
        details["collections"] = collections
    constraints = []
    for constraint in list(getattr(obj, "constraints", []))[:32]:
        item = {
            "type": str(constraint.type)[:32],
            "name": str(constraint.name)[:64],
            "influence": float(getattr(constraint, "influence", 1.0)),
            "mute": bool(getattr(constraint, "mute", False)),
        }
        target = getattr(constraint, "target", None)
        if target is not None and target.get("bmp_uuid"):
            item["target"] = target.get("bmp_uuid")
        subtarget = getattr(constraint, "subtarget", "")
        if subtarget:
            item["subtarget"] = str(subtarget)[:64]
        constraints.append(item)
    if constraints:
        details["constraints"] = constraints
    return details


def _modifier_signature(snapshot: list, details=None) -> str:
    return json.dumps({"modifiers": snapshot, "details": details or {}}, sort_keys=True, default=str)


def _sync_modifiers(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or not settings.sync_modifiers or runtime.get("bootstrapping"):
        return
    now = time.monotonic()
    if now - runtime["last_modifier"] < 0.5:
        return
    runtime["last_modifier"] = now
    candidates = [
        item
        for item in context.scene.objects
        if item.get("bmp_uuid") and not item.get("bmp_ping")
    ]
    if not candidates:
        return
    cursor = runtime.get("modifier_cursor", 0) % len(candidates)
    runtime["modifier_cursor"] = cursor + 4
    for obj in candidates[cursor : cursor + 4]:
        uid = obj.get("bmp_uuid")
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        snapshot = modifier_snapshot(obj)
        details = object_details(obj)
        signature = _modifier_signature(snapshot, details)
        if not settings.is_host and uid not in runtime["owned_leases"]:
            runtime["modifier_shadow"][uid] = signature
            continue
        if runtime["modifier_shadow"].get(uid) == signature:
            continue
        runtime["modifier_shadow"][uid] = signature
        _note_rev(uid)
        session.send_modifier_stack(uid, snapshot, details)


def _modifier_signature(snapshot: list, details=None) -> str:
    return json.dumps({"modifiers": snapshot, "details": details or {}}, sort_keys=True, default=str)


def _apply_object_details(obj, details) -> None:
    if not isinstance(details, dict):
        return
    for key in ("hide_viewport", "hide_render", "show_in_front"):
        if key in details:
            try:
                setattr(obj, key, bool(details[key]))
            except (AttributeError, TypeError, RuntimeError):
                pass
    color = details.get("color")
    if isinstance(color, list) and len(color) == 4:
        try:
            obj.color = tuple(max(0.0, min(1.0, float(value))) for value in color)
        except (TypeError, ValueError):
            pass
    parent_uid = details.get("parent")
    try:
        world = obj.matrix_world.copy()
    except (AttributeError, RuntimeError):
        world = None
    current_parent = obj.parent.get("bmp_uuid") if obj.parent is not None else None
    desired_inverse = details.get("matrix_parent_inverse")
    inverse_matches = True
    if isinstance(desired_inverse, list) and len(desired_inverse) == 16 and obj.parent is not None:
        try:
            current_inverse = [float(value) for row in obj.matrix_parent_inverse for value in row]
            inverse_matches = all(
                abs(current_inverse[index] - float(value)) <= 1e-6
                for index, value in enumerate(desired_inverse)
            )
        except (TypeError, ValueError, AttributeError):
            inverse_matches = False
    if not (parent_uid == current_parent and inverse_matches):
        if parent_uid is None:
            if obj.parent is not None:
                try:
                    obj.parent = None
                    if world is not None:
                        obj.matrix_world = world
                except (TypeError, RuntimeError):
                    pass
        else:
            parent = obj_sync.find(parent_uid)
            if parent is not None and parent is not obj:
                ancestor = parent
                cycle = False
                while ancestor is not None:
                    if ancestor is obj:
                        cycle = True
                        break
                    ancestor = ancestor.parent
                if not cycle:
                    try:
                        obj.parent = parent
                        inverse = details.get("matrix_parent_inverse")
                        if isinstance(inverse, list) and len(inverse) == 16:
                            from mathutils import Matrix

                            obj.matrix_parent_inverse = Matrix(
                                (inverse[0:4], inverse[4:8], inverse[8:12], inverse[12:16])
                            )
                        # Reparenting must not move the object: transforms are
                        # authoritative over a separate channel.
                        if world is not None:
                            obj.matrix_world = world
                    except (TypeError, ValueError, RuntimeError):
                        pass
    collections = details.get("collections")
    if isinstance(collections, list):
        scene_collection = bpy.context.scene.collection
        keep = set()
        for item in collections[:16]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))[:64]
            if not name:
                continue
            collection = bpy.data.collections.get(name)
            if collection is None:
                try:
                    collection = bpy.data.collections.new(name)
                    scene_collection.children.link(collection)
                except (TypeError, RuntimeError):
                    continue
            try:
                if obj.name not in collection.objects:
                    collection.objects.link(obj)
            except (TypeError, RuntimeError):
                pass
            keep.add(collection)
            for flag in ("hide_viewport", "hide_render", "hide_select"):
                if flag in item:
                    try:
                        setattr(collection, flag, bool(item[flag]))
                    except (AttributeError, TypeError, RuntimeError):
                        pass
        if keep:
            for collection in list(obj.users_collection):
                if collection != scene_collection and collection not in keep:
                    try:
                        collection.objects.unlink(obj)
                    except (TypeError, RuntimeError):
                        pass
    constraints = details.get("constraints")
    if isinstance(constraints, list):
        for item in constraints[:32]:
            if not isinstance(item, dict):
                continue
            type_ = str(item.get("type", ""))[:32]
            name = str(item.get("name", ""))[:64]
            if not type_ or not name:
                continue
            constraint = obj.constraints.get(name)
            if constraint is None or constraint.type != type_:
                if constraint is not None:
                    try:
                        obj.constraints.remove(constraint)
                    except (TypeError, RuntimeError):
                        pass
                try:
                    constraint = obj.constraints.new(type_)
                    constraint.name = name
                except (TypeError, RuntimeError):
                    continue
            try:
                constraint.influence = float(item.get("influence", 1.0))
                constraint.mute = bool(item.get("mute", False))
                target = obj_sync.find(str(item.get("target", "")))
                if target is not None:
                    constraint.target = target
                subtarget = item.get("subtarget")
                if isinstance(subtarget, str) and subtarget:
                    constraint.subtarget = subtarget
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass


def _apply_modifier_stack(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    snapshot = body.get("modifiers")
    if obj is None or not hasattr(obj, "modifiers") or not isinstance(snapshot, list):
        return
    _apply_object_details(obj, body.get("details"))
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
            if key == "object_uid":
                target = obj_sync.find(str(value))
                if target is not None:
                    try:
                        mod.object = target
                    except Exception:  # noqa: BLE001
                        pass
                continue
            try:
                setattr(mod, key, value)
            except Exception:  # noqa: BLE001
                pass
    try:
        runtime["modifier_shadow"][body.get("uuid")] = _modifier_signature(
            modifier_snapshot(obj), object_details(obj)
        )
    except Exception:  # noqa: BLE001
        runtime["modifier_shadow"][body.get("uuid")] = _modifier_signature(snapshot, body.get("details"))


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


def _material_images(material):
    tree = getattr(material, "node_tree", None)
    if not material.use_nodes or tree is None:
        return []
    images = []
    for node in tree.nodes:
        if node.bl_idname not in {"ShaderNodeTexImage", "ShaderNodeTexEnvironment"}:
            continue
        image = getattr(node, "image", None)
        if image is not None and image.name not in {item.name for item in images}:
            images.append(image)
    return images


def _queue_material_images(material) -> None:
    for image in _material_images(material):
        _queue_image(image)


def _queue_image(image) -> None:
    if image is None or image.name in runtime["image_queued"]:
        return
    runtime["image_queued"].add(image.name)
    runtime["image_queue"].append(image.name)


def _process_image_queue(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None or not runtime["image_queue"]:
        return
    now = time.monotonic()
    if now - runtime["last_image"] < 0.2:
        return
    runtime["last_image"] = now
    name = runtime["image_queue"].pop(0)
    runtime["image_queued"].discard(name)
    image = bpy.data.images.get(name)
    if image is None:
        return
    data, format_hint = _image_bytes(context, image)
    if not data or len(data) > image_payload.MAX_IMAGE:
        return
    digest = hashlib.sha1(data).hexdigest()
    runtime["image_hash_cache"][image.name] = (
        (getattr(image, "filepath", ""), len(data), tuple(getattr(image, "size", (0, 0)))),
        digest,
    )
    if digest in runtime["shared_images"]:
        return
    wire = session.send_image(image.name, format_hint, data)
    if wire:
        runtime["shared_images"].add(digest)
        if len(data) > image_payload.COMPRESS_THRESHOLD and wire < len(data):
            saved = len(data) - wire
            _push_message(
                context,
                "system",
                f"Compressed {image.name[:32]} from {len(data) / 1048576:.1f} MB to {wire / 1048576:.1f} MB",
            )
            eventlog.log(
                "info",
                "IMAGE_COMPRESSED",
                "image compressed before transfer",
                name=image.name[:64],
                raw=len(data),
                wire=wire,
                saved=saved,
            )


def _apply_image_settings(image, colorspace, alpha_mode) -> None:
    if image is None:
        return
    if colorspace:
        try:
            if image.colorspace_settings.name != colorspace:
                image.colorspace_settings.name = colorspace
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass
    if alpha_mode:
        try:
            if image.alpha_mode != alpha_mode:
                image.alpha_mode = alpha_mode
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass


def _bind_material_images(name: str, image, digest: str | None) -> None:
    for material in bpy.data.materials:
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        for node in tree.nodes:
            if node.bl_idname not in {"ShaderNodeTexImage", "ShaderNodeTexEnvironment"}:
                continue
            try:
                marker = node.get("bmp_image")
                marker_hash = node.get("bmp_image_hash")
            except (AttributeError, TypeError):
                continue
            if marker == name or (digest and marker_hash == digest):
                try:
                    node.image = image
                except (TypeError, RuntimeError):
                    continue
                _apply_image_settings(
                    image,
                    node.get("bmp_image_colorspace"),
                    node.get("bmp_image_alpha_mode"),
                )


def _apply_image(context, body: dict) -> None:
    data = body.get("data")
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > image_payload.MAX_IMAGE:
        return
    format_hint = str(body.get("format", "png")).lower()
    if format_hint not in image_payload.ALLOWED:
        format_hint = "png"
    name = str(body.get("name", "Image"))[:64] or "Image"
    digest = str(body.get("sha1", "")) or hashlib.sha1(bytes(data)).hexdigest()
    existing_name = runtime["received_images"].get(digest)
    if existing_name:
        existing = bpy.data.images.get(existing_name)
        if existing is not None:
            _bind_material_images(name, existing, digest)
            return
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
        try:
            image.filepath_raw = ""
        except (AttributeError, RuntimeError):
            pass
        runtime["received_images"][digest] = name
        _bind_material_images(name, image, digest)
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
            if material is None:
                continue
            for image in _material_images(material):
                images[image.name] = image
    for image in images.values():
        _queue_image(image)
    return len(images)


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
    if obj.type == "ARMATURE":
        return object_payload.KIND_ARMATURE
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
        _queue_material_images(material)
        runtime["material_shadow"][(uid, index)] = _material_signature(data)
    if hasattr(obj, "modifiers"):
        snapshot = modifier_snapshot(obj)
        details = object_details(obj)
        session.send_modifier_stack(uid, snapshot, details)
        runtime["modifier_shadow"][uid] = _modifier_signature(snapshot, details)


def _note_rev(uid: str) -> None:
    if not uid:
        return
    runtime["scene_revs"][uid] = runtime["scene_revs"].get(uid, 0) + 1
    session: SessionService | None = runtime.get("session")
    if session is not None:
        session.bump_rev(uid)


def _start_journal(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.is_host:
        return
    session_id = getattr(session, "session_id", "") or settings.code
    if not session_id:
        return
    runtime["journal_session"] = session_id
    runtime["journal_active"] = True
    runtime["journal_pending"] = {}
    runtime["journal_last_flush"] = 0.0
    runtime["last_journal_capture"] = 0.0


def _journal_capture(context) -> None:
    if not runtime.get("journal_active"):
        return
    session: SessionService | None = runtime.get("session")
    if session is not None and session.state in {"connected"}:
        return
    now = time.monotonic()
    if now - runtime.get("last_journal_capture", 0.0) < 0.2:
        return
    runtime["last_journal_capture"] = now
    shadow = runtime["object_shadow"]
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        matrix = obj.matrix_world
        current = tuple(round(float(value), 6) for row in matrix for value in row)
        previous = shadow.get(uid)
        if previous is None:
            shadow[uid] = current
            continue
        if current == previous:
            continue
        shadow[uid] = current
        runtime["journal_pending"][uid] = {
            "ts": journal.stamp(),
            "uid": uid,
            "kind": "transform",
            "base_rev": int(runtime["scene_revs"].get(uid, 0)),
            "matrix": list(current),
        }
    active = context.active_object
    if (
        active is not None
        and active.type == "MESH"
        and context.mode != "EDIT_MESH"
        and (uid := active.get("bmp_uuid"))
    ):
        mirror = runtime["mirrors"].get(uid)
        if mirror is not None:
            try:
                delta = mirror.poll(active.data)
            except Exception:  # noqa: BLE001
                delta = None
            if delta is not None:
                try:
                    payload = mirror.full_snapshot(active.data)
                except Exception:  # noqa: BLE001
                    payload = b""
                if payload:
                    runtime["journal_pending"][(uid, "mesh")] = {
                        "ts": journal.stamp(),
                        "uid": uid,
                        "kind": "mesh_snapshot",
                        "base_rev": int(runtime["scene_revs"].get(uid, 0)),
                        "payload": base64.b64encode(payload).decode("ascii"),
                    }
        for index, slot in enumerate(getattr(active, "material_slots", [])[:16]):
            material = slot.material
            if material is None:
                continue
            data = material_values(material)
            signature = _material_signature(data)
            key = (uid, index)
            if runtime["material_shadow"].get(key) == signature:
                continue
            runtime["material_shadow"][key] = signature
            runtime["journal_pending"][(uid, index, "material")] = {
                "ts": journal.stamp(),
                "uid": uid,
                "kind": "material",
                "slot": index,
                "base_rev": int(runtime["scene_revs"].get(uid, 0)),
                "material": data,
            }
    if runtime["journal_pending"] and now - runtime.get("journal_last_flush", 0.0) > 0.5:
        _flush_journal()


def _flush_journal() -> None:
    session_id = runtime.get("journal_session", "")
    if not session_id:
        return
    for entry in runtime["journal_pending"].values():
        journal.append(session_id, entry)
    runtime["journal_pending"] = {}
    runtime["journal_last_flush"] = time.monotonic()


def _replay_journal(context, body: dict) -> None:
    session_id = runtime.get("journal_session", "") or str(body.get("session_id", ""))
    if not session_id:
        return
    _flush_journal()
    entries, skipped = journal.read(session_id)
    revs = body.get("revs") if isinstance(body.get("revs"), dict) else {}
    settings = context.window_manager.bmp
    session: SessionService | None = runtime.get("session")
    conflicts = []
    replayed = 0
    if session is not None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind not in {"transform", "mesh_snapshot", "material"}:
                continue
            uid = entry.get("uid")
            if not isinstance(uid, str):
                continue
            base = entry.get("base_rev", 0)
            try:
                host_rev = int(revs.get(uid, 0))
            except (TypeError, ValueError):
                host_rev = 0
            if host_rev > int(base):
                conflicts.append(entry)
                continue
            if kind == "transform":
                matrix = entry.get("matrix")
                if not isinstance(matrix, list) or len(matrix) != 16:
                    continue
                session.send_transform(uid, matrix)
                obj = obj_sync.find(uid)
                if obj is not None:
                    obj_sync.apply(uid, matrix)
                    runtime["object_shadow"][uid] = tuple(round(float(value), 6) for value in matrix)
                replayed += 1
            elif kind == "mesh_snapshot":
                payload = entry.get("payload")
                if not isinstance(payload, str):
                    continue
                try:
                    data = base64.b64decode(payload.encode("ascii"))
                except (ValueError, TypeError):
                    continue
                obj = obj_sync.find(uid)
                if obj is not None and obj.type == "MESH" and data:
                    session.send_mesh(obj, "snapshot", data, 0)
                    replayed += 1
            elif kind == "material":
                slot = entry.get("slot")
                material = entry.get("material")
                if isinstance(slot, int) and isinstance(material, dict):
                    session.send_material(uid, slot, material)
                    replayed += 1
    runtime["journal_conflicts"] = conflicts
    settings.conflict_count = len(conflicts)
    settings.conflict_info = (
        f"{len(conflicts)} offline edit(s) conflict with host changes"
        if conflicts
        else ""
    )
    if conflicts:
        eventlog.log("warn", "JOURNAL_CONFLICT", "offline edits conflict", count=len(conflicts))
        _push_message(context, "system", settings.conflict_info)
    else:
        journal.clear(session_id)
        runtime["journal_active"] = False
        runtime["journal_pending"] = {}
        runtime["journal_files"] = []
        settings.journal_available = False
        if replayed:
            _push_message(context, "system", f"Restored {replayed} offline edit(s)")
        if skipped:
            eventlog.log("warn", "JOURNAL_CORRUPT", "skipped journal lines", count=skipped)


def _resolve_conflict(context, keep_mine: bool) -> None:
    conflicts = runtime.get("journal_conflicts") or []
    if not conflicts:
        return
    entry = conflicts.pop(0)
    session_id = runtime.get("journal_session", "")
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if keep_mine and session is not None:
        uid = entry.get("uid")
        kind = entry.get("kind")
        if isinstance(uid, str):
            if kind == "transform":
                matrix = entry.get("matrix")
                if isinstance(matrix, list) and len(matrix) == 16:
                    session.send_transform(uid, matrix)
                    obj = obj_sync.find(uid)
                    if obj is not None:
                        obj_sync.apply(uid, matrix)
                        runtime["object_shadow"][uid] = tuple(round(float(value), 6) for value in matrix)
            elif kind == "mesh_snapshot":
                payload = entry.get("payload")
                if isinstance(payload, str):
                    try:
                        data = base64.b64decode(payload.encode("ascii"))
                    except (ValueError, TypeError):
                        data = b""
                    obj = obj_sync.find(uid)
                    if obj is not None and obj.type == "MESH" and data:
                        session.send_mesh(obj, "snapshot", data, 0)
            elif kind == "material":
                slot = entry.get("slot")
                material = entry.get("material")
                if isinstance(slot, int) and isinstance(material, dict):
                    session.send_material(uid, slot, material)
    settings.conflict_count = len(conflicts)
    settings.conflict_info = (
        f"{len(conflicts)} offline edit(s) conflict with host changes" if conflicts else ""
    )
    if not conflicts and session_id:
        journal.clear(session_id)
        runtime["journal_active"] = False
        runtime["journal_pending"] = {}
        runtime["journal_files"] = []
        settings.journal_available = False
        _push_message(context, "system", "Offline journal resolved")


def _resume_peer(context, name: str) -> None:
    """Resend authoritative state to a client that reconnected."""
    session: SessionService | None = runtime.get("session")
    if session is None or not session.is_host:
        return
    objects = [obj for obj in context.scene.objects if not obj.get("bmp_ping")]
    uids = [obj.get("bmp_uuid") for obj in objects if obj.get("bmp_uuid")]
    session.send_resume(uids)
    runtime["shared_images"].clear()
    runtime["material_shadow"].clear()
    runtime["modifier_shadow"].clear()
    for obj in objects:
        _send_object_create(session, obj)
        uid = obj.get("bmp_uuid")
        if uid:
            session.send_transform(uid, [float(value) for row in obj.matrix_world for value in row])
        _send_object_details(session, obj)
    for obj in objects:
        if obj.type != "MESH":
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        try:
            mirror = MeshMirror()
            payload = mirror.full_snapshot(obj.data)
            session.send_mesh(obj, "snapshot", payload, 0)
            runtime["mirrors"][uid] = mirror
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    _push_message(context, "system", f"Resent scene to {name}")


def _apply_world_render(context, world_values, render_values) -> None:
    scene = context.scene
    if isinstance(world_values, dict) and scene.world is not None:
        world = scene.world
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
    if isinstance(render_values, dict):
        engine = render_values.get("engine")
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
            value = render_values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    setattr(scene.render, key, max(low, min(high, int(value))))
                except Exception:  # noqa: BLE001
                    pass


def _checkpoint_scene(context) -> dict:
    objects = []
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        body = _object_create_payload(obj)
        geometry = body.get("geometry") or b""
        objects.append(
            {
                "uuid": body["uuid"],
                "kind": body["kind"],
                "meta": body["meta"],
                "geometry": base64.b64encode(geometry).decode("ascii") if geometry else "",
            }
        )
    camera = context.scene.camera
    return {
        "format": 1,
        "addon": ADDON_VERSION,
        "blender": bpy.app.version_string,
        "code": context.window_manager.bmp.code,
        "objects": objects,
        "camera": camera.get("bmp_uuid") if camera is not None else None,
        "world": _world_values(context.scene.world),
        "render": _render_snapshot(context),
    }


def _restore_checkpoint(context, scene: dict) -> bool:
    objects = scene.get("objects")
    if not isinstance(objects, list):
        return False
    _clear_scene(context)
    for item in objects:
        if not isinstance(item, dict):
            continue
        try:
            geometry = base64.b64decode(str(item.get("geometry", "")).encode("ascii")) if item.get("geometry") else b""
        except (ValueError, TypeError):
            geometry = b""
        _apply_object_create(
            context,
            {
                "uuid": item.get("uuid"),
                "kind": item.get("kind"),
                "meta": item.get("meta") or {},
                "geometry": geometry,
            },
        )
    _apply_world_render(context, scene.get("world"), scene.get("render"))
    camera_uid = scene.get("camera")
    if isinstance(camera_uid, str):
        camera = obj_sync.find(camera_uid)
        if camera is not None:
            context.scene.camera = camera
    runtime["mirrors"].clear()
    runtime["edit_mirrors"].clear()
    runtime["object_shadow"].clear()
    runtime["material_shadow"].clear()
    runtime["modifier_shadow"].clear()
    runtime["tracked"].clear()
    runtime["known_objects"].clear()
    _prime_objects(context)
    for obj in context.scene.objects:
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        runtime["object_shadow"][uid] = tuple(round(float(value), 6) for row in obj.matrix_world for value in row)
        if obj.type == "MESH":
            mirror = MeshMirror()
            mirror.remember(obj.data)
            runtime["mirrors"][uid] = mirror
            runtime["tracked"].add(obj.name)
        for index, slot in enumerate(getattr(obj, "material_slots", []) or []):
            if slot.material is not None:
                runtime["material_shadow"][(uid, index)] = _material_signature(material_values(slot.material))
        if hasattr(obj, "modifiers"):
            runtime["modifier_shadow"][uid] = _modifier_signature(modifier_snapshot(obj), object_details(obj))
    session: SessionService | None = runtime.get("session")
    if session is not None and session.is_host:
        session.reset_leases()
        collections = _collection_snapshot(context)
        for name in session.peer_names():
            session.send_scene_reset(name, {"collections": collections})
            _queue_scene_bootstrap(context, name)
    return True


def _create_checkpoint(context, announce: bool = False) -> str:
    settings = context.window_manager.bmp
    scene = _checkpoint_scene(context)
    path = checkpoints.create(settings.code or "offline", scene)
    try:
        blend = path.with_suffix(".blend")
        bpy.ops.wm.save_as_mainfile(filepath=str(blend), copy=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    if announce:
        _push_message(context, "system", f"Checkpoint saved: {path.name}")
    eventlog.log("info", "CHECKPOINT_CREATE", "checkpoint saved", file=path.name, objects=len(scene["objects"]))
    return str(path)


def _maybe_auto_checkpoint(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None or not session.is_host or not context.scene.objects:
        return
    now = time.monotonic()
    if now - runtime.get("last_checkpoint", 0.0) < 600.0:
        return
    runtime["last_checkpoint"] = now
    try:
        _create_checkpoint(context, announce=False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


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
            runtime["modifier_shadow"][uid] = _modifier_signature(snapshot, object_details(obj))
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


MAX_RIG_BYTES = 8 * 1024 * 1024


def _pack_floats(values) -> str:
    return base64.b64encode(zlib.compress(np.asarray(values, dtype=np.float32).tobytes(), 6)).decode("ascii")


def _unpack_floats(value: str, count: int):
    try:
        raw = zlib.decompress(base64.b64decode(str(value).encode("ascii")))
    except (ValueError, TypeError, zlib.error):
        return None
    if len(raw) != count * 4:
        return None
    return np.frombuffer(raw, dtype=np.float32).copy()


def _armature_bones(obj) -> list:
    bones = []
    try:
        source = list(obj.data.bones)[:256]
    except (AttributeError, ReferenceError):
        return bones
    for bone in source:
        try:
            try:
                _axis, roll = bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())
                roll = float(roll)
            except (AttributeError, TypeError, ValueError):
                roll = 0.0
            bones.append(
                {
                    "name": bone.name[:64],
                    "parent": bone.parent.name[:64] if bone.parent else "",
                    "head": [float(value) for value in bone.head_local],
                    "tail": [float(value) for value in bone.tail_local],
                    "roll": roll,
                    "connected": bool(bone.use_connect),
                    "deform": bool(bone.use_deform),
                }
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return bones


def _mesh_rig_payload(obj) -> tuple[list, list]:
    mesh = obj.data
    count = len(mesh.vertices)
    if count == 0:
        return [], []
    arrays = []
    try:
        for group in list(obj.vertex_groups)[:64]:
            arrays.append((group.name[:64], np.zeros(count, dtype=np.float32)))
    except (AttributeError, ReferenceError):
        return [], []
    if arrays:
        for vertex in mesh.vertices:
            for item in vertex.groups:
                if 0 <= item.group < len(arrays):
                    arrays[item.group][1][vertex.index] = float(item.weight)
    groups = []
    used = 0
    for name, values in arrays:
        if not np.any(values):
            continue
        data = _pack_floats(values)
        used += len(data)
        if used > MAX_RIG_BYTES:
            break
        groups.append({"name": name, "count": count, "data": data})
    shapes = []
    try:
        key_blocks = list(mesh.shape_keys.key_blocks) if mesh.shape_keys is not None else []
    except (AttributeError, ReferenceError):
        key_blocks = []
    for key in key_blocks[1:32]:
        values = np.empty(count * 3, dtype=np.float32)
        try:
            key.data.foreach_get("co", values)
        except (AttributeError, RuntimeError):
            continue
        data = _pack_floats(values)
        if used + len(data) > MAX_RIG_BYTES:
            break
        used += len(data)
        shapes.append(
            {
                "name": key.name[:64],
                "count": count,
                "data": data,
                "value": float(key.value),
                "min": float(key.slider_min),
                "max": float(key.slider_max),
            }
        )
    return groups, shapes


def _apply_mesh_rig(obj, meta: dict) -> None:
    mesh = obj.data
    count = len(mesh.vertices)
    for item in meta.get("vertex_groups") or []:
        if not isinstance(item, dict) or item.get("count") != count:
            continue
        values = _unpack_floats(item.get("data", ""), count)
        if values is None:
            continue
        name = str(item.get("name", "Group"))[:64] or "Group"
        group = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
        buckets: dict[float, list[int]] = {}
        for index in np.nonzero(values > 0.0)[0]:
            weight = round(float(values[index]), 3)
            buckets.setdefault(weight, []).append(int(index))
        for weight, indices in buckets.items():
            try:
                group.add(indices, weight, "REPLACE")
            except (TypeError, RuntimeError):
                pass
    shapes = meta.get("shape_keys") or []
    if not shapes:
        return
    try:
        if mesh.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)
        for item in shapes:
            if not isinstance(item, dict) or item.get("count") != count:
                continue
            values = _unpack_floats(item.get("data", ""), count * 3)
            if values is None:
                continue
            key = obj.shape_key_add(name=str(item.get("name", "Key"))[:64] or "Key", from_mix=False)
            key.data.foreach_set("co", values)
            key.value = max(float(item.get("min", 0.0)), min(float(item.get("max", 1.0)), float(item.get("value", 0.0))))
            key.slider_min = float(item.get("min", 0.0))
            key.slider_max = float(item.get("max", 1.0))
    except (AttributeError, RuntimeError, TypeError):
        traceback.print_exc()


def _object_create_payload(obj) -> dict:
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
        try:
            encoded = json.dumps(meta, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            encoded = b""
        if len(encoded) > 48 * 1024:
            # The per-object JSON budget is tight for many textured slots.
            # Ship the basic values now and let the material poller send the
            # full graphs as separate updates.
            for item in materials:
                item.pop("nodes", None)
                item.pop("links", None)
        for slot in getattr(obj, "material_slots", [])[:16]:
            if slot.material is not None:
                _queue_material_images(slot.material)
    try:
        meta["modifiers"] = modifier_snapshot(obj)
        meta["details"] = object_details(obj)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
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
    elif kind == object_payload.KIND_ARMATURE:
        meta["bones"] = _armature_bones(obj)
        action = _action_snapshot(obj)
        if action is not None:
            meta["action"] = action
    if kind == object_payload.KIND_MESH:
        try:
            groups, shapes = _mesh_rig_payload(obj)
            if groups:
                meta["vertex_groups"] = groups
            if shapes:
                meta["shape_keys"] = shapes
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return {"uuid": uid, "kind": kind, "meta": meta, "geometry": geometry}


def _send_object_create(session: SessionService, obj) -> None:
    body = _object_create_payload(obj)
    uid = body["uuid"]
    kind = body["kind"]
    try:
        sent = session.send_object_create(uid, kind, body["meta"], body["geometry"])
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sent = False
    if not sent:
        if uid not in runtime["create_errors"]:
            runtime["create_errors"].add(uid)
            _push_message(
                bpy.context,
                "system",
                f"{obj.name[:40]} is too large to sync; reduce its geometry and re-import",
            )
    else:
        runtime["create_errors"].discard(uid)
    if kind == object_payload.KIND_MESH:
        runtime["tracked"].add(obj.name)


def _apply_armature_bones(context, obj, bones) -> None:
    if not isinstance(bones, list) or not bones:
        return
    previous = context.view_layer.objects.active
    previous_mode = context.mode
    selected = [item for item in context.selected_objects]
    try:
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = obj.data.edit_bones
        pending = [item for item in bones[:256] if isinstance(item, dict)]
        for _ in range(4):
            remaining = []
            for item in pending:
                name = str(item.get("name", ""))[:64]
                if not name or name in edit_bones:
                    continue
                parent = str(item.get("parent", ""))[:64]
                if parent and parent not in edit_bones:
                    remaining.append(item)
                    continue
                try:
                    bone = edit_bones.new(name)
                    bone.head = [float(value) for value in item.get("head", (0.0, 0.0, 0.0))][:3]
                    bone.tail = [float(value) for value in item.get("tail", (0.0, 0.0, 1.0))][:3]
                    bone.roll = float(item.get("roll", 0.0))
                    bone.use_deform = bool(item.get("deform", True))
                    if parent:
                        bone.parent = edit_bones.get(parent)
                        bone.use_connect = bool(item.get("connected", False))
                except (TypeError, ValueError, RuntimeError):
                    continue
            pending = remaining
            if not pending:
                break
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            for item in context.selected_objects:
                if item not in selected:
                    item.select_set(False)
            for item in selected:
                item.select_set(True)
            context.view_layer.objects.active = previous
            if previous is not None and previous_mode in {"EDIT_MESH", "SCULPT", "POSE", "EDIT_ARMATURE"}:
                bpy.ops.object.mode_set(mode=previous_mode)
        except Exception:  # noqa: BLE001
            pass


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
        elif kind == object_payload.KIND_ARMATURE:
            data = bpy.data.armatures.new(name)
            obj = bpy.data.objects.new(name, data)
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
        _apply_mesh_rig(obj, meta)
        if obj.data.shape_keys is not None:
            runtime["shape_shadow"][uid] = {
                key.name: round(float(key.value), 4) for key in obj.data.shape_keys.key_blocks
            }
    elif obj.type == "ARMATURE":
        _apply_armature_bones(context, obj, meta.get("bones"))
        runtime["pose_shadow"][uid] = {
            bone.name: (
                tuple(round(float(value), 5) for value in bone.location),
                tuple(round(float(value), 5) for value in bone.rotation_quaternion),
                tuple(round(float(value), 5) for value in bone.scale),
                str(bone.rotation_mode)[:16],
            )
            for bone in obj.pose.bones
        }
    modifiers = meta.get("modifiers")
    details = meta.get("details")
    if isinstance(modifiers, list) or isinstance(details, dict):
        _apply_modifier_stack(
            context,
            {
                "uuid": uid,
                "modifiers": modifiers if isinstance(modifiers, list) else [],
                "details": details,
            },
        )
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
    if obj.type == "ARMATURE" and isinstance(meta.get("action"), dict):
        _apply_action(context, {"uuid": uid, "action": meta.get("action")})


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
    if not settings.is_host and session.state != "connected":
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
        if not settings.is_host and uid not in runtime["owned_leases"]:
            # Only the host broadcasts unleased objects. A client that does
            # not hold the lease must not send idle/echo transforms, or it
            # silently takes ownership and fights the host.
            matrix = obj.matrix_world
            shadow[uid] = tuple(round(v, 6) for row in matrix for v in row)
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
            runtime["undo"].push(uid, "transform", previous, current)
            _note_rev(uid)
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
        _note_rev(uid)
        session.send_mesh(obj, "delta", payload, rev)


def _sync_sculpt_mesh(context, session: SessionService, obj) -> None:
    settings = context.window_manager.bmp
    uid = obj.get("bmp_uuid") or str(uuid.uuid4())
    obj["bmp_uuid"] = uid
    owner = runtime["lease_owners"].get(uid)
    if owner is not None and owner != settings.name:
        return
    if any(mod.type == "MULTIRES" for mod in obj.modifiers):
        if uid not in runtime["sculpt_warned"]:
            runtime["sculpt_warned"].add(uid)
            _push_message(context, "system", f"{obj.name[:40]}: multires sculpt is not synced; apply the modifier or use plain sculpt")
        return
    now = time.monotonic()
    if now - runtime.get("last_sculpt", 0.0) < 1.0 / 20.0:
        return
    runtime["last_sculpt"] = now
    mirror = runtime["mirrors"].get(uid)
    if mirror is None or not mirror.matches(obj.data):
        mirror = MeshMirror()
        runtime["mirrors"][uid] = mirror
        try:
            snapshot = mirror.full_snapshot(obj.data)
            _note_rev(uid)
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
        _note_rev(uid)
        session.send_mesh(obj, "delta", payload, rev)


def _sync_mesh(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_mesh or settings.paused or runtime.get("bootstrapping"):
        return
    active = context.active_object
    editing = active is not None and active.type == "MESH" and context.mode == "EDIT_MESH"
    sculpting = active is not None and active.type == "MESH" and context.mode == "SCULPT"
    if editing:
        _sync_edit_mesh(context, session, active)
    elif sculpting:
        _sync_sculpt_mesh(context, session, active)
    elif runtime["edit_mirrors"]:
        runtime["edit_mirrors"].clear()
        runtime["edit_lease_uid"] = None

    now = time.monotonic()
    if now - runtime["last_mesh"] < MESH_POLL:
        return
    runtime["last_mesh"] = now

    names = sorted(name for name in runtime["tracked"] if name in bpy.data.objects)
    if editing or sculpting:
        # The edit/sculpt fast path already streams this object.
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
            _note_rev(uid)
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
        _note_rev(uid)
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


def _send_subscription(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.is_host:
        return
    now = time.monotonic()
    if now - runtime.get("last_subscription", 0.0) < 0.5:
        return
    runtime["last_subscription"] = now
    view = _viewport_view(context)
    camera = None
    inverse = None
    aspect = 1.6
    if view is not None:
        from mathutils import Matrix

        matrix = Matrix((view["matrix"][0:4], view["matrix"][4:8], view["matrix"][8:12], view["matrix"][12:16]))
        camera = matrix.translation
        inverse = matrix.inverted()
        aspect = float(view.get("aspect", 1.6))
    uids = []
    modes = {}
    priorities = {}
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        priority = 2
        if camera is not None and inverse is not None:
            point = obj.matrix_world.translation
            distance = (point - camera).length
            local = inverse @ point
            depth = max(0.001, -float(local.z))
            visible = float(local.z) < 0.0 and abs(float(local.x)) / depth <= aspect and abs(float(local.y)) / depth <= 1.0
            if visible and distance < 50.0:
                priority = 0
            elif distance < 150.0:
                priority = 1
        uids.append(uid)
        priorities[uid] = priority
        if uid in runtime["muted"]:
            modes[uid] = "mute"
        elif settings.sync_visible_only and priority >= 2:
            modes[uid] = "mute"
        else:
            modes[uid] = "full"
    signature = (tuple(sorted((key, modes[key], priorities[key]) for key in uids)))
    if signature == runtime.get("subscription_sig"):
        return
    runtime["subscription_sig"] = signature
    session.send_subscription(uids, modes, priorities)


def _send_client_stats(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None or session.is_host:
        return
    now = time.monotonic()
    if now - runtime.get("last_client_stats", 0.0) < 1.0:
        return
    runtime["last_client_stats"] = now
    stats = session.stats()
    total_in = int(stats.get("bytes_in", 0))
    total_out = int(stats.get("bytes_out", 0))
    previous_in = int(runtime.get("stats_in", total_in))
    previous_out = int(runtime.get("stats_out", total_out))
    elapsed = max(0.1, now - float(runtime.get("stats_time", now)))
    runtime["stats_in"] = total_in
    runtime["stats_out"] = total_out
    runtime["stats_time"] = now
    times = sorted(runtime.get("apply_times", [])[-120:])
    p95 = times[int(len(times) * 0.95)] if times else 0.0
    fps = 0.0
    try:
        fps = float(bpy.context.scene.render.fps)
    except Exception:  # noqa: BLE001
        fps = 60.0
    session.send_client_stats(
        {
            "apply_ms_p95": round(float(p95), 2),
            "fps": round(max(1.0, min(240.0, fps)), 1),
            "rx_kbps": round(max(0.0, (total_in - previous_in) / 1024.0 / elapsed), 1),
            "tx_kbps": round(max(0.0, (total_out - previous_out) / 1024.0 / elapsed), 1),
            "queue_depth": 0,
        }
    )


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
        "playing": bool(getattr(context.scene, "is_animation_playing", False)),
        "fps": int(context.scene.render.fps),
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


def _scene_stats(context) -> dict:
    stats = {
        "objects": 0,
        "meshes": 0,
        "vertices": 0,
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "collections": len(bpy.data.collections),
    }
    for obj in context.scene.objects:
        stats["objects"] += 1
        if obj.type != "MESH":
            continue
        stats["meshes"] += 1
        try:
            stats["vertices"] += len(obj.data.vertices)
        except (AttributeError, ReferenceError):
            pass
    runtime["scene_stats"] = stats
    runtime["last_scene_stats"] = time.monotonic()
    return stats


def _heartbeat_tick(context) -> None:
    now = time.monotonic()
    if now - runtime.get("heartbeat_last", 0.0) < 2.0:
        return
    runtime["heartbeat_last"] = now
    settings = context.window_manager.bmp
    heartbeat.write(
        settings.state,
        settings.code,
        {"blender": bpy.app.version_string, "addon": ADDON_VERSION},
    )
    entries = runtime.get("crash_entries")
    if entries and not settings.crash_notice:
        latest = entries[0]
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(latest.get("stamp", 0))))
        settings.crash_notice = (
            f"Blendermp closed unexpectedly on {stamp} (state {latest.get('state', '?')}). "
            "Check for a recovery checkpoint."
        )
        settings.recovery_available = True
        _push_message(context, "system", settings.crash_notice)
        eventlog.log("warn", "CRASH_DETECTED", settings.crash_notice, entries=entries[:4])
    if runtime.get("journal_files"):
        settings.journal_available = True
        if not settings.crash_notice:
            settings.crash_notice = (
                "Unsynced offline edits were found. Reconnect to the original room, "
                "then restore the journal."
            )
            _push_message(context, "system", settings.crash_notice)


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
        if live_state in {"offline", "hosting", "joining", "connected", "reconnecting"}:
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
            "error_code": settings.last_error_code,
            "identity_public": getattr(session, "identity_public", "") if session else "",
            "identity_signature": getattr(session, "identity_signature", "") if session else "",
            "identity_descriptor": json.dumps(getattr(session, "identity_descriptor", {}) or {}, separators=(",", ":")) if session else "{}",
            "scene": _scene_stats(context) if now - runtime.get("last_scene_stats", 0.0) > 5.0 else runtime.get("scene_stats", {}),
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
        _heartbeat_tick(context)
        session: SessionService | None = runtime.get("session")
        _update_timing(session)
        inbox = runtime["inbox"]
        _expire_pings(context)
        while True:
            try:
                kind, body = inbox.get_nowait()
            except queue.Empty:
                break
            started = time.monotonic()
            _handle_event(context, kind, body)
            times = runtime.setdefault("apply_times", [])
            times.append((time.monotonic() - started) * 1000.0)
            if len(times) > 240:
                del times[: len(times) - 240]
        if session is not None:
            _sync_selection(context)
            _sync_transforms(context)
            _sync_mesh(context)
            _sync_edit_selection(context)
            _sync_materials(context)
            _sync_modifiers(context)
            _sync_pose(context)
            _sync_shape_keys(context)
            _sync_actions(context)
            _process_image_queue(context)
            _send_presence(context)
            _send_subscription(context)
            _send_client_stats(context)
        _journal_capture(context)
        _maybe_auto_checkpoint(context)
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
            settings.last_error_code = getattr(session, "failure_code", "") or "INTERNAL"
            stop_session(context)
        elif session is not None and session.failed:
            settings = context.window_manager.bmp
            settings.last_error = session.failure_reason or "connection failed"
            settings.last_error_code = getattr(session, "failure_code", "") or "INTERNAL"
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


class BMP_OT_toggle_mute(Operator):
    bl_idname = "bmp.toggle_mute"
    bl_label = "Mute Object Sync"
    bl_description = "Stop receiving updates for the active object on this client"

    def execute(self, context):
        obj = context.active_object
        uid = obj.get("bmp_uuid") if obj is not None else None
        if not uid:
            self.report({"WARNING"}, "Select a synced object first")
            return {"CANCELLED"}
        muted = runtime.setdefault("muted", set())
        if uid in muted:
            muted.discard(uid)
            self.report({"INFO"}, f"{obj.name} sync resumed")
        else:
            muted.add(uid)
            self.report({"INFO"}, f"{obj.name} sync muted")
        runtime["subscription_sig"] = None
        return {"FINISHED"}


class BMP_OT_undo_shared(Operator):
    bl_idname = "bmp.undo_shared"
    bl_label = "Undo Shared Edit"
    bl_description = "Undo your last shared edit if nobody else changed that object since"

    def execute(self, context):
        log = runtime.get("undo")
        entry = log.pop() if log is not None else None
        if entry is None:
            self.report({"INFO"}, "Nothing to undo")
            return {"CANCELLED"}
        obj = obj_sync.find(entry.uid)
        if obj is None:
            self.report({"WARNING"}, "The object no longer exists")
            return {"CANCELLED"}
        if entry.kind != "transform":
            self.report({"WARNING"}, "That edit cannot be undone safely")
            return {"CANCELLED"}
        values = obj_sync.normalize(entry.after)
        current = tuple(round(float(value), 6) for row in obj.matrix_world for value in row)
        expected = tuple(round(float(value), 6) for value in values) if values else ()
        if current != expected:
            self.report({"WARNING"}, "Changed by someone else; not undone")
            return {"CANCELLED"}
        before = obj_sync.normalize(entry.before)
        if before is None or not obj_sync.apply(entry.uid, before):
            self.report({"ERROR"}, "Could not restore the previous transform")
            return {"CANCELLED"}
        runtime["object_shadow"][entry.uid] = tuple(round(float(value), 6) for value in before)
        session: SessionService | None = runtime.get("session")
        if session is not None:
            _note_rev(entry.uid)
            session.send_transform(entry.uid, before)
        self.report({"INFO"}, f"Undid {obj.name}'s last edit")
        return {"FINISHED"}


class BMP_OT_conflict_keep_mine(Operator):
    bl_idname = "bmp.conflict_keep_mine"
    bl_label = "Keep My Offline Edit"
    bl_description = "Replay the next offline edit even though the host changed that object"

    def execute(self, context):
        _resolve_conflict(context, True)
        return {"FINISHED"}


class BMP_OT_conflict_take_host(Operator):
    bl_idname = "bmp.conflict_take_host"
    bl_label = "Take Host Version"
    bl_description = "Discard the next conflicting offline edit"

    def execute(self, context):
        _resolve_conflict(context, False)
        return {"FINISHED"}


class BMP_OT_checkpoint_create(Operator):
    bl_idname = "bmp.checkpoint_create"
    bl_label = "Create Checkpoint"
    bl_description = "Save a recovery checkpoint of this scene"

    def execute(self, context):
        try:
            path = _create_checkpoint(context, announce=True)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, f"Checkpoint failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Checkpoint saved: {path}")
        return {"FINISHED"}


class BMP_OT_checkpoint_restore(Operator):
    bl_idname = "bmp.checkpoint_restore"
    bl_label = "Restore Checkpoint"
    bl_description = "Replace this scene with the newest checkpoint and resync peers"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        settings = context.window_manager.bmp
        if not settings.is_host and settings.state != "offline":
            self.report({"ERROR"}, "Only the host can restore a shared scene")
            return {"CANCELLED"}
        path = checkpoints.latest(settings.code or "offline")
        if path is None:
            self.report({"INFO"}, "No checkpoint found")
            return {"CANCELLED"}
        scene = checkpoints.read(path)
        if not scene or not _restore_checkpoint(context, scene):
            self.report({"ERROR"}, "Checkpoint is corrupt or unsupported")
            return {"CANCELLED"}
        _push_message(context, "system", f"Restored checkpoint {path.name}")
        eventlog.log("info", "CHECKPOINT_RESTORE", "checkpoint restored", file=path.name)
        return {"FINISHED"}


class BMP_OT_restore_journal(Operator):
    bl_idname = "bmp.restore_journal"
    bl_label = "Restore Offline Edits"
    bl_description = "Replay unsynced offline edits after reconnecting to the original room"

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        settings = context.window_manager.bmp
        if session is None or settings.state not in {"connected"}:
            self.report({"WARNING"}, "Reconnect to the original room first")
            return {"CANCELLED"}
        _replay_journal(context, {"revs": runtime.get("scene_revs", {})})
        if settings.conflict_count:
            self.report({"INFO"}, f"{settings.conflict_count} edit(s) need a conflict decision")
        else:
            settings.crash_notice = ""
            settings.journal_available = False
            runtime["journal_files"] = []
            self.report({"INFO"}, "Offline edits restored")
        return {"FINISHED"}


class BMP_OT_recover_checkpoints(Operator):
    bl_idname = "bmp.recover_checkpoints"
    bl_label = "Recover Checkpoints"
    bl_description = "Open the newest local recovery checkpoint, if one exists"

    def execute(self, context):
        settings = context.window_manager.bmp
        try:
            latest = checkpoints.latest(settings.code or "offline") or checkpoints.latest_any()
        except OSError:
            latest = None
        if latest is not None:
            scene = checkpoints.read(latest)
            if scene and _restore_checkpoint(context, scene):
                settings.crash_notice = ""
                settings.recovery_available = False
                eventlog.log("info", "CHECKPOINT_RECOVER", "recovered checkpoint", file=latest.name)
                _push_message(context, "system", f"Recovered checkpoint {latest.name}")
                return {"FINISHED"}
        try:
            files = sorted(
                (
                    path
                    for path in control.checkpoints_dir().rglob("*.blend")
                    if path.is_file()
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            files = []
        if not files:
            settings.crash_notice = ""
            settings.recovery_available = False
            eventlog.log("info", "CHECKPOINT_NONE", "no recovery checkpoint found")
            self.report({"INFO"}, "No recovery checkpoint found; Blender's autosave may still help")
            return {"CANCELLED"}
        path = files[0]
        eventlog.log("info", "CHECKPOINT_OPEN", "opening recovery checkpoint", checkpoint=path.name)
        settings.crash_notice = ""
        settings.recovery_available = False
        try:
            bpy.ops.wm.open_mainfile(filepath=str(path))
        except Exception as exc:  # noqa: BLE001
            eventlog.log("error", "CHECKPOINT_FAILED", str(exc))
            self.report({"ERROR"}, f"Could not open checkpoint: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class BMP_OT_dismiss_crash(Operator):
    bl_idname = "bmp.dismiss_crash"
    bl_label = "Dismiss Crash Notice"
    bl_description = "Hide the unexpected-exit notice"

    def execute(self, context):
        settings = context.window_manager.bmp
        for session_id in runtime.get("journal_files", []) or []:
            journal.clear(session_id)
        runtime["journal_files"] = []
        runtime["journal_active"] = False
        runtime["journal_pending"] = {}
        settings.journal_available = False
        settings.crash_notice = ""
        settings.recovery_available = False
        return {"FINISHED"}


SECRET_KEYS = {
    "hostkey",
    "host_key",
    "relaytoken",
    "relay_token",
    "token",
    "pin",
    "password",
    "secret",
    "privatekey",
    "private_key",
}
PATH_USER_RE = re.compile(r"([A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/])[^\\/\s\"']+", re.IGNORECASE)
SECRET_LINE_RE = re.compile(
    r'("?(?:hostKey|relay_token|token|pin|password)"?\s*[:=]\s*)"[^"]*"', re.IGNORECASE
)


def _redact_text(text: str) -> str:
    text = PATH_USER_RE.sub(r"\1<user>", text)
    text = text.replace(str(control.data_dir()), "<appdata>")
    text = text.replace(str(Path.home()), "<home>")
    text = SECRET_LINE_RE.sub(r'\1"<redacted>"', text)
    return text


def _redact_json(value):
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in SECRET_KEYS else _redact_json(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value[:256]]
    if isinstance(value, (bytes, bytearray)):
        return _redact_text(bytes(value).decode("utf-8", "replace"))
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _tail_lines(path: Path, count: int = 500) -> str:
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    return "\n".join(_redact_text(line) for line in lines[-count:])


def _diagnostics_system() -> dict:
    data = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor()[:200],
        "python": platform.python_version(),
        "blender": bpy.app.version_string,
        "blender_build": bpy.app.build_platform,
        "addon": ADDON_VERSION,
    }
    try:
        import gpu

        data["gpu_vendor"] = gpu.platform.vendor_get()
        data["gpu_renderer"] = gpu.platform.renderer_get()
        data["gpu_version"] = gpu.platform.version_get()
    except Exception:  # noqa: BLE001
        pass
    try:
        data["os_version"] = platform.win32_ver()[1] if os.name == "nt" else platform.release()
    except Exception:  # noqa: BLE001
        pass
    return data


def export_diagnostics(context, filepath: str) -> dict:
    """Write a redacted support zip. Returns a small summary."""
    settings = context.window_manager.bmp
    session: SessionService | None = runtime.get("session")
    stats = session.stats() if session else {}
    network = {
        "state": settings.state,
        "relay": bool(stats.get("relay", False)),
        "rtt_ms": stats.get("rtt_ms"),
        "bytes_in": stats.get("bytes_in", 0),
        "bytes_out": stats.get("bytes_out", 0),
        "peers": len(stats.get("peers", []) or []),
        "leases": len(runtime.get("lease_owners", {})),
        "fast_mode": bool(runtime.get("fast_mode", True)),
        "flags": {
            "sync_transforms": bool(settings.sync_transforms),
            "sync_mesh": bool(settings.sync_mesh),
            "sync_materials": bool(settings.sync_materials),
            "sync_modifiers": bool(settings.sync_modifiers),
        },
        "last_error": settings.last_error,
        "last_error_code": settings.last_error_code,
    }
    scene = _scene_stats(context)
    versions = {
        "addon": ADDON_VERSION,
        "blender": bpy.app.version_string,
        "protocol": 2,
        "app": "",
    }
    status_body = {}
    try:
        status_body = json.loads(control.status_path().read_text(encoding="utf-8"))
        if isinstance(status_body, dict):
            versions["app"] = str(status_body.get("addon", ""))
    except (OSError, json.JSONDecodeError):
        status_body = {}

    temp = filepath + ".tmp"
    entries = 0
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as bundle:
        def add_json(name: str, value) -> None:
            nonlocal entries
            bundle.writestr(name, json.dumps(_redact_json(value), indent=2, sort_keys=True))
            entries += 1

        def add_text(name: str, text: str) -> None:
            nonlocal entries
            bundle.writestr(name, _redact_text(text))
            entries += 1

        add_json("versions.json", versions)
        add_json("system.json", _diagnostics_system())
        add_json("health.json", network)
        add_json("scene.json", scene)
        add_json("status.json", status_body)
        add_json("crash.json", {"entries": runtime.get("crash_entries", [])})
        for name in ("settings.json", "session.json"):
            path = control.data_dir() / name
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(body, dict):
                add_json(name, body)
        for name in ("ui.log", "blender.log", "events.log", "events.log.1"):
            text = _tail_lines(control.data_dir() / name)
            if text:
                add_text(name, text)
        add_text(
            "README.txt",
            "Blendermp support bundle\n"
            "Absolute user paths, host keys, relay tokens and PINs are redacted.\n"
            "Attach this zip to a bug report together with what both sides were doing.\n",
        )
    os.replace(temp, filepath)
    eventlog.log("info", "DIAGNOSTICS_EXPORT", "support bundle written", entries=entries)
    return {"path": filepath, "entries": entries, "bytes": os.path.getsize(filepath)}


class BMP_OT_export_log(Operator, ExportHelper):
    bl_idname = "bmp.export_log"
    bl_label = "Export diagnostics bundle"
    bl_description = "Write a redacted support zip (versions, logs, stats) for troubleshooting"
    filename_ext = ".zip"
    filter_glob: StringProperty(default="*.zip", options={"HIDDEN"})

    def execute(self, context):
        try:
            result = export_diagnostics(context, self.filepath)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            eventlog.log("error", "DIAGNOSTICS_FAILED", str(exc))
            self.report({"ERROR"}, f"Could not write diagnostics: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Diagnostics written ({result['entries']} files, {result['bytes']} bytes)")
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
    BMP_OT_recover_checkpoints,
    BMP_OT_dismiss_crash,
    BMP_OT_conflict_keep_mine,
    BMP_OT_conflict_take_host,
    BMP_OT_undo_shared,
    BMP_OT_toggle_mute,
    BMP_OT_checkpoint_create,
    BMP_OT_checkpoint_restore,
    BMP_OT_restore_journal,
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
    try:
        keyconfig = bpy.context.window_manager.keyconfigs.addon
        if keyconfig is not None:
            keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
            item = keymap.keymap_items.new("bmp.undo_shared", "Z", "PRESS", ctrl=True, alt=True)
            addon_keymaps.append((keymap, item))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        runtime["crash_entries"] = heartbeat.unclean_exits()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        runtime["crash_entries"] = []
    try:
        pending_journals = journal.pending()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        pending_journals = []
    if pending_journals:
        runtime["journal_session"] = pending_journals[0][0]
        runtime["journal_active"] = True
        runtime["journal_files"] = [name for name, _path in pending_journals[:8]]
    atexit.register(heartbeat.mark_clean)
    try:
        heartbeat.write("starting", "", {"blender": bpy.app.version_string, "addon": ADDON_VERSION})
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    ui.register()
    runtime["started"] = time.monotonic()
    bpy.app.timers.register(bmp_tick, first_interval=0.5, persistent=True)
    _register_remote_draw()
    bpy.app.handlers.frame_change_post.append(on_frame_change)


def unregister() -> None:
    if not getattr(bpy, "_blendermp_active", False):
        return
    bpy._blendermp_active = False
    heartbeat.mark_clean()
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
