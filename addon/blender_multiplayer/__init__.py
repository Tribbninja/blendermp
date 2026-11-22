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
import copy
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
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper

from . import control, ui
from .core import chunker
from .core import accessibility
from .core import ai
from .core import compression
from .core import asset_bundle
from .core import bookmarks
from .core import broadcast
from .core import checkpoints
from .core import cross_engine
from .core import eventlog
from .core import heartbeat
from .core import image_payload
from .core import journal
from .core import undo as undo_log
from .core import material as material_payload
from .core import object_payload
from .core import perf
from .core import recording
from .core import renderfarm
from .core import renderjob
from .core import render_payload
from .core.version import ADDON_VERSION
from .session import MAX_MESH, SessionService
from .sync import objects as obj_sync
from .sync.mesh import EditMirror, MeshMirror, snapshot_bmesh

bl_info = {
    "name": "Blendermp",
    "author": "Blendermp",
    "version": (0, 11, 7),
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
SELECTION_POLL = 0.05
LEASE_RENEW = 15.0
LEASE_IDLE = 30.0
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


def effective_color(name: str, color, colorblind: bool = False) -> tuple:
    if colorblind:
        digest = hashlib.sha256(str(name).encode("utf-8")).digest()
        return accessibility.palette()[digest[0] % len(accessibility.palette())]
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
    "lock_notified": set(),
    "owned_leases": set(),
    "last_selection": 0.0,
    "remote_batches": [],
    "remote_point_batches": [],
    "remote_labels": [],
    "remote_shader": None,
    "tooltip_shader": None,
    "last_remote": 0.0,
    "view_smoothers": {},
    "remote_debug": {},
    "remote_selections": {},
    "remote_signature": None,
    "remote_points": [],
    "lasers": {},
    "laser_active": False,
    "recording": None,
    "broadcast": None,
    "bridge": None,
    "bridge_exporter": None,
    "bridge_shadow": {},
    "bridge_last": 0.0,
    "bridge_materials_at": 0.0,
    "farm_server": None,
    "farm_client": None,
    "farm_busy": False,
    "farm_last": 0.0,
    "replaying": False,
    "replay_cancel": None,
    "perf": perf.PerfRegistry(),
    "last_tick": 0.0,
    "ai_session": None,
    "ai_queue": queue.Queue(),
    "ai_pending_edits": [],
    "overlay_scale": 1.0,
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
    "draw_hover": None,
    "hover_info": None,
    "hover_mouse": None,
    "hover_op_active": False,
    "hover_op_try": 0.0,
    "hover_last_update": 0.0,
    "transform_smoothers": {},
    "mesh_smoothers": {},
    "lease_renew": 0.0,
    "tick_fps": 60.0,
    "tick_count": 0,
    "tick_fps_at": 0.0,
    "lease_activity": {},
    "lease_suppress_until": 0.0,
    "animated_cache": {},
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
    "bookmarks_loaded": False,
    "bundle_offers": {},
    "bundle_imported": set(),
    "bundle_sent": {},
    "transfer_clear_at": 0.0,
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
    "last_image_scan": 0.0,
    "last_presence": 0.0,
    "presence_shadow": {},
    "presence_full_at": 0.0,
    "control_checked": 0.0,
    "started": 0.0,
    "session_started": 0.0,
    "relay_bridge": None,
    "render_manager": None,
    "render_request": None,
    "render_active": False,
    "render_queue": [],
    "render_busy": False,
    "render_last_poll": 0.0,
    "render_job_id": "",
    "render_output": "",
    "render_format": "MP4",
    "render_error": "",
    "render_done": 0,
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
    view_corners: FloatVectorProperty(size=21)
    has_corners: BoolProperty()
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
    scene_hash: StringProperty()


class BMPMessage(PropertyGroup):
    author: StringProperty()
    text: StringProperty()
    stamp: StringProperty()


class BMPBookmark(PropertyGroup):
    name: StringProperty()
    matrix: FloatVectorProperty(size=16)
    aspect: FloatProperty(default=1.6)
    persp: StringProperty(default="PERSP")
    distance: FloatProperty(default=7.0)


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
    pending_fingerprint: StringProperty()
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
    show_cameras: BoolProperty(name="Show remote cameras", default=True)
    show_selections: BoolProperty(name="Show remote selections", default=True)
    share_view: BoolProperty(name="Share my viewport camera", default=True)
    show_labels: BoolProperty(name="Show name tags", default=True)
    show_hover_info: BoolProperty(name="Show who is editing on hover", default=True)
    show_lasers: BoolProperty(name="Show laser pointers", default=False)
    overlay_opacity: FloatProperty(name="Overlay opacity", default=0.85, min=0.2, max=1.0)
    overlay_scale: FloatProperty(name="Overlay scale", default=1.0, min=0.5, max=2.0)
    colorblind_safe: BoolProperty(name="Colour-blind-safe colours", default=False)
    scene_mismatch: StringProperty(name="Scene mismatch", default="")
    tick_errors: StringProperty(name="Tick errors", default="")
    ai_enabled: BoolProperty(name="Enable AI copilot", default=False)
    ai_url: StringProperty(name="Ollama URL", default="http://127.0.0.1:11434")
    ai_model: StringProperty(name="Ollama model", default="qwen2.5:7b")
    ai_timeout: IntProperty(name="Timeout (seconds)", default=180, min=10, max=900)
    ai_prompt: StringProperty(name="Ask the copilot", default="")
    ai_status: StringProperty(name="AI status", default="idle")
    ai_pending_text: StringProperty(name="AI pending edits", default="")
    ai_pending_count: IntProperty(name="AI pending count", default=0, min=0)
    ai_reply: StringProperty(name="AI reply", default="")
    recording_active: BoolProperty(name="Recording", default=False)
    recording_info: StringProperty(name="Recording info", default="")
    replay_speed: FloatProperty(name="Replay speed", default=1.0, min=0.1, max=10.0)
    broadcast_active: BoolProperty(name="Live broadcast", default=False)
    broadcast_port: IntProperty(name="Broadcast port", default=7102, min=1024, max=65535)
    broadcast_url: StringProperty(name="Broadcast URL", default="")
    bridge_active: BoolProperty(name="Cross-engine bridge", default=False)
    bridge_engine: EnumProperty(
        name="Engine",
        items=[
            ("unity", "Unity", "Unity receiver"),
            ("unreal", "Unreal", "Unreal receiver"),
            ("godot", "Godot", "Godot receiver"),
        ],
        default="unity",
    )
    bridge_port: IntProperty(name="Bridge port", default=7103, min=1024, max=65535)
    bridge_meshes: BoolProperty(name="Send meshes", default=True)
    bridge_url: StringProperty(name="Bridge URL", default="")
    bridge_clients: IntProperty(name="Bridge clients", default=0, min=0)
    farm_mode: EnumProperty(
        name="Render farm mode",
        items=[
            ("host", "Host", "Share this PC as the render farm host"),
            ("client", "Join", "Render frames for another PC"),
        ],
        default="host",
    )
    farm_active: BoolProperty(name="Render farm active", default=False)
    farm_role: StringProperty(name="Render farm role", default="")
    farm_port: IntProperty(name="Farm port", default=7110, min=1024, max=65535)
    farm_url: StringProperty(name="Farm URL", default="")
    farm_token: StringProperty(name="Farm token", default="")
    farm_workers: IntProperty(name="Farm workers", default=0, min=0)
    farm_progress: StringProperty(name="Farm progress", default="")
    farm_output: StringProperty(name="Output folder", default="", subtype="DIR_PATH")
    farm_frame_start: IntProperty(name="Start frame", default=1, min=0)
    farm_frame_end: IntProperty(name="End frame", default=250, min=0)
    farm_frame_step: IntProperty(name="Frame step", default=1, min=1)
    farm_use_scene_range: BoolProperty(name="Use scene frame range", default=True)
    farm_host_worker: BoolProperty(name="This PC renders too", default=False)
    farm_client_url: StringProperty(name="Farm URL", default="")
    farm_client_token: StringProperty(name="Farm token", default="")
    render_progress: StringProperty(name="Render progress", default="")
    render_request: StringProperty(name="Render request", default="")
    host_name: StringProperty()
    sync_transforms: BoolProperty(name="Sync object transforms", default=True)
    sync_animation_values: BoolProperty(name="Sync live pose/shape values", default=False)
    users: CollectionProperty(type=BMPUser)
    messages: CollectionProperty(type=BMPMessage)
    show_users: BoolProperty(default=True)
    show_chat: BoolProperty(default=True)
    lease_info: StringProperty()
    lease_requester: StringProperty()
    mesh_info: StringProperty()
    role: StringProperty(default="editor")
    internet_mode: BoolProperty(name="Internet mode", default=False)
    lan_fast: BoolProperty(name="LAN fast path", default=False)
    overlay_info: StringProperty()
    direct_code: StringProperty()
    direct_codes: StringProperty()
    crash_notice: StringProperty()
    recovery_available: BoolProperty(default=False)
    conflict_count: IntProperty(default=0, min=0)
    conflict_info: StringProperty()
    journal_available: BoolProperty(default=False)
    bookmark_name: StringProperty(name="Bookmark name", default="View")
    bookmarks: CollectionProperty(type=BMPBookmark)
    apply_bookmarks: BoolProperty(name="Accept pushed views", default=True)
    bundle_count: IntProperty(default=0, min=0)
    bundle_info: StringProperty()
    bundle_sender: StringProperty()
    bundle_id: StringProperty()
    transfer_active: BoolProperty(default=False)
    transfer_info: StringProperty()
    transfer_percent: FloatProperty(default=0.0, min=0.0, max=100.0)


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
    if author == "system":
        _broadcast_event("system", {"name": author, "text": text})


def _broadcast_event(kind: str, body) -> None:
    """Publish one event to the live browser view, if it is running."""
    server = runtime.get("broadcast")
    if server is None:
        return
    if kind == "transform":
        now = time.monotonic()
        if now - runtime.get("last_broadcast_transform", 0.0) < 0.1:
            return
        runtime["last_broadcast_transform"] = now
    try:
        server.publish(kind, body)
    except Exception:  # noqa: BLE001
        pass


def _record_local(kind: str, body) -> None:
    """Tap a locally sent session event into the recorder and live view."""
    recorder = runtime.get("recording")
    if recorder is not None and not runtime.get("replaying"):
        try:
            recorder.record(kind, body)
        except Exception:  # noqa: BLE001
            pass
    _broadcast_event(kind, body)


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


def _same_lan(host_ip: str) -> bool:
    """True when the host address is on this machine's /24 (typical home LAN)."""
    try:
        local = _local_ip()
    except Exception:  # noqa: BLE001
        return False
    if not host_ip or host_ip.startswith("127.") or local.startswith("127."):
        return False
    local_parts = local.split(".")
    host_parts = host_ip.split(".")
    return (
        len(local_parts) == 4
        and len(host_parts) == 4
        and local_parts[:3] == host_parts[:3]
    )


# --------------------------------------------------------------------------- session control
def start_host(
    context,
    code: str | None = None,
    port: int | None = None,
    relay_url: str | None = None,
    relay_token: str | None = None,
    mesh_url: str | None = None,
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
    settings.show_lasers = False
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
            mesh_url=mesh_url or relay_url,
        )
        session.start()
    except Exception:
        if bridge is not None:
            bridge.stop()
        raise
    runtime["relay_bridge"] = bridge
    runtime["session"] = session
    session.event_sink = _record_local
    runtime["mirrors"].clear()
    runtime["object_shadow"].clear()
    _prime_objects(context)
    runtime["last_selection"] = 0.0
    runtime["presence_shadow"].clear()
    runtime["presence_full_at"] = 0.0
    _sync_selection(context)
    _upsert_user(context, name, tuple(settings.color), host=True)
    _push_message(context, "system", f"Hosting room {code}")
    eventlog.log("info", "HOST_START", "hosting", room=code, relay=bool(relay))
    _ensure_timer()


def start_join(
    context,
    code: str,
    relay_url: str | None = None,
    fingerprint: str | None = None,
    prefer_direct: bool = False,
    direct_code: str | None = None,
    direct_codes: list[str] | None = None,
    mesh_url: str | None = None,
) -> None:
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
    settings.show_lasers = False
    settings.last_error = ""
    settings.last_error_code = ""
    settings.pending_fingerprint = ""
    runtime["last_join"] = {
        "code": settings.code,
        "relay_url": relay_url or "",
        "fingerprint": fingerprint or "",
        "prefer_direct": bool(prefer_direct),
        "direct_code": direct_code or "",
        "direct_codes": [code for code in (direct_codes or []) if isinstance(code, str)][:4],
    }
    candidates = [code for code in (direct_codes or []) if isinstance(code, str)][:4]
    if not candidates and direct_code:
        candidates = [direct_code]
    chosen = ""
    if prefer_direct:
        from .core.roomcode import decode_room

        for candidate in candidates:
            try:
                info = decode_room(candidate)
            except Exception:  # noqa: BLE001
                continue
            if _same_lan(info.ip):
                chosen = candidate
                break
    direct_first = bool(prefer_direct and chosen)
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
            fingerprint=fingerprint or None,
            pin=settings.pin,
            relay=relay,
            direct_code=chosen or None,
            direct_first=bool(direct_first),
            mesh_url=mesh_url or relay_url,
        )
        session.start()
    except Exception:
        if bridge is not None:
            bridge.stop()
        raise
    runtime["relay_bridge"] = bridge
    runtime["session"] = session
    session.event_sink = _record_local
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
    runtime["hover_op_active"] = False
    runtime["hover_info"] = None
    manager = runtime.get("render_manager")
    if manager is not None:
        try:
            manager.cancel()
        except Exception:  # noqa: BLE001
            pass
    runtime["render_manager"] = None
    runtime["render_request"] = None
    runtime["render_active"] = False
    runtime["render_queue"] = []
    runtime["render_busy"] = False
    runtime["render_job_id"] = ""
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
    runtime["lasers"].clear()
    runtime["laser_active"] = False
    runtime["selection_digest"].clear()
    runtime["last_edit_sel"] = 0.0
    runtime["selection"].clear()
    runtime["hidden_objects"].clear()
    runtime["deselected"].clear()
    runtime["lock_notified"].clear()
    runtime["last_selection"] = 0.0
    runtime["presence_shadow"].clear()
    runtime["presence_full_at"] = 0.0
    runtime["lease_renew"] = 0.0
    runtime["bootstraps"].clear()
    runtime["bootstrapping"] = False
    runtime["bootstrap_deadline"] = 0.0
    settings = context.window_manager.bmp
    runtime["bundle_offers"].clear()
    runtime["bundle_imported"].clear()
    runtime["bundle_sent"].clear()
    settings.bundle_count = 0
    settings.bundle_info = ""
    settings.bundle_sender = ""
    settings.bundle_id = ""
    settings.transfer_active = False
    settings.transfer_percent = 0.0
    settings.transfer_info = ""
    runtime["transfer_clear_at"] = 0.0
    runtime["edit_lease_uid"] = None
    runtime["last_edit"] = 0.0
    settings.rtt_ms = 0
    settings.users.clear()
    try:
        recorder = runtime.get("recording")
        if recorder is not None:
            recorder.stop()
            runtime["recording"] = None
            settings.recording_active = False
        _stop_broadcast(settings)
        _stop_bridge(settings)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
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
        codes = body.get("direct_codes")
        settings.direct_codes = (
            ",".join(str(code) for code in codes if isinstance(code, str))[:200]
            if isinstance(codes, list)
            else settings.direct_code
        )
        settings.fingerprint = body.get("fingerprint", "")
        settings.verify_code = body.get("short", "")
        _push_message(context, "system", f"Room {settings.code} ready")
    elif kind == "host_key":
        settings.fingerprint = body.get("fingerprint", "")
        settings.verify_code = body.get("short", "")
        _push_message(context, "system", f"Verify host code: {settings.verify_code}")
    elif kind == "cert_changed":
        actual = body.get("fingerprint", "")
        if not isinstance(actual, str) or len(actual) != 64:
            actual = ""
        settings.pending_fingerprint = actual
        settings.verify_code = body.get("short", "")
        settings.last_error_code = "CERT_CHANGED"
        settings.last_error = "Host certificate changed"
        _push_message(
            context,
            "system",
            "Host certificate changed. Compare the new code "
            f"{settings.verify_code or '?'} with the host, then click "
            "'Trust new certificate' in the Blendermp panel.",
        )
    elif kind == "welcome":
        settings.state = "connected"
        runtime["presence_shadow"].clear()
        runtime["presence_full_at"] = 0.0
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
        runtime["presence_shadow"].clear()
        runtime["presence_full_at"] = 0.0
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
        name = body.get("name", "?")
        _remove_user(context, name)
        # A peer that leaves (or crashes) must not leave phantom locks in the
        # local mirror, even if a lease-release frame was missed.
        for uid, owner in list(runtime["lease_owners"].items()):
            if owner == name:
                runtime["lease_owners"].pop(uid, None)
                runtime["owned_leases"].discard(uid)
                runtime["lease_activity"].pop(uid, None)
        for uid in list(runtime["hidden_objects"]):
            if uid not in runtime["lease_owners"]:
                obj = obj_sync.find(uid)
                if obj is not None:
                    try:
                        obj.hide_select = False
                    except Exception:  # noqa: BLE001
                        pass
                runtime["hidden_objects"].discard(uid)
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
                if "scene" in body:
                    user.scene = str(body.get("scene", ""))[:64]
                if "frame" in body:
                    user.frame = int(body.get("frame", 0) or 0)
                if "playing" in body:
                    user.playing = bool(body.get("playing"))
                if "fps" in body:
                    user.fps = int(body.get("fps", 24) or 24)
                if "mode" in body:
                    user.mode = str(body.get("mode", ""))[:32]
                if "active" in body:
                    user.active = str(body.get("active", ""))[:64]
                if role:
                    user.role = role
                    if body.get("name") == settings.name:
                        settings.role = role
                if "view" in body:
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
                if "view_corners" in body:
                    corners = body.get("view_corners")
                    if isinstance(corners, list) and len(corners) == 21:
                        try:
                            values = [float(value) for value in corners]
                        except (TypeError, ValueError):
                            values = []
                        if len(values) == 21:
                            user.view_corners = values
                            user.has_corners = True
                        else:
                            user.has_corners = False
                    else:
                        user.has_corners = False
                if "aspect" in body:
                    aspect = body.get("aspect")
                    if isinstance(aspect, (int, float)) and not isinstance(aspect, bool):
                        user.view_aspect = max(0.1, min(10.0, float(aspect)))
                if "persp" in body:
                    user.view_persp = str(body.get("persp", ""))[:16]
                if "dist" in body:
                    distance = body.get("dist")
                    if isinstance(distance, (int, float)) and not isinstance(distance, bool):
                        user.view_distance = max(0.05, min(100000.0, float(distance)))
                if "cursor" in body:
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
                if "selected" in body:
                    old_selected = {
                        item for item in str(user.selected or "").split(",") if item
                    }
                    user.selected = str(body.get("selected", ""))[:512]
                    new_selected = {
                        item for item in str(user.selected or "").split(",") if item
                    }
                    # A remote deselect must free the object even if the lease
                    # release frame was lost. Without this a mesh could stay
                    # "locked" forever after its owner stopped selecting it.
                    for uid in old_selected - new_selected:
                        if runtime["lease_owners"].get(uid) == user.name:
                            runtime["lease_owners"].pop(uid, None)
                            runtime["owned_leases"].discard(uid)
                            runtime["lease_activity"].pop(uid, None)
                            runtime["hidden_objects"].discard(uid)
                if "scene_hash" in body:
                    user.scene_hash = str(body.get("scene_hash", ""))[:32]
                if "ping_ms" in body:
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
                # Do not auto-renew an idle selection. The transform/mesh
                # activity hooks renew active leases; this one is about to go.
                settings.lease_info = "Your edit lock is expiring - edit to renew"
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
    elif kind == "view_bookmark":
        name = str(body.get("name", ""))[:48]
        bookmark = body.get("bookmark")
        if isinstance(bookmark, dict) and name and name != settings.name and settings.apply_bookmarks:
            if _apply_bookmark(context, bookmark):
                _push_message(context, "system", f"{name} moved everyone to {bookmark.get('name', 'a view')}")
    elif kind == "asset_bundle":
        _handle_bundle_offer(context, body)
    elif kind == "asset_bundle_sent":
        target = str(body.get("target", ""))[:48]
        _push_message(
            context,
            "system",
            f"Sent {body.get('objects', 0)} object(s) to {target or 'teammate'}",
        )
    elif kind == "transfer_progress":
        direction = str(body.get("direction", "receive"))
        percent = float(body.get("percent", 0.0) or 0.0)
        total_bytes = float(body.get("total_bytes", 0) or 0)
        sent_bytes = float(body.get("bytes", 0) or 0)
        settings.transfer_active = True
        settings.transfer_percent = max(0.0, min(100.0, percent))
        verb = "Sending" if direction == "send" else "Receiving"
        settings.transfer_info = (
            f"{verb} large object: {percent:.0f}% "
            f"({sent_bytes / 1048576:.1f} / {total_bytes / 1048576:.1f} MB)"
        )
    elif kind == "transfer_done":
        settings.transfer_percent = 100.0
        runtime["transfer_clear_at"] = time.monotonic() + 2.0
    elif kind == "transfer_expired":
        count = int(body.get("count", 0) or 0)
        if count:
            _push_message(context, "system", "A partial large-object transfer expired")
        if not runtime.get("transfer_clear_at"):
            settings.transfer_active = False
            settings.transfer_percent = 0.0
            settings.transfer_info = ""
    elif kind == "image_asset":
        _apply_image(context, body)
    elif kind == "render_control":
        _handle_render_control(context, body)
    elif kind == "render_frame":
        _handle_render_frame(context, body)
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
    elif kind == "object_rename":
        _apply_object_rename(context, body)
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
            for operator in window.modal_operators:
                ident = str(getattr(operator, "bl_idname", ""))
                # The passive hover watcher is always modal while a session is
                # active; it must not count as "the user is transforming".
                if ident not in {"BMP_OT_hover", "bmp.hover"}:
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


