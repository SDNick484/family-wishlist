"""Runs the JavaScript unit tests (tests/frontend.test.mjs) so `pytest` covers the frontend too."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def node_major():
    node = shutil.which("node")
    if not node:
        return 0
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout  # e.g. v22.3.0
    return int(out.strip().lstrip("v").split(".")[0] or 0)


# In CI (which sets CI=true) a missing Node is a broken setup, so fail instead of skipping.
@pytest.mark.skipif(node_major() < 20 and not os.environ.get("CI"),
                    reason="needs Node.js 20+ for the built-in test runner")
def test_frontend_unit_tests():
    r = subprocess.run(["node", "--test", os.path.join(HERE, "frontend.test.mjs")],
                       capture_output=True, text=True, cwd=os.path.dirname(HERE))
    assert r.returncode == 0, r.stdout + r.stderr
