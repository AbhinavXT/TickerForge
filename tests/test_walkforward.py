"""Offline tests for walk-forward validation — no Kite, synthetic candles.

The whole point is that the optimiser never sees out-of-sample outcomes, so
most of these test that boundary rather than the arithmetic.

Run:  python -m pytest tests/test_walkforward.py
"""
import random
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "wf.db"
config.DB_PATH = _DB

from app import backtest, db, walkforward as wf  # noqa: E402

db.init()

INST = {"instrument_token": 1, "tradingsymbol": "BEL", "name": "Bharat Elec"}


def series(n=1500, drift=0.0, seed=7):
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
        wf,
        instruments=mock.MagicMock(**{"lookup.return_value": INST}),
        kite_auth=mock.MagicMock(**{"get_kite.return_value": object()}),
        candles=mock.MagicMock(**{"ensure.return_value": None,
                                  "get.return_value": bars}))


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    yield


# ------------------------------------------------------------------ folds
def test_rolling_windows_are_fixed_length():
    f = wf._folds(1000, 300, 100, "rolling")
    assert all(is_end - start == 300 for start, is_end, _ in f)


def test_anchored_windows_grow_from_the_start():
    f = wf._folds(1000, 300, 100, "anchored")
    assert all(start == 0 for start, _, _ in f)
    assert f[-1][1] > f[0][1]


def test_folds_do_not_overlap_out_of_sample():
    """Each out-of-sample window must be scored exactly once, or the stitched
    curve double-counts the same bars."""
    f = wf._folds(1000, 300, 100, "rolling")
    spans = [(is_end, oos_end) for _, is_end, oos_end in f]
    for (a1, a2), (b1, b2) in zip(spans, spans[1:]):
        assert a2 <= b1


def test_out_of_sample_always_follows_in_sample():
    for mode in ("rolling", "anchored"):
        for start, is_end, oos_end in wf._folds(1000, 300, 100, mode):
            assert start < is_end < oos_end


def test_not_enough_bars_produces_no_folds():
    assert wf._folds(200, 300, 100, "rolling") == []


# ------------------------------------------------------------------ guards
def test_short_in_sample_window_is_rejected():
    with offline():
        with pytest.raises(ValueError, match="too short to optimise"):
            wf.run("BEL", "ema_cross", None, is_bars=50, oos_bars=60)


def test_tiny_out_of_sample_window_is_rejected():
    with offline():
        with pytest.raises(ValueError, match="at least"):
            wf.run("BEL", "ema_cross", None, is_bars=300, oos_bars=5)


def test_unknown_strategy_metric_and_mode_rejected():
    with offline():
        for kw, msg in (({"strategy": "nope"}, "unknown strategy"),
                        ({"metric": "vibes"}, "unknown metric"),
                        ({"mode": "sideways"}, "rolling")):
            args = {"symbol": "BEL", "strategy": "ema_cross",
                    "param_grid": None, "is_bars": 300, "oos_bars": 60}
            args.update(kw)
            with pytest.raises(ValueError, match=msg):
                wf.run(**args)


def test_unknown_parameter_names_the_real_ones():
    with offline():
        with pytest.raises(ValueError, match="has no parameter"):
            wf.run("BEL", "ema_cross", {"speed": [5, 10]},
                   is_bars=300, oos_bars=60)


def test_too_short_history_explains_itself():
    with offline(series(200)):
        with pytest.raises(ValueError, match="isn't enough"):
            wf.run("BEL", "ema_cross", None, is_bars=300, oos_bars=60)


def test_combination_limit_enforced():
    with offline():
        with pytest.raises(ValueError, match="exceeds the"):
            wf.run("BEL", "ema_cross",
                   {"fast": list(range(2, 30)), "slow": list(range(30, 60))},
                   is_bars=300, oos_bars=60)


# ------------------------------------------------------- the honesty test
def test_optimiser_never_sees_out_of_sample_bars():
    """The single property everything else depends on. Every optimisation call
    must be on a candle slice that ends at or before the in-sample boundary."""
    bars = series()
    seen = []
    real = backtest.evaluate_on

    def spy(c, strategy, **kw):
        seen.append(len(c))
        return real(c, strategy, **kw)

    with offline(bars), mock.patch.object(backtest, "evaluate_on", spy):
        r = wf.run("BEL", "ema_cross", {"fast": [5, 10]},
                   is_bars=300, oos_bars=100)

    # For each fold: N optimisation calls of exactly is_bars, then one
    # evaluation of is_bars + oos_bars. Nothing longer during optimisation.
    for f in r["folds"]:
        assert f["is_bars"] == 300 and f["oos_bars"] == 100
    assert 300 in seen and 400 in seen
    assert not any(300 < n < 400 for n in seen), \
        "an optimisation ran on a slice extending into out-of-sample"


def test_indicators_are_warm_across_the_boundary():
    """A 200-bar SMA scored on a 60-bar out-of-sample slice would be undefined
    for every bar. Evaluating over IS+OOS is what avoids that."""
    bars = series()
    with offline(bars):
        r = wf.run("BEL", "price_sma", {"length": [200]},
                   is_bars=300, oos_bars=100)
    scored = [f for f in r["folds"] if f["oos_score"] is not None]
    assert scored, "no fold produced a result — indicators weren't warm"


def test_chosen_params_are_applied_unchanged_out_of_sample():
    bars = series()
    with offline(bars):
        r = wf.run("BEL", "ema_cross", {"fast": [5, 15]},
                   is_bars=300, oos_bars=100)
    for f in r["folds"]:
        if f["params"]:
            assert f["params"]["fast"] in (5, 15)


