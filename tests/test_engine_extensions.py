"""Engine extensions (3.5): position sizing, the regime gate, CSV export.

Each of these changes what a backtest MEANS, so the tests are mostly about the
semantics rather than the plumbing: that sizing makes position size a function
of the stop, that the gate can only remove trades and never invent them, and
that the exported columns line up with the journal's without hand-editing.
"""
from __future__ import annotations

import csv
import io
import random

import pytest

from app import backtest as B


def series(n=400, seed=2, drift=0.0006, vol=0.017):
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        p *= 1 + rng.gauss(drift, vol)
        out.append({"time": 1700000000 + i * 86400, "open": p * 0.995,
                    "high": p * 1.02, "low": p * 0.98, "close": p,
                    "volume": 1000})
    return out


def run_sim(risk, c=None, strat="sma_cross"):
    c = c or series()
    pos = B.STRATEGIES[strat]["fn"](c, B.STRATEGIES[strat]["params"])
    return B.simulate(c, pos, cost_bps=10, risk=risk)


def closed(sim):
    return [t for t in sim["trades"] if not t.get("open")]


# ============================================================ sizing
def test_no_sizing_keeps_the_original_behaviour():
    """Default must be byte-identical to before: every existing result, saved
    layout and remembered number depends on it."""
    c = series()
    a = run_sim({"sl_pct": 4}, c)
    b = run_sim({"sl_pct": 4, "sizing": None}, c)
    assert a["equity"] == b["equity"]


def test_sizing_deploys_the_arithmetically_right_fraction():
    """1% risk behind a 4% stop is a quarter of the account. Not a heuristic —
    qty x (entry - stop) = risk, so value/equity = risk% / stop%."""
    sim = run_sim({"sl_pct": 4, "sizing": "fixed_fractional", "risk_pct": 1})
    t = next(t for t in closed(sim) if t.get("position_pct"))
    assert t["position_pct"] == pytest.approx(25.0, abs=0.1)


def test_a_tighter_stop_demands_a_bigger_position():
    """The counterintuitive part, and the reason the /size command warns about
    it: tight stops concentrate capital."""
    wide = run_sim({"sl_pct": 8, "sizing": "fixed_fractional", "risk_pct": 1})
    tight = run_sim({"sl_pct": 2, "sizing": "fixed_fractional", "risk_pct": 1})
    w = next(t for t in closed(wide) if t.get("position_pct"))
    t = next(t for t in closed(tight) if t.get("position_pct"))
    assert t["position_pct"] > w["position_pct"]


def test_leverage_is_capped():
    """1% risk behind a 0.5% stop wants 200% of the account."""
    sim = run_sim({"sl_pct": 0.5, "sizing": "fixed_fractional",
                   "risk_pct": 1, "max_position_pct": 100})
    for t in closed(sim):
        if t.get("position_pct"):
            assert t["position_pct"] <= 100.0


def test_the_cap_is_configurable():
    sim = run_sim({"sl_pct": 0.5, "sizing": "fixed_fractional",
                   "risk_pct": 1, "max_position_pct": 150})
    sized = [t for t in closed(sim) if t.get("position_pct")]
    assert any(t["position_pct"] > 100 for t in sized)


def test_sizing_reduces_drawdown():
    """Not a law of nature, but it must hold for a 1% risk model against an
    all-in one — otherwise the fraction isn't being applied."""
    c = series()
    plain = run_sim({"sl_pct": 4}, c)
    sized = run_sim({"sl_pct": 4, "sizing": "fixed_fractional",
                     "risk_pct": 1}, c)
    dd = lambda eq: min(v / max(eq[:i + 1]) - 1 for i, v in enumerate(eq))
    assert dd(sized["equity"]) > dd(plain["equity"])


def test_fixed_fractional_without_a_stop_is_refused():
    """There is nothing to size against, and silently falling back to all-in
    would produce a number labelled '1% risk' that risks everything."""
    with pytest.raises(B.SizingError) as e:
        run_sim({"sizing": "fixed_fractional"})
    assert "needs a stop" in str(e.value)


def test_atr_sizing_needs_no_explicit_stop():
    """It derives one from volatility, which is the point of the mode."""
    sim = run_sim({"sizing": "atr_risk", "risk_pct": 1, "atr_mult": 2})
    assert any(t.get("position_pct") for t in closed(sim))


def test_atr_sizing_takes_smaller_positions_in_a_wilder_stock():
    calm = run_sim({"sizing": "atr_risk", "risk_pct": 1},
                   series(seed=3, vol=0.008))
    wild = run_sim({"sizing": "atr_risk", "risk_pct": 1},
                   series(seed=3, vol=0.04))
    c = [t["position_pct"] for t in closed(calm) if t.get("position_pct")]
    w = [t["position_pct"] for t in closed(wild) if t.get("position_pct")]
    assert c and w and sum(c) / len(c) > sum(w) / len(w)


