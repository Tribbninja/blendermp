from pathlib import Path

from core import accessibility


def test_okabe_ito_palette_is_complete():
    palette = accessibility.palette()
    assert len(palette) == 8
    assert all(len(color) == 4 for color in palette)
    assert all(accessibility.is_finite_color(color) for color in palette)


def test_every_palette_colour_has_a_readable_text_pair():
    for color in accessibility.palette():
        text = accessibility.best_text_color(color)
        assert accessibility.contrast_ratio(color, text) >= 4.5


def test_deuteranopia_projection_is_deterministic_and_bounded():
    first = accessibility.simulate_deuteranopia((0.9, 0.1, 0.2))
    second = accessibility.simulate_deuteranopia((0.9, 0.1, 0.2))
    assert first == second
    assert all(0.0 <= channel <= 1.0 for channel in first)
    assert accessibility.contrast_ratio(first, (0.03, 0.03, 0.04)) > 1.0


def test_accessibility_css_hooks_exist():
    root = Path(__file__).resolve().parents[1] / "desktop" / "ui" / "styles.css"
    text = root.read_text(encoding="utf-8")
    assert 'html[data-contrast="high"]' in text
    assert 'html[data-font="large"]' in text
    assert 'html[data-motion="reduced"]' in text
    assert "focus-visible" in text
