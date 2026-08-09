"""SIPs, fund composition, and overlap.

The two halves of this module come from different places, and that difference
is the thing most worth encoding in tests.

**SIPs come from Kite.** Ordinary plumbing, with one piece of arithmetic that
is easy to get quietly wrong: the next instalment date.

**Composition does not come from Kite.** There is no endpoint — `mf_holdings`,
`mf_orders`, `mf_sips`, `mf_instruments`, and the last is scheme metadata, not
a portfolio. So composition is imported from the AMC's monthly disclosure, and
everything downstream has to be honest about which funds you actually have
data for. A fund with no imported portfolio must read as *missing*, never as
*no overlap* — those are identical in a result table and mean opposite things.

The disclosure parser gets the most attention here because it's the part
handling files this codebase has never seen: laid out for a PDF, headers on
some arbitrary line, column names different at every fund house, and a body
interrupted by section headings and subtotals. A subtotal counted as a holding
shows up as the fund's largest position.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import db, mf


SHEET = """Portfolio Statement as on July 31, 2026
Some Fund - Direct Plan - Growth

Name of the Instrument,ISIN,Quantity,Market value (Rs. in Lakhs),% to NAV
EQUITY & EQUITY RELATED,,,,
Listed / awaiting listing on Stock Exchanges,,,,
HDFC Bank Limited,INE040A01034,"12,50,000",24500.50,8.42
Reliance Industries Ltd,INE002A01018,"3,20,000",41800.00,7.15
Infosys Limited,INE009A01021,"8,00,000",12600.25,4.31
Sub Total,,,78900.75,19.88
TREPS / Reverse Repo,,,1200.00,0.30
Net Current Assets,,,-45.00,-0.02
Grand Total,,,80055.75,100.00
"""


@pytest.fixture(autouse=True)
def _fresh():
    db.init()
    with db.connect() as c:
        c.execute("DELETE FROM mf_scheme_holdings")
    yield
    with db.connect() as c:
        c.execute("DELETE FROM mf_scheme_holdings")


# ============================================================ the disclosure
def test_holdings_are_found_below_a_preamble():
    """The header is never on line 1 — these sheets are laid out for a PDF."""
    rows = mf.parse_portfolio(SHEET)
    assert [r["name"] for r in rows] == [
        "HDFC Bank Limited", "Reliance Industries Ltd", "Infosys Limited"]


def test_subtotals_and_cash_are_not_holdings():
    """A subtotal imported as a company is the fund's largest position, and
    it looks entirely plausible in a sorted table."""
    names = [r["name"] for r in mf.parse_portfolio(SHEET)]
    for junk in ("Sub Total", "Grand Total", "Net Current Assets",
                 "TREPS / Reverse Repo", "EQUITY & EQUITY RELATED"):
        assert junk not in names


@pytest.mark.parametrize("junk", [
    "Total", "Sub Total", "Grand Total", "Net Current Assets",
    "Other Current Assets", "Cash & Cash Equivalents", "Tri-Party Repo",
    "TREPS", "Reverse Repo", "Money Market Instruments", "Derivatives",
    "Unlisted", "Notes:", "Margin Deposit",
])
def test_every_known_non_holding_is_dropped(junk):
    assert mf._NON_HOLDINGS.match(junk), junk


@pytest.mark.parametrize("company", [
    "TotalEnergies Marketing India",     # starts with "total"
    "Total Energies SE",
    "Equitas Small Finance Bank Ltd",    # starts with "equit"
    "Cashfree Payments Ltd",             # starts with "cash"
    "Marginal Gains Ltd",
    "HDFC Bank Limited",
])
def test_real_companies_are_not_mistaken_for_headings(company):
    """The filter runs both ways. Over-matching silently deletes a holding,
    which is the harder failure to notice — the fund just looks smaller."""
    assert not mf._NON_HOLDINGS.match(company), company


def test_indian_digit_grouping_parses():
    rows = mf.parse_portfolio(SHEET)
    assert rows[0]["quantity"] == 1250000.0


def test_column_names_vary_by_fund_house():
    """"Market/Fair Value (Rs. in Lacs)" and "Market value (Rs. in Lakhs)"
    are the same column. Hardcoding either is a parser for one AMC."""
    alt = ('Company Name,ISIN,No. of Shares,Market/Fair Value (Rs. in Lacs),'
           '% of Net Assets\n'
           'Infosys Limited,INE009A01021,1000,50.5,3.2\n')
    rows = mf.parse_portfolio(alt)
    assert rows[0]["pct_to_nav"] == 3.2 and rows[0]["name"] == "Infosys Limited"


def test_a_sentence_containing_name_is_not_a_header():
    """A header needs an instrument column AND something quantitative, or
    the disclaimer paragraph becomes the schema."""
    assert mf._header_map(["Name of scheme and its objectives", "", ""]) == {}


def test_an_unrecognisable_file_says_so_rather_than_importing_nothing():
    with pytest.raises(mf.MFError) as e:
        mf.import_portfolio("X", "just,some,csv\n1,2,3\n")
    assert "header" in str(e.value).lower()


def test_the_disclosure_month_is_required_to_look_like_one():
    with pytest.raises(mf.MFError):
        mf.import_portfolio("X", SHEET, as_of="July 2026")


def test_reimporting_a_month_replaces_it(monkeypatch):
    """Not appends. A corrected disclosure must not sit alongside the one it
    corrects, doubling every weight."""
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: None)
    mf.import_portfolio("F", SHEET, "2026-07")
    mf.import_portfolio("F", SHEET, "2026-07")
    assert len(mf.holdings_of("F", "2026-07")) == 3


# =================================================================== overlap
def _two_funds(monkeypatch):
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: None)
    a = ("Name of the Instrument,ISIN,% to NAV\n"
         "HDFC Bank Limited,INE040A01034,8.0\n"
         "Reliance Industries Ltd,INE002A01018,7.0\n"
         "Infosys Limited,INE009A01021,4.0\n")
    b = ("Name of the Instrument,ISIN,% to NAV\n"
         "HDFC Bank Ltd,INE040A01034,6.0\n"
         "Reliance Industries Limited,INE002A01018,9.0\n"
         "Tata Consultancy Services Ltd,INE467B01029,5.0\n")
    mf.import_portfolio("A", a, "2026-07")
    mf.import_portfolio("B", b, "2026-07")


def test_funds_are_joined_on_isin_not_on_name(monkeypatch):
    """"HDFC Bank Limited" and "HDFC Bank Ltd" are one company. Joined on
    name they're two, and the overlap comes back zero."""
    _two_funds(monkeypatch)
    o = mf.overlap("A", "B")
    assert o["shared_count"] == 2


