"""Accessibility helpers shared by the addon and tests.

The Okabe-Ito palette is a widely used colour-blind-safe categorical palette.
It is kept in plain Python so the contrast checks can run without Blender.
"""

from __future__ import annotations

import math

# Okabe-Ito, as linear-ish sRGB values in 0..1.
OKABE_ITO = (
    (0.000, 0.447, 0.698),  # blue
    (0.902, 0.624, 0.000),  # orange
    (0.337, 0.706, 0.914),  # sky blue
    (0.000, 0.620, 0.451),  # bluish green
    (0.941, 0.894, 0.259),  # yellow
    (0.835, 0.369, 0.000),  # vermillion
    (0.800, 0.475, 0.655),  # reddish purple
    (0.000, 0.000, 0.000),  # black
)


def _linear(channel: float) -> float:
    channel = max(0.0, min(1.0, float(channel)))
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(color) -> float:
    values = [float(item) for item in color[:3]]
    if len(values) != 3:
        raise ValueError("colour needs three channels")
    red, green, blue = (_linear(value) for value in values)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second) -> float:
    a = luminance(first)
    b = luminance(second)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def simulate_deuteranopia(color):
    """Approximate deuteranopia (red-green) projection for previews/tests."""
    red, green, blue = [float(item) for item in color[:3]]
    return (
        0.625 * red + 0.375 * green,
        0.700 * red + 0.300 * green,
        0.300 * green + 0.700 * blue,
    )


def palette(colors=OKABE_ITO):
    return [tuple(float(channel) for channel in color[:3]) + (1.0,) for color in colors]


def best_text_color(background) -> tuple[float, float, float, float]:
    light = contrast_ratio(background, (1.0, 1.0, 1.0))
    dark = contrast_ratio(background, (0.03, 0.03, 0.04))
    return (1.0, 1.0, 1.0, 1.0) if light >= dark else (0.03, 0.03, 0.04, 1.0)


def is_finite_color(color) -> bool:
    return all(math.isfinite(float(value)) for value in color[:3])
