"""Offline tests for the trade journal — temp DB, no network.

Run:  python -m pytest tests/test_journal.py
"""
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app import config

_tmp = tempfile.TemporaryDirectory()
config.DB_PATH = Path(_tmp.name) / "journal.db"

from app import db, journal  # noqa: E402  (after DB_PATH override)

db.init()


def approx(a, b, tol=0.02):
    assert abs(a - b) <= tol, f"{a} != {b}"


def clear():
    with db.connect() as c:
        c.execute("DELETE FROM trades")


def setup_function():
    clear()


# ------------------------------------------------------------ open / close
def test_open_and_read_back():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398, tag="st_adx")
    t = journal.get(tid)
    assert t["symbol"] == "BEL" and t["qty"] == 100 and t["open"] is True
    assert t["r_multiple"] is None


def test_close_computes_r():
    """2R winner: entry 405, stop 398 (1R = 7), exit 419."""
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    t = journal.close_trade(tid, exit_price=419, reason="target", charges=0)
    approx(t["r_multiple"], 2.0)
    assert t["open"] is False and t["reason"] == "target"


def test_stop_out_is_minus_one_r():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    t = journal.close_trade(tid, exit_price=398, reason="stop", charges=0)
    approx(t["r_multiple"], -1.0)


def test_short_side_r():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=412, side="short")
    t = journal.close_trade(tid, exit_price=391, charges=0)
    approx(t["r_multiple"], 2.0)


def test_charges_reduce_r():
    """The rule that keeps live comparable to backtested."""
    a = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    gross = journal.close_trade(a, exit_price=419, charges=0)["r_multiple"]
    b = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    net = journal.close_trade(b, exit_price=419)["r_multiple"]   # estimated cost
    assert net < gross, "charges must eat into R"
    assert net > 1.8, "10bps shouldn't cost half an R"


def test_charges_make_a_loser_worse():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    t = journal.close_trade(tid, exit_price=398)
    assert t["r_multiple"] < -1.0


def test_r_measured_against_original_stop_not_trailed():
    """Trailing the stop up must not re-base R — that would inflate every
    winner and make expectancy meaningless."""
    tid = journal.open_trade("BEL", entry=400, qty=100, stop=390)   # 1R = 10
    journal.update_trade(tid, stop=395)                              # trail it up
    t = journal.close_trade(tid, exit_price=420, charges=0)
    # against the ORIGINAL 10-point risk this is +2R, not +4R
    approx(t["r_multiple"], 2.0)


def test_trailed_stop_is_still_visible():
    """You trail it to act on it, so the current level has to be readable —
    it just isn't what R is measured against."""
    tid = journal.open_trade("BEL", entry=400, qty=100, stop=390)
    journal.update_trade(tid, stop=397)
    t = journal.get(tid)
    assert t["stop"] == 397 and t["initial_stop"] == 390


def test_live_r_uses_the_original_basis_too():
    """The intraday number must be on the same scale as the closing one."""
    tid = journal.open_trade("BEL", entry=400, qty=100, stop=390)
    journal.update_trade(tid, stop=398)
    approx(journal.live_r(journal.get(tid), 420), 2.0)


def test_migration_backfills_initial_stop():
    """A journal written before the split must not lose its R basis."""
    with db.connect() as c:
        c.execute("INSERT INTO trades (symbol, side, qty, entry, stop, entry_time) "
                  "VALUES ('OLD','long',10,400,390,'2026-01-01T09:20:00')")
        c.execute("UPDATE trades SET initial_stop=NULL WHERE symbol='OLD'")
    db.init()                                    # runs the migration
    row = [t for t in journal.open_positions("OLD")][0]
    assert row["initial_stop"] == 390


def test_double_close_refused():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    journal.close_trade(tid, exit_price=410, charges=0)
    with pytest.raises(journal.JournalError, match="already closed"):
        journal.close_trade(tid, exit_price=415)


def test_close_unknown_trade():
    with pytest.raises(journal.JournalError, match="no trade"):
        journal.close_trade(9999, exit_price=100)


def test_trade_without_stop_is_recorded_but_unscored():
    """Recording an undisciplined trade beats not recording it."""
    tid = journal.open_trade("BEL", entry=405, qty=100)
    t = journal.close_trade(tid, exit_price=420, charges=0)
    assert t["r_multiple"] is None
    s = journal.stats()
    assert s["trades"] == 0 and s["unscored"] == 1


