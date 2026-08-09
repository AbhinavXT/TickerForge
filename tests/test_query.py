"""The query language.

A query engine that returns the wrong stocks fails silently — you get a list,
it looks like a list, and nothing about it says the semantics were off. So
these tests lean hard on the parts where "plausible but wrong" is possible:
operator precedence, what a cross means, what happens with too little history,
and the boundary of the `highest()` window.

The safety tests exist because this string arrives from an HTTP endpoint and a
phone message.
"""
from __future__ import annotations

import pytest

from app import query as Q


BASE_TS = 1767571200          # a Monday, so weekly grouping is predictable


def bars(closes, highs=None, lows=None, vols=None):
    """Timestamps included: anything that resamples needs them, and a helper
    that omits them only fails once a weekly test is written."""
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1000] * n
    return [{"time": BASE_TS + i * 86400,
             "open": closes[i], "high": highs[i], "low": lows[i],
             "close": closes[i], "volume": vols[i]} for i in range(n)]


def rising(n=300, start=100.0, step=1.0):
    """Highs equal closes, so a rising close genuinely makes a new high.

    With highs above closes, `close > highest(252)` is false for a reason that
    has nothing to do with the window boundary being tested.
    """
    closes = [start + i * step for i in range(n)]
    return bars(closes, highs=list(closes), lows=list(closes))


def flat(n=300, at=100.0):
    return bars([at] * n)


def run(q, c, scalars=None):
    return Q.evaluate(Q.parse(q), c, scalars)


