"""Offline tests for the batch backtest — no Kite, synthetic results.

The module exists to counter selection bias, so most of these test the
breadth statistics and the verdict rather than the ranking.

Run:  python -m pytest tests/test_batch.py
"""
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "batch.db"
config.DB_PATH = _DB

from app import batch, db  # noqa: E402

db.init()


def result(expectancy, trades=40, dd=-12.0, pf=1.4):
    """A minimal backtest.run() return shaped like the real one."""
    return {"stats": {"expectancy_pct": expectancy, "trades": trades,
                      "open_trades": 0, "win_rate_pct": 55.0,
                      "profit_factor": pf,
                      "total_return_pct": (None if expectancy is None
                                           else expectancy * trades),
                      "cagr_pct": 8.0, "max_drawdown_pct": dd, "sharpe": 0.6,
                      "exposure_pct": 50.0, "vs_buyhold_pct": 2.0},
            "bars": 500}


def fake(mapping, fail=()):
    def run(symbol, strategy, **kw):
        if symbol in fail:
            raise ValueError(f"not enough candles for {symbol}")
        return result(mapping[symbol])
    return mock.patch.object(batch.backtest, "run", run)


def run_batch(mapping, fail=(), **kw):
    with fake(mapping, fail):
        return batch.run("ema_cross", symbols=list(mapping) + list(fail), **kw)


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    yield


# ----------------------------------------------------------------- basics
def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        batch.run("no_such_strategy", symbols=["BEL"])


def test_no_symbols_is_rejected():
    with mock.patch.object(batch.watchlist, "items", lambda: []):
        with pytest.raises(ValueError, match="no symbols"):
            batch.run("ema_cross")


def test_defaults_to_the_watchlist():
    with mock.patch.object(batch.watchlist, "items",
                           lambda: [{"tradingsymbol": "BEL"},
                                    {"tradingsymbol": "HAL"}]), \
         fake({"BEL": 1.0, "HAL": 2.0}):
        r = batch.run("ema_cross")
    assert {x["symbol"] for x in r["rows"]} == {"BEL", "HAL"}


def test_one_failing_symbol_does_not_stop_the_batch():
    r = run_batch({"A": 1.0, "B": 2.0}, fail=("BAD",))
    assert len(r["rows"]) == 2
    assert r["errors"][0]["symbol"] == "BAD"
    assert r["requested"] == 3


def test_failures_are_reported_not_dropped():
    """A table showing 2 of 3 without saying so overstates its own breadth."""
    r = run_batch({"A": 1.0}, fail=("X", "Y"))
    assert len(r["errors"]) == 2
    assert r["requested"] == 3 and r["aggregate"]["scored"] == 1


def test_systemexit_from_a_missing_session_is_caught():
    def boom(symbol, strategy, **kw):
        raise SystemExit("Missing config: KITE_API_KEY")

    with mock.patch.object(batch.backtest, "run", boom):
        r = batch.run("ema_cross", symbols=["BEL"])
    assert r["rows"] == [] and len(r["errors"]) == 1


# ----------------------------------------------------------------- sorting
def test_default_sort_is_by_expectancy_descending():
    r = run_batch({"LOW": 0.5, "HIGH": 3.0, "MID": 1.5})
    assert [x["symbol"] for x in r["rows"]] == ["HIGH", "MID", "LOW"]


def test_unscored_symbols_sort_last():
    """A symbol with no completed trades hasn't earned either end."""
    def run(symbol, strategy, **kw):
        return result(None if symbol == "NONE" else 2.0, trades=0
                      if symbol == "NONE" else 40)

    with mock.patch.object(batch.backtest, "run", run):
        r = batch.run("ema_cross", symbols=["NONE", "A", "B"])
    assert r["rows"][-1]["symbol"] == "NONE"


def test_alternate_sorts():
    r = run_batch({"A": 1.0, "B": 2.0}, sort="symbol")
    assert [x["symbol"] for x in r["rows"]] == ["A", "B"]


def test_unknown_sort_falls_back_to_expectancy():
    r = run_batch({"A": 1.0, "B": 2.0}, sort="nonsense")
    assert r["rows"][0]["symbol"] == "B"


# ------------------------------------------------------------- aggregate
def test_median_not_mean_is_the_headline():
    """One runaway symbol must not carry the sample — that's the whole point."""
    vals = {"A": 0.1, "B": 0.1, "C": 0.1, "D": 0.1, "E": 50.0}
    a = run_batch(vals)["aggregate"]
    assert a["median_expectancy_pct"] == 0.1
    assert a["mean_expectancy_pct"] > 5          # the mean is fooled
    assert a["best_expectancy_pct"] == 50.0


def test_profitable_share_counted():
    a = run_batch({"A": 1.0, "B": 1.0, "C": -1.0, "D": -1.0})["aggregate"]
    assert a["profitable"] == 2 and a["profitable_pct"] == 50.0


def test_empty_aggregate_has_the_full_shape():
    """Callers index into this dict; a missing key is a 500 on the page."""
    a = batch.aggregate([])
    for k in ("symbols", "scored", "median_expectancy_pct", "best_symbol",
              "profitable", "profitable_pct", "outlier_ratio", "lone_winner",
              "median_max_dd_pct", "median_trades", "total_trades"):
        assert k in a, k


