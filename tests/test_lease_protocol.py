import queue
import socket
import threading
import time

import session as session_mod
from core.roomcode import encode_room
from sync.leases import Role


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(predicate, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Events:
    def __init__(self, inbox):
        self.items = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._pump, args=(inbox,), daemon=True)
        self.thread.start()

    def _pump(self, inbox):
        while not self.stop_flag.is_set():
            try:
                item = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                self.items.append(item)

    def has(self, kind, **fields):
        with self.lock:
            for name, body in self.items:
                if name == kind and all(body.get(k) == v for k, v in fields.items()):
                    return True
        return False

    def stop(self):
        self.stop_flag.set()


def test_host_grant_and_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        key = "22222222-2222-2222-2222-222222222222"
        assert host.local_lease(key, "acquire")
        assert wait_for(lambda: host_events.has("lease", action="acquire", owner="host"))

        client.send_lease(key, "acquire")
        assert wait_for(lambda: client_events.has("lease", action="denied", owner="host"))
        assert wait_for(lambda: host_events.has("lease", action="requested", requester="alice", key=key))

        host.send_lease(key, "grant", "alice")
        assert wait_for(lambda: client_events.has("lease", action="grant", owner="alice"))
        assert host._leases.owner_of(key, time.monotonic()) == "alice"

        assert host.local_lease(key, "force")
        key2 = "44444444-4444-4444-4444-444444444444"
        assert host.local_lease(key2, "acquire")
        client.send_lease(key2, "acquire")
        assert wait_for(lambda: host_events.has("lease", action="requested", requester="alice", key=key2))
        host.send_lease(key2, "deny", "alice")
        assert wait_for(lambda: client_events.has("lease", action="denied_request", requester="alice", key=key2))
        assert host._leases.requests_for(key2) == []
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_lease_expiry_is_broadcast(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host._leases = session_mod.LeaseManager(ttl=0.4, grace=0.4)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        key = "33333333-3333-3333-3333-333333333333"
        client.send_lease(key, "acquire")
        assert wait_for(lambda: client_events.has("lease", action="acquire", owner="alice"))
        assert wait_for(lambda: client_events.has("lease", action="expiring"), timeout=4)
        assert wait_for(lambda: client_events.has("lease", action="released"), timeout=4)
        assert host._leases.owner_of(key, time.monotonic()) is None
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_force_release_all_clears_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        key = "55555555-5555-5555-5555-555555555555"
        client.send_lease(key, "acquire")
        assert wait_for(lambda: host._leases.owner_of(key, time.monotonic()) == "alice")
        host.force_release_all()
        assert wait_for(lambda: client_events.has("lease", action="released"))
        assert host._leases.owner_of(key, time.monotonic()) is None
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_presence_delta_does_not_reset_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        client.send_presence(
            {
                "scene": "Demo",
                "frame": 1,
                "playing": False,
                "fps": 24,
                "mode": "OBJECT",
                "active": "Cube",
                "selected": "u",
            }
        )
        assert wait_for(lambda: host_events.has("presence", frame=1, scene="Demo"))
        client.send_presence({"frame": 2})
        assert wait_for(lambda: host_events.has("presence", frame=2))
        with host_events.lock:
            bodies = [
                body
                for name, body in host_events.items
                if name == "presence" and body.get("frame") == 2
            ]
        assert bodies and "scene" not in bodies[-1]

        host.send_presence({"frame": 3})
        assert wait_for(lambda: client_events.has("presence", frame=3))
        with client_events.lock:
            bodies = [
                body
                for name, body in client_events.items
                if name == "presence" and body.get("frame") == 3
            ]
        assert bodies and "scene" not in bodies[-1]
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_client_stats_quality_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        client.send_client_stats(
            {"apply_ms_p95": 5.0, "fps": 60.0, "rx_kbps": 1.0, "tx_kbps": 1.0, "queue_depth": 0.0}
        )
        assert wait_for(lambda: host_events.has("client_stats", name="alice", quality=0))
        client.send_client_stats(
            {"apply_ms_p95": 50.0, "fps": 10.0, "rx_kbps": 1.0, "tx_kbps": 1.0, "queue_depth": 0.0}
        )
        assert wait_for(lambda: host_events.has("client_stats", name="alice", quality=2))
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_role_guard_and_downgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    client_inbox = queue.Queue()
    host_events = Events(host_inbox)
    client_events = Events(client_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    client = session_mod.SessionService(client_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected")
        key = "55555555-5555-5555-5555-555555555555"
        client.send_lease(key, "acquire")
        assert wait_for(lambda: client_events.has("lease", action="acquire", owner="alice"))

        host.set_role("alice", int(Role.VIEWER))
        assert wait_for(lambda: client_events.has("role", name="alice", role=int(Role.VIEWER)))
        # Downgrading releases the viewer's leases.
        assert wait_for(lambda: client_events.has("lease", action="release"), timeout=4)
        assert host._leases.owner_of(key, time.monotonic()) is None

        client.send_lease(key, "acquire")
        assert wait_for(
            lambda: client_events.has("error", message="your role does not allow editing"),
            timeout=4,
        )

        host.set_role("alice", int(Role.EDITOR))
        assert wait_for(lambda: client_events.has("role", name="alice", role=int(Role.EDITOR)))
        client.send_lease(key, "acquire")
        assert wait_for(lambda: client_events.has("lease", action="acquire", owner="alice"))
    finally:
        client.stop()
        host.stop()
        client_events.stop()
        host_events.stop()


def test_selection_and_cursor_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_inbox = queue.Queue()
    a_inbox = queue.Queue()
    b_inbox = queue.Queue()
    host_events = Events(host_inbox)
    a_events = Events(a_inbox)
    b_events = Events(b_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")
    a = session_mod.SessionService(a_inbox, host=False, code=code, name="alice", color=(1, 1, 1, 1))
    b = session_mod.SessionService(b_inbox, host=False, code=code, name="bob", color=(1, 1, 1, 1))
    a.start()
    b.start()
    try:
        assert wait_for(lambda: a.state == "connected" and b.state == "connected")
        uid = "66666666-6666-6666-6666-666666666666"
        a.send_selection(uid, bytes([0b00000101]), 3)
        assert wait_for(
            lambda: b_events.has(
                "selection", name="alice", uuid=uid, count=3, mask=bytes([0b00000101])
            )
        )

        a.send_presence({"cursor": [1.0, 2.0, 3.0], "scene": "Scene", "frame": 1, "mode": "EDIT_MESH"})
        assert wait_for(
            lambda: b_events.has("presence", name="alice", cursor=[1.0, 2.0, 3.0])
        )

        a.send_laser([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        assert wait_for(
            lambda: b_events.has(
                "laser", name="alice", points=[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
            )
        )
    finally:
        a.stop()
        b.stop()
        host.stop()
        a_events.stop()
        b_events.stop()
        host_events.stop()


def test_lease_request_and_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    port = free_port()
    code = encode_room("127.0.0.1", port)

    host_inbox = queue.Queue()
    host_events = Events(host_inbox)
    host = session_mod.SessionService(host_inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")

    a_inbox = queue.Queue()
    b_inbox = queue.Queue()
    a_events = Events(a_inbox)
    b_events = Events(b_inbox)
    a = session_mod.SessionService(a_inbox, host=False, code=code, name="alice", color=(0.2, 0.6, 1.0, 1.0))
    b = session_mod.SessionService(b_inbox, host=False, code=code, name="bob", color=(1.0, 0.6, 0.2, 1.0))
    a.start()
    b.start()
    try:
        assert wait_for(lambda: a.state == "connected" and b.state == "connected")
        key = "11111111-1111-1111-1111-111111111111"
        a.send_lease(key, "acquire")
        assert wait_for(lambda: a_events.has("lease", action="acquire", owner="alice"))

        b.send_lease(key, "acquire")
        assert wait_for(lambda: b_events.has("lease", action="denied", owner="alice"))
        assert wait_for(lambda: a_events.has("lease", action="requested", requester="bob"))

        a.send_lease(key, "grant", "bob")
        assert wait_for(lambda: b_events.has("lease", action="grant", owner="bob"))
    finally:
        a.stop()
        b.stop()
        host.stop()
        a_events.stop()
        b_events.stop()
        host_events.stop()

