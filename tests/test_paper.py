"""The paper broker.

Almost every test here is about one rule: **fill at the next open, never the
close that produced the signal**. That's the discipline the whole module
exists to enforce, and it's the exact thing that, done wrong, manufactures an
edge which evaporates the moment real money arrives.

The rest is about not lying by omission: orders surviving a restart, costs on
both legs, and paper rows never blending into real ones.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import db, journal, paper

DAY = 86400


@pytest.fixture(autouse=True)
def clean():
    """Paper rows AND the real ones these tests create.

    paper.reset deliberately spares real trades — which is the point of it —
    so the fixture has to clear those itself or they pile up across tests.
    """
    db.init()

    def wipe():
        paper.reset(confirm=True)
        with db.connect() as conn:
            conn.execute("DELETE FROM trades WHERE symbol IN ('REAL','BEL')")

    wipe()
    yield
    wipe()


def seed_candles(monkeypatch, bars):
    """bars: [(iso_date, open, high, low, close)]"""
    rows = [{"time": int(datetime.fromisoformat(d).timestamp()),
             "open": o, "high": h, "low": lo, "close": c, "volume": 1000}
            for d, o, h, lo, c in bars]
    monkeypatch.setattr(paper.instruments, "lookup",
                        lambda s: {"instrument_token": 1, "tradingsymbol": s})
    monkeypatch.setattr(paper.candles, "get", lambda *a, **k: rows)
    return rows


# ========================================================== the core rule
def test_an_order_does_not_fill_on_the_bar_that_signalled_it(monkeypatch):
    """A flip computed from tonight's close could not have been traded at
    tonight's close. Filling there is the lookahead this module exists to
    avoid."""
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104)])
    paper.queue_order("BEL", "entry", "t", signal_price=104,
                      qty=10, stop=99)
    # the only bar available IS the signal bar
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     [datetime.fromtimestamp(
                         int(datetime.fromisoformat('2026-01-05').timestamp())
                     ).isoformat()])
    out = paper.fill_pending(account_size=100000, risk_pct=1)
    assert out["filled"] == []
    assert out["still_pending"] == 1


def test_it_fills_at_the_next_bars_open(monkeypatch):
    seed_candles(monkeypatch, [
        ("2026-01-05", 100, 105, 99, 104),      # signal bar
        ("2026-01-06", 107, 110, 106, 109),     # fills here, at 107
    ])
    with db.connect() as conn:
        pass
    paper.queue_order("BEL", "entry", "t", signal_price=104, qty=10, stop=99)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    out = paper.fill_pending(account_size=100000, risk_pct=1)
    assert len(out["filled"]) == 1
    assert out["filled"][0]["price"] == 107.0, "did not fill at the next OPEN"


def test_the_gap_between_signal_and_fill_is_kept_not_hidden(monkeypatch):
    """The overnight gap is where a real strategy's profit quietly goes, so
    the fill must be allowed to be worse than the signal price."""
    seed_candles(monkeypatch, [
        ("2026-01-05", 100, 105, 99, 104),
        ("2026-01-06", 96, 99, 95, 98),         # gapped DOWN overnight
    ])
    paper.queue_order("BEL", "entry", "t", signal_price=104, qty=10, stop=90)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    out = paper.fill_pending(account_size=100000, risk_pct=1)
    assert out["filled"][0]["price"] == 96.0


def test_an_order_stays_pending_until_a_bar_exists(monkeypatch):
    """Normal state overnight, not an error."""
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104)])
    paper.queue_order("BEL", "entry", "t", signal_price=104, qty=10, stop=99)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    first = paper.fill_pending(account_size=100000, risk_pct=1)
    assert first["still_pending"] == 1
    assert len(paper.pending()) == 1, "the order was lost while waiting"


# ================================================================ persistence
def test_orders_survive_being_reloaded(monkeypatch):
    """The window between signal and fill is overnight — exactly when the
    process gets restarted. An in-memory queue would drop these silently."""
    paper.queue_order("BEL", "entry", "t", signal_price=100, qty=5, stop=95)
    import importlib
    importlib.reload(paper)
    assert len(paper.pending()) == 1


def test_cancelling_removes_it_from_pending():
    oid = paper.queue_order("BEL", "entry", "t", signal_price=100, qty=5)
    assert paper.cancel(oid)
    assert paper.pending() == []
    assert not paper.cancel(oid), "cancelling twice should report nothing done"


def test_a_filled_order_records_where_it_went(monkeypatch):
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104),
                               ("2026-01-06", 107, 110, 106, 109)])
    paper.queue_order("BEL", "entry", "t", signal_price=104, qty=10, stop=99)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    paper.fill_pending(account_size=100000, risk_pct=1)
    row = paper.orders(1)[0]
    assert row["status"] == "filled"
    assert row["trade_id"] is not None
    assert row["fill_price"] == 107.0


# ==================================================================== sizing
def test_it_sizes_off_the_real_risk_model(monkeypatch):
    """A paper run that bets the account on every signal tests a strategy
    nobody would trade."""
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104),
                               ("2026-01-06", 100, 110, 99, 109)])
    paper.queue_order("BEL", "entry", "t", signal_price=104, stop=98)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    out = paper.fill_pending(account_size=100000, risk_pct=1)
    # 1% of 100k = 1000 risk, 2 rupees a share => ~500 shares
    assert out["filled"][0]["qty"] == pytest.approx(500, abs=2)


def test_an_unsizeable_order_is_cancelled_with_a_reason(monkeypatch):
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104),
                               ("2026-01-06", 100, 110, 99, 109)])
    paper.queue_order("BEL", "entry", "t", signal_price=104)   # no qty, no stop
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    out = paper.fill_pending(account_size=100000, risk_pct=1)
    assert out["problems"]
    assert paper.orders(1)[0]["status"] == "cancelled"


# ===================================================== journal integration
def _fill_entry_then(monkeypatch, exit_bars):
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104),
                               ("2026-01-06", 100, 110, 99, 109)])
    paper.queue_order("BEL", "entry", "st_adx", signal_price=104, qty=10,
                      stop=95)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    paper.fill_pending(account_size=100000, risk_pct=1)
    seed_candles(monkeypatch, exit_bars)


def test_a_filled_entry_becomes_an_open_journal_position(monkeypatch):
    _fill_entry_then(monkeypatch, [])
    assert len(journal.open_positions(source="paper")) == 1


def test_paper_rows_are_marked_as_paper(monkeypatch):
    _fill_entry_then(monkeypatch, [])
    t = journal.open_positions(source="paper")[0]
    assert t["source"] == "paper"


def test_paper_never_blends_into_real_statistics(monkeypatch):
    """The single most misleading number the app could produce: a month of
    flawless simulated trading lifting the figure you judge real results by."""
    _fill_entry_then(monkeypatch, [
        ("2026-01-06", 100, 110, 99, 109),
        ("2026-01-07", 120, 125, 118, 124),
    ])
    paper.queue_order("BEL", "exit", "st_adx", signal_price=109)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=? "
                     "WHERE status='pending'", ["2026-01-06T15:30:00"])
    paper.fill_pending()

    real_only = journal.stats(exclude_source="paper")
    paper_only = journal.stats(source="paper")
    assert paper_only["trades"] == 1
    assert real_only["trades"] == 0, "a paper trade leaked into real stats"


def test_an_exit_closes_the_position_and_scores_it(monkeypatch):
    _fill_entry_then(monkeypatch, [
        ("2026-01-06", 100, 110, 99, 109),
        ("2026-01-07", 120, 125, 118, 124),
    ])
    paper.queue_order("BEL", "exit", "st_adx", signal_price=109)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=? "
                     "WHERE status='pending'", ["2026-01-06T15:30:00"])
    out = paper.fill_pending()
    assert out["filled"][0]["kind"] == "exit"
    assert out["filled"][0]["price"] == 120.0        # next open, again
    assert journal.open_positions(source="paper") == []
    assert out["filled"][0]["r_multiple"] is not None


def test_costs_are_charged_on_both_legs(monkeypatch):
    _fill_entry_then(monkeypatch, [
        ("2026-01-06", 100, 110, 99, 109),
        ("2026-01-07", 120, 125, 118, 124),
    ])
    paper.queue_order("BEL", "exit", "st_adx", signal_price=109)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=? "
                     "WHERE status='pending'", ["2026-01-06T15:30:00"])
    out = paper.fill_pending()
    assert out["filled"][0]["charges"] > 0
    closed = journal.closed_trades(source="paper")[0]
    assert closed["charges"] > 0


def test_exiting_something_not_held_is_reported_not_invented(monkeypatch):
    seed_candles(monkeypatch, [("2026-01-05", 100, 105, 99, 104),
                               ("2026-01-06", 107, 110, 106, 109)])
    paper.queue_order("BEL", "exit", "t", signal_price=104)
    with db.connect() as conn:
        conn.execute("UPDATE paper_orders SET signal_time=?",
                     ["2026-01-05T15:30:00"])
    out = paper.fill_pending()
    assert out["problems"] and "no open paper position" in out["problems"][0]


# ================================================== signals -> orders
FLIPS = {"flips": [
    {"symbol": "AAA", "strategy": "st_adx", "to": 1, "price": 100.0},
    {"symbol": "BBB", "strategy": "st_adx", "to": 0, "price": 50.0},
]}


def test_a_long_flip_queues_an_entry():
    out = paper.from_signals(scan=FLIPS)
    kinds = {q["symbol"]: q["kind"] for q in out["queued"]}
    assert kinds.get("AAA") == "entry"


def test_a_flat_flip_with_nothing_held_is_skipped_not_shorted():
    """Long-only, like the backtester. Inventing a short here would compare
    against strategies that never had one."""
    out = paper.from_signals(scan=FLIPS)
    assert any("BBB" in s for s in out["skipped"])


def test_it_does_not_double_up_on_a_name_already_held(monkeypatch):
    _fill_entry_then(monkeypatch, [])
    out = paper.from_signals(scan={"flips": [
        {"symbol": "BEL", "strategy": "st_adx", "to": 1, "price": 120.0}]})
    assert out["queued"] == []
    assert "already holding" in out["skipped"][0]


def test_a_flip_without_a_price_is_skipped_loudly():
    out = paper.from_signals(scan={"flips": [
        {"symbol": "AAA", "strategy": "t", "to": 1}]})
    assert out["queued"] == [] and out["skipped"]


def test_from_signals_never_fires_the_push(monkeypatch):
    """Queueing paper orders must not send you the EOD alert a second time."""
    seen = {}
    monkeypatch.setattr(paper.signals, "scan",
                        lambda **kw: seen.update(kw) or {"flips": []})
    paper.from_signals()
    assert seen.get("push") is False


# ======================================================== state and reset
def test_state_reports_only_paper(monkeypatch):
    journal.open_trade("REAL", entry=100, qty=1, stop=90)     # a real trade
    _fill_entry_then(monkeypatch, [])
    st = paper.state()
    assert [t["symbol"] for t in st["open"]] == ["BEL"]


def test_reset_needs_confirmation():
    with pytest.raises(paper.PaperError):
        paper.reset()


def test_reset_spares_real_trades(monkeypatch):
    journal.open_trade("REAL", entry=100, qty=1, stop=90)
    _fill_entry_then(monkeypatch, [])
    paper.reset(confirm=True)
    assert journal.open_positions(source="paper") == []
    survivors = [t["symbol"] for t in journal.open_positions()]
    assert "REAL" in survivors, "reset deleted a real trade"


def test_start_is_stamped_once():
    a = paper.start()
    b = paper.start()
    assert a == b, "the start date moved — 'a month of paper' would never end"


def test_summary_is_readable_with_no_history():
    assert "no closed trades yet" in paper.summary()


# ==================================================================== wiring
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.server import app
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_paper_page_loads_and_is_in_the_nav():
    c = _client()
    assert c.get("/paper").status_code == 200
    assert 'href="/paper"' in c.get("/journal").text


def test_page_states_the_next_open_rule():
    """The rule has to be visible on the page, not just true in the code —
    a reader who assumes close-fills will misread every number here."""
    html = _client().get("/paper").text
    assert "next open" in html
    assert "source=paper" in html


def test_api_reports_state():
    r = _client().get("/api/paper")
    assert r.status_code == 200
    assert set(r.json()) >= {"open", "closed", "pending", "stats"}


def test_reset_route_needs_confirmation():
    c = _client()
    assert c.post("/api/paper/reset").status_code == 400
    assert c.post("/api/paper/reset?confirm=true").status_code == 200


def test_paper_writes_are_csrf_protected():
    c = _client()
    r = c.post("/api/paper/fill", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