def test_overlap_is_the_sum_of_the_smaller_weights(monkeypatch):
    """min(8,6) + min(7,9) = 6 + 7 = 13. That's the portion you'd be buying
    twice.
    Averaging instead inflates it whenever the funds hold different amounts
    of the same name."""
    _two_funds(monkeypatch)
    assert mf.overlap("A", "B")["overlap_pct"] == 13.0


def test_the_shared_list_is_ordered_by_what_you_own_twice(monkeypatch):
    _two_funds(monkeypatch)
    shared = mf.overlap("A", "B")["shared"]
    assert shared[0]["common_pct"] >= shared[-1]["common_pct"]


def test_a_fund_with_no_portfolio_reads_as_missing_not_as_zero(monkeypatch):
    """The whole reason this module states its data source so loudly. "0%
    overlap" and "I have no idea" are the same row in a table."""
    _two_funds(monkeypatch)
    o = mf.overlap("A", "Never Imported")
    assert o["overlap_pct"] is None
    assert o["missing"] == ["Never Imported"]
    assert "no imported portfolio" in o["note"]


def test_comparing_across_months_is_flagged(monkeypatch):
    """Two funds from different disclosures is a comparison of two different
    portfolios presented as one number."""
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: None)
    one = "Name of the Instrument,ISIN,% to NAV\nX Ltd,INE111A01011,5.0\n"
    mf.import_portfolio("A", one, "2026-06")
    mf.import_portfolio("B", one, "2026-07")
    o = mf.overlap("A", "B")
    assert o["note"] and "different months" in o["note"]


