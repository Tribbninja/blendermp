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
