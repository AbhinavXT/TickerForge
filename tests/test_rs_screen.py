"""Relative strength (2.3) and the value-with-momentum screen (2.4).

RS is arithmetic over two aligned series, so most of this is exact. The parts
worth being careful about are the ones that fail silently: aligning by date
rather than position, skipping the recent month in the 12-month window, and
ranking None as None rather than zero.
"""
from __future__ import annotations

import pytest

from app import rs, screen

DAY = 86400
T0 = 1700000000


def series(rates, start=100.0, skip=()):
    """Compounding closes. `skip` omits bar indices, to model a suspension."""
    out, p = [], start
    for i, r in enumerate(rates):
        p *= 1 + r
        if i in skip:
            continue
        out.append({"time": T0 + i * DAY, "close": p})
    return out


def flat(n, rate, start=100.0, skip=()):
    return series([rate] * n, start, skip)


# ================================================================ alignment
def test_series_are_aligned_by_date_not_position():
    """A suspension in one series would otherwise shift every comparison by a
    day — and every resulting number still looks plausible."""
    idx = flat(300, 0.001)
    stock = flat(300, 0.001, skip=(50, 51, 52))
    s, i = rs._aligned(stock, idx)
    assert len(s) == len(i) == 297
    # the survivors must still be same-day pairs
    times = [b["time"] for b in stock]
    assert times[60] == (T0 + 63 * DAY)


def test_alignment_drops_days_the_index_did_not_trade():
    idx = flat(300, 0.001, skip=(10,))
    stock = flat(300, 0.001)
    s, i = rs._aligned(stock, idx)
    assert len(s) == 299


def test_no_overlap_scores_nothing():
    a = [{"time": 1, "close": 10}]
    b = [{"time": 2, "close": 10}]
    assert rs.score_one(a, b)["score"] is None


# ============================================================ relative return
def test_outperformance_is_positive():
    idx = flat(300, 0.0003)
    stock = flat(300, 0.0010)
    out = rs.score_one(stock, idx)
    assert out["r3m"] > 0 and out["r6m"] > 0 and out["r12m"] > 0
    assert out["score"] > 0


def test_underperformance_is_negative():
    idx = flat(300, 0.0010)
    stock = flat(300, 0.0003)
    assert rs.score_one(stock, idx)["score"] < 0


def test_tracking_the_index_scores_about_zero():
    idx = flat(300, 0.0006)
    stock = flat(300, 0.0006, start=250.0)     # different price level
    assert abs(rs.score_one(stock, idx)["score"]) < 1e-6


def test_price_level_is_irrelevant():
    """A ₹40 stock and a ₹3,000 stock with identical shape must score the same."""
    idx = flat(300, 0.0004)
    cheap = rs.score_one(flat(300, 0.0009, start=40), idx)["score"]
    dear = rs.score_one(flat(300, 0.0009, start=3000), idx)["score"]
    assert cheap == pytest.approx(dear, abs=1e-9)


def test_relative_return_compounds_rather_than_subtracts():
    """Up 50% against an index up 20% is +25% relative, not +30%."""
    stock = [1.0, 1.5]
    index = [1.0, 1.2]
    assert rs._rel(stock, index, 2) == pytest.approx(0.25, abs=1e-9)


def test_a_window_longer_than_the_history_is_none():
    assert rs._rel([1.0, 1.1], [1.0, 1.0], 100) is None


# =========================================================== the 12-1 window
def test_twelve_month_window_skips_the_recent_month():
    """Short-horizon reversal is why the momentum factor is defined 12-minus-1.
    Including the last month contaminates a year of trend with a few weeks of
    mean reversion.

    Built as a year of steady outperformance ending in a sharp recent slump:
    the 3-month figure should see the slump and the 12-month one should not.
    """
    rates = [0.002] * 279 + [-0.03] * 21
    stock = series(rates)
    idx = flat(300, 0.0)
    out = rs.score_one(stock, idx)
    assert out["r3m"] < out["r12m"], \
        "the 12-month window is picking up the last month's collapse"


def test_the_three_month_window_does_not_skip():
    """Recency still has to be represented somewhere."""
    calm = flat(300, 0.0)
    spike = series([0.0] * 279 + [0.01] * 21)
    assert rs.score_one(spike, calm)["r3m"] > 0


# ================================================================== blending
def test_score_blends_the_available_windows():
    idx = flat(300, 0.0003)
    out = rs.score_one(flat(300, 0.001), idx)
    manual = sum(rs.WEIGHTS[k] * out[k] for k in rs.LOOKBACKS)
    assert out["score"] == pytest.approx(round(manual, 4), abs=1e-4)


def test_a_young_listing_is_scored_on_what_it_has():
    """Re-weighted rather than refused: a six-month-old listing has a real
    6-month number, and `bars` reports the thinness."""
    idx = flat(300, 0.0003)
    young = flat(80, 0.001)
    out = rs.score_one(young, idx)
    assert out["r12m"] is None and out["r3m"] is not None
    assert out["score"] is not None
    assert out["bars"] == 80