def test_the_latest_disclosure_is_used_by_default(monkeypatch):
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: None)
    old = "Name of the Instrument,ISIN,% to NAV\nOld Ltd,INE111A01011,5.0\n"
    new = "Name of the Instrument,ISIN,% to NAV\nNew Ltd,INE222A01022,5.0\n"
    mf.import_portfolio("F", old, "2026-05")
    mf.import_portfolio("F", new, "2026-07")
    assert mf.holdings_of("F")[0]["name"] == "New Ltd"


def test_symbol_resolution_refuses_to_guess(monkeypatch):
    """A wrong symbol silently attributes one company's fund weight to
    another. An unresolved row you can see is better."""
    monkeypatch.setattr("app.instruments.search",
                        lambda q, limit=8: [
                            {"name": "Acme Industries", "tradingsymbol": "ACME1",
                             "exchange": "NSE"},
                            {"name": "Acme Industries", "tradingsymbol": "ACME2",
                             "exchange": "NSE"}])
    assert mf._resolve_symbol("Acme Industries Limited") is None


def test_company_suffixes_are_stripped_before_matching():
    assert mf._clean_name("Reliance Industries Limited") == "reliance industries"
    assert mf._clean_name("Infosys Ltd.") == "infosys"


# ====================================================================== SIPs
def test_the_next_instalment_is_taken_from_kite_when_given():
    nxt, days = mf._next_instalment(
        {"next_instalment": "2026-08-10"}, date(2026, 8, 5))
    assert nxt == "2026-08-10" and days == 5


def test_it_is_reconstructed_from_the_day_when_not():
    nxt, days = mf._next_instalment({"instalment_day": 15}, date(2026, 8, 5))
    assert nxt == "2026-08-15" and days == 10


def test_a_date_already_past_rolls_to_next_month():
    nxt, _ = mf._next_instalment({"instalment_day": 1}, date(2026, 8, 5))
    assert nxt == "2026-09-01"


def test_the_31st_lands_on_the_last_day_of_a_short_month():
    """A SIP set for the 31st doesn't skip February. Rolling it forward to
    March would say the money leaves a month later than it does."""
    nxt, _ = mf._next_instalment({"instalment_day": 31}, date(2026, 2, 5))
    assert nxt == "2026-02-28"


def test_an_unknown_schedule_is_none_not_today():
    assert mf._next_instalment({}, date(2026, 8, 5)) == (None, None)


def test_sips_are_sorted_soonest_first(monkeypatch):
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([
        {"fund": "Far", "instalment_amount": 5000, "instalment_day": 28},
        {"fund": "Near", "instalment_amount": 5000, "instalment_day": 10},
    ], None))
    out = mf.sips(today=date(2026, 8, 5))
    assert [s["fund"] for s in out["sips"]] == ["Near", "Far"]


def test_an_undated_sip_sorts_last(monkeypatch):
    """It isn't due today just because we couldn't work out when it's due."""
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([
        {"fund": "Unknown", "instalment_amount": 1000},
        {"fund": "Dated", "instalment_amount": 1000, "instalment_day": 28},
    ], None))
    assert mf.sips(today=date(2026, 8, 5))["sips"][-1]["fund"] == "Unknown"


def test_the_monthly_cost_accounts_for_frequency(monkeypatch):
    """A weekly SIP of 1,000 is not 1,000 a month, and a running total that
    says so understates what's leaving the account by a factor of four."""
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([
        {"fund": "W", "instalment_amount": 1000, "frequency": "weekly",
         "instalment_day": 1},
    ], None))
    assert mf.sips(today=date(2026, 8, 5))["monthly_total"] > 4000


