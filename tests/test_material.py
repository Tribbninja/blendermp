import pytest

from core import material


def test_sanitize_basic():
    data = material.sanitize(
        {
            "name": "Red",
            "base_color": [1.0, 0.1, 0.1, 1.0],
            "metallic": 0.2,
            "roughness": 0.4,
            "emission_color": [0.0, 0.0, 0.0],
            "emission_strength": 0.0,
        }
    )
    assert data["name"] == "Red"
    assert data["base_color"] == [1.0, 0.1, 0.1, 1.0]


def test_sanitize_defaults():
    data = material.sanitize({})
    assert data is not None
    assert data["name"] == "Material"
    assert data["metallic"] == 0.0
    assert data["roughness"] == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "material",
        {"base_color": [2.0, 0.0, 0.0, 1.0]},
        {"metallic": 5},
        {"roughness": -1},
        {"emission_strength": 1000},
        {"base_color": [0.0, 0.0]},
        {"emission_color": "red"},
    ],
)
def test_sanitize_rejects_bad_values(bad):
    assert material.sanitize(bad) is None


def test_name_is_trimmed():
    data = material.sanitize({"name": "  " + "x" * 100})
    assert len(data["name"]) == 64


def _graph_material():
    return {
        "name": "Textured",
        "nodes": [
            {"bl_idname": "ShaderNodeOutputMaterial", "name": "Material Output", "location": [300, 0]},
            {
                "bl_idname": "ShaderNodeBsdfPrincipled",
                "name": "Principled BSDF",
                "location": [0, 0],
                "inputs": {"Base Color": [0.5, 0.2, 0.1, 1.0], "Metallic": 0.25},
            },
            {
                "bl_idname": "ShaderNodeTexImage",
                "name": "Image Texture",
                "location": [-300, 0],
                "image": "brick.png",
                "image_hash": "a" * 40,
                "image_colorspace": "sRGB",
                "image_alpha_mode": "STRAIGHT",
                "props": {"interpolation": "Closest", "extension": "REPEAT"},
            },
            {
                "bl_idname": "ShaderNodeValToRGB",
                "name": "Color Ramp",
                "props": {"interpolation": "EASE", "elements": [[0.0, 0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0, 1.0, 1.0]]},
            },
        ],
        "links": [
            [2, "Color", 1, "Base Color"],
            [1, "BSDF", 0, "Surface"],
        ],
    }


def test_sanitize_keeps_whitelisted_graph():
    data = material.sanitize(_graph_material())
    assert data is not None
    assert [node["bl_idname"] for node in data["nodes"]][0] == "ShaderNodeOutputMaterial"
    assert data["nodes"][2]["image"] == "brick.png"
    assert data["nodes"][2]["image_colorspace"] == "sRGB"
    assert data["nodes"][2]["image_alpha_mode"] == "STRAIGHT"
    assert data["nodes"][2]["props"]["interpolation"] == "Closest"
    assert data["links"] == [[2, "Color", 1, "Base Color"], [1, "BSDF", 0, "Surface"]]
    assert data["nodes"][3]["props"]["elements"][0] == [0.0, 0.0, 0.0, 0.0, 1.0]


def test_sanitize_rejects_unknown_or_unsafe_node():
    value = _graph_material()
    value["nodes"][2]["bl_idname"] = "ShaderNodeScript"
    assert material.sanitize(value) is None


def test_sanitize_rejects_out_of_range_link():
    value = _graph_material()
    value["links"][0][0] = 9
    assert material.sanitize(value) is None


def test_sanitize_rejects_bad_image_hash():
    value = _graph_material()
    value["nodes"][2]["image_hash"] = "not-a-hash"
    assert material.sanitize(value) is None


def test_sanitize_graph_without_nodes_is_basic():
    data = material.sanitize({"nodes": [], "links": []})
    assert data is not None
    assert "nodes" not in data

