"""Public server directory for Blendermp.

Hosts announce a public server (name, room code, player count, password flag)
and everyone else lists or searches it. The directory is intentionally dumb:
it never sees scene data, passwords or private keys. The room code is only an
endpoint locator and is already visible to anyone allowed to join.

Run it on the same small VPS as the relay::

    python -m server.directory --host 0.0.0.0 --port 7101

The desktop app talks to this over HTTP JSON. Put it behind a TLS reverse proxy
if the directory is on the public internet.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("blendermp.directory")

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 15
MAX_BODY = 8 * 1024
MAX_SERVERS = 500
MAX_TEXT = 48
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
CODE_RE = re.compile(r"^[" + CROCKFORD + "]{" + str(CODE_LEN) + r"}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class DirectoryError(ValueError):
    pass


def clean_text(value: Any, limit: int = MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        if required:
            raise DirectoryError("expected text")
        return ""
    text = value.strip()
    if required and not text:
        raise DirectoryError("text is empty")
    if len(text) > limit or CONTROL_RE.search(text):
        raise DirectoryError("text is invalid")
    return text


def clean_code(value: Any) -> str:
    if not isinstance(value, str):
        raise DirectoryError("room code is required")
    code = "".join(ch for ch in value.upper() if ch.isalnum())
    if not CODE_RE.match(code):
        raise DirectoryError("room code is invalid")
    return code


def clean_int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectoryError("expected an integer")
    if not low <= value <= high:
        raise DirectoryError("number out of range")
    return value


class Directory:
    def __init__(self, ttl: float = 60.0, max_servers: int = MAX_SERVERS):
        self.ttl = ttl
        self.max_servers = max_servers
        self._servers: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expiry = now - self.ttl
        for key, entry in list(self._servers.items()):
            if entry["seen"] < expiry:
                del self._servers[key]

    def announce(self, body: Any, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if not isinstance(body, dict):
            raise DirectoryError("expected a JSON object")
        server_id = body.get("id")
        if not isinstance(server_id, str) or not ID_RE.match(server_id):
            raise DirectoryError("invalid server id")
        name = clean_text(body.get("name"))
        host = clean_text(body.get("host", ""), required=False)
        code = clean_code(body.get("code"))
        players = clean_int(body.get("players", 1), 1, 64)
        maximum = clean_int(body.get("max", 16), 2, 64)
        if players > maximum:
            raise DirectoryError("player count is above the maximum")
        password = bool(body.get("password", False))
        relay = bool(body.get("relay", False))
        version = clean_text(body.get("version", ""), 16, required=False)
        protocol = clean_int(body.get("protocol", 0), 0, 999)
        caps = clean_int(body.get("caps", 0), 0, 2**31 - 1)
        min_app = clean_text(body.get("min_app", ""), 16, required=False)
        direct_code_text = clean_text(body.get("direct_code", ""), 16, required=False)
        direct_code = clean_code(direct_code_text) if direct_code_text else ""

        with self._lock:
            self._prune(now)
            if server_id not in self._servers and len(self._servers) >= self.max_servers:
                raise DirectoryError("directory is full")
            entry = {
                "id": server_id,
                "name": name,
                "host": host,
                "code": code,
                "players": players,
                "max": maximum,
                "password": password,
                "relay": relay,
                "version": version,
                "protocol": protocol,
                "caps": caps,
                "min_app": min_app,
                "direct_code": direct_code,
                "seen": now,
            }
            self._servers[server_id] = entry
        return {"ok": True, "ttl": int(self.ttl)}

    def remove(self, body: Any) -> bool:
        if not isinstance(body, dict):
            raise DirectoryError("expected a JSON object")
        server_id = body.get("id")
        if not isinstance(server_id, str) or not ID_RE.match(server_id):
            raise DirectoryError("invalid server id")
        with self._lock:
            return self._servers.pop(server_id, None) is not None

    def servers(self, query: str = "", limit: int = 100, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        needle = query.strip().lower()[:MAX_TEXT]
        limit = max(1, min(200, int(limit)))
        with self._lock:
            self._prune(now)
            entries = [
                {
                    "name": entry["name"],
                    "host": entry["host"],
                    "code": entry["code"],
                    "players": entry["players"],
                    "max": entry["max"],
                    "password": entry["password"],
                    "relay": entry["relay"],
                    "version": entry["version"],
                    "protocol": entry["protocol"],
                    "caps": entry["caps"],
                    "min_app": entry["min_app"],
                    "direct_code": entry["direct_code"],
                    "updated": int(entry["seen"]),
                }
                for entry in self._servers.values()
                if not needle or needle in entry["name"].lower() or needle in entry["host"].lower()
            ]
        entries.sort(key=lambda item: (-item["players"], item["name"].lower()))
        return entries[:limit]

    def count(self) -> int:
        with self._lock:
            self._prune(time.time())
            return len(self._servers)


class DirectoryHandler(BaseHTTPRequestHandler):
    server_version = "BlendermpDirectory/1"
    directory: Directory

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if status != 204 and self.command != "HEAD":
            self.wfile.write(payload)

    def _body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            raise DirectoryError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DirectoryError("invalid JSON") from exc

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._send(200, {"ok": True, "servers": self.directory.count()})
            return
        if parts.path == "/servers":
            params = parse_qs(parts.query)
            query = (params.get("q") or [""])[0]
            try:
                limit = int((params.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            self._send(200, {"servers": self.directory.servers(query, limit)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        try:
            body = self._body()
            if parts.path == "/announce":
                self._send(200, self.directory.announce(body))
                return
            if parts.path == "/remove":
                removed = self.directory.remove(body)
                self._send(200, {"ok": removed})
                return
        except DirectoryError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})


def create_server(host: str, port: int, directory: Directory | None = None) -> ThreadingHTTPServer:
    directory = directory or Directory()
    handler = type("BoundDirectoryHandler", (DirectoryHandler,), {"directory": directory})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, ttl: float) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = Directory(ttl=ttl)
    server = create_server(host, port, directory)
    log.info("directory listening on %s:%s", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blendermp public server directory")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7101)
    parser.add_argument("--ttl", type=float, default=60.0)
    args = parser.parse_args()
    serve(args.host, args.port, args.ttl)
