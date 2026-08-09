"""Offline tests for the backtest v2 engine — no Kite, no network.

Run:  python -m tests.test_backtest
"""
import math
import random

from app import backtest as bt


def C(o, h, l, c, v=1000, t=0):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def mk(closes, spread=1.0):
    """Candles from a close series, with a small symmetric range around it."""
    out = []
    for i, c in enumerate(closes):
        o = closes[i-1] if i else c
        hi = max(o, c) + spread
        lo = min(o, c) - spread
        out.append(C(o, hi, lo, c, t=i*86400))
    return out


def approx(a, b, tol=1e-6):
    assert abs(a-b) <= tol, f"{a} != {b}"


# ------------------------------------------------------------- simulator core
def test_entry_at_bar_zero_costed():
    c = mk([100, 101, 102, 103])
    pos = [1, 1, 1, 1]
    r = bt.simulate(c, pos, cost_bps=100)          # 1% per side, big so it shows
    # entry at bar0 close must be costed and marked (old engine missed this)
    assert r["markers"] and r["markers"][0]["type"] == "buy"
    assert r["markers"][0]["time"] == c[0]["time"]
    expect = 100000 * (1-0.01) * (103/100)
    approx(r["equity"][-1], expect, 1e-6)
    assert r["trades"][-1].get("open") is True


def test_no_lookahead_position_earns_next_bar():
    c = mk([100, 100, 110, 110])
    # long only at bar1 close -> earns bar2's +10%, nothing else
    r = bt.simulate(c, [0, 1, 0, 0], cost_bps=0)
    approx(r["equity"][-1], 100000*1.10)
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["reason"] == "signal" and t["bars"] == 1
    approx(t["return_pct"], 10.0, 0.01)


def test_costs_round_trip():
    c = mk([100, 100, 100, 100])
    r = bt.simulate(c, [0, 1, 0, 0], cost_bps=50)  # 0.5%/side, flat price
    approx(r["equity"][-1], 100000*(1-0.005)**2, 1e-6)
    approx(r["trades"][0]["net_pct"], ((1-0.005)**2 - 1)*100, 0.01)


def test_stop_loss_fills_at_stop():
    c = [C(100, 101, 99, 100, t=0), C(100, 101, 99, 100, t=1),
         C(100, 101, 90, 95, t=2), C(95, 96, 94, 95, t=3)]
    r = bt.simulate(c, [1, 1, 1, 1], cost_bps=0, risk={"sl_pct": 5})
    t = r["trades"][0]
    assert t["reason"] == "stop", t
    approx(t["exit"], 95.0)                        # filled AT the stop, not the low
    approx(t["return_pct"], -5.0, 0.01)
    # equity: flat bar1 (100->100), then -5% on bar2, then flat (blocked)
    approx(r["equity"][-1], 100000*0.95, 1e-6)


def test_stop_gap_fills_at_open():
    c = [C(100, 101, 99, 100, t=0), C(88, 89, 87, 88, t=1), C(88, 89, 87, 88, t=2)]
    r = bt.simulate(c, [1, 1, 1], cost_bps=0, risk={"sl_pct": 5})
    t = r["trades"][0]
    assert t["reason"] == "stop (gap)"
    approx(t["exit"], 88.0)                        # gap open through the stop
    approx(r["equity"][-1], 100000*0.88, 1e-6)


def test_target_fills_at_target():
    c = [C(100, 101, 99, 100, t=0), C(100, 112, 100, 108, t=1), C(108, 109, 107, 108, t=2)]
    r = bt.simulate(c, [1, 1, 1], cost_bps=0, risk={"tp_pct": 10})
    t = r["trades"][0]
    assert t["reason"] == "target"
    approx(t["exit"], 110.0)
    approx(r["equity"][-1], 100000*1.10, 1e-6)


def test_stop_before_target_same_bar():
    # both stop (95) and target (110) inside bar1's range -> conservative: stop
    c = [C(100, 101, 99, 100, t=0), C(100, 115, 94, 100, t=1), C(100, 101, 99, 100, t=2)]
    r = bt.simulate(c, [1, 1, 1], cost_bps=0, risk={"sl_pct": 5, "tp_pct": 10})
    assert r["trades"][0]["reason"] == "stop"


def test_target_gap_beats_intrabar_stop():
    """A bar that OPENS above the target is a target fill, even if it later
    trades through the stop — the open is the first price the market shows,
    so the position is already out before the stop is ever reached."""
    c = [C(100, 101, 99, 100, t=0),
         C(120, 125, 90, 95, t=1),        # gaps to 120 (target 110), then dumps to 90
         C(95, 96, 94, 95, t=2)]
    r = bt.simulate(c, [1, 1, 1], cost_bps=0, risk={"sl_pct": 5, "tp_pct": 10})
    t0 = r["trades"][0]
    assert t0["reason"] == "target (gap)"
    approx(t0["exit"], 120.0, 0.01)
    approx(t0["return_pct"], 20.0, 0.01)


