"""Offline tests for the pre-market brief — temp DB, no network.

Run:  python -m pytest tests/test_brief.py
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app import config

_DB = Path(tempfile.mkdtemp()) / "brief.db"
config.DB_PATH = _DB
config.ACCOUNT_SIZE = 500_000
config.RISK_PCT = 1
config.DAILY_LOSS_R = 2
config.MAX_POSITIONS = 5

from app import brief, db, journal  # noqa: E402

db.init()

NIFTY_TOKEN = 256265

# The brief resolves symbols to tokens, so the instrument index has to exist.
with db.connect() as _c:
    for _tok, _sym, _name in ((NIFTY_TOKEN, "NIFTY 50", "Nifty 50 Index"),
                              (1, "BEL", "Bharat Electronics"),
                              (2, "HAL", "Hindustan Aeronautics")):
        _c.execute(
            "INSERT OR IGNORE INTO instruments (instrument_token, tradingsymbol,"
            " name, exchange, instrument_type) VALUES (?,?,?,'NSE','EQ')",
            (_tok, _sym, _name))
    _c.execute("INSERT OR IGNORE INTO watchlist (tradingsymbol, exchange, name)"
               " VALUES ('BEL','NSE','Bharat Electronics')")


def bars(n=320, base=24000.0, slope=1.0):
    return [{"time": i, "open": base + i * slope, "high": base + i * slope + 5,
             "low": base + i * slope - 5, "close": base + i * slope,
             "volume": 1000} for i in range(n)]


def clear():
    with db.connect() as c:
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM alerts")
        c.execute("DELETE FROM signal_state")
        c.execute("DELETE FROM meta WHERE key='brief_last_sent'")


def setup_function():
    # Other test modules also point config.DB_PATH at their own temp file at
    # import time, and db.connect() reads it at call time — so re-pin ours
    # before every test rather than relying on import order.
    config.DB_PATH = _DB
    config.ACCOUNT_SIZE = 500_000
    config.RISK_PCT = 1
    config.DAILY_LOSS_R = 2
    clear()


def offline():
    """No Kite session, no stored candles — the pessimistic case."""
    return mock.patch.multiple(brief, _kite=lambda: None,
                               _closes=lambda k, t, days=260: [])


def with_market(slope=1.0):
    return mock.patch.multiple(
        brief, _kite=lambda: object(),
        _closes=lambda k, t, days=260: bars(slope=slope))


# ----------------------------------------------------------- composition
def test_compose_never_raises_with_an_empty_database():
    with offline():
        out = brief.compose(skip=("news",))
    assert out.startswith("Pre-market —")


def test_a_failing_section_does_not_take_the_brief_down():
    """A brief that doesn't arrive is worse than one missing a line."""
    def boom():
        raise RuntimeError("news source down")

    with offline(), mock.patch.object(brief, "SECTIONS",
                                      (("news", boom),
                                       ("risk", brief.budget_section))):
        out = brief.compose()
    assert "RISK" in out
    assert "unavailable: news" in out


def test_sections_can_be_skipped():
    journal.open_trade("BEL", entry=405, qty=10, stop=398)
    with offline():
        assert "POSITIONS" in brief.compose(skip=("news",))
        assert "POSITIONS" not in brief.compose(skip=("news", "positions"))


# ------------------------------------------------------------- positions
def test_positions_section_leads_with_what_you_own():
    journal.open_trade("BEL", entry=405, qty=714, stop=398, tag="st_adx")
    with offline():
        out = brief.compose(skip=("news",))
    assert "POSITIONS (1)" in out
    assert out.index("POSITIONS") < out.index("RISK")     # ordering matters


def test_positions_section_absent_when_flat():
    with offline():
        assert "POSITIONS" not in brief.compose(skip=("news",))


def test_positions_show_live_r_when_a_price_is_available():
    journal.open_trade("BEL", entry=400, qty=10, stop=390)
    with mock.patch.multiple(
            brief, _kite=lambda: object(),
            _closes=lambda k, t, days=260: [{"time": 1, "open": 420,
                                             "high": 421, "low": 419,
                                             "close": 420, "volume": 1}]):
        out = brief.positions_section()
    assert any("+2.00R" in l for l in out)


# ------------------------------------------------------------------ risk
def test_risk_section_reports_the_budget():
    with offline():
        out = brief.budget_section()[0]
    assert "today's budget 2R" in out
    assert "1R = ₹5,000" in out


def test_risk_section_carries_yesterdays_damage():
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
    with mock.patch("app.notify.send", lambda m, **k: None):
        journal.close_trade(tid, exit_price=390, charges=0,
                            exit_time=f"{y}T15:00:00")
    assert "yesterday -1.00R" in brief.budget_section()[0]


