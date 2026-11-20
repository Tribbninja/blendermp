from core import cross_engine as ce


def test_unity_exporter():
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16, metadata={"x": 1})
    mat = ce.ExportedMaterial(name="Red", base_color=(1.0, 0.0, 0.0, 1.0))
    exporter = ce.UnityExporter()
    scene = exporter.export_scene([obj], [mat])
    assert scene["engine"] == "unity"
    assert scene["objects"][0]["prefab"] == "Cube"
    assert scene["materials"][0]["color"] == [1.0, 0.0, 0.0, 1.0]


def test_unreal_exporter():
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16)
    exporter = ce.UnrealExporter()
    scene = exporter.export_scene([obj])
    assert scene["engine"] == "unreal"
    assert scene["objects"][0]["actor"] == "Cube"


def test_registry_and_scene_stream():
    registry = ce.EngineRegistry()
    registry.register(ce.UnityExporter())
    assert registry.names() == ["unity"]
    received = []
    stream = ce.SceneStream(registry.get("unity"), received.append)
    obj = ce.ExportedObject(uid="u", name="Cube", kind="mesh", matrix=[1.0] * 16)
    stream.push_object(obj)
    assert received and received[0]["type"] == "object"