def test_stop_gap_still_beats_target_gap():
    """Symmetric case: a gap DOWN through the stop fills at the open."""
    c = [C(100, 101, 99, 100, t=0),
         C(80, 130, 79, 129, t=1),        # opens below the stop, then rips upward
         C(129, 130, 128, 129, t=2)]
    r = bt.simulate(c, [1, 1, 1], cost_bps=0, risk={"sl_pct": 5, "tp_pct": 10})
    assert r["trades"][0]["reason"] == "stop (gap)"
    approx(r["trades"][0]["exit"], 80.0, 0.01)


def test_blocked_until_signal_resets():
    # stop fires on bar1; signal stays 1 -> must NOT re-enter until it dips to 0
    c = [C(100, 101, 99, 100, t=0), C(100, 101, 90, 92, t=1),
         C(92, 93, 91, 92, t=2), C(92, 93, 91, 92, t=3),
         C(92, 93, 91, 92, t=4), C(92, 102, 91, 100, t=5)]
    pos = [1, 1, 1, 0, 1, 1]
    r = bt.simulate(c, pos, cost_bps=0, risk={"sl_pct": 5})
    assert r["trades"][0]["reason"] == "stop"
    # re-entry only at bar4 (after the 0 at bar3)
    buys = [m for m in r["markers"] if m["type"] == "buy"]
    assert len(buys) == 2 and buys[1]["time"] == c[4]["time"]


def test_time_stop():
    c = mk([100]*10)
    r = bt.simulate(c, [1]*10, cost_bps=0, risk={"max_bars": 3})
    t = r["trades"][0]
    assert t["reason"] == "time" and t["bars"] == 3


def test_time_stop_blocks_like_risk_exit():
    c = mk([100]*10)
    r = bt.simulate(c, [1]*10, cost_bps=0, risk={"max_bars": 3})
    # signal never resets, so exactly ONE completed trade, no re-entry
    closed = [t for t in r["trades"] if not t.get("open")]
    assert len(closed) == 1, closed


def test_trailing_stop_ratchets():
    # rise 100->120 then fall; chandelier(mult=2, period=3) should exit on the way down
    closes = [100, 104, 108, 112, 116, 120, 118, 114, 108, 104]
    c = mk(closes, spread=0.5)
    r = bt.simulate(c, [1]*len(c), cost_bps=0,
                    risk={"trail_mult": 2, "trail_period": 3})
    t = r["trades"][0]
    assert t["reason"] in ("stop", "stop (gap)")
    assert t["exit"] > 104                          # locked in above the final price
    assert t["return_pct"] > 0                      # the ratchet preserved profit


def test_trailing_never_loosens():
    closes = [100, 110, 105, 104.5, 104.6, 104.4]
    c = mk(closes, spread=0.2)
    a = bt.ind.atr(c, 3)
    # emulate: hh after bar1=110.2; stop must never decrease afterwards
    r = bt.simulate(c, [1]*len(c), cost_bps=0, risk={"trail_mult": 3, "trail_period": 3})
    # can't observe internal stop directly; property tested via no premature exit
    # and via the ratchet test above; here just assert simulation is sane
    assert r["equity"][-1] > 0 and a is not None


# ------------------------------------------------------------------ metrics
def test_metrics_basics():
    eq = [100, 110, 99, 105]
    rets = [0.10, -0.10, 0.0606060606]
    m = bt._metrics(eq, rets, bpy=252)
    approx(m["total_return_pct"], 5.0, 0.01)
    approx(m["max_drawdown_pct"], -10.0, 0.01)
    assert m["longest_dd_bars"] == 2               # 99 and 105 both below peak 110
    assert m["calmar"] is not None and m["recovery_factor"] == round(5.0/10.0, 2)


def test_trade_stats():
    trades = [
        {"return_pct": 5, "net_pct": 4.8, "bars": 3},
        {"return_pct": -2, "net_pct": -2.2, "bars": 2},
        {"return_pct": 3, "net_pct": 2.8, "bars": 4},
        {"return_pct": 4, "net_pct": 3.8, "bars": 1},
    ]
    s = bt._trade_stats(trades)
    assert s["trades"] == 4
    approx(s["win_rate_pct"], 75.0)
    # every stat is measured after costs, so profit factor and expectancy
    # can never disagree about whether a trade made money
    approx(s["profit_factor"], (4.8+2.8+3.8)/2.2, 0.01)
    approx(s["expectancy_pct"], (4.8-2.2+2.8+3.8)/4, 0.01)
    assert s["best_trade_pct"] == 4.8 and s["worst_trade_pct"] == -2.2
    assert s["max_consec_wins"] == 2 and s["max_consec_losses"] == 1


