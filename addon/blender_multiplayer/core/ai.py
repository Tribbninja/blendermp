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
* A plain-text scene snapshot is injected into the prompt before the request,
  so small local models do not have to spend a tool round-trip discovering
  object names.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = """You are Blendermp's AI copilot. You help a 3D artist edit the
current scene. You may call the provided tools to inspect and propose edits.
Never invent object names; inspect the scene first. Propose only safe, minimal
edits. Edits are applied only after the user confirms them, so you may propose
several at once. Explain briefly what you will do. When a request is ambiguous,
prefer reading the scene over guessing. Coordinates are Blender world-space
metres; rotations are Euler degrees unless a tool says otherwise."""

MAX_HISTORY_MESSAGES = 40


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], Any], dict[str, Any]]
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
    result: dict[str, Any] | None = None


class OllamaClient:
    """Minimal client for Ollama's /api/chat endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = float(timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 404:
                raise RuntimeError(
                    f"Ollama has no model '{self.model}'. Run: ollama pull {self.model}"
                ) from exc
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail or exc.reason}") from exc
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
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            return []
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        return [
            str(item.get("name", ""))
            for item in models
            if isinstance(item, dict) and item.get("name")
        ]

    def health(self) -> tuple[bool, str]:
        models = self.list_models()
        if not models:
            return False, f"No Ollama server answered at {self.base_url}"
        if self.model not in models and not any(
            item.split(":")[0] == self.model.split(":")[0] for item in models
        ):
            return (
                False,
                f"Model '{self.model}' is not installed. Available: {', '.join(models[:6])}",
            )
        return True, f"Connected. {len(models)} model(s) available."


@dataclass
class AISession:
    client: OllamaClient
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending: list[ProposedEdit] = field(default_factory=list)
    status: str = "idle"  # idle | thinking | done | error
    error: str = ""
    reply: str = ""

    def __post_init__(self):
        if not self.history:
            self.history.append({"role": "system", "content": SYSTEM_PROMPT})

    def ask(
        self,
        user_text: str,
        callback: Callable[[dict[str, Any]], None] | None = None,
        scene_context: str = "",
    ) -> None:
        """Send a user message in a background thread; result lands in ``pending``."""
        text = (user_text or "").strip()
        if not text:
            return
        self.status = "thinking"
        self.error = ""
        self.reply = ""
        self.pending = []

        def worker():
            try:
                messages = list(self.history)
                if scene_context:
                    messages.append(
                        {
                            "role": "system",
                            "content": "Current scene snapshot:\n" + scene_context[:6000],
                        }
                    )
                messages.append({"role": "user", "content": text})
                message = self.client.chat(
                    messages, [tool.schema() for tool in self.tools.values()]
                )
                assistant_content = str(message.get("content", "") or "")
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": assistant_content})
                if len(self.history) > MAX_HISTORY_MESSAGES:
                    self.history = [self.history[0]] + self.history[1:][
                        -(MAX_HISTORY_MESSAGES - 1) :
                    ]
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
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    pending.append(
                        ProposedEdit(
                            tool=name,
                            arguments=args,
                            summary=_summarise(tool, args),
                        )
                    )
                self.pending = pending
                self.reply = assistant_content[:600]
                self.status = "done"
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:300]
                self.status = "error"
            if callback is not None:
                try:
                    callback(
                        {
                            "status": self.status,
                            "pending": len(self.pending),
                            "error": self.error,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=worker, name="bmp-ai", daemon=True)
        thread.start()

    def apply_all(self, context: Any) -> int:
        applied = 0
        for edit in list(self.pending):
            if not edit.approved:
                continue
            if self._apply(edit, context):
                applied += 1
        self.pending = [
            edit
            for edit in self.pending
            if edit.result is None
            or not edit.result.get("ok", True)
            or edit.result.get("error")
        ]
        return applied

    def apply_one(self, index: int, context: Any) -> bool:
        if index < 0 or index >= len(self.pending):
            return False
        edit = self.pending[index]
        if not edit.approved:
            return False
        if not self._apply(edit, context):
            return False
        self.pending.pop(index)
        return True

    def _apply(self, edit: ProposedEdit, context: Any) -> bool:
        tool = self.tools.get(edit.tool)
        if tool is None:
            edit.result = {"ok": False, "error": "unknown tool"}
            return False
        try:
            result = tool.handler(edit.arguments, context)
            if not isinstance(result, dict):
                result = {"ok": True}
            edit.result = result
            return bool(result.get("ok", True)) and not result.get("error")
        except Exception as exc:  # noqa: BLE001
            edit.result = {"ok": False, "error": str(exc)[:200]}
            return False

    def clear(self) -> None:
        self.pending = []
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.status = "idle"
        self.error = ""
        self.reply = ""


def _summarise(tool: ToolSpec, args: dict[str, Any]) -> str:
    parts = []
    for key, value in list(args.items())[:4]:
        text = repr(value)
        if len(text) > 48:
            text = text[:45] + "..."
        parts.append(f"{key}={text}")
    return f"{tool.name}({', '.join(parts)})"


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    handlers: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]],
    read_only: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
        handler=handlers.get(name, _noop),
        read_only=read_only,
    )


def build_default_tools(
    context: Any,
    handlers: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]],
) -> dict[str, ToolSpec]:
    """Build the tool catalogue from a handler map supplied by the addon.

    Keeping the handlers out of this module means ``core.ai`` stays importable
    in plain Python (and testable) without Blender.
    """
    vector = {"type": "array", "items": {"type": "number"}}
    name = {"type": "string"}

    specs = [
        # ---- read-only -------------------------------------------------
        _tool(
            "get_scene_info",
            "List every object in the scene with its type, location and selection state.",
            {},
            [],
            handlers,
            True,
        ),
        _tool(
            "get_selected_objects",
            "List the currently selected objects.",
            {},
            [],
            handlers,
            True,
        ),
        _tool(
            "get_object_info",
            "Get an object's location, rotation, scale, dimensions and materials.",
            {"name": name},
            ["name"],
            handlers,
            True,
        ),
        _tool(
            "get_materials",
            "List the materials in the scene and which objects use them.",
            {},
            [],
            handlers,
            True,
        ),
        _tool(
            "get_object_modifiers",
            "List an object's modifier stack in order.",
            {"name": name},
            ["name"],
            handlers,
            True,
        ),
        # ---- transforms -------------------------------------------------
        _tool(
            "move_object",
            "Move an object to a new world-space location [x, y, z].",
            {"name": name, "location": vector},
            ["name", "location"],
            handlers,
        ),
        _tool(
            "rotate_object",
            "Set an object's Euler rotation in degrees [x, y, z].",
            {"name": name, "rotation": vector},
            ["name", "rotation"],
            handlers,
        ),
        _tool(
            "scale_object",
            "Set an object's scale. Pass one number for uniform scale or [x, y, z].",
            {"name": name, "scale": vector},
            ["name", "scale"],
            handlers,
        ),
        # ---- object lifecycle ------------------------------------------
        _tool(
            "create_primitive",
            "Create a primitive mesh object (cube/sphere/plane/cylinder/cone).",
            {
                "type": {
                    "type": "string",
                    "enum": ["cube", "sphere", "plane", "cylinder", "cone"],
                },
                "name": name,
                "location": vector,
                "size": {"type": "number"},
            },
            ["type"],
            handlers,
        ),
        _tool(
            "delete_object",
            "Delete an object from the scene.",
            {"name": name},
            ["name"],
            handlers,
        ),
        _tool(
            "duplicate_object",
            "Duplicate an object, optionally with a new name.",
            {"name": name, "new_name": name},
            ["name"],
            handlers,
        ),
        _tool(
            "rename_object",
            "Rename an object.",
            {"name": name, "new_name": name},
            ["name", "new_name"],
            handlers,
        ),
        _tool(
            "select_object",
            "Select an object and make it active.",
            {"name": name, "add": {"type": "boolean"}},
            ["name"],
            handlers,
        ),
        _tool(
            "frame_object",
            "Frame an object in the 3D viewport.",
            {"name": name},
            ["name"],
            handlers,
        ),
        _tool(
            "set_object_visibility",
            "Show or hide an object in the viewport and render.",
            {
                "name": name,
                "visible": {"type": "boolean"},
                "render": {"type": "boolean"},
            },
            ["name", "visible"],
            handlers,
        ),
        _tool(
            "set_object_color",
            "Set an object's viewport colour [r, g, b, a] in 0..1.",
            {"name": name, "color": vector},
            ["name", "color"],
            handlers,
        ),
        # ---- materials --------------------------------------------------
        _tool(
            "create_material",
            "Create a new Principled material with a base colour.",
            {
                "name": name,
                "color": vector,
                "metallic": {"type": "number"},
                "roughness": {"type": "number"},
            },
            ["name"],
            handlers,
        ),
        _tool(
            "assign_material",
            "Assign a material to an object's material slot.",
            {"object": name, "material": name, "slot": {"type": "integer"}},
            ["object", "material"],
            handlers,
        ),
        _tool(
            "set_material_color",
            "Set the base colour of a material [r, g, b] or [r, g, b, a].",
            {"material": name, "color": vector},
            ["material", "color"],
            handlers,
        ),
        _tool(
            "set_material_property",
            "Set metallic, roughness or alpha on a material.",
            {
                "material": name,
                "metallic": {"type": "number"},
                "roughness": {"type": "number"},
                "alpha": {"type": "number"},
            },
            ["material"],
            handlers,
        ),
        # ---- modifiers / boolean ---------------------------------------
        _tool(
            "add_modifier",
            "Add a modifier to an object (subsurf, bevel, solidify, mirror, array).",
            {
                "name": name,
                "modifier": {
                    "type": "string",
                    "enum": ["SUBSURF", "BEVEL", "SOLIDIFY", "MIRROR", "ARRAY"],
                },
                "levels": {"type": "integer"},
                "width": {"type": "number"},
            },
            ["name", "modifier"],
            handlers,
        ),
        _tool(
            "apply_boolean",
            "Add or update a Boolean modifier on an object using another object.",
            {
                "name": name,
                "operand": name,
                "operation": {
                    "type": "string",
                    "enum": ["DIFFERENCE", "UNION", "INTERSECT"],
                },
                "apply": {"type": "boolean"},
            },
            ["name", "operand"],
            handlers,
        ),
        # ---- scene ------------------------------------------------------
        _tool(
            "set_scene_setting",
            "Set the current frame, frame range or render engine.",
            {
                "frame": {"type": "integer"},
                "frame_start": {"type": "integer"},
                "frame_end": {"type": "integer"},
                "engine": {"type": "string"},
            },
            [],
            handlers,
        ),
    ]
    return {spec.name: spec for spec in specs}


def _noop(_args, _context=None):
    return {"ok": False, "error": "handler not registered"}
