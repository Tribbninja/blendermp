"""Local control-plane files shared with the desktop app.

The app writes ``session.json`` (host/join/leave), the addon writes
``status.json`` (state, users, latency). Both live in the per-user application
data directory.

This is a local trust boundary, so treat the file as untrusted input: strict
schema, action whitelist, size cap, and no paths or arbitrary arguments are
accepted. The app never sends executable content.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .core.roomcode import RoomCodeError, decode_room, normalize
except ImportError:  # pragma: no cover - allows importing the module standalone
    from core.roomcode import RoomCodeError, decode_room, normalize

APP_DIR = "Blendermp"
MAX_CONTROL_BYTES = 4096
MAX_STATUS_BYTES = 64 * 1024
ALLOWED_ACTIONS = {"host", "join", "leave", "chat", "kick", "role", "render", "render_stop", "render_retry", "render_reply"}
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,48}$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def certs_dir() -> Path:
    path = data_dir() / "certs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def checkpoints_dir() -> Path:
    path = data_dir() / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_path() -> Path:
    return data_dir() / "session.json"


def status_path() -> Path:
    return data_dir() / "status.json"


def ack_path() -> Path:
    return data_dir() / "ack.json"


def read_ack() -> str:
    """The last control nonce this user's Blender already executed."""
    body = _read_json(ack_path(), 512)
    nonce = body.get("nonce") if body else None
    return nonce if isinstance(nonce, str) and 8 <= len(nonce) <= 64 else ""


def write_ack(nonce: str) -> None:
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
        return
    _atomic_write(ack_path(), json.dumps({"nonce": nonce}, separators=(",", ":")), ".ack-")


def _atomic_write(path: Path, payload: str, prefix: str) -> None:
    descriptor, temp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        # The desktop app polls this file. A short-lived reader handle must not
        # make the atomic replace fail on Windows.
        last_error: OSError | None = None
        for _ in range(10):
            try:
                os.replace(temp, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.03)
        if last_error is not None:
            raise last_error
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


