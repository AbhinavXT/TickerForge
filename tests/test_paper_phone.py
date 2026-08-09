"""Paper trading without being at the machine.

The two halves of the paper loop land at the two times you are least likely to
be at the keyboard: the queue is built after the 15:35 close, and the fill
needs a bar that only exists after the 09:15 open. A paper experiment you have
to be home for is one that quietly stops after a fortnight, and the month of
live-vs-backtest data it exists to produce never arrives.

So: a phone command, and an automatic fill at 09:20.

The subtle one is the top-up. `fill_pending` reads STORED candles and never
fetches. Without a top-up first, the morning fill finds no bar for today,
reports every order as still pending, and reads exactly like a quiet morning
with nothing to do — a silent no-op wearing the shape of a successful run.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import commands as C
from app import paper as P


# ============================================================ the phone command
def test_paper_is_dispatched_at_all():
    assert "paper" in C.MULTI_ARG


def test_bare_paper_reports_without_writing(monkeypatch):
    called = {"fill": 0, "queue": 0}
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: called.update(fill=1) or {})
    monkeypatch.setattr(P, "from_signals",
                        lambda *a, **k: called.update(queue=1) or {})
    out = C.handle("/paper")
    assert out and called == {"fill": 0, "queue": 0}


def test_unknown_subcommand_explains_itself():
    out = C.handle("/paper wibble")
    assert "/paper fill" in out and "queue" in out


def test_fill_tops_up_the_candles_first(monkeypatch):
    """The whole reason this isn't just a wrapper around fill_pending. Stored
    bars only; no top-up means no bar for today means everything 'pending'."""
    order = []
    monkeypatch.setattr(C, "_topup_today",
                        lambda: (order.append("topup"), (True, ""))[1])
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: (order.append("fill"),
                                         {"filled": [], "still_pending": 0,
                                          "problems": []})[1])
    C.handle("/paper fill")
    assert order == ["topup", "fill"]


def test_a_failed_topup_is_named_in_the_reply(monkeypatch):
    """"3 still waiting" before the open is normal. At 10:00 it means the
    top-up didn't land, and the reply has to say which one you're looking at."""
    monkeypatch.setattr(C, "_topup_today", lambda: (False, "no Kite session"))
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: {"filled": [], "still_pending": 3,
                                         "problems": []})
    out = C.handle("/paper fill")
    assert "no Kite session" in out


def test_a_clean_fill_lists_what_filled(monkeypatch):
    monkeypatch.setattr(C, "_topup_today", lambda: (True, ""))
    monkeypatch.setattr(P, "fill_pending", lambda *a, **k: {
        "filled": [{"kind": "entry", "symbol": "BEL", "qty": 71,
                    "price": 405.5, "r_multiple": None}],
        "still_pending": 0, "problems": []})
    out = C.handle("/paper fill")
    assert "BEL" in out and "71" in out


def test_queue_passes_the_stop_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(P, "start", lambda: "x")
    monkeypatch.setattr(P, "from_signals",
                        lambda stop_pct=None: seen.update(sl=stop_pct)
                        or {"queued": [], "skipped": [], "flips": 0})
    C.handle("/paper queue sl=4")
    assert seen["sl"] == 4.0


def test_queue_says_when_the_fill_happens(monkeypatch):
    """Queuing produces nothing visible until tomorrow. Without saying so the
    reply looks like a failure."""
    monkeypatch.setattr(P, "start", lambda: "x")
    monkeypatch.setattr(P, "from_signals", lambda stop_pct=None: {
        "queued": [{"kind": "entry", "symbol": "BEL", "tag": "st_adx"}],
        "skipped": [], "flips": 1})
    out = C.handle("/paper queue")
    assert "NEXT open" in out


# ==================================================================== the PIN
def test_fill_and_queue_are_treated_as_writes():
    assert ("paper", "fill") in C.WRITE_SUBCMDS
    assert ("paper", "queue") in C.WRITE_SUBCMDS


def test_a_pin_is_demanded_for_the_writes_but_not_the_status(monkeypatch):
    """Guarding the whole command would put a PIN in front of a status check
    you want to glance at one-handed; guarding none of it leaves order
    creation open."""
    monkeypatch.setattr(C.config, "get",
                        lambda k, d=None: "1234" if k == "NTFY_CMD_PIN" else d)
    ok, _why, _rest = C._check_pin("paper", [])
    assert ok is True
    ok, why, _rest = C._check_pin("paper", ["fill"])
    assert ok is False and "PIN" in why
    ok, _why, rest = C._check_pin("paper", ["fill", "pin=1234"])
    assert ok is True and rest == ["fill"]


# ========================================================= the automatic fill
def test_it_waits_until_after_the_open(monkeypatch):
    """07:30 is before the market opens, so no bar for today exists and
    nothing can fill. It's also when the Kite token dies."""
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    assert P.maybe_morning_fill(datetime(2026, 7, 29, 7, 30)) is False


