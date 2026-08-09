"""Tests for the valuation engine.

552 lines that decide what to buy, and until now no test touched them. The
reason it went untested this long is that the intended semantics weren't
written down anywhere — so these assert the rules the field guide documents,
which makes the guide and the code check each other.

Everything here is pure arithmetic on explicit inputs: no database, no
network, no screener. The scraping layer is a separate problem.
"""
from __future__ import annotations

import pytest

from app import valuation as V


def approx(a, b, tol=0.01):
    assert a is not None, "expected a number, got None"
    assert abs(a - b) <= tol, f"{a} != {b}"


# ------------------------------------------------------------ discount rate
def test_cost_of_equity_is_built_not_assumed():
    """re = risk-free + ERP + a size premium, and nothing else."""
    approx(V.smart_re(50_000), V.RISK_FREE + V.EQUITY_RISK_PREMIUM)


@pytest.mark.parametrize("mcap,premium", [
    (300, 0.03),          # micro
    (3_000, 0.02),        # small
    (15_000, 0.01),       # mid
    (50_000, 0.00),       # large
])
def test_size_premium_tiers(mcap, premium):
    approx(V.smart_re(mcap), V.RISK_FREE + V.EQUITY_RISK_PREMIUM + premium)


def test_smaller_companies_discount_harder():
    """The whole point of the size premium: same cash flows, lower value."""
    assert V.smart_re(300) > V.smart_re(3_000) > V.smart_re(50_000)


def test_unknown_market_cap_gets_no_premium():
    """Absent data must not silently become a risk adjustment."""
    approx(V.smart_re(None), V.RISK_FREE + V.EQUITY_RISK_PREMIUM)


# ----------------------------------------------------------------- verdicts
@pytest.mark.parametrize("mos,verdict", [
    (60, "undervalued"),
    (30, "undervalued"),      # boundary, inclusive
    (29.9, "fair"),
    (0, "fair"),              # boundary, inclusive
    (-0.1, "rich"),
    (-25, "rich"),            # boundary, inclusive
    (-25.1, "overvalued"),
    (None, "n/a"),
])
def test_verdict_thresholds(mos, verdict):
    assert V._verdict(mos) == verdict


# ------------------------------------------------------------------ median
def test_median_of_even_count_is_the_average_of_the_middles():
    """`sorted(v)[len(v)//2]` takes the UPPER middle, biasing fair value — and
    therefore margin of safety — upward. In the direction that costs money."""
    approx(V._median([100, 200]), 150)
    approx(V._median([10, 20, 30, 40]), 25)


def test_median_of_odd_count_is_the_middle():
    approx(V._median([10, 20, 30]), 20)


def test_median_of_nothing_is_none():
    assert V._median([]) is None


def test_median_resists_one_wild_model():
    """Why the composite is a median and not a mean."""
    sane = [200, 210, 220]
    approx(V._median(sane + [12_700]), 215)


# ------------------------------------------------- the (re - g) blowup guard
def test_justified_pb_declines_when_growth_approaches_the_discount_rate():
    """(ROE - g) / (re - g) explodes as the denominator vanishes.

    Clamping g to just under re — which is what the call sites used to do —
    turned an ordinary high-quality compounder into a fair value of 127x book.
    The model has nothing to say here and must say nothing.
    """
    re = V.smart_re(30_000)
    assert V._justified_pb(bvps=100.0, roe=0.25, g=re - 0.001, re=re) is None
    assert V._justified_pb(bvps=100.0, roe=0.25, g=0.18, re=re) is None


def test_justified_pb_still_answers_with_a_healthy_spread():
    re = V.smart_re(30_000)
    fv = V._justified_pb(bvps=100.0, roe=0.25, g=0.08, re=re)
    assert fv is not None
    assert 2 < fv / 100 < 10, f"implied P/B of {fv / 100:.1f}x is not sane"


def test_justified_pb_rewards_roe_above_the_discount_rate():
    """A company earning more than its cost of equity deserves a premium to
    book; one earning less deserves a discount. That's the whole model."""
    re = 0.12
    premium = V._justified_pb(bvps=100.0, roe=0.20, g=0.05, re=re)
    discount = V._justified_pb(bvps=100.0, roe=0.08, g=0.05, re=re)
    assert premium > 100 > discount


def test_ddm_has_the_same_floor():
    """Gordon growth divides by the same denominator and needs the same rule."""
    re = 0.12
    assert V._ddm(price=1000, div_yield=2.0, g=re - 0.001, re=re) is None
    assert V._ddm(price=1000, div_yield=2.0, g=0.05, re=re) is not None


def test_ddm_ignores_non_payers():
    assert V._ddm(price=1000, div_yield=0, re=0.12, g=0.05) is None
    assert V._ddm(price=1000, div_yield=None, re=0.12, g=0.05) is None


def test_min_spread_is_the_documented_two_points():
    """A magic number worth pinning: the guide states it."""
    approx(V.MIN_SPREAD, 0.02)


# ------------------------------------------------------------ Graham models
def test_graham_number():
    """sqrt(22.5 x EPS x BVPS) — the 1930s defensive ceiling."""
    approx(V._graham_number(eps=20.0, bvps=100.0), (22.5 * 20 * 100) ** 0.5)


def test_graham_number_rejects_losses():
    """A negative EPS has no square root and no meaning here."""
    assert V._graham_number(eps=-5.0, bvps=100.0) is None


def test_graham_revised_takes_growth_in_percent():
    """The unit trap in this file: g_pct is 12 for 12%, not 0.12.

    Passing the fraction collapses the valuation instead of erroring, which is
    the worst way for a unit bug to behave.
    """
    as_pct = V._graham_revised(eps=20.0, g_pct=12.0, bondY=0.072)
    as_frac = V._graham_revised(eps=20.0, g_pct=0.12, bondY=0.072)
    assert as_pct > as_frac * 2


