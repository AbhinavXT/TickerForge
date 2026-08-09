"""Offline tests for the position-sizing and risk model — pure functions.

Run:  python -m pytest tests/test_risk.py
"""
import pytest

from app import risk


def approx(a, b, tol=0.01):
    assert abs(a - b) <= tol, f"{a} != {b}"


# ------------------------------------------------------------------ sizing
def test_size_basic():
    """₹5L account, 1% risk = ₹5000. Stop is ₹7 away -> 714 shares."""
    s = risk.size(entry=405, stop=398, account=500_000, risk_pct=1)
    assert s["qty"] == 714
    approx(s["r_per_share"], 7.0)
    approx(s["risk_amount"], 4998.0)          # 714 x 7, just under the budget
    assert s["side"] == "long"


def test_size_rounds_down_never_up():
    """Rounding up would risk more than you chose. That direction is one-way."""
    s = risk.size(entry=100, stop=97, account=100_000, risk_pct=1)
    assert s["qty"] == 333                     # 1000/3 = 333.33
    assert s["risk_amount"] <= 1000


def test_wider_stop_gives_fewer_shares():
    """The whole point: rupee risk stays constant, size absorbs the difference."""
    tight = risk.size(entry=400, stop=396, account=500_000, risk_pct=1)
    wide = risk.size(entry=400, stop=380, account=500_000, risk_pct=1)
    assert wide["qty"] < tight["qty"]
    approx(tight["risk_amount"], wide["risk_amount"], tol=20)


def test_short_side_inferred_from_stop_above_entry():
    s = risk.size(entry=400, stop=410, account=500_000, risk_pct=1)
    assert s["side"] == "short"
    assert s["qty"] == 500                     # 5000 / 10


def test_lot_size_respected():
    s = risk.size(entry=100, stop=97, account=1_000_000, risk_pct=1, lot_size=50)
    assert s["qty"] % 50 == 0
    assert s["qty"] == 3300                    # 3333 floored to the lot


def test_ordinary_trade_is_not_shrunk():
    """The behaviour that matters most: a normal trade returns the size the
    risk model asked for. A 1.7% stop with 1% risk genuinely needs 58% of the
    account — surprising, but it's what risking 1% means. Shrinking it would
    silently reduce risk below what was chosen."""
    s = risk.size(entry=405, stop=398, account=500_000, risk_pct=1)
    assert s["qty"] == 714
    assert s["capped"] is False
    approx(s["risk_amount"], s["risk_budget"], tol=10)


def test_large_but_affordable_position_warns_without_shrinking():
    s = risk.size(entry=405, stop=398, account=500_000, risk_pct=1)
    assert s["concentrated"] is True
    assert s["position_pct"] > 30
    assert "in one name" in s["note"]
    assert s["qty"] == 714                     # unchanged — informed, not overruled


def test_hard_cap_binds_when_capital_runs_out():
    """position value = risk% / stop-distance%. A 0.25% stop with 1% risk
    demands 400% of the account, which is an order you cannot place."""
    s = risk.size(entry=100, stop=99.75, account=100_000, risk_pct=1)
    assert s["capped"] is True
    assert s["position_value"] <= 100_000
    assert s["risk_amount"] < s["risk_budget"]   # less risk than asked, by necessity
    assert "reduced to fit" in s["note"]


def test_margin_account_can_raise_the_cap():
    s = risk.size(entry=100, stop=99.75, account=100_000, risk_pct=1,
                  max_position_pct=400)
    assert s["capped"] is False
    approx(s["risk_amount"], 1000, tol=5)


def test_modest_position_is_neither_capped_nor_concentrated():
    """1% risk with a 10% stop = 10% of the account. Nothing to flag."""
    s = risk.size(entry=100, stop=90, account=500_000, risk_pct=1)
    assert s["capped"] is False and s["concentrated"] is False
    assert s["note"] == ""
    approx(s["position_pct"], 10.0, tol=0.5)


def test_capped_and_concentrated_are_mutually_exclusive():
    """A capped position is already at the ceiling; saying it's also 'large'
    would be two warnings for one condition."""
    s = risk.size(entry=100, stop=99.75, account=100_000, risk_pct=1)
    assert s["capped"] and not s["concentrated"]


def test_zero_qty_explains_itself():
    """Silently returning 0 looks like a bug at the moment you're placing a trade."""
    s = risk.size(entry=5000, stop=4000, account=10_000, risk_pct=1)
    assert s["qty"] == 0
    assert "won't cover one share" in s["note"]


# ------------------------------------------------------------- bad inputs
@pytest.mark.parametrize("kwargs", [
    dict(entry=100, stop=100, account=100_000, risk_pct=1),   # stop == entry
    dict(entry=0, stop=95, account=100_000, risk_pct=1),
    dict(entry=100, stop=0, account=100_000, risk_pct=1),
    dict(entry=100, stop=95, account=0, risk_pct=1),
    dict(entry=100, stop=95, account=100_000, risk_pct=0),
    dict(entry=100, stop=95, account=100_000, risk_pct=150),
])
def test_bad_inputs_raise(kwargs):
    with pytest.raises(risk.RiskError):
        risk.size(**kwargs)


