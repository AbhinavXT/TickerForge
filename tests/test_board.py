"""The Board's maths, on synthetic candles — no Kite anywhere.

Displacement, order-block selection and mitigation, liquidity clustering,
the OI read, scoring (including choppy → 0), ranking, breadth, sector flow,
and the alert condition the zone view arms.
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from app import config

_tmp = tempfile.TemporaryDirectory()
_DB = Path(_tmp.name) / "board.db"
config.DB_PATH = _DB

from app import alerts, board, db  # noqa: E402  (after DB_PATH override)


@pytest.fixture(autouse=True)
def _db():
    config.DB_PATH = _DB
    db.init()
    yield


# ----------------------------------------------------------------- builders
def bar(t, o, c, hi=None, lo=None, v=1000):
    return {"time": t, "open": o, "close": c,
            "high": hi if hi is not None else max(o, c) + 0.2,
            "low": lo if lo is not None else min(o, c) - 0.2, "volume": v}


def chop(n=32, base=100.0, t0=0, amp=0.8):
    """A sideways range: a sine wave, so swing highs/lows are strict local
    extremes and every cycle peaks at the same price (equal highs). ATR ≈ 1."""
    import math
    out, prev = [], base
    for i in range(n):
        c = base + amp * math.sin((i + 1) * 2 * math.pi / 8)
        out.append(bar(t0 + i, prev, c, hi=max(prev, c) + 0.2, lo=min(prev, c) - 0.2))
        prev = c
    return out


def bullish_setup():
    """Range → one bear candle (the OB) → a big bull candle through the
    range high (the displacement) → drift higher."""
    c = chop(30)
    t = len(c)
    c.append(bar(t, 100.6, 100.0, hi=100.8, lo=99.7))          # bear: the OB
    c.append(bar(t + 1, 100.0, 104.0, hi=104.2, lo=99.9))      # displacement
    for k in range(6):                                          # holds above
        c.append(bar(t + 2 + k, 104 + k * 0.3, 104.3 + k * 0.3))
    return c, t, t + 1


# ------------------------------------------------------------ displacement
def test_big_candle_through_the_swing_high_is_a_displacement():
    c, ob_i, d_i = bullish_setup()
    d = board.find_displacements(c)
    assert [x["i"] for x in d if x["dir"] == "bull"] == [d_i]
    hit = d[-1]
    assert hit["atr_mult"] > board.K_ATR
    assert hit["broke"] < c[d_i]["close"]


def test_big_candle_inside_the_range_is_not():
    """Body is huge but it never closes beyond the last swing high."""
    c = chop(30)
    t = len(c)
    c.append(bar(t, 97.0, 100.5, hi=100.6, lo=96.9))
    assert not [x for x in board.find_displacements(c) if x["i"] == t]


def test_small_candle_breaking_the_high_is_not():
    c = chop(30)
    t = len(c)
    c.append(bar(t, 100.6, 101.2))          # breaks the high, body ≈ 0.6 ATR
    assert not [x for x in board.find_displacements(c) if x["i"] == t]


def test_bearish_displacement_mirrors():
    c = chop(30)
    t = len(c)
    c.append(bar(t, 100.0, 100.6, hi=100.9, lo=99.9))          # bull: the OB
    c.append(bar(t + 1, 100.6, 96.5, hi=100.7, lo=96.3))       # displacement
    d = [x for x in board.find_displacements(c) if x["dir"] == "bear"]
    assert d and d[-1]["i"] == t + 1


def test_k_is_a_tunable():
    c, _, d_i = bullish_setup()
    assert not [x for x in board.find_displacements(c, k=50) if x["i"] == d_i]


# ------------------------------------------------------------ order blocks
def test_order_block_is_the_last_opposite_colour_candle():
    c, ob_i, d_i = bullish_setup()
    d = [x for x in board.find_displacements(c) if x["i"] == d_i][0]
    ob = board.order_block_for(c, d)
    assert ob["i"] == ob_i
    assert (ob["top"], ob["bottom"]) == (c[ob_i]["high"], c[ob_i]["low"])


def test_order_block_skips_same_colour_candles():
    """A small bull candle between the bear OB and the displacement doesn't
    become the OB — it's the last *bearish* candle that counts."""
    c = chop(30)
    t = len(c)
    c.append(bar(t, 100.6, 100.0, hi=100.8, lo=99.7))          # bear (OB)
    c.append(bar(t + 1, 100.0, 100.3))                         # small bull
    c.append(bar(t + 2, 100.3, 104.5, hi=104.6, lo=100.2))     # displacement
    d = [x for x in board.find_displacements(c) if x["i"] == t + 2][0]
    assert board.order_block_for(c, d)["i"] == t


