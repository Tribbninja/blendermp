import json
import urllib.error
import urllib.request

import pytest

from core import broadcast


def _base(url: str) -> str:
    return url.split("?", 1)[0]


def test_broadcast_serves_page_and_state():
    server = broadcast.BroadcastServer(port=0, title="Test session")
    url = server.start()
    try:
        base = _base(url)
        with urllib.request.urlopen(url, timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "Test session" in html
        assert "EventSource" in html

        with urllib.request.urlopen(
            f"{base}state?token={server.token}", timeout=3
        ) as response:
            state = json.loads(response.read().decode("utf-8"))
        assert state["seq"] == 0
        assert state["events"] == []

        server.publish("chat", {"name": "Ada", "text": "hello"})
        with urllib.request.urlopen(
            f"{base}state?token={server.token}", timeout=3
        ) as response:
            state = json.loads(response.read().decode("utf-8"))
        assert state["seq"] == 1
        assert state["events"][0]["kind"] == "chat"
        assert state["events"][0]["body"]["text"] == "hello"
    finally:
        server.stop()


def test_broadcast_requires_a_token():
    server = broadcast.BroadcastServer(port=0)
    url = server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(f"{_base(url)}state", timeout=3)
        assert info.value.code == 403
    finally:
        server.stop()


def test_broadcast_sse_sends_backlog():
    server = broadcast.BroadcastServer(port=0)
    url = server.start()
    try:
        server.publish("chat", {"name": "Ada", "text": "hi"})
        stream = urllib.request.urlopen(
            f"{_base(url)}events?token={server.token}", timeout=3
        )
        try:
            data = b""
            for _ in range(12):
                line = stream.readline()
                if not line:
                    break
                data += line
                if line.startswith(b"data:"):
                    break
        finally:
            stream.close()
        assert b'"kind":"chat"' in data
    finally:
        server.stop()


def test_broadcast_bounds_large_bodies():
    server = broadcast.BroadcastServer(port=0)
    server.start()
    try:
        server.publish("mesh_snapshot", {"uuid": "u", "payload": b"x" * 10000})
        state = server.snapshot()
        body = state["events"][0]["body"]
        assert body["payload"] == {"bytes": 10000}
    finally:
        server.stop()
