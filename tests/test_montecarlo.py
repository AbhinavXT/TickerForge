"""Monte Carlo over the trade sequence.

The properties worth pinning are mathematical, so most of these are exact
rather than statistical: a shuffle cannot change the compounded total, a
drawdown cannot be positive, a seeded run must reproduce. The few genuinely
statistical assertions are given enough simulations to be stable, and a seed
so a bad day can't turn into a flaky suite.
"""
from __future__ import annotations

import pytest

from app import montecarlo as MC


def trades(*returns, **kw):
    """Trade records in the shape backtest.simulate emits."""
    out = [{"net_pct": r} for r in returns]
    if kw.get("open_last"):
        out[-1]["open"] = True
    return out


def spread(n=40, seed=5):
    """A believable trend-following distribution: mostly small losses, a few
    large wins."""
    import random
    rng = random.Random(seed)
    return trades(*[round(rng.gauss(12, 9) if rng.random() < 0.35
                          else rng.gauss(-4, 2), 2) for _ in range(n)])


# ------------------------------------------------------------------ guards
def test_too_few_trades_is_refused():
    """A distribution from four trades is noise wearing a decimal point."""
    with pytest.raises(MC.MonteCarloError) as e:
        MC.run(trades(1, 2, -1, 2), sims=100)
    assert "closed trades" in str(e.value)


def test_open_trades_are_excluded():
    """Same exclusion the backtester makes: no exit, so no known contribution.
    Counting an unrealised gain would flatter every simulated path."""
    with_open = spread(6)
    with_open[-1]["open"] = True
    assert len(MC._closed(with_open)) == 5


def test_bad_mode_is_refused():
    with pytest.raises(MC.MonteCarloError):
        MC.run(spread(), sims=10, mode="wishful")


def test_absurd_sim_counts_are_refused():
    with pytest.raises(MC.MonteCarloError):
        MC.run(spread(), sims=0)
    with pytest.raises(MC.MonteCarloError):
        MC.run(spread(), sims=MC.MAX_SIMS + 1)


# ------------------------------------------------------- the core invariant
def test_shuffling_cannot_change_the_final_return():
    """Compounding is commutative, so every ordering lands on the same number.

    This is why the shuffle mode reports no return distribution: it would be
    zero-width, and printing it invites people to read meaning into it.
    """
    returns = [0.10, -0.05, 0.22, -0.11, 0.03, 0.40, -0.20]
    finals = set()
    import random
    rng = random.Random(0)
    for _ in range(50):
        draw = returns[:]
        rng.shuffle(draw)
        finals.add(round(MC._walk(draw).final_pct, 9))
    assert len(finals) == 1


def test_shuffle_reports_no_return_distribution():
    r = MC.run(spread(), sims=200, mode="shuffle", seed=1)
    assert r["final_pct"] is None
    assert "commutativity" in r["final_note"] or "same number" in r["final_note"]


def test_bootstrap_does_vary_the_return():
    r = MC.run(spread(), sims=500, mode="bootstrap", seed=1)
    assert r["final_pct"] is not None
    assert r["final_pct"]["p95"] > r["final_pct"]["p5"]


def test_shuffling_does_change_the_drawdown():
    """The whole point: same trades, different worst moment."""
    r = MC.run(spread(), sims=500, mode="shuffle", seed=1)
    assert r["max_dd_pct"]["p5"] < r["max_dd_pct"]["p95"]


# -------------------------------------------------------------- walk maths
def test_drawdown_is_never_positive():
    assert MC._walk([0.1, 0.2, 0.3]).max_dd == 0.0
    assert MC._walk([0.1, -0.2, 0.3]).max_dd < 0


def test_drawdown_is_measured_from_the_peak_not_the_start():
    """Up 50%, then down 20% from there, is a 20% drawdown — not a gain."""
    p = MC._walk([0.5, -0.2])
    assert p.max_dd == pytest.approx(-20.0, abs=0.01)
    assert p.final_pct == pytest.approx(20.0, abs=0.01)


def test_longest_drawdown_counts_trades_under_water():
    p = MC._walk([0.2, -0.05, -0.05, -0.05, 0.5])
    assert p.longest_dd == 3


def test_a_flat_series_has_no_drawdown():
    p = MC._walk([0.0, 0.0, 0.0])
    assert p.max_dd == 0.0 and p.longest_dd == 0


# ----------------------------------------------------------- reproducibility
def test_the_same_seed_gives_the_same_answer():
    a = MC.run(spread(), sims=300, seed=42)
    b = MC.run(spread(), sims=300, seed=42)
    assert a["max_dd_pct"] == b["max_dd_pct"]


def test_different_seeds_differ():
    a = MC.run(spread(), sims=300, seed=1)
    b = MC.run(spread(), sims=300, seed=2)
    assert a["max_dd_pct"] != b["max_dd_pct"]


# ------------------------------------------------------------- percentiles
def test_percentile_interpolates():
    assert MC._percentile([0, 10], 50) == pytest.approx(5)
    assert MC._percentile([0, 10, 20], 50) == pytest.approx(10)
    assert MC._percentile([5], 99) == 5


def test_percentiles_are_ordered():
    r = MC.run(spread(), sims=1000, seed=3)
    dd = r["max_dd_pct"]
    assert dd["p5"] <= dd["p25"] <= dd["p50"] <= dd["p75"] <= dd["p95"]


def test_the_floor_is_the_fifth_percentile():
    """'95% of orderings stayed above X' — X is p5 of the drawdowns, and the
    headline is wrong if these ever drift apart."""
    r = MC.run(spread(), sims=800, seed=4)
    assert r["dd_95_floor_pct"] == r["max_dd_pct"]["p5"]


