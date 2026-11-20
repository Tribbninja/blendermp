import json
import socket

from core import cross_engine as ce


def test_unity_exporter_fields():
    obj = ce.ExportedObject(
        uid="u",
        name="Cube",
        kind="mesh",
        matrix=[1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 2.0, 3.0, 4.0, 1.0],
        metadata={"materials": ["Red"]},
    )
    mat = ce.ExportedMaterial(name="Red", base_color=(1.0, 0.0, 0.0, 1.0))
    scene = ce.UnityExporter().export_scene([obj], [mat])
    assert scene["engine"] == "unity"
    assert scene["objects"][0]["uid"] == "u"
    assert scene["objects"][0]["name"] == "Cube"
    assert scene["objects"][0]["position"] == [2.0, 4.0, -3.0]
    assert scene["materials"][0]["color"] == [1.0, 0.0, 0.0, 1.0]


def test_unreal_and_godot_exporters():
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16)
    unreal = ce.UnrealExporter().export_scene([obj])
    assert unreal["engine"] == "unreal"
    assert unreal["objects"][0]["actor"] == "Cube"
    godot = ce.GodotExporter().export_scene([obj])
    assert godot["engine"] == "godot"
    assert "position" in godot["objects"][0]


def test_y_up_conversion_roundtrip_shape():
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    quat = ce.y_up_quat(identity)
    assert len(quat) == 4
    assert abs(sum(value * value for value in quat) - 1.0) < 1e-4


def test_registry_and_scene_stream():
    registry = ce.EngineRegistry.default()
    assert registry.names() == ["godot", "unity", "unreal"]
    received = []
    stream = ce.SceneStream(registry.get("unity"), received.append)
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16)
    stream.push_object(obj)
    assert received and received[0]["type"] == "object"
    assert received[0]["data"]["name"] == "Cube"


def test_mesh_payload_is_included():
    obj = ce.ExportedObject(
        uid="u",
        name="Cube",
        kind="mesh",
        matrix=[1.0] * 16,
        mesh={"positions": [0, 0, 0, 1, 0, 0, 0, 1, 0], "indices": [0, 1, 2]},
    )
    data = ce.UnityExporter().export_object(obj)
    assert data["mesh"]["indices"] == [0, 1, 2]


def test_bridge_streams_objects_to_a_client():
    server = ce.BridgeServer(port=0)
    url = server.start()
    host, port = url.split("://", 1)[1].rsplit(":", 1)
    client = socket.create_connection((host, int(port)), timeout=3)
    reader = client.makefile("rb")
    try:
        hello = json.loads(reader.readline())
        assert hello["type"] == "hello"
        assert hello["protocol"] == ce.BRIDGE_PROTOCOL
        assert hello["engine"] == "unity"

        obj = ce.ExportedObject(
            uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16
        )
        server.publish_object(ce.UnityExporter(), obj)
        message = json.loads(reader.readline())
        assert message["type"] == "object"
        assert message["uid"] == "u"

        server.publish_remove("u")
        removed = json.loads(reader.readline())
        assert removed["type"] == "remove"
        assert removed["uid"] == "u"
    finally:
        reader.close()
        client.close()
        server.stop()


def test_bridge_replays_the_scene_snapshot_to_late_clients():
    server = ce.BridgeServer(port=0)
    url = server.start()
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16)
    server.publish_scene(ce.UnityExporter(), [obj])
    host, port = url.split("://", 1)[1].rsplit(":", 1)
    client = socket.create_connection((host, int(port)), timeout=3)
    reader = client.makefile("rb")
    try:
        assert json.loads(reader.readline())["type"] == "hello"
        scene = json.loads(reader.readline())
        assert scene["type"] == "scene"
        assert scene["objects"][0]["uid"] == "u"
    finally:
        reader.close()
        client.close()
        server.stop()


def test_bridge_answers_ping():
    server = ce.BridgeServer(port=0)
    url = server.start()
    host, port = url.split("://", 1)[1].rsplit(":", 1)
    client = socket.create_connection((host, int(port)), timeout=3)
    reader = client.makefile("rb")
    try:
        reader.readline()  # hello
        client.sendall(b'{"type":"ping"}\n')
        pong = json.loads(reader.readline())
        assert pong["type"] == "pong"
    finally:
        reader.close()
        client.close()
        server.stop()