def test_it_does_not_fire_at_the_bell(monkeypatch):
    """09:15:00 is a coin flip on whether the first bar has been written.
    09:20 costs five minutes and removes the race."""
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    assert P.maybe_morning_fill(datetime(2026, 7, 29, 9, 15)) is False
    assert P._FILL_HM == (9, 20)


def test_it_does_not_fire_at_the_weekend(monkeypatch):
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    assert P.maybe_morning_fill(datetime(2026, 8, 1, 10, 0)) is False


def test_an_empty_queue_does_not_burn_the_days_slot(monkeypatch):
    """Marked-done-once-a-day is the right guard for a run that ATTEMPTED
    something. Spending it on a morning with nothing queued would mean an
    order queued at 09:30 waits until tomorrow."""
    monkeypatch.setattr(P, "pending", lambda *a, **k: [])
    monkeypatch.setattr(P.db, "set_meta",
                        lambda *a: pytest.fail("burned the slot"))
    assert P.maybe_morning_fill(datetime(2026, 7, 29, 9, 30)) is False


def test_it_runs_once_a_day_even_if_it_fails(monkeypatch):
    """A dead session at 09:20 must not become a retry storm — the watcher
    loop comes round every ten seconds."""
    marks = {}
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    monkeypatch.setattr(P.db, "get_meta", lambda k: marks.get(k))
    monkeypatch.setattr(P.db, "set_meta",
                        lambda k, v: marks.__setitem__(k, v))
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no session")))
    now = datetime(2026, 7, 29, 9, 30)
    P.maybe_morning_fill(now)
    assert marks.get("paper_last_fill") == "2026-07-29"
    assert P.maybe_morning_fill(now) is False


def test_it_tops_up_before_filling(monkeypatch):
    order = []
    marks = {}
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    monkeypatch.setattr(P.db, "get_meta", lambda k: marks.get(k))
    monkeypatch.setattr(P.db, "set_meta", lambda k, v: marks.__setitem__(k, v))
    monkeypatch.setattr("app.maintenance.topup_candles",
                        lambda *a, **k: order.append("topup"))
    monkeypatch.setattr("app.kite_auth.get_kite", lambda: object())
    monkeypatch.setattr("app.watchlist.items", lambda: [])
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: (order.append("fill"),
                                         {"filled": [], "still_pending": 0,
                                          "problems": []})[1])
    P.maybe_morning_fill(datetime(2026, 7, 29, 9, 30))
    assert order == ["topup", "fill"]


def test_a_broken_topup_does_not_stop_the_fill(monkeypatch):
    """Some symbols may already be topped up by the watcher. A failed fetch
    should cost accuracy, not the whole morning."""
    marks = {}
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    monkeypatch.setattr(P.db, "get_meta", lambda k: marks.get(k))
    monkeypatch.setattr(P.db, "set_meta", lambda k, v: marks.__setitem__(k, v))
    monkeypatch.setattr("app.kite_auth.get_kite",
                        lambda: (_ for _ in ()).throw(SystemExit(1)))
    done = {}
    monkeypatch.setattr(P, "fill_pending",
                        lambda *a, **k: done.update(ran=1) or {
                            "filled": [], "still_pending": 1, "problems": []})
    P.maybe_morning_fill(datetime(2026, 7, 29, 9, 30))
    assert done.get("ran") == 1


def test_fills_are_pushed_to_the_phone(monkeypatch):
    """The point of automating it. A fill nobody is told about is a position
    you find out about from the P&L."""
    marks, pushed = {}, {}
    monkeypatch.setattr(P, "pending", lambda *a, **k: [{"symbol": "BEL"}])
    monkeypatch.setattr(P.db, "get_meta", lambda k: marks.get(k))
    monkeypatch.setattr(P.db, "set_meta", lambda k, v: marks.__setitem__(k, v))
    monkeypatch.setattr("app.kite_auth.get_kite", lambda: object())
    monkeypatch.setattr("app.watchlist.items", lambda: [])
    monkeypatch.setattr("app.maintenance.topup_candles", lambda *a, **k: None)
    monkeypatch.setattr(P, "fill_pending", lambda *a, **k: {
        "filled": [{"kind": "entry", "symbol": "BEL", "qty": 71,
                    "price": 405.5}],
        "still_pending": 0, "problems": []})
    monkeypatch.setattr(P.notify, "send",
                        lambda m, **k: pushed.update(body=m))
    P.maybe_morning_fill(datetime(2026, 7, 29, 9, 30))
    assert "BEL" in pushed.get("body", "")


def test_the_watcher_calls_it():
    """The hook is the only thing making any of this automatic."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "app" / "watcher.py").read_text()
    assert "maybe_morning_fill" in src
