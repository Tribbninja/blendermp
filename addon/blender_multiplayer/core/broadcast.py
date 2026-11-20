"""Token-protected live broadcast of a Blendermp session.

This is the "watch a teammate work" / "see what happened" transport. It is a
tiny HTTP server on localhost that streams session events to a browser page
using Server-Sent Events (no websocket dependency, no external JS).

Why HTTP+SSE instead of a second desktop window: the Tauri/WebView2 second
window is unreliable on some Windows machines, and a normal browser tab can be
moved to another monitor or shared on a call.

Security
--------
* Binds to ``127.0.0.1`` by default.
* Requires a random token in the URL (``?token=...``).
* The page is static, self-contained HTML; event text is inserted with
  ``textContent`` so chat cannot inject markup.
* Event bodies are bounded and JSON-sanitised before they leave the process.

The module imports only the standard library, so it is testable without bpy.
"""

from __future__ import annotations

import base64
import hmac
import json
import math
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_EVENTS = 400
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT = 2000


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Convert a session event body into bounded, JSON-safe data."""
    if depth > 6:
        return "..."
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) > 4096:
            return {"bytes": len(raw)}
        return {"__bytes__": base64.b64encode(raw).decode("ascii")}
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return 0.0
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:128]]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _json_safe(item, depth + 1)
            for key, item in list(value.items())[:64]
        }
    return repr(value)[:200]


class BroadcastServer:
    """A localhost SSE server that fans session events out to browsers."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7102,
        token: str | None = None,
        title: str = "Blendermp session",
    ):
        self.host = host
        self.port = int(port)
        self.token = token or secrets.token_urlsafe(18)
        self.title = title or "Blendermp session"
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = 0.0
        self._clients = 0
        self.last_error = ""

    # -- lifecycle -----------------------------------------------------
    def start(self) -> str:
        if self._server is not None:
            return self.url
        self._stop.clear()
        server = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        server.daemon_threads = True
        server.allow_reuse_address = True
        self._server = server
        self.port = int(server.server_address[1])
        self._started = time.time()
        self._thread = threading.Thread(
            target=self._serve, name="bmp-broadcast", daemon=True
        )
        self._thread.start()
        return self.url

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.25)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:200]

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._thread = None

    @property
    def is_active(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    @property
    def client_count(self) -> int:
        with self._lock:
            return self._clients

    # -- publishing ----------------------------------------------------
    def publish(self, kind: str, body: Any) -> None:
        if not isinstance(kind, str) or not kind:
            return
        if not isinstance(body, dict):
            body = {"value": body}
        safe = _json_safe(body)
        try:
            encoded = json.dumps(safe, separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = json.dumps({"value": repr(body)[:MAX_TEXT]})
        if len(encoded) > MAX_BODY_BYTES:
            encoded = json.dumps({"truncated": True, "size": len(encoded)})
        with self._condition:
            self._seq += 1
            event = {
                "seq": self._seq,
                "t": round(time.time(), 3),
                "kind": kind[:48],
                "body": json.loads(encoded),
            }
            self._events.append(event)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "title": self.title,
                "started": self._started,
                "clients": self._clients,
                "seq": self._seq,
                "events": list(self._events),
            }

    # -- helpers used by the request handler ---------------------------
    def _authorised(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(str(token), str(self.token))

    def _client_joined(self) -> None:
        with self._lock:
            self._clients += 1

    def _client_left(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)


def _make_handler(app: BroadcastServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):  # noqa: D401 - silence stderr
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            token = (query.get("token") or [""])[0]
            if not app._authorised(token):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if parsed.path in ("/", "/index.html"):
                html = _PAGE.replace("__TITLE__", _html_escape(app.title)).replace(
                    "__TOKEN__", _js_escape(app.token)
                )
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/state":
                body = json.dumps(app.snapshot(), separators=(",", ":")).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if parsed.path == "/health":
                body = json.dumps(
                    {"ok": True, "clients": app.client_count}, separators=(",", ":")
                ).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if parsed.path == "/events":
                self._stream()
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            app._client_joined()
            last_seq = 0
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while not app._stop.is_set():
                    with app._condition:
                        pending = [e for e in app._events if e["seq"] > last_seq]
                        if not pending:
                            app._condition.wait(timeout=15.0)
                            pending = [e for e in app._events if e["seq"] > last_seq]
                    if pending:
                        chunks = []
                        for event in pending:
                            last_seq = event["seq"]
                            chunks.append(
                                "id: %d\ndata: %s\n\n"
                                % (event["seq"], json.dumps(event, separators=(",", ":")))
                            )
                        self.wfile.write("".join(chunks).encode("utf-8"))
                        self.wfile.flush()
                    elif not app._stop.is_set():
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app._client_left()

    return Handler


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _js_escape(text: str) -> str:
    return json.dumps(str(text))[1:-1]


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d;
          --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  header { position:sticky; top:0; z-index:2; background:rgba(13,17,23,.92);
           backdrop-filter:blur(6px); border-bottom:1px solid var(--line);
           padding:14px 18px; display:flex; gap:14px; align-items:center; }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.2px; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted); }
  .dot.live { background:var(--ok); box-shadow:0 0 10px var(--ok); }
  .stats { margin-left:auto; display:flex; gap:16px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .stats b { color:var(--text); font-weight:600; }
  main { max-width:1100px; margin:0 auto; padding:18px; }
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  .toolbar button { background:var(--panel); color:var(--text); border:1px solid var(--line);
                    border-radius:6px; padding:6px 10px; cursor:pointer; }
  .toolbar button.active { border-color:var(--accent); color:var(--accent); }
  #feed { display:flex; flex-direction:column; gap:6px; }
  .row { background:var(--panel); border:1px solid var(--line); border-radius:7px;
         padding:8px 11px; display:grid; grid-template-columns:86px 1fr; gap:10px; }
  .kind { color:var(--accent); font-family:ui-monospace, Consolas, monospace; font-size:12px; }
  .body { color:var(--text); word-break:break-word; }
  .body .muted { color:var(--muted); }
  .row.chat { border-color:#1f6feb55; }
  .row.chat .kind { color:#d2a8ff; }
  .row.error { border-color:#f8514955; }
  .row.error .kind { color:#ff7b72; }
  .empty { color:var(--muted); padding:28px 0; text-align:center; }
  footer { color:var(--muted); text-align:center; padding:20px; font-size:12px; }
</style>
</head>
<body>
<header>
  <span id="dot" class="dot"></span>
  <h1>__TITLE__</h1>
  <div class="stats">
    <span>events <b id="count">0</b></span>
    <span>clients <b id="clients">1</b></span>
    <span id="clock">--:--:--</span>
  </div>
</header>
<main>
  <div class="toolbar" id="filters">
    <button data-kind="all" class="active">All</button>
    <button data-kind="transform">Transforms</button>
    <button data-kind="chat">Chat</button>
    <button data-kind="object_create">Objects</button>
    <button data-kind="lease">Locks</button>
    <button data-kind="error">Errors</button>
  </div>
  <div id="feed"><div class="empty" id="empty">Waiting for events…</div></div>
</main>
<footer>Read-only live view. Close this tab to stop watching.</footer>
<script>
const TOKEN = "__TOKEN__";
const feed = document.getElementById('feed');
const countEl = document.getElementById('count');
const clientsEl = document.getElementById('clients');
const dot = document.getElementById('dot');
const empty = document.getElementById('empty');
let filter = 'all';
let total = 0;
let events = [];

function describe(kind, body) {
  if (!body || typeof body !== 'object') return String(body);
  if (kind === 'chat') return (body.name || '?') + ': ' + (body.text || '');
  if (kind === 'system') return body.text || '';
  if (kind === 'transform') {
    const m = body.matrix || [];
    const p = m.length >= 15 ? [m[12], m[13], m[14]].map(v => Number(v).toFixed(2)).join(', ') : '';
    return (body.uuid ? String(body.uuid).slice(0, 8) : 'object') + ' → ' + p;
  }
  if (kind === 'object_create') return 'created ' + ((body.meta && body.meta.name) || body.uuid || 'object');
  if (kind === 'object_delete') return 'deleted ' + String(body.uuid || '').slice(0, 8);
  if (kind === 'lease') return [body.action, body.owner, body.key && String(body.key).slice(0, 8)].filter(Boolean).join(' ');
  if (kind === 'presence') return (body.name || '?') + (body.active ? ' editing ' + body.active : '');
  if (kind === 'error') return body.message || body.code || 'error';
  const keys = Object.keys(body).slice(0, 5);
  return keys.map(k => k + '=' + JSON.stringify(body[k])).join('  ');
}

function render() {
  const rows = events.filter(e => filter === 'all' || e.kind === filter);
  feed.replaceChildren();
  if (!rows.length) {
    const d = document.createElement('div');
    d.className = 'empty';
    d.textContent = 'Waiting for events…';
    feed.appendChild(d);
    return;
  }
  for (const e of rows.slice(-300)) {
    const row = document.createElement('div');
    row.className = 'row' + (e.kind === 'chat' ? ' chat' : '') + (e.kind === 'error' ? ' error' : '');
    const kind = document.createElement('div');
    kind.className = 'kind';
    kind.textContent = e.kind;
    const body = document.createElement('div');
    body.className = 'body';
    const time = document.createElement('span');
    time.className = 'muted';
    time.textContent = new Date(e.t * 1000).toLocaleTimeString() + '  ';
    body.appendChild(time);
    body.appendChild(document.createTextNode(describe(e.kind, e.body)));
    row.appendChild(kind);
    row.appendChild(body);
    feed.appendChild(row);
  }
}

function add(event) {
  total += 1;
  countEl.textContent = String(total);
  events.push(event);
  if (events.length > 400) events = events.slice(-400);
  render();
}

document.querySelectorAll('#filters button').forEach(btn => {
  btn.addEventListener('click', () => {
    filter = btn.dataset.kind;
    document.querySelectorAll('#filters button').forEach(b => b.classList.toggle('active', b === btn));
    render();
  });
});

const source = new EventSource('/events?token=' + encodeURIComponent(TOKEN));
source.onopen = () => { dot.classList.add('live'); };
source.onerror = () => { dot.classList.remove('live'); };
source.onmessage = (msg) => { try { add(JSON.parse(msg.data)); } catch (_) {} };

setInterval(() => {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

fetch('/health?token=' + encodeURIComponent(TOKEN))
  .then(r => r.json())
  .then(d => { clientsEl.textContent = String(d.clients || 1); })
  .catch(() => {});
</script>
</body>
</html>
"""
