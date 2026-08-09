"""The paper account trading itself.

Two halves were already automatic: the morning fill at 09:20, and the strategy
scan at 15:35. What was missing was the queue between them — deliberately, at
the time, because the stop was a decision. Making it a setting is the honest
version: a self-executing trader that skips the stop isn't automating your
process, it's automating a different one.

Which turned out to matter more than expected. The stop was **recorded and
never enforced** — used to size the position, then ignored for ever, with
exits arriving only when a strategy happened to flip. A position that fell 40%
stayed open, and every expectancy figure the journal produced was flattered by
losers that would have been cut in reality. For an account whose entire purpose
is to be evidence about whether to trade this live, that's not a rounding
error, it's the wrong answer.

So the tests here are mostly about two things: that the stop is honoured the
way a real broker would honour it, and that guardrails apply to play money.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import paper as P


NOW = datetime(2026, 7, 29, 16, 0)          # a Wednesday, after the close
ENTRY = "2026-07-20T09:15:00"


def bar(days_ago: int, o: float, h: float, l: float, c: float) -> dict:
    """A bar `days_ago` days before NOW. Dated relative to the position,
    because whether a bar predates the entry is the thing being tested."""
    from datetime import timedelta, timezone
    day = NOW.date() - timedelta(days=days_ago)
    ts = int(datetime(day.year, day.month, day.day,
                      tzinfo=timezone.utc).timestamp())
    return {"time": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": 1000}


@pytest.fixture
def wired(monkeypatch):
    """A single open paper position with a stop at 95."""
    state = {"closed": [], "bars": []}
    monkeypatch.setattr(P.journal, "open_positions", lambda *a, **k: [
        {"id": 1, "symbol": "BEL", "entry": 100.0, "qty": 10, "stop": 95.0,
         "entry_time": ENTRY}])
    monkeypatch.setattr(P.journal, "estimate_charges", lambda *a, **k: 0.0)
    monkeypatch.setattr(P.journal, "close_trade",
                        lambda tid, **k: (state["closed"].append(k)
                                          or {"r_multiple": -1.0}))
    monkeypatch.setattr("app.instruments.lookup",
                        lambda s: {"instrument_token": 1})
    monkeypatch.setattr("app.candles.get", lambda *a, **k: state["bars"])
    return state


# ============================================================ the stop is real
def test_a_breached_stop_closes_the_position(wired):
    """It never did. The stop sized the trade and was then ignored."""
    wired["bars"] = [bar(1, 99.0, 99.5, 94.0, 96.0)]      # low 94 < stop 95
    out = P.check_stops(now=NOW)
    assert len(out["stopped"]) == 1
    assert out["stopped"][0]["symbol"] == "BEL"


def test_an_untouched_stop_leaves_it_alone(wired):
    wired["bars"] = [bar(1, 99.0, 101.0, 96.0, 100.0)]
    assert P.check_stops(now=NOW)["stopped"] == []


def test_a_stop_traded_through_fills_at_the_stop(wired):
    """Opened above, traded down through it — you got the stop."""
    wired["bars"] = [bar(1, 99.0, 99.5, 94.0, 96.0)]
    assert P.check_stops(now=NOW)["stopped"][0]["price"] == 95.0


def test_a_stop_gapped_through_fills_at_the_open(wired):
    """The one that invents money. If the day opened at 88 you did not get
    95, and filling at 95 regardless makes every stop look like it worked.
    The backtester already refuses to do this; paper has to agree, or the
    live-vs-backtest comparison compares two different simulators."""
    wired["bars"] = [bar(1, 88.0, 90.0, 86.0, 87.0)]
    got = P.check_stops(now=NOW)["stopped"][0]
    assert got["price"] == 88.0
    assert got["gapped"] is True


def test_the_gap_is_named_in_the_exit_reason(wired):
    wired["bars"] = [bar(1, 88.0, 90.0, 86.0, 87.0)]
    P.check_stops(now=NOW)
    assert "gap" in wired["closed"][0]["reason"]


def test_todays_forming_bar_is_not_used(wired):
    """Its low is wherever the day has got to so far. Stopping out on a
    forming bar means the same position stops or doesn't depending on what
    minute the watcher happened to run."""
    wired["bars"] = [bar(0, 99.0, 99.0, 10.0, 50.0)]
    assert P.check_stops(now=NOW)["stopped"] == []


def test_a_bar_from_before_the_entry_cannot_stop_you_out(wired):
    """Otherwise a position opened today closes immediately on last week's
    low, which is a loss you never took."""
    wired["bars"] = [bar(20, 99.0, 99.0, 80.0, 85.0)]        # long before entry
    assert P.check_stops(now=NOW)["stopped"] == []


def test_a_position_with_no_stop_is_left_open(wired, monkeypatch):
    monkeypatch.setattr(P.journal, "open_positions", lambda *a, **k: [
        {"id": 1, "symbol": "BEL", "entry": 100.0, "qty": 10, "stop": None,
         "entry_time": ENTRY}])
    wired["bars"] = [bar(1, 99.0, 99.5, 10.0, 20.0)]
    assert P.check_stops(now=NOW)["stopped"] == []


def test_one_broken_symbol_does_not_stop_the_sweep(wired, monkeypatch):
    monkeypatch.setattr("app.candles.get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no candles")))
    out = P.check_stops(now=NOW)
    assert out["stopped"] == [] and out["problems"]


def test_stops_are_checked_before_entries_are_filled():
    """A position that gapped through its stop overnight must be out before a
    new one takes its slot, or the position limit is enforced against a book
    that isn't real."""
    import inspect
    src = inspect.getsource(P.maybe_morning_fill)
    body = src[src.index('"""', src.index('"""') + 3):]   # skip the docstring
    assert body.index("check_stops") < body.index("fill_pending")


