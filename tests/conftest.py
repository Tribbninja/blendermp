import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON_ENGINE = ROOT / "addon" / "blender_multiplayer"

for path in (str(ROOT), str(ADDON_ENGINE)):
    if path not in sys.path:
        sys.path.insert(0, path)
