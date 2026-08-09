"""Offline tests for the Kite portfolio — fake broker, temp DB, no network.

Run:  python -m pytest tests/test_portfolio.py
"""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "pf.db"
config.DB_PATH = _DB

from app import db, portfolio  # noqa: E402

db.init()


class FakeKite:
    """Just the two read methods. Deliberately has no place_order — if the
    module ever tries to trade, these tests fail with AttributeError."""

    def __init__(self, holdings=None, positions=None, fail=None):
        self._h = holdings if holdings is not None else HOLDINGS
        self._p = positions if positions is not None else POSITIONS
        self._fail = fail

    def holdings(self):
        if self._fail:
            raise self._fail
        return self._h

    def positions(self):
        if self._fail:
            raise self._fail
        return {"net": self._p, "day": self._p}


HOLDINGS = [
    {"tradingsymbol": "BEL", "exchange": "NSE", "quantity": 100,
     "t1_quantity": 0, "average_price": 300.0, "last_price": 400.0,
     "day_change_percentage": 1.5, "instrument_token": 1},
    {"tradingsymbol": "HAL", "exchange": "NSE", "quantity": 10,
     "t1_quantity": 5, "average_price": 4000.0, "last_price": 3800.0,
     "day_change_percentage": -0.8, "instrument_token": 2},
    {"tradingsymbol": "ZERO", "exchange": "NSE", "quantity": 0,
     "t1_quantity": 0, "average_price": 100.0, "last_price": 100.0,
     "instrument_token": 3},
]

POSITIONS = [
    {"tradingsymbol": "BEL", "exchange": "NSE", "quantity": -50,
     "average_price": 410.0, "last_price": 400.0, "pnl": 500.0,
     "product": "MIS", "instrument_token": 1},
    {"tradingsymbol": "HAL", "exchange": "NSE", "quantity": 0,
     "average_price": 0, "last_price": 0, "pnl": 0, "product": "MIS"},
]


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    portfolio.clear_cache()
    with db.connect() as c:
        c.execute("DELETE FROM signal_state")
        c.execute("DELETE FROM watchlist")
        for tok, sym, name in ((1, "BEL", "Bharat Elec"),
                               (2, "HAL", "Hindustan Aero")):
            c.execute("INSERT OR IGNORE INTO instruments (instrument_token,"
                      " tradingsymbol, name, exchange, instrument_type)"
                      " VALUES (?,?,?,'NSE','EQ')", (tok, sym, name))
    yield


def snap(**kw):
    k = FakeKite(**kw)
    return portfolio.snapshot(kite=k, annotate_rows=kw.pop("annotate", True))


# ------------------------------------------------------------- read-only
def test_module_cannot_place_orders():
    """The guarantee this page makes. If someone wires order placement in
    here, this fails and they have to go read Phase 6 first."""
    import re
    src = Path("app/portfolio.py").read_text()
    # Match actual call syntax, not prose — the module docstring names these
    # deliberately, to say it doesn't use them.
    calls = re.findall(r"\.\s*(place_order|modify_order|cancel_order|"
                       r"exit_order|place_mf_order|convert_position)\s*\(", src)
    assert not calls, f"portfolio.py calls {set(calls)}"


def test_snapshot_only_calls_read_methods():
    calls = []

    class Spy(FakeKite):
        def holdings(self):
            calls.append("holdings"); return super().holdings()

        def positions(self):
            calls.append("positions"); return super().positions()

    portfolio.snapshot(kite=Spy(), annotate_rows=False)
    assert set(calls) == {"holdings", "positions"}


# -------------------------------------------------------------- totals
def test_totals_add_up():
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    # BEL 100 @300 -> 400 = 40,000 value / 30,000 cost
    # HAL 15 @4000 -> 3800 = 57,000 value / 60,000 cost
    assert s["totals"]["value"] == 97_000
    assert s["totals"]["cost"] == 90_000
    assert s["totals"]["pnl"] == 7_000
    assert abs(s["totals"]["pnl_pct"] - 7.78) < 0.05