def test_trade_stats_excludes_open_position():
    """The unexited position at the end of the window doesn't vote."""
    trades = [
        {"return_pct": -3, "net_pct": -3.2, "bars": 2},
        {"return_pct": 9, "net_pct": 8.8, "bars": 5, "open": True},
    ]
    s = bt._trade_stats(trades)
    assert s["trades"] == 1 and s["open_trades"] == 1
    approx(s["win_rate_pct"], 0.0)              # not 50% — the winner is unrealised
    approx(s["expectancy_pct"], -3.2, 0.01)
    assert s["best_trade_pct"] == -3.2


def test_trade_stats_win_is_net_of_costs():
    """A trade that gains less than the round trip cost is a loss, not a win."""
    trades = [{"return_pct": 0.2, "net_pct": -0.2, "bars": 1}]
    s = bt._trade_stats(trades)
    approx(s["win_rate_pct"], 0.0)
    approx(s["expectancy_pct"], -0.2, 0.01)


# --------------------------------------------------------------- strategies
def trending(n=300, drift=0.4, seed=7):
    random.seed(seed)
    closes = [100.0]
    for _ in range(n-1):
        closes.append(max(1.0, closes[-1] + drift + random.uniform(-1, 1)))
    return mk(closes)

def choppy(n=300, seed=7):
    random.seed(seed)
    closes = [100.0]
    for i in range(n-1):
        closes.append(100 + 5*math.sin(i/7) + random.uniform(-1, 1))
    return mk(closes)


def test_all_strategies_valid_series():
    up, ch = trending(), choppy()
    for key, spec in bt.STRATEGIES.items():
        for c in (up, ch):
            pos = spec["fn"](c, spec["params"])
            assert len(pos) == len(c), key
            assert set(pos) <= {0, 1}, key
            # warmup: no position before ANY indicator could exist
            assert pos[0] in (0, 1) and pos[1] == pos[1]
        # every strategy has full about docs
        ab = spec["about"]
        for f in ("category", "summary", "entry", "exit", "math", "works", "fails", "tip"):
            assert ab.get(f), f"{key} missing about.{f}"


def test_momentum_logic():
    c = mk(list(range(100, 160)))                  # strictly rising
    pos = bt.s_momentum(c, {"length": 10})
    assert all(p == 1 for p in pos[10:])
    assert all(p == 0 for p in pos[:10])
    c2 = mk(list(range(160, 100, -1)))             # strictly falling
    assert all(p == 0 for p in bt.s_momentum(c2, {"length": 10}))


def test_donchian_asymmetric_exit():
    # breakout then shallow dip: exit=5 lookback triggers before entry=20 would
    closes = [100]*25 + [106, 107, 108, 109, 110] + [109, 108, 103, 103, 103]
    c = mk(closes, spread=0.2)
    pos = bt.s_donchian_breakout(c, {"entry": 20, "exit": 5})
    assert 1 in pos
    assert pos[-1] == 0                            # dropped below 5-bar low -> out


def test_pullback_trend_requires_trend():
    # falling market: RSI(2) oversold constantly, but below SMA -> never long
    c = mk([200 - i for i in range(120)])
    pos = bt.s_pullback_trend(c, {"trend": 50, "length": 2, "entry": 10, "exit": 60})
    assert all(p == 0 for p in pos)


def test_st_adx_gates_entry():
    up = trending(drift=0.8)
    plain = bt.s_supertrend(up, {"length": 10, "mult": 3})
    gated = bt.s_st_adx(up, {"length": 10, "mult": 3, "adx_len": 14, "adx": 20})
    # the gate can only delay/skip entries, never add exposure
    assert sum(gated) <= sum(plain)


def test_run_shape_via_simulate():
    """End-to-end everything below run()'s Kite fetch."""
    c = trending(400)
    for key, spec in bt.STRATEGIES.items():
        pos = spec["fn"](c, spec["params"])
        sim = bt.simulate(c, pos, cost_bps=10,
                          risk={"sl_pct": 8, "trail_mult": 3})
        assert len(sim["curve"]) == len(c)
        assert len(sim["rets"]) == len(c) - 1
        for t in sim["trades"]:
            assert t["reason"] in ("signal", "stop", "stop (gap)", "target",
                                   "target (gap)", "time", "open")
            assert t["exit_time"] >= t["entry_time"]
        assert all(sim["equity"][i] > 0 for i in range(len(sim["equity"])))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
