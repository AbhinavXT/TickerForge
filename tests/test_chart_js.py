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


def _obscan_cases():
    """Python's view of a few candle sets: a clean trend with a live OB, a
    range whose highs get swept, and one where the OB is traded through."""
    import math
    from app import board, indicators as ind

    def bar(t, o, c, hi=None, lo=None):
        return {"time": t * 300, "open": o, "close": c,
                "high": hi if hi is not None else max(o, c) + 0.2,
                "low": lo if lo is not None else min(o, c) - 0.2, "volume": 1000}

    def chop(n, base=100.0, t0=0, amp=0.8):
        out, prev = [], base
        for i in range(n):
            c = base + amp * math.sin((i + 1) * 2 * math.pi / 8)
            out.append(bar(t0 + i, prev, c, max(prev, c) + 0.2, min(prev, c) - 0.2))
            prev = c
        return out

    a = chop(30)
    a += [bar(30, 100.6, 100.0, 100.8, 99.7), bar(31, 100.0, 104.0, 104.2, 99.9)]
    a += [bar(32 + k, 104 + k * 0.3, 104.3 + k * 0.3) for k in range(6)]

    b = chop(30)
    b += [bar(30, 100.5, 100.4, 101.6, 100.2), bar(31, 100.4, 100.1),
          bar(32, 100.1, 99.6, 100.2, 98.6)]

    c = list(a) + [bar(38 + k, 105 - k * 1.2, 104.5 - k * 1.2) for k in range(6)]

    cases = []
    for cs, pdh, pdl in ((a, None, None), (b, 101.3, 99.0), (c, 106.0, 98.0)):
        atr = ind.atr(cs, board.ATR_LEN)
        disps = board.find_displacements(cs, atr)
        obs = []
        for d in disps:
            ob = board.order_block_for(cs, d)
            if ob:
                obs.append({**ob, "live": not board.is_mitigated(cs, ob)})
        atr_now = next((x for x in reversed(atr) if x), None)
        levels = board.liquidity_levels(cs, atr_now, pdh, pdl)
        traps = board.liquidity_traps(cs, levels)
        cases.append({"candles": cs, "atr": atr, "pdh": pdh, "pdl": pdl,
                      "py": {"disps": disps, "obs": obs, "levels": levels,
                             "traps": traps}})
    return cases


def test_chart_order_blocks_match_the_board(tmp_path, monkeypatch):
    import json
    fx = tmp_path / "obscan.json"
    fx.write_text(json.dumps({"cases": _obscan_cases()}))
    monkeypatch.setenv("OBSCAN_FIXTURE", str(fx))
    out = run_js("obscan.body.js")
    assert "FAIL" not in out, out
