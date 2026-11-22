import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDON_ENGINE = ROOT / "addon" / "blender_multiplayer"

for path in (str(ROOT), str(ADDON_ENGINE)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(autouse=True)
def _reset_compression_policy():
    """The compression flag is process-wide; reset it between tests."""
    from core import compression

    compression.set_enabled(True)
    yield
    compression.set_enabled(True)