def test_a_cancelled_sip_is_not_counted_in_the_running_cost(monkeypatch):
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([
        {"fund": "Dead", "instalment_amount": 9999, "status": "CANCELLED",
         "instalment_day": 1},
    ], None))
    out = mf.sips(today=date(2026, 8, 5))
    assert out["monthly_total"] == 0 and out["active"] == 0


def test_no_mf_subscription_is_a_sentence_not_a_crash(monkeypatch):
    class Dead:
        def mf_sips(self):
            raise Exception("Insufficient permission for that call")
    rows, note = mf.raw_sips(Dead())
    assert rows == [] and "subscription" in note


def test_due_within_only_looks_forward(monkeypatch):
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([
        {"fund": "Soon", "instalment_amount": 1000, "instalment_day": 7},
        {"fund": "Later", "instalment_amount": 1000, "instalment_day": 28},
    ], None))
    due = mf.due_within(5, today=date(2026, 8, 5))
    assert [d["fund"] for d in due] == ["Soon"]


def test_the_brief_has_a_sip_section():
    from app import brief
    assert any(name == "sips" for name, _fn in brief.SECTIONS)


def test_the_brief_section_is_silent_when_nothing_is_due(monkeypatch):
    """Every brief section earns its line. Nothing due is nothing said."""
    from app import brief
    monkeypatch.setattr(mf, "raw_sips", lambda kite=None: ([], None))
    assert brief.sips_section() == []


# ============================================================== look-through
def test_look_through_adds_fund_exposure_to_direct_holdings(monkeypatch):
    """The question a fund investor can't otherwise answer. A 4% direct
    holding that is also 8% of a fund worth as much again is a 12% position
    no screen on this platform would have flagged."""
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: "HDFCBANK")
    mf.import_portfolio(
        "F", "Name of the Instrument,ISIN,% to NAV\nHDFC Bank Ltd,IN1,50.0\n",
        "2026-07")
    monkeypatch.setattr("app.portfolio.snapshot", lambda k=None, **kw: {
        "holdings": [{"symbol": "HDFCBANK", "name": "HDFC Bank",
                      "value": 100000.0}],
        "mf": [{"name": "F", "value": 200000.0}]})
    out = mf.look_through()
    row = out["exposure"][0]
    assert row["direct"] == 100000.0
    assert row["via_funds"] == 100000.0      # 50% of 200,000
    assert row["total"] == 200000.0


def test_funds_without_a_portfolio_are_reported_as_blind_spots(monkeypatch):
    """Silently omitting them would make the look-through look complete when
    half the money isn't in it."""
    monkeypatch.setattr("app.portfolio.snapshot", lambda k=None, **kw: {
        "holdings": [], "mf": [{"name": "Unimported", "value": 500000.0}]})
    out = mf.look_through()
    assert out["unmapped_value"] == 500000.0
    assert "not looked through" in out["note"]


def test_a_fund_holding_that_never_matched_a_symbol_is_skipped(monkeypatch):
    """Not attributed to a guess. Overlap still works for it — that joins on
    ISIN — but exposure needs a symbol to attach to."""
    monkeypatch.setattr(mf, "_resolve_symbol", lambda n: None)
    mf.import_portfolio(
        "F", "Name of the Instrument,ISIN,% to NAV\nMystery Ltd,IN9,50.0\n",
        "2026-07")
    monkeypatch.setattr("app.portfolio.snapshot", lambda k=None, **kw: {
        "holdings": [], "mf": [{"name": "F", "value": 100000.0}]})
    assert mf.look_through()["exposure"] == []


# =========================================================== still read-only
def test_this_module_places_no_orders():
    """The MF API has place_mf_order, place_mf_sip, modify_mf_sip and
    cancel_mf_sip. None of them belongs here — this reports, it doesn't act.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "mf.py").read_text()
    banned = {"place_mf_order", "cancel_mf_order", "place_mf_sip",
              "modify_mf_sip", "cancel_mf_sip", "place_order"}
    called = {n.func.attr for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & banned), called & banned