def test_stop_equal_entry_message_is_useful():
    with pytest.raises(risk.RiskError, match="undefined risk"):
        risk.r_value(100, 100)


# ----------------------------------------------------------------- targets
def test_targets_long_and_short():
    t = risk.targets(400, 390)                 # long, 1R = 10
    assert t["1R"] == 410 and t["2R"] == 420 and t["3R"] == 430
    t = risk.targets(400, 410)                 # short, 1R = 10
    assert t["1R"] == 390 and t["3R"] == 370


# ------------------------------------------------------------- R multiples
def test_r_multiple_long():
    approx(risk.r_multiple(400, 390, 420), 2.0)      # made twice what it risked
    approx(risk.r_multiple(400, 390, 390), -1.0)     # stop did its job
    approx(risk.r_multiple(400, 390, 405), 0.5)


def test_r_multiple_short():
    approx(risk.r_multiple(400, 410, 380, side="short"), 2.0)
    approx(risk.r_multiple(400, 410, 410, side="short"), -1.0)


def test_r_multiple_infers_side():
    approx(risk.r_multiple(400, 410, 380), 2.0)      # stop above entry = short


# -------------------------------------------------------------- ATR stops
def bars(n=40, base=100.0, rng=4.0):
    return [{"time": i, "open": base, "high": base + rng, "low": base - rng,
             "close": base, "volume": 1000} for i in range(n)]


def test_atr_stop_sits_below_entry_for_longs():
    s = risk.atr_stop(bars(), entry=100, side="long", period=14, mult=2)
    assert s is not None and s["stop"] < 100
    assert s["distance_pct"] > 0


def test_atr_stop_flips_for_shorts():
    s = risk.atr_stop(bars(), entry=100, side="short", period=14, mult=2)
    assert s["stop"] > 100


def test_atr_stop_scales_with_volatility():
    """Same multiple, different instruments — that's the feature."""
    quiet = risk.atr_stop(bars(rng=1.0), entry=100, mult=2)
    wild = risk.atr_stop(bars(rng=8.0), entry=100, mult=2)
    assert wild["distance_pct"] > quiet["distance_pct"]


def test_atr_stop_needs_enough_bars():
    assert risk.atr_stop(bars(5), entry=100, period=14) is None
    assert risk.atr_stop([], entry=100) is None


def test_atr_stop_feeds_size():
    """The two compose: suggest a stop, then size off it."""
    st = risk.atr_stop(bars(), entry=100, side="long", mult=2)
    s = risk.size(entry=100, stop=st["stop"], account=500_000, risk_pct=1)
    assert s["qty"] > 0
    approx(s["r_per_share"], 100 - st["stop"])


# ------------------------------------------------------------ daily guard
def test_guard_allows_a_clean_day():
    g = risk.check_limits(realised_r_today=0.0, daily_loss_r=2)
    assert g["ok"] and g["remaining_r"] == 2.0


def test_guard_allows_a_winning_day():
    g = risk.check_limits(realised_r_today=3.5, daily_loss_r=2)
    assert g["ok"]


def test_guard_blocks_at_the_limit():
    g = risk.check_limits(realised_r_today=-2.0, daily_loss_r=2)
    assert not g["ok"]
    assert "daily loss limit hit" in g["reasons"][0]
    assert g["remaining_r"] == 0.0


def test_guard_counts_open_risk_too():
    """-1.5R realised looks survivable until you notice 1R is still live."""
    alone = risk.check_limits(realised_r_today=-1.5, daily_loss_r=2)
    assert alone["ok"]
    withopen = risk.check_limits(realised_r_today=-1.5, open_risk_r=1.0,
                                 daily_loss_r=2)
    assert not withopen["ok"]
    assert "open positions" in withopen["reasons"][0]
    assert withopen["effective_r"] == -2.5


def test_guard_blocks_on_position_count():
    g = risk.check_limits(realised_r_today=0.0, open_positions=5, max_positions=5)
    assert not g["ok"] and "already open" in g["reasons"][0]


def test_guard_can_report_both_reasons():
    g = risk.check_limits(realised_r_today=-3, open_positions=9, max_positions=5)
    assert len(g["reasons"]) == 2


def test_guard_remaining_never_negative():
    g = risk.check_limits(realised_r_today=-10, daily_loss_r=2)
    assert g["remaining_r"] == 0.0


# -------------------------------------------------------------- formatting
def test_format_size_is_phone_sized():
    out = risk.format_size(risk.size(405, 398, 500_000, 1))
    assert "LONG 714" in out
    assert len(out.splitlines()) <= 5
    assert "targets" in out


def test_format_zero_qty_warns():
    out = risk.format_size(risk.size(5000, 4000, 10_000, 1))
    assert out.startswith("⚠")