# ------------------------------------------------------------- bad inputs
def test_long_stop_above_entry_is_caught():
    """Almost always a typo or a mixed-up side, and it would flip every R."""
    with pytest.raises(journal.JournalError, match="side=short"):
        journal.open_trade("BEL", entry=400, qty=10, stop=410, side="long")


def test_short_stop_below_entry_is_caught():
    with pytest.raises(journal.JournalError, match="side=long"):
        journal.open_trade("BEL", entry=400, qty=10, stop=390, side="short")


@pytest.mark.parametrize("kw", [
    dict(entry=0, qty=10), dict(entry=100, qty=0),
    dict(entry=100, qty=10, side="sideways"),
    dict(entry=100, qty=10, stop=-5),
])
def test_bad_open_inputs(kw):
    with pytest.raises(journal.JournalError):
        journal.open_trade("BEL", **kw)


def test_update_rejects_protected_columns():
    tid = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    with pytest.raises(journal.JournalError, match="cannot update"):
        journal.update_trade(tid, r_multiple=5.0)


# ------------------------------------------------------------------ queries
def test_open_positions_and_close_moves_it():
    a = journal.open_trade("BEL", entry=405, qty=100, stop=398)
    journal.open_trade("TCS", entry=3800, qty=10, stop=3700)
    assert len(journal.open_positions()) == 2
    assert len(journal.open_positions("BEL")) == 1
    journal.close_trade(a, exit_price=410, charges=0)
    assert len(journal.open_positions()) == 1
    assert len(journal.closed_trades()) == 1


def test_live_r_on_an_open_trade():
    tid = journal.open_trade("BEL", entry=400, qty=100, stop=390)
    t = journal.get(tid)
    approx(journal.live_r(t, 420), 2.0)
    approx(journal.live_r(t, 395), -0.5)


def test_filter_by_tag():
    a = journal.open_trade("BEL", entry=400, qty=1, stop=390, tag="st_adx")
    b = journal.open_trade("TCS", entry=400, qty=1, stop=390, tag="momentum")
    journal.close_trade(a, exit_price=410, charges=0)
    journal.close_trade(b, exit_price=410, charges=0)
    assert len(journal.closed_trades(tag="st_adx")) == 1


# --------------------------------------------------------------- statistics
def seed(rs, tag=None):
    """Create closed trades with the given R multiples (1R = 10 points)."""
    for i, r in enumerate(rs):
        tid = journal.open_trade("BEL", entry=400, qty=1, stop=390, tag=tag,
                                 entry_time=f"2026-07-{i+1:02d}T09:20:00")
        journal.close_trade(tid, exit_price=400 + r * 10, charges=0,
                            exit_time=f"2026-07-{i+1:02d}T15:20:00")


def test_stats_basic():
    seed([2, -1, 3, -1])
    s = journal.stats()
    assert s["trades"] == 4
    approx(s["win_rate_pct"], 50.0)
    approx(s["expectancy_r"], 0.75)
    approx(s["total_r"], 3.0)
    approx(s["profit_factor"], 2.5)
    approx(s["avg_win_r"], 2.5)
    approx(s["avg_loss_r"], -1.0)


def test_stats_streaks_and_drawdown():
    seed([1, 1, -1, -1, -1, 2])
    s = journal.stats()
    assert s["max_consec_wins"] == 2 and s["max_consec_losses"] == 3
    approx(s["max_drawdown_r"], 3.0)          # +2 peak down to -1


def test_stats_excludes_open_trades():
    seed([2])
    journal.open_trade("TCS", entry=400, qty=1, stop=390)   # still open
    s = journal.stats()
    assert s["trades"] == 1


def test_stats_empty():
    s = journal.stats()
    assert s["trades"] == 0 and s["expectancy_r"] is None
    assert s["total_r"] == 0.0


def test_by_tag_groups_and_keeps_untagged():
    seed([2, 2], tag="st_adx")
    seed([-1], tag="momentum")
    seed([1])                                  # no tag
    g = journal.by_tag()
    assert set(g) == {"st_adx", "momentum", "(untagged)"}
    assert g["st_adx"]["trades"] == 2
    approx(g["st_adx"]["expectancy_r"], 2.0)
    assert g["(untagged)"]["trades"] == 1