# --------------------------------------------------------------- outcomes
def test_random_walk_does_not_hold_up():
    """The negative control. If a coin flip 'holds up', the method is broken."""
    with offline(series(seed=7)):
        r = wf.run("BEL", "ema_cross", {"fast": "5:25:5", "slow": "30:70:20"},
                   is_bars=300, oos_bars=100)
    assert r["verdict"]["read"] in ("curve-fit", "degrades",
                                    "fails out of sample", "unclear")
    assert r["aggregate"]["efficiency"] is None or r["aggregate"]["efficiency"] < 0.6


def test_persistent_trend_holds_up():
    """The positive control — a real, persistent edge must survive."""
    with offline(series(drift=0.0012, seed=3)):
        r = wf.run("BEL", "ema_cross", {"fast": "5:25:5", "slow": "30:70:20"},
                   is_bars=300, oos_bars=100)
    assert r["verdict"]["read"] == "holds up"
    assert r["aggregate"]["oos_positive_pct"] >= 60


def test_no_grid_runs_plain_out_of_sample_testing():
    with offline():
        r = wf.run("BEL", "ema_cross", None, is_bars=300, oos_bars=100)
    assert r["combos"] == 1
    assert all(f["params"] == {} for f in r["folds"] if f["params"] is not None)


# ------------------------------------------------------------- aggregate
def test_efficiency_needs_a_positive_in_sample():
    """Dividing by a negative in-sample score yields a number that looks like a
    ratio and isn't one."""
    folds = [{"is_score": -0.5, "oos_score": -0.2, "params": {}},
             {"is_score": -0.3, "oos_score": 0.1, "params": {}}]
    a = wf.aggregate(folds, [], [{}], "sharpe", "day")
    assert a["efficiency"] is None


def test_efficiency_computed_when_in_sample_is_positive():
    folds = [{"is_score": 1.0, "oos_score": 0.5, "params": {}},
             {"is_score": 1.0, "oos_score": 0.5, "params": {}}]
    a = wf.aggregate(folds, [], [{}], "sharpe", "day")
    assert a["efficiency"] == 0.5


def test_stitched_curve_covers_every_out_of_sample_bar():
    with offline():
        r = wf.run("BEL", "ema_cross", None, is_bars=300, oos_bars=100)
    scored = [f for f in r["folds"] if f.get("oos_score") is not None]
    assert len(r["oos_equity"]) >= 100 * (len(scored) - 1)


def test_empty_aggregate_has_the_full_shape():
    a = wf.aggregate([], [], [], "sharpe", "day")
    for k in ("folds", "scored", "is_mean", "oos_mean", "efficiency",
              "oos_positive", "stability", "oos_bars"):
        assert k in a, k
    assert wf.verdict(a, 0)["read"] == "no result"


# ------------------------------------------------------------- stability
def test_stable_parameters_score_high():
    st = wf._stability([{"fast": 10}, {"fast": 10}, {"fast": 10}])
    assert st["read"] == "stable" and st["score"] == 1.0


def test_jumping_parameters_score_low():
    """A strategy whose best setting jumps around doesn't have one."""
    st = wf._stability([{"fast": 5}, {"fast": 30}, {"fast": 12}, {"fast": 25}])
    assert st["read"] in ("unstable", "drifting")
    assert st["per_param"]["fast"]["distinct"] == 4


def test_instability_is_called_out_in_the_verdict():
    a = wf.aggregate(
        [{"is_score": 1.0, "oos_score": 0.9, "params": {}}] * 4, [],
        [{"fast": 5}, {"fast": 40}, {"fast": 8}, {"fast": 35}], "sharpe", "day")
    v = wf.verdict(a, 4)
    assert any("no stable optimum" in n or "drift" in n for n in v["notes"])


def test_few_folds_are_flagged():
    a = wf.aggregate([{"is_score": 1.0, "oos_score": 0.9, "params": {}}], [],
                     [{}], "sharpe", "day")
    assert any("too few" in n for n in wf.verdict(a, 2)["notes"])


# ---------------------------------------------------------------- render
def test_as_text_leads_with_the_verdict():
    with offline():
        r = wf.run("BEL", "ema_cross", {"fast": [5, 15]},
                   is_bars=300, oos_bars=100)
    out = wf.as_text(r)
    assert out.index(r["verdict"]["read"].upper()) < out.index("  1.")


def test_caveat_names_the_actual_finding():
    assert "usually much worse" in wf.CAVEAT and "hindsight" in wf.CAVEAT


# ------------------------------------------------------------------- API
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

client = TestClient(server.app)


def test_walkforward_route():
    with offline():
        r = client.get("/api/backtest/walkforward",
                       params={"symbol": "BEL", "strategy": "ema_cross",
                               "grid": '{"fast":[5,15]}',
                               "is_bars": 300, "oos_bars": 100})
    assert r.status_code == 200
    d = r.json()
    assert d["folds"] and "verdict" in d and "caveat" in d


def test_walkforward_route_rejects_bad_grid():
    r = client.get("/api/backtest/walkforward",
                   params={"symbol": "BEL", "strategy": "ema_cross",
                           "grid": "not json"})
    assert r.status_code == 400


def test_walkforward_route_401s_without_a_session():
    with mock.patch.object(wf.instruments, "lookup", lambda *a, **k: INST), \
         mock.patch.object(wf.kite_auth, "get_kite",
                           side_effect=SystemExit("No valid Kite session")):
        r = client.get("/api/backtest/walkforward",
                       params={"symbol": "BEL", "strategy": "ema_cross"})
    assert r.status_code == 401


def test_backtest_page_wires_walkforward():
    html = client.get("/backtest").text
    for needle in ("runWalkForward", "renderWF", "wfGridControls",
                   "/api/backtest/walkforward", "wfOut"):
        assert needle in html, f"page missing {needle}"