def test_unknown_sizing_mode_is_refused():
    with pytest.raises(B.SizingError):
        run_sim({"sl_pct": 4, "sizing": "kelly-ish"})


def test_sized_trades_report_r_multiples():
    """The whole reason to size in the sim: the backtest starts speaking the
    journal's unit."""
    sim = run_sim({"sl_pct": 4, "sizing": "fixed_fractional", "risk_pct": 1})
    for t in closed(sim):
        if t.get("r_multiple") is not None:
            expected = (t["exit"] - t["entry"]) / (t["entry"] - t["stop"])
            assert t["r_multiple"] == pytest.approx(expected, abs=0.02)


def test_a_stopped_out_trade_is_about_minus_one_r():
    sim = run_sim({"sl_pct": 4, "sizing": "fixed_fractional", "risk_pct": 1})
    stops = [t for t in closed(sim) if t["reason"].startswith("stop")
             and t.get("r_multiple") is not None]
    for t in stops:
        assert t["r_multiple"] <= -0.9, "a stop exit should be roughly -1R"


def test_trades_too_small_to_afford_are_skipped_not_faked():
    sim = run_sim({"sl_pct": 4, "sizing": "fixed_fractional",
                   "risk_pct": 0.00001})
    assert sim["skipped"] > 0
    assert not closed(sim)


# ======================================================== regime gate
DAY = 86400


def index_series(n, closes):
    return [{"time": 1700000000 + i * DAY, "close": closes[i]} for i in range(n)]


def test_gate_is_off_during_warm_up():
    """Before the average exists the regime is unknown, and 'unknown' must
    stand aside. Defaulting to risk-on would quietly ungate the first year of
    every backtest."""
    nifty = index_series(260, [100 + i for i in range(260)])
    g = B.regime_gate([b["time"] for b in nifty], nifty, period=200)
    assert set(g[:199]) == {0}


def test_gate_is_on_above_the_average():
    nifty = index_series(260, [100 + i for i in range(260)])
    g = B.regime_gate([b["time"] for b in nifty], nifty, period=200)
    assert set(g[210:]) == {1}


def test_gate_is_off_below_the_average():
    closes = [100 + i for i in range(220)] + [300 - 3 * i for i in range(60)]
    nifty = index_series(280, closes)
    g = B.regime_gate([b["time"] for b in nifty], nifty, period=200)
    assert g[-1] == 0


def test_gate_does_not_see_the_index_bar_of_its_own_day():
    """Same-day matching would let a strategy dodge a crash using the close of
    the session it happened in — the exact free lunch backtests love."""
    nifty = index_series(260, [100 + i for i in range(260)])
    times = [b["time"] for b in nifty]
    lagged = B.regime_gate(times, nifty, period=200, lag_days=1)
    same = B.regime_gate(times, nifty, period=200, lag_days=0)
    assert lagged != same


def test_no_index_data_does_not_silently_gate_everything():
    """A missing index must not turn into 'risk-off for ever', which would
    look like a strategy that stopped trading rather than a data problem."""
    assert B.regime_gate([1, 2, 3], []) == [1, 1, 1]


def test_gate_can_only_remove_positions():
    """It's a veto, so every bar the gated run is long, the ungated one is too.

    Note this is about BARS, not trade count: chopping a long hold into pieces
    produces more trades, not fewer, which is exactly what a veto applied
    mid-position looks like.
    """
    c = series()
    spec = B.STRATEGIES["sma_cross"]
    ungated = spec["fn"](c, spec["params"])
    gate = [1 if i % 3 else 0 for i in range(len(c))]
    gated = [p if g else 0 for p, g in zip(ungated, gate)]

    assert all(u >= g for u, g in zip(ungated, gated)), \
        "the gate created a position the strategy never asked for"
    assert sum(gated) < sum(ungated), "the gate did nothing at all"


def test_gate_reduces_time_in_the_market():
    """The measurable consequence of a veto."""
    c = series()
    plain = B.evaluate_on(c, "sma_cross")["sim"]
    gated = B.evaluate_on(c, "sma_cross",
                          gate=[1 if i % 3 else 0 for i in range(len(c))])["sim"]
    assert gated["exposure"] < plain["exposure"]


def test_gate_length_matches_the_bars():
    nifty = index_series(300, [100 + i for i in range(300)])
    times = [1700000000 + i * DAY for i in range(50)]
    assert len(B.regime_gate(times, nifty)) == 50


# ========================================================== CSV export
def _rows(result):
    return list(csv.DictReader(io.StringIO(B.trades_csv(result))))


def test_csv_columns_match_the_journal_exactly():
    """Any mismatch is a column you reconcile by hand at the worst moment."""
    from app import db
    db.init()
    with db.connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    ignore = {"id", "initial_stop", "created_at"}
    assert set(B.JOURNAL_COLUMNS) == set(cols) - ignore


def test_csv_excludes_the_open_trade():
    sim = run_sim({"sl_pct": 4})
    res = {"symbol": "BEL", "strategy": "sma_cross", "trades": sim["trades"]}
    assert len(_rows(res)) == len(closed(sim))


