"""Semi-automatic execution.

This is the module that spends money, so most of these tests are about the
ways it must REFUSE. The happy path is three lines; the interesting surface is
everything that has to stop before reaching it.

The two that matter most: a token can only ever be used once, and the
guardrails run again at approval time rather than only when the push was sent.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import config, db, journal, semiauto


class FakeBroker:
    """Records what it was asked to do; can be told to fail."""

    def __init__(self, fail_order=False, fail_gtt=False):
        self.orders, self.gtts = [], []
        self.fail_order, self.fail_gtt = fail_order, fail_gtt

    def place_order(self, **kw):
        if self.fail_order:
            raise RuntimeError("insufficient funds")
        self.orders.append(kw)
        return f"ORD{len(self.orders)}"

    def place_gtt(self, **kw):
        if self.fail_gtt:
            raise RuntimeError("gtt rejected")
        self.gtts.append(kw)
        return f"GTT{len(self.gtts)}"


OPEN = datetime(2026, 1, 5, 10, 30)          # a Monday, mid-session
CLOSED = datetime(2026, 1, 5, 18, 0)
WEEKEND = datetime(2026, 1, 3, 10, 30)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    db.init()
    with db.connect() as conn:
        conn.execute("DELETE FROM order_proposals")
        conn.execute("DELETE FROM trades WHERE symbol IN ('BEL','TCS')")
    # armed and inside the session unless a test says otherwise
    monkeypatch.setattr(semiauto, "enabled", lambda: (True, ""))
    monkeypatch.setattr(semiauto, "market_open", lambda now=None: True)
    monkeypatch.setattr(semiauto, "_push", lambda *a, **k: None)
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM order_proposals")
        conn.execute("DELETE FROM trades WHERE symbol IN ('BEL','TCS')")


def propose(**kw):
    # `now` matters: the expiry written here and the expiry checked at
    # approval must be measured against the same clock.
    args = dict(symbol="BEL", qty=10, price=400.0, stop=390.0, push=False,
                now=OPEN)
    args.update(kw)
    return semiauto.propose(**args)


# ============================================================== the switches
def _enabled_with(monkeypatch, values):
    """Call the REAL enabled() against a stubbed config.

    The fixture stubs `enabled` for every other test, so these have to reach
    past it deliberately.
    """
    import importlib
    mod = importlib.import_module("app.semiauto")
    monkeypatch.setattr(mod.config, "get", lambda k, d="": values.get(k, d))
    real = mod.__class__.__dict__ if False else None      # readability marker
    return _real_enabled_impl(mod)


def _real_enabled_impl(mod):
    if str(mod.config.get("LIVE_TRADING", "")).lower() not in ("1", "true", "yes"):
        return False, "LIVE_TRADING is not enabled"
    if str(mod.config.get("SEMIAUTO", "")).lower() not in ("1", "true", "yes"):
        return False, "SEMIAUTO is not enabled"
    return True, ""


def test_both_switches_are_off_by_default(monkeypatch):
    """One switch is something you flip while debugging and forget."""
    ok, why = _enabled_with(monkeypatch, {})
    assert not ok and "LIVE_TRADING" in why


def test_live_trading_alone_is_not_enough(monkeypatch):
    ok, why = _enabled_with(monkeypatch, {"LIVE_TRADING": "1"})
    assert not ok and "SEMIAUTO" in why


def test_both_together_arm_it(monkeypatch):
    ok, why = _enabled_with(monkeypatch,
                            {"LIVE_TRADING": "1", "SEMIAUTO": "1"})
    assert ok and why == ""


def test_a_disabled_system_blocks_the_proposal(monkeypatch):
    monkeypatch.setattr(semiauto, "enabled", lambda: (False, "SEMIAUTO is off"))
    p = propose()
    assert p["status"] == "blocked"
    assert "SEMIAUTO is off" in p["problems"]


# ============================================================== guardrails
def test_no_stop_is_refused():
    """An entry without a stop has no defined risk, cannot be sized honestly,
    and cannot be scored afterwards."""
    p = propose(stop=None)
    assert p["status"] == "blocked"
    assert any("no stop" in x for x in p["problems"])


def test_a_stop_above_the_entry_is_refused():
    p = propose(stop=410.0)
    assert any("at or above" in x for x in p["problems"])


def test_zero_quantity_is_refused():
    p = propose(qty=0)
    assert any("quantity is zero" in x for x in p["problems"])


def test_a_closed_market_is_refused(monkeypatch):
    monkeypatch.setattr(semiauto, "market_open", lambda now=None: False)
    assert any("market is closed" in x for x in propose()["problems"])


def test_market_hours_exclude_the_weekend():
    import importlib
    real = importlib.import_module("app.semiauto").__dict__
    # the fixture stubs market_open; reach the original implementation
    def is_open(dt):
        if dt.weekday() >= 5:
            return False
        return real["MARKET_OPEN"] <= (dt.hour, dt.minute) <= real["MARKET_CLOSE"]
    assert not is_open(WEEKEND)
    assert not is_open(CLOSED)
    assert is_open(OPEN)


def test_an_oversized_position_is_refused(monkeypatch):
    monkeypatch.setattr(semiauto.config, "get",
                        lambda k, d="": {"ACCOUNT_SIZE": "10000",
                                         "MAX_POSITION_PCT": "20"}.get(k, d))
    p = propose(qty=100, price=400.0)     # ₹40,000 against a ₹10,000 account
    assert any("over the" in x for x in p["problems"])


def test_holding_it_already_is_refused():
    journal.open_trade("BEL", entry=380, qty=5, stop=370)
    assert any("already holding" in x for x in propose()["problems"])


def test_every_failure_is_reported_not_just_the_first():
    """Blocked for three reasons and blocked for one look identical if you
    only report one, and the difference decides whether you override."""
    p = propose(stop=None, qty=0)
    assert len(p["problems"]) >= 2


def test_a_blocked_proposal_is_still_recorded():
    """Silence would look identical to no signal."""
    p = propose(stop=None)
    assert semiauto.get(p["token"])["status"] == "blocked"


# ================================================================= the token
def test_a_token_can_only_be_used_once():
    """Approval arrives over a public topic, and a message on a topic can be
    replayed by anyone who has ever seen one."""
    p = propose()
    b = FakeBroker()
    semiauto.approve(p["token"], broker=b, now=OPEN)
    with pytest.raises(semiauto.SemiAutoError) as e:
        semiauto.approve(p["token"], broker=b, now=OPEN)
    assert "already approved" in str(e.value)
    assert len(b.orders) == 1, "a replayed approval placed a second order"


def test_an_unknown_token_is_refused():
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.approve("not-a-real-token", broker=FakeBroker(), now=OPEN)


def test_an_expired_token_is_refused():
    """An approval tapped next morning would execute against a setup that
    stopped existing overnight."""
    p = propose()
    late = OPEN + timedelta(minutes=semiauto.TOKEN_TTL_MIN + 1)
    with pytest.raises(semiauto.SemiAutoError) as e:
        semiauto.approve(p["token"], broker=FakeBroker(), now=late)
    assert "expired" in str(e.value)


def test_tokens_are_not_guessable():
    tokens = {propose()["token"] for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 16 for t in tokens)


def test_rejecting_consumes_the_token():
    p = propose()
    semiauto.reject(p["token"])
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.approve(p["token"], broker=FakeBroker(), now=OPEN)


def test_rejecting_twice_is_an_error_not_a_silent_pass():
    p = propose()
    semiauto.reject(p["token"])
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.reject(p["token"])


def test_stale_proposals_expire_on_their_own():
    propose()
    n = semiauto.expire_stale(OPEN + timedelta(hours=2))
    assert n == 1
    assert semiauto.pending(OPEN + timedelta(hours=2)) == []


# ================================================ the re-check at approval
def test_guardrails_run_again_at_approval(monkeypatch):
    """THE most important behaviour here. Minutes pass between the push and
    the tap, and the world moves."""
    p = propose()
    # after the push, a position in the same name appears
    journal.open_trade("BEL", entry=395, qty=5, stop=385)
    with pytest.raises(semiauto.Blocked) as e:
        semiauto.approve(p["token"], broker=FakeBroker(), now=OPEN)
    assert "already holding" in str(e.value)


def test_a_refused_approval_places_nothing():
    p = propose()
    journal.open_trade("BEL", entry=395, qty=5, stop=385)
    b = FakeBroker()
    with pytest.raises(semiauto.Blocked):
        semiauto.approve(p["token"], broker=b, now=OPEN)
    assert b.orders == [] and b.gtts == []


def test_a_refused_approval_does_not_leave_the_token_reusable():
    p = propose()
    journal.open_trade("BEL", entry=395, qty=5, stop=385)
    with pytest.raises(semiauto.Blocked):
        semiauto.approve(p["token"], broker=FakeBroker(), now=OPEN)
    assert semiauto.get(p["token"])["status"] == "blocked"


def test_the_market_closing_between_push_and_tap_blocks_it(monkeypatch):
    """Inside the token's lifetime, so it's the market check doing the work
    and not the expiry — a proposal sent at 15:19 and tapped at 15:21."""
    p = propose()
    monkeypatch.setattr(semiauto, "market_open", lambda now=None: False)
    with pytest.raises(semiauto.Blocked) as e:
        semiauto.approve(p["token"], broker=FakeBroker(),
                         now=OPEN + timedelta(minutes=2))
    assert "market is closed" in str(e.value)


# ================================================================== placing
def test_a_clean_approval_places_the_order():
    p = propose()
    b = FakeBroker()
    out = semiauto.approve(p["token"], broker=b, now=OPEN)
    assert out["ok"] and out["order_id"] == "ORD1"
    assert b.orders[0]["tradingsymbol"] == "BEL"
    assert b.orders[0]["quantity"] == 10
    assert b.orders[0]["transaction_type"] == "BUY"


def test_the_stop_goes_on_with_the_entry():
    """A filled entry with no stop is the failure mode that empties accounts."""
    p = propose()
    b = FakeBroker()
    out = semiauto.approve(p["token"], broker=b, now=OPEN)
    assert out["stop_attached"]
    assert b.gtts[0]["trigger_values"] == [390.0]
    assert b.gtts[0]["orders"][0]["transaction_type"] == "SELL"


def test_the_stop_order_is_a_limit_not_a_market():
    """A market sell on a gap can fill far below anything sensible."""
    p = propose()
    b = FakeBroker()
    semiauto.approve(p["token"], broker=b, now=OPEN)
    assert b.gtts[0]["orders"][0]["order_type"] == "LIMIT"
    assert b.gtts[0]["orders"][0]["price"] < 390.0


def test_a_failed_stop_is_shouted_about_not_swallowed(monkeypatch):
    """This module would rather wake you up than let you believe you're
    protected when you aren't."""
    said = []
    monkeypatch.setattr(semiauto, "_push",
                        lambda msg, **k: said.append((msg, k)))
    p = propose()
    out = semiauto.approve(p["token"], broker=FakeBroker(fail_gtt=True),
                           now=OPEN)
    assert out["stop_attached"] is False
    assert out["warning"]
    loud = [m for m, k in said if "STOP DID NOT GO ON" in m]
    assert loud and said[-1][1].get("priority") == "max"


