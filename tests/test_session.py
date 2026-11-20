import queue
import socket
import threading
import time

import pytest

import control
import session as session_mod
from core import tls
from core.roomcode import encode_room


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(predicate, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Events:
    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox
        self.items: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                item = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                self.items.append(item)

    def all(self):
        with self.lock:
            return list(self.items)

    def has(self, kind: str, **fields) -> bool:
        with self.lock:
            for name, body in self.items:
                if name != kind:
                    continue
                if all(body.get(key) == value for key, value in fields.items()):
                    return True
        return False

    def stop(self):
        self._stop.set()


@pytest.fixture()
def appdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_host_and_client_chat(appdir):
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_events = Events(queue.Queue())
    client_events = Events(queue.Queue())

    host = session_mod.SessionService(
        host_events.inbox, host=True, code=code, name="host", color=(0.4, 0.9, 1.0, 1.0), port=port
    )
    host.start()
    assert wait_for(lambda: host.state == "hosting"), "host did not start"
    assert host.fingerprint

    client = session_mod.SessionService(
        client_events.inbox, host=False, code=code, name="ada", color=(0.9, 0.4, 1.0, 1.0), port=port
    )
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected"), "client did not connect"
        assert wait_for(lambda: host_events.has("peer_join", name="ada")), "host missed the join"
        assert wait_for(lambda: host_events.has("host_ready")), "host never published its TLS identity"

        client.send_chat("hello from ada")
        assert wait_for(lambda: host_events.has("chat", name="ada", text="hello from ada")), "chat did not arrive"

        # The host is authoritative: relayed messages carry the host-verified name.
        host.send_chat("hello from the host")
        assert wait_for(lambda: client_events.has("chat", name="host", text="hello from the host"))

        assert client.stats()["bytes_in"] > 0
        assert client.stats()["bytes_out"] > 0
        assert host.stats()["bytes_out"] > 0
    finally:
        client.stop()
        host.stop()
        host_events.stop()
        client_events.stop()


def test_scene_bootstrap_messages(appdir):
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_events = Events(queue.Queue())
    client_events = Events(queue.Queue())
    host = session_mod.SessionService(
        host_events.inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port
    )
    client = session_mod.SessionService(
        client_events.inbox, host=False, code=code, name="ada", color=(1, 1, 1, 1), port=port
    )
    host.start()
    try:
        assert wait_for(lambda: host.state == "hosting")
        client.start()
        assert wait_for(lambda: client.state == "connected"), client.failure_reason
        assert wait_for(lambda: host_events.has("peer_join", name="ada"))
        host.send_scene_reset("ada")
        assert wait_for(lambda: client_events.has("scene_reset", target="ada"))
        host.send_scene_ready("ada")
        assert wait_for(lambda: client_events.has("scene_ready", target="ada"))
    finally:
        client.stop()
        host.stop()
        host_events.stop()
        client_events.stop()


def test_client_rejects_changed_certificate(appdir):
    port = free_port()
    code = encode_room("127.0.0.1", port)
    tls.remember_fingerprint(control.data_dir(), code, "0" * 64)

    events = Events(queue.Queue())
    host = session_mod.SessionService(events.inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port)
    host.start()
    assert wait_for(lambda: host.state == "hosting")

    client = session_mod.SessionService(events.inbox, host=False, code=code, name="ada", color=(1, 1, 1, 1), port=port)
    client.start()
    try:
        assert wait_for(lambda: client.failed), "client accepted an unpinned certificate"
        assert "certificate" in client.failure_reason.lower()
    finally:
        client.stop()
        host.stop()
        events.stop()


def test_invalid_room_code_fails_closed(appdir):
    events = Events(queue.Queue())
    client = session_mod.SessionService(events.inbox, host=False, code="ZZZZZ-ZZZZZ-ZZZZZ", name="ada", color=(1, 1, 1, 1))
    client.start()
    try:
        assert wait_for(lambda: client.failed)
    finally:
        client.stop()
        events.stop()

def test_host_can_kick_client(appdir):
    port = free_port()
    code = encode_room("127.0.0.1", port)
    host_events = Events(queue.Queue())
    client_events = Events(queue.Queue())

    host = session_mod.SessionService(
        host_events.inbox, host=True, code=code, name="host", color=(1, 1, 1, 1), port=port
    )
    host.start()
    assert wait_for(lambda: host.state == "hosting"), "host did not start"
    client = session_mod.SessionService(
        client_events.inbox, host=False, code=code, name="ada", color=(1, 1, 1, 1), port=port
    )
    client.start()
    try:
        assert wait_for(lambda: client.state == "connected"), client.failure_reason
        assert wait_for(lambda: host_events.has("peer_join", name="ada"))
        host.kick("ada")
        assert wait_for(lambda: client_events.has("kicked")), "client did not receive the kick"
        assert wait_for(lambda: client.failed), "client did not stop after the kick"
        assert wait_for(lambda: host_events.has("peer_leave", name="ada")), "host did not drop the peer"
    finally:
        client.stop()
        host.stop()
        host_events.stop()
        client_events.stop()
