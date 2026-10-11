"""Offline tests for the parameter sweep — no Kite, synthetic candles.

The module exists to tell a plateau from a spike, so most of these test the
shape analysis rather than the arithmetic.

Run:  python -m pytest tests/test_heatmap.py
"""
import random
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "hm.db"
config.DB_PATH = _DB

from app import backtest, db, heatmap  # noqa: E402

db.init()

INST = {"instrument_token": 1, "tradingsymbol": "BEL", "name": "Bharat Elec"}


def series(n=700, drift=0.0008, seed=3):
    rnd = random.Random(seed)
    c, out = 100.0, []
    for i in range(n):
        c *= 1 + drift + rnd.gauss(0, 0.011)
        w = c * 0.007
        out.append({"time": i * 86400, "open": c, "high": c + w, "low": c - w,
                    "close": c, "volume": 1000})
    return out


def offline(bars=None):
    bars = bars if bars is not None else series()
    return mock.patch.multiple(
        heatmap,
        instruments=mock.MagicMock(**{"lookup.return_value": INST}),
        kite_auth=mock.MagicMock(**{"get_kite.return_value": object()}),
        candles=mock.MagicMock(**{"ensure.return_value": None,
                                  "get.return_value": bars}))


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    yield


# ---------------------------------------------------------------- inputs
def test_a_list_is_always_literal_values():
    """The bug this guards: a three-number list used to be read as a RANGE,
    so asking for exactly 5, 10 and 15 silently produced one cell."""
    assert heatmap._values([5, 10, 15]) == [5, 10, 15]
    assert heatmap._values([5, 15, 5]) == [5, 15, 5]


def test_a_string_is_a_range():
    assert heatmap._values("5:15:5") == [5, 10, 15]


def test_float_range_stays_float():
    assert heatmap._values("1:2:0.5") == [1.0, 1.5, 2.0]


def test_zero_step_is_rejected():
    with pytest.raises(ValueError, match="step must be positive"):
        heatmap._values("1:10:0")


def test_backwards_range_is_rejected():
    with pytest.raises(ValueError, match="stop is below start"):
        heatmap._values("30:5:5")


def test_malformed_range_is_rejected():
    with pytest.raises(ValueError, match="bad range"):
        heatmap._values("5:10")
    with pytest.raises(ValueError, match="bad range"):
        heatmap._values("a:b:c")


def test_unknown_strategy_rejected():
    with offline():
        with pytest.raises(ValueError, match="unknown strategy"):
            heatmap.sweep("BEL", "nope", "fast", [5, 10])


def test_unknown_metric_rejected():
    with offline():
        with pytest.raises(ValueError, match="unknown metric"):
            heatmap.sweep("BEL", "ema_cross", "fast", [5, 10], metric="vibes")


def test_unknown_parameter_names_the_real_ones():
    """A typo shouldn't produce a silent all-defaults grid."""
    with offline():
        with pytest.raises(ValueError, match="has no parameter"):
            heatmap.sweep("BEL", "ema_cross", "speed", [5, 10])


def test_cell_limit_enforced():
    with offline():
        with pytest.raises(ValueError, match="exceeds the"):
            heatmap.sweep("BEL", "ema_cross", "fast", list(range(1, 30)),
                          "slow", list(range(30, 60)))


# ------------------------------------------------------------ efficiency
def test_candles_fetched_once_for_the_whole_grid():
    """The reason this module exists rather than looping backtest.run: a 4x3
    sweep would otherwise be 12 identical fetches against a rate-limited API."""
    gets = []
    cm = mock.MagicMock()
    cm.get.side_effect = lambda *a, **k: (gets.append(1) or series())
    cm.ensure.return_value = None
    with mock.patch.multiple(
            heatmap,
            instruments=mock.MagicMock(**{"lookup.return_value": INST}),
            kite_auth=mock.MagicMock(**{"get_kite.return_value": object()}),
            candles=cm):
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10, 15, 20],
                          "slow", [30, 50, 70])
    assert r["cells"] == 12
    assert len(gets) == 1


def test_cell_matches_a_single_run_on_the_same_candles():
    """A cell and a click-through single run must agree, or the grid lies."""
    bars = series()
    with offline(bars):
        r = heatmap.sweep("BEL", "ema_cross", "fast", [12], "slow", [26],
                          metric="sharpe")
    direct = backtest.evaluate_on(bars, "ema_cross",
                                  params={"fast": 12, "slow": 26})
    assert r["grid"][0][0]["value"] == direct["stats"]["sharpe"]


# ---------------------------------------------------------------- shape
def grid_of(values):
    """Build a grid directly, to test the analysis without a strategy."""
    return [[{"x": ci, "y": ri, "value": v, "trades": 50, "thin": False}
             for ci, v in enumerate(row)] for ri, row in enumerate(values)]


def test_plateau_is_recognised():
    g = grid_of([[1.0, 1.05, 1.0],
                 [1.05, 1.10, 1.05],
                 [1.0, 1.05, 1.0]])
    a = heatmap.analyse(g, "sharpe")
    assert a["neighbour_ratio"] > 0.9
    assert heatmap.verdict(a)["read"] == "plateau"


def test_sharp_peak_is_recognised():
    g = grid_of([[0.1, 0.1, 0.1],
                 [0.1, 3.0, 0.1],
                 [0.1, 0.1, 0.1]])
    a = heatmap.analyse(g, "sharpe")
    assert a["neighbour_ratio"] < 0.2
    v = heatmap.verdict(a)
    assert v["read"] == "sharp peak" and "curve-fitting" in v["detail"]


