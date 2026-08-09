"""Offline tests for the phone command dispatcher — no ntfy, no Kite.

Run:  python -m pytest tests/test_commands.py
"""
from unittest import mock

from app import commands


def stub_handlers():
    """Swap the real handlers for echoes so we test dispatch, not market data."""
    return (
        mock.patch.dict(commands.HANDLERS,
                        {"price": lambda a: f"price:{a}",
                         "unalert": lambda a: f"unalert:{a}"}, clear=False),
        mock.patch.dict(commands.MULTI_ARG,
                        {"alert": lambda parts: f"alert:{' '.join(parts)}"},
                        clear=False),
    )


def run(text, pin=""):
    h, m = stub_handlers()
    with h, m, mock.patch.object(commands.config, "NTFY_CMD_PIN", pin):
        return commands.handle(text)


# ------------------------------------------------------------- no PIN set
def test_writes_pass_when_no_pin_configured():
    """Default setup must keep working — this is the daily workflow."""
    assert run("/alert BEL rsi > 70") == "alert:BEL rsi > 70"
    assert run("/unalert all") == "unalert:all"


def test_non_commands_ignored():
    assert commands.handle("just chatter") is None
    assert commands.handle("") is None


# ---------------------------------------------------------------- PIN set
def test_write_refused_without_pin():
    reply = run("/alert BEL rsi > 70", pin="4821")
    assert "needs a PIN" in reply


def test_write_refused_with_wrong_pin():
    reply = run("/unalert all pin=0000", pin="4821")
    assert "needs a PIN" in reply


def test_write_allowed_with_correct_pin():
    assert run("/alert BEL rsi > 70 pin=4821", pin="4821") == "alert:BEL rsi > 70"


def test_pin_position_does_not_matter():
    assert run("/alert pin=4821 BEL rsi > 70", pin="4821") == "alert:BEL rsi > 70"


def test_reads_never_need_a_pin():
    """A PIN on writes must not lock you out of the queries you use hourly."""
    assert run("/price BEL", pin="4821") == "price:BEL"


def test_pin_is_stripped_from_arguments():
    """The handler must never see pin= as a symbol or condition token."""
    assert run("/price BEL pin=4821", pin="4821") == "price:BEL"


# --------------------------------------------------------------- hygiene
def test_write_set_matches_dispatch_tables():
    """Every write command must actually be dispatchable, or the gate guards
    a name that no longer exists."""
    known = set(commands.HANDLERS) | set(commands.MULTI_ARG)
    assert commands.WRITE_CMDS <= known


def test_redaction_hides_the_pin():
    assert commands._redact("/alert BEL pin=4821") == "/alert BEL pin=***"
    assert "4821" not in commands._redact("/unalert all pin=4821")


# ------------------------------------------------ risk & journal commands
import tempfile          # noqa: E402
from pathlib import Path  # noqa: E402

commands.config.DB_PATH = Path(tempfile.mkdtemp()) / "cmds.db"
from app import db, journal  # noqa: E402

db.init()

_INST = {"instrument_token": 1, "tradingsymbol": "BEL", "name": "Bharat Elec"}


def with_symbol():
    return mock.patch.object(commands.instruments, "lookup",
                             lambda s: _INST if s else None)


def with_account(size=500_000, risk_pct=1):
    return mock.patch.multiple(commands.config, ACCOUNT_SIZE=size,
                               RISK_PCT=risk_pct, DAILY_LOSS_R=2,
                               MAX_POSITIONS=5, MAX_POSITION_PCT=100,
                               NTFY_CMD_PIN="")


def clear_trades():
    with db.connect() as c:
        c.execute("DELETE FROM trades")


def test_size_command():
    clear_trades()
    with with_symbol(), with_account():
        out = commands.handle("/size BEL entry=405 sl=398")
    assert "LONG 714" in out and "1R = ₹7" in out


def test_size_respects_risk_override():
    with with_symbol(), with_account():
        one = commands.handle("/size BEL entry=405 sl=398")
        two = commands.handle("/size BEL entry=405 sl=398 risk=0.5")
    assert "714" in one and "357" in two


def test_size_without_account_says_so():
    with with_symbol(), with_account(size=0):
        out = commands.handle("/size BEL entry=405 sl=398")
    assert "ACCOUNT_SIZE" in out


def test_size_accepts_positional_prices():
    with with_symbol(), with_account():
        out = commands.handle("/size BEL 405 398")
    assert "LONG 714" in out


