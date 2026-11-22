"""Static invariants for the Cloudflare relay host-token flow.

These are source-level checks on purpose: the Worker runs in workerd, which the
Python suite cannot execute. They guard the architecture that fixed the "hub
rejected the websocket: 403" regression:

* the relay request must be forwarded to the room and verified there, not by a
  pre-flight subrequest to the directory (a subrequest from a WebSocket upgrade
  context can resolve to a different Durable Object namespace with empty state);
* ``/announce`` must push the token hash to the room;
* the room must compare the supplied token in constant time.
"""

from pathlib import Path

WORKER = (
    Path(__file__).resolve().parents[1] / "cloudflare" / "src" / "index.js"
).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    first = text.index(start)
    last = text.index(end, first)
    return text[first:last]


def test_relay_is_forwarded_to_the_room_without_a_preflight_verify():
    default_export = _between(WORKER, "export default {", "export class Hub")
    relay_branch = _between(default_export, 'startsWith("/relay/")', "const directory =")
    assert "ROOM.get" in relay_branch
    assert "room.fetch(request)" in relay_branch
    assert "/verify" not in relay_branch


def test_announce_pushes_the_host_token_hash_to_the_room():
    announce = _between(WORKER, 'url.pathname === "/announce"', 'url.pathname === "/remove"')
    assert "room/host-token" in announce
    assert "env.ROOM" in announce
    assert "hash" in announce


def test_room_verifies_the_host_token_from_its_own_state():
    relay = WORKER[WORKER.index("export class Room extends Hub {") :]
    assert 'role === "host"' in relay
    assert "host-token" in relay
    assert "timingSafeEqual" in relay
    assert "host token required" in relay


def test_health_reports_no_debug_markers():
    assert '"build"' not in WORKER
    assert "/debug-state" not in WORKER


def test_signal_requests_are_routed_to_the_room():
    default_export = _between(WORKER, "export default {", "export class Hub")
    signal_branch = _between(default_export, 'startsWith("/signal/")', "const directory =")
    assert "ROOM.get" in signal_branch
    assert "room.fetch(request)" in signal_branch


def test_room_forwards_signalling_messages_between_peers():
    room = WORKER[WORKER.index("export class Room extends Hub {") :]
    assert "async signal(" in room
    assert "signalMessage" in room
    assert "broadcastSignal" in room
    assert "sendSignal" in room
    assert "peer already connected" in room
    assert "MAX_SIGNAL" in WORKER


def test_signalling_validates_peer_and_message_shapes():
    assert "cleanSignalPeer" in WORKER
    assert "cleanSignalType" in WORKER
    assert "length <= 128" in WORKER