def test_retest_does_not_mitigate_but_trading_through_does():
    c, ob_i, d_i = bullish_setup()
    d = [x for x in board.find_displacements(c) if x["i"] == d_i][0]
    ob = board.order_block_for(c, d)
    t = c[-1]["time"] + 1
    retest = c + [bar(t, 104.0, 100.9, lo=ob["top"] - 0.05)]    # dips into it
    assert not board.is_mitigated(retest, ob)
    through = retest + [bar(t + 1, 100.9, 99.0, lo=ob["bottom"] - 0.3)]
    assert board.is_mitigated(through, ob)


def test_latest_unmitigated_ob_skips_dead_ones():
    c, ob_i, d_i = bullish_setup()
    t = c[-1]["time"] + 1
    # trade back through the first OB …
    c += [bar(t, 105.5, 99.0, hi=105.6, lo=99.3)]
    c += chop(30, base=99.0, t0=t + 1)
    # … then a fresh bull setup above it
    t2 = c[-1]["time"] + 1
    c.append(bar(t2, 99.6, 99.0, hi=99.8, lo=98.7))
    c.append(bar(t2 + 1, 99.0, 103.5, hi=103.6, lo=98.9))
    ob, d = board.latest_unmitigated_ob(c, "bull")
    assert ob is not None and ob["time"] == t2


# --------------------------------------------------------------- liquidity
def test_equal_highs_cluster_within_tolerance():
    c = chop(40)            # every swing high in chop() sits at ~100.9
    lv = board.liquidity_levels(c, atr_now=1.0, tol_atr=0.15)
    eqh = [x for x in lv if x["kind"] == "eqh"]
    assert eqh and eqh[0]["touches"] >= 2


def test_distinct_highs_do_not_cluster():
    c = []
    for k in range(5):      # a staircase of ranges, each peaking 1 point higher
        c += chop(8, base=100 + k, t0=8 * k)
    lv = board.liquidity_levels(c, atr_now=1.0, tol_atr=0.15)
    assert not [x for x in lv if x["kind"] == "eqh"]


def test_pdh_pdl_are_always_liquidity():
    lv = board.liquidity_levels([], atr_now=None, pdh=110, pdl=95)
    assert [(x["kind"], x["price"]) for x in lv] == [("pdl", 95), ("pdh", 110)]


def test_cluster_groups_neighbours():
    assert board.cluster([1.0, 1.05, 3.0, 3.1, 3.12, 7], 0.15) == \
        [[1.0, 1.05], [3.0, 3.1, 3.12], [7]]


# ------------------------------------------------------------------ OI read
@pytest.mark.parametrize("p,oi,read", [
    (1.2, 5.0, "long_buildup"), (-1.2, 5.0, "short_buildup"),
    (1.2, -5.0, "short_covering"), (-1.2, -5.0, "long_unwinding"),
    (None, 3.0, None), (1.0, None, None),
])
def test_oi_read(p, oi, read):
    assert board.oi_read(p, oi) == read


# ------------------------------------------------------------------- score
def test_choppy_scores_zero():
    s = board.score("bull", move_atr=4, oi="long_buildup", vol_ratio=5,
                    adx=board.ADX_CHOPPY - 0.1, prox=1)
    assert s == {"score": 0, "choppy": True, "parts": {}}


def test_unknown_adx_is_treated_as_choppy():
    assert board.score("bull", move_atr=4, oi=None, vol_ratio=5, adx=None,
                       prox=1)["score"] == 0


def test_everything_maxed_is_100():
    s = board.score("bull", move_atr=board.MOVE_FULL_ATR, oi="long_buildup",
                    vol_ratio=board.VOL_FULL, adx=board.ADX_FULL, prox=1)
    assert s["score"] == 100 and not s["choppy"]