def _read_json(path: Path, limit: int) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > limit:
            return None
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def read_session() -> dict[str, Any] | None:
    """Read and sanitise a control request. Returns ``None`` if invalid."""
    body = _read_json(session_path(), MAX_CONTROL_BYTES)
    if body is None:
        return None

    action = body.get("action")
    if action not in ALLOWED_ACTIONS:
        return None

    nonce = body.get("nonce")
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
        return None

    result: dict[str, Any] = {"action": action, "nonce": nonce}

    pin = body.get("pin")
    if pin is not None:
        if not isinstance(pin, str) or len(pin) > 64 or any(ord(ch) < 32 for ch in pin):
            return None
        result["pin"] = pin

    name = body.get("name")
    if name is not None:
        if not isinstance(name, str) or not NAME_RE.match(name):
            return None
        result["name"] = name

    port = body.get("port")
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            return None
        result["port"] = port

    fingerprint = body.get("fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or not HEX64_RE.match(fingerprint):
            return None
        result["fingerprint"] = fingerprint.lower()

    if action == "join":
        code = body.get("code")
        if not isinstance(code, str):
            return None
        normalized = normalize(code)
        try:
            decode_room(normalized)
        except RoomCodeError:
            return None
        result["code"] = normalized

    relay = body.get("relay")
    if relay is not None:
        if not isinstance(relay, str):
            return None
        try:
            host, port_text = relay.rsplit(":", 1)
            ipaddress.IPv4Address(host)
            port = int(port_text)
            if not 1024 <= port <= 65535:
                raise ValueError
        except (ValueError, ipaddress.AddressValueError):
            return None
        result["relay"] = f"{host}:{port}"

    use_relay = body.get("use_relay")
    if isinstance(use_relay, bool):
        result["use_relay"] = use_relay

    relay_url = body.get("relay_url")
    if relay_url is not None:
        if (
            not isinstance(relay_url, str)
            or len(relay_url) > 256
            or not relay_url.startswith(("ws://", "wss://"))
            or any(ord(ch) < 32 for ch in relay_url)
        ):
            return None
        result["relay_url"] = relay_url

    prefer_direct = body.get("prefer_direct")
    if isinstance(prefer_direct, bool):
        result["prefer_direct"] = prefer_direct

    direct_code = body.get("direct_code")
    if direct_code is not None:
        if not isinstance(direct_code, str):
            return None
        try:
            decode_room(normalize(direct_code))
        except Exception:  # noqa: BLE001
            return None
        result["direct_code"] = direct_code.strip().upper()[:32]

    direct_codes = body.get("direct_codes")
    if isinstance(direct_codes, list):
        cleaned: list[str] = []
        for item in direct_codes[:4]:
            if not isinstance(item, str):
                continue
            try:
                decode_room(normalize(item))
            except Exception:  # noqa: BLE001
                continue
            cleaned.append(item.strip().upper()[:32])
        if cleaned:
            result["direct_codes"] = cleaned

    relay_token = body.get("relay_token")
    if relay_token is not None:
        if (
            not isinstance(relay_token, str)
            or not 32 <= len(relay_token) <= 128
            or re.fullmatch(r"[A-Za-z0-9_-]+", relay_token) is None
        ):
            return None
        result["relay_token"] = relay_token

    if action == "chat":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return None
        result["text"] = text.strip()

    if action == "kick":
        target = body.get("target")
        if not isinstance(target, str) or not NAME_RE.match(target):
            return None
        result["target"] = target

    if action == "role":
        target = body.get("target")
        if not isinstance(target, str) or not NAME_RE.match(target):
            return None
        role = body.get("role")
        if role not in {"viewer", "commenter", "editor", "admin"}:
            return None
        result["target"] = target
        result["role"] = role

    if action == "host" and body.get("code") is not None:
        code = body.get("code")
        if not isinstance(code, str):
            return None
        try:
            decode_room(normalize(code))
        except RoomCodeError:
            return None
        result["code"] = normalize(code)

    if action == "render":
        for key, low, high in (("start", 0, 1_000_000), ("end", 0, 1_000_000), ("step", 1, 1000)):
            value = body.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                return None
            result[key] = value
        if result["end"] < result["start"]:
            return None
        for key in ("width", "height"):
            value = body.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 16384:
                return None
            result[key] = value
        fmt = body.get("format")
        if fmt not in {"MP4", "WEBM", "PNG"}:
            return None
        result["format"] = fmt
        engine = body.get("engine")
        if engine is not None:
            if engine not in {"CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"}:
                return None
            result["engine"] = engine
        for key in ("tiles_x", "tiles_y"):
            value = body.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16:
                return None
            result[key] = value
        output = body.get("output")
        if output is not None:
            if not isinstance(output, str) or len(output) > 512 or any(ord(ch) < 32 for ch in output):
                return None
            result["output"] = output

    if action == "render_reply":
        job = body.get("job")
        if not isinstance(job, str) or not job or len(job) > 32 or not job.isalnum():
            return None
        accept = body.get("accept")
        if not isinstance(accept, bool):
            return None
        result["job"] = job
        result["accept"] = accept

    return result


def _safe_int(value, low: int, high: int, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def write_status(data: dict[str, Any]) -> None:
    """Write a bounded, sanitised status document atomically."""
    safe: dict[str, Any] = {}
    for key in ("state", "code", "name", "addon", "blender", "fingerprint", "verify_code", "last_error", "error_code", "role", "overlay_info", "direct_code", "identity_public", "identity_signature", "identity_descriptor", "broadcast_url", "bridge_url", "recording", "farm_url", "farm_progress", "scene_hash", "scene_mismatch"):
        value = data.get(key)
        if isinstance(value, str):
            safe[key] = value[:512]
    codes = data.get("direct_codes")
    if isinstance(codes, list):
        safe["direct_codes"] = [
            str(code)[:32] for code in codes[:4] if isinstance(code, str)
        ]

    scene = data.get("scene")
    if isinstance(scene, dict):        safe["scene"] = {
            key: max(0, min(int(scene.get(key, 0)), 1 << 40))
            for key in ("objects", "meshes", "vertices", "materials", "images", "collections")
            if isinstance(scene.get(key, 0), int) and not isinstance(scene.get(key, 0), bool)
        }
    mesh = data.get("mesh")
    if isinstance(mesh, dict):
        safe["mesh"] = {
            "active": bool(mesh.get("active")),
            "peers": [
                str(peer)[:64]
                for peer in (mesh.get("peers") or [])[:64]
                if isinstance(peer, str)
            ],
            "public_address": str(mesh.get("public_address", ""))[:128],
            "error": str(mesh.get("error", ""))[:256],
            "sent": max(0, min(int(mesh.get("sent", 0) or 0), 1 << 62)),
            "received": max(0, min(int(mesh.get("received", 0) or 0), 1 << 62)),
        }
    render = data.get("render")
    if isinstance(render, dict):
        clean = {
            "active": bool(render.get("active")),
            "role": str(render.get("role", ""))[:16],
            "id": str(render.get("id", ""))[:32],
            "name": str(render.get("name", ""))[:80],
            "start": max(0, min(int(render.get("start", 0) or 0), 1 << 31)),
            "end": max(0, min(int(render.get("end", 0) or 0), 1 << 31)),
            "step": max(1, min(int(render.get("step", 1) or 1), 1000)),
            "engine": str(render.get("engine", ""))[:32],
            "format": str(render.get("format", ""))[:8],
            "fps": max(1, min(int(render.get("fps", 24) or 24), 240)),
            "tiles_x": max(1, min(int(render.get("tiles_x", 1) or 1), 16)),
            "tiles_y": max(1, min(int(render.get("tiles_y", 1) or 1), 16)),
            "frames": max(0, min(int(render.get("frames", 0) or 0), 1 << 31)),
            "frames_done": max(0, min(int(render.get("frames_done", 0) or 0), 1 << 31)),
            "total": max(0, min(int(render.get("total", 0) or 0), 1 << 31)),
            "done": max(0, min(int(render.get("done", 0) or 0), 1 << 31)),
            "failed": max(0, min(int(render.get("failed", 0) or 0), 1 << 31)),
            "claimed": max(0, min(int(render.get("claimed", 0) or 0), 1 << 31)),
            "pending": max(0, min(int(render.get("pending", 0) or 0), 1 << 31)),
            "percent": round(max(0.0, min(100.0, float(render.get("percent", 0) or 0))), 1),
            "elapsed": max(0.0, min(float(render.get("elapsed", 0) or 0), 1 << 31)),
            "eta_seconds": max(0.0, min(float(render.get("eta_seconds", 0) or 0), 1 << 31)),
            "workers": [
                str(worker)[:48]
                for worker in (render.get("workers") or [])[:64]
                if isinstance(worker, str)
            ],
            "worker_detail": {
                str(name)[:48]: {
                    "done": max(0, min(int(entry.get("done", 0) or 0), 1 << 31)),
                    "failed": max(0, min(int(entry.get("failed", 0) or 0), 1 << 31)),
                    "claimed": max(0, min(int(entry.get("claimed", 0) or 0), 1 << 31)),
                }
                for name, entry in list((render.get("worker_detail") or {}).items())[:64]
                if isinstance(entry, dict)
            },
            "started": bool(render.get("started")),
            "finished": bool(render.get("finished")),
            "stopped": bool(render.get("stopped")),
            "error": str(render.get("error", ""))[:200],
            "output_dir": str(render.get("output_dir", ""))[:512],
        }
        request = render.get("request")
        if isinstance(request, dict):
            clean["request"] = {
                "job": str(request.get("job", ""))[:32],
                "name": str(request.get("name", ""))[:80],
                "start": max(0, min(int(request.get("start", 0) or 0), 1 << 31)),
                "end": max(0, min(int(request.get("end", 0) or 0), 1 << 31)),
                "step": max(1, min(int(request.get("step", 1) or 1), 1000)),
                "width": max(0, min(int(request.get("width", 0) or 0), 1 << 16)),
                "height": max(0, min(int(request.get("height", 0) or 0), 1 << 16)),
                "format": str(request.get("format", "PNG"))[:8],
                "engine": str(request.get("engine", ""))[:32],
                "tiles_x": max(1, min(int(request.get("tiles_x", 1) or 1), 16)),
                "tiles_y": max(1, min(int(request.get("tiles_y", 1) or 1), 16)),
            }
        safe["render"] = clean
    for key in ("is_host", "relay", "fast_mode", "farm_active", "internet_mode", "lan_fast"):
        value = data.get(key)
        if isinstance(value, bool):
            safe[key] = value
    rtt = data.get("rtt_ms")
    if isinstance(rtt, (int, float)) and not isinstance(rtt, bool):
        safe["rtt_ms"] = round(max(0.0, min(5000.0, float(rtt))), 1)
    for key in ("bytes_in", "bytes_out", "pid", "uptime", "total_bytes", "bridge_clients", "farm_workers"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            safe[key] = max(-1, min(value, 1 << 62))

    users = data.get("users")
    if isinstance(users, list):
        safe["users"] = [
            {
                "name": str(item.get("name", ""))[:48],
                "color": [float(c) for c in item.get("color", [])][:4],
                "ping_ms": int(item.get("ping_ms", 0)),
                "host": bool(item.get("host", False)),
                "role": str(item.get("role", "editor"))[:16],
            }
            for item in users[:64]
            if isinstance(item, dict)
        ]

    messages = data.get("messages")
    if isinstance(messages, list):
        safe["messages"] = [
            {
                "author": str(item.get("author", ""))[:48],
                "text": str(item.get("text", ""))[:2000],
                "stamp": str(item.get("stamp", ""))[:16],
            }
            for item in messages[-20:]
            if isinstance(item, dict)
        ]

    leases = data.get("leases")
    if isinstance(leases, list):
        safe["leases"] = [
            {
                "object": str(item.get("object", ""))[:64],
                "owner": str(item.get("owner", ""))[:48],
                "requests": [str(name)[:48] for name in (item.get("requests") or [])[:4]],
            }
            for item in leases[:64]
            if isinstance(item, dict)
        ]

    sync = data.get("sync")
    if isinstance(sync, list):
        safe["sync"] = [
            {
                "name": str(item.get("name", ""))[:48],
                "queue": _safe_int(item.get("queue"), 0, 1 << 20),
                "pending": _safe_int(item.get("pending"), 0, 1 << 20),
                "quality": _safe_int(item.get("quality"), 0, 4),
                "rtt_ms": round(max(0.0, min(5000.0, float(item.get("rtt_ms", 0.0) or 0.0))), 1),
            }
            for item in sync[:64]
            if isinstance(item, dict)
        ]

    perf = data.get("perf")
    if isinstance(perf, dict):
        budgets = perf.get("budgets")
        if isinstance(budgets, dict):
            safe["perf"] = {
                "violations": [
                    str(name)[:48]
                    for name in (perf.get("violations") or [])[:16]
                    if isinstance(name, str)
                ],
                "budgets": {
                    str(name)[:48]: {
                        "max_ms": round(float(item.get("max_ms", 0.0) or 0.0), 3),
                        "p95_ms": round(float(item.get("p95_ms", 0.0) or 0.0), 3),
                        "ok": bool(item.get("ok", True)),
                    }
                    for name, item in list(budgets.items())[:16]
                    if isinstance(item, dict)
                },
            }

    payload = json.dumps(safe, separators=(",", ":"))[:MAX_STATUS_BYTES]
    _atomic_write(status_path(), payload, ".status-")
