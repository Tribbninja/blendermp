"""Object transform snapshots and application.

Transforms are validated again on receive (finite values, sane magnitude) even
though the host already checked them: never trust a frame enough to hand it to
``mathutils`` unchecked.
"""

from __future__ import annotations

import math

MAX_MAGNITUDE = 1e12


def normalize(values) -> list[float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 16:
        return None
    try:
        numbers = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and abs(v) <= MAX_MAGNITUDE for v in numbers):
        return None
    return numbers


def find(uid: str):
    import bpy

    for obj in bpy.data.objects:
        if obj.get("bmp_uuid") == uid:
            return obj
    return None


def apply(uid: str, values) -> bool:
    numbers = normalize(values)
    if numbers is None:
        return False

    import bpy
    from mathutils import Matrix

    obj = find(uid)
    if obj is None:
        return False
    try:
        obj.matrix_world = Matrix(
            (
                numbers[0:4],
                numbers[4:8],
                numbers[8:12],
                numbers[12:16],
            )
        )
    except (ValueError, TypeError):
        return False
    return True
