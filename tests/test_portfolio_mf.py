"""Mutual funds in the portfolio.

Kite holds funds as well as shares, and the portfolio page was showing half
the account. Adding the other half is mostly plumbing; what isn't plumbing is
keeping the two apart.

**They must not join the equity totals.** Folding fund value into
`total_value` moves every holding's `weight_pct` and quietly defuses the
concentration flag — a 30% position stops looking like one the moment an index
fund is added to the denominator. So: their own list, their own totals, and a
combined figure alongside for when you want the whole picture.

**They carry none of the annotations.** No instrument token means no candles,
which means no chandelier stop, no signal stance, no results date. A fund is
not a company and the page must not imply the same tooling reaches it.

**A missing MF feed must not take equities down with it.** Kite's mutual-fund
API is a separate subscription, so this 403s for plenty of accounts that are
otherwise fine. That's a billing fact, not a broken setup.
"""
from __future__ import annotations

import pytest

from app import portfolio as P


FUND = {
    "folio": "12345/67",
    "fund": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
    "tradingsymbol": "INF879O01027",
    "average_price": 51.2345,
    "last_price": 60.6789,
    "last_price_date": "2026-07-31",
    "quantity": 4821.375,
    "pnl": 45500.0,
}


@pytest.fixture(autouse=True)
def _no_cache():
    P.clear_cache()
    yield
    P.clear_cache()


class FakeKite:
    def __init__(self, mf=None, holdings=None, error=None):
        self._mf, self._h, self._err = mf or [], holdings or [], error

    def holdings(self):
        return self._h

    def positions(self):
        return {"net": []}

    def mf_holdings(self):
        if self._err:
            raise self._err
        return self._mf


# ================================================================== the shape
def test_units_stay_fractional():
    """MF allotments run to three decimals. int() here writes off part of a
    holding, silently and in your favour."""
    assert P._norm_mf(FUND)["units"] == 4821.375


def test_value_and_cost_come_from_units_times_nav():
    m = P._norm_mf(FUND)
    assert m["value"] == round(4821.375 * 60.6789, 2)
    assert m["cost"] == round(4821.375 * 51.2345, 2)
    assert m["pnl"] == round(m["value"] - m["cost"], 2)


def test_the_nav_carries_its_date():
    """NAVs are struck once a day. Printed next to live equity prices without
    a date, a three-day-old NAV reads as a quote."""
    assert P._norm_mf(FUND)["nav_date"] == "2026-07-31"


def test_the_folio_is_kept():
    assert P._norm_mf(FUND)["folio"] == "12345/67"


def test_a_fund_is_marked_as_one():
    assert P._norm_mf(FUND)["kind"] == "mf"


def test_a_fund_with_no_cost_basis_does_not_divide_by_zero():
    assert P._norm_mf({**FUND, "average_price": 0})["pnl_pct"] is None


# ======================================================= kept out of equities
def test_funds_do_not_change_equity_weights(monkeypatch):
    """The one that matters. A concentration flag that stops firing because
    you bought an index fund is worse than no flag."""
    holding = {"tradingsymbol": "BEL", "quantity": 100,
               "average_price": 100.0, "last_price": 400.0}
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[FUND], holdings=[holding]))
    assert snap["holdings"][0]["weight_pct"] == 100.0
    assert snap["totals"]["value"] == 40000.0


def test_the_combined_figure_is_available_separately(monkeypatch):
    holding = {"tradingsymbol": "BEL", "quantity": 100,
               "average_price": 100.0, "last_price": 400.0}
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[FUND], holdings=[holding]))
    assert snap["totals"]["net_worth"] == round(
        40000.0 + snap["mf_totals"]["value"], 2)


def test_funds_get_their_own_totals(monkeypatch):
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[FUND]))
    assert snap["mf_totals"]["count"] == 1
    assert snap["mf_totals"]["value"] > 0


def test_funds_are_never_annotated(monkeypatch):
    """No token, no candles — a chandelier stop on a mutual fund would be a
    number with no meaning behind it."""
    monkeypatch.setattr(P, "annotate",
                        lambda row, kite=None: {**row, "chandelier": "X"})
    snap = P.snapshot(FakeKite(mf=[FUND]))
    assert "chandelier" not in snap["mf"][0]


def test_funds_are_not_added_to_the_watchlist(monkeypatch):
    """sync_watchlist reads raw_holdings, which is equities only. An ISIN on
    the watchlist would break every scan that iterates it."""
    added = []
    monkeypatch.setattr("app.watchlist.items", lambda: [])
    monkeypatch.setattr("app.watchlist.add",
                        lambda s, **k: added.append(s))
    P.sync_watchlist(FakeKite(mf=[FUND], holdings=[]))
    assert added == []


def test_a_zero_unit_fund_is_dropped(monkeypatch):
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[{**FUND, "quantity": 0}]))
    assert snap["mf"] == []