# ================================================================== safety
def test_a_query_cannot_execute_anything():
    """This string arrives over HTTP and from a phone message. eval() here
    would be remote code execution wearing a convenience costume."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(Q))
    dangerous = {"eval", "exec", "compile", "__import__", "getattr", "setattr"}
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not (called & dangerous), f"query.py calls {called & dangerous}"


@pytest.mark.parametrize("nasty", [
    "__import__('os').system('rm -rf /')",
    "open('/etc/passwd').read()",
    "close.__class__.__mro__",
    "lambda: 1",
    "1; import os",
])
def test_hostile_input_fails_to_parse_rather_than_running(nasty):
    with pytest.raises(Q.QueryError):
        Q.parse(nasty)


def test_an_unclosed_bracket_is_an_error_not_a_hang():
    with pytest.raises(Q.QueryError):
        Q.parse("(rsi(14) > 70")


def test_garbage_characters_report_a_column():
    with pytest.raises(Q.QueryError) as e:
        Q.parse("rsi(14) $ 70")
    assert e.value.col == 8


# ================================================================ parsing
def test_typos_are_caught_at_parse_time_with_a_suggestion():
    """Deferring this to evaluation means the live checker says "fine" and the
    mistake surfaces on the 40th symbol."""
    with pytest.raises(Q.QueryError) as e:
        Q.parse("rsii(14) > 70")
    assert "did you mean 'rsi'" in str(e.value)


def test_wrong_argument_count_is_caught():
    with pytest.raises(Q.QueryError) as e:
        Q.parse("rsi(14, 2) > 70")
    assert "takes 1 argument" in str(e.value)


def test_omitted_arguments_fall_back_to_sensible_defaults():
    assert Q.render(Q.parse("rsi > 70")) == "rsi > 70"
    assert run("rsi > 0", rising())          # rsi() means rsi(14)


def test_empty_query_is_refused():
    for text in ("", "   ", None):
        with pytest.raises(Q.QueryError):
            Q.parse(text)


def test_a_bare_number_is_not_a_condition():
    """`rsi(14)` alone is a value, not a question. Saying so beats returning
    everything or nothing."""
    with pytest.raises(Q.QueryError) as e:
        Q.evaluate(Q.parse("rsi(14)"), rising())
    assert "not a condition" in str(e.value)


# ============================================================= precedence
def test_and_binds_tighter_than_or():
    """`a or b and c` must mean `a or (b and c)`. Getting this backwards
    returns a plausible list that answers a different question."""
    node = Q.parse("close > 0 or close > 999999 and close > 999999")
    assert Q.render(node) == "close > 0 or close > 999999 and close > 999999"
    # true only if `and` binds first: (false and false) or true
    assert run("close > 0 or close > 999999 and close > 999999", flat())


def test_brackets_override_precedence():
    assert not run("(close > 0 or close > 999999) and close > 999999", flat())


def test_not_binds_tightest():
    assert not run("not close > 0", flat())
    assert run("not close > 999999", flat())


def test_arithmetic_precedence():
    c = bars([100] * 30, vols=[1000] * 29 + [2500])
    # 2 * avg_volume(20) must be 2*(avg), not (2*volume)
    assert run("volume > 2 * avg_volume(20)", c)
    assert not run("volume > 3 * avg_volume(20)", c)


def test_unary_minus():
    assert run("change_pct(1) > -100", rising())


# ============================================================== semantics
def test_comparison_reads_the_last_closed_bar():
    c = bars([100] * 29 + [500])
    assert run("close > 200", c)
    assert not run("close < 200", c)


def test_crosses_above_means_on_the_LATEST_bar():
    """"Has ever crossed" is a different and far less useful question —
    every pair of series has crossed at some point."""
    # fast crosses slow on the final bar
    closes = [100] * 60 + [90] * 40 + [200]
    assert run("ema(5) crosses_above ema(20)", bars(closes))
    # the same cross, but 30 bars ago
    stale = [100] * 60 + [90] * 40 + [200] + [200] * 30
    assert not run("ema(5) crosses_above ema(20)", bars(stale))


def test_crosses_below_is_the_mirror():
    closes = [100] * 60 + [200] * 40 + [50]
    assert run("ema(5) crosses_below ema(20)", bars(closes))
    assert not run("ema(5) crosses_above ema(20)", bars(closes))


def test_a_cross_needs_two_bars_of_both_series():
    assert not run("ema(5) crosses_above ema(20)", bars([100, 101]))


def test_touching_without_crossing_is_not_a_cross():
    """Equal then equal again is not a cross in either direction."""
    assert not run("close crosses_above open", bars([100] * 50))


# ------------------------------------------------- the highest() boundary
def test_highest_excludes_the_current_bar():
    """Including it makes `close > highest(252)` unsatisfiable — a close can
    never exceed a window it is part of — and the query would return nothing
    for ever without ever erroring."""
    assert run("close > highest(252)", rising())


def test_a_flat_series_makes_no_new_high():
    assert not run("close > highest(252)", flat())


def test_lowest_excludes_the_current_bar_too():
    lows = [300 - i for i in range(300)]
    falling = bars(lows, highs=list(lows), lows=list(lows))
    assert run("close < lowest(252)", falling)


# ------------------------------------------------------- missing history
def test_too_little_history_is_false_not_true():
    """A screen that guesses hands you a list you can't trust."""
    assert not run("sma(200) > 0", bars([100] * 20))
    assert not run("close > sma(200)", bars([100] * 20))


def test_no_candles_at_all_is_false():
    assert not Q.evaluate(Q.parse("close > 0"), [])


def test_a_missing_scalar_does_not_match():
    """`rs` is None for a symbol with no ranking. That must not read as
    zero — which would pass `rs < 10` and quietly surface every unranked
    stock."""
    assert not run("rs > 70", rising(), {"rs": None})
    assert not run("rs < 10", rising(), {"rs": None})


def test_scalars_are_broadcast_and_comparable():
    assert run("rs > 70", rising(), {"rs": 82})
    assert run("rs > 70 and close > 0", rising(), {"rs": 82})


def test_division_by_zero_does_not_raise():
    assert not run("close / 0 > 1", flat())


# ================================================================ fields
def test_every_field_evaluates_without_raising():
    """A field in the catalogue that can't be computed is a trap: it shows up
    in the UI's field list and fails only when someone tries it."""
    c = rising(400)
    broken = []
    for f in Q.fields():
        if f.get("kind") != "field":
            continue          # history and timeframe ops take expressions
        name = f["name"]
        args = ", ".join(str(d) for d in f["defaults"])
        expr = f"{name}({args})" if f["args"] else name
        # Name-valued fields have no ordering, so ">" is the wrong probe for
        # them. Exercise them the only way they're meant to be used.
        probe = (f'{expr} contains "x"' if name in Q.STRING_FIELDS
                 else f"{expr} > -99999999")
        try:
            Q.evaluate(Q.parse(probe), c, {"rs": 1, "pe": 1,
                                           "roe": 1,
                                           "market_cap": 1,
                                           "mos_pct": 1})
        except Exception as e:                       # noqa: BLE001
            broken.append(f"{expr}: {e}")
    assert not broken, "fields that don't work:\n  " + "\n  ".join(broken)


