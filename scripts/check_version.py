"""Fail the build when a version string drifts from version.json.

Run directly (`python scripts/check_version.py`) or from pytest. Keeping this
check in CI is cheap and prevents the 0.1.5-vs-0.1.6 class of session mismatch.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _search(path: Path, pattern: str) -> str | None:
    match = re.search(pattern, _read(path), re.MULTILINE)
    return match.group(1) if match else None


def expected_version() -> str:
    data = json.loads(_read(ROOT / "version.json"))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("version.json must contain {'version': 'X.Y.Z'}")
    return version


def crate_version() -> str:
    data = json.loads(_read(ROOT / "version.json"))
    value = data.get("crate_version") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def collect() -> dict[str, str | None]:
    tauri = json.loads(_read(ROOT / "desktop" / "src-tauri" / "tauri.conf.json"))
    bl_info = re.search(
        r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)',
        _read(ROOT / "addon" / "blender_multiplayer" / "__init__.py"),
    )
    return {
        "blender_manifest.toml": _search(
            ROOT / "addon" / "blender_multiplayer" / "blender_manifest.toml",
            r'^version\s*=\s*"([^"]+)"',
        ),
        "core/version.py": _search(
            ROOT / "addon" / "blender_multiplayer" / "core" / "version.py",
            r'^ADDON_VERSION\s*=\s*"([^"]+)"',
        ),
        "bl_info": ".".join(bl_info.groups()) if bl_info else None,
        "desktop APP_VERSION": _search(
            ROOT / "desktop" / "ui" / "app.js",
            r'^const APP_VERSION\s*=\s*"([^"]+)"',
        ),
        "tauri.conf.json": str(tauri.get("version")) if isinstance(tauri, dict) else None,
        "installer.nsi": _search(
            ROOT / "scripts" / "installer.nsi",
            r'^!define VERSION\s+"([^"]+)"',
        ),
        "index.html": _search(
            ROOT / "desktop" / "ui" / "index.html",
            r"v(\d+\.\d+\.\d+)",
        ),
    }


def main() -> int:
    version = expected_version()
    values = collect()
    bad = {name: value for name, value in values.items() if value != version}
    crate = crate_version()
    cargo = _search(ROOT / "desktop" / "src-tauri" / "Cargo.toml", r'^version\s*=\s*"([^"]+)"')
    if crate and cargo != crate:
        bad["Cargo.toml (crate_version)"] = cargo
    if bad:
        print(f"version mismatch: version.json says {version}")
        for name, value in values.items():
            mark = "ok" if value == version else "DRIFT"
            print(f"  {mark:5} {name}: {value}")
        if crate:
            mark = "ok" if cargo == crate else "DRIFT"
            print(f"  {mark:5} Cargo.toml: {cargo} (crate_version {crate})")
        return 1
    print(f"versions OK: {version} (crate {crate or 'unchecked'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