def test_score_is_monotone_in_each_component():
    base = dict(move_atr=1.5, oi="short_covering", vol_ratio=1.5, adx=25, prox=0.4)
    s0 = board.score("bull", **base)["score"]
    for k, better in [("move_atr", 2.5), ("oi", "long_buildup"),
                      ("vol_ratio", 2.5), ("adx", 35), ("prox", 0.9)]:
        assert board.score("bull", **{**base, k: better})["score"] > s0, k


def test_oi_read_is_judged_per_side():
    kw = dict(move_atr=2, vol_ratio=2, adx=30, prox=0.5)
    assert board.score("bull", oi="long_buildup", **kw)["score"] > \
        board.score("bull", oi="short_buildup", **kw)["score"]
    assert board.score("bear", oi="short_buildup", **kw)["score"] > \
        board.score("bear", oi="long_buildup", **kw)["score"]


def test_weights_are_tunable_constants():
    assert set(board.WEIGHTS) == {"move", "oi", "volume", "adx", "proximity"}
    only_move = {k: (1 if k == "move" else 0) for k in board.WEIGHTS}
    s = board.score("bull", move_atr=board.MOVE_FULL_ATR, oi=None, vol_ratio=0,
                    adx=30, prox=0, weights=only_move)
    assert s["score"] == 100


def test_proximity_inside_zone_is_full_credit():
    ob = {"top": 101, "bottom": 100}
    assert board.proximity(100.5, ob, 1.0)[0] == 1.0
    credit, d_atr, d_pct = board.proximity(103, ob, 1.0)
    assert d_atr == pytest.approx(2.0)
    assert credit == pytest.approx(1 - 2 / board.OB_NEAR_ATR)
    assert d_pct < 0                      # the zone is below price
    assert board.proximity(110, ob, 1.0)[0] == 0.0


# ------------------------------------------------------------ whole stock
def trend(n=120, drift=0.3, amp=1.2):
    """A steady climb with wiggles: strict swing points and a strong ADX."""
    import math
    out, prev = [], 100.0
    for i in range(n):
        c = 100 + drift * i + amp * math.sin((i + 1) * 2 * math.pi / 8)
        out.append(bar(i, prev, c, hi=max(prev, c) + 0.1, lo=min(prev, c) - 0.1))
        prev = c
    return out


def test_flat_range_is_choppy_and_scores_zero():
    a = board.analyze(chop(120), chg_pct=0.1)
    assert a["side"] == "choppy" and a["score"] == 0


def test_trend_with_a_live_ob_ranks_bullish():
    c = trend(100)
    t = len(c)
    p = c[-1]["close"]
    c.append(bar(t, p + 0.3, p - 0.2, hi=p + 0.4, lo=p - 0.3))     # bear OB
    c.append(bar(t + 1, p - 0.2, p + 4.0, hi=p + 4.1, lo=p - 0.3))  # displacement
    for k in range(4):
        c.append(bar(t + 2 + k, p + 4 + k * .2, p + 4.2 + k * .2))
    a = board.analyze(c, chg_pct=2.1, oi_chg_pct=6.0)
    assert a["side"] == "bull"
    assert a["score"] > 0
    assert a["ob"] is not None and a["ob"]["dir"] == "bull"
    assert a["oi_read"] == "long_buildup"


# --------------------------------------------------------- board assembly
def test_rank_breadth_and_sector_flow():
    rows = [{"symbol": f"S{i}", "side": side, "score": sc, "chg_pct": ch}
            for i, (side, sc, ch) in enumerate([
                ("bull", 80, 2.0), ("bull", 60, 1.0), ("bear", 70, -1.5),
                ("choppy", 0, 0.1), ("bull", 90, 0.5)])]
    top = board.rank(rows, "bull", n=2)
    assert [r["symbol"] for r in top] == ["S4", "S0"]
    assert [r["rank"] for r in top] == [1, 2]
    assert board.breadth(rows) == {"bull": 3, "choppy": 1, "bear": 1}
    flow = board.sector_flow(rows, {"S0": "Metals", "S1": "Metals", "S2": "IT"})
    assert flow[0]["sector"] == "Metals" and flow[0]["chg_pct"] == 1.5
    assert flow[-1]["sector"] == "IT"
    assert {f["sector"] for f in flow} == {"Metals", "IT", "Unclassified"}


