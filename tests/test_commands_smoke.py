"""Smoke test: every phone command, against a populated fixture DB.

The `/fundamentals` command read a dict shape that had never existed, so it
returned "no ratio fields stored" for every symbol from the day it shipped.
Nothing caught it because no test had ever invoked it against stored data —
the module's own tests covered `summary()` and `compute()`, both of which were
fine. The bug lived in the gap between them.

This closes that gap for all 40-odd commands at once. It is deliberately
shallow: it asserts each command returns *something plausible* rather than
checking exact output, because the point is to catch commands that are broken
outright, not to pin their wording. Deep assertions live in the per-feature
test modules.

Network calls are stubbed. Anything that genuinely needs a Kite session should
say so politely rather than raising.

Run:  python -m pytest tests/test_commands_smoke.py
"""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "smoke.db"
config.DB_PATH = _DB
config.ACCOUNT_SIZE = 500_000
config.RISK_PCT = 1
config.DAILY_LOSS_R = 2
config.MAX_POSITIONS = 5
config.MAX_POSITION_PCT = 100
config.NTFY_CMD_PIN = ""

from app import commands, db, fundamentals, journal  # noqa: E402

db.init()

TOKEN = 1
SYM = "BEL"

# Phrases that mean the command fell over rather than answered. Deliberately
# narrow: "Usage:" and "Unknown symbol" are legitimate replies.
BROKEN = (
    "no ratio fields stored",
    "traceback",
    "'nonetype'",
    "keyerror",
    "attributeerror",
    "typeerror:",
    "failed:",
)

FULL_FUND = {
    "snapshot": {"Market Cap": "₹ 1,000 Cr.", "Current Price": "₹ 400",
                 "Stock P/E": "20.0", "Book Value": "₹ 100", "ROE": "20 %",
                 "ROCE": "25 %", "Dividend Yield": "1.0 %"},
    "tables": {s: {"periods": ["2023", "2024"], "rows": {
        "Sales": [90, 100], "Net Profit": [9, 10],
        "Profit before tax": [11, 12], "Interest": [1, 1],
        "OPM %": [18, 20], "Equity Capital": [5, 5], "Reserves": [40, 45],
        "Borrowings": [10, 10], "ROCE %": [17, 18],
        "EPS in Rs": [1.8, 2.0]}}
        for s in fundamentals.SECTIONS},
}


def candles(n=300, base=400.0):
    return [{"time": i * 86400, "open": base + i * 0.1, "high": base + i * 0.1 + 3,
             "low": base + i * 0.1 - 3, "close": base + i * 0.1,
             "volume": 10_000} for i in range(n)]


@pytest.fixture(scope="module", autouse=True)
def fixture_db():
    config.DB_PATH = _DB
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO instruments (instrument_token,"
                  " tradingsymbol, name, exchange, instrument_type)"
                  " VALUES (?,?,?,'NSE','EQ')", (TOKEN, SYM, "Bharat Elec"))
        c.execute("INSERT OR IGNORE INTO instruments (instrument_token,"
                  " tradingsymbol, name, exchange, instrument_type)"
                  " VALUES (256265,'NIFTY 50','Nifty 50','NSE','EQ')")
        c.execute("INSERT OR IGNORE INTO watchlist (tradingsymbol, exchange,"
                  " name) VALUES (?,'NSE','Bharat Elec')", (SYM,))
        c.execute("INSERT OR IGNORE INTO signal_state (symbol, strategy, state,"
                  " bar_time, updated_at) VALUES (?,'st_adx',1,1,?)",
                  (SYM, datetime.now().isoformat()))
    fundamentals.store(SYM, FULL_FUND, "consolidated")
    from app import alerts
    alerts.add(SYM, TOKEN, "price_gt", {"x": 410})
    tid = journal.open_trade(SYM, entry=400, qty=10, stop=390, tag="st_adx")
    with mock.patch("app.notify.send", lambda *a, **k: None):
        journal.close_trade(tid, exit_price=420, charges=0)
    journal.open_trade(SYM, entry=405, qty=10, stop=398, tag="st_adx")
    yield


FAKE_NEWS = {SYM: [{"title": "Bharat Electronics wins defence order",
                    "link": "https://example.com/1", "source": "Test",
                    "published": datetime.now().isoformat()}]}