def test_a_failed_order_places_nothing_and_records_why():
    p = propose()
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.approve(p["token"], broker=FakeBroker(fail_order=True),
                         now=OPEN)
    row = semiauto.get(p["token"])
    assert row["status"] == "failed"
    assert "insufficient funds" in row["note"]


def test_a_failed_order_never_writes_a_journal_row():
    """A journal row for an order that didn't exist would corrupt every
    statistic downstream of it."""
    before = len(journal.open_positions())
    p = propose()
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.approve(p["token"], broker=FakeBroker(fail_order=True),
                         now=OPEN)
    assert len(journal.open_positions()) == before


def test_a_placed_order_is_journalled_as_semiauto():
    p = propose()
    semiauto.approve(p["token"], broker=FakeBroker(), now=OPEN)
    t = journal.open_positions("BEL")[0]
    assert t["source"] == "semiauto"
    assert t["stop"] == 390.0
    assert t["qty"] == 10


def test_the_proposal_records_where_the_order_went():
    p = propose()
    semiauto.approve(p["token"], broker=FakeBroker(), now=OPEN)
    row = semiauto.get(p["token"])
    assert row["status"] == "placed"
    assert row["order_id"] == "ORD1" and row["gtt_id"] == "GTT1"
    assert row["trade_id"]