def test_retest_condition_is_a_valid_alert():
    bull = {"dir": "bull", "top": 101.25, "bottom": 100.0}
    bear = {"dir": "bear", "top": 99.0, "bottom": 98.4}
    assert board.retest_condition(bull) == "< 101.25"
    assert board.retest_condition(bear) == "> 98.4"
    assert board.retest_condition(None) is None
    for cond in (board.retest_condition(bull), board.retest_condition(bear)):
        kind, params, _ = alerts.parse(cond.split())
        assert kind in ("price_lt", "price_gt") and "x" in params


def test_market_hours_are_ist():
    ist = board.IST
    assert board.market_open(datetime(2026, 10, 12, 9, 15, tzinfo=ist))      # Mon
    assert board.market_open(datetime(2026, 10, 12, 15, 29, tzinfo=ist))
    assert not board.market_open(datetime(2026, 10, 12, 15, 30, tzinfo=ist))
    assert not board.market_open(datetime(2026, 10, 12, 9, 14, tzinfo=ist))
    assert not board.market_open(datetime(2026, 10, 11, 11, 0, tzinfo=ist))  # Sun


# ---------------------------------------------------------------- universe
def test_universe_is_stock_futures_with_current_month():
    with db.connect() as conn:
        conn.execute("DELETE FROM instruments")
        rows = [
            (1, 1, "RELIANCE", "RELIANCE", "NSE", "NSE", "EQ", 1, 0.05, None),
            (2, 2, "RELIANCE26OCTFUT", "RELIANCE", "NFO", "NFO-FUT", "FUT", 250, 0.1, "2099-10-29"),
            (3, 3, "RELIANCE26NOVFUT", "RELIANCE", "NFO", "NFO-FUT", "FUT", 250, 0.1, "2099-11-26"),
            (4, 4, "NIFTY26OCTFUT", "NIFTY", "NFO", "NFO-FUT", "FUT", 75, 0.1, "2099-10-29"),
            (5, 5, "OLD25JANFUT", "OLD", "NFO", "NFO-FUT", "FUT", 1, 0.1, "2000-01-30"),
            (6, 6, "OLD", "OLD", "NSE", "NSE", "EQ", 1, 0.05, None),
        ]
        conn.executemany("INSERT INTO instruments (instrument_token, exchange_token, "
                         "tradingsymbol, name, exchange, segment, instrument_type, "
                         "lot_size, tick_size, expiry) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    u = board.universe()
    assert u == [{"symbol": "RELIANCE", "token": 1, "fut": "RELIANCE26OCTFUT",
                  "fut_token": 2}]


def test_snapshot_round_trips():
    data = {"built_at": "2026-10-09T15:29:00+05:30", "rows": [], "bull": []}
    board._save_snapshot(data)
    assert board._load_snapshot() == data


# ------------------------------------------------------------------ routes
def test_routes_answer_without_a_kite_session(monkeypatch):
    from fastapi.testclient import TestClient
    from app import kite_auth, server

    def no_session():
        raise SystemExit("No valid Kite session for today.")
    monkeypatch.setattr(kite_auth, "get_kite", no_session)
    board._market_cache.update(at=0.0, data=None)
    c = TestClient(server.app, base_url="http://127.0.0.1:5000")
    m = c.get("/api/board/market")
    assert m.status_code == 200
    body = m.json()
    assert set(body) == {"open", "ist", "nifty"} and body["nifty"] is None
    assert c.get("/api/board/RELIANCE").status_code == 401


def test_board_serves_the_last_session_outside_hours(monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    snap = {"built_at": "2026-10-09T15:29:00+05:30", "rows": [], "bull": [],
            "bear": [], "breadth": {"bull": 0, "choppy": 0, "bear": 0}}
    board._save_snapshot(snap)
    board._cache.update(at=0.0, data=None)
    monkeypatch.setattr(board, "market_open", lambda now=None: False)
    c = TestClient(server.app, base_url="http://127.0.0.1:5000")
    r = c.get("/api/board")
    assert r.status_code == 200
    assert r.json()["built_at"] == snap["built_at"] and r.json()["stale"] is True


def test_board_page_is_served():
    from fastapi.testclient import TestClient
    from app import server
    c = TestClient(server.app, base_url="http://127.0.0.1:5000")
    r = c.get("/board")
    assert r.status_code == 200 and 'data-page="board"' in r.text