# --------------------------------------------------------------- verdict
def test_broad_edge_reads_broad():
    v = run_batch({c: 1.5 for c in "ABCDEFGH"})["verdict"]
    assert v["read"] == "broad" and "across the sample" in v["detail"]


def test_narrow_edge_reads_narrow():
    vals = {"A": 5.0, "B": 4.0, "C": 3.0, "D": 0.2,
            "E": -1.0, "F": -1.0, "G": -1.0, "H": -1.0, "I": -1.0}
    v = run_batch(vals)["verdict"]
    assert v["read"] in ("narrow", "coin-flip")


def test_losing_strategy_reads_negative():
    v = run_batch({c: -1.5 for c in "ABCDEFGH"})["verdict"]
    assert v["read"] == "negative"


def test_coin_flip_reads_as_no_edge():
    vals = {"A": 1.0, "B": 1.0, "C": -1.2, "D": -1.2, "E": -0.1}
    v = run_batch(vals)["verdict"]
    assert v["read"] == "coin-flip" and "no edge" in v["detail"]


def test_lone_winner_is_called_out():
    """The exact trap: one profitable name at the top of a losing table."""
    vals = {"LUCKY": 40.0, **{c: -1.0 for c in "ABCDEFG"}}
    r = run_batch(vals)
    assert r["aggregate"]["lone_winner"] is True
    assert any("only profitable name" in n for n in r["verdict"]["notes"])
    assert r["rows"][0]["symbol"] == "LUCKY"      # still ranked first
    assert r["verdict"]["read"] == "negative"     # but the verdict disagrees


def test_outlier_ratio_flagged_when_median_is_positive():
    vals = {"BIG": 12.0, **{c: 1.0 for c in "ABCDEFG"}}
    r = run_batch(vals)
    assert r["aggregate"]["outlier_ratio"] == 12.0
    assert any("outlier" in n for n in r["verdict"]["notes"])


def test_small_sample_is_flagged():
    v = run_batch({"A": 2.0, "B": 2.0})["verdict"]
    assert any("too few" in n for n in v["notes"])


def test_thin_trade_counts_are_flagged():
    def run(symbol, strategy, **kw):
        return result(2.0, trades=6)

    with mock.patch.object(batch.backtest, "run", run):
        v = batch.run("ema_cross", symbols=list("ABCDEFGH"))["verdict"]
    assert any("barely a number" in n for n in v["notes"])


def test_no_results_says_so():
    def run(symbol, strategy, **kw):
        return result(None, trades=0)

    with mock.patch.object(batch.backtest, "run", run):
        v = batch.run("ema_cross", symbols=["A", "B"])["verdict"]
    assert v["read"] == "no result"


# ---------------------------------------------------------------- render
def test_as_text_leads_with_breadth_not_the_ranking():
    """Order matters: the verdict must appear before any symbol row."""
    vals = {"LUCKY": 40.0, **{c: -1.0 for c in "ABCDEFG"}}
    out = batch.as_text(run_batch(vals))
    assert out.index("NEGATIVE") < out.index("LUCKY")


def test_as_text_is_phone_sized():
    out = batch.as_text(run_batch({c: 1.0 for c in "ABCDEFGHIJKLMNO"}), limit=6)
    assert len(out.splitlines()) < 20
    assert "+ 9 more" in out


def test_caveat_is_present_and_names_the_risk():
    assert "curve-fitting" in batch.CAVEAT and "median" in batch.CAVEAT


# ------------------------------------------------------------------- API
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

client = TestClient(server.app)


def test_batch_route_returns_rows_and_breadth():
    with fake({"A": 2.0, "B": -1.0}):
        r = client.get("/api/backtest/batch",
                       params={"strategy": "ema_cross", "symbols": "A,B"})
    assert r.status_code == 200
    d = r.json()
    assert len(d["rows"]) == 2
    assert "aggregate" in d and "verdict" in d and "caveat" in d


def test_batch_route_rejects_unknown_strategy():
    r = client.get("/api/backtest/batch", params={"strategy": "nope"})
    assert r.status_code == 400


def test_batch_route_rejects_bad_params_json():
    r = client.get("/api/backtest/batch",
                   params={"strategy": "ema_cross", "symbols": "A",
                           "params": "not json"})
    assert r.status_code == 400


def test_batch_route_passes_risk_overlay_through():
    seen = {}

    def spy(strategy, **kw):
        seen.update(kw)
        return {"strategy": strategy, "name": "x", "params": {}, "rows": [],
                "errors": [], "aggregate": batch.aggregate([]),
                "verdict": {"read": "no result", "detail": ""},
                "requested": 0, "interval": "day", "days": 730,
                "cost_bps": 10.0, "risk": {}, "sort": "expectancy_pct"}

    with mock.patch.object(batch, "run", spy):
        client.get("/api/backtest/batch",
                   params={"strategy": "ema_cross", "symbols": "A",
                           "sl_pct": 3, "trail_mult": 2.5})
    assert seen["risk"] == {"sl_pct": 3.0, "trail_mult": 2.5}


def test_backtest_page_wires_the_batch_ui():
    """A defined-but-uncalled handler is how UI wiring silently ships dead."""
    html = client.get("/backtest").text
    for needle in ("id=\"runBatch\"", "runBatch()", "renderBatch(",
                   "/api/backtest/batch", "batchOut"):
        assert needle in html, f"backtest page missing {needle}"