# -------------------------------------------------------------- daily guard
def test_realised_r_today_only_counts_today():
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    a = journal.open_trade("BEL", entry=400, qty=1, stop=390)
    journal.close_trade(a, exit_price=380, charges=0,
                        exit_time=f"{today}T15:20:00")
    b = journal.open_trade("TCS", entry=400, qty=1, stop=390)
    journal.close_trade(b, exit_price=380, charges=0,
                        exit_time="2020-01-01T15:20:00")
    approx(journal.realised_r_today(), -2.0)


def test_guard_blocks_after_two_stops():
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    for _ in range(2):
        tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
        journal.close_trade(tid, exit_price=390, charges=0,
                            exit_time=f"{today}T11:00:00")
    g = journal.guard(daily_loss_r=2)
    assert not g["ok"]
    approx(g["realised_r"], -2.0)


def test_guard_counts_open_risk():
    """One stop taken plus one live position reaches a 2R limit."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
    journal.close_trade(tid, exit_price=390, charges=0,
                        exit_time=f"{today}T11:00:00")
    journal.open_trade("TCS", entry=400, qty=1, stop=390)      # 1R live
    g = journal.guard(daily_loss_r=2)
    assert not g["ok"]
    approx(g["open_risk_r"], 1.0)


def test_guard_clean_day_is_ok():
    assert journal.guard(daily_loss_r=2)["ok"]


def test_guard_blocks_on_position_count():
    for i in range(5):
        journal.open_trade(f"S{i}", entry=400, qty=1, stop=390)
    g = journal.guard(daily_loss_r=99, max_positions=5)
    assert not g["ok"] and "already open" in g["reasons"][0]


# ------------------------------------------------------------------ import
def test_import_backtest_trades():
    bt = [
        {"entry": 100, "exit": 110, "entry_time": 1, "exit_time": 2,
         "reason": "signal", "net_pct": 9.8},
        {"entry": 110, "exit": 105, "entry_time": 3, "exit_time": 4,
         "reason": "stop", "net_pct": -4.7},
        {"entry": 105, "exit": 120, "entry_time": 5, "open": True},   # skipped
    ]
    n = journal.import_backtest(bt, symbol="BEL", tag="sma_cross")
    assert n == 2
    rows = journal.closed_trades(tag="sma_cross")
    assert len(rows) == 2
    assert all(r["source"] == "import" for r in rows)


# ---------------------------------------------------------------- rendering
def test_format_open_and_closed():
    tid = journal.open_trade("BEL", entry=400, qty=100, stop=390)
    t = journal.get(tid)
    line = journal.format_trade(t, last_price=420)
    assert "BEL" in line and "+2.00R" in line

    closed = journal.close_trade(tid, exit_price=420, charges=0)
    line = journal.format_trade(closed)
    assert "→ 420" in line and "+2.00R" in line


def test_format_stats_flags_unscored():
    tid = journal.open_trade("BEL", entry=400, qty=1)      # no stop
    journal.close_trade(tid, exit_price=420, charges=0)
    out = journal.format_stats(journal.stats())
    assert "no closed trades" in out and "without a stop" in out


# ------------------------------------------------------- stand-down notice
def close_at(exit_price, entry=400, stop=390, day=None):
    from datetime import datetime as _dt
    day = day or _dt.now().strftime("%Y-%m-%d")
    tid = journal.open_trade("BEL", entry=entry, qty=1, stop=stop)
    return journal.close_trade(tid, exit_price=exit_price, charges=0,
                               exit_time=f"{day}T11:00:00")


def reset_notice():
    db.set_meta("journal_stand_down", "")


def test_no_notice_on_a_clean_day():
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        close_at(420)                                  # a winner
        assert journal.notify_if_breached(daily_loss_r=2) is None
    assert pushed == []


def test_notice_fires_when_the_limit_is_reached():
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        close_at(390); close_at(390)                   # two full stops = -2R
        # the second close already pushed via the automatic hook, so an
        # explicit call here is correctly deduped and returns None
        assert journal.notify_if_breached(daily_loss_r=2) is None
    assert len(pushed) == 1
    assert "Daily limit reached" in pushed[0]
    assert "-2.00R" in pushed[0]
    assert journal.stood_down_today()


def test_notice_fires_automatically_on_close():
    """No caller should have to remember to check — closing is the trigger."""
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        close_at(390)
        assert pushed == []                            # -1R, under the limit
        close_at(390)                                  # -2R, breached
    assert len(pushed) == 1
    assert "Daily limit reached" in pushed[0]


def test_notice_is_sent_once_per_day():
    """A watcher restart loop must not re-push the same notice all afternoon."""
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        close_at(390); close_at(390)                   # auto-push on close
        journal.notify_if_breached(daily_loss_r=2)     # + three explicit calls
        journal.notify_if_breached(daily_loss_r=2)
        journal.notify_if_breached(daily_loss_r=2)
    assert len(pushed) == 1


def test_notice_dedupe_survives_a_restart():
    """The flag lives in the DB, not in memory."""
    clear(); reset_notice()
    with mock.patch("app.notify.send", lambda m, **k: None):
        close_at(390); close_at(390)
        journal.notify_if_breached(daily_loss_r=2)
    assert db.get_meta("journal_stand_down") == \
        __import__("datetime").datetime.now().strftime("%Y-%m-%d")


def test_notice_resets_on_a_new_day():
    clear(); reset_notice()
    db.set_meta("journal_stand_down", "2020-01-01")     # yesterday's flag
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        close_at(390); close_at(390)
    assert len(pushed) == 1


def test_open_risk_can_trip_the_notice():
    """-1.5R realised looks survivable until you count the 1R still live."""
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
        journal.close_trade(tid, exit_price=395, charges=0)      # -0.5R
        tid = journal.open_trade("TCS", entry=400, qty=1, stop=390)
        journal.close_trade(tid, exit_price=390, charges=0)      # -1.5R total
        journal.open_trade("HAL", entry=400, qty=1, stop=390)    # 1R live
        msg = journal.notify_if_breached(daily_loss_r=2)
    assert msg and "still live" in msg


def test_position_count_alone_does_not_push():
    """Hitting max positions is a visible state, not news worth a buzz."""
    clear(); reset_notice()
    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        for i in range(5):
            journal.open_trade(f"S{i}", entry=400, qty=1, stop=390)
        journal.notify_if_breached(daily_loss_r=99, max_positions=5)
    assert pushed == []


def test_failed_push_does_not_mark_as_notified():
    """Otherwise a transient network blip silently eats the day's only notice."""
    clear(); reset_notice()

    def dead(msg, **k):
        raise OSError("no route to host")

    with mock.patch("app.notify.send", dead):
        close_at(390); close_at(390)
        assert journal.notify_if_breached(daily_loss_r=2) is None
    assert not journal.stood_down_today()

    pushed = []
    with mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        assert journal.notify_if_breached(daily_loss_r=2)       # retries
    assert len(pushed) == 1