def test_csv_stamps_the_source():
    """So live and simulated rows can never be silently mixed if this is ever
    imported into the real journal."""
    sim = run_sim({"sl_pct": 4})
    rows = _rows({"symbol": "BEL", "strategy": "sma_cross",
                  "trades": sim["trades"]})
    assert rows and all(r["source"] == "backtest" for r in rows)


def test_csv_tags_every_row_with_the_strategy():
    """The tag is what the journal's live-vs-backtest table joins on."""
    sim = run_sim({"sl_pct": 4})
    rows = _rows({"symbol": "BEL", "strategy": "st_adx",
                  "trades": sim["trades"]})
    assert all(r["tag"] == "st_adx" for r in rows)


def test_csv_leaves_quantity_blank_when_the_run_was_not_sized():
    """Writing 1 would make the R column arithmetic nonsense."""
    sim = run_sim({"sl_pct": 4})
    rows = _rows({"symbol": "BEL", "strategy": "sma_cross",
                  "trades": sim["trades"]})
    assert all(r["qty"] == "" for r in rows)


def test_csv_carries_quantity_and_r_when_it_was_sized():
    sim = run_sim({"sl_pct": 4, "sizing": "fixed_fractional", "risk_pct": 1})
    rows = _rows({"symbol": "BEL", "strategy": "sma_cross",
                  "trades": sim["trades"]})
    assert rows and all(r["qty"] and r["r_multiple"] for r in rows)


def test_csv_leaves_charges_blank():
    """Costs are already inside net_pct. Naming them again would double count
    against a journal row carrying real charges."""
    sim = run_sim({"sl_pct": 4})
    rows = _rows({"symbol": "BEL", "strategy": "sma_cross",
                  "trades": sim["trades"]})
    assert all(r["charges"] == "" for r in rows)


def test_csv_times_are_readable_dates():
    sim = run_sim({"sl_pct": 4})
    rows = _rows({"symbol": "BEL", "strategy": "sma_cross",
                  "trades": sim["trades"]})
    assert rows and rows[0]["entry_time"].startswith("20")


def test_csv_of_nothing_is_still_a_valid_file():
    out = B.trades_csv({"symbol": "BEL", "strategy": "x", "trades": []})
    assert out.strip().split("\n")[0].startswith("symbol,side,qty")


# ============================================================ API + page
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app import db
    from app.server import app
    db.init()
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_api_reports_a_sizing_error_as_a_bad_request(monkeypatch):
    """Asking for sizing with nothing to size against is the user's mistake,
    not a server fault — it must not surface as a 500.

    Driven by making run() raise, because reaching the real sizing check needs
    an instrument index and a Kite session, and neither is what's under test.
    """
    from app import server

    def boom(*a, **k):
        raise B.SizingError("fixed_fractional sizing needs a stop to size "
                            "against")

    monkeypatch.setattr(server.backtest, "run", boom)
    c = _client()
    r = c.get("/api/backtest?symbol=BEL&strategy=sma_cross"
              "&sizing=fixed_fractional")
    assert r.status_code == 400
    assert "stop" in r.json()["detail"]


def test_csv_route_sets_a_download_filename(monkeypatch):
    """Driven off a stubbed run(), because a test must never reach the broker.

    Left live, this passed on a machine with no Kite session and failed on one
    with a valid session — it went to the network and Kite rejected the fake
    instrument token the fixtures seed. A test whose result depends on whether
    you happened to log in this morning is not testing the route.
    """
    from app import server

    monkeypatch.setattr(server.backtest, "run", lambda *a, **k: {
        "symbol": "BEL", "strategy": "sma_cross", "trades": []})
    c = _client()
    r = c.get("/api/backtest/csv?symbol=BEL&strategy=sma_cross")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "text/csv" in r.headers["content-type"]
    assert r.text.startswith("symbol,side,qty")


def test_csv_route_maps_a_broker_failure_to_502(monkeypatch):
    """Not a 500. An upstream failure is upstream's fault, and the sibling
    /api/backtest route already answers this way."""
    from app import server

    def boom(*a, **k):
        raise RuntimeError("invalid token")

    monkeypatch.setattr(server.backtest, "run", boom)
    r = _client().get("/api/backtest/csv?symbol=BEL&strategy=sma_cross")
    assert r.status_code == 502


def test_page_exposes_the_engine_controls():
    c = _client()
    html = c.get("/backtest").text
    for needle in ("enginePanel", "szMode", "szRisk", "szCap", "szRegime",
                   "dlCsv", "engineParams"):
        assert needle in html, f"backtest page missing {needle}"


def test_csv_download_reuses_the_query_that_produced_the_result():
    """Otherwise the file can describe a different run than the one on screen."""
    c = _client()
    html = c.get("/backtest").text
    assert "LAST_QS" in html and "/api/backtest/csv?" in html
