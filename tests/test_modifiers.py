import queue

import session as session_mod
from core.roomcode import encode_room


def service():
    return session_mod.SessionService(
        queue.Queue(),
        host=True,
        code=encode_room("127.0.0.1", 51999),
        name="host",
        color=(1, 1, 1, 1),
    )


def test_modifier_stack_sanitizes():
    svc = service()
    body = {
        "uuid": "11111111-1111-1111-1111-111111111111",
        "modifiers": [
            {
                "type": "SUBSURF",
                "name": "Subdivision",
                "show_viewport": True,
                "show_render": False,
                "props": {"levels": 2, "render_levels": 3, "subdivision_type": "CATMULL_CLARK"},
            },
            {"type": "ARRAY", "name": "Array", "props": {"count": 4, "relative_offset_displace": [1.0, 0.0, 0.0]}},
        ],
    }
    result = svc._sanitize_modifiers(body)
    assert result["uuid"] == body["uuid"]
    assert [m["type"] for m in result["modifiers"]] == ["SUBSURF", "ARRAY"]
    assert result["modifiers"][0]["show_render"] is False
    assert result["modifiers"][1]["props"]["count"] == 4


def test_modifier_stack_rejects_nonsense():
    svc = service()
    assert svc._sanitize_modifiers({"uuid": "nope", "modifiers": []}) is None
    assert svc._sanitize_modifiers({"uuid": "11111111-1111-1111-1111-111111111111", "modifiers": {}}) is None
    assert svc._sanitize_modifiers({"uuid": "11111111-1111-1111-1111-111111111111", "modifiers": [1, 2]}) is None
    assert (
        svc._sanitize_modifiers(
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "modifiers": [{"type": "", "name": "x", "props": {}}],
            }
        )
        is None
    )
    assert (
        svc._sanitize_modifiers(
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "modifiers": [{"type": "ARRAY", "name": "a", "props": {"count": {"nested": 1}}}],
            }
        )
        is None
    )
