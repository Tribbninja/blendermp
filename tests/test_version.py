import subprocess
import sys
from pathlib import Path

from core.version import compatible, parts

ROOT = Path(__file__).resolve().parents[1]


def test_versions_are_consistent():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_version_compatibility_rules():
    assert parts("0.1.6") == (0, 1, 6)
    assert compatible("0.1.6", "0.1.6")
    assert not compatible("0.1.6", "0.1.7")  # pre-1.0 requires exact versions
    assert not compatible("0.1.6", "0.1.5")
    assert not compatible("0.1.6", "0.2.0")
    assert compatible("1.2.3", "1.2.9")  # post-1.0 allows patch drift
    assert compatible("0.1.6", "")  # legacy peer without a version field


def test_version_check_catches_deliberate_drift(tmp_path):
    (tmp_path / "addon" / "blender_multiplayer" / "core").mkdir(parents=True)
    (tmp_path / "desktop" / "src-tauri").mkdir(parents=True)
    (tmp_path / "desktop" / "ui").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "version.json").write_text('{"version": "1.0.0"}', encoding="utf-8")
    (tmp_path / "addon" / "blender_multiplayer" / "blender_manifest.toml").write_text(
        'version = "1.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "addon" / "blender_multiplayer" / "core" / "version.py").write_text(
        'ADDON_VERSION = "1.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "addon" / "blender_multiplayer" / "__init__.py").write_text(
        'bl_info = {"version": (1, 0, 0)}\n', encoding="utf-8"
    )
    (tmp_path / "desktop" / "ui" / "app.js").write_text(
        'const APP_VERSION = "1.0.0";\n', encoding="utf-8"
    )
    (tmp_path / "desktop" / "ui" / "index.html").write_text("<span>v1.0.0</span>\n", encoding="utf-8")
    (tmp_path / "desktop" / "src-tauri" / "tauri.conf.json").write_text(
        '{"version": "1.0.0"}', encoding="utf-8"
    )
    (tmp_path / "scripts" / "installer.nsi").write_text('!define VERSION "1.0.0"\n', encoding="utf-8")
    (tmp_path / "desktop" / "src-tauri" / "Cargo.toml").write_text('version = "0.1.6"\n', encoding="utf-8")

    ok = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr

    (tmp_path / "addon" / "blender_multiplayer" / "blender_manifest.toml").write_text(
        'version = "1.0.1"\n', encoding="utf-8"
    )
    drift = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert drift.returncode != 0
    assert "DRIFT" in drift.stdout
