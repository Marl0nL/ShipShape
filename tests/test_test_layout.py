"""The test suite collects the same whatever order its files are given in.

Both ship/tests and fleetledger/tests have a conftest.py, so `import conftest` from a
test file means whichever one Python loaded first. Helpers live in uniquely named
modules instead (ship_harness.py, fl_harness.py).
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROM_CONFTEST = re.compile(r"^\s*(from conftest import|import conftest)\b", re.M)


def test_no_test_imports_from_conftest():
    offenders = [
        str(path.relative_to(ROOT))
        for folder in ("tests", "ship/tests", "fleetledger/tests")
        for path in sorted((ROOT / folder).rglob("*.py"))
        if "node_modules" not in path.parts and FROM_CONFTEST.search(path.read_text("utf-8"))
    ]
    assert offenders == []


def collect(*files: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *files],
        cwd=ROOT, capture_output=True, text=True, timeout=120)


def test_a_fleetledger_file_before_a_ship_file_collects():
    files = ("fleetledger/tests/test_ca.py", "ship/tests/test_ship_lib.py")
    forward, backward = collect(*files), collect(*reversed(files))
    assert forward.returncode == 0, forward.stdout + forward.stderr
    assert backward.returncode == 0, backward.stdout + backward.stderr
    count = re.compile(r"(\d+) tests? collected")
    assert count.search(forward.stdout).group(1) == count.search(backward.stdout).group(1)