# ---------------------------------------------------------------------- DCF
def test_dcf_rises_with_growth():
    a = V._dcf_eps(20.0, 0.05, 10, 0.12, 0.05)
    b = V._dcf_eps(20.0, 0.15, 10, 0.12, 0.05)
    assert b > a


def test_dcf_falls_as_the_discount_rate_rises():
    """Riskier company, same cash flows, lower value today."""
    cheap = V._dcf_eps(20.0, 0.10, 10, 0.11, 0.05)
    dear = V._dcf_eps(20.0, 0.10, 10, 0.16, 0.05)
    assert cheap > dear


def test_dcf_rejects_negative_earnings():
    assert V._dcf_eps(-3.0, 0.10, 10, 0.12, 0.05) is None


# ----------------------------------------------------------- composite maths
def _model(**kw):
    base = {"eps": 20.0, "bvps": 100.0, "roe": 18.0, "price": 300.0,
            "div_yield": 1.5, "g_hist": 0.10, "market_cap": 30_000,
            "name": "Test", "pe": 15.0, "eps_hist": []}
    base.update(kw)
    return base


def test_composite_sits_inside_the_model_range():
    """A median can never be an extrapolation."""
    m = _model()
    fv = V._composite_at(m, 0.10, 0.124, 10, 0.05, 0.072)
    assert fv is not None
    roe_frac = m["roe"] / 100
    vals = [v for v in (
        V._dcf_eps(m["eps"], 0.10, 10, 0.124, 0.05),
        V._graham_number(m["eps"], m["bvps"]),
        V._graham_revised(m["eps"], 10.0, 0.072),
        V._justified_pb(m["bvps"], roe_frac, 0.10, 0.124),
        V._ddm(m["price"], m["div_yield"], 0.10, 0.124),
    ) if v is not None and v > 0]
    assert min(vals) <= fv <= max(vals)


def test_composite_survives_a_loss_maker():
    """Three of five models need positive EPS. The rest must still answer."""
    m = _model(eps=-5.0)
    assert V._composite_at(m, 0.08, 0.124, 10, 0.05, 0.072) is not None


def test_composite_is_none_when_nothing_can_be_valued():
    m = _model(eps=None, bvps=None, roe=None, div_yield=None)
    assert V._composite_at(m, 0.10, 0.124, 10, 0.05, 0.072) is None


# ------------------------------------------------------------------ margin
def test_margin_of_safety_direction():
    """Positive MoS means the price is below fair value, not above it."""
    fair, price = 500.0, 350.0
    mos = (fair - price) / fair * 100
    approx(mos, 30.0)
    assert V._verdict(mos) == "undervalued"


# --------------------------------------------------------------- scenarios
def test_scenarios_are_ordered():
    """Bear must not value a company above bull, whatever the inputs."""
    m = _model()
    a = {"re": 0.124, "g_term": 0.05, "years": 10, "bondY": 0.072,
         "g_cap": 0.18}
    s = V.scenarios(m, a)
    assert s["bear"]["fair"] <= s["base"]["fair"] <= s["bull"]["fair"]


def test_scenarios_move_both_levers():
    """Bear is lower growth AND a higher discount rate — not just one."""
    m = _model()
    a = {"re": 0.124, "g_term": 0.05, "years": 10, "bondY": 0.072,
         "g_cap": 0.18}
    s = V.scenarios(m, a)
    assert s["bear"]["g_pct"] < s["base"]["g_pct"] < s["bull"]["g_pct"]
    assert s["bear"]["re_pct"] > s["base"]["re_pct"] > s["bull"]["re_pct"]


# ------------------------------------------------------------- sensitivity
def test_sensitivity_grid_shape():
    m = _model()
    a = {"re": 0.124, "g_term": 0.05, "years": 10, "bondY": 0.072,
         "g_cap": 0.18}
    s = V.sensitivity(m, a)
    assert len(s["grid"]) == len(s["g_axis_pct"])
    assert all(len(row) == len(s["re_axis_pct"]) for row in s["grid"])


def test_sensitivity_is_monotonic_across_the_discount_rate():
    """Along any row, a higher discount rate must not raise fair value."""
    m = _model()
    a = {"re": 0.124, "g_term": 0.05, "years": 10, "bondY": 0.072,
         "g_cap": 0.18}
    s = V.sensitivity(m, a)
    for row in s["grid"]:
        vals = [c["fair"] for c in row if c["fair"] is not None]
        assert vals == sorted(vals, reverse=True), \
            f"fair value rose with the discount rate: {vals}"


def test_sensitivity_axes_bracket_the_base_case():
    """The grid must show you being wrong in both directions, not just one."""
    m = _model()
    a = {"re": 0.124, "g_term": 0.05, "years": 10, "bondY": 0.072,
         "g_cap": 0.18}
    s = V.sensitivity(m, a)
    assert min(s["re_axis_pct"]) < 12.4 < max(s["re_axis_pct"])


# ------------------------------------------------------------------- units
def test_roe_is_converted_from_percent_before_use():
    """screener reports ROE as 18, the models want 0.18.

    Passing 18 straight through gives (18 - g) / (re - g) — a fair value two
    orders of magnitude too high, with no error to notice.
    """
    re, g = 0.124, 0.08
    correct = V._justified_pb(bvps=100.0, roe=0.18, g=g, re=re)
    wrong = V._justified_pb(bvps=100.0, roe=18.0, g=g, re=re)
    assert wrong > correct * 50
