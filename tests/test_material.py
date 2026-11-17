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