def test_failed_push_does_not_break_the_close():
    """A trade must still be recorded even if the notice can't be sent."""
    clear(); reset_notice()

    def dead(msg, **k):
        raise OSError("down")

    with mock.patch("app.notify.send", dead):
        close_at(390)
        t = close_at(390)
    assert t["exit"] == 390 and t["r_multiple"] is not None


# ------------------------------------------------- open risk vs a moved stop
def test_open_risk_falls_as_the_stop_is_trailed():
    """A trailed stop has less left to lose, and the guard must see that.

    Counting a flat 1R per open position spends the day's budget on risk that
    no longer exists — the guard stands you down while you are, in fact,
    risking nothing.
    """
    clear()
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
    approx(journal.open_risk_r(), 1.0)

    journal.update_trade(tid, stop=395)                  # half way up
    approx(journal.open_risk_r(), 0.5)

    journal.update_trade(tid, stop=400)                  # breakeven
    approx(journal.open_risk_r(), 0.0)


def test_open_risk_never_goes_negative():
    """Locked-in profit is not a licence to lose more elsewhere."""
    clear()
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
    journal.update_trade(tid, stop=420)                  # +2R locked in
    assert journal.open_risk_r() == 0.0


def test_open_risk_is_measured_in_original_r():
    """The unit stays the entry-time 1R, so it lines up with everything else."""
    clear()
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=380)   # 1R = 20
    journal.update_trade(tid, stop=390)                  # 10 left of 20
    approx(journal.open_risk_r(), 0.5)


def test_open_risk_handles_shorts():
    clear()
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=410, side="short")
    approx(journal.open_risk_r(), 1.0)
    journal.update_trade(tid, stop=405)
    approx(journal.open_risk_r(), 0.5)
    journal.update_trade(tid, stop=395)                  # past breakeven
    assert journal.open_risk_r() == 0.0


def test_open_risk_ignores_positions_without_a_stop():
    """Undefined risk can't be summed — same exclusion stats makes."""
    clear()
    journal.open_trade("BEL", entry=400, qty=1)          # no stop
    assert journal.open_risk_r() == 0.0