def test_risk_section_counts_live_risk():
    journal.open_trade("BEL", entry=400, qty=1, stop=390)
    assert "1.00R already live" in brief.budget_section()[0]


# ---------------------------------------------------------------- regime
def test_regime_reads_risk_on_in_an_uptrend():
    with with_market(slope=1.0):
        out = brief.regime_section()[0]
    assert "RISK-ON" in out and "above 200-DMA" in out


def test_regime_reads_risk_off_in_a_downtrend():
    with mock.patch.multiple(
            brief, _kite=lambda: object(),
            _closes=lambda k, t, days=260: bars(slope=-1.0)):
        out = brief.regime_section()
    assert "RISK-OFF" in out[0]
    assert any("bleed" in l for l in out)          # the actionable warning


def test_regime_degrades_without_history():
    with offline():
        out = brief.regime_section()[0]
    assert "TAPE" in out and ("not enough" in out or "not in the instrument" in out)


# ---------------------------------------------------------------- alerts
def test_near_alerts_ranks_by_proximity():
    from app import alerts
    alerts.add("BEL", 1, "price_gt", {"x": 24100})     # ~0.3% away
    alerts.add("HAL", 2, "price_gt", {"x": 30000})     # far
    with with_market():
        out = brief.near_alerts_section()
    assert "ALERTS (2 armed)" in out[0]
    assert any("BEL" in l and "% away" in l for l in out)
    assert any("further out" in l for l in out)


def test_no_alerts_section_when_none_armed():
    with offline():
        assert brief.near_alerts_section() == []


# --------------------------------------------------------------- signals
def test_flips_section_reads_recent_stance_changes():
    with db.connect() as c:
        c.execute("INSERT INTO signal_state (symbol, strategy, state, bar_time,"
                  " updated_at) VALUES ('BEL','st_adx',1,1,?)",
                  (datetime.now().isoformat(),))
    out = brief.flips_section()
    assert any("BEL" in l and "LONG" in l for l in out)


def test_flips_section_ignores_stale_state():
    with db.connect() as c:
        c.execute("INSERT INTO signal_state (symbol, strategy, state, bar_time,"
                  " updated_at) VALUES ('OLD','st_adx',1,1,'2020-01-01T00:00:00')")
    assert brief.flips_section() == []


# -------------------------------------------------------------- scheduling
def test_maybe_send_fires_once_then_stops():
    sent = []
    now = datetime(2026, 7, 27, 8, 45)             # a Monday
    with offline(), mock.patch.object(brief, "send",
                                      lambda: sent.append(1)):
        assert brief.maybe_send(now=now) is True
        assert brief.maybe_send(now=now) is False   # date-guarded
    assert len(sent) == 1


def test_maybe_send_skips_weekends():
    with offline(), mock.patch.object(brief, "send", lambda: None):
        assert brief.maybe_send(now=datetime(2026, 7, 25, 8, 45)) is False  # Sat
        assert brief.maybe_send(now=datetime(2026, 7, 26, 8, 45)) is False  # Sun


def test_maybe_send_honours_the_window_not_the_instant():
    """The caller's loop ticks every 20s and may restart — an exact-minute
    match would lose the day's brief to a badly timed restart."""
    with offline(), mock.patch.object(brief, "send", lambda: None):
        assert brief.maybe_send(now=datetime(2026, 7, 27, 8, 44)) is False
        assert brief.maybe_send(now=datetime(2026, 7, 27, 9, 5)) is True


def test_maybe_send_marks_before_composing():
    """Composing hits the network. A retry storm at 08:45 is worse than a
    missed brief, so the day is marked first."""
    def boom():
        raise RuntimeError("everything is down")

    with offline(), mock.patch.object(brief, "send", boom), \
         mock.patch("app.notify.send", lambda m, **k: None):
        assert brief.maybe_send(now=datetime(2026, 7, 27, 8, 45)) is True
        assert brief.maybe_send(now=datetime(2026, 7, 27, 8, 46)) is False


def test_send_survives_a_dead_ntfy():
    def dead(msg, **k):
        raise OSError("no route")

    with offline(), mock.patch("app.notify.send", dead):
        assert "push failed" in brief.send()


# ---------------------------------------------------------------- command
def test_premarket_command_is_separate_from_brief_sym():
    from app import commands
    with offline():
        out = commands.handle("/premarket")
    assert out.startswith("Pre-market —")


def test_bare_brief_gives_the_morning_brief():
    from app import commands
    with offline():
        assert commands.handle("/brief").startswith("Pre-market —")


def test_brief_with_a_symbol_still_gives_the_stock_cockpit():
    """The two must not collide — /brief SYM predates this feature."""
    from app import commands
    out = commands.handle("/brief NOSUCHSYMBOL")
    assert not out.startswith("Pre-market —")