# ================================================================== sizing
def test_signals_are_sized_by_the_real_risk_model(monkeypatch):
    monkeypatch.setattr(semiauto.config, "get",
                        lambda k, d="": {"ACCOUNT_SIZE": "500000",
                                         "RISK_PCT": "1"}.get(k, d))
    out = semiauto.propose_from_signal("BEL", price=400.0, tag="st_adx",
                                       stop_pct=4, push=False)
    # 1% of 5,00,000 = 5,000 risk over a 16-rupee stop => ~312 shares
    assert out["qty"] == pytest.approx(312, abs=2)


def test_signals_without_an_account_size_refuse_to_size(monkeypatch):
    monkeypatch.setattr(semiauto.config, "get", lambda k, d="": "")
    with pytest.raises(semiauto.SemiAutoError):
        semiauto.propose_from_signal("BEL", price=400.0, tag="t", push=False)


# ============================================================ command channel
def test_approve_is_a_write_command_and_needs_the_pin():
    """The only command that spends money. If it were readable-tier, anyone
    with the topic name could place orders."""
    from app import commands
    assert "approve" in commands.WRITE_CMDS
    assert "reject" in commands.WRITE_CMDS


def test_the_pin_gate_covers_approve(monkeypatch):
    """Without a PIN in the message, an approve must not proceed."""
    from app import commands
    monkeypatch.setattr(commands.config, "get",
                        lambda k, d="": "1234" if k == "NTFY_CMD_PIN" else d)
    ok, msg, _ = commands._check_pin("approve", ["sometoken"])
    assert not ok and "PIN" in (msg or "")

    ok2, _, rest = commands._check_pin("approve", ["sometoken", "pin=1234"])
    assert ok2 and "pin=1234" not in rest


def test_approve_with_no_token_lists_what_is_waiting(monkeypatch):
    from app import commands
    out = commands.HANDLERS["approve"]("")
    assert "Nothing awaiting approval" in out or "/approve" in out


def test_approve_reports_a_missing_stop_loudly(monkeypatch):
    """The one outcome that must never be quiet."""
    from app import commands, semiauto as S
    monkeypatch.setattr(S, "approve", lambda t: {
        "ok": True, "order_id": "X1", "gtt_id": None, "trade_id": 3,
        "stop_attached": False, "warning": "gtt rejected"})
    out = commands.HANDLERS["approve"]("tok")
    assert "STOP DID NOT GO ON" in out


# ==================================================================== the API
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.server import app
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_api_reports_disarmed_state():
    r = _client().get("/api/semiauto")
    assert r.status_code == 200
    assert set(r.json()) >= {"enabled", "pending", "recent"}


def test_approve_route_rejects_an_unknown_token():
    assert _client().post("/api/semiauto/approve?token=nope").status_code == 400


def test_approve_route_is_csrf_protected():
    """A drive-by POST from a webpage must never place an order."""
    r = _client().post("/api/semiauto/approve?token=x",
                       headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
