"""Every page, loaded in jsdom: no script errors, and the shared shell mounts.

The other JS tests extract single functions from index.html. This one loads
whole pages — inline scripts, `/ui.js`, the lot — with the network and the
chart library stubbed out (see tests/js/smoke.js), so a typo anywhere in a
page, or a shell that fails to mount, shows up here instead of as a blank tab.

Needs node and jsdom (`npm install` in tests/js); skips cleanly without them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "tests" / "js"
WEB = ROOT / "app" / "web"

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node not installed"),
    pytest.mark.skipif(not (JS / "node_modules" / "jsdom").exists(),
                       reason="jsdom not installed (npm install in tests/js)"),
]

PAGES = sorted(p.name for p in WEB.glob("*.html"))


def smoke(*files: str, **opts) -> list[dict]:
    flags = [f"--{k}={v}" if v is not True else f"--{k}" for k, v in opts.items()]
    r = subprocess.run(["node", str(JS / "smoke.js"), *flags, *files],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def desktop():
    return {r["file"]: r for r in smoke(*PAGES, width=1440)}


@pytest.mark.parametrize("page", PAGES)
def test_page_runs_without_script_errors(desktop, page):
    assert desktop[page]["errors"] == [], desktop[page]["errors"]