def run(text):
    """Run one command with every network edge stubbed out.

    The stubs are the point: a command that only works when Google News
    answers isn't being tested, it's being sampled.
    """
    with mock.patch.multiple("app.candles", ensure=mock.DEFAULT,
                             get=mock.DEFAULT) as m, \
         mock.patch("app.kite_auth.get_kite", lambda: object()), \
         mock.patch("app.notify.send", lambda *a, **k: None), \
         mock.patch("app.notify.send_image", lambda *a, **k: None), \
         mock.patch("app.news.collect", lambda hours=24: FAKE_NEWS), \
         mock.patch("app.news._fetch_all_sources",
                    lambda name, sym, hours=48: FAKE_NEWS[SYM]), \
         mock.patch("app.news.aggregate",
                    lambda hours=48: {"grouped": FAKE_NEWS, "curated": ""}), \
         mock.patch("app.intraday.today_minutes", lambda *a, **k: candles(80)):
        m["ensure"].return_value = None
        m["get"].return_value = candles()
        return commands.handle(text)


# Every command with an argument shape that should produce a real answer.
CASES = [
    "/help",
    "/price BEL", "/quote BEL",
    "/levels BEL", "/lvl BEL",
    "/ind BEL", "/indicators BEL", "/ta BEL",
    "/conf BEL", "/confluence BEL", "/mtf BEL",
    "/exit BEL", "/trail BEL", "/stop BEL",
    "/brief BEL", "/b BEL",
    "/premarket", "/morning", "/brief",
    "/fundamentals BEL", "/fund BEL",
    "/valuation BEL", "/val BEL",
    "/results BEL", "/result BEL",
    "/dividends BEL", "/div BEL", "/dividend BEL",
    "/news BEL",
    "/signals BEL", "/sig BEL", "/signals",
    "/alerts", "/alerts BEL",
    "/size BEL entry=405 sl=398",
    "/journal", "/j", "/trades", "/journal stats",
]


@pytest.mark.parametrize("cmd", CASES)
def test_command_answers(cmd):
    out = run(cmd)
    assert out is not None, f"{cmd} returned None"
    assert out.strip(), f"{cmd} returned empty"
    low = out.lower()
    for bad in BROKEN:
        assert bad not in low, f"{cmd} -> {out[:160]}"


@pytest.mark.parametrize("cmd", [
    "/price", "/levels", "/ind", "/conf", "/exit", "/fundamentals",
    "/valuation", "/news", "/size", "/log", "/close", "/amend",
])
def test_command_without_args_explains_itself(cmd):
    """A bare command should teach, not crash."""
    out = run(cmd)
    assert out and ("usage" in out.lower() or "give" in out.lower()
                    or "need" in out.lower()), f"{cmd} -> {out[:120]}"


@pytest.mark.parametrize("cmd", [
    "/price NOSUCHTICKER", "/levels NOSUCHTICKER", "/valuation NOSUCHTICKER",
    "/fundamentals NOSUCHTICKER", "/ind NOSUCHTICKER",
])
def test_unknown_symbol_is_handled(cmd):
    out = run(cmd)
    assert out and "traceback" not in out.lower()


def test_unknown_command_is_reported():
    assert "Unknown command" in run("/notacommand")


def test_write_commands_round_trip():
    """log -> amend -> close, the sequence that actually matters."""
    out = run("/log buy BEL 500 sl=490 qty=5 tag=smoke")
    assert "Logged" in out
    tid = [t for t in journal.open_positions() if t["tag"] == "smoke"][0]["id"]
    assert "Updated" in run(f"/amend {tid} sl=495")
    assert "Closed" in run(f"/close {tid} 520 charges=0")


def test_alert_arm_and_disarm():
    out = run("/alert BEL rsi > 70 15m")
    assert "Armed" in out
    assert "Removed" in run("/unalert BEL")


def test_no_command_raises():
    """Nothing should escape handle() — the listener has one try/except and a
    raise there drops the reply silently."""
    for cmd in CASES + ["/log", "/close 999 100", "/amend 999 sl=1",
                        "/unalert nope", "/alert", "/journal NOPE"]:
        try:
            run(cmd)
        except Exception as e:                     # noqa: BLE001
            pytest.fail(f"{cmd} raised {e.__class__.__name__}: {e}")
        except SystemExit as e:
            pytest.fail(f"{cmd} raised SystemExit: {e}")