def test_ridge_sits_between_the_two():
    g = grid_of([[0.4, 0.6, 0.4],
                 [0.6, 1.0, 0.6],
                 [0.4, 0.6, 0.4]])
    assert heatmap.verdict(heatmap.analyse(g, "sharpe"))["read"] == "ridge"


def test_all_negative_says_nothing_works():
    g = grid_of([[-0.5, -0.6], [-0.7, -0.8]])
    v = heatmap.verdict(heatmap.analyse(g, "sharpe"))
    assert v["read"] == "nothing works"


def test_edge_peak_is_flagged():
    """A best cell in a corner has too few neighbours to judge."""
    g = [[{"x": 0, "y": 0, "value": 5.0, "trades": 50, "thin": False},
          {"x": 1, "y": 0, "value": 0.1, "trades": 50, "thin": False}]]
    v = heatmap.verdict(heatmap.analyse(g, "sharpe"))
    assert any("edge of the grid" in n for n in v["notes"])


def test_thin_cells_are_flagged():
    g = [[{"x": 0, "y": 0, "value": 2.0, "trades": 3, "thin": True},
          {"x": 1, "y": 0, "value": 1.0, "trades": 4, "thin": True}]]
    v = heatmap.verdict(heatmap.analyse(g, "sharpe"))
    assert any("noise" in n for n in v["notes"])
    assert any("very little evidence" in n for n in v["notes"])


def test_empty_analysis_has_the_full_shape():
    a = heatmap.analyse([[{"x": 0, "y": 0, "value": None,
                           "trades": None, "thin": True}]], "sharpe")
    for k in ("scored", "best", "best_at", "neighbour_ratio", "plateau_cells",
              "plateau_pct", "positive_pct", "median", "spread", "thin_pct"):
        assert k in a, k
    assert heatmap.verdict(a)["read"] == "no result"


# ----------------------------------------------------------------- shape
def test_one_dimensional_sweep_renders_as_one_row():
    with offline():
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10, 15])
    assert len(r["grid"]) == 1 and len(r["grid"][0]) == 3
    assert r["y_param"] is None and r["y_values"] == []


def test_failed_cells_do_not_stop_the_sweep():
    real = backtest.evaluate_on          # capture BEFORE patching, or the
    calls = {"n": 0}                     # mock calls itself forever

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return real(*a, **k)

    with offline(), mock.patch.object(backtest, "evaluate_on", flaky):
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10, 15])
    assert len(r["errors"]) == 1
    assert r["grid"][0][1]["value"] is None
    assert r["grid"][0][0]["value"] is not None      # the others still ran


def test_systemexit_in_a_cell_is_caught():
    def boom(*a, **k):
        raise SystemExit("Missing config: KITE_API_KEY")

    with offline(), mock.patch.object(backtest, "evaluate_on", boom):
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10])
    assert len(r["errors"]) == 2


# ---------------------------------------------------------------- render
def test_as_text_leads_with_the_verdict():
    with offline():
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10, 15],
                          "slow", [30, 50])
    out = heatmap.as_text(r)
    assert out.index(r["verdict"]["read"].upper()) < out.index("low ")


def test_as_text_blank_means_missing_not_low():
    """A space for 'worst score' would read as a gap and invert the picture."""
    with offline():
        r = heatmap.sweep("BEL", "ema_cross", "fast", [5, 10])
    out = heatmap.as_text(r)
    assert "· = no result" in out
    assert " " not in "‹.:-=+*#@›"


def test_caveat_names_the_actual_advice():
    assert "middle of a broad region" in heatmap.CAVEAT


# ------------------------------------------------------------------- API
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

# The host guard only answers loopback; TestClient defaults to "testserver".
client = TestClient(server.app, base_url="http://127.0.0.1:5000")


def test_metrics_route():
    d = client.get("/api/backtest/metrics").json()
    assert any(m["key"] == "sharpe" for m in d["metrics"])


def test_heatmap_route():
    with offline():
        r = client.get("/api/backtest/heatmap",
                       params={"symbol": "BEL", "strategy": "ema_cross",
                               "x_param": "fast", "x_values": "5,10,15",
                               "y_param": "slow", "y_values": "30:70:20"})
    assert r.status_code == 200
    d = r.json()
    assert len(d["grid"]) == 3 and len(d["grid"][0]) == 3
    assert "verdict" in d and "caveat" in d


def test_heatmap_route_rejects_bad_values():
    r = client.get("/api/backtest/heatmap",
                   params={"symbol": "BEL", "strategy": "ema_cross",
                           "x_param": "fast", "x_values": "a,b,c"})
    assert r.status_code == 400


def test_heatmap_route_401s_without_a_session():
    """No session is the normal state before the morning login — the route
    must say 'log in', not 500."""
    with mock.patch.object(heatmap.instruments, "lookup", lambda *a, **k: INST), \
         mock.patch.object(heatmap.kite_auth, "get_kite",
                           side_effect=SystemExit("No valid Kite session")):
        r = client.get("/api/backtest/heatmap",
                       params={"symbol": "BEL", "strategy": "ema_cross",
                               "x_param": "fast", "x_values": "5,10"})
    assert r.status_code == 401


def test_backtest_page_wires_the_sweep_ui():
    html = client.get("/backtest").text
    for needle in ("hmSweepControls", "runHeatmap", "renderHeatmap",
                   "/api/backtest/heatmap", "hmOut", "table.hm"):
        assert needle in html, f"page missing {needle}"