def test_funds_are_ordered_by_value(monkeypatch):
    small = {**FUND, "fund": "Small", "quantity": 10}
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[small, FUND]))
    assert snap["mf"][0]["value"] > snap["mf"][1]["value"]


# ============================================================ degrading well
def test_no_mf_subscription_does_not_break_the_portfolio(monkeypatch):
    """Kite's mutual-fund API is a separate add-on. Plenty of otherwise
    healthy accounts 403 here, and the equity half must still render."""
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    err = Exception("Insufficient permission for that call.")
    holding = {"tradingsymbol": "BEL", "quantity": 100,
               "average_price": 100.0, "last_price": 400.0}
    snap = P.snapshot(FakeKite(holdings=[holding], error=err))
    assert snap["holdings"], "the equity side died with the MF side"
    assert snap["mf"] == []
    assert "subscription" in snap["mf_note"]


def test_a_permission_error_reads_as_billing_not_breakage(monkeypatch):
    rows, note = P.raw_mf_holdings(
        FakeKite(error=Exception("403 Forbidden: permission denied")))
    assert rows == [] and "subscription" in note


def test_another_error_is_reported_as_itself(monkeypatch):
    """Not everything is a subscription problem, and calling a network
    timeout one would send you to the wrong page of the Kite console."""
    rows, note = P.raw_mf_holdings(FakeKite(error=Exception("read timeout")))
    assert rows == [] and "timeout" in note


def test_owning_no_funds_is_silent(monkeypatch):
    """Empty with a reason and empty without mean different things. Only one
    of them deserves a line on the page."""
    monkeypatch.setattr(P, "annotate", lambda row, kite=None: row)
    snap = P.snapshot(FakeKite(mf=[]))
    assert snap["mf"] == [] and snap["mf_note"] is None


def test_no_session_is_carried_through_not_swallowed(monkeypatch):
    class Dead:
        def mf_holdings(self):
            raise SystemExit("no Kite session")

    rows, note = P.raw_mf_holdings(Dead())
    assert rows == [] and "session" in note


# =================================================================== output
def test_the_text_view_shows_funds():
    snap = {
        "as_of": "x", "holdings": [], "positions": [], "flags": [],
        "mf": [{"name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
                "value": 284310.0, "pnl_pct": 18.4}],
        "mf_totals": {"value": 284310.0, "cost": 240000.0, "pnl": 44310.0,
                      "pnl_pct": 18.4, "count": 1},
        "totals": {"value": 100.0, "cost": 100.0, "pnl": 0, "pnl_pct": 0,
                   "day_pnl": 0, "positions_pnl": 0, "count": 0,
                   "net_worth": 284410.0},
    }
    out = P.as_text(snap)
    assert "Mutual funds" in out and "Parag Parikh" in out
    assert "combined" in out


def test_fund_names_are_shortened_for_the_phone():
    """"Direct Plan Growth" is on every line of a Kite portfolio, so on a
    phone it's width spent saying nothing."""
    short = P._short_fund("Parag Parikh Flexi Cap Fund - Direct Plan - Growth")
    assert "Direct" not in short and "Parag Parikh" in short


def test_a_very_long_fund_name_is_truncated():
    assert len(P._short_fund("X" * 200)) <= 34


def test_the_note_is_shown_when_there_are_no_funds():
    snap = {
        "as_of": "x", "holdings": [], "positions": [], "flags": [],
        "mf": [], "mf_note": "a separate subscription",
        "mf_totals": {"value": 0, "cost": 0, "pnl": 0, "pnl_pct": None,
                      "count": 0},
        "totals": {"value": 100.0, "cost": 100.0, "pnl": 0, "pnl_pct": 0,
                   "day_pnl": 0, "positions_pnl": 0, "count": 0,
                   "net_worth": 100.0},
    }
    assert "separate subscription" in P.as_text(snap)


def test_the_page_renders_funds_and_escapes_their_names():
    """Fund names come from the broker rather than this codebase — the only
    text on the page that does."""
    html = (__import__("pathlib").Path(__file__).resolve().parent.parent
            / "app" / "web" / "portfolio.html").read_text()
    assert 'id="mf"' in html and "mf_totals" in html
    assert "const esc" in html and "esc(m.name)" in html


def test_the_module_is_still_read_only():
    """The docstring promises this permanently. Mutual funds are a read; the
    MF API also has order and SIP-modification endpoints, and neither belongs
    here.

    Checked against the parsed AST rather than the text, because the module's
    own docstring says "if you find yourself adding place_order here, stop" —
    a grep fails on the warning while missing a call written as
    `getattr(k, 'place' + '_order')`. Attribute names are what actually run.
    """
    import ast

    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "app" / "portfolio.py").read_text()
    banned = {"place_order", "modify_order", "cancel_order",
              "place_mf_order", "cancel_mf_order",
              "place_mf_sip", "modify_mf_sip", "cancel_mf_sip"}
    called = {n.func.attr for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    assert not (called & banned), called & banned