def _action_data(action) -> dict | None:
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


def _action_snapshot(obj) -> dict | None:
    animation = getattr(obj, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    return _action_data(action)


def _sync_pose(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or runtime.get("bootstrapping") or runtime.get("replaying") or not settings.sync_animation_values:
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
    if session is None or settings.paused or runtime.get("bootstrapping") or runtime.get("replaying") or not settings.sync_animation_values:
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
    if session is None or settings.paused or runtime.get("bootstrapping") or runtime.get("replaying"):
        return
    if not settings.is_host and session.state != "connected":
        return
    now = time.monotonic()
    if now - runtime.get("last_action", 0.0) < 0.5:
        return
    runtime["last_action"] = now
    for obj in context.scene.objects:
        if not (uid := obj.get("bmp_uuid")):
            continue
        owner = runtime["lease_owners"].get(uid)
        if owner is not None and owner != settings.name:
            continue
        has_animation = getattr(obj, "animation_data", None) is not None
        if not has_animation and obj.type != "ARMATURE" and uid not in runtime["action_shadow"]:
            continue
        action = _action_snapshot(obj)
        nla = _nla_snapshot(obj)
        snapshot = {"action": action, "nla": nla}
        signature = json.dumps(snapshot, sort_keys=True, default=str)
        if runtime["action_shadow"].get(uid) == signature:
            continue
        if not settings.is_host and uid not in runtime["owned_leases"]:
            runtime["action_shadow"][uid] = signature
            continue
        runtime["action_shadow"][uid] = signature
        _note_rev(uid)
        session.send_action(uid, action, nla)


def _write_action_data(action, snapshot: dict, obj) -> bool:
    if not isinstance(snapshot, dict):
        return False
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
        return True
    except (AttributeError, TypeError, ValueError, RuntimeError):
        traceback.print_exc()
        return False


def _apply_action(context, body: dict) -> None:
    obj = obj_sync.find(body.get("uuid"))
    snapshot = body.get("action")
    if obj is None:
        return
    uid = obj.get("bmp_uuid")
    has_nla = "nla" in body
    nla = body.get("nla")
    if snapshot is None:
        animation = getattr(obj, "animation_data", None)
        if animation is not None:
            animation.action = None
        if has_nla:
            if isinstance(nla, dict):
                _apply_nla(obj, nla)
            else:
                _clear_nla(obj)
        if uid:
            runtime["action_shadow"][uid] = json.dumps(
                {"action": None, "nla": nla if has_nla else None}, sort_keys=True, default=str
            )
        return
    if not isinstance(snapshot, dict):
        return
    name = str(snapshot.get("name", "Action"))[:64] or "Action"
    action = bpy.data.actions.get(name)
    if action is None:
        action = bpy.data.actions.new(name)
    if _write_action_data(action, snapshot, obj):
        animation = obj.animation_data_create()
        animation.action = action
        if hasattr(animation, "action_slot") and len(action.slots):
            try:
                animation.action_slot = action.slots[0]
            except (AttributeError, TypeError, RuntimeError):
                pass
        if nla is not None:
            _apply_nla(obj, nla)
        elif has_nla:
            _clear_nla(obj)
        if uid:
            runtime["action_shadow"][uid] = json.dumps(
                {"action": snapshot, "nla": nla if has_nla else None}, sort_keys=True, default=str
            )


def _nla_snapshot(obj) -> dict | None:
    animation = getattr(obj, "animation_data", None)
    tracks = getattr(animation, "nla_tracks", None) if animation is not None else None
    if not tracks:
        return None
    result = []
    for track in list(tracks)[:16]:
        strips = []
        for strip in list(getattr(track, "strips", []))[:32]:
            item = {
                "name": str(getattr(strip, "name", "Strip"))[:64],
                "frame_start": float(getattr(strip, "frame_start", 0.0)),
                "frame_end": float(getattr(strip, "frame_end", 0.0)),
                "action_frame_start": float(getattr(strip, "action_frame_start", 0.0)),
                "action_frame_end": float(getattr(strip, "action_frame_end", 0.0)),
                "repeat": float(getattr(strip, "repeat", 1.0)),
                "scale": float(getattr(strip, "scale", 1.0)),
                "blend_type": str(getattr(strip, "blend_type", "REPLACE"))[:16],
                "extrapolation": str(getattr(strip, "extrapolation", "HOLD"))[:16],
                "influence": float(getattr(strip, "influence", 1.0)),
                "mute": bool(getattr(strip, "mute", False)),
                "use_auto_blend": bool(getattr(strip, "use_auto_blend", False)),
                "use_sync_length": bool(getattr(strip, "use_sync_length", False)),
            }
            action = getattr(strip, "action", None)
            if action is not None:
                data = _action_data(action)
                if data is not None:
                    item["action"] = data
            strips.append(item)
        result.append(
            {
                "name": str(getattr(track, "name", "Track"))[:64],
                "mute": bool(getattr(track, "mute", False)),
                "is_solo": bool(getattr(track, "is_solo", False)),
                "strips": strips,
            }
        )
    return {"tracks": result} if result else None


def _clear_nla(obj) -> None:
    animation = getattr(obj, "animation_data", None)
    if animation is None:
        return
    try:
        for track in list(animation.nla_tracks):
            animation.nla_tracks.remove(track)
    except (AttributeError, TypeError, RuntimeError):
        pass


def _apply_nla(obj, snapshot) -> None:
    if not isinstance(snapshot, dict):
        return
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        return
    try:
        animation = obj.animation_data_create()
        for track in list(animation.nla_tracks):
            animation.nla_tracks.remove(track)
        for track_spec in tracks[:16]:
            if not isinstance(track_spec, dict):
                continue
            track = animation.nla_tracks.new()
            track.name = str(track_spec.get("name", "Track"))[:64] or "Track"
            track.mute = bool(track_spec.get("mute", False))
            try:
                track.is_solo = bool(track_spec.get("is_solo", False))
            except (AttributeError, TypeError, RuntimeError):
                pass
            for strip_spec in (track_spec.get("strips") or [])[:32]:
                if not isinstance(strip_spec, dict):
                    continue
                action_spec = strip_spec.get("action")
                action = None
                if isinstance(action_spec, dict):
                    name = str(action_spec.get("name", "Action"))[:64] or "Action"
                    action = bpy.data.actions.get(name)
                    if action is None:
                        action = bpy.data.actions.new(name)
                    _write_action_data(action, action_spec, obj)
                if action is None:
                    continue
                try:
                    start = int(round(float(strip_spec.get("frame_start", 0.0))))
                    strip = track.strips.new(str(strip_spec.get("name", "Strip"))[:64], start, action)
                    strip.action_frame_start = float(strip_spec.get("action_frame_start", 0.0))
                    strip.action_frame_end = float(strip_spec.get("action_frame_end", 0.0))
                    strip.repeat = max(0.01, min(1000.0, float(strip_spec.get("repeat", 1.0))))
                    strip.scale = max(0.01, min(1000.0, float(strip_spec.get("scale", 1.0))))
                    strip.blend_type = str(strip_spec.get("blend_type", "REPLACE"))[:16]
                    strip.extrapolation = str(strip_spec.get("extrapolation", "HOLD"))[:16]
                    strip.influence = max(0.0, min(1.0, float(strip_spec.get("influence", 1.0))))
                    strip.mute = bool(strip_spec.get("mute", False))
                    strip.use_auto_blend = bool(strip_spec.get("use_auto_blend", False))
                    strip.use_sync_length = bool(strip_spec.get("use_sync_length", False))
                    strip.frame_end = float(strip_spec.get("frame_end", strip.frame_end))
                    if hasattr(strip, "action_slot") and len(action.slots):
                        try:
                            strip.action_slot = action.slots[0]
                        except (AttributeError, TypeError, RuntimeError):
                            pass
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    traceback.print_exc()
    except (AttributeError, TypeError, RuntimeError):
        traceback.print_exc()


def _sync_materials(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or settings.paused or not settings.sync_materials or runtime.get("bootstrapping") or runtime.get("replaying"):
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
            animation = getattr(material, "animation_data", None)
            if animation is not None and getattr(animation, "action", None) is not None:
                # Animated material values are evaluated locally per frame.
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
    "BOOLEAN": ("operation", "solver", "use_self", "use_hole_tolerant"),
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
        if mod.type in {"ARMATURE", "LATTICE", "CURVE", "HOOK", "SURFACE_DEFORM", "MESH_DEFORM", "BOOLEAN"}:
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
    if session is None or settings.paused or not settings.sync_modifiers or runtime.get("bootstrapping") or runtime.get("replaying"):
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


def _scan_images(context) -> None:
    """Queue every usable image, not just material textures.

    This catches reference boards, compositor inputs and world textures that no
    material points at, plus images loaded after the session started.
    """
    session = runtime.get("session")
    if session is None:
        return
    now = time.monotonic()
    if now - runtime.get("last_image_scan", 0.0) < 2.0:
        return
    runtime["last_image_scan"] = now
    for image in list(bpy.data.images):
        if image is None or image.name in runtime["image_queued"]:
            continue
        if getattr(image, "type", "IMAGE") in {"RENDER_RESULT", "COMPOSITING"}:
            continue
        if getattr(image, "source", "FILE") == "VIEWER":
            continue
        packed = getattr(image, "packed_file", None) is not None
        filepath = getattr(image, "filepath", "")
        if not packed and not filepath and tuple(getattr(image, "size", (0, 0))) == (0, 0):
            continue
        _queue_image(image)


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
                start_join(
                    context,
                    code,
                    request.get("relay_url"),
                    request.get("fingerprint"),
                    bool(request.get("prefer_direct")),
                    request.get("direct_code"),
                    request.get("direct_codes"),
                )
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
        elif action == "render":
            _start_session_render(
                context,
                request.get("start", 1),
                request.get("end", 250),
                request.get("step", 1),
                request.get("format", "MP4"),
                request.get("output", ""),
            )
        elif action == "render_stop":
            _stop_session_render(context)
        elif action == "render_reply":
            session = runtime.get("session")
            pending = runtime.get("render_request") or {}
            job_id = request.get("job") or pending.get("job") or ""
            accept = bool(request.get("accept"))
            if session is not None and job_id:
                session.send_render_control("accept" if accept else "decline", {"job": job_id})
                if not accept:
                    runtime["render_request"] = None
                _push_message(
                    context,
                    "system",
                    "Joined the render" if accept else "Declined the render",
                )
                _write_status(context, force=True)
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
    if obj.type == "CURVE":
        return object_payload.KIND_CURVE
    if obj.type == "FONT":
        return object_payload.KIND_FONT
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
    _touch_lease(uid)
    session: SessionService | None = runtime.get("session")
    if session is not None:
        session.bump_rev(uid)


def _touch_lease(uid: str) -> None:
    """Mark a lease as actively edited so it does not idle out."""
    if uid:
        runtime.setdefault("lease_activity", {})[uid] = time.monotonic()


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
    if not runtime.get("journal_active") or runtime.get("replaying"):
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
        try:
            body = _object_create_payload(obj)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            continue
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
            session.mark_bootstrap_done(target)
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
    runtime["lock_notified"].clear()
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
    runtime["presence_shadow"].clear()
    runtime["presence_full_at"] = 0.0
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
    raw = np.asarray(values, dtype=np.float32).tobytes()
    if compression.enabled():
        return "z" + base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
    return "r" + base64.b64encode(raw).decode("ascii")


def _unpack_floats(value: str, count: int):
    text = str(value)
    if len(text) < 2 or text[0] not in "zr":
        return None
    try:
        raw = base64.b64decode(text[1:].encode("ascii"))
        if text[0] == "z":
            raw = zlib.decompress(raw)
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


DRIVER_PATHS = {
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "scale",
    "delta_location",
    "delta_rotation_euler",
    "delta_rotation_quaternion",
    "delta_scale",
}
DRIVER_FUNCTIONS = {
    "frame",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "atan2",
    "sqrt",
    "pow",
    "abs",
    "min",
    "max",
    "floor",
    "ceil",
    "round",
    "pi",
    "e",
    "radians",
    "degrees",
    "fmod",
}
DRIVER_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_+-*/().,%<>=!&|?: ")
SINGLE_PROP_PATHS = {
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "scale",
    "dimensions",
    "delta_location",
    "delta_rotation_euler",
    "delta_scale",
}


def _safe_single_prop_path(value: str) -> str | None:
    value = str(value or "")[:64]
    match = re.fullmatch(r"([a-z_]+)(?:\[([0-3])\])?", value)
    if not match or match.group(1) not in SINGLE_PROP_PATHS:
        return None
    return value


def _safe_driver_expression(expression: str, names: set[str]) -> str | None:
    if not isinstance(expression, str):
        return None
    expression = expression.strip()
    if not expression or len(expression) > 200:
        return None
    if "__" in expression or any(ch not in DRIVER_CHARS for ch in expression):
        return None
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression):
        if token not in names and token not in DRIVER_FUNCTIONS:
            return None
    return expression


def _driver_snapshot(obj) -> list:
    animation = getattr(obj, "animation_data", None)
    drivers = getattr(animation, "drivers", None) if animation is not None else None
    if not drivers:
        return []
    result = []
    for curve in list(drivers)[:16]:
        path = str(getattr(curve, "data_path", ""))[:64]
        if path not in DRIVER_PATHS:
            continue
        driver = getattr(curve, "driver", None)
        if driver is None:
            continue
        names = {str(var.name)[:64] for var in getattr(driver, "variables", [])[:16]}
        expression = str(getattr(driver, "expression", ""))
        driver_type = str(getattr(driver, "type", "SCRIPTED"))[:16]
        if driver_type == "SCRIPTED":
            expression = _safe_driver_expression(expression, names)
            if expression is None:
                continue
        variables = []
        for var in getattr(driver, "variables", [])[:16]:
            item = {"name": str(var.name)[:64], "type": str(var.type)[:16]}
            targets = getattr(var, "targets", [])
            target = targets[0] if len(targets) else None
            if target is None:
                continue
            if var.type == "SINGLE_PROP":
                target_id = getattr(target, "id", None)
                uid = target_id.get("bmp_uuid") if target_id is not None else None
                data_path = str(getattr(target, "data_path", ""))[:64]
                safe_path = _safe_single_prop_path(data_path)
                if not uid or safe_path is None:
                    continue
                item["id"] = uid
                item["data_path"] = safe_path
            elif var.type == "TRANSFORMS":
                target_id = getattr(target, "id", None)
                uid = target_id.get("bmp_uuid") if target_id is not None else None
                if not uid:
                    continue
                item["id"] = uid
                item["bone_target"] = str(getattr(target, "bone_target", ""))[:64]
                item["transform_type"] = str(getattr(target, "transform_type", "LOC_X"))[:32]
                item["transform_space"] = str(getattr(target, "transform_space", "WORLD_SPACE"))[:32]
            else:
                continue
            variables.append(item)
        result.append(
            {
                "path": path,
                "index": int(getattr(curve, "array_index", 0)),
                "type": driver_type,
                "expression": expression[:200],
                "variables": variables,
            }
        )
    return result


def _apply_drivers(obj, specs) -> None:
    if not isinstance(specs, list) or not specs:
        return
    try:
        animation = obj.animation_data_create()
        for curve in list(animation.drivers):
            try:
                animation.drivers.remove(curve)
            except (TypeError, RuntimeError):
                pass
        for spec in specs[:16]:
            if not isinstance(spec, dict):
                continue
            path = str(spec.get("path", ""))[:64]
            index = spec.get("index")
            if path not in DRIVER_PATHS or not isinstance(index, int) or isinstance(index, bool):
                continue
            driver_type = str(spec.get("type", "SCRIPTED"))[:16]
            if driver_type not in {"SCRIPTED", "AVERAGE", "SUM", "MIN", "MAX"}:
                continue
            names = {
                str(item.get("name", ""))[:64]
                for item in (spec.get("variables") or [])[:16]
                if isinstance(item, dict)
            }
            expression = ""
            if driver_type == "SCRIPTED":
                expression = _safe_driver_expression(str(spec.get("expression", "")), names)
                if expression is None:
                    continue
            try:
                limit = 3 if path in {"rotation_quaternion", "rotation_axis_angle", "delta_rotation_quaternion"} else 2
                curve = obj.driver_add(path, max(0, min(limit, index)))
                driver = curve.driver
                driver.type = driver_type
                if driver_type == "SCRIPTED":
                    driver.expression = expression
                for item in (spec.get("variables") or [])[:16]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", ""))[:64]
                    var_type = str(item.get("type", ""))[:16]
                    if not name or var_type not in {"SINGLE_PROP", "TRANSFORMS"}:
                        continue
                    target = obj_sync.find(str(item.get("id", "")))
                    if target is None:
                        continue
                    var = driver.variables.new()
                    var.name = name
                    var.type = var_type
                    target_spec = var.targets[0]
                    target_spec.id_type = "OBJECT"
                    target_spec.id = target
                    if var_type == "SINGLE_PROP":
                        data_path = _safe_single_prop_path(str(item.get("data_path", "")))
                        if data_path:
                            target_spec.data_path = data_path
                    else:
                        target_spec.bone_target = str(item.get("bone_target", ""))[:64]
                        target_spec.transform_type = str(item.get("transform_type", "LOC_X"))[:32]
                        target_spec.transform_space = str(item.get("transform_space", "WORLD_SPACE"))[:32]
            except (AttributeError, TypeError, ValueError, RuntimeError):
                traceback.print_exc()
    except (AttributeError, TypeError, RuntimeError):
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
    elif kind == object_payload.KIND_CURVE:
        meta["dimensions"] = str(getattr(obj.data, "dimensions", "3D"))[:8]
    elif kind == object_payload.KIND_FONT:
        meta["body"] = str(getattr(obj.data, "body", ""))[:512]
        meta["size"] = float(getattr(obj.data, "size", 1.0))
        meta["align_x"] = str(getattr(obj.data, "align_x", "LEFT"))[:16]
        meta["align_y"] = str(getattr(obj.data, "align_y", "TOP_BASELINE"))[:16]
    action = _action_snapshot(obj)
    if action is not None:
        meta["action"] = action
    nla = _nla_snapshot(obj)
    if nla is not None:
        meta["nla"] = nla
    drivers = _driver_snapshot(obj)
    if drivers:
        meta["drivers"] = drivers
    if getattr(obj, "instance_type", "NONE") == "COLLECTION":
        collection = getattr(obj, "instance_collection", None)
        if collection is not None:
            meta["instance_type"] = "COLLECTION"
            meta["instance_collection"] = collection.name[:64]
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


def _send_object_create(session: SessionService, obj) -> bool:
    try:
        body = _object_create_payload(obj)
    except Exception:  # noqa: BLE001
        # One malformed object (for example a UV layer that disagrees with the
        # loop count) must not abort the whole scene bootstrap and kick the
        # joining peer.
        traceback.print_exc()
        return False
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
    return bool(sent)


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
    runtime.setdefault("name_shadow", {})[uid] = obj.name
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
    if isinstance(meta.get("action"), dict) or isinstance(meta.get("nla"), dict):
        _apply_action(
            context,
            {
                "uuid": uid,
                "action": meta.get("action"),
                "nla": meta.get("nla"),
            },
        )
    _apply_drivers(obj, meta.get("drivers"))
    if meta.get("instance_type") == "COLLECTION":
        collection = bpy.data.collections.get(str(meta.get("instance_collection", "")))
        if collection is not None:
            try:
                obj.instance_type = "COLLECTION"
                obj.instance_collection = collection
            except (AttributeError, TypeError, RuntimeError):
                pass


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
    runtime.setdefault("name_shadow", {}).pop(uid, None)
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


def _apply_object_rename(context, body: dict) -> None:
    uid = body.get("uuid")
    name = body.get("name")
    if not isinstance(uid, str) or not isinstance(name, str) or not name.strip():
        return
    obj = obj_sync.find(uid)
    if obj is None:
        return
    try:
        obj.name = name.strip()[:48]
    except Exception:  # noqa: BLE001
        return
    runtime.setdefault("name_shadow", {})[uid] = obj.name


def _lease_request(session: SessionService, uid: str, action: str) -> bool:
    if session.is_host:
        return session.local_lease(uid, action)
    session.send_lease(uid, action)
    return True


def _drain_ai_queue(context) -> None:
    settings = context.window_manager.bmp
    while True:
        try:
            result = runtime["ai_queue"].get_nowait()
        except queue.Empty:
            break
        session = runtime.get("ai_session")
        if session is None:
            continue
        settings.ai_status = session.status
        settings.ai_reply = session.reply
        if session.status == "done":
            runtime["ai_pending_edits"] = list(session.pending)
            settings.ai_pending_count = len(session.pending)
            settings.ai_pending_text = "\n".join(edit.summary for edit in session.pending)
            if session.pending:
                _push_message(context, "system", f"AI proposed {len(session.pending)} edit(s). Confirm to apply.")
            elif session.reply:
                _push_message(context, "system", f"AI: {session.reply[:160]}")
        elif session.status == "error":
            settings.last_error = session.error
            settings.last_error_code = "AI_ERROR"


def _sync_selection(context) -> None:
    """Claim selected objects so only one person edits them at a time.

    Locks are soft. A lease is renewed only while its owner is actively
    editing; a selection left idle releases itself after ``LEASE_IDLE``. This
    keeps a teammate who walked away from locking the scene forever.
    """
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or runtime.get("bootstrapping") or runtime.get("replaying"):
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
    activity = runtime.setdefault("lease_activity", {})

    # --- Multi-User DynamicRightSelectTimer port -------------------------
    # Selection is the lock: newly selected objects are locked to us, objects
    # that leave the selection are released. There is no idle expiry.
    to_lock = selected - previous

    # Release deselected objects. The short grace also covers a lease that was
    # granted after the object was already deselected.
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
            activity.pop(uid, None)
        runtime["deselected"].pop(uid, None)

    # Lock newly selected objects.
    suppress = now < runtime.get("lease_suppress_until", 0.0)
    for uid in to_lock:
        if suppress:
            continue
        owner = owners.get(uid)
        if owner is None or owner == settings.name:
            granted = _lease_request(session, uid, "acquire")
            if granted and session.is_host:
                owners[uid] = settings.name
                owned.add(uid)
            activity[uid] = now

    # If nothing is selected, release everything we own (Multi-User does this).
    if not selected:
        for uid in list(owned):
            _lease_request(session, uid, "release")
            owned.discard(uid)
            owners.pop(uid, None)
            activity.pop(uid, None)

    # Renew held leases so they never expire while selected.
    if now - runtime["lease_renew"] > LEASE_RENEW:
        runtime["lease_renew"] = now
        for uid in list(owned):
            if uid in selected:
                _lease_request(session, uid, "acquire")

    # Multi-User's is_node_readonly pass: every object owned by someone else is
    # made non-selectable. This is the part that actually enforces the lock.
    active = context.active_object
    hidden = runtime["hidden_objects"]
    for obj in context.scene.objects:
        uid = obj.get("bmp_uuid")
        if not uid:
            continue
        owner = owners.get(uid)
        selectable = owner in (None, settings.name)
        try:
            if obj.hide_select == selectable:
                obj.hide_select = not selectable
        except Exception:  # noqa: BLE001
            pass
        if selectable:
            hidden.discard(uid)
            runtime["lock_notified"].discard(uid)
            continue
        hidden.add(uid)
        try:
            if obj.select_get():
                obj.select_set(False)
        except Exception:  # noqa: BLE001
            pass
        if uid not in runtime["lock_notified"]:
            runtime["lock_notified"].add(uid)
            settings.lease_info = f"{obj.name} is locked by {owner}"
        if obj is active and context.mode != "OBJECT":
            # A locked object must not stay in Edit/Sculpt mode; otherwise the
            # user can keep changing it locally even though it will not sync.
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:  # noqa: BLE001
                pass

    runtime["selection"] = selected

def _object_transform_animated(obj) -> bool:
    """Cached wrapper: animation state changes rarely, so do not inspect the
    action graph 60 times a second for every object."""
    now = time.monotonic()
    uid = str(obj.get("bmp_uuid") or obj.name)
    cache = runtime.setdefault("animated_cache", {})
    cached = cache.get(uid)
    if cached is not None and now - cached[0] < 1.0:
        return cached[1]
    result = _object_transform_animated_uncached(obj)
    cache[uid] = (now, result)
    return result


def _object_transform_animated_uncached(obj) -> bool:
    """True when an action/NLA drives this object's transform.

    Animated transforms are evaluated locally from the shared action, so each
    user can sit on a different frame without the other users' current frame
    being streamed over the top.
    """
    animation = getattr(obj, "animation_data", None)
    if animation is None:
        return False
    try:
        if len(getattr(animation, "nla_tracks", ())):
            return True
        if len(getattr(animation, "drivers", ())):
            return True
        action = getattr(animation, "action", None)
        if action is None:
            return False
        for curve in _action_fcurves(action):
            path = str(getattr(curve, "data_path", ""))
            if path.startswith(("location", "rotation_", "scale", "delta_")):
                return True
    except (AttributeError, TypeError, RuntimeError):
        return False
    return False


def _sync_transforms(context) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.sync_transforms or settings.paused or runtime.get("bootstrapping") or runtime.get("replaying"):
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
        names = runtime.setdefault("name_shadow", {})
        previous_name = names.get(uid)
        if previous_name is not None and previous_name != obj.name:
            _note_rev(uid)
            session.send_object_rename(uid, obj.name)
        names[uid] = obj.name
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
        if _object_transform_animated(obj):
            # Each peer evaluates the shared action at its own frame.
            matrix = obj.matrix_world
            shadow[uid] = tuple(round(v, 6) for row in matrix for v in row)
            continue
        owner = runtime["lease_owners"].get(uid)
        if owner not in (None, settings.name):
            # Locked by another peer. Never broadcast or steal the lease, and
            # undo any local drift so the object cannot be moved here.
            matrix = obj.matrix_world
            current = tuple(round(v, 6) for row in matrix for v in row)
            previous = shadow.get(uid)
            if previous is not None and current != previous:
                try:
                    obj_sync.apply(uid, previous)
                except Exception:  # noqa: BLE001
                    pass
            else:
                shadow[uid] = current
            continue
        if not settings.is_host and uid not in runtime["owned_leases"]:
            # A client may only broadcast objects it holds the lease for. Keep
            # the shadow current so a remote-applied matrix is never echoed.
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


def _sync_ai_pending(settings) -> None:
    """Mirror the AI session's pending edits into the settings UI."""
    session = runtime.get("ai_session")
    if session is None:
        settings.ai_pending_text = ""
        settings.ai_pending_count = 0
        runtime["ai_pending_edits"] = []
        return
    runtime["ai_pending_edits"] = list(session.pending)
    settings.ai_pending_count = len(session.pending)
    settings.ai_pending_text = "\\n".join(edit.summary for edit in session.pending)


def _ai_callback(result: dict) -> None:
    runtime["ai_queue"].put(result)


def _ai_scene_context(context) -> str:
    """Build a compact scene summary for the copilot prompt."""
    try:
        import bpy

        scene = context.scene
        objects = [obj for obj in scene.objects if not obj.get("bmp_ping")]
        selected = [obj.name for obj in context.selected_objects][:12]
        active = context.active_object.name if context.active_object is not None else ""
        lines = [
            f"Scene: {scene.name}",
            f"Frame: {scene.frame_current} ({scene.frame_start}-{scene.frame_end})",
            f"Objects: {len(objects)}",
            f"Active: {active or 'none'}",
            f"Selected: {', '.join(selected) if selected else 'none'}",
        ]
        for obj in objects[:40]:
            location = ", ".join(f"{float(value):.2f}" for value in obj.location)
            flags = []
            if obj.select_get():
                flags.append("selected")
            if obj.hide_viewport:
                flags.append("hidden")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"- {obj.name} ({obj.type}) at ({location}){suffix}")
        if len(objects) > 40:
            lines.append(f"- ... {len(objects) - 40} more")
        materials = [material.name for material in bpy.data.materials][:30]
        if materials:
            lines.append("Materials: " + ", ".join(materials))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _ai_handlers() -> dict:
    import bpy
    from math import radians

    from . import obj_sync

    def _obj(args):
        name = str(args.get("name", args.get("object", ""))).strip()
        return bpy.data.objects.get(name) if name else None

    def _uid(obj):
        uid = obj.get("bmp_uuid")
        if not uid:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
            runtime["tracked"].add(obj.name)
        return uid

    def _send_transform(obj):
        uid = _uid(obj)
        _note_rev(uid)
        runtime["object_shadow"].pop(uid, None)
        sess = runtime.get("session")
        if sess is not None:
            matrix = tuple(round(float(value), 6) for row in obj.matrix_world for value in row)
            sess.send_transform(uid, matrix)

    def _invalidate_material(obj):
        uid = obj.get("bmp_uuid")
        if not uid:
            return
        for index in range(len(getattr(obj, "material_slots", []))):
            runtime["material_shadow"].pop((uid, index), None)

    def _invalidate_modifier(obj):
        uid = obj.get("bmp_uuid")
        if uid:
            runtime["modifier_shadow"].pop(uid, None)

    def _color(value):
        if not (isinstance(value, (list, tuple)) and len(value) in (3, 4)):
            return None
        try:
            values = [max(0.0, min(1.0, float(item))) for item in value]
        except (TypeError, ValueError):
            return None
        if len(values) == 3:
            values.append(1.0)
        return values

    def _principled(material):
        if material is None or not material.use_nodes or material.node_tree is None:
            return None
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
        return None

    # ---------------------------------------------------------------- read
    def get_scene_info(args, ctx):
        objects = []
        for obj in bpy.context.scene.objects:
            if obj.get("bmp_ping"):
                continue
            objects.append(
                {
                    "name": obj.name,
                    "uid": obj.get("bmp_uuid", ""),
                    "type": obj.type,
                    "location": [round(float(value), 3) for value in obj.location],
                    "selected": bool(obj.select_get()),
                    "hidden": bool(obj.hide_viewport),
                }
            )
        return {"count": len(objects), "objects": objects[:120]}

    def get_selected_objects(args, ctx):
        return {
            "objects": [
                {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [round(float(value), 3) for value in obj.location],
                }
                for obj in bpy.context.selected_objects
            ]
        }

    def get_object_info(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        return {
            "name": obj.name,
            "uid": obj.get("bmp_uuid", ""),
            "type": obj.type,
            "location": [round(float(value), 3) for value in obj.location],
            "rotation": [round(float(value), 3) for value in obj.rotation_euler],
            "scale": [round(float(value), 3) for value in obj.scale],
            "dimensions": [round(float(value), 3) for value in obj.dimensions],
            "materials": [
                slot.material.name
                for slot in getattr(obj, "material_slots", [])
                if slot.material is not None
            ],
            "modifiers": [modifier.type for modifier in obj.modifiers],
        }

    def get_materials(args, ctx):
        users: dict[str, list[str]] = {}
        for obj in bpy.context.scene.objects:
            for slot in getattr(obj, "material_slots", []):
                if slot.material is not None:
                    users.setdefault(slot.material.name, []).append(obj.name)
        return {
            "materials": [
                {"name": material.name, "users": users.get(material.name, [])[:8]}
                for material in bpy.data.materials
            ][:120]
        }

    def get_object_modifiers(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        return {
            "name": obj.name,
            "modifiers": [
                {
                    "name": modifier.name,
                    "type": modifier.type,
                    "show_viewport": bool(modifier.show_viewport),
                }
                for modifier in obj.modifiers
            ],
        }

    # ---------------------------------------------------------- transforms
    def move_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        loc = args.get("location")
        if not (isinstance(loc, (list, tuple)) and len(loc) == 3):
            return {"error": "location must be [x, y, z]"}
        try:
            obj.location = tuple(float(value) for value in loc)
        except (TypeError, ValueError):
            return {"error": "invalid location"}
        _send_transform(obj)
        return {
            "ok": True,
            "name": obj.name,
            "location": [round(float(value), 3) for value in obj.location],
        }

    def rotate_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        rot = args.get("rotation")
        if not (isinstance(rot, (list, tuple)) and len(rot) == 3):
            return {"error": "rotation must be [x, y, z] degrees"}
        try:
            obj.rotation_euler = tuple(radians(float(value)) for value in rot)
        except (TypeError, ValueError):
            return {"error": "invalid rotation"}
        _send_transform(obj)
        return {"ok": True, "name": obj.name}

    def scale_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        scale = args.get("scale")
        if not (isinstance(scale, (list, tuple)) and len(scale) in (1, 3)):
            return {"error": "scale must be a number or [x, y, z]"}
        try:
            if len(scale) == 1:
                value = float(scale[0])
                obj.scale = (value, value, value)
            else:
                obj.scale = tuple(float(value) for value in scale)
        except (TypeError, ValueError):
            return {"error": "invalid scale"}
        _send_transform(obj)
        return {"ok": True, "name": obj.name}

    # ------------------------------------------------------------ lifecycle
    def create_primitive(args, ctx):
        if bpy.context.mode != "OBJECT":
            return {"error": "switch to Object Mode first"}
        ptype = str(args.get("type", "cube")).lower()
        name = str(args.get("name", "")).strip() or ptype
        loc = args.get("location", [0.0, 0.0, 0.0])
        try:
            size = float(args.get("size", 1.0) or 1.0)
        except (TypeError, ValueError):
            size = 1.0
        if not (isinstance(loc, (list, tuple)) and len(loc) == 3):
            return {"error": "location must be [x, y, z]"}
        try:
            bpy.ops.object.select_all(action="DESELECT")
            if ptype == "cube":
                bpy.ops.mesh.primitive_cube_add(size=size, location=tuple(float(v) for v in loc))
            elif ptype == "sphere":
                bpy.ops.mesh.primitive_uv_sphere_add(radius=size, location=tuple(float(v) for v in loc))
            elif ptype == "plane":
                bpy.ops.mesh.primitive_plane_add(size=size, location=tuple(float(v) for v in loc))
            elif ptype == "cylinder":
                bpy.ops.mesh.primitive_cylinder_add(radius=size, depth=size, location=tuple(float(v) for v in loc))
            elif ptype == "cone":
                bpy.ops.mesh.primitive_cone_add(radius1=size, depth=size, location=tuple(float(v) for v in loc))
            else:
                return {"error": f"unknown primitive {ptype}"}
            obj = bpy.context.active_object
            if obj is None:
                return {"error": "creation failed"}
            obj.name = name
            uid = _uid(obj)
            runtime["tracked"].add(obj.name)
            sess = runtime.get("session")
            if sess is not None:
                _send_object_create(sess, obj)
            return {"ok": True, "name": obj.name, "uid": uid}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}

    def delete_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        uid = obj.get("bmp_uuid")
        name = obj.name
        bpy.data.objects.remove(obj, do_unlink=True)
        if uid:
            runtime["tracked"].discard(name)
            runtime["object_shadow"].pop(uid, None)
            runtime["mirrors"].pop(uid, None)
            runtime["edit_mirrors"].pop(uid, None)
            sess = runtime.get("session")
            if sess is not None:
                sess.send_object_delete(uid)
        return {"ok": True, "name": name}

    def duplicate_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        new_name = str(args.get("new_name", "")).strip() or f"{obj.name} copy"
        copy = obj.copy()
        if obj.data is not None:
            copy.data = obj.data.copy()
        bpy.context.scene.collection.objects.link(copy)
        copy.name = new_name
        uid = _uid(copy)
        runtime["tracked"].add(copy.name)
        sess = runtime.get("session")
        if sess is not None:
            _send_object_create(sess, copy)
        return {"ok": True, "name": copy.name, "uid": uid}

    def rename_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        new_name = str(args.get("new_name", "")).strip()
        if not new_name:
            return {"error": "new_name is required"}
        old_name = obj.name
        try:
            obj.name = new_name[:48]
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        runtime["tracked"].discard(old_name)
        runtime["tracked"].add(obj.name)
        uid = _uid(obj)
        runtime.setdefault("name_shadow", {})[uid] = obj.name
        sess = runtime.get("session")
        if sess is not None:
            sess.send_object_rename(uid, obj.name)
        return {"ok": True, "name": obj.name}

    def select_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        if not bool(args.get("add", False)):
            for item in bpy.context.selected_objects:
                item.select_set(False)
        try:
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        return {"ok": True, "name": obj.name}

    def frame_object(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        try:
            for item in bpy.context.selected_objects:
                item.select_set(False)
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            screen = getattr(bpy.context, "screen", None)
            for area in getattr(screen, "areas", []):
                if area.type != "VIEW_3D":
                    continue
                region = next(
                    (item for item in area.regions if item.type == "WINDOW"), None
                )
                with bpy.context.temp_override(area=area, region=region):
                    bpy.ops.view3d.view_selected()
                return {"ok": True, "name": obj.name}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not frame in this view: {exc}"[:200]}
        return {"error": "no 3D viewport is open"}

    def set_object_visibility(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        visible = bool(args.get("visible", True))
        render = args.get("render", visible)
        try:
            obj.hide_viewport = not visible
            obj.hide_render = not bool(render)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        _invalidate_modifier(obj)
        return {"ok": True, "name": obj.name, "visible": visible}

    def set_object_color(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        color = _color(args.get("color"))
        if color is None:
            return {"error": "color must be [r, g, b] or [r, g, b, a]"}
        try:
            obj.color = tuple(color)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        _invalidate_modifier(obj)
        return {"ok": True, "name": obj.name}

    # ------------------------------------------------------------- materials
    def create_material(args, ctx):
        name = str(args.get("name", "")).strip()[:48] or "Material"
        material = bpy.data.materials.get(name)
        if material is None:
            material = bpy.data.materials.new(name)
        material.use_nodes = True
        color = _color(args.get("color", [0.8, 0.8, 0.8]))
        if color is not None:
            material.diffuse_color = tuple(color)
        bsdf = _principled(material)
        if bsdf is not None:
            if color is not None and "Base Color" in bsdf.inputs:
                bsdf.inputs["Base Color"].default_value = tuple(color)
            for key, field in (("metallic", "Metallic"), ("roughness", "Roughness")):
                value = args.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if field in bsdf.inputs:
                        bsdf.inputs[field].default_value = max(0.0, min(1.0, float(value)))
        return {"ok": True, "material": material.name}

    def assign_material(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        material = bpy.data.materials.get(str(args.get("material", "")).strip())
        if material is None:
            return {"error": "material not found"}
        if getattr(obj.data, "materials", None) is None:
            return {"error": "object has no material slots"}
        try:
            slot_index = max(0, min(63, int(args.get("slot", 0))))
        except (TypeError, ValueError):
            slot_index = 0
        while len(obj.material_slots) <= slot_index:
            obj.data.materials.append(None)
        obj.material_slots[slot_index].material = material
        _invalidate_material(obj)
        return {"ok": True, "object": obj.name, "material": material.name}

    def set_material_color(args, ctx):
        material = bpy.data.materials.get(str(args.get("material", "")).strip())
        if material is None:
            return {"error": "material not found"}
        color = _color(args.get("color"))
        if color is None:
            return {"error": "color must be [r, g, b] or [r, g, b, a]"}
        material.diffuse_color = tuple(color)
        bsdf = _principled(material)
        if bsdf is not None and "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = tuple(color)
        return {"ok": True, "material": material.name}

    def set_material_property(args, ctx):
        material = bpy.data.materials.get(str(args.get("material", "")).strip())
        if material is None:
            return {"error": "material not found"}
        bsdf = _principled(material)
        if bsdf is None:
            return {"error": "material has no Principled BSDF"}
        changed = []
        for key, field in (("metallic", "Metallic"), ("roughness", "Roughness"), ("alpha", "Alpha")):
            value = args.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if field in bsdf.inputs:
                    bsdf.inputs[field].default_value = max(0.0, min(1.0, float(value)))
                    changed.append(key)
        if "alpha" in changed:
            material.diffuse_color = tuple(list(material.diffuse_color[:3]) + [float(bsdf.inputs["Alpha"].default_value)])
        if not changed:
            return {"error": "pass metallic, roughness or alpha"}
        return {"ok": True, "material": material.name, "changed": changed}

    # ------------------------------------------------------------- modifiers
    def add_modifier(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        modifier_type = str(args.get("modifier", "")).upper()
        if modifier_type not in {"SUBSURF", "BEVEL", "SOLIDIFY", "MIRROR", "ARRAY"}:
            return {"error": "unsupported modifier"}
        try:
            modifier = obj.modifiers.new(name=modifier_type.title(), type=modifier_type)
            if modifier_type == "SUBSURF" and hasattr(modifier, "levels"):
                modifier.levels = max(1, min(6, int(args.get("levels", 1) or 1)))
            if modifier_type == "BEVEL" and hasattr(modifier, "width"):
                modifier.width = max(0.0, min(10.0, float(args.get("width", 0.1) or 0.1)))
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        _invalidate_modifier(obj)
        return {"ok": True, "name": obj.name, "modifier": modifier.type}

    def apply_boolean(args, ctx):
        obj = _obj(args)
        if obj is None:
            return {"error": "object not found"}
        operand = bpy.data.objects.get(str(args.get("operand", "")).strip())
        if operand is None:
            return {"error": "operand not found"}
        if operand is obj:
            return {"error": "an object cannot boolean itself"}
        _uid(obj)
        _uid(operand)
        operation = str(args.get("operation", "DIFFERENCE")).upper()
        if operation not in {"DIFFERENCE", "UNION", "INTERSECT"}:
            return {"error": "operation must be DIFFERENCE, UNION or INTERSECT"}
        try:
            modifier = None
            for item in obj.modifiers:
                if item.type == "BOOLEAN":
                    modifier = item
                    break
            if modifier is None:
                modifier = obj.modifiers.new(name="Boolean", type="BOOLEAN")
            modifier.operation = operation
            modifier.object = operand
            applied = False
            if bool(args.get("apply", False)):
                if bpy.context.mode != "OBJECT":
                    return {"error": "switch to Object Mode first"}
                for item in bpy.context.selected_objects:
                    item.select_set(False)
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=modifier.name)
                applied = True
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        _invalidate_modifier(obj)
        return {
            "ok": True,
            "name": obj.name,
            "operand": operand.name,
            "operation": operation,
            "applied": applied,
        }

    # ----------------------------------------------------------------- scene
    def set_scene_setting(args, ctx):
        scene = bpy.context.scene
        changed = []
        for key in ("frame", "frame_start", "frame_end"):
            value = args.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                target = "frame_current" if key == "frame" else key
                try:
                    setattr(scene, target, max(0, value))
                    changed.append(key)
                except (AttributeError, TypeError, ValueError):
                    pass
        engine = args.get("engine")
        if isinstance(engine, str) and engine:
            try:
                scene.render.engine = engine
                changed.append("engine")
            except (TypeError, ValueError):
                return {"error": f"unknown render engine {engine}"}
        if not changed:
            return {"error": "pass frame, frame_start, frame_end or engine"}
        return {"ok": True, "changed": changed}

    return {
        "get_scene_info": get_scene_info,
        "get_selected_objects": get_selected_objects,
        "get_object_info": get_object_info,
        "get_materials": get_materials,
        "get_object_modifiers": get_object_modifiers,
        "move_object": move_object,
        "rotate_object": rotate_object,
        "scale_object": scale_object,
        "create_primitive": create_primitive,
        "delete_object": delete_object,
        "duplicate_object": duplicate_object,
        "rename_object": rename_object,
        "select_object": select_object,
        "frame_object": frame_object,
        "set_object_visibility": set_object_visibility,
        "set_object_color": set_object_color,
        "create_material": create_material,
        "assign_material": assign_material,
        "set_material_color": set_material_color,
        "set_material_property": set_material_property,
        "add_modifier": add_modifier,
        "apply_boolean": apply_boolean,
        "set_scene_setting": set_scene_setting,
    }

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
        bm.edges.ensure_lookup_table()
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
        with runtime["perf"].measure("topology_read"):
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
            settings.lease_info = f"Live edit locked by {owner}"
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
    if session is None or not settings.sync_mesh or settings.paused or runtime.get("bootstrapping") or runtime.get("replaying"):
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


def _viewport_corners(context):
    """Seven world-space points matching Multi-User's camera representation.

    Order: bottom-left, top-right, top-left, bottom-right, centre,
    view_location, apex. The four corners are the viewport edges projected one
    unit into the scene, the centre is the same at the centre pixel, the view
    location is the orbit pivot, and the apex sits 0.8 units behind the camera.
    """
    from bpy_extras import view3d_utils

    screen = getattr(context, "screen", None)
    if screen is None:
        return None
    area = next((item for item in screen.areas if item.type == "VIEW_3D"), None)
    if area is None or area.spaces.active is None:
        return None
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    region_3d = area.spaces.active.region_3d
    if region is None or region_3d is None:
        return None

    def project(x: float, y: float, distance: float = 1.0):
        view_vector = view3d_utils.region_2d_to_vector_3d(
            region, region_3d, (x, y)
        )
        ray_origin = view3d_utils.region_2d_to_origin_3d(
            region, region_3d, (x, y)
        )
        target = ray_origin + view_vector * distance
        return [float(target.x), float(target.y), float(target.z)]

    width = float(region.width)
    height = float(region.height)
    try:
        v1 = project(0.0, 0.0)
        v3 = project(0.0, height)
        v2 = project(width, height)
        v4 = project(width, 0.0)
        v5 = project(width * 0.5, height * 0.5)
        v6 = [float(value) for value in region_3d.view_location]
        v7 = project(width * 0.5, height * 0.5, distance=-0.8)
    except Exception:  # noqa: BLE001
        return None
    points = [v1, v2, v3, v4, v5, v6, v7]
    for point in points:
        if not all(math.isfinite(value) and abs(value) < 1e7 for value in point):
            return None
    return points


def _selected_uids(context) -> list:
    # Only actually selected objects. The active object can be unselected, and
    # including it kept the remote outline drawn after the owner deselected.
    objects = list(context.selected_objects)
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


# --------------------------------------------------------------------------- bookmarks
def _bookmarks_path() -> Path:
    return control.data_dir() / "bookmarks.json"


def _ensure_bookmarks(context) -> None:
    if runtime.get("bookmarks_loaded"):
        return
    runtime["bookmarks_loaded"] = True
    settings = context.window_manager.bmp
    while len(settings.bookmarks):
        settings.bookmarks.remove(len(settings.bookmarks) - 1)
    for item in bookmarks.load(_bookmarks_path()):
        entry = settings.bookmarks.add()
        entry.name = item["name"]
        entry.matrix = item["matrix"]
        entry.aspect = item["aspect"]
        entry.persp = item["persp"]
        entry.distance = item["distance"]


def _store_bookmarks(context) -> list:
    settings = context.window_manager.bmp
    result = []
    for item in settings.bookmarks:
        result.append(
            {
                "name": item.name,
                "matrix": list(item.matrix),
                "aspect": item.aspect,
                "persp": item.persp,
                "distance": item.distance,
            }
        )
    return result


def _save_bookmarks(context) -> None:
    try:
        bookmarks.save(_bookmarks_path(), _store_bookmarks(context))
    except (OSError, ValueError):
        traceback.print_exc()


def _bookmark_from_view(context, name: str) -> dict | None:
    view = _viewport_view(context)
    if view is None:
        return None
    return {
        "name": name,
        "matrix": view["matrix"],
        "aspect": view["aspect"],
        "persp": view["persp"],
        "distance": view["distance"],
    }


def _find_bookmark(context, name: str):
    wanted = str(name).strip().lower()
    for item in context.window_manager.bmp.bookmarks:
        if item.name.strip().lower() == wanted:
            return item
    return None


def _apply_bookmark(context, bookmark: dict) -> bool:
    from mathutils import Matrix

    clean = bookmarks.sanitize(bookmark)
    if clean is None or context.screen is None:
        return False
    area = next((item for item in context.screen.areas if item.type == "VIEW_3D"), None)
    if area is None or area.spaces.active is None:
        return False
    region = area.spaces.active.region_3d
    if region is None:
        return False
    values = clean["matrix"]
    matrix = Matrix((values[0:4], values[4:8], values[8:12], values[12:16]))
    try:
        region.view_perspective = clean["persp"] if clean["persp"] in {"PERSP", "ORTHO"} else "PERSP"
        region.view_matrix = matrix.inverted()
        region.view_distance = clean["distance"]
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


# --------------------------------------------------------------------------- asset bundles
BUNDLE_TYPES = {"MESH", "CAMERA", "LIGHT", "ARMATURE", "EMPTY", "CURVE", "FONT"}


def _bundle_objects(context) -> list:
    """Selected objects plus the dependencies needed to rebuild them."""
    selected = [
        obj
        for obj in list(context.selected_objects)
        if not obj.get("bmp_ping") and obj.type in BUNDLE_TYPES
    ]
    active = context.active_object
    if active is not None and active.type in BUNDLE_TYPES and active not in selected and not active.get("bmp_ping"):
        selected.append(active)
    result = []
    seen = set()
    queue = list(selected)
    while queue and len(result) < asset_bundle.MAX_OBJECTS:
        obj = queue.pop(0)
        if obj is None or obj.name in seen or obj.get("bmp_ping") or obj.type not in BUNDLE_TYPES:
            continue
        seen.add(obj.name)
        result.append(obj)
        parent = getattr(obj, "parent", None)
        if parent is not None:
            queue.append(parent)
        for mod in list(getattr(obj, "modifiers", []) or []):
            target = getattr(mod, "object", None)
            if target is not None:
                queue.append(target)
        for constraint in list(getattr(obj, "constraints", []) or []):
            target = getattr(constraint, "target", None)
            if target is not None:
                queue.append(target)
        if getattr(obj, "instance_type", "NONE") == "COLLECTION":
            collection = getattr(obj, "instance_collection", None)
            if collection is not None:
                try:
                    queue.extend(list(collection.all_objects))
                except (AttributeError, TypeError, RuntimeError):
                    pass
    result.sort(key=_parent_depth)
    return result


def _bundle_manifest(context, objects) -> dict:
    payloads = []
    images = {}
    for obj in objects[: asset_bundle.MAX_OBJECTS]:
        if not obj.get("bmp_uuid"):
            obj["bmp_uuid"] = str(uuid.uuid4())
    for obj in objects[: asset_bundle.MAX_OBJECTS]:
        try:
            body = _object_create_payload(obj)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            continue
        if body.get("kind") not in object_payload.KINDS:
            continue
        payloads.append(
            {
                "uid": body.get("uuid"),
                "kind": body.get("kind"),
                "meta": body.get("meta") or {},
                "geometry": body.get("geometry", b""),
            }
        )
        for slot in getattr(obj, "material_slots", [])[:16]:
            if slot.material is None:
                continue
            for image in _material_images(slot.material):
                if image.name in images:
                    continue
                data, format_hint = _image_bytes(context, image)
                if not data or len(data) > image_payload.MAX_IMAGE:
                    continue
                digest = hashlib.sha1(data).hexdigest()
                images[image.name] = {
                    "name": image.name[:64],
                    "format": format_hint,
                    "sha1": digest,
                    "data": data,
                }
    if not payloads:
        raise ValueError("nothing supported is selected")
    settings = context.window_manager.bmp
    return {
        "objects": payloads,
        "images": list(images.values())[: asset_bundle.MAX_IMAGES],
        "label": f"{len(payloads)} object(s) from {settings.name}",
    }


def _remap_bundle_meta(meta: dict, mapping: dict, image_names: dict) -> dict:
    result = copy.deepcopy(meta)
    parent = result.get("parent")
    result["parent"] = mapping.get(parent) if isinstance(parent, str) else None
    materials = result.get("materials")
    if isinstance(materials, list):
        for item in materials:
            if not isinstance(item, dict):
                continue
            for node in item.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                image = node.get("image")
                if isinstance(image, str) and image in image_names:
                    node["image"] = image_names[image]
    modifiers = result.get("modifiers")
    if isinstance(modifiers, list):
        for item in modifiers:
            if not isinstance(item, dict):
                continue
            props = item.get("props")
            if not isinstance(props, dict):
                continue
            target = props.get("object_uid")
            if isinstance(target, str):
                props["object_uid"] = mapping.get(target)
                if props["object_uid"] is None:
                    props.pop("object_uid", None)
    details = result.get("details")
    if isinstance(details, dict):
        parent_uid = details.get("parent")
        if isinstance(parent_uid, str):
            details["parent"] = mapping.get(parent_uid)
        for item in details.get("constraints") or []:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            if isinstance(target, str):
                item["target"] = mapping.get(target)
                if item["target"] is None:
                    item.pop("target", None)
    for spec in result.get("drivers") or []:
        if not isinstance(spec, dict):
            continue
        for item in spec.get("variables") or []:
            if not isinstance(item, dict):
                continue
            target = item.get("id")
            if isinstance(target, str):
                item["id"] = mapping.get(target)
                if item["id"] is None:
                    item.pop("id", None)
    return result


def _import_bundle(context, manifest: dict) -> int:
    if not isinstance(manifest, dict):
        return 0
    bundle_id = str(manifest.get("id", ""))
    if not bundle_id:
        try:
            bundle_id = asset_bundle.content_id(manifest)
        except asset_bundle.AssetBundleError:
            return 0
    if bundle_id in runtime["bundle_imported"]:
        _push_message(context, "system", "That bundle is already in this scene")
        return 0

    image_names = {}
    for item in manifest.get("images", [])[: asset_bundle.MAX_IMAGES]:
        if not isinstance(item, dict):
            continue
        sha1 = str(item.get("sha1", ""))
        if sha1 and sha1 in runtime["received_images"]:
            image_names[str(item.get("name", ""))] = runtime["received_images"][sha1]
            continue
        _apply_image(
            context,
            {
                "name": item.get("name", "Image"),
                "format": item.get("format", "png"),
                "sha1": sha1,
                "data": item.get("data", b""),
            },
        )
        if sha1 and sha1 in runtime["received_images"]:
            image_names[str(item.get("name", ""))] = runtime["received_images"][sha1]

    objects = [item for item in manifest.get("objects", []) if isinstance(item, dict)]
    mapping = {}
    for item in objects:
        try:
            mapping[str(item.get("uid"))] = str(uuid.uuid4())
        except Exception:  # noqa: BLE001
            continue
    pending = list(objects)
    ordered = []
    seen = set()
    while pending:
        progress = False
        for item in list(pending):
            uid = str(item.get("uid"))
            parent = (item.get("meta") or {}).get("parent")
            if not parent or parent not in mapping or parent in seen:
                ordered.append(item)
                seen.add(uid)
                pending.remove(item)
                progress = True
        if not progress:
            ordered.extend(pending)
            break

    imported = []
    for item in ordered:
        old_uid = str(item.get("uid"))
        new_uid = mapping.get(old_uid)
        if not new_uid:
            continue
        body = {
            "uuid": new_uid,
            "kind": item.get("kind"),
            "meta": _remap_bundle_meta(item.get("meta") or {}, mapping, image_names),
            "geometry": item.get("geometry", b""),
        }
        try:
            _apply_object_create(context, body)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            continue
        obj = obj_sync.find(new_uid)
        if obj is not None:
            imported.append(obj)

    for item in ordered:
        meta = item.get("meta") or {}
        obj = obj_sync.find(mapping.get(str(item.get("uid"))))
        if obj is None:
            continue
        if meta.get("instance_type") == "COLLECTION":
            collection = bpy.data.collections.get(str(meta.get("instance_collection", "")))
            if collection is not None:
                try:
                    obj.instance_type = "COLLECTION"
                    obj.instance_collection = collection
                except (AttributeError, TypeError, RuntimeError):
                    pass
        if meta.get("drivers"):
            _apply_drivers(obj, meta.get("drivers"))

    if not imported:
        _push_message(context, "system", "The bundle could not be imported")
        return 0
    for obj in context.selected_objects:
        try:
            obj.select_set(False)
        except Exception:  # noqa: BLE001
            pass
    for obj in imported:
        try:
            obj.select_set(True)
        except Exception:  # noqa: BLE001
            pass
    try:
        context.view_layer.objects.active = imported[-1]
    except Exception:  # noqa: BLE001
        pass
    runtime["bundle_imported"].add(bundle_id)
    if len(runtime["bundle_imported"]) > 128:
        runtime["bundle_imported"] = set(list(runtime["bundle_imported"])[-64:])
    eventlog.log(
        "info",
        "ASSET_BUNDLE_IMPORT",
        "asset bundle imported",
        bundle=bundle_id,
        objects=len(imported),
        images=len(image_names),
    )
    return len(imported)


def _refresh_bundle_ui(settings) -> None:
    offers = runtime.get("bundle_offers", {})
    settings.bundle_count = len(offers)
    if not offers:
        settings.bundle_info = ""
        settings.bundle_sender = ""
        settings.bundle_id = ""
        return
    newest = max(offers.items(), key=lambda item: item[1].get("stamp", 0.0))
    bundle_id, entry = newest
    summary = entry.get("summary") or {}
    settings.bundle_id = bundle_id
    settings.bundle_sender = entry.get("sender", "")
    settings.bundle_info = (
        f"{summary.get('objects', 0)} object(s), {summary.get('images', 0)} image(s)"
        f" - {summary.get('vertices', 0):,} verts"
    )


def _handle_bundle_offer(context, manifest: dict) -> None:
    try:
        summary = asset_bundle.summary(manifest)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return
    bundle_id = summary.get("id", "")
    if not bundle_id:
        return
    offers = runtime.setdefault("bundle_offers", {})
    offers[bundle_id] = {
        "manifest": manifest,
        "sender": str(manifest.get("sender", ""))[:48] or "someone",
        "summary": summary,
        "stamp": time.monotonic(),
    }
    while len(offers) > 4:
        oldest = min(offers, key=lambda key: offers[key].get("stamp", 0.0))
        offers.pop(oldest, None)
    _refresh_bundle_ui(context.window_manager.bmp)
    sender = offers[bundle_id]["sender"]
    _push_message(
        context,
        "system",
        f"{sender} sent {summary.get('objects', 0)} object(s). Accept it in Collaboration.",
    )
    eventlog.log(
        "info",
        "ASSET_BUNDLE_OFFER",
        "asset bundle received",
        bundle=bundle_id,
        sender=sender,
        objects=summary.get("objects", 0),
        images=summary.get("images", 0),
    )


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
            coords.append((first.x, first.y, first.z))
            coords.append((second.x, second.y, second.z))
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

def _readable_label(color) -> tuple[float, float, float]:
    """Brighten dark per-user colours so name tags stay legible on any theme."""
    try:
        red, green, blue = (max(0.0, min(1.0, float(color[index]))) for index in range(3))
    except (TypeError, ValueError, IndexError):
        return (1.0, 1.0, 1.0)
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    if luminance < 0.6:
        mix = (0.6 - luminance) / max(0.0001, 1.0 - luminance)
        red += (1.0 - red) * mix
        green += (1.0 - green) * mix
        blue += (1.0 - blue) * mix
    return (red, green, blue)


def _with_opacity(color, opacity: float):
    values = tuple(float(value) for value in color[:4])
    if len(values) < 4:
        values = values + (1.0,) * (4 - len(values))
    return values[:3] + (max(0.0, min(1.0, values[3] * float(opacity))),)


def _tag_view3d_redraw() -> None:
    """Force the 3D viewport to repaint so remote overlays animate live.

    Blender does not continuously redraw an idle viewport. Without this, a
    remote camera only appears to move when the local user moves their own
    camera (which triggers a repaint), which is exactly the stale-camera bug.
    """
    try:
        screen = getattr(bpy.context, "screen", None)
        if screen is None:
            return
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:  # noqa: BLE001
        pass


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

    show_cameras = bool(getattr(settings, "show_cameras", False))
    show_selections = bool(getattr(settings, "show_selections", False))
    show_lasers = bool(getattr(settings, "show_lasers", False))
    colorblind = bool(getattr(settings, "colorblind_safe", False))
    opacity = float(getattr(settings, "overlay_opacity", 0.85) or 0.85)
    overlay_scale = float(getattr(settings, "overlay_scale", 1.0) or 1.0)
    runtime["overlay_scale"] = max(0.5, min(2.0, overlay_scale))

    lines = []
    labels = []
    points = []
    total = 0
    point_total = 0
    max_segment = 0.0
    seen_users = set()

    if show_lasers:
        now_laser = time.monotonic()
        for name, entry in list(runtime.get("lasers", {}).items()):
            stamp = float(entry.get("stamp", 0.0))
            if now_laser - stamp > 1.2:
                runtime["lasers"].pop(name, None)
                continue
            raw_points = entry.get("points") or []
            if not raw_points:
                continue
            try:
                point = Vector((float(raw_points[0][0]), float(raw_points[0][1]), float(raw_points[0][2])))
            except (TypeError, ValueError, IndexError):
                continue
            color = effective_color(name, DEFAULT_COLOR, colorblind)
            laser_color = _with_opacity((1.0, 0.82, 0.12, 1.0), opacity)
            size = max(0.04, min(0.8, 0.12 * max(0.5, overlay_scale)))
            segments = [
                (point - Vector((size, 0.0, 0.0)), point + Vector((size, 0.0, 0.0))),
                (point - Vector((0.0, size, 0.0)), point + Vector((0.0, size, 0.0))),
                (point - Vector((0.0, 0.0, size)), point + Vector((0.0, 0.0, size))),
            ]
            lines.append((laser_color, segments))
            points.append((laser_color, [point]))
            labels.append((point + Vector((0.0, 0.0, size * 1.4)), f"{name} *", color))
            total += len(segments)
            point_total += 1
            max_segment = max(max_segment, size * 2.0)

    for user in settings.users:
        if user.name == settings.name:
            continue
        seen_users.add(user.name)
        color = _with_opacity(effective_color(user.name, user.color, colorblind), opacity)
        if show_cameras and user.has_corners:
            corners = list(user.view_corners)
            if len(corners) == 21:
                camera_points = [
                    Vector(
                        (
                            corners[index * 3],
                            corners[index * 3 + 1],
                            corners[index * 3 + 2],
                        )
                    )
                    for index in range(7)
                ]
                # Multi-User's exact camera widget indices.
                indices = (
                    (1, 3), (2, 1), (3, 0),
                    (2, 0), (4, 5), (1, 6),
                    (2, 6), (3, 6), (0, 6),
                )
                segments = [
                    (camera_points[first], camera_points[second])
                    for first, second in indices
                ]
                lines.append((color, segments))
                # The name sits on the top-right corner, like Multi-User.
                labels.append((camera_points[1], user.name, color))
                total += len(segments)
                for first, second in segments:
                    max_segment = max(
                        max_segment, float((second - first).length)
                    )
        if show_selections:
            uids = [uid for uid in str(user.selected or "").split(",") if uid]
            for uid in uids[:6]:
                obj = obj_sync.find(uid)
                if obj is None:
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
    signature = tuple(
        (
            user.name,
            tuple(round(float(value), 2) for value in user.view_corners)
            if user.has_corners
            else (),
            str(user.selected or ""),
            tuple(round(float(value), 2) for value in user.cursor)
            if user.has_cursor
            else (),
        )
        for user in settings.users
        if user.name != settings.name
    )
    if signature != runtime.get("remote_signature"):
        runtime["remote_signature"] = signature
        _tag_view3d_redraw()
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
        overlay_scale = float(runtime.get("overlay_scale", 1.0) or 1.0)
        try:
            gpu.state.line_width_set(max(1.0, 2.0 * overlay_scale))
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
                gpu.state.point_size_set(max(2.0, 4.0 * overlay_scale))
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
    overlay_scale = float(runtime.get("overlay_scale", 1.0) or 1.0)
    lift = 8.0 * overlay_scale
    try:
        blf.size(font, 16.0 * overlay_scale, 72.0)
    except Exception:  # noqa: BLE001
        try:
            blf.size(font, 16)
        except Exception:  # noqa: BLE001
            pass
    drawn = []
    try:
        # A previous draw (or an older addon build) can leave the native blurry
        # shadow enabled on this font; clear it so only our crisp outline shows.
        blf.disable(font, blf.SHADOW)
    except Exception:  # noqa: BLE001
        pass
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
            label = str(name)[:24]
            text_width, _text_height = blf.dimensions(font, label)
            left = round(x - text_width * 0.5)
            base = round(y)
            # One clean drop shadow and one fill. The old four-way outline plus
            # a double fill made the tag look bold and smeared on high-DPI
            # screens.
            shadow = max(1, int(round(overlay_scale)))
            blf.color(font, 0.0, 0.0, 0.0, 0.85)
            blf.position(font, left + shadow, base - shadow, 0.0)
            blf.draw(font, label)
            red, green, blue = _readable_label(color)
            blf.color(font, red, green, blue, 1.0)
            blf.position(font, left, base, 0.0)
            blf.draw(font, label)
            drawn.append((label, x, y))
        except Exception:  # noqa: BLE001
            pass


def _viewport_region_at(window, x: float, y: float):
    """Return (area, region, region_data) under a window-space point."""
    try:
        screen = window.screen
    except Exception:  # noqa: BLE001
        return None, None, None
    if screen is None:
        return None, None, None
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        if region is None:
            continue
        if not (
            region.x <= x < region.x + region.width
            and region.y <= y < region.y + region.height
        ):
            continue
        try:
            region_data = area.spaces.active.region_3d
        except Exception:  # noqa: BLE001
            region_data = None
        if region_data is not None:
            return area, region, region_data
    return None, None, None


def _remote_selection_names(settings, uid: str) -> list[str]:
    names = []
    for user in settings.users:
        if user.name == settings.name:
            continue
        selected = {item for item in str(user.selected or "").split(",") if item}
        if uid in selected:
            names.append(user.name)
    return names


def _hover_color(settings, name: str):
    for user in settings.users:
        if user.name == name:
            return effective_color(
                user.name,
                user.color,
                bool(getattr(settings, "colorblind_safe", False)),
            )
    return DEFAULT_COLOR


def _update_hover(context) -> None:
    """Ray-cast under the mouse and remember who is editing that object."""
    settings = getattr(context.window_manager, "bmp", None)
    session = runtime.get("session")
    if (
        settings is None
        or session is None
        or not getattr(settings, "show_hover_info", True)
    ):
        runtime["hover_info"] = None
        return
    mouse = runtime.get("hover_mouse")
    if not mouse:
        runtime["hover_info"] = None
        return
    try:
        window_x, window_y, stamp = mouse
    except (TypeError, ValueError):
        runtime["hover_info"] = None
        return
    if time.monotonic() - float(stamp) > 2.0:
        runtime["hover_info"] = None
        return
    window = getattr(context, "window", None)
    if window is None:
        runtime["hover_info"] = None
        return
    area, region, region_data = _viewport_region_at(window, window_x, window_y)
    if region is None or region_data is None:
        runtime["hover_info"] = None
        return
    from bpy_extras import view3d_utils

    region_x = float(window_x) - float(region.x)
    region_y = float(window_y) - float(region.y)
    try:
        origin = view3d_utils.region_2d_to_origin_3d(
            region, region_data, (region_x, region_y)
        )
        direction = view3d_utils.region_2d_to_vector_3d(
            region, region_data, (region_x, region_y)
        )
        depsgraph = context.evaluated_depsgraph_get()
        hit, _location, _normal, _index, obj, _matrix = context.scene.ray_cast(
            depsgraph, origin, direction
        )
    except Exception:  # noqa: BLE001
        runtime["hover_info"] = None
        return
    if not hit or obj is None:
        runtime["hover_info"] = None
        return
    uid = obj.get("bmp_uuid")
    if not uid:
        runtime["hover_info"] = None
        return
    names = _remote_selection_names(settings, uid)
    owner = runtime.get("lease_owners", {}).get(uid, "")
    if owner == settings.name:
        owner = ""
    if not names and not owner:
        runtime["hover_info"] = None
        return
    parts = []
    if owner:
        parts.append(f"{owner} is editing")
    others = [name for name in names if name != owner]
    if others:
        if len(others) == 1:
            parts.append(f"{others[0]} has it selected")
        else:
            parts.append(f"{', '.join(others[:3])} have it selected")
    text = " - ".join(parts)
    color = _hover_color(settings, owner or (others[0] if others else ""))
    runtime["hover_info"] = {
        "text": text[:140],
        "color": tuple(color),
        "object": str(obj.name)[:64],
        "region": region.as_pointer(),
        "area": area.as_pointer(),
        "mouse": (window_x, window_y),
        "stamp": time.monotonic(),
    }


def _draw_hover_tooltip() -> None:
    info = runtime.get("hover_info")
    if not info:
        return
    if time.monotonic() - float(info.get("stamp", 0.0)) > 2.0:
        runtime["hover_info"] = None
        return
    try:
        import blf
    except ImportError:
        return
    context = bpy.context
    region = getattr(context, "region", None)
    if region is None or info.get("region") != region.as_pointer():
        return
    mouse = info.get("mouse")
    text = str(info.get("text") or "")
    if not mouse or not text:
        return
    scale = float(runtime.get("overlay_scale", 1.0) or 1.0)
    font = 0
    try:
        blf.size(font, 14.0 * scale, 72.0)
    except Exception:  # noqa: BLE001
        try:
            blf.size(font, 14)
        except Exception:  # noqa: BLE001
            pass
    try:
        width, height = blf.dimensions(font, text)
    except Exception:  # noqa: BLE001
        width, height = 8.0 * len(text), 14.0
    pad = 6.0 * scale
    box_w = width + pad * 2.0
    box_h = height + pad * 2.0
    x = float(mouse[0]) - float(region.x)
    y = float(mouse[1]) - float(region.y)
    left = x + 16.0 * scale
    bottom = y + 18.0 * scale
    if left + box_w > region.width - 4.0:
        left = x - 16.0 * scale - box_w
    if bottom + box_h > region.height - 4.0:
        bottom = y - 18.0 * scale - box_h
    left = max(4.0, left)
    bottom = max(4.0, bottom)
    color = info.get("color") or (1.0, 1.0, 1.0)
    accent_color = _readable_label(color)
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader

        try:
            shader = runtime.get("tooltip_shader")
            if shader is None:
                try:
                    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
                except Exception:  # noqa: BLE001
                    try:
                        shader = gpu.shader.from_builtin("2D_UNIFORM_COLOR")
                    except Exception:  # noqa: BLE001
                        shader = gpu.shader.from_builtin("3D_UNIFORM_COLOR")
                runtime["tooltip_shader"] = shader
        except Exception:  # noqa: BLE001
            return
        quad = batch_for_shader(
            shader,
            "TRI_FAN",
            {
                "pos": [
                    (left, bottom),
                    (left + box_w, bottom),
                    (left + box_w, bottom + box_h),
                    (left, bottom + box_h),
                ]
            },
        )
        gpu.state.blend_set("ALPHA")
        shader.uniform_float("color", (0.03, 0.03, 0.04, 0.9))
        quad.draw(shader)
        accent = batch_for_shader(
            shader,
            "LINES",
            {
                "pos": [
                    (left + 1.0, bottom + 1.0),
                    (left + 1.0, bottom + box_h - 1.0),
                ]
            },
        )
        try:
            gpu.state.line_width_set(max(2.0, 3.0 * scale))
        except Exception:  # noqa: BLE001
            pass
        shader.uniform_float(
            "color", (accent_color[0], accent_color[1], accent_color[2], 1.0)
        )
        accent.draw(shader)
        try:
            gpu.state.line_width_set(1.0)
        except Exception:  # noqa: BLE001
            pass
        gpu.state.blend_set("NONE")
    except Exception:  # noqa: BLE001
        pass
    try:
        blf.color(font, 0.0, 0.0, 0.0, 0.9)
        blf.position(font, left + pad + 1.0, bottom + pad - 1.0, 0.0)
        blf.draw(font, text)
        blf.color(font, 1.0, 1.0, 1.0, 1.0)
        blf.position(font, left + pad, bottom + pad, 0.0)
        blf.draw(font, text)
    except Exception:  # noqa: BLE001
        pass


def _ensure_hover_operator(context) -> None:
    """Start the passive hover watcher when a session is active."""
    if runtime.get("hover_op_active"):
        return
    if runtime.get("session") is None:
        return
    settings = getattr(context.window_manager, "bmp", None)
    if settings is None or not getattr(settings, "show_hover_info", True):
        return
    now = time.monotonic()
    if now - float(runtime.get("hover_op_try", 0.0)) < 2.0:
        return
    runtime["hover_op_try"] = now
    window = getattr(context, "window", None)
    if window is None:
        windows = list(getattr(context.window_manager, "windows", []))
        window = windows[0] if windows else None
    if window is None:
        return
    try:
        screen = window.screen
    except Exception:  # noqa: BLE001
        return
    if screen is None:
        return
    area = next((item for item in screen.areas if item.type == "VIEW_3D"), None)
    if area is None:
        return
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        return
    try:
        with context.temp_override(
            window=window, screen=screen, area=area, region=region
        ):
            bpy.ops.bmp.hover("INVOKE_DEFAULT")
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
        if runtime.get("draw_hover") is None:
            runtime["draw_hover"] = SpaceView3D.draw_handler_add(
                _draw_hover_tooltip, (), "WINDOW", "POST_PIXEL"
            )
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _unregister_remote_draw() -> None:
    for key in ("draw_lines", "draw_labels", "draw_hover"):
        handle = runtime.pop(key, None)
        if handle is None:
            continue
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
        except Exception:  # noqa: BLE001
            pass
    runtime["hover_op_active"] = False
    runtime["hover_info"] = None
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
    if now - runtime.get("last_subscription", 0.0) < runtime.get("subscription_interval", 0.5):
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
            visible = float(local.z) < 0.0 and abs(float(local.x)) / depth <= aspect * 1.15 and abs(float(local.y)) / depth <= 1.15
            if visible and distance < 55.0:
                priority = 0
            elif distance < 165.0:
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
    if now - runtime.get("last_client_stats", 0.0) < runtime.get("stats_interval", 1.0):
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
    fps = float(runtime.get("tick_fps", 60.0) or 60.0)
    session.send_client_stats(
        {
            "apply_ms_p95": round(float(p95), 2),
            "fps": round(max(1.0, min(240.0, fps)), 1),
            "rx_kbps": round(max(0.0, (total_in - previous_in) / 1024.0 / elapsed), 1),
            "tx_kbps": round(max(0.0, (total_out - previous_out) / 1024.0 / elapsed), 1),
            "queue_depth": 0,
        }
    )


def _scene_fingerprint(context) -> str:
    """Cheap structural fingerprint: object identities, types, counts, slots.

    It deliberately ignores vertex positions and frame-dependent values, so two
    users watching different frames still match when their models match.
    """
    digest = hashlib.blake2b(digest_size=8)
    try:
        objects = sorted(context.scene.objects, key=lambda item: item.name)
    except Exception:  # noqa: BLE001
        return ""
    for obj in objects:
        if obj.get("bmp_ping"):
            continue
        uid = str(obj.get("bmp_uuid") or obj.name)
        digest.update(uid.encode("utf-8", "replace"))
        digest.update(b"|")
        digest.update(str(obj.type).encode("ascii", "replace"))
        if obj.type == "MESH":
            try:
                digest.update(str(len(obj.data.vertices)).encode("ascii"))
                digest.update(b":")
                digest.update(str(len(obj.data.polygons)).encode("ascii"))
            except (AttributeError, ReferenceError):
                pass
        for slot in getattr(obj, "material_slots", [])[:16]:
            digest.update(b"|")
            digest.update(
                (slot.material.name if slot.material is not None else "").encode(
                    "utf-8", "replace"
                )
            )
    return digest.hexdigest()


def _refresh_scene_hash(context) -> None:
    now = time.monotonic()
    if now - runtime.get("last_scene_hash", 0.0) < 2.0:
        return
    runtime["last_scene_hash"] = now
    local = _scene_fingerprint(context)
    runtime["scene_hash"] = local
    settings = context.window_manager.bmp
    mismatch = [
        user.name
        for user in settings.users
        if user.name != settings.name and user.scene_hash and user.scene_hash != local
    ]
    settings.scene_mismatch = ", ".join(mismatch[:4])


def _send_presence(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    now = time.monotonic()
    if now - runtime["last_presence"] < runtime.get("presence_interval", PRESENCE_POLL):
        return
    runtime["last_presence"] = now
    full = {
        "scene": str(context.scene.name)[:64],
        "frame": int(context.scene.frame_current),
        "playing": bool(getattr(context.scene, "is_animation_playing", False)),
        "fps": int(context.scene.render.fps),
        "mode": str(context.mode)[:32],
        "active": (
            str(context.active_object.name)[:64]
            if context.active_object is not None
            else ""
        ),
        "selected": ",".join(sorted(_selected_uids(context))),
        "scene_hash": runtime.get("scene_hash", ""),
    }
    if getattr(context.window_manager.bmp, "share_view", False):
        full["cursor"] = [
            round(float(value), 4) for value in context.scene.cursor.location
        ]
        view = _viewport_view(context)
        if view:
            full["view"] = [round(float(value), 3) for value in view["matrix"]]
            full["aspect"] = round(float(view["aspect"]), 3)
            full["persp"] = view["persp"]
            full["dist"] = round(float(view["distance"]), 3)
        corners = _viewport_corners(context)
        if corners:
            full["view_corners"] = [
                round(float(value), 3) for point in corners for value in point
            ]

    # Delta-encode presence. While the user is idle this sends nothing except a
    # small full snapshot every 30 seconds. The old code sent the full view
    # matrix at 20-30 Hz even when nothing had changed, which dominated internet
    # usage.
    shadow = runtime.setdefault("presence_shadow", {})
    changed = {
        key: value for key, value in full.items() if shadow.get(key) != value
    }
    if not changed:
        return
    if now - runtime.get("presence_full_at", 0.0) > 30.0:
        changed = dict(full)
        runtime["presence_full_at"] = now
    shadow.update(changed)
    session.send_presence(changed)


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
    entries = [
        entry
        for entry in (runtime.get("crash_entries") or [])
        if str(entry.get("addon", "")) == ADDON_VERSION
    ]
    runtime["crash_entries"] = entries
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
    settings.tick_errors = "; ".join(runtime.get("tick_errors", [])[-3:])
    stats = session.stats() if session else {}
    mesh_status = session.mesh_status() if session is not None else {"active": False, "peers": []}
    if mesh_status.get("active"):
        count = len(mesh_status.get("peers") or [])
        settings.mesh_info = f"P2P mesh: {count} direct peer(s)"
    else:
        settings.mesh_info = ""
    render_manager = runtime.get("render_manager")
    render_status = render_manager.status() if render_manager is not None else {"active": False}
    render_status["request"] = runtime.get("render_request")
    render_status["role"] = "host" if settings.is_host else "client"
    if render_status.get("active") or render_status.get("finished"):
        settings.render_progress = (
            f"{render_status.get('done', 0)}/{render_status.get('total', 0)} frames "
            f"({render_status.get('percent', 0):.0f}%)"
        )
    else:
        settings.render_progress = ""
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
            "color": list(effective_color(user.name, user.color, settings.colorblind_safe)),
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
    leases = []
    for key, owner in list(runtime.get("lease_owners", {}).items())[:64]:
        obj = obj_sync.find(key)
        leases.append(
            {
                "object": obj.name[:64] if obj is not None else key[:8],
                "owner": str(owner)[:48],
                "requests": [
                    str(name)[:48]
                    for name in (runtime.get("lease_requests", {}).get(key) or [])[:4]
                ],
            }
        )
    sync_queue = [
        {
            "name": str(peer.get("name", ""))[:48],
            "queue": int(peer.get("queue", 0) or 0),
            "pending": int(peer.get("pending", 0) or 0),
            "quality": int(peer.get("quality", 0) or 0),
            "rtt_ms": float(peer.get("rtt_ms", 0.0) or 0.0),
        }
        for peer in stats.get("peers", [])
        if isinstance(peer, dict)
    ][:64]
    control.write_status(
        {
            "state": state,
            "code": code,
            "is_host": is_host,
            "name": name,
            "role": "owner" if is_host else settings.role,
            "overlay_info": settings.overlay_info,
            "direct_code": settings.direct_code,
            "direct_codes": [
                code for code in settings.direct_codes.split(",") if code
            ][:4],
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
            "mesh": session.mesh_status() if session is not None else {"active": False, "peers": []},
            "rtt_ms": live_rtt,
            "bytes_in": stats.get("bytes_in", 0),
            "bytes_out": stats.get("bytes_out", 0),
            "peers": stats.get("peers", []),
            "users": users,
            "messages": messages,
            "leases": leases,
            "sync": sync_queue,
            "broadcast_url": settings.broadcast_url,
            "bridge_url": settings.bridge_url,
            "bridge_clients": settings.bridge_clients,
            "recording": settings.recording_info if settings.recording_active else "",
            "farm_url": settings.farm_url,
            "farm_workers": settings.farm_workers,
            "farm_progress": settings.farm_progress,
            "farm_active": settings.farm_active,
            "render": render_status,
            "internet_mode": settings.internet_mode,
            "lan_fast": settings.lan_fast,
            "scene_hash": runtime.get("scene_hash", ""),
            "scene_mismatch": settings.scene_mismatch,
            "perf": runtime["perf"].snapshot(),
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
    if session is None or runtime.get("replaying"):
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
    owner = runtime["lease_owners"].get(uid)
    if owner not in (None, context.window_manager.bmp.name):
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
    lan_peer = bool(
        session is not None and session.is_host and session.has_lan_peer()
    )
    fast = not relay or lan_peer
    runtime["fast_mode"] = fast
    runtime["lan_fast"] = lan_peer if (session is not None and session.is_host) else (not relay)
    settings = bpy.context.window_manager.bmp if bpy.context.window_manager else None
    if settings is not None:
        settings.internet_mode = not fast
        settings.lan_fast = bool(runtime.get("lan_fast"))
    runtime["transform_interval"] = 1.0 / 60.0 if fast else 1.0 / 30.0
    # Presence is delta-encoded, so this is the maximum update rate while the
    # user is moving. Relay sessions send far fewer packets; an idle session
    # sends only a small full snapshot every 30 s.
    if fast:
        runtime["presence_interval"] = 1.0 / 30.0
        runtime["subscription_interval"] = 0.5
        runtime["stats_interval"] = 1.0
    else:
        runtime["presence_interval"] = 1.0 / (5.0 if rtt > 150.0 else 10.0)
        runtime["subscription_interval"] = 5.0
        runtime["stats_interval"] = 2.0
    runtime["transform_smooth"] = 0.018 if fast else 0.035
    runtime["mesh_smooth"] = 0.025 if fast else 0.045
    runtime["view_smooth"] = 0.045 if fast else 0.075


def _safe_tick_step(name: str, func) -> None:
    """Run one tick step; never let a single failure kill the whole tick."""
    try:
        func()
    except Exception:  # noqa: BLE001
        try:
            runtime["tick_errors"] = (runtime.get("tick_errors", []) + [name])[-20:]
        except Exception:  # noqa: BLE001
            pass
        try:
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            pass


def bmp_tick():
    context = bpy.context
    if context is None or context.window_manager is None:
        return TICK
    try:
        tick_started = time.perf_counter()
        # Measure the addon's real tick rate for CLIENT_STATS. The old code sent
        # scene.render.fps, so a 24 fps scene made the host think the client was
        # slow and downgrade it to the lowest quality tier.
        runtime["tick_count"] = int(runtime.get("tick_count", 0)) + 1
        fps_now = time.monotonic()
        fps_at = float(runtime.get("tick_fps_at", 0.0))
        if fps_at <= 0.0:
            runtime["tick_fps_at"] = fps_now
        elif fps_now - fps_at >= 1.0:
            runtime["tick_fps"] = runtime["tick_count"] / max(0.001, fps_now - fps_at)
            runtime["tick_count"] = 0
            runtime["tick_fps_at"] = fps_now
        _safe_tick_step("poll_control", lambda: _poll_control(context))
        _safe_tick_step("bookmarks", lambda: _ensure_bookmarks(context))
        _safe_tick_step("heartbeat", lambda: _heartbeat_tick(context))
        session: SessionService | None = runtime.get("session")
        _safe_tick_step("timing", lambda: _update_timing(session))
        inbox = runtime["inbox"]
        _safe_tick_step("expire_pings", lambda: _expire_pings(context))
        if runtime.get("transfer_clear_at") and time.monotonic() > runtime["transfer_clear_at"]:
            runtime["transfer_clear_at"] = 0.0
            settings = context.window_manager.bmp
            settings.transfer_active = False
            settings.transfer_percent = 0.0
            settings.transfer_info = ""
        while True:
            try:
                kind, body = inbox.get_nowait()
            except queue.Empty:
                break
            started = time.monotonic()
            recorder = runtime.get("recording")
            if recorder is not None and not runtime.get("replaying"):
                try:
                    recorder.record(kind, body)
                except Exception:  # noqa: BLE001
                    pass
            publisher = runtime.get("broadcast")
            if publisher is not None:
                try:
                    publisher.publish(kind, body)
                except Exception:  # noqa: BLE001
                    pass
            try:
                _handle_event(context, kind, body)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            times = runtime.setdefault("apply_times", [])
            elapsed = (time.monotonic() - started) * 1000.0
            times.append(elapsed)
            runtime["perf"].record("apply", elapsed)
            if len(times) > 240:
                del times[: len(times) - 240]
        if session is not None:
            _safe_tick_step("drain_ai", lambda: _drain_ai_queue(context))
            _safe_tick_step("sync_selection", lambda: _sync_selection(context))
            _safe_tick_step("sync_transforms", lambda: _sync_transforms(context))
            _safe_tick_step("sync_mesh", lambda: _sync_mesh(context))
            _safe_tick_step("sync_edit_selection", lambda: _sync_edit_selection(context))
            _safe_tick_step("sync_materials", lambda: _sync_materials(context))
            _safe_tick_step("sync_modifiers", lambda: _sync_modifiers(context))
            _safe_tick_step("sync_pose", lambda: _sync_pose(context))
            _safe_tick_step("sync_shape_keys", lambda: _sync_shape_keys(context))
            _safe_tick_step("sync_actions", lambda: _sync_actions(context))
            _safe_tick_step("scan_images", lambda: _scan_images(context))
            _safe_tick_step("process_images", lambda: _process_image_queue(context))
            _safe_tick_step("refresh_scene_hash", lambda: _refresh_scene_hash(context))
            _safe_tick_step("send_presence", lambda: _send_presence(context))
            _safe_tick_step("send_subscription", lambda: _send_subscription(context))
            _safe_tick_step("send_client_stats", lambda: _send_client_stats(context))
        _safe_tick_step("journal_capture", lambda: _journal_capture(context))
        _safe_tick_step("auto_checkpoint", lambda: _maybe_auto_checkpoint(context))
        _safe_tick_step("scene_bootstrap", lambda: _pump_scene_bootstrap(context))
        _safe_tick_step("remote_visuals", lambda: _sync_remote_visuals(context))
        _safe_tick_step("hover_operator", lambda: _ensure_hover_operator(context))
        _safe_tick_step("bridge", lambda: _pump_bridge(context))
        _safe_tick_step("renderfarm", lambda: _pump_renderfarm(context))
        _safe_tick_step("session_render", lambda: _pump_session_render(context))
        _safe_tick_step("follow", lambda: _apply_follow(context))
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
        runtime["perf"].record(
            "tick",
            max(
                0.0,
                (time.perf_counter() - tick_started) * 1000.0
                - runtime.pop("farm_render_ms", 0.0),
            ),
        )
        _safe_tick_step("write_status", lambda: _write_status(context))
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


class BMP_OT_resync_scene(Operator):
    bl_idname = "bmp.resync_scene"
    bl_label = "Resync scene"
    bl_description = "Send the host scene to every connected client again"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        session: SessionService | None = runtime.get("session")
        if session is None or not session.is_host:
            self.report({"ERROR"}, "Only the host can resync the scene")
            return {"CANCELLED"}
        targets = [
            user.name
            for user in settings.users
            if user.name and user.name != settings.name
        ]
        if not targets:
            self.report({"INFO"}, "No clients connected")
            return {"CANCELLED"}
        for name in targets:
            _queue_scene_bootstrap(context, name)
        _push_message(
            context, "system", f"Resyncing the scene to {len(targets)} client(s)"
        )
        self.report({"INFO"}, f"Resyncing {len(targets)} client(s)")
        return {"FINISHED"}


class BMP_OT_release_locks(Operator):
    bl_idname = "bmp.release_locks"
    bl_label = "Release all locks"
    bl_description = "Release every edit lock in this session"
    bl_options = {"REGISTER"}

    def execute(self, context):
        session: SessionService | None = runtime.get("session")
        settings = context.window_manager.bmp
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        hidden = runtime.get("hidden_objects", set())
        for uid in list(hidden):
            obj = obj_sync.find(uid)
            if obj is not None:
                try:
                    obj.hide_select = False
                except Exception:  # noqa: BLE001
                    pass
        hidden.clear()
        if session.is_host:
            session.force_release_all()
            runtime["lease_owners"].clear()
            runtime["owned_leases"].clear()
            runtime["lease_activity"].clear()
            _push_message(context, "system", "Released all edit locks")
        else:
            for uid in list(runtime["owned_leases"]):
                _lease_request(session, uid, "release")
            runtime["owned_leases"].clear()
            for uid, owner in list(runtime["lease_owners"].items()):
                if owner == settings.name:
                    runtime["lease_owners"].pop(uid, None)
            runtime["lease_activity"].clear()
            runtime["lease_suppress_until"] = time.monotonic() + 10.0
            _push_message(context, "system", "Released your edit locks")
        self.report({"INFO"}, "Locks released")
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


class BMP_OT_copy_direct_code(Operator):
    bl_idname = "bmp.copy_direct_code"
    bl_label = "Copy Direct Code"
    bl_description = "Copy the direct LAN code to the clipboard"
    bl_options = {"REGISTER"}

    def execute(self, context):
        code = context.window_manager.bmp.direct_code
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


def _laser_point(context, event):
    try:
        from bpy_extras import view3d_utils
    except ImportError:
        return None
    region = getattr(context, "region", None)
    region_data = getattr(context, "region_data", None)
    if region is None or region_data is None:
        return None
    try:
        coord = (float(event.mouse_region_x), float(event.mouse_region_y))
        origin = view3d_utils.region_2d_to_origin_3d(region, region_data, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, region_data, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _normal, _index, _obj, _matrix = context.scene.ray_cast(
            depsgraph, origin, direction
        )
    except Exception:  # noqa: BLE001
        return None
    if not hit:
        try:
            distance = max(1.0, float(region_data.view_distance))
        except (TypeError, ValueError):
            distance = 7.0
        location = origin + direction * distance
    values = [float(location[0]), float(location[1]), float(location[2])]
    if not all(math.isfinite(value) and abs(value) < 1e7 for value in values):
        return None
    return values


class BMP_OT_laser(Operator):
    bl_idname = "bmp.laser"
    bl_label = "Laser Pointer"
    bl_description = "Point at the scene; move the mouse and click or press Esc to stop"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        session: SessionService | None = runtime.get("session")
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "Open a 3D viewport first")
            return {"CANCELLED"}
        self._session = session
        self._last = 0.0
        runtime["laser_active"] = True
        try:
            context.window.cursor_modal_set("CROSSHAIR")
        except Exception:  # noqa: BLE001
            pass
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, "Laser pointer active - move the mouse, click or Esc to stop")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session: SessionService | None = runtime.get("session")
        if session is None or session is not self._session or not session.is_alive():
            self._stop(context)
            return {"CANCELLED"}
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            now = time.monotonic()
            if now - self._last >= 1.0 / 30.0:
                self._last = now
                point = _laser_point(context, event)
                if point is not None:
                    session.send_laser([point])
                    runtime["lasers"][context.window_manager.bmp.name] = {
                        "points": [point],
                        "stamp": now,
                    }
            return {"RUNNING_MODAL"}
        if event.type in {"LEFTMOUSE", "RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            self._stop(context)
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _stop(self, context) -> None:
        runtime["laser_active"] = False
        try:
            context.window.cursor_modal_restore()
        except Exception:  # noqa: BLE001
            pass


class BMP_OT_hover(Operator):
    bl_idname = "bmp.hover"
    bl_label = "Collaborator hover info"
    bl_description = "Show who is editing the object under the mouse"
    bl_options = {"REGISTER", "INTERNAL"}

    def invoke(self, context, event):
        if context.area is None or context.area.type != "VIEW_3D":
            return {"CANCELLED"}
        self._window_ptr = context.window.as_pointer()
        self._timer = context.window_manager.event_timer_add(
            0.1, window=context.window
        )
        runtime["hover_op_active"] = True
        runtime["hover_mouse"] = (event.mouse_x, event.mouse_y, time.monotonic())
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session = runtime.get("session")
        settings = getattr(context.window_manager, "bmp", None)
        if (
            not runtime.get("hover_op_active")
            or session is None
            or settings is None
            or not getattr(settings, "show_hover_info", True)
            or getattr(context, "window", None) is None
            or context.window.as_pointer() != self._window_ptr
        ):
            self._finish(context)
            return {"CANCELLED"}
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            runtime["hover_mouse"] = (
                event.mouse_x,
                event.mouse_y,
                time.monotonic(),
            )
            now = time.monotonic()
            if now - float(runtime.get("hover_last_update", 0.0)) >= 0.05:
                runtime["hover_last_update"] = now
                _update_hover(context)
        elif event.type == "TIMER":
            now = time.monotonic()
            if now - float(runtime.get("hover_last_update", 0.0)) >= 0.35:
                runtime["hover_last_update"] = now
                _update_hover(context)
        return {"PASS_THROUGH"}

    def _finish(self, context) -> None:
        runtime["hover_op_active"] = False
        runtime["hover_info"] = None
        try:
            context.window_manager.event_timer_remove(self._timer)
        except Exception:  # noqa: BLE001
            pass


class BMP_OT_trust_host(Operator):
    bl_idname = "bmp.trust_host"
    bl_label = "Trust new certificate"
    bl_description = (
        "Replace the pinned certificate for this room with the one the host "
        "presented, then reconnect. Only do this after comparing the code with "
        "the host out of band"
    )

    def execute(self, context):
        settings = context.window_manager.bmp
        pending = settings.pending_fingerprint
        if not pending or len(pending) != 64:
            self.report({"ERROR"}, "No new certificate to trust")
            return {"CANCELLED"}
        last = runtime.get("last_join") or {}
        code = last.get("code") or settings.code
        if not code:
            self.report({"ERROR"}, "Join a room first")
            return {"CANCELLED"}
        from .core import tls

        tls.remember_fingerprint(control.data_dir(), code, pending)
        settings.pending_fingerprint = ""
        settings.last_error = ""
        settings.last_error_code = ""
        relay_url = last.get("relay_url") or ""
        start_join(
            context,
            code,
            relay_url or None,
            pending,
            bool(last.get("prefer_direct")),
            last.get("direct_code") or None,
            last.get("direct_codes"),
        )
        self.report({"INFO"}, "Trusted the new host certificate; rejoining")
        return {"FINISHED"}


def _export_mesh_data(obj, max_vertices: int = 20000):
    """Return local-space triangles for an object, or None if too large."""
    if obj.type != "MESH":
        return None
    evaluated = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            count = len(mesh.vertices)
            if count == 0 or count > max_vertices:
                return None
            positions = np.empty(count * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", positions)
            mesh.calc_loop_triangles()
            triangles = len(mesh.loop_triangles)
            if triangles == 0:
                return None
            indices = np.empty(triangles * 3, dtype=np.uint32)
            try:
                mesh.loop_triangles.foreach_get("vertices", indices)
            except (AttributeError, TypeError, RuntimeError):
                values = []
                for triangle in mesh.loop_triangles:
                    values.extend(triangle.vertices)
                indices = np.asarray(values, dtype=np.uint32)
            normals = np.empty(count * 3, dtype=np.float32)
            try:
                mesh.vertex_normals.foreach_get("vector", normals)
            except (AttributeError, TypeError, RuntimeError):
                normals = np.zeros(count * 3, dtype=np.float32)
            return {
                "positions": [round(float(value), 5) for value in positions],
                "normals": [round(float(value), 5) for value in normals],
                "indices": [int(value) for value in indices],
            }
        finally:
            evaluated.to_mesh_clear()
    except Exception:  # noqa: BLE001
        return None


def _exported_object(obj, include_mesh: bool = False):
    uid = obj.get("bmp_uuid")
    if not uid:
        uid = str(uuid.uuid4())
        obj["bmp_uuid"] = uid
    dimensions = obj.dimensions
    metadata = {
        "type": obj.type,
        "euler": [round(float(value), 6) for value in obj.rotation_euler],
        "materials": [
            slot.material.name
            for slot in getattr(obj, "material_slots", [])
            if slot.material is not None
        ],
        "parent": obj.parent.get("bmp_uuid") if obj.parent is not None else None,
    }
    return cross_engine.ExportedObject(
        uid=uid,
        name=obj.name,
        kind=obj.type.lower(),
        matrix=[float(value) for row in obj.matrix_world for value in row],
        bounds=(
            0.0,
            0.0,
            0.0,
            float(dimensions.x),
            float(dimensions.y),
            float(dimensions.z),
        ),
        metadata=metadata,
        visible=not bool(obj.hide_viewport),
        mesh=_export_mesh_data(obj) if include_mesh else None,
    )


def _exported_materials():
    result = []
    for material in bpy.data.materials:
        try:
            data = material_values(material)
        except Exception:  # noqa: BLE001
            continue
        base = data.get("base_color", (1.0, 1.0, 1.0, 1.0))
        result.append(
            cross_engine.ExportedMaterial(
                name=material.name,
                base_color=tuple(float(value) for value in list(base)[:4])
                + (1.0,) * max(0, 4 - len(list(base)[:4])),
                metallic=float(data.get("metallic", 0.0) or 0.0),
                roughness=float(data.get("roughness", 0.5) or 0.5),
            )
        )
    return result


def _start_broadcast(settings):
    server = runtime.get("broadcast")
    if server is not None:
        return server
    for offset in range(0, 10):
        candidate = broadcast.BroadcastServer(
            port=settings.broadcast_port + offset,
            title=f"Blendermp - {settings.name or settings.code or 'session'}",
        )
        try:
            candidate.start()
        except OSError:
            candidate.stop()
            continue
        runtime["broadcast"] = candidate
        settings.broadcast_active = True
        settings.broadcast_port = candidate.port
        settings.broadcast_url = candidate.url
        return candidate
    return None


def _stop_broadcast(settings):
    server = runtime.get("broadcast")
    runtime["broadcast"] = None
    settings.broadcast_active = False
    settings.broadcast_url = ""
    if server is not None:
        server.stop()


def _start_bridge(settings, context):
    server = runtime.get("bridge")
    if server is not None:
        return server
    registry = cross_engine.EngineRegistry.default()
    exporter = registry.get(settings.bridge_engine) or registry.get("unity")
    for offset in range(0, 10):
        candidate = cross_engine.BridgeServer(
            port=settings.bridge_port + offset, engine=exporter.name
        )
        try:
            candidate.start()
        except OSError:
            candidate.stop()
            continue
        runtime["bridge"] = candidate
        runtime["bridge_exporter"] = exporter
        runtime["bridge_shadow"] = {}
        runtime["bridge_last"] = 0.0
        runtime["bridge_materials_at"] = 0.0
        settings.bridge_active = True
        settings.bridge_port = candidate.port
        settings.bridge_url = candidate.url
        objects = [
            _exported_object(obj, include_mesh=settings.bridge_meshes)
            for obj in context.scene.objects
            if not obj.get("bmp_ping")
        ]
        candidate.publish_scene(exporter, objects, _exported_materials())
        return candidate
    return None


def _stop_bridge(settings):
    server = runtime.get("bridge")
    runtime["bridge"] = None
    runtime["bridge_exporter"] = None
    runtime["bridge_shadow"] = {}
    settings.bridge_active = False
    settings.bridge_url = ""
    settings.bridge_clients = 0
    if server is not None:
        server.stop()


def _pump_bridge(context) -> None:
    """Push changed objects to connected engines at 10 Hz."""
    bridge = runtime.get("bridge")
    exporter = runtime.get("bridge_exporter")
    if bridge is None or exporter is None:
        return
    settings = context.window_manager.bmp
    settings.bridge_clients = bridge.client_count
    now = time.monotonic()
    if now - runtime.get("bridge_last", 0.0) < 0.1:
        return
    runtime["bridge_last"] = now
    shadow = runtime.get("bridge_shadow", {})
    seen = set()
    for obj in context.scene.objects:
        if obj.get("bmp_ping"):
            continue
        uid = obj.get("bmp_uuid")
        if not uid:
            uid = str(uuid.uuid4())
            obj["bmp_uuid"] = uid
        seen.add(uid)
        signature = (
            tuple(round(float(value), 5) for row in obj.matrix_world for value in row),
            bool(obj.hide_viewport),
            tuple(
                slot.material.name if slot.material is not None else ""
                for slot in getattr(obj, "material_slots", [])
            ),
        )
        if shadow.get(uid) == signature:
            continue
        is_new = uid not in shadow
        shadow[uid] = signature
        try:
            bridge.publish_object(
                exporter,
                _exported_object(
                    obj, include_mesh=is_new and settings.bridge_meshes
                ),
            )
        except Exception:  # noqa: BLE001
            pass
    for uid in list(shadow):
        if uid not in seen:
            shadow.pop(uid, None)
            try:
                bridge.publish_remove(uid)
            except Exception:  # noqa: BLE001
                pass
    if now - runtime.get("bridge_materials_at", 0.0) > 2.0:
        runtime["bridge_materials_at"] = now
        try:
            bridge.publish_materials(exporter, _exported_materials())
        except Exception:  # noqa: BLE001
            pass
    if settings.bridge_meshes and bridge.client_count:
        if now - runtime.get("bridge_scene_at", 0.0) > 5.0:
            runtime["bridge_scene_at"] = now
            objects = [
                _exported_object(obj, include_mesh=True)
                for obj in context.scene.objects
                if not obj.get("bmp_ping")
            ]
            try:
                bridge.publish_scene(exporter, objects, _exported_materials())
            except Exception:  # noqa: BLE001
                pass


def _default_farm_output() -> str:
    return str(control.data_dir() / "renderfarm")


def _start_farm_server(settings):
    server = runtime.get("farm_server")
    if server is not None:
        return server
    base = settings.farm_output.strip() or _default_farm_output()
    for offset in range(0, 10):
        candidate = renderfarm.FarmServer(
            port=settings.farm_port + offset, output_dir=base
        )
        try:
            candidate.start()
        except OSError:
            candidate.stop()
            continue
        runtime["farm_server"] = candidate
        settings.farm_active = True
        settings.farm_role = "host"
        settings.farm_port = candidate.port
        settings.farm_url = candidate.url
        settings.farm_token = candidate.token
        settings.farm_workers = candidate.worker_count
        settings.farm_progress = candidate.queue.progress_text()
        return candidate
    return None


def _stop_farm_server(settings):
    server = runtime.get("farm_server")
    runtime["farm_server"] = None
    if server is not None:
        server.stop()
    if runtime.get("farm_client") is None:
        settings.farm_active = False
        settings.farm_role = ""
        settings.farm_url = ""
        settings.farm_token = ""
        settings.farm_workers = 0
        settings.farm_progress = ""


def _start_farm_client(settings):
    client = runtime.get("farm_client")
    if client is not None:
        return client
    url = settings.farm_client_url.strip()
    token = settings.farm_client_token.strip()
    if not url or not token:
        return None
    client = renderfarm.FarmClient(
        url, token, settings.name or "worker", poll_interval=0.75
    )
    client.start()
    runtime["farm_client"] = client
    settings.farm_active = True
    settings.farm_role = "client"
    settings.farm_url = url
    settings.farm_token = token
    settings.farm_progress = "waiting for work"
    return client


def _stop_farm_client(settings):
    client = runtime.get("farm_client")
    runtime["farm_client"] = None
    if client is not None:
        client.stop()
    if runtime.get("farm_server") is None:
        settings.farm_active = False
        settings.farm_role = ""
        settings.farm_url = ""
        settings.farm_token = ""
        settings.farm_workers = 0
        settings.farm_progress = ""


def _render_farm_frame(context, work, server=None, client=None):
    """Render one farm frame on Blender's main thread, then hand it back."""
    scene = context.scene
    if scene.camera is None:
        return False, "scene has no active camera"
    previous_frame = scene.frame_current
    previous_path = scene.render.filepath
    previous_format = scene.render.image_settings.file_format
    if server is not None:
        job = server.queue.get(work.job_id)
        if job is None:
            return False, "unknown render job"
        output_dir = Path(job.output_dir)
    else:
        output_dir = Path(control.data_dir()) / "renderfarm" / "worker"
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / f"frame_{int(work.frame):05d}"
    path = Path(str(base) + ".png")
    try:
        scene.render.image_settings.file_format = "PNG"
        scene.frame_set(int(work.frame))
        scene.render.filepath = str(base)
        bpy.ops.render.render(write_still=True)
        if not path.exists():
            alternate = Path(str(base))
            if alternate.exists():
                path = alternate
            else:
                return False, "render produced no PNG"
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    finally:
        try:
            scene.frame_set(previous_frame)
        except Exception:  # noqa: BLE001
            pass
        scene.render.filepath = previous_path
        scene.render.image_settings.file_format = previous_format
    if client is not None:
        if not client.submit(work, data):
            return False, client.last_error or "upload failed"
    elif server is not None:
        server.queue.complete(
            work.job_id, work.frame, context.window_manager.bmp.name or "host", str(path)
        )
    return True, ""


def _pump_renderfarm(context) -> None:
    """Claim/render at most one farm frame per tick."""
    settings = context.window_manager.bmp
    server = runtime.get("farm_server")
    client = runtime.get("farm_client")
    if server is None and client is None:
        return
    now = time.monotonic()
    if now - runtime.get("farm_last", 0.0) < 0.5:
        return
    runtime["farm_last"] = now
    if server is not None:
        settings.farm_workers = server.worker_count
        settings.farm_progress = server.queue.progress_text()
    if client is not None:
        status = client.status()
        settings.farm_workers = int(status.get("worker_count", 0) or 0)
        jobs = status.get("jobs") or []
        if jobs:
            job = jobs[-1]
            settings.farm_progress = (
                f"{job.get('done', 0)}/{job.get('frames', 0)} frames "
                f"({job.get('percent', 0)}%) · {settings.farm_workers} worker(s)"
            )
    if runtime.get("farm_busy"):
        return
    work = None
    if client is not None:
        work = client.poll()
        if work is not None:
            runtime["farm_busy"] = True
            started = time.perf_counter()
            try:
                ok, error = _render_farm_frame(context, work, client=client)
                if not ok:
                    client.fail(work, error)
                    _push_message(
                        context, "system", f"Farm frame {work.frame} failed: {error[:100]}"
                    )
            finally:
                runtime["farm_render_ms"] = (time.perf_counter() - started) * 1000.0
                runtime["farm_busy"] = False
    elif server is not None and settings.farm_host_worker:
        claimed = server.queue.claim(settings.name or "host")
        if claimed is not None:
            job, chunk = claimed
            work = renderfarm.FarmWork(
                job_id=job.id,
                job_name=job.name,
                frame=chunk.frame,
                output_name=f"frame_{chunk.frame:05d}.png",
                width=job.width,
                height=job.height,
            )
            runtime["farm_busy"] = True
            started = time.perf_counter()
            try:
                ok, error = _render_farm_frame(context, work, server=server)
                if not ok:
                    server.queue.fail(job.id, chunk.frame, settings.name or "host", error)
                    _push_message(
                        context, "system", f"Farm frame {chunk.frame} failed: {error[:100]}"
                    )
            finally:
                runtime["farm_render_ms"] = (time.perf_counter() - started) * 1000.0
                runtime["farm_busy"] = False


# --------------------------------------------------------------------------- session render
def _render_manager():
    manager = runtime.get("render_manager")
    if manager is None:
        manager = renderjob.RenderManager()
        runtime["render_manager"] = manager
    return manager


def _render_progress_text(status: dict) -> str:
    if not isinstance(status, dict):
        return ""
    total = int(status.get("total", 0) or 0)
    done = int(status.get("done", 0) or 0)
    failed = int(status.get("failed", 0) or 0)
    if not total:
        return ""
    text = f"{done}/{total} frames ({status.get('percent', 0):.0f}%)"
    if failed:
        text += f", {failed} failed"
    return text


def _start_session_render(context, start, end, step, format_hint, output) -> None:
    session: SessionService | None = runtime.get("session")
    settings = context.window_manager.bmp
    if session is None or not settings.is_host:
        settings.last_error = "Only the host can start a render"
        return
    manager = _render_manager()
    if manager.active():
        settings.last_error = "A render is already running"
        return
    scene = context.scene
    out_dir = output or str(control.data_dir() / "render")
    job = manager.create(
        name=f"Render {int(start)}-{int(end)}",
        start=int(start),
        end=int(end),
        step=int(step or 1),
        width=int(scene.render.resolution_x),
        height=int(scene.render.resolution_y),
        format_hint="PNG",
        engine=str(scene.render.engine),
        output_dir=out_dir,
        fps=int(getattr(scene.render, "fps", 24) or 24),
    )
    if job is None:
        settings.last_error = "Could not create the render job"
        return
    runtime["render_job_id"] = job.id
    runtime["render_output"] = out_dir
    runtime["render_format"] = str(format_hint or "MP4")[:8].upper()
    runtime["render_error"] = ""
    request = {
        "action": "request",
        "job": job.id,
        "name": job.name,
        "start": job.start,
        "end": job.end,
        "step": job.step,
        "width": job.width,
        "height": job.height,
        "format": job.format,
        "engine": job.engine,
    }
    peers = session.authenticated_peers()
    for name in peers:
        manager.ask(name)
    session.send_render_control("request", request)
    settings.render_progress = _render_progress_text(manager.status())
    _push_message(context, "system", f"Render {job.start}-{job.end} requested from {len(peers)} peer(s)")
    _write_status(context, force=True)


def _stop_session_render(context) -> None:
    settings = context.window_manager.bmp
    if not settings.is_host:
        settings.last_error = "Only the host can stop the render"
        return
    manager = _render_manager()
    if manager.stop():
        _push_message(context, "system", "Stopping the render; the rendered frames are kept")
        _write_status(context, force=True)
    else:
        settings.last_error = "No render is running"


def _handle_render_control(context, event: dict) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    settings = context.window_manager.bmp
    action = str(event.get("action", ""))
    message = event.get("body") if isinstance(event.get("body"), dict) else {}
    manager = _render_manager()
    if settings.is_host:
        job = manager.job()
        if job is None:
            return
        name = str(event.get("name") or "peer")[:48]
        if action == "accept":
            manager.accept(name)
            _push_message(context, "system", f"{name} joined the render")
        elif action == "decline":
            manager.decline(name)
            _push_message(context, "system", f"{name} declined the render")
        elif action == "need":
            frames = manager.claim(name, renderjob.MAX_BATCH)
            if frames:
                session.send_render_control_to(name, "work", {"job": job.id, "frames": frames})
        elif action == "fail":
            frame = message.get("frame")
            if isinstance(frame, int) and not isinstance(frame, bool):
                manager.fail(frame, name, str(message.get("error", ""))[:200])
        settings.render_progress = _render_progress_text(manager.status())
        return
    job_id = str(message.get("job", ""))
    if action == "request":
        runtime["render_request"] = {
            "job": job_id,
            "name": str(message.get("name", ""))[:80],
            "start": message.get("start", 0),
            "end": message.get("end", 0),
            "step": message.get("step", 1),
            "width": message.get("width", 0),
            "height": message.get("height", 0),
            "format": str(message.get("format", "PNG"))[:8],
            "engine": str(message.get("engine", ""))[:32],
        }
        runtime["render_width"] = int(message.get("width", 0) or 0)
        runtime["render_height"] = int(message.get("height", 0) or 0)
        _push_message(context, "system", f"{message.get('name', 'Host')} asked you to join a render")
        _write_status(context, force=True)
    elif action == "start":
        runtime["render_active"] = True
        runtime["render_job_id"] = job_id
        runtime["render_queue"] = []
        runtime["render_last_poll"] = 0.0
        _push_message(context, "system", "Render started")
    elif action == "work":
        frames = message.get("frames") or []
        for frame in frames:
            if isinstance(frame, int) and not isinstance(frame, bool):
                runtime["render_queue"].append((job_id, frame))
        runtime["render_active"] = True
        runtime["render_job_id"] = job_id
    elif action in {"cancel", "finish"}:
        runtime["render_active"] = False
        runtime["render_queue"] = []
        runtime["render_busy"] = False
        runtime["render_request"] = None
        _push_message(context, "system", "Render finished" if action == "finish" else "Render cancelled")


def _handle_render_frame(context, event: dict) -> None:
    manager = _render_manager()
    job = manager.job()
    if job is None:
        return
    try:
        body = render_payload.decode(event.get("payload", b""))
    except render_payload.RenderPayloadError:
        return
    if body["job"] != job.id:
        return
    try:
        path = renderjob.write_frame(job.output_dir, body["frame"], job.format, body["data"])
    except OSError:
        traceback.print_exc()
        return
    manager.complete(body["frame"], str(event.get("sender", "")), path)
    context.window_manager.bmp.render_progress = _render_progress_text(manager.status())
    _write_status(context, force=True)


def _render_session_frame(context, frame: int, width: int = 0, height: int = 0) -> tuple[bool, str, bytes]:
    scene = context.scene
    old_frame = scene.frame_current
    old_path = scene.render.filepath
    old_format = scene.render.image_settings.file_format
    old_x = scene.render.resolution_x
    old_y = scene.render.resolution_y
    old_pct = scene.render.resolution_percentage
    handle, path = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    try:
        scene.frame_set(int(frame))
        scene.render.filepath = path
        scene.render.image_settings.file_format = "PNG"
        if width and height:
            scene.render.resolution_x = int(width)
            scene.render.resolution_y = int(height)
            scene.render.resolution_percentage = 100
        bpy.ops.render.render(write_still=True)
        with open(path, "rb") as reader:
            data = reader.read()
        if not data:
            return False, "render produced no image", b""
        return True, "", data
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200], b""
    finally:
        try:
            scene.frame_set(old_frame)
        except Exception:  # noqa: BLE001
            pass
        scene.render.filepath = old_path
        scene.render.image_settings.file_format = old_format
        scene.render.resolution_x = old_x
        scene.render.resolution_y = old_y
        scene.render.resolution_percentage = old_pct
        try:
            os.unlink(path)
        except OSError:
            pass


def _compile_session_video(context, job) -> str:
    settings = context.window_manager.bmp
    output_format = str(runtime.get("render_format", "MP4")).upper()
    if output_format in {"NONE", "PNG", "SEQUENCE"}:
        return ""
    try:
        import imageio_ffmpeg
    except Exception:  # noqa: BLE001
        raise RuntimeError("imageio-ffmpeg is not installed")
    import subprocess

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    pattern = os.path.join(job.output_dir, "frame_%05d.png")
    extension = "webm" if output_format == "WEBM" else "mp4"
    output = os.path.join(job.output_dir, f"render_{job.id}.{extension}")
    codec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30"] if extension == "webm" else ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]
    command = [
        ffmpeg,
        "-y",
        "-framerate",
        str(max(1, int(getattr(job, "fps", 24) or 24))),
        "-i",
        pattern,
        *codec,
        output,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffmpeg failed").strip()[-300:])
    _push_message(context, "system", f"Render video saved to {output}")
    return output


def _pump_session_render(context) -> None:
    session: SessionService | None = runtime.get("session")
    if session is None:
        return
    settings = context.window_manager.bmp
    manager = _render_manager()
    now = time.time()
    if settings.is_host:
        job = manager.job()
        if job is None:
            return
        if not job.started and not job.finished:
            all_answered = bool(job.asked) and job.asked <= (job.accepted | job.declined)
            timed_out = bool(job.asked) and now - job.created_at > renderjob.CONSENT_TIMEOUT
            if all_answered or timed_out or not job.asked:
                manager.start()
                for name in job.accepted:
                    session.send_render_control_to(name, "start", {"job": job.id})
                if not job.accepted:
                    _push_message(context, "system", "No peers joined; rendering on the host only")
        if job.started and not job.finished and not runtime.get("render_busy"):
            frames = manager.claim(settings.name or "host", 1)
            if frames:
                runtime["render_busy"] = True
                try:
                    ok, error, data = _render_session_frame(context, frames[0], job.width, job.height)
                    if ok:
                        path = renderjob.write_frame(job.output_dir, frames[0], job.format, data)
                        manager.complete(frames[0], settings.name or "host", path)
                    else:
                        manager.fail(frames[0], settings.name or "host", error)
                finally:
                    runtime["render_busy"] = False
                settings.render_progress = _render_progress_text(manager.status())
                _write_status(context, force=True)
        if job.finished:
            if job.stopped:
                session.send_render_control("cancel", {"job": job.id})
                _push_message(context, "system", "Render stopped; the rendered frames were kept")
            else:
                if not job.error:
                    try:
                        _compile_session_video(context, job)
                    except Exception as exc:  # noqa: BLE001
                        job.error = str(exc)[:200]
                        settings.last_error = job.error
                session.send_render_control("finish", {"job": job.id})
            settings.render_progress = _render_progress_text(manager.status())
            manager.clear()
            _write_status(context, force=True)
            runtime["render_job_id"] = ""
        return
    if not runtime.get("render_active") or runtime.get("render_busy"):
        return
    queue = runtime.get("render_queue") or []
    if queue:
        job_id, frame = queue.pop(0)
        runtime["render_busy"] = True
        try:
            ok, error, data = _render_session_frame(
                context,
                frame,
                int(runtime.get("render_width", 0) or 0),
                int(runtime.get("render_height", 0) or 0),
            )
            if ok:
                session.send_render_frame(
                    job_id,
                    frame,
                    "png",
                    int(runtime.get("render_width", 0) or 0),
                    int(runtime.get("render_height", 0) or 0),
                    data,
                )
                runtime["render_done"] = int(runtime.get("render_done", 0)) + 1
            else:
                session.send_render_control("fail", {"job": job_id, "frame": frame, "error": error})
        finally:
            runtime["render_busy"] = False
        if not queue:
            session.send_render_control("need", {"job": job_id})
        return
    if now - runtime.get("render_last_poll", 0.0) > 3.0:
        runtime["render_last_poll"] = now
        session.send_render_control("need", {"job": runtime.get("render_job_id", "")})


class BMP_OT_ai_ask(Operator):
    bl_idname = "bmp.ai_ask"
    bl_label = "Ask"
    bl_description = "Ask the copilot to propose edits to this scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if not settings.ai_enabled:
            self.report({"ERROR"}, "Enable the AI copilot first")
            return {"CANCELLED"}
        from .core import ai as ai_core

        if runtime.get("ai_session") is None:
            handlers = _ai_handlers()
            tools = ai_core.build_default_tools(context, handlers)
            client = ai_core.OllamaClient(
                settings.ai_url,
                settings.ai_model,
                timeout=float(settings.ai_timeout),
            )
            runtime["ai_session"] = ai_core.AISession(client, tools)
        else:
            runtime["ai_session"].client = ai_core.OllamaClient(
                settings.ai_url,
                settings.ai_model,
                timeout=float(settings.ai_timeout),
            )
        prompt = settings.ai_prompt.strip()
        if not prompt:
            self.report({"ERROR"}, "Type a prompt first")
            return {"CANCELLED"}
        settings.ai_status = "thinking"
        settings.ai_pending_text = ""
        settings.ai_pending_count = 0
        settings.ai_reply = ""
        settings.last_error = ""
        runtime["ai_pending_edits"] = []
        runtime["ai_session"].ask(
            prompt, callback=_ai_callback, scene_context=_ai_scene_context(context)
        )
        self.report({"INFO"}, "Thinking...")
        return {"FINISHED"}


class BMP_OT_ai_test(Operator):
    bl_idname = "bmp.ai_test"
    bl_label = "Test connection"
    bl_description = "Check that Ollama is running and the model is installed"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        from .core import ai as ai_core

        client = ai_core.OllamaClient(
            settings.ai_url,
            settings.ai_model,
            timeout=float(min(60, settings.ai_timeout)),
        )
        ok, message = client.health()
        if ok:
            settings.ai_status = "ready"
            self.report({"INFO"}, message)
        else:
            settings.ai_status = "error"
            settings.last_error = message[:180]
            settings.last_error_code = "AI_ERROR"
            self.report({"ERROR"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BMP_OT_ai_apply_all(Operator):
    bl_idname = "bmp.ai_apply_all"
    bl_label = "Apply all"
    bl_description = "Apply every proposed edit"
    bl_options = {"REGISTER"}

    def execute(self, context):
        session = runtime.get("ai_session")
        settings = context.window_manager.bmp
        if session is None or not session.pending:
            self.report({"INFO"}, "No pending edits")
            return {"CANCELLED"}
        results = list(session.pending)
        applied = session.apply_all(context)
        failed = [
            edit
            for edit in results
            if edit.result
            and (not edit.result.get("ok", True) or edit.result.get("error"))
        ]
        settings.ai_pending_text = "\n".join(
            f"{edit.summary} -> {edit.result.get('error', 'failed') if edit.result else 'failed'}"
            for edit in failed[:6]
        )
        settings.ai_pending_count = len(failed)
        runtime["ai_pending_edits"] = failed
        settings.ai_status = "idle" if not failed else "error"
        if failed:
            settings.last_error = f"{len(failed)} AI edit(s) failed"
            settings.last_error_code = "AI_ERROR"
        self.report({"INFO"}, f"Applied {applied} edit(s), {len(failed)} failed")
        return {"FINISHED"}


class BMP_OT_ai_clear(Operator):
    bl_idname = "bmp.ai_clear"
    bl_label = "Clear"
    bl_description = "Discard pending edits and the conversation"
    bl_options = {"REGISTER"}

    def execute(self, context):
        session = runtime.get("ai_session")
        settings = context.window_manager.bmp
        if session is not None:
            session.clear()
        settings.ai_pending_text = ""
        settings.ai_pending_count = 0
        settings.ai_status = "idle"
        settings.ai_reply = ""
        runtime["ai_pending_edits"] = []
        self.report({"INFO"}, "Cleared")
        return {"FINISHED"}


class BMP_OT_record_start(Operator):
    bl_idname = "bmp.record_start"
    bl_label = "Start recording"
    bl_description = "Record this session's events to a JSONL file for replay and debugging"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if runtime.get("recording") is not None:
            self.report({"INFO"}, "Already recording")
            return {"CANCELLED"}
        recorder = recording.Recorder(
            str(control.data_dir() / "recordings" / f"session-{int(time.time())}.jsonl")
        )
        try:
            recorder.start()
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not start recording: {exc}")
            return {"CANCELLED"}
        runtime["recording"] = recorder
        settings.recording_active = True
        settings.recording_info = recorder.path.name
        _push_message(context, "system", f"Recording to {recorder.path.name}")
        self.report({"INFO"}, f"Recording to {recorder.path.name}")
        return {"FINISHED"}


class BMP_OT_record_stop(Operator):
    bl_idname = "bmp.record_stop"
    bl_label = "Stop recording"
    bl_description = "Stop the active recording"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        recorder = runtime.get("recording")
        if recorder is None:
            self.report({"INFO"}, "No recording active")
            return {"CANCELLED"}
        count = recorder.event_count
        recorder.stop()
        runtime["recording"] = None
        settings.recording_active = False
        settings.recording_info = f"{count} events"
        _push_message(context, "system", f"Recording stopped ({count} events)")
        self.report({"INFO"}, f"Recording stopped ({count} events)")
        return {"FINISHED"}


class BMP_OT_broadcast_start(Operator):
    bl_idname = "bmp.broadcast_start"
    bl_label = "Start broadcast"
    bl_description = "Serve a live read-only view of this session in a browser"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        server = _start_broadcast(settings)
        if server is None:
            self.report({"ERROR"}, "Could not bind a broadcast port")
            return {"CANCELLED"}
        _push_message(context, "system", f"Live view: {settings.broadcast_url}")
        self.report({"INFO"}, f"Live view: {settings.broadcast_url}")
        return {"FINISHED"}


class BMP_OT_broadcast_stop(Operator):
    bl_idname = "bmp.broadcast_stop"
    bl_label = "Stop broadcast"
    bl_description = "Stop the live browser view"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        _stop_broadcast(settings)
        _push_message(context, "system", "Live view stopped")
        return {"FINISHED"}


class BMP_OT_broadcast_open(Operator):
    bl_idname = "bmp.broadcast_open"
    bl_label = "Open live view"
    bl_description = "Open the live session view in the default browser"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if not settings.broadcast_url:
            self.report({"ERROR"}, "Start the broadcast first")
            return {"CANCELLED"}
        try:
            import webbrowser

            webbrowser.open(settings.broadcast_url)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not open a browser: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class BMP_OT_bridge_start(Operator):
    bl_idname = "bmp.bridge_start"
    bl_label = "Start bridge"
    bl_description = "Stream this scene to a Unity, Unreal or Godot receiver"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        server = _start_bridge(settings, context)
        if server is None:
            self.report({"ERROR"}, "Could not bind a bridge port")
            return {"CANCELLED"}
        _push_message(
            context,
            "system",
            f"{settings.bridge_engine.title()} bridge: {settings.bridge_url}",
        )
        self.report({"INFO"}, f"Bridge: {settings.bridge_url}")
        return {"FINISHED"}


class BMP_OT_bridge_stop(Operator):
    bl_idname = "bmp.bridge_stop"
    bl_label = "Stop bridge"
    bl_description = "Stop the cross-engine bridge"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        _stop_bridge(settings)
        _push_message(context, "system", "Cross-engine bridge stopped")
        return {"FINISHED"}


class BMP_OT_farm_start(Operator):
    bl_idname = "bmp.farm_start"
    bl_label = "Start farm host"
    bl_description = "Host a LAN render farm on this PC"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        server = _start_farm_server(settings)
        if server is None:
            self.report({"ERROR"}, "Could not bind a render farm port")
            return {"CANCELLED"}
        _push_message(
            context,
            "system",
            f"Render farm: {settings.farm_url} (use Copy farm invite for the token)",
        )
        self.report({"INFO"}, f"Farm: {settings.farm_url}")
        return {"FINISHED"}


class BMP_OT_farm_join(Operator):
    bl_idname = "bmp.farm_join"
    bl_label = "Join farm"
    bl_description = "Render frames for a LAN farm host"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        client = _start_farm_client(settings)
        if client is None:
            self.report({"ERROR"}, "Enter the host URL and token first")
            return {"CANCELLED"}
        _push_message(
            context,
            "system",
            f"Joined render farm as {settings.name or 'worker'}",
        )
        self.report({"INFO"}, "Joined render farm")
        return {"FINISHED"}


class BMP_OT_farm_stop(Operator):
    bl_idname = "bmp.farm_stop"
    bl_label = "Stop farm"
    bl_description = "Stop hosting or working for the render farm"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        _stop_farm_server(settings)
        _stop_farm_client(settings)
        _push_message(context, "system", "Render farm stopped")
        return {"FINISHED"}


class BMP_OT_farm_render(Operator):
    bl_idname = "bmp.farm_render"
    bl_label = "Render range on farm"
    bl_description = "Split the scene frame range across every connected LAN worker"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        server = runtime.get("farm_server")
        if server is None:
            self.report({"ERROR"}, "Start the farm host first")
            return {"CANCELLED"}
        scene = context.scene
        if settings.farm_use_scene_range:
            start = int(scene.frame_start)
            end = int(scene.frame_end)
            step = max(1, int(scene.frame_step))
        else:
            start = int(settings.farm_frame_start)
            end = int(settings.farm_frame_end)
            step = max(1, int(settings.farm_frame_step))
        if end < start:
            start, end = end, start
        base = Path(settings.farm_output.strip() or _default_farm_output())
        output_dir = base / f"render-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not create output folder: {exc}")
            return {"CANCELLED"}
        job = server.queue.create_job(
            scene.name,
            start,
            end,
            step,
            str(output_dir),
            width=scene.render.resolution_x,
            height=scene.render.resolution_y,
        )
        settings.farm_progress = server.queue.progress_text()
        _push_message(
            context,
            "system",
            f"Farm job {job.id[:6]}: {len(job.chunks)} frame(s) to {output_dir}",
        )
        self.report({"INFO"}, f"Farm job {job.id[:6]} created")
        return {"FINISHED"}


class BMP_OT_farm_copy(Operator):
    bl_idname = "bmp.farm_copy"
    bl_label = "Copy farm invite"
    bl_description = "Copy the farm URL and token to the clipboard"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if not settings.farm_url or not settings.farm_token:
            self.report({"ERROR"}, "Start or join a farm first")
            return {"CANCELLED"}
        context.window_manager.clipboard = (
            f"{settings.farm_url} {settings.farm_token}"
        )
        self.report({"INFO"}, "Farm URL and token copied")
        return {"FINISHED"}


class BMP_OT_farm_open(Operator):
    bl_idname = "bmp.farm_open"
    bl_label = "Open farm page"
    bl_description = "Open the LAN farm status page in a browser"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if not settings.farm_url:
            self.report({"ERROR"}, "Start a farm first")
            return {"CANCELLED"}
        try:
            import webbrowser

            webbrowser.open(
                f"{settings.farm_url}?token={settings.farm_token}"
            )
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not open a browser: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class BMP_OT_replay(Operator):
    bl_idname = "bmp.replay"
    bl_label = "Replay latest"
    bl_description = "Replay the most recent recording into this scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.window_manager.bmp
        if runtime.get("recording") is not None:
            self.report({"ERROR"}, "Stop the active recording first")
            return {"CANCELLED"}
        if runtime.get("replaying"):
            self.report({"ERROR"}, "A replay is already running")
            return {"CANCELLED"}
        directory = control.data_dir() / "recordings"
        recordings = recording.list_recordings(directory, 1)
        if not recordings:
            self.report({"ERROR"}, "No recording found")
            return {"CANCELLED"}
        path = recordings[0].path
        replay = recording.Replay(str(path))
        runtime["replay_cancel"] = replay
        runtime["replaying"] = True

        def sink(kind, body):
            runtime["inbox"].put((kind, body))

        def worker():
            try:
                replay.play(sink, speed=max(0.1, float(settings.replay_speed)))
            except Exception:  # noqa: BLE001
                pass
            finally:
                runtime["replaying"] = False
                runtime["replay_cancel"] = None

        import threading

        threading.Thread(target=worker, name="bmp-replay", daemon=True).start()
        _push_message(
            context,
            "system",
            f"Replaying {path.name} ({len(replay.events)} events)",
        )
        self.report({"INFO"}, f"Replaying {path.name}")
        return {"FINISHED"}


class BMP_OT_replay_stop(Operator):
    bl_idname = "bmp.replay_stop"
    bl_label = "Stop replay"
    bl_description = "Stop the active replay"
    bl_options = {"REGISTER"}

    def execute(self, context):
        replay = runtime.get("replay_cancel")
        if replay is None:
            self.report({"INFO"}, "No replay active")
            return {"CANCELLED"}
        replay.cancel()
        self.report({"INFO"}, "Stopping replay")
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


class BMP_OT_bookmark_save(Operator):
    bl_idname = "bmp.bookmark_save"
    bl_label = "Save View Bookmark"
    bl_description = "Save the current viewport as a named bookmark"
    bl_options = {"REGISTER"}

    name: StringProperty(name="Name", default="View", maxlen=48)

    def invoke(self, context, event):
        self.name = context.window_manager.bmp.bookmark_name or "View"
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        _ensure_bookmarks(context)
        name = self.name.strip()[:48]
        if not name:
            self.report({"ERROR"}, "Enter a bookmark name")
            return {"CANCELLED"}
        bookmark = _bookmark_from_view(context, name)
        if bookmark is None:
            self.report({"ERROR"}, "Open a 3D viewport first")
            return {"CANCELLED"}
        clean = bookmarks.sanitize(bookmark)
        if clean is None:
            self.report({"ERROR"}, "Could not read the current view")
            return {"CANCELLED"}
        settings = context.window_manager.bmp
        entry = _find_bookmark(context, name)
        if entry is None:
            entry = settings.bookmarks.add()
        entry.name = clean["name"]
        entry.matrix = clean["matrix"]
        entry.aspect = clean["aspect"]
        entry.persp = clean["persp"]
        entry.distance = clean["distance"]
        settings.bookmark_name = clean["name"]
        _save_bookmarks(context)
        self.report({"INFO"}, f"Saved view {clean['name']}")
        return {"FINISHED"}


class BMP_OT_bookmark_jump(Operator):
    bl_idname = "bmp.bookmark_jump"
    bl_label = "Jump to Bookmark"
    bl_description = "Move the viewport to a saved bookmark"

    name: StringProperty()

    def execute(self, context):
        _ensure_bookmarks(context)
        entry = _find_bookmark(context, self.name)
        if entry is None:
            self.report({"WARNING"}, "Bookmark not found")
            return {"CANCELLED"}
        bookmark = {
            "name": entry.name,
            "matrix": list(entry.matrix),
            "aspect": entry.aspect,
            "persp": entry.persp,
            "distance": entry.distance,
        }
        if not _apply_bookmark(context, bookmark):
            self.report({"ERROR"}, "Open a 3D viewport first")
            return {"CANCELLED"}
        return {"FINISHED"}


class BMP_OT_bookmark_delete(Operator):
    bl_idname = "bmp.bookmark_delete"
    bl_label = "Delete Bookmark"
    bl_description = "Delete a saved view bookmark"

    name: StringProperty()

    def execute(self, context):
        _ensure_bookmarks(context)
        settings = context.window_manager.bmp
        wanted = self.name.strip().lower()
        for index, item in enumerate(settings.bookmarks):
            if item.name.strip().lower() == wanted:
                settings.bookmarks.remove(index)
                _save_bookmarks(context)
                return {"FINISHED"}
        return {"CANCELLED"}


class BMP_OT_bookmark_push(Operator):
    bl_idname = "bmp.bookmark_push"
    bl_label = "Push View Bookmark"
    bl_description = "Move everyone in the room to this view (admin only)"

    name: StringProperty()

    def execute(self, context):
        _ensure_bookmarks(context)
        settings = context.window_manager.bmp
        session: SessionService | None = runtime.get("session")
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        if not settings.is_host and settings.role not in {"admin", "owner"}:
            self.report({"ERROR"}, "Only a host or admin can move everyone's view")
            return {"CANCELLED"}
        entry = _find_bookmark(context, self.name)
        if entry is None:
            self.report({"WARNING"}, "Bookmark not found")
            return {"CANCELLED"}
        bookmark = {
            "name": entry.name,
            "matrix": list(entry.matrix),
            "aspect": entry.aspect,
            "persp": entry.persp,
            "distance": entry.distance,
        }
        if not session.send_view_bookmark(bookmark):
            self.report({"ERROR"}, "Could not push the view")
            return {"CANCELLED"}
        _apply_bookmark(context, bookmark)
        _push_message(context, "system", f"Moved everyone to {entry.name}")
        return {"FINISHED"}


class BMP_OT_bundle_send(Operator):
    bl_idname = "bmp.bundle_send"
    bl_label = "Send Selection"
    bl_description = "Send the selected objects, materials and images to a teammate"

    target: StringProperty()

    def execute(self, context):
        settings = context.window_manager.bmp
        session: SessionService | None = runtime.get("session")
        target = self.target.strip()[:48]
        if session is None:
            self.report({"ERROR"}, "Not connected")
            return {"CANCELLED"}
        if not target or target == settings.name:
            self.report({"ERROR"}, "Choose another person in the room")
            return {"CANCELLED"}
        objects = _bundle_objects(context)
        if not objects:
            self.report({"ERROR"}, "Select one or more supported objects")
            return {"CANCELLED"}
        try:
            manifest = _bundle_manifest(context, objects)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, f"Could not package the selection: {exc}")
            return {"CANCELLED"}
        summary = asset_bundle.summary(manifest)
        bundle_id = summary.get("id", "")
        key = f"{bundle_id}:{target}"
        now = time.monotonic()
        if now - runtime["bundle_sent"].get(key, 0.0) < 10.0:
            self.report({"INFO"}, "That selection was just sent to this person")
            return {"CANCELLED"}
        try:
            wire = asset_bundle.pack(manifest)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, f"Bundle failed: {exc}")
            return {"CANCELLED"}
        if len(wire) > MAX_MESH:
            self.report({"ERROR"}, f"Bundle is too large ({len(wire) // 1048576} MB)")
            return {"CANCELLED"}
        if not session.send_asset_bundle(target, manifest):
            self.report({"ERROR"}, "The bundle is too large or could not be sent")
            return {"CANCELLED"}
        runtime["bundle_sent"][key] = now
        if len(runtime["bundle_sent"]) > 64:
            runtime["bundle_sent"] = dict(list(runtime["bundle_sent"].items())[-32:])
        eventlog.log(
            "info",
            "ASSET_BUNDLE_SEND",
            "asset bundle sent",
            target=target,
            objects=summary.get("objects", 0),
            images=summary.get("images", 0),
        )
        self.report({"INFO"}, f"Sent {summary.get('objects', 0)} object(s) to {target}")
        return {"FINISHED"}


class BMP_OT_bundle_accept(Operator):
    bl_idname = "bmp.bundle_accept"
    bl_label = "Accept Bundle"
    bl_description = "Import the offered objects into this scene"

    bundle: StringProperty()

    def execute(self, context):
        offers = runtime.get("bundle_offers", {})
        entry = offers.get(self.bundle)
        if not isinstance(entry, dict):
            self.report({"WARNING"}, "That bundle is no longer available")
            return {"CANCELLED"}
        count = _import_bundle(context, entry.get("manifest") or {})
        offers.pop(self.bundle, None)
        _refresh_bundle_ui(context.window_manager.bmp)
        if count == 0:
            self.report({"WARNING"}, "Nothing was imported")
            return {"CANCELLED"}
        _push_message(context, "system", f"Imported {count} object(s) from {entry.get('sender', 'a teammate')}")
        self.report({"INFO"}, f"Imported {count} object(s)")
        return {"FINISHED"}


class BMP_OT_bundle_dismiss(Operator):
    bl_idname = "bmp.bundle_dismiss"
    bl_label = "Dismiss Bundle"
    bl_description = "Discard this incoming bundle"

    bundle: StringProperty()

    def execute(self, context):
        runtime.get("bundle_offers", {}).pop(self.bundle, None)
        _refresh_bundle_ui(context.window_manager.bmp)
        return {"FINISHED"}


class BMP_OT_copy_invite(Operator):
    bl_idname = "bmp.copy_invite"
    bl_label = "Copy Invite Link"
    bl_description = "Copy a blender:// invite link for this room"

    def execute(self, context):
        code = context.window_manager.bmp.code
        if not code:
            self.report({"ERROR"}, "Start or join a room first")
            return {"CANCELLED"}
        link = f"blender://join/{code.replace('-', '')}"
        context.window_manager.clipboard = link
        self.report({"INFO"}, "Invite link copied")
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
        "protocol": 3,
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
    BMPBookmark,
    BMPState,
    BMP_OT_host,
    BMP_OT_join,
    BMP_OT_leave,
    BMP_OT_resync_scene,
    BMP_OT_release_locks,
    BMP_OT_copy_code,
    BMP_OT_copy_direct_code,
    BMP_OT_send_chat,
    BMP_OT_track_mesh,
    BMP_OT_untrack_mesh,
    BMP_OT_ping,
    BMP_OT_laser,
    BMP_OT_hover,
    BMP_OT_trust_host,
    BMP_OT_ai_ask,
    BMP_OT_ai_test,
    BMP_OT_ai_apply_all,
    BMP_OT_ai_clear,
    BMP_OT_record_start,
    BMP_OT_record_stop,
    BMP_OT_replay,
    BMP_OT_replay_stop,
    BMP_OT_broadcast_start,
    BMP_OT_broadcast_stop,
    BMP_OT_broadcast_open,
    BMP_OT_bridge_start,
    BMP_OT_bridge_stop,
    BMP_OT_farm_start,
    BMP_OT_farm_join,
    BMP_OT_farm_stop,
    BMP_OT_farm_render,
    BMP_OT_farm_copy,
    BMP_OT_farm_open,
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
    BMP_OT_bookmark_save,
    BMP_OT_bookmark_jump,
    BMP_OT_bookmark_delete,
    BMP_OT_bookmark_push,
    BMP_OT_bundle_send,
    BMP_OT_bundle_accept,
    BMP_OT_bundle_dismiss,
    BMP_OT_copy_invite,
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
            item = keymap.keymap_items.new("bmp.laser", "L", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((keymap, item))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        entries = heartbeat.unclean_exits()
        # A marker from an older build is not a crash of this build. Ignoring it
        # also prevents an installer/update shutdown from showing a scary notice
        # after the new version starts.
        runtime["crash_entries"] = [
            entry
            for entry in entries
            if str(entry.get("addon", "")) == ADDON_VERSION
        ]
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
        _stop_broadcast(settings)
        _stop_bridge(settings)
        recorder = runtime.get("recording")
        if recorder is not None:
            recorder.stop()
            runtime["recording"] = None
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
