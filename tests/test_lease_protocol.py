import queue
import socket
import threading
import time

import session as session_mod
from core.roomcode import encode_room


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