def test_worst_seen_is_at_least_as_bad_as_the_floor():
    r = MC.run(spread(), sims=800, seed=4)
    assert r["worst_dd_pct"] <= r["dd_95_floor_pct"]


# --------------------------------------------------------- the luck ranking
def test_a_lucky_backtest_ranks_high():
    """If the observed ordering was mild, the user must be told the number
    they've been anchoring on is optimistic."""
    values = [-50.0, -40.0, -30.0, -20.0, -10.0]
    assert MC._rank_pct(values, -10.0) > 75


def test_an_unlucky_backtest_ranks_low():
    values = [-50.0, -40.0, -30.0, -20.0, -10.0]
    assert MC._rank_pct(values, -50.0) < 25


def test_verdict_warns_when_the_backtest_got_a_kind_ordering():
    r = {"dd_95_floor_pct": -30.0, "observed_dd_percentile": 90.0,
         "observed": {"max_dd_pct": -8.0}, "mode": "shuffle",
         "loss_probability_pct": 0}
    text = MC._verdict(r)
    assert "kind ordering" in text and "-30.0%" in text


def test_verdict_always_states_the_floor():
    for pct in (5.0, 50.0, 95.0):
        r = {"dd_95_floor_pct": -22.5, "observed_dd_percentile": pct,
             "observed": {"max_dd_pct": -20.0}, "mode": "shuffle",
             "loss_probability_pct": 0}
        assert "-22.5%" in MC._verdict(r)


# ------------------------------------------------------------- loss chance
def test_loss_probability_is_zero_for_an_always_winning_set():
    r = MC.run(trades(*[5.0] * 20), sims=200, mode="bootstrap", seed=1)
    assert r["loss_probability_pct"] == 0.0


def test_loss_probability_is_total_for_an_always_losing_set():
    r = MC.run(trades(*[-5.0] * 20), sims=200, mode="bootstrap", seed=1)
    assert r["loss_probability_pct"] == 100.0


# -------------------------------------------------------------- histogram
def test_histogram_counts_every_sample():
    r = MC.run(spread(), sims=600, seed=1)
    assert sum(r["dd_histogram"]["counts"]) == 600
    assert len(r["dd_histogram"]["edges"]) == len(r["dd_histogram"]["counts"]) + 1


def test_histogram_handles_identical_values():
    h = MC.histogram([-5.0] * 10)
    assert sum(h["counts"]) == 10


def test_histogram_of_nothing_is_empty():
    assert MC.histogram([]) == {"edges": [], "counts": []}


def test_bootstrap_adds_a_return_histogram():
    r = MC.run(spread(), sims=300, mode="bootstrap", seed=1)
    assert sum(r["final_histogram"]["counts"]) == 300


# ---------------------------------------------------------------- rendering
def test_report_fits_on_a_phone():
    r = MC.run(spread(), sims=300, mode="bootstrap", seed=1)
    assert len(MC.format_report(r).splitlines()) <= 16


def test_report_names_the_mode_and_sample_size():
    r = MC.run(spread(), sims=300, seed=1)
    text = MC.format_report(r)
    assert "shuffle" in text and "300" in text and "40 trades" in text


def test_shuffle_report_explains_the_missing_return_distribution():
    text = MC.format_report(MC.run(spread(), sims=200, seed=1))
    assert "Use bootstrap" in text


# ------------------------------------------------------------ the comparison
def test_bootstrap_is_the_more_pessimistic_mode():
    """Resampling can draw the same bad trade twice, so its tail is fatter.

    Worth asserting because it's the reason to prefer bootstrap for forecasts:
    a shuffle is bounded by the losses you actually took.
    """
    t = spread(60, seed=11)
    sh = MC.run(t, sims=3000, mode="shuffle", seed=1)
    bs = MC.run(t, sims=3000, mode="bootstrap", seed=1)
    assert bs["worst_dd_pct"] < sh["worst_dd_pct"]


# ------------------------------------------------------------------- the API
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app import db
    from app.server import app
    db.init()
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_route_analyses_the_trades_it_is_given():
    """POSTed rather than re-run server-side: the page is showing one specific
    result, and re-simulating could quietly analyse a different one."""
    c = _client()
    r = c.post("/api/backtest/montecarlo",
               json={"trades": spread(30), "sims": 200, "seed": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["trades"] == 30 and body["sims"] == 200
    assert body["verdict"]


def test_route_rejects_too_few_trades():
    c = _client()
    r = c.post("/api/backtest/montecarlo", json={"trades": spread(3)})
    assert r.status_code == 400
    assert "closed trades" in r.json()["detail"]


def test_route_rejects_a_bad_mode():
    c = _client()
    r = c.post("/api/backtest/montecarlo",
               json={"trades": spread(20), "mode": "vibes"})
    assert r.status_code == 400


def test_route_rejects_a_non_list():
    c = _client()
    assert c.post("/api/backtest/montecarlo",
                  json={"trades": "all of them"}).status_code == 400


def test_page_wires_montecarlo():
    c = _client()
    html = c.get("/backtest").text
    for needle in ("runMonteCarlo", "renderMC", "mcRun", "mcOut",
                   "/api/backtest/montecarlo"):
        assert needle in html, f"page missing {needle}"


def test_page_has_no_duplicate_element_ids():
    """The walk-forward panel shipped twice with duplicated ids, so every
    getElementById resolved to the dead copy. Cheap to guard against."""
    import re
    c = _client()
    ids = re.findall(r'\sid="([A-Za-z0-9_]+)"', c.get("/backtest").text)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate ids on the backtest page: {sorted(dupes)}"
