"""Minimal material payloads.

This covers the common Principled BSDF controls (base colour, metallic,
roughness, emission) and the material viewport colour. Full node-graph
replication is a later milestone; arbitrary node trees are rejected here.
"""

from __future__ import annotations

import math


def _color(value, size: int):
    if not isinstance(value, (list, tuple)) or len(value) < size:
        return None
    result = []
    for component in value[:size]:
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return None
        result.append(number)
    return result


def _number(value, low: float, high: float):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def sanitize(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name", "Material")
    if not isinstance(name, str):
        return None
    name = name.strip()[:64] or "Material"
    base = _color(value.get("base_color", [0.8, 0.8, 0.8, 1.0]), 4)
    emission = _color(value.get("emission_color", [0.0, 0.0, 0.0]), 3)
    metallic = _number(value.get("metallic", 0.0), 0.0, 1.0)
    roughness = _number(value.get("roughness", 0.5), 0.0, 1.0)
    strength = _number(value.get("emission_strength", 0.0), 0.0, 100.0)
    if base is None or emission is None or metallic is None or roughness is None or strength is None:
        return None
    return {
        "name": name,
        "base_color": base,
        "metallic": metallic,
        "roughness": roughness,
        "emission_color": emission,
        "emission_strength": strength,
    }