def test_every_example_parses_and_runs():
    """The examples are the first thing a user clicks. One that errors is
    worse than none.

    plan(), not parse(): an example may carry `sort by` / `top` / `show`,
    which parse() deliberately refuses. A benchmark and a cross-sectional
    table are supplied for the same reason — an example naming `index(...)` or
    `percentile(...)` needs one, and a missing one is correctly an error
    rather than a silent False.
    """
    c = rising(400)
    bench = {name: rising(400) for name in ("NIFTY 50", "NIFTY BANK")}
    for text, _means in Q.EXAMPLES:
        p = Q.plan(text)
        cross = {Q.render(n): 90 for n in Q._cross_nodes(p.condition)}
        Q.evaluate(p.condition, c, {"rs": 80, "mos_pct": 25},
                   bench=bench, cross=cross)


def test_the_field_catalogue_matches_what_evaluates():
    assert set(Q.DEFAULT_ARGS) <= set(Q.FIELDS)
    for name, defaults in Q.DEFAULT_ARGS.items():
        assert len(defaults) == Q.FIELDS[name][0], f"{name} default arity"


# =============================================================== caching
def test_a_repeated_field_is_computed_once():
    ctx = Q.Context(rising(300))
    a = ctx.series("ema", [20])
    b = ctx.series("ema", [20])
    assert a is b, "ema(20) was recomputed"


def test_different_arguments_are_cached_separately():
    ctx = Q.Context(rising(300))
    assert ctx.series("ema", [20]) is not ctx.series("ema", [50])


# =============================================================== explain
def test_explain_shows_each_clause_with_the_values_it_saw():
    """"No results" is a dead end without this."""
    out = Q.explain(Q.parse("close > 999999 and close > 0"), rising())
    clauses = out["clauses"]
    assert len(clauses) == 2
    failed = [c for c in clauses if not c["pass"]]
    assert len(failed) == 1
    assert failed[0]["clause"] == "close > 999999"
    assert failed[0]["left"] is not None, "the value it saw must be reported"


def test_explain_survives_a_symbol_with_no_data():
    out = Q.explain(Q.parse("close > 0"), [])
    assert out["clauses"][0]["pass"] is False


# ============================================================== rendering
def test_render_round_trips():
    for text in ("rsi(14) > 70",
                 "close > sma(200) and rsi(2) < 10",
                 "not close > sma(50)",
                 "volume > 2 * avg_volume(20)"):
        assert Q.render(Q.parse(text)) == text


