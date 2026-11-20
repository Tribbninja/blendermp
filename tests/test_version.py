import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_versions_are_consistent():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
