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


# ------------------------------------------------------------------ the shell
# Pages on the shared shell. Grows one page per migration commit; once every
# page is on it this becomes PAGES minus the chart (which has its own toolbar).
SHELL_PAGES = {"board.html", "news.html", "actions.html", "fundamentals.html", "results.html", "metrics.html"}
ROUTE = {"index.html": "/"}


def href_of(page: str) -> str:
    return ROUTE.get(page, "/" + page.removesuffix(".html"))


def ui_nav_hrefs() -> list[str]:
    import re
    src = (WEB / "ui.js").read_text()
    return re.findall(r'\["\w+", "[^"]+", "(/[a-z]*)"\]', src)


def test_ui_js_is_valid_javascript():
    r = subprocess.run(["node", "--check", str(WEB / "ui.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("page", sorted(SHELL_PAGES))
def test_shell_mounts(desktop, page):
    p = desktop[page]["probes"]
    assert p["hasRail"] and p["hasTopbar"] and p["hasTabbar"], p
    assert p["activeRail"] == href_of(page), p
    assert p["hasThemeToggle"]
    assert p["railLinks"] == ui_nav_hrefs()
    assert p["iconButtonsWithoutLabel"] == 0, "icon button without aria-label"


@pytest.mark.parametrize("page", sorted(SHELL_PAGES))
def test_static_nav_matches_the_shell(page):
    """The static links are the no-JS fallback and what the route tests read;
    they must not drift from ui.js's NAV."""
    import re
    html = (WEB / page).read_text()
    block = html[html.index('<div id="nav"'):]
    block = block[:block.index("</div>")]
    assert re.findall(r'href="([^"]+)"', block) == ui_nav_hrefs()
    assert 'id="status"' in block and "TF.shell()" in html
    assert '<script src="/ui.js"></script>' in html.split("</head>")[0]


def test_phone_gets_five_tabs_and_a_more_sheet():
    [r] = smoke("board.html", width=360, more=True)
    p = r["probes"]
    assert p["tabbarSlots"] == ["board", "chart", "desk", "results", "more"]
    assert p["moreOpen"]
    assert set(p["moreLinks"]) | {"/board", "/", "/desk", "/results"} == set(ui_nav_hrefs())


def test_theme_toggle_flips_persists_and_announces():
    [r] = smoke("board.html", toggle=True)
    p = r["probes"]
    assert p["theme"] == "dark"
    assert p["afterToggle"] == {"theme": "light", "stored": "light", "event": "light"}


def test_theme_follows_the_os_until_chosen():
    [r] = smoke("board.html", prefers="light")
    assert r["probes"]["theme"] == "light"


def test_stored_theme_wins_over_the_os():
    [r] = smoke("board.html", prefers="light", theme="dark")
    assert r["probes"]["theme"] == "dark"
