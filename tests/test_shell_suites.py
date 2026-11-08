"""Run tc's shell test suites (tests/shell/test-*.sh) and require each to pass."""
import subprocess
from pathlib import Path

import pytest

SUITES = sorted((Path(__file__).parent / "shell").glob("test-*.sh"))


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_shell_suite_passes(suite):
    r = subprocess.run(["bash", str(suite)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
