import threading

import pytest

from core import ai


class FakeClient:
    def __init__(self, message):
        self.message = message
        self.last_tools = None

    def chat(self, messages, tools=None):
        self.last_tools = tools
        return self.message

    def list_models(self):
        return ["m"]


def test_tool_schema_shape():
    def noop(args, ctx=None):
        return {"ok": True}

    tools = ai.build_default_tools(None, {"get_scene_info": noop})
    assert "get_scene_info" in tools
    schema = tools["get_scene_info"].schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_scene_info"
    assert schema["function"]["parameters"]["type"] == "object"


def test_session_proposes_edits_from_tool_calls():
    message = {
        "role": "assistant",
        "content": "moving the cube",
        "tool_calls": [
            {"function": {"name": "move_object", "arguments": {"name": "Cube", "location": [1.0, 2.0, 3.0]}}}
        ],
    }
    client = FakeClient(message)
    tools = ai.build_default_tools(None, {"move_object": lambda a, c: {"ok": True}})
    session = ai.AISession(client, tools)
    session.ask("move the cube", callback=lambda _r: None)
    # worker thread: wait briefly
    for _ in range(50):
        if session.status != "thinking":
            break
        threading.Event().wait(0.05)
    assert session.status == "done"
    assert len(session.pending) == 1
    assert session.pending[0].tool == "move_object"
    assert session.pending[0].arguments["name"] == "Cube"


def test_apply_all_runs_handlers():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "move_object", "arguments": {"name": "Cube", "location": [1.0, 2.0, 3.0]}}}
        ],
    }
    client = FakeClient(message)
    calls = []

    def handler(args, ctx):
        calls.append(args)
        return {"ok": True}

    tools = ai.build_default_tools(None, {"move_object": handler})
    session = ai.AISession(client, tools)
    session.ask("move", callback=lambda _r: None)
    for _ in range(50):
        if session.status != "thinking":
            break
        threading.Event().wait(0.05)
    applied = session.apply_all(None)
    assert applied == 1
    assert calls and calls[0]["name"] == "Cube"
    assert session.pending == []


def test_clear_resets_state():
    client = FakeClient({"role": "assistant", "content": "hi", "tool_calls": []})
    session = ai.AISession(client, {})
    session.ask("hello", callback=lambda _r: None)
    for _ in range(50):
        if session.status != "thinking":
            break
        threading.Event().wait(0.05)
    session.clear()
    assert session.pending == []
    assert session.status == "idle"


def test_ollama_client_error_raises():
    client = ai.OllamaClient("http://127.0.0.1:1", model="m", timeout=1)
    with pytest.raises(RuntimeError):
        client.chat([{"role": "user", "content": "hi"}])