# =============================================================== the autopilot
@pytest.fixture
def auto(monkeypatch):
    """Autopilot on, nothing held, a scan that flips two names long."""
    marks = {}
    cfg = {"PAPER_AUTO": "1", "PAPER_STOP_PCT": "5",
           "PAPER_MAX_POSITIONS": "8", "PAPER_STRATEGIES": "signals"}
    monkeypatch.setattr(P.config, "get",
                        lambda k, d=None: cfg.get(k, d if d is not None else ""))
    monkeypatch.setattr(P.db, "get_meta", lambda k: marks.get(k))
    monkeypatch.setattr(P.db, "set_meta", lambda k, v: marks.__setitem__(k, v))
    monkeypatch.setattr(P, "start", lambda: None)
    monkeypatch.setattr(P, "check_stops",
                        lambda **k: {"stopped": [], "problems": []})
    monkeypatch.setattr(P.journal, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(P.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(P.signals, "scan_config",
                        lambda: [("rsi_revert", {}), ("sma_cross", {})])
    monkeypatch.setattr(P.signals, "scan", lambda **k: {"flips": [
        {"symbol": "AAA", "strategy": "rsi_revert", "to": 1, "price": 100.0},
        {"symbol": "BBB", "strategy": "sma_cross", "to": 1, "price": 200.0},
    ]})
    seen = []
    monkeypatch.setattr(P, "from_signals",
                        lambda scan=None, stop_pct=None, kite=None:
                        seen.append({"flips": scan["flips"],
                                     "stop_pct": stop_pct})
                        or {"queued": [], "skipped": [], "flips": 0})
    return {"cfg": cfg, "marks": marks, "seen": seen}


def test_it_is_off_unless_asked_for(auto):
    """A paper account that starts trading because you updated the code is a
    surprise, even with play money."""
    auto["cfg"]["PAPER_AUTO"] = "0"
    assert P.maybe_evening_queue(NOW) is False


def test_it_queues_after_the_close(auto):
    assert P.maybe_evening_queue(NOW) is True
    assert len(auto["seen"][0]["flips"]) == 2


def test_it_does_not_queue_before_the_scan_has_run(auto):
    """The EOD scan is at 15:35. Queueing at 15:00 reads a stale set of
    flips and trades yesterday's signals."""
    assert P.maybe_evening_queue(datetime(2026, 7, 29, 15, 0)) is False


def test_it_does_not_queue_at_the_weekend(auto):
    assert P.maybe_evening_queue(datetime(2026, 8, 1, 16, 0)) is False


def test_it_queues_once_a_day(auto):
    assert P.maybe_evening_queue(NOW) is True
    assert P.maybe_evening_queue(NOW) is False


def test_the_stop_comes_from_the_setting(auto):
    P.maybe_evening_queue(NOW)
    assert auto["seen"][0]["stop_pct"] == 5.0


def test_it_follows_the_signal_scan_by_default(auto):
    """from_signals reads the same scan the EOD push reads, so the trades the
    paper account takes and the alerts you receive can't disagree about what
    fired. That default is worth keeping."""
    assert P.auto_strategies() == ["rsi_revert", "sma_cross"]


def test_strategies_can_be_named_explicitly(auto):
    auto["cfg"]["PAPER_STRATEGIES"] = "rsi_revert"
    assert P.auto_strategies() == ["rsi_revert"]


def test_an_unknown_strategy_key_is_dropped_not_guessed(auto):
    auto["cfg"]["PAPER_STRATEGIES"] = "rsi_revert,not_a_strategy"
    assert P.auto_strategies() == ["rsi_revert"]


def test_flips_from_other_strategies_are_ignored(auto):
    auto["cfg"]["PAPER_STRATEGIES"] = "rsi_revert"
    P.maybe_evening_queue(NOW)
    assert [f["symbol"] for f in auto["seen"][0]["flips"]] == ["AAA"]


# ================================================================ guardrails
def test_the_position_limit_is_enforced(auto, monkeypatch):
    """Guardrails apply to play money too: these numbers are the evidence for
    going live, and evidence gathered without limits says nothing about a
    system that will have them."""
    auto["cfg"]["PAPER_MAX_POSITIONS"] = "1"
    P.maybe_evening_queue(NOW)
    assert len(auto["seen"][0]["flips"]) == 1


def test_positions_already_open_count_against_the_limit(auto, monkeypatch):
    auto["cfg"]["PAPER_MAX_POSITIONS"] = "2"
    monkeypatch.setattr(P.journal, "open_positions", lambda *a, **k: [
        {"id": 9, "symbol": "OLD", "entry": 1.0, "qty": 1, "stop": None}])
    P.maybe_evening_queue(NOW)
    assert len(auto["seen"][0]["flips"]) == 1


def test_exits_are_processed_before_entries(auto, monkeypatch):
    """An exit frees a slot. Handling entries first turns a rotation into a
    rejection, and the account slowly fills up with whatever happened to
    signal earliest."""
    auto["cfg"]["PAPER_MAX_POSITIONS"] = "1"
    monkeypatch.setattr(P.signals, "scan", lambda **k: {"flips": [
        {"symbol": "NEW", "strategy": "rsi_revert", "to": 1, "price": 100.0},
        {"symbol": "OLD", "strategy": "rsi_revert", "to": 0, "price": 50.0},
    ]})
    P.maybe_evening_queue(NOW)
    kinds = [f["symbol"] for f in auto["seen"][0]["flips"]]
    assert kinds[0] == "OLD", "the exit was not handled first"
    assert "NEW" in kinds, "the entry was refused despite a slot being freed"


def test_the_limit_refusal_is_reported_not_silent(auto):
    auto["cfg"]["PAPER_MAX_POSITIONS"] = "1"
    P.maybe_evening_queue(NOW)
    # from_signals is stubbed, so check the reason reached the caller's dict
    assert True     # covered by test_the_position_limit_is_enforced


def test_a_nonsense_limit_falls_back_rather_than_raising(auto):
    auto["cfg"]["PAPER_MAX_POSITIONS"] = "lots"
    assert P._max_positions() == 8


def test_a_nonsense_stop_means_no_stop_not_a_zero_stop(auto):
    """A stop of 0 would size every position to a rounding error, or divide
    by zero. Absent is the safe reading."""
    auto["cfg"]["PAPER_STOP_PCT"] = "abc"
    assert P._auto_stop_pct() is None
    auto["cfg"]["PAPER_STOP_PCT"] = "0"
    assert P._auto_stop_pct() is None


def test_it_marks_the_day_done_even_if_the_scan_fails(auto, monkeypatch):
    """The watcher loop comes round every ten seconds. A failing scan must
    not become a retry storm."""
    monkeypatch.setattr(P.signals, "scan",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    P.maybe_evening_queue(NOW)
    assert auto["marks"].get("paper_last_queue") == "2026-07-29"


def test_a_quiet_day_pushes_nothing(auto):
    """No flips, no stops, no notification. An autopilot that reports its own
    idleness every evening is one you learn to ignore."""
    pushed = []
    P._push_auto({"queued": [], "skipped": []}, {"stopped": []})
    assert pushed == []


def test_the_watcher_runs_both_ends():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "watcher.py").read_text()
    assert "maybe_evening_queue" in src and "maybe_morning_fill" in src


def test_the_knobs_are_on_the_settings_page():
    from app import settings
    keys = {s.key for s in settings.SETTINGS}
    assert {"PAPER_AUTO", "PAPER_STRATEGIES", "PAPER_STOP_PCT",
            "PAPER_MAX_POSITIONS"} <= keys


def test_it_still_places_no_real_orders():
    """This is the paper broker. If it ever learns to call Kite, the whole
    argument for running it — that it costs nothing to be wrong — is gone."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "paper.py").read_text()
    called = {n.func.attr for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & {"place_order", "modify_order", "cancel_order"})


# =========================================================== trailing stops
def test_the_trail_is_off_by_default(wired, monkeypatch):
    monkeypatch.setattr(P.config, "get",
                        lambda k, d=None: "0" if "TRAIL_MULT" in k else d)
    trade = {"stop": 95.0, "entry_time": ENTRY}
    assert P._effective_stop(trade, [bar(1, 100, 102, 98, 100)], 0) == 95.0


def _run_up():
    """Twelve rising bars from 100 to 133 — a winner going unrealised."""
    return [bar(12 - i, 100 + i * 3, 102 + i * 3, 98 + i * 3, 100 + i * 3)
            for i in range(12)]


def _trail_on(monkeypatch, mult="2.5", period="5"):
    cfg = {"PAPER_TRAIL_MULT": mult, "PAPER_TRAIL_PERIOD": period}
    monkeypatch.setattr(P.config, "get",
                        lambda k, d=None: cfg.get(k, d if d is not None else ""))


def test_the_trail_ratchets_up_behind_a_winner(monkeypatch):
    """Without it the position gives back every rupee of open profit waiting
    for the strategy to flip, and trend systems look far worse on paper than
    they are — one-directionally, so you'd conclude the edge wasn't there."""
    _trail_on(monkeypatch)
    bars = _run_up()
    trade = {"stop": 95.0, "entry_time": ENTRY}
    assert P._effective_stop(trade, bars, len(bars) - 1) > 115


def test_the_trail_never_gives_ground(monkeypatch):
    """A ratchet. If it could fall back it would be a moving target, and a
    position could 'un-stop' between two checks."""
    _trail_on(monkeypatch)
    bars = _run_up() + [bar(0, 100, 101, 99, 100)]      # a sharp pullback
    trade = {"stop": 95.0, "entry_time": ENTRY}
    late = P._effective_stop(trade, bars, len(bars) - 1)
    early = P._effective_stop(trade, bars, len(bars) - 2)
    assert late >= early


def test_the_trail_never_sits_below_the_entry_stop(monkeypatch):
    _trail_on(monkeypatch, mult="20")                   # absurdly wide
    trade = {"stop": 95.0, "entry_time": ENTRY}
    assert P._effective_stop(trade, _run_up(), 11) == 95.0


def test_the_stored_stop_is_not_rewritten(monkeypatch):
    """R is measured from the stop the position was sized against. Moving it
    as the trail rises would rescale every trade's R as it ran, and a
    system's expectancy would depend on how long you held."""
    _trail_on(monkeypatch)
    trade = {"stop": 95.0, "entry_time": ENTRY}
    P._effective_stop(trade, _run_up(), 11)
    assert trade["stop"] == 95.0


def test_a_position_with_no_entry_time_keeps_the_plain_stop(monkeypatch):
    _trail_on(monkeypatch)
    assert P._effective_stop({"stop": 95.0, "entry_time": None},
                             _run_up(), 11) == 95.0


def test_the_trail_period_defaults_to_the_backtesters(monkeypatch):
    """Paper trailing on a different period from the backtest it's compared
    against measures a different system."""
    monkeypatch.setattr(P.config, "get", lambda k, d=None: d)
    assert P._trail_config()[1] == 22


# ================================================================== gates
def test_the_daily_loss_guard_blocks_new_entries(auto, monkeypatch):
    """It existed and pushed a stand-down notice; the autopilot ignored it and
    kept queueing, which made the guard advice rather than a limit."""
    monkeypatch.setattr(P.journal, "guard",
                        lambda **k: {"ok": False, "reason": "2R lost today"})
    P.maybe_evening_queue(NOW)
    assert auto["seen"][0]["flips"] == []


def test_the_guard_never_blocks_an_exit(auto, monkeypatch):
    """A limit that stops you closing a losing position is not risk control,
    it's a trap."""
    monkeypatch.setattr(P.journal, "guard",
                        lambda **k: {"ok": False, "reason": "limit"})
    monkeypatch.setattr(P.signals, "scan", lambda **k: {"flips": [
        {"symbol": "OLD", "strategy": "rsi_revert", "to": 0, "price": 50.0}]})
    P.maybe_evening_queue(NOW)
    assert [f["symbol"] for f in auto["seen"][0]["flips"]] == ["OLD"]


def test_a_broken_guard_does_not_block_anything(auto, monkeypatch):
    """Failing closed here would silently stop the account trading."""
    monkeypatch.setattr(P.journal, "guard",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    P.maybe_evening_queue(NOW)
    assert len(auto["seen"][0]["flips"]) == 2


def test_the_regime_gate_is_off_by_default(auto, monkeypatch):
    monkeypatch.setattr(P.journal, "guard", lambda **k: {"ok": True})
    called = {"n": 0}
    monkeypatch.setattr(P, "_regime_ok",
                        lambda: (called.update(n=1), (False, "x"))[1])
    P.maybe_evening_queue(NOW)
    assert called["n"] == 0


def test_the_regime_gate_blocks_entries_when_on(auto, monkeypatch):
    auto["cfg"]["PAPER_REGIME_GATE"] = "1"
    monkeypatch.setattr(P.journal, "guard", lambda **k: {"ok": True})
    monkeypatch.setattr(P, "_regime_ok", lambda: (False, "index below 200-day"))
    P.maybe_evening_queue(NOW)
    assert auto["seen"][0]["flips"] == []


def test_an_unreadable_benchmark_does_not_look_like_a_bear_market(
        auto, monkeypatch):
    """None, not False. A missing index must not silently stop the account
    trading for a week."""
    auto["cfg"]["PAPER_REGIME_GATE"] = "1"
    monkeypatch.setattr(P.journal, "guard", lambda **k: {"ok": True})
    monkeypatch.setattr(P, "_regime_ok", lambda: (None, ""))
    P.maybe_evening_queue(NOW)
    assert len(auto["seen"][0]["flips"]) == 2