def test_t1_quantity_is_counted():
    """Shares bought yesterday sit in t1 until settlement — omitting them
    understates the position you actually own."""
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    hal = [h for h in s["holdings"] if h["symbol"] == "HAL"][0]
    assert hal["qty"] == 15


def test_zero_quantity_rows_are_dropped():
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    assert "ZERO" not in [h["symbol"] for h in s["holdings"]]


def test_closed_positions_are_dropped():
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    assert len(s["positions"]) == 1 and s["positions"][0]["qty"] == -50


def test_short_position_side_and_pnl_sign():
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    p = s["positions"][0]
    assert p["side"] == "short"
    # short from 410, now 400 -> profitable, so pnl_pct must read positive
    assert p["pnl_pct"] > 0


def test_weights_and_sort_order():
    s = portfolio.snapshot(kite=FakeKite(), annotate_rows=False)
    assert s["holdings"][0]["value"] >= s["holdings"][1]["value"]
    assert abs(sum(h["weight_pct"] for h in s["holdings"]) - 100) < 0.1


def test_positions_use_net_not_day():
    """`day` double-counts anything carried over, so the same trade appears
    twice and the P&L is wrong."""
    dup = {"tradingsymbol": "BEL", "exchange": "NSE", "quantity": -50,
           "average_price": 410.0, "last_price": 400.0, "pnl": 500.0}

    class TwoBooks(FakeKite):
        def positions(self):
            return {"net": [dup], "day": [dup, dup]}

    s = portfolio.snapshot(kite=TwoBooks(), annotate_rows=False)
    assert len(s["positions"]) == 1


# ---------------------------------------------------------- annotations
def test_signal_stance_read_from_stored_state():
    with db.connect() as c:
        for strat, state in (("st_adx", 1), ("supertrend", 1), ("momentum", 0)):
            c.execute("INSERT INTO signal_state (symbol, strategy, state,"
                      " bar_time, updated_at) VALUES ('BEL',?,?,1,?)",
                      (strat, state, datetime.now().isoformat()))
    s = portfolio.snapshot(kite=FakeKite())
    bel = [h for h in s["holdings"] if h["symbol"] == "BEL"][0]
    assert bel["signals"]["stance"] == "long"      # 2 of 3
    assert bel["signals"]["total"] == 3


def test_annotation_absent_when_no_signal_state():
    s = portfolio.snapshot(kite=FakeKite())
    assert all(h["signals"] is None for h in s["holdings"])


def test_annotations_never_raise_on_missing_data():
    """Every annotation source can be absent — the table must still render."""
    s = portfolio.snapshot(kite=FakeKite())
    for h in s["holdings"]:
        assert set(h) >= {"valuation", "signals", "results_due", "alerts"}


def test_chandelier_computed_from_stored_candles():
    bars = [{"time": i, "open": 400, "high": 410, "low": 390, "close": 400,
             "volume": 1} for i in range(60)]
    with mock.patch("app.candles.get", lambda *a, **k: bars):
        s = portfolio.snapshot(kite=FakeKite())
    bel = [h for h in s["holdings"] if h["symbol"] == "BEL"][0]
    assert bel["chandelier"] and bel["chandelier"]["stop"] < 410


def test_chandelier_none_without_enough_history():
    with mock.patch("app.candles.get", lambda *a, **k: []):
        s = portfolio.snapshot(kite=FakeKite())
    assert all(h.get("chandelier") is None for h in s["holdings"])


# --------------------------------------------------------------- flags
def test_concentration_is_flagged():
    s = portfolio.snapshot(kite=FakeKite())
    assert any("% of the portfolio" in f for f in s["flags"])


def test_below_stop_is_flagged():
    # A tight range keeps ATR small, so the stop stays near the highs and a
    # modest drop genuinely breaches it. A violent crash would widen ATR and
    # drag the stop down with the price, which is the point of an ATR stop.
    holds = [dict(HOLDINGS[0], last_price=380.0)]
    bars = [{"time": i, "open": 400, "high": 402, "low": 398, "close": 400,
             "volume": 1} for i in range(60)]
    bars.append({"time": 99, "open": 398, "high": 398, "low": 380,
                 "close": 380, "volume": 1})
    with mock.patch("app.candles.get", lambda *a, **k: bars):
        s = portfolio.snapshot(kite=FakeKite(holdings=holds, positions=[]))
    assert any("below its" in f for f in s["flags"])


