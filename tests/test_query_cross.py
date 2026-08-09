"""Comparing a stock to the rest of the universe, from inside the query.

    close > sma(200) and percentile(change_pct(63)) > 80
    rank(change_pct(20) - index(change_pct(20))) <= 25

Every other operator answers a question about one symbol using only that
symbol's bars. These two can't: a percentile has no meaning until every other
candidate has been measured. That makes them a different shape of computation,
and the two things most worth pinning down are *what population* they measure
against and *when they refuse to run at all*.

**Population.** Over everything scanned, not over whatever survived the other
clauses. If the population narrowed as filters were added, `percentile(x) > 80`
would mean "top fifth of the universe" in one query and "top fifth of the
stocks already above their 200-DMA" in another — a much weaker screen, wearing
identical syntax, with nothing in the output to tell them apart.

**Refusal.** A percentile is computed from today's universe. Applying it to a
bar from 2023 asserts you knew this year's cross-section two years ago, which
is the same sin `roe` is already refused for, and about as flattering.
"""
from __future__ import annotations

import pytest

from app import query as Q


DAY = 86400
BASE_TS = 1767571200


def series(closes):
    return [{"time": BASE_TS + i * DAY, "open": c, "high": c * 1.01,
             "low": c * 0.99, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def trend(n, pct_total):
    """n bars ending pct_total% above where they started."""
    step = (1 + pct_total / 100) ** (1 / (n - 1))
    return series([100.0 * step ** i for i in range(n)])


def universe(monkeypatch, moves: dict, days=300):
    """Symbols whose 20-day change is controlled by `moves` (percent)."""
    from app import query as mod
    order = list(moves)
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": order.index(s) + 1}
                        if s in moves else None)
    monkeypatch.setattr(
        "app.candles.get",
        lambda tok, iv, d: trend(days, moves[order[tok - 1]]))
    monkeypatch.setattr(mod, "_load_scalars", lambda syms, d: {})
    return order


# ================================================================ population
def test_the_population_is_everything_scanned_not_what_survived(monkeypatch):
    """The clause that matters most. AAA is the strongest of four; a filter
    that removes the two weakest must NOT push AAA's percentile down by
    shrinking the field it is measured against.
    """
    moves = {"AAA": 40.0, "BBB": 30.0, "CCC": 20.0, "DDD": 10.0}
    syms = universe(monkeypatch, moves)

    alone = Q.run("percentile(change_pct(20)) > 0 "
                  "show percentile(change_pct(20))", syms)
    assert len(alone["hits"]) == 4
    assert alone["hits"][0]["percentile(change_pct(20))"] is not None

    # Same ranked expression, plus a filter on something unrelated (final
    # close: 140/130/120/110) that keeps only the top two.
    filtered = Q.run("close > 125 and percentile(change_pct(20)) > 80", syms)
    assert [h["symbol"] for h in filtered["hits"]] == ["AAA"], (
        "AAA is top of four (87.5th percentile); if the population had "
        "shrunk to the two survivors it would be 75th and drop out")


def test_a_symbol_with_no_value_is_not_in_the_population(monkeypatch):
    """A stock with too little history can't be ranked, and must not dilute
    the percentiles of the ones that can."""
    from app import query as mod
    order = ["AAA", "BBB", "SHORT"]
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": order.index(s) + 1})
    monkeypatch.setattr(
        "app.candles.get",
        lambda tok, iv, d: (trend(300, 40.0) if tok == 1 else
                            trend(300, 10.0) if tok == 2 else series([100.0] * 5)))
    monkeypatch.setattr(mod, "_load_scalars", lambda syms, d: {})
    out = mod.run("percentile(change_pct(20)) > 60 show percentile(change_pct(20))",
                  order)
    got = {h["symbol"]: h["percentile(change_pct(20))"] for h in out["hits"]}
    assert got == {"AAA": 75.0}, got


def test_the_note_says_how_big_the_population_was(monkeypatch):
    syms = universe(monkeypatch, {"AAA": 40.0, "BBB": 10.0})
    out = Q.run("percentile(change_pct(20)) > 0", syms)
    assert "2" in out["note"] and "percentile" in out["note"].lower()


# =============================================================== convention
def test_higher_value_means_higher_percentile(monkeypatch):
    syms = universe(monkeypatch, {"BEST": 50.0, "MID": 20.0, "WORST": 1.0})
    out = Q.run("percentile(change_pct(20)) > 0 "
                "sort by percentile(change_pct(20))", syms)
    assert [h["symbol"] for h in out["hits"]] == ["BEST", "MID", "WORST"]


def test_percentile_matches_the_one_rs_already_uses():
    """Two ideas of "percentile" in one app is a number that means different
    things on two pages."""
    from app import rs
    scores = {"a": 3.0, "b": 1.0, "c": 2.0}
    assert Q._percentiles(scores) == rs.percentile_ranks(scores)


def test_ties_share_the_midpoint(monkeypatch):
    """Twenty identical values must not hand one of them an arbitrary
    advantage over the others."""
    got = Q._percentiles({"a": 5.0, "b": 5.0, "c": 1.0})
    assert got["a"] == got["b"]
    assert got["a"] > got["c"]


def test_rank_counts_from_one_with_the_largest_first(monkeypatch):
    syms = universe(monkeypatch, {"BEST": 50.0, "MID": 20.0, "WORST": 1.0})
    out = Q.run("rank(change_pct(20)) <= 2 show rank(change_pct(20))", syms)
    got = {h["symbol"]: h["rank(change_pct(20))"] for h in out["hits"]}
    assert got == {"BEST": 1, "MID": 2}, got


def test_tied_ranks_share_the_better_position():
    got = Q._ranks({"a": 5.0, "b": 5.0, "c": 1.0})
    assert got["a"] == got["b"] == 1
    assert got["c"] == 3, "the tie consumes two places, as ranks do"


def test_an_unrankable_symbol_gets_none_not_last_place():
    got = Q._ranks({"a": 5.0, "b": None})
    assert got["a"] == 1 and got["b"] is None


# ================================================================= refusals
def test_a_cross_sectional_query_cannot_be_backtested():
    """Today's cross-section applied to a 2023 bar asserts you knew this
    year's universe two years ago — the same lookahead `roe` is refused for,
    and it would flatter every momentum system ever tested."""
    with pytest.raises(Q.QueryError) as e:
        Q.backtest("percentile(change_pct(63)) > 80", "INFY")
    assert "percentile" in str(e.value) or "universe" in str(e.value)


def test_evaluating_one_symbol_on_its_own_is_refused():
    """There is no honest answer for a single stock — the operator's whole
    input is the other stocks."""
    with pytest.raises(Q.QueryError):
        Q.evaluate(Q.plan("percentile(close) > 50").condition,
                   trend(300, 10.0))


def test_cross_sectional_operators_cannot_nest():
    for bad in ("percentile(rank(close)) > 1", "rank(percentile(close)) > 1"):
        with pytest.raises(Q.QueryError):
            Q.plan(bad)


def test_ranking_on_fundamentals_is_refused_with_a_reason():
    """`percentile(roe)` would need a statements fetch for every company in
    the universe — 2,000 requests to somebody else's website, which is the
    exact cost the two-stage design exists to avoid."""
    with pytest.raises(Q.QueryError) as e:
        Q.plan("percentile(roe) > 80")
    assert "roe" in str(e.value)


def test_it_takes_exactly_one_expression():
    for bad in ("percentile() > 1", "percentile(close, 20) > 1"):
        with pytest.raises(Q.QueryError):
            Q.plan(bad)


# ============================================================ with the rest
def test_it_composes_with_the_index(monkeypatch):
    """The capstone: relative strength, defined in the language rather than
    pre-computed — beat the index over a quarter, ranked against everyone
    else who did."""
    from app import query as mod
    moves = {"AAA": 40.0, "BBB": 20.0, "CCC": 5.0}
    syms = universe(monkeypatch, moves)
    monkeypatch.setattr(mod, "_load_benchmarks",
                        lambda names, days: {n: trend(300, 10.0)
                                             for n in names})
    out = mod.run(
        "percentile(change_pct(20) - index(change_pct(20))) > 60", syms)
    assert [h["symbol"] for h in out["hits"]] == ["AAA"]


def test_it_can_be_sorted_and_shown(monkeypatch):
    syms = universe(monkeypatch, {"AAA": 40.0, "BBB": 20.0, "CCC": 5.0})
    out = Q.run("close > 1 sort by percentile(change_pct(20)) top 2 "
                "show rank(change_pct(20))", syms)
    assert [h["symbol"] for h in out["hits"]] == ["AAA", "BBB"]
    assert out["hits"][0]["rank(change_pct(20))"] == 1


def test_a_sort_on_a_cross_operator_still_measures_the_whole_universe(
        monkeypatch):
    """`sort by` doesn't filter, so this is really a check that the clause
    path feeds the same population as the condition path."""
    syms = universe(monkeypatch, {"AAA": 40.0, "BBB": 20.0, "CCC": 5.0})
    out = Q.run("close > 1 show percentile(change_pct(20))", syms)
    vals = sorted(h["percentile(change_pct(20))"] for h in out["hits"])
    assert vals == [16.7, 50.0, 83.3]


def test_ranking_a_whole_universe_is_not_quadratic():
    """Both tables are bisected against a list built once. The counting form,
    or rebuilding the key inside the loop, is O(n²) — fine on a watchlist,
    four million operations on a universe, and invisible until it isn't.

    Loose on purpose: this checks a complexity class, not a benchmark.
    """
    import time

    scores = {f"S{i}": float(i % 997) for i in range(20000)}
    start = time.monotonic()
    Q._ranks(scores)
    Q._percentiles(scores)
    assert time.monotonic() - start < 2.0


def test_it_round_trips():
    for text in ("percentile(change_pct(63)) > 80",
                 "rank(close / sma(200)) <= 25",
                 "percentile(change_pct(20) - index(change_pct(20))) > 70"):
        assert Q.render_plan(Q.plan(text)) == text


# ==================================================================== cost
def test_candles_are_read_twice_only_when_a_cross_operator_is_used(
        monkeypatch):
    """The population has to be complete before any symbol can be judged, so
    a cross-sectional query makes a measuring pass and then an evaluating
    pass. That's a real cost and it must not be paid by queries that don't
    ask for it.
    """
    from app import query as mod
    reads = {"n": 0}
    order = ["AAA", "BBB"]

    def spy(tok, iv, d):
        reads["n"] += 1
        return trend(300, 10.0 * tok)

    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": order.index(s) + 1})
    monkeypatch.setattr("app.candles.get", spy)
    monkeypatch.setattr(mod, "_load_scalars", lambda syms, d: {})

    mod.run("close > 1", order)
    assert reads["n"] == 2, "a plain query read candles more than once"

    reads["n"] = 0
    mod.run("percentile(change_pct(20)) > 0", order)
    assert reads["n"] == 4, "expected one measuring pass and one evaluating pass"
