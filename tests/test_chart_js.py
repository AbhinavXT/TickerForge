"""Run the chart's JavaScript through node.

The Supertrend rendering bug — two full-width lines instead of one alternating
line — was invisible to every Python test, because it lived entirely in the
browser. These extract the relevant functions from index.html and exercise
them directly, so the geometry is asserted rather than eyeballed.

Skips cleanly where node isn't installed.

Run:  python -m pytest tests/test_chart_js.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent / "js"
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def run_js(body: str) -> str:
    src = subprocess.run([sys.executable, str(HERE / "extract.py")],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    script = src.stdout + "\n" + (HERE / body).read_text()
    tmp = ROOT / f".{body}.run.js"
    tmp.write_text(script)
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           cwd=ROOT)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    return r.stdout


def test_supertrend_renders_as_one_alternating_line():
    out = run_js("supertrend.body.js")
    assert "FAIL" not in out, out


def test_chart_event_markers():
    out = run_js("chart_events.body.js")
    assert "FAIL" not in out, out


def test_index_html_is_valid_javascript():
    """Catches a syntax error anywhere in the page — which otherwise blanks the
    whole chart with nothing but a console message."""
    import re
    html = (ROOT / "app" / "web" / "index.html").read_text()
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    tmp = ROOT / ".index.check.js"
    tmp.write_text(js)
    try:
        r = subprocess.run(["node", "--check", str(tmp)],
                           capture_output=True, text=True)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr


def test_supertrend_renders_as_one_coloured_series():
    """The rendering half of the same bug.

    supertrend() producing correctly gapped arrays was never sufficient — the
    two lines appeared downstream, where two series each relied on whitespace
    to stay silent across the other trend's bars. This asserts the shape
    I_super actually hands to setData.
    """
    out = run_js("supertrend_render.body.js")
    assert "FAIL" not in out, out


def test_supertrend_split_series_are_gone():
    """Cheap guard against the two-series shape being reintroduced by hand."""
    html = (ROOT / "app" / "web" / "index.html").read_text()
    assert "Supertrend\u2191" not in html and "Supertrend\u2193" not in html, \
        "the split up/down Supertrend series are back"


def test_drawing_geometry():
    """Hit-testing and Fib ladders, asserted rather than eyeballed.

    A hit test that's subtly wrong doesn't throw — a line you can see becomes
    a line you can't grab, and the feature just feels broken.
    """
    out = run_js("drawings.body.js")
    assert "FAIL" not in out, out


def test_small_chart_wins():
    """Sparkline paths and index-to-100, the two pure helpers behind 4.3."""
    out = run_js("smallwins.body.js")
    assert "FAIL" not in out, out


def test_chart_type_and_timeframe_transforms():
    """Heikin Ashi and weekly/monthly resampling.

    These turn candles into different candles, which fails quietly: a
    resample that drops the last bar or stamps it at the end of its period
    still renders a plausible chart.
    """
    out = run_js("charttypes.body.js")
    assert "FAIL" not in out, out
