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


def test_object_details_sanitize():
    svc = service()
    body = {
        "uuid": "11111111-1111-1111-1111-111111111111",
        "modifiers": [],
        "details": {
            "hide_viewport": True,
            "hide_render": False,
            "show_in_front": True,
            "color": [0.1, 0.2, 0.3, 1.0],
            "parent": "22222222-2222-2222-2222-222222222222",
            "matrix_parent_inverse": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 2.0, 0.0, 0.0, 1.0],
            "collections": [{"name": "Props", "hide_viewport": False, "hide_render": True, "hide_select": True}],
        },
    }
    result = svc._sanitize_modifiers(body)
    details = result["details"]
    assert details["hide_viewport"] is True
    assert details["color"] == [0.1, 0.2, 0.3, 1.0]
    assert details["parent"] == body["details"]["parent"]
    assert details["matrix_parent_inverse"][12] == 2.0
    assert details["collections"][0]["hide_select"] is True


def test_object_details_rejects_bad_input():
    svc = service()
    uid = "11111111-1111-1111-1111-111111111111"
    assert svc._sanitize_object_details("nope") is None
    assert svc._sanitize_object_details({"parent": "not-a-uuid"}) is None
    assert svc._sanitize_object_details({"color": [1.0, 0.0]}) is None
    assert svc._sanitize_object_details({"matrix_parent_inverse": [1.0] * 15}) is None
    assert svc._sanitize_object_details({"collections": [{"name": ""}]}) is None
    assert svc._sanitize_modifiers({"uuid": uid, "modifiers": [], "details": {"parent": "x"}}) is None