# =============================================================== percentiles
def test_percentiles_order_correctly():
    ranks = rs.percentile_ranks({"a": 0.5, "b": -0.2, "c": 0.1})
    assert ranks["a"] > ranks["c"] > ranks["b"]


def test_ties_share_the_midpoint():
    """Twenty identical scores must not hand one of them an arbitrary edge."""
    ranks = rs.percentile_ranks({"x": 1, "y": 1, "z": 0})
    assert ranks["x"] == ranks["y"]


def test_unscorable_names_rank_as_none_not_zero():
    """No history is not the same fact as terrible performance, and 0 would
    rank a new listing below every genuine laggard."""
    ranks = rs.percentile_ranks({"a": 0.1, "b": None})
    assert ranks["b"] is None


def test_percentiles_of_an_empty_universe():
    assert rs.percentile_ranks({}) == {}


def test_single_name_universe_does_not_crash():
    assert rs.percentile_ranks({"a": 0.3})["a"] == 50.0


# ============================================================= the screen
def _fake(monkeypatch, val_rows, matrix, strength):
    from app import screen as S
    monkeypatch.setattr(S.valuation, "screen", lambda syms=None: {"rows": val_rows})
    monkeypatch.setattr(S.signals, "scan",
                        lambda **k: {"matrix": matrix})
    monkeypatch.setattr(S.rs, "for_symbols", lambda syms, days=420: strength)


VAL = [
    {"symbol": "AAA", "verdict": "undervalued", "mos_pct": 35, "price": 100,
     "composite": 154, "n_models": 4},
    {"symbol": "BBB", "verdict": "overvalued", "mos_pct": -40, "price": 100,
     "composite": 71, "n_models": 3},
    {"symbol": "CCC", "verdict": "fair", "mos_pct": 10, "price": 100,
     "composite": 111, "n_models": 5},
]
MATRIX = {"AAA": {"s1": 1, "s2": 1}, "BBB": {"s1": 1, "s2": 0},
          "CCC": {"s1": 0, "s2": 0}}
STRENGTH = {"AAA": {"rs": 88.0, "r3m": 12.0, "r6m": 20.0},
            "BBB": {"rs": 95.0, "r3m": 30.0, "r6m": 40.0},
            "CCC": {"rs": 40.0, "r3m": -3.0, "r6m": 1.0}}


def test_only_the_intersection_passes(monkeypatch):
    """AAA is cheap, strong and long. BBB is strong but overvalued. CCC is
    cheap-ish but weak and flat."""
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    res = screen.run(symbols=["AAA", "BBB", "CCC"])
    assert [r["symbol"] for r in res["hits"]] == ["AAA"]


def test_each_axis_records_why_it_failed(monkeypatch):
    """An invisible rejection teaches you nothing."""
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    res = screen.run(symbols=["AAA", "BBB", "CCC"])
    rows = {r["symbol"]: r for r in res["rows"]}
    assert rows["BBB"]["failed"] == ["value"]
    assert set(rows["CCC"]["failed"]) == {"strength", "signal"}


def test_near_misses_are_the_ones_failing_exactly_one(monkeypatch):
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    res = screen.run(symbols=["AAA", "BBB", "CCC"])
    assert [r["symbol"] for r in res["near_misses"]] == ["BBB"]


def test_raising_the_rs_bar_removes_hits(monkeypatch):
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    assert screen.run(symbols=["AAA"], min_rs=95)["hits"] == []


def test_signal_requirement_can_be_dropped(monkeypatch):
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    loose = screen.run(symbols=["CCC"], min_rs=0, require_long=False)
    assert [r["symbol"] for r in loose["hits"]] == ["CCC"]


def test_hits_are_sorted_by_margin_of_safety(monkeypatch):
    val = VAL + [{"symbol": "DDD", "verdict": "undervalued", "mos_pct": 60,
                  "price": 100, "composite": 250, "n_models": 4}]
    matrix = {**MATRIX, "DDD": {"s1": 1}}
    strength = {**STRENGTH, "DDD": {"rs": 75.0, "r3m": 5.0, "r6m": 8.0}}
    _fake(monkeypatch, val, matrix, strength)
    res = screen.run(symbols=["AAA", "DDD"])
    assert [r["symbol"] for r in res["hits"]] == ["DDD", "AAA"]


def test_stance_is_described_not_just_counted(monkeypatch):
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    rows = {r["symbol"]: r for r in screen.run(symbols=["AAA", "BBB"])["rows"]}
    assert rows["AAA"]["stance"] == "all long"
    assert rows["BBB"]["stance"] == "1/2 long"