def test_no_flags_on_a_boring_portfolio():
    holds = [dict(HOLDINGS[0], tradingsymbol=f"S{i}", quantity=10,
                  average_price=100, last_price=101, instrument_token=100 + i)
             for i in range(10)]
    with mock.patch("app.candles.get", lambda *a, **k: []):
        s = portfolio.snapshot(kite=FakeKite(holdings=holds, positions=[]))
    assert s["flags"] == []


# ------------------------------------------------------------- failures
def test_missing_session_becomes_portfolio_error():
    """SystemExit from config.require must not escape as a process exit."""
    with mock.patch("app.kite_auth.get_kite",
                    side_effect=SystemExit("No valid Kite session for today")):
        with pytest.raises(portfolio.PortfolioError, match="session"):
            portfolio.raw_holdings()


def test_broker_error_becomes_portfolio_error():
    with pytest.raises(portfolio.PortfolioError, match="could not read"):
        portfolio.raw_holdings(kite=FakeKite(fail=RuntimeError("500 from Kite")))


# --------------------------------------------------------------- caching
def test_broker_call_is_cached():
    """Kite rate-limits; a page refresh shouldn't spend a call."""
    calls = []

    class Counting(FakeKite):
        def holdings(self):
            calls.append(1); return HOLDINGS

    k = Counting()
    portfolio.raw_holdings(kite=k)
    portfolio.raw_holdings(kite=k)
    assert len(calls) == 1
    portfolio.clear_cache()
    portfolio.raw_holdings(kite=k)
    assert len(calls) == 2


# ------------------------------------------------------------- watchlist
def test_sync_watchlist_adds_missing_holdings():
    r = portfolio.sync_watchlist(kite=FakeKite())
    assert set(r["added"]) == {"BEL", "HAL"}
    from app import watchlist
    assert "BEL" in {w["tradingsymbol"] for w in watchlist.items()}


def test_sync_watchlist_survives_a_symbol_not_in_the_index():
    """watchlist.add raises SystemExit for an unknown symbol — a stale index
    or a BSE-only holding must not abort the sync for everything else."""
    r = portfolio.sync_watchlist(kite=FakeKite())
    assert any(f.startswith("ZERO:") for f in r["failed"])
    assert "BEL" in r["added"]


def test_sync_watchlist_is_additive_only():
    """A watchlist entry you added on purpose shouldn't vanish when you sell."""
    from app import watchlist
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO instruments (instrument_token,"
                  " tradingsymbol, name, exchange, instrument_type)"
                  " VALUES (99,'TCS','Tata Consultancy','NSE','EQ')")
    watchlist.add("TCS", "NSE", autofetch=False)
    portfolio.sync_watchlist(kite=FakeKite())
    assert "TCS" in {w["tradingsymbol"] for w in watchlist.items()}


def test_sync_watchlist_is_idempotent():
    portfolio.sync_watchlist(kite=FakeKite())
    second = portfolio.sync_watchlist(kite=FakeKite())
    assert second["added"] == []


# ---------------------------------------------------------------- render
def test_as_text_is_phone_sized():
    s = portfolio.snapshot(kite=FakeKite())
    out = portfolio.as_text(s)
    assert "Portfolio —" in out and "BEL" in out
    assert len(out.splitlines()) < 30


def test_as_text_handles_an_empty_portfolio():
    s = portfolio.snapshot(kite=FakeKite(holdings=[], positions=[]))
    out = portfolio.as_text(s)
    assert "Portfolio" in out


# --------------------------------------------------------------- command
def test_portfolio_command_is_read_only():
    from app import commands
    assert "portfolio" not in commands.WRITE_CMDS
    assert "portfolio" in commands.HANDLERS


def test_portfolio_command_reports_a_missing_session():
    from app import commands
    with mock.patch.object(portfolio, "snapshot",
                           side_effect=portfolio.PortfolioError("No session")):
        out = commands.handle("/portfolio")
    assert "unavailable" in out.lower()
