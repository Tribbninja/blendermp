"""Ollama-backed AI copilot for the Blendermp addon.

The copilot talks to a *local* Ollama server (http://127.0.0.1:11434 by
default) and proposes edits as a list of structured tool calls. The user
reviews and confirms them before anything touches the scene, so the AI can
never silently change a file or send a sync frame.

Design
------
* ``ToolSpec`` describes one callable capability: a name, a human description,
  a JSON Schema for its arguments, and a handler.
* ``AISession`` owns the conversation history, the pending proposals and the
  status. It is stateless with respect to Blender: handlers are callables
  injected by the addon, so the whole module is testable without bpy.
* ``OllamaClient`` is a thin wrapper over ``urllib.request`` so the addon has
  no dependency on ``requests``.
* All scene mutation happens through the tool handlers, which run on Blender's
  main thread (they are applied from the addon tick / an operator). That keeps
  undo, sync and lease handling identical to a manual edit.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = """You are Blendermp's AI copilot. You help a 3D artist edit the
current scene. You may call the provided tools to inspect and propose edits.
Never invent object names; inspect the scene first. Propose only safe, minimal
edits. Edits are applied only after the user confirms them, so you may propose
several at once. Explain briefly what you will do."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    read_only: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ProposedEdit:
    tool: str
    arguments: dict[str, Any]
    summary: str
    approved: bool = True


class OllamaClient:
    """Minimal client for Ollama's /api/chat endpoint."""

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL, model: str = DEFAULT_MODEL, timeout: float = 120.0):
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = float(timeout)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama unreachable at {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc
        message = payload.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Ollama response had no message")
        return message

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError):
            return []
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        return [str(item.get("name", "")) for item in models if isinstance(item, dict)]


@dataclass
class AISession:
    client: OllamaClient
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending: list[ProposedEdit] = field(default_factory=list)
    status: str = "idle"  # idle | thinking | done | error
    error: str = ""

    def __post_init__(self):
        if not self.history:
            self.history.append({"role": "system", "content": SYSTEM_PROMPT})

    def ask(self, user_text: str, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Send a user message in a background thread; result lands in ``pending``."""
        text = (user_text or "").strip()
        if not text:
            return
        self.status = "thinking"
        self.error = ""
        self.pending = []

        def worker():
            try:
                messages = list(self.history) + [{"role": "user", "content": text}]
                message = self.client.chat(messages, [tool.schema() for tool in self.tools.values()])
                assistant_content = str(message.get("content", "") or "")
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": assistant_content})
                pending = []
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name", ""))
                    tool = self.tools.get(name)
                    if tool is None:
                        continue
                    args = function.get("arguments", {})
                    if not isinstance(args, dict):
                        args = {}
                    pending.append(ProposedEdit(tool=name, arguments=args, summary=_summarise(tool, args)))
                self.pending = pending
                self.status = "done"
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:300]
                self.status = "error"
            if callback is not None:
                try:
                    callback({"status": self.status, "pending": len(self.pending), "error": self.error})
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=worker, name="bmp-ai", daemon=True)
        thread.start()

    def apply_all(self, context: Any) -> int:
        applied = 0
        for edit in list(self.pending):
            if not edit.approved:
                continue
            tool = self.tools.get(edit.tool)
            if tool is None:
                continue
            try:
                tool.handler(edit.arguments, context)
                applied += 1
            except Exception:  # noqa: BLE001
                pass
        self.pending = []
        return applied

    def apply_one(self, index: int, context: Any) -> bool:
        if index < 0 or index >= len(self.pending):
            return False
        edit = self.pending[index]
        if not edit.approved:
            return False
        tool = self.tools.get(edit.tool)
        if tool is None:
            return False
        try:
            tool.handler(edit.arguments, context)
        except Exception:  # noqa: BLE001
            return False
        self.pending.pop(index)
        return True

    def clear(self) -> None:
        self.pending = []
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.status = "idle"
        self.error = ""


def _summarise(tool: ToolSpec, args: dict[str, Any]) -> str:
    return f"{tool.name}({', '.join(f'{key}={value!r}' for key, value in list(args.items())[:4])})"


def build_default_tools(context: Any, handlers: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]]) -> dict[str, ToolSpec]:
    """Build the tool catalogue from a handler map supplied by the addon.

    Keeping the handlers out of this module means ``core.ai`` stays importable
    in plain Python (and testable) without Blender.
    """
    specs: list[ToolSpec] = []
    # read-only
    specs.append(ToolSpec("get_scene_info", "List every object in the scene with its type.",
        {"type": "object", "properties": {}, "required": []}, handlers.get("get_scene_info", _noop), True))
    specs.append(ToolSpec("get_selected_objects", "List the currently selected objects.",
        {"type": "object", "properties": {}, "required": []}, handlers.get("get_selected_objects", _noop), True))
    specs.append(ToolSpec("get_object_info", "Get an object's location, rotation, scale and dimensions.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        handlers.get("get_object_info", _noop), True))
    specs.append(ToolSpec("get_materials", "List the materials in the scene.",
        {"type": "object", "properties": {}, "required": []}, handlers.get("get_materials", _noop), True))
    # proposed edits
    specs.append(ToolSpec("move_object", "Move an object to a new location.",
        {"type": "object", "properties": {"name": {"type": "string"}, "location": {"type": "array", "items": {"type": "number"}}}, "required": ["name", "location"]},
        handlers.get("move_object", _noop)))
    specs.append(ToolSpec("rotate_object", "Rotate an object (euler degrees).",
        {"type": "object", "properties": {"name": {"type": "string"}, "rotation": {"type": "array", "items": {"type": "number"}}}, "required": ["name", "rotation"]},
        handlers.get("rotate_object", _noop)))
    specs.append(ToolSpec("scale_object", "Scale an object.",
        {"type": "object", "properties": {"name": {"type": "string"}, "scale": {"type": "array", "items": {"type": "number"}}}, "required": ["name", "scale"]},
        handlers.get("scale_object", _noop)))
    specs.append(ToolSpec("create_primitive", "Create a primitive object (cube/sphere/plane/cylinder/cone).",
        {"type": "object", "properties": {"type": {"type": "string", "enum": ["cube", "sphere", "plane", "cylinder", "cone"]}, "name": {"type": "string"}, "location": {"type": "array", "items": {"type": "number"}}, "size": {"type": "number"}}, "required": ["type"]},
        handlers.get("create_primitive", _noop)))
    specs.append(ToolSpec("delete_object", "Delete an object.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        handlers.get("delete_object", _noop)))
    specs.append(ToolSpec("duplicate_object", "Duplicate an object.",
        {"type": "object", "properties": {"name": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["name"]},
        handlers.get("duplicate_object", _noop)))
    specs.append(ToolSpec("rename_object", "Rename an object.",
        {"type": "object", "properties": {"name": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["name", "new_name"]},
        handlers.get("rename_object", _noop)))
    specs.append(ToolSpec("set_material_color", "Set the base colour of a material.",
        {"type": "object", "properties": {"material": {"type": "string"}, "color": {"type": "array", "items": {"type": "number"}}}, "required": ["material", "color"]},
        handlers.get("set_material_color", _noop)))
    return {spec.name: spec for spec in specs}


def _noop(_args, _context=None):
    return {"ok": False, "error": "handler not registered"}
