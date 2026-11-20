import threading

import pytest

from core import ai


class FakeClient:
    def __init__(self, message):
        self.message = message
        self.last_tools = None
        self.last_messages = None

    def chat(self, messages, tools=None):
        self.last_tools = tools
        self.last_messages = messages
        return self.message

    def list_models(self):
        return ["m"]


def _wait(session, timeout=2.5):
    step = 0.05
    for _ in range(int(timeout / step)):
        if session.status != "thinking":
            return
        threading.Event().wait(step)


def test_tool_schema_shape():
    def noop(args, ctx=None):
        return {"ok": True}

    tools = ai.build_default_tools(None, {"get_scene_info": noop})
    assert "get_scene_info" in tools
    schema = tools["get_scene_info"].schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_scene_info"
    assert schema["function"]["parameters"]["type"] == "object"


def test_tool_catalogue_covers_editing_and_materials():
    tools = ai.build_default_tools(None, {})
    for name in (
        "get_scene_info",
        "get_materials",
        "get_object_modifiers",
        "move_object",
        "create_primitive",
        "rename_object",
        "create_material",
        "assign_material",
        "set_material_property",
        "add_modifier",
        "apply_boolean",
        "set_scene_setting",
    ):
        assert name in tools
    assert tools["get_scene_info"].read_only is True
    assert tools["apply_boolean"].read_only is False


def test_session_proposes_edits_from_tool_calls():
    message = {
        "role": "assistant",
        "content": "moving the cube",
        "tool_calls": [
            {
                "function": {
                    "name": "move_object",
                    "arguments": {"name": "Cube", "location": [1.0, 2.0, 3.0]},
                }
            }
        ],
    }
    client = FakeClient(message)
    tools = ai.build_default_tools(None, {"move_object": lambda a, c: {"ok": True}})
    session = ai.AISession(client, tools)
    session.ask("move the cube", callback=lambda _r: None)
    _wait(session)
    assert session.status == "done"
    assert len(session.pending) == 1
    assert session.pending[0].tool == "move_object"
    assert session.pending[0].arguments["name"] == "Cube"
    assert session.reply == "moving the cube"


def test_scene_context_is_injected():
    client = FakeClient({"role": "assistant", "content": "ok", "tool_calls": []})
    session = ai.AISession(client, {})
    session.ask("what is here?", scene_context="Scene: Demo\n- Cube (MESH)")
    _wait(session)
    contents = [message["content"] for message in client.last_messages]
    assert any("scene snapshot" in text.lower() for text in contents)
    assert any("Cube (MESH)" in text for text in contents)


def test_tool_arguments_accept_a_json_string():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "move_object",
                    "arguments": '{"name": "Cube", "location": [1, 2, 3]}',
                }
            }
        ],
    }
    client = FakeClient(message)
    tools = ai.build_default_tools(None, {"move_object": lambda a, c: {"ok": True}})
    session = ai.AISession(client, tools)
    session.ask("move", callback=lambda _r: None)
    _wait(session)
    assert session.pending[0].arguments["name"] == "Cube"


def test_apply_all_runs_handlers():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "move_object",
                    "arguments": {"name": "Cube", "location": [1.0, 2.0, 3.0]},
                }
            }
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
    _wait(session)
    applied = session.apply_all(None)
    assert applied == 1
    assert calls and calls[0]["name"] == "Cube"
    assert session.pending == []


def test_apply_all_keeps_failed_edits():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "move_object",
                    "arguments": {"name": "Missing", "location": [0, 0, 0]},
                }
            }
        ],
    }
    client = FakeClient(message)

    def handler(args, ctx):
        return {"ok": False, "error": "object not found"}

    tools = ai.build_default_tools(None, {"move_object": handler})
    session = ai.AISession(client, tools)
    session.ask("move", callback=lambda _r: None)
    _wait(session)
    assert session.apply_all(None) == 0
    assert len(session.pending) == 1
    assert session.pending[0].result["error"] == "object not found"


def test_apply_all_treats_error_results_as_failures():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "delete_object",
                    "arguments": {"name": "Missing"},
                }
            }
        ],
    }
    client = FakeClient(message)
    tools = ai.build_default_tools(
        None, {"delete_object": lambda a, c: {"error": "object not found"}}
    )
    session = ai.AISession(client, tools)
    session.ask("delete", callback=lambda _r: None)
    _wait(session)
    assert session.apply_all(None) == 0
    assert session.pending[0].result["error"] == "object not found"


def test_clear_resets_state():
    client = FakeClient({"role": "assistant", "content": "hi", "tool_calls": []})
    session = ai.AISession(client, {})
    session.ask("hello", callback=lambda _r: None)
    _wait(session)
    session.clear()
    assert session.pending == []
    assert session.status == "idle"
    assert session.history[0]["role"] == "system"


def test_ollama_client_error_raises():
    client = ai.OllamaClient("http://127.0.0.1:1", model="m", timeout=1)
    with pytest.raises(RuntimeError):
        client.chat([{"role": "user", "content": "hi"}])