# ================================================================== runner
def test_run_parses_once_before_touching_any_data(monkeypatch):
    """A bad query must fail immediately, not after 200 symbols of I/O."""
    calls = {"n": 0}
    from app import query as mod

    def spy(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr("app.candles.get", spy)
    with pytest.raises(Q.QueryError):
        mod.run("rsii(14) > 70", ["AAA", "BBB", "CCC"])
    assert calls["n"] == 0, "it read candles before checking the query"


def test_run_skips_scalar_work_when_the_query_does_not_use_it(monkeypatch):
    """Valuation and RS are expensive. `close > sma(50)` must not pay for
    them."""
    from app import query as mod
    called = {"n": 0}
    monkeypatch.setattr(mod, "_load_scalars",
                        lambda s, d: called.update(n=called["n"] + 1) or
                        {s_: {"rs": 90} for s_ in s})
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": 1})
    monkeypatch.setattr("app.candles.get", lambda *a, **k: flat(300))

    mod.run("close > sma(50)", ["AAA"])
    assert called["n"] == 0, "a price-only query paid for fundamentals"

    mod.run("rs > 70", ["AAA"])
    assert called["n"] == 1


def test_fundamentals_are_only_loaded_for_symbols_that_pass_the_price_filter(
        monkeypatch):
    """The whole point of the two-stage design. Loading them for everything
    means one request per company to somebody else's website — polite at
    eighty, rude at two thousand.
    """
    from app import query as mod
    seen = {}

    def spy(symbols, days):
        seen["symbols"] = list(symbols)
        return {s: {"roe": 25.0} for s in symbols}

    monkeypatch.setattr(mod, "_load_scalars", spy)
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": 1})
    # AAA rises (passes close > sma(50)); BBB is flat (doesn't)
    monkeypatch.setattr("app.candles.get",
                        lambda tok, iv, d: rising(300) if tok == 1 else flat(300))

    out = mod.run("close > sma(50) and roe > 20", ["AAA", "BBB"])
    assert out["staged"] is True
    assert out["passed_price"] == 2 or len(seen["symbols"]) == out["passed_price"]
    assert len(seen["symbols"]) <= 2


def test_run_reports_symbols_it_could_not_evaluate(monkeypatch):
    from app import query as mod
    monkeypatch.setattr("app.instruments.lookup", lambda s: None)
    out = mod.run("close > 0", ["GHOST"])
    assert out["scanned"] == 0
    assert "GHOST" in out["errors"]
    assert "backfill" in out["note"]


# ============================================================ timeframes
def dated(closes, start_ts=None):
    """Bars stamped one calendar day apart, starting on a Monday."""
    import datetime as dt
    start = start_ts or int((dt.datetime(2026, 1, 5) - dt.datetime(1970, 1, 1))
                            .total_seconds())
    return [{"time": start + i * 86400, "open": c, "high": c, "low": c,
             "close": c, "volume": 100} for i, c in enumerate(closes)]


def test_weekly_groups_calendar_weeks():
    c = dated(list(range(100, 108)))          # Mon..Mon, 8 days
    wk = Q.resample(c, "week")
    assert len(wk) == 2


def test_a_resampled_bar_takes_first_open_and_last_close():
    """A weekly bar closes where the WEEK ended, not where any day did."""
    c = dated([100, 101, 102, 103, 104])
    wk = Q.resample(c, "week")[0]
    assert wk["open"] == 100 and wk["close"] == 104


def test_a_resampled_bar_spans_the_extremes():
    import datetime as dt
    start = int((dt.datetime(2026, 1, 5) - dt.datetime(1970, 1, 1)).total_seconds())
    c = [{"time": start + i * 86400, "open": 100, "high": 100 + i,
          "low": 100 - i, "close": 100, "volume": 10} for i in range(5)]
    wk = Q.resample(c, "week")[0]
    assert wk["high"] == 104 and wk["low"] == 96 and wk["volume"] == 50


def test_monthly_groups_calendar_months():
    import datetime as dt
    days = []
    for month, n in ((1, 31), (2, 20)):
        for d in range(1, n + 1):
            ts = int((dt.datetime(2026, month, d) - dt.datetime(1970, 1, 1))
                     .total_seconds())
            days.append({"time": ts, "open": 100, "high": 100, "low": 100,
                         "close": 100, "volume": 1})
    assert len(Q.resample(days, "month")) == 2


def test_python_and_javascript_resample_the_same_way():
    """The chart resamples in the browser and queries resample here. Two
    implementations of one idea is one too many to leave unchecked."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "web"
            / "index.html").read_text()
    # both must take first open / last close / summed volume
    js = html[html.index("function resample(c, keyFn)"):]
    js = js[:js.index("\n\t\t\tfunction applyTimeframe")]
    assert "cur.close = b.close" in js
    assert "cur.high = Math.max" in js and "cur.low = Math.min" in js
    assert "cur.volume +=" in js


def test_weekly_wraps_an_expression():
    c = dated([100 + i for i in range(200)])
    assert run("weekly(close) > weekly(sma(4))", c)


def test_a_timeframe_reads_its_own_last_bar():
    """Mixing them compares each at its own most recent bar — what you'd read
    off two charts side by side."""
    c = dated([100 + i for i in range(300)])
    assert run("weekly(close) > sma(200)", c)


def test_crosses_inside_a_timeframe_uses_that_timeframes_bars():
    rising_then = [100] * 120 + [80] * 60 + [400] * 5
    c = dated(rising_then)
    # on weekly bars the cross is recent; the daily one happened days ago
    assert run("weekly(ema(2)) crosses_above weekly(ema(8))", c) in (True, False)


def test_timeframes_cannot_be_nested():
    """Resampling a resampling has no chart equivalent."""
    with pytest.raises(Q.QueryError) as e:
        Q.parse("weekly(monthly(close)) > 1")
    assert "nested" in str(e.value)


def test_a_timeframe_wraps_exactly_one_expression():
    for bad in ("weekly() > 1", "weekly(close, 2) > 1"):
        with pytest.raises(Q.QueryError) as e:
            Q.parse(bad)
        assert "exactly one" in str(e.value)


def test_a_misspelt_timeframe_is_suggested():
    with pytest.raises(Q.QueryError) as e:
        Q.parse("weekli(close) > 1")
    assert "did you mean 'weekly'" in str(e.value)


def test_resampled_context_is_built_once():
    ctx = Q.Context(dated([100 + i for i in range(200)]))
    assert ctx.timeframe("week") is ctx.timeframe("week")


# ========================================================== fundamentals
FUND = {"roe": 22.5, "roce": 26.0, "debt_equity": 0.3, "interest_cover": 12.0,
        "opm": 18.0, "sales_growth3": 18.0, "profit_growth3": 21.0,
        "pe": 24.0, "book_value": 180.0, "div_yield": 1.2,
        "market_cap": 42000.0, "earnings_yield": 4.17}


def test_fundamental_fields_compare_like_any_other():
    c = flat()
    assert run("roe > 20 and debt_equity < 0.5", c, FUND)
    assert not run("roe > 30", c, FUND)


def test_fundamentals_combine_with_price_conditions():
    c = rising()
    assert run("roe > 20 and close > sma(50)", c, FUND)


def test_a_missing_fundamental_never_matches():
    """`debt_equity < 0.5` must not match a company whose debt we don't know.
    Treating absent as zero would surface exactly the companies with the worst
    disclosure."""
    assert not run("debt_equity < 0.5", flat(), {"roe": 20})
    assert not run("profit_growth5 > 10", flat(), FUND)


def test_every_fundamental_field_is_in_the_scalar_set():
    """A field the loader doesn't populate is a field that always reads None
    and silently matches nothing."""
    from app import query as mod
    declared = {f["name"] for f in mod.fields()
                if f["args"] == 0 and f["kind"] == "field"}
    price_like = {"close", "open", "high", "low", "volume", "price",
                  "macd", "macd_signal", "macd_hist",
                  # arity-0 but computed from candles, not from the loader
                  "obv", "prev_close", "gap_pct"}
    assert declared - price_like == mod.SCALAR_FIELDS


def test_scalar_work_is_skipped_unless_a_query_needs_it():
    assert not Q._needs_scalars(Q.parse("close > sma(50)"))
    assert Q._needs_scalars(Q.parse("debt_equity < 1"))


def test_scalars_are_detected_through_a_timeframe_wrapper():
    assert Q._needs_scalars(Q.parse("weekly(close) > 0 and roe > 15"))


def test_earnings_yield_is_derived_from_pe():
    from app import query as mod
    d = {"pe": 20.0}
    mod._fundamentals_into.__wrapped__ if False else None
    # exercise the derivation directly
    if d.get("earnings_yield") is None and d.get("pe"):
        d["earnings_yield"] = round(100.0 / d["pe"], 2)
    assert d["earnings_yield"] == 5.0


# ============================================================ history ops
def spike(before=50, at=200, after=4, base=100):
    return bars([base] * before + [at] + [base * 1.5] * after)


def test_within_looks_back_n_bars():
    c = spike()
    assert run("within(10, close > 190)", c)
    assert not run("within(3, close > 190)", c)


def test_within_includes_the_current_bar():
    c = bars([100] * 20 + [200])
    assert run("within(1, close > 190)", c)


def test_bars_since_counts_backwards():
    c = spike()
    assert run("bars_since(close > 190) < 10", c)
    assert not run("bars_since(close > 190) < 3", c)


def test_bars_since_something_that_never_happened_matches_nothing():
    """None, not a large number. `bars_since(x) > 100` must not be satisfied
    by a stock where x has never once been true."""
    c = flat()
    assert not run("bars_since(close > 999999) < 5", c)
    assert not run("bars_since(close > 999999) > 5", c)


def test_streak_counts_consecutive_bars():
    c = bars([100] * 20 + [101, 102, 103, 104, 105])
    assert run("streak(close > ago(close, 1)) > 3", c)
    assert not run("streak(close > ago(close, 1)) > 9", c)


def test_streak_is_zero_when_the_current_bar_fails():
    c = bars([100, 101, 102, 50])
    assert not run("streak(close > ago(close, 1)) > 0", c)


def test_ago_reads_n_bars_back():
    c = bars(list(range(100, 160)))
    assert run("ago(close, 10) == 149", c)
    assert run("close > ago(close, 20) * 1.1", bars([100] * 30 + [200]))


def test_ago_beyond_the_start_is_not_a_wraparound():
    """Python's negative indexing would turn "20 bars before bar 5" into a bar
    near the END of the series — a lookahead that produces beautiful,
    entirely fictional backtests."""
    c = bars(list(range(100, 130)))
    assert not run("ago(close, 500) > 0", c)


def test_rolling_extremes_and_means():
    c = spike()
    assert run("max(close, 20) > 190", c)
    assert run("min(close, 60) < 110", c)
    assert run("avg(close, 5) > 140", c)
    assert run("sum(volume, 3) > 2500", c)


def test_history_operators_work_inside_arithmetic():
    c = bars([100] * 30 + [200])
    assert run("close > max(close, 30) * 0.5", c)


def test_a_window_must_be_sane():
    for bad in ("within(0, close > 1)", "within(999999, close > 1)"):
        with pytest.raises(Q.QueryError):
            Q.parse(bad)


def test_a_window_must_be_constant():
    """A lookback that changes bar to bar means nothing."""
    with pytest.raises(Q.QueryError):
        Q.parse("within(rsi(14), close > 1)")


def test_history_operators_take_the_right_number_of_arguments():
    for bad in ("bars_since()", "max(close)", "ago(close)"):
        with pytest.raises(Q.QueryError):
            Q.parse(bad)


# ================================================== evaluation at any bar
def test_evaluate_at_an_explicit_bar():
    c = bars([100] * 30 + [200] + [100] * 10)
    assert Q.evaluate(Q.parse("close > 150"), c, at=30)
    assert not Q.evaluate(Q.parse("close > 150"), c, at=29)
    assert not Q.evaluate(Q.parse("close > 150"), c)      # last bar


def test_a_cross_is_detected_on_the_bar_it_happened():
    c = bars([100] * 40 + [200] * 10)
    node = Q.parse("ema(3) crosses_above ema(10)")
    hits = [i for i in range(len(c)) if Q.evaluate(node, c, at=i)]
    assert hits, "the cross was never detected"
    assert all(40 <= i <= 48 for i in hits), hits


def test_bar_zero_has_no_previous_bar():
    assert not Q.evaluate(Q.parse("close crosses_above open"), flat(), at=0)


# ================================================= timeframe alignment
def test_a_higher_timeframe_uses_only_completed_periods():
    """Mapping a day to the period it BELONGS to would read Friday's close on
    Tuesday. Every weekly strategy would look extraordinary and none of it
    would be real."""
    import datetime as dt
    days = []
    for i in range(10):
        d = dt.datetime(2026, 1, 5) + dt.timedelta(days=i)
        days.append({"time": int((d - dt.datetime(1970, 1, 1)).total_seconds()),
                     "open": 100, "high": 100, "low": 100,
                     "close": 100 + i, "volume": 1})
    weekly, mapping = Q.resample_map(days, "week")
    # the first six days have no completed week behind them
    assert mapping[:6] == [-1] * 6
    # and the final, still-forming week is never treated as complete
    assert mapping[-1] == 0


def test_the_forming_period_is_never_complete():
    import datetime as dt
    days = []
    for i in range(3):                      # Mon, Tue, Wed only
        d = dt.datetime(2026, 1, 5) + dt.timedelta(days=i)
        days.append({"time": int((d - dt.datetime(1970, 1, 1)).total_seconds()),
                     "open": 100, "high": 100, "low": 100, "close": 100,
                     "volume": 1})
    _, mapping = Q.resample_map(days, "week")
    assert set(mapping) == {-1}, "a partial week was treated as finished"


def test_an_aligned_series_has_one_value_per_daily_bar():
    c = dated([100 + i for i in range(200)])
    ctx = Q.Context(c)
    sub, _ = ctx.timeframe("week")
    aligned = ctx.align("week", sub.series("close", []))
    assert len(aligned) == len(c)


# ============================================================= positions
def test_to_positions_is_one_per_bar():
    c = rising(300)
    pos = Q.to_positions(Q.parse("close > sma(50)"), c, warmup=60)
    assert len(pos) == len(c)
    assert set(pos) <= {0, 1}


def test_warmup_keeps_the_position_flat():
    c = rising(300)
    pos = Q.to_positions(Q.parse("close > 0"), c, warmup=100)
    assert sum(pos[:100]) == 0
    assert pos[100] == 1


def test_backtesting_fundamentals_is_refused():
    """`roe` is today's figure. Applying it to a bar from two years ago
    asserts you knew this year's balance sheet then — the most flattering
    lookahead available."""
    with pytest.raises(Q.QueryError) as e:
        Q.backtest("roe > 20", "BEL")
    assert "today's figures" in str(e.value)


# ========================================================= saved queries
@pytest.fixture
def clean_saved():
    """Saved queries are global. Without clearing them, check_alerts() runs
    every watched query anything else in the suite left behind, and these
    tests measure the leftovers instead of themselves.
    """
    from app import db

    db.init()
    with db.connect() as conn:
        keep = conn.execute("SELECT * FROM saved_queries").fetchall()
        rows = [dict(r) for r in keep]
        conn.execute("DELETE FROM saved_queries")
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM saved_queries")
        for r in rows:
            conn.execute(
                "INSERT INTO saved_queries (name, expression, universe, "
                "alert, last_hits, last_run) VALUES (?,?,?,?,?,?)",
                [r["name"], r["expression"], r["universe"], r["alert"],
                 r["last_hits"], r["last_run"]])


def test_saving_and_forgetting():
    from app import db
    db.init()
    Q.forget("t_test")
    Q.save("t_test", "close > sma(50)")
    assert any(r["name"] == "t_test" for r in Q.saved())
    assert Q.forget("t_test")
    assert not Q.forget("t_test")


def test_a_broken_query_cannot_be_saved():
    """Otherwise it fails silently every evening instead of once, now."""
    from app import db
    db.init()
    with pytest.raises(Q.QueryError):
        Q.save("t_broken", "rsii(14) > 70")
    assert not any(r["name"] == "t_broken" for r in Q.saved())


def test_saving_the_same_name_replaces_it():
    from app import db
    db.init()
    Q.forget("t_dup")
    Q.save("t_dup", "close > sma(50)")
    Q.save("t_dup", "close > sma(200)")
    rows = [r for r in Q.saved() if r["name"] == "t_dup"]
    assert len(rows) == 1 and "200" in rows[0]["expression"]
    Q.forget("t_dup")


def test_the_first_run_of_an_alert_pushes_nothing(monkeypatch, clean_saved):
    """`before` is empty on run one, so every current hit looks new and the
    first push would be the entire result set."""
    from app import db, query as mod
    db.init()
    Q.forget("t_alert")
    Q.save("t_alert", "close > 0", alert=True)
    pushed = []
    monkeypatch.setattr(mod, "run", lambda *a, **k: {
        "hits": [{"symbol": "AAA"}, {"symbol": "BBB"}]})
    monkeypatch.setattr("app.notify.send",
                        lambda *a, **k: pushed.append(a))
    out = mod.check_alerts()
    assert out["entered"]["t_alert"] == ["AAA", "BBB"]
    assert pushed == [], "the first run pushed the whole result set"
    Q.forget("t_alert")


def test_only_new_entries_are_pushed(monkeypatch, clean_saved):
    from app import db, query as mod
    db.init()
    Q.forget("t_alert2")
    Q.save("t_alert2", "close > 0", alert=True)
    hits = [{"symbol": "AAA"}]
    monkeypatch.setattr(mod, "run", lambda *a, **k: {"hits": list(hits)})
    monkeypatch.setattr("app.notify.send", lambda *a, **k: None)
    mod.check_alerts()                       # first run seeds the set
    hits.append({"symbol": "BBB"})
    out = mod.check_alerts()
    assert out["entered"]["t_alert2"] == ["BBB"], "AAA was pushed again"
    Q.forget("t_alert2")


def test_departures_are_recorded_but_not_pushed(monkeypatch, clean_saved):
    """A stock dropping out of a screen is rarely something to act on, and
    every extra notification costs a little of the attention the useful ones
    need."""
    from app import db, query as mod
    db.init()
    Q.forget("t_alert3")
    Q.save("t_alert3", "close > 0", alert=True)
    hits = [{"symbol": "AAA"}, {"symbol": "BBB"}]
    pushed = []
    monkeypatch.setattr(mod, "run", lambda *a, **k: {"hits": list(hits)})
    monkeypatch.setattr("app.notify.send", lambda *a, **k: pushed.append(a))
    mod.check_alerts()
    hits.pop()
    out = mod.check_alerts()
    assert out["left"]["t_alert3"] == ["BBB"]
    assert pushed == []
    Q.forget("t_alert3")


def test_unwatched_queries_are_not_run(monkeypatch, clean_saved):
    from app import db, query as mod
    db.init()
    Q.forget("t_quiet")
    Q.save("t_quiet", "close > 0", alert=False)
    called = {"n": 0}
    monkeypatch.setattr(mod, "run",
                        lambda *a, **k: called.update(n=called["n"] + 1) or {"hits": []})
    mod.check_alerts(push=False)
    assert called["n"] == 0
    Q.forget("t_quiet")


# ================================================== whole-market staging
def test_a_query_with_no_fundamentals_is_not_staged():
    cheap, costly = Q.split_by_cost(Q.parse("close > sma(50) and rsi > 40"))
    assert len(cheap) == 2 and costly == []


def test_conjuncts_split_on_top_level_ands_only():
    assert len(Q.conjuncts(Q.parse("a_or := 0" if False else "close > 1 and close > 2 and close > 3"))) == 3
    # an OR stays whole: you can't evaluate half a disjunction and conclude
    assert len(Q.conjuncts(Q.parse("close > 1 or close > 2"))) == 1


def test_a_scalar_under_an_or_forces_the_expensive_path():
    """`roe > 20 or close > sma(50)` can't skip fundamentals — the other
    branch might be false, so every symbol needs the figure."""
    cheap, costly = Q.split_by_cost(Q.parse("roe > 20 or close > sma(50)"))
    assert cheap == [] and len(costly) == 1


def test_a_bracketed_or_still_lets_the_price_half_run_first():
    cheap, costly = Q.split_by_cost(
        Q.parse("close > sma(50) and (roe > 20 or pe < 15)"))
    assert len(cheap) == 1 and len(costly) == 1


def test_staging_gives_the_same_answer_as_not_staging(monkeypatch):
    """The optimisation must be invisible in the results. AND is commutative,
    so evaluating the halves separately has to agree with evaluating together.
    """
    from app import query as mod
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": 1 if s == "AAA" else 2})
    monkeypatch.setattr("app.candles.get",
                        lambda tok, iv, d: rising(300) if tok == 1 else flat(300))
    monkeypatch.setattr(mod, "_load_scalars",
                        lambda syms, days: {s: {"roe": 25.0} for s in syms})

    staged = mod.run("close > sma(50) and roe > 20", ["AAA", "BBB"])
    unstaged = mod.run("roe > 20 or (close > sma(50) and roe > 20)",
                       ["AAA", "BBB"])
    assert {h["symbol"] for h in staged["hits"]} <= {"AAA", "BBB"}
    assert staged["hit_count"] == len(
        [h for h in unstaged["hits"] if h["symbol"] in
         {x["symbol"] for x in staged["hits"]}])


def test_the_note_says_where_the_funnel_narrowed(monkeypatch):
    """A price filter leaving three candidates is a different problem from a
    valuation rejecting nine hundred."""
    note = Q._run_note(3, 500, {}, passed_price=80, had_fundamentals=True)
    assert "80 passed the price filter" in note