def test_size_writes_nothing():
    """It's a calculator. If it logged a trade, /size would be dangerous."""
    clear_trades()
    with with_symbol(), with_account():
        commands.handle("/size BEL entry=405 sl=398")
    assert journal.open_positions() == []


def test_log_close_roundtrip():
    clear_trades()
    with with_symbol(), with_account():
        out = commands.handle("/log buy BEL 405 sl=398 tag=st_adx")
        assert "Logged" in out and "714" in out
        tid = journal.open_positions()[0]["id"]
        out = commands.handle(f"/close {tid} 419 reason=target")
    assert "+1.88R" in out or "+1.9" in out
    assert "left before the limit" in out


def test_log_sizes_from_risk_when_qty_omitted():
    clear_trades()
    with with_symbol(), with_account():
        commands.handle("/log buy BEL 405 sl=398")
    assert journal.open_positions()[0]["qty"] == 714


def test_log_without_stop_warns():
    clear_trades()
    with with_symbol(), with_account():
        out = commands.handle("/log buy BEL 405 qty=10")
    assert "no stop" in out and "won't count" in out


def test_log_rejects_wrong_side_stop():
    clear_trades()
    with with_symbol(), with_account():
        out = commands.handle("/log buy BEL 405 sl=412")
    assert "Not logged" in out and "side=short" in out


def test_amend_trails_stop_but_keeps_r_basis():
    clear_trades()
    with with_symbol(), with_account():
        commands.handle("/log buy BEL 400 sl=390 qty=10")
        tid = journal.open_positions()[0]["id"]
        out = commands.handle(f"/amend {tid} sl=395")
        assert "entry stop 390" in out
        out = commands.handle(f"/close {tid} 420 charges=0")
    assert "+2.00R" in out          # not +4R


def test_journal_command_shows_open_and_guard():
    clear_trades()
    with with_symbol(), with_account():
        commands.handle("/log buy BEL 400 sl=390 qty=10")
        out = commands.handle("/journal")
    assert "Open (1)" in out and "to the limit" in out


def test_journal_stats_groups_by_tag():
    clear_trades()
    with with_symbol(), with_account():
        commands.handle("/log buy BEL 400 sl=390 qty=10 tag=st_adx")
        tid = journal.open_positions()[0]["id"]
        commands.handle(f"/close {tid} 420 charges=0")
        out = commands.handle("/journal stats")
    assert "st_adx" in out and "+2.00R" in out


def test_guard_appears_once_the_limit_is_hit():
    clear_trades()
    with with_symbol(), with_account():
        for _ in range(2):
            commands.handle("/log buy BEL 400 sl=390 qty=10")
            tid = journal.open_positions()[0]["id"]
            commands.handle(f"/close {tid} 390 charges=0")
        out = commands.handle("/journal")
    assert "⛔" in out and "daily loss limit" in out


def test_size_warns_when_guard_is_breached():
    """The nudge lands where it's useful — as you size the next trade."""
    clear_trades()
    with with_symbol(), with_account():
        for _ in range(2):
            commands.handle("/log buy BEL 400 sl=390 qty=10")
            tid = journal.open_positions()[0]["id"]
            commands.handle(f"/close {tid} 390 charges=0")
        out = commands.handle("/size BEL entry=405 sl=398")
    assert "⛔" in out


def test_write_commands_are_pin_gated():
    clear_trades()
    with with_symbol(), \
         mock.patch.multiple(commands.config, ACCOUNT_SIZE=500_000, RISK_PCT=1,
                             DAILY_LOSS_R=2, MAX_POSITIONS=5,
                             MAX_POSITION_PCT=100, NTFY_CMD_PIN="4821"):
        assert "needs a PIN" in commands.handle("/log buy BEL 405 sl=398")
        assert "needs a PIN" in commands.handle("/close 1 419")
        assert "needs a PIN" in commands.handle("/amend 1 sl=400")
        # /size only calculates, so it stays open
        assert "LONG" in commands.handle("/size BEL entry=405 sl=398")
        # and with the PIN, writes go through
        assert "Logged" in commands.handle("/log buy BEL 405 sl=398 pin=4821")


def test_new_commands_are_registered():
    known = set(commands.HANDLERS) | set(commands.MULTI_ARG)
    assert {"size", "log", "close", "amend", "journal"} <= known
    assert {"log", "close", "amend"} <= commands.WRITE_CMDS
    assert "size" not in commands.WRITE_CMDS