def test_missing_strength_data_says_so_rather_than_returning_nothing(monkeypatch):
    """An empty table has two very different causes, and conflating them means
    concluding the market has nothing to offer when you never backfilled."""
    _fake(monkeypatch, VAL, MATRIX, {})
    res = screen.run(symbols=["AAA"])
    assert res["hits"] == []
    assert "backfill" in res["note"]


def test_an_honest_empty_result_says_that_instead(monkeypatch):
    _fake(monkeypatch, VAL, MATRIX, STRENGTH)
    res = screen.run(symbols=["BBB"])
    assert "normal result" in res["note"]


def test_the_screen_does_not_fire_the_alert_push(monkeypatch):
    """A screen is a question, not an event. Pushing every time someone opens
    a page trains you to ignore the push."""
    seen = {}
    from app import screen as S
    monkeypatch.setattr(S.valuation, "screen", lambda syms=None: {"rows": VAL})
    monkeypatch.setattr(S.rs, "for_symbols", lambda s, days=420: STRENGTH)

    def spy(**kw):
        seen.update(kw)
        return {"matrix": MATRIX}

    monkeypatch.setattr(S.signals, "scan", spy)
    screen.run(symbols=["AAA"])
    assert seen.get("push") is False
    assert seen.get("record") is False


# ==================================================================== wiring
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app import db
    from app.server import app
    db.init()
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_rs_route_reports_missing_candles_clearly():
    c = _client()
    r = c.get("/api/rs?symbols=BEL")
    assert r.status_code in (200, 400)
    if r.status_code == 400:
        assert "backfill" in r.json()["detail"]


def test_rs_route_needs_something_to_rank():
    c = _client()
    assert c.get("/api/rs?symbols=").status_code == 400


def test_screen_page_loads_and_is_in_the_nav():
    c = _client()
    assert c.get("/screen").status_code == 200
    assert 'href="/screen"' in c.get("/scanner").text


def test_screen_page_explains_all_three_axes():
    """The page has to say why the intersection matters, or it's just a table
    of three numbers."""
    html = _client().get("/screen").text
    for needle in ("value trap", "Momentum alone", "timing tool"):
        assert needle in html, f"screen page missing the rationale: {needle}"


# ============================================== whole-market universe
def test_from_market_excludes_things_that_are_not_companies(monkeypatch):
    """Rights issues, the T2T and SME series, ETFs. Each one costs a
    rate-limited request at backfill and can't be the answer to "find me an
    undervalued stock"."""
    from app import db, universe as U

    db.init()
    rows = [("RELIANCE", "Reliance"), ("TCS", "TCS"),
            ("SOMECO-RE", "rights"), ("TINY-SM", "sme"), ("XYZ-BE", "t2t"),
            ("NIFTYBEES", "etf"), ("GOLDBEES", "etf")]
    with db.connect() as conn:
        conn.execute("DELETE FROM instruments WHERE tradingsymbol IN "
                     "('RELIANCE','TCS','SOMECO-RE','TINY-SM','XYZ-BE',"
                     "'NIFTYBEES','GOLDBEES')")
        for i, (sym, name) in enumerate(rows):
            conn.execute(
                "INSERT OR REPLACE INTO instruments (instrument_token, "
                "tradingsymbol, name, exchange, instrument_type) "
                "VALUES (?,?,?,?,?)", [900000 + i, sym, name, "NSE", "EQ"])

    out = U.from_market("t_market")
    kept = set(out["symbols"])
    assert {"RELIANCE", "TCS"} <= kept
    assert not kept & {"SOMECO-RE", "TINY-SM", "XYZ-BE", "NIFTYBEES",
                       "GOLDBEES"}
    U.remove("t_market")


def test_prune_needs_prices_so_it_runs_after_the_backfill(monkeypatch):
    """Liquidity is a fact about prices, and the instrument index has none.
    Without pruning, a whole-market screen surfaces micro-caps trading four
    thousand shares a day — technically hits, completely unactionable."""
    from app import universe as U

    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": 1})

    def fake(tok, iv, days):
        return [{"time": i, "open": 5, "high": 5, "low": 5, "close": 5,
                 "volume": 100} for i in range(300)]

    monkeypatch.setattr("app.candles.get", fake)
    monkeypatch.setattr(U, "symbols", lambda name: ["PENNY"])
    out = U.prune("t_market", dry=True)
    assert out["kept"] == 0 and out["dropped"] == 1
    assert "price" in list(out["why"].values())[0]


def test_prune_dry_run_changes_nothing(monkeypatch):
    from app import universe as U
    monkeypatch.setattr("app.instruments.lookup", lambda s: None)
    monkeypatch.setattr(U, "symbols", lambda name: ["GHOST"])
    called = {"n": 0}
    monkeypatch.setattr(U, "set_members",
                        lambda *a, **k: called.update(n=called["n"] + 1))
    U.prune("t_market", dry=True)
    assert called["n"] == 0
