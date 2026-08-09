"""Position sizing and the daily risk guard.

The whole module is built around one idea: **you decide what you're willing to
lose before you decide how much to buy.** Size is an output, never an input.

    risk per trade (₹) = account × risk%
    quantity           = risk per trade ÷ (entry − stop)

The distance from entry to stop is "1R" — one unit of risk. Every trade risks
the same rupee amount regardless of how volatile the stock is, so a wide-stop
trade simply gets fewer shares. That's what makes results across different
trades comparable, and it's the same R the trailing stops and the backtester
already speak in.

Everything here is a pure function of its arguments — no DB, no config reads,
no network — for the same reason `backtest.simulate` is: it gets reused by the
`/size` phone command, the Desk widget, position sizing inside the simulator
(roadmap 3.5) and eventually the bot's hard limits (6.3). A function that
reaches for global state can't serve four callers with different ideas of what
"the account" means.

Sizing math is exact; the guard rails are policy. Both are here, but only the
first is arithmetic — see `size()` vs `check_limits()`.
"""
from __future__ import annotations

from . import indicators as ind

# Two different ideas, deliberately not merged.
#
# The HARD cap is capital you don't have. Risk-based sizing has a property
# that surprises people: position value = risk% ÷ stop-distance%, so a 1% risk
# with a 0.5% stop demands 200% of the account. On a cash account that order
# simply can't be placed. Raise this if you trade on margin.
#
# The WARNING threshold is different in kind — the position is affordable, just
# large. A 2% stop with 1% risk is a perfectly ordinary trade that puts half
# the account in one name, and that is worth *seeing* rather than silently
# preventing. Shrinking it would quietly reduce your risk below what you chose,
# which defeats the point of choosing.
DEFAULT_MAX_POSITION_PCT = 100.0     # hard: can't spend more than you have
DEFAULT_CONCENTRATION_PCT = 30.0     # soft: flag it, don't shrink it
DEFAULT_MIN_QTY = 1


class RiskError(ValueError):
    """Bad inputs — a stop on the wrong side of entry, a zero account, etc."""


def r_value(entry: float, stop: float) -> float:
    """One R in rupees per share: the distance from entry to stop.

    Direction-agnostic on purpose. A long risks entry−stop, a short risks
    stop−entry, and both are just |entry − stop| per share.
    """
    if entry <= 0:
        raise RiskError("entry must be positive")
    if stop <= 0:
        raise RiskError("stop must be positive")
    if abs(entry - stop) < 1e-9:
        raise RiskError("stop equals entry — that's an undefined risk, not a free trade")
    return abs(entry - stop)


def side_of(entry: float, stop: float) -> str:
    """Infer direction from where the stop sits. Below entry = long."""
    return "long" if stop < entry else "short"


def size(entry: float, stop: float, account: float, risk_pct: float,
         max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
         concentration_pct: float = DEFAULT_CONCENTRATION_PCT,
         lot_size: int = 1) -> dict:
    """How many shares to buy, given what you're willing to lose.

    Returns the quantity plus every intermediate number, because the
    intermediates are what make the answer checkable at a glance — a size you
    can't sanity-check is a size you won't trust at 09:20.

    Two flags to read:

      * `capped` — the position was REDUCED because it exceeded available
        capital. Your actual rupee risk is now lower than you asked for, and
        `risk_amount` vs `risk_budget` shows by how much.
      * `concentrated` — the position is affordable but large. Nothing was
        changed; you're just being told.
    """
    if account <= 0:
        raise RiskError("account size must be positive")
    if risk_pct <= 0:
        raise RiskError("risk % must be positive")
    if risk_pct > 100:
        raise RiskError("risk % above 100 — check the units")

    per_share = r_value(entry, stop)
    risk_budget = account * risk_pct / 100.0
    raw_qty = risk_budget / per_share

    qty = int(raw_qty // lot_size) * lot_size      # always round DOWN
    capped = False

    ceiling_value = account * max_position_pct / 100.0
    if qty * entry > ceiling_value:
        qty = int((ceiling_value / entry) // lot_size) * lot_size
        capped = True

    side = side_of(entry, stop)
    if qty < max(DEFAULT_MIN_QTY, lot_size):
        # Rounding down is right — never round INTO more risk than you chose —
        # but it can land on zero, and silently returning 0 looks like a bug at
        # the moment you're trying to place a trade. Say why.
        why = (f"₹{ceiling_value:,.0f} of capital won't buy one "
               f"{'lot' if lot_size > 1 else 'share'} at ₹{entry:,.2f}"
               if capped else
               f"risk budget ₹{risk_budget:,.0f} won't cover one share at "
               f"₹{per_share:,.2f} of risk — raise the risk %, or find a "
               f"tighter stop")
        return {
            "qty": 0, "side": side, "entry": entry, "stop": stop,
            "r_per_share": round(per_share, 2),
            "risk_amount": 0.0, "risk_budget": round(risk_budget, 2),
            "position_value": 0.0, "position_pct": 0.0,
            "capped": capped, "concentrated": False,
            "risk_pct": risk_pct, "account": account,
            "note": f"no position — {why}",
        }

    position_value = qty * entry
    position_pct = position_value / account * 100
    actual_risk = qty * per_share
    concentrated = (not capped) and position_pct > concentration_pct

    if capped:
        note = (f"reduced to fit {max_position_pct:g}% of capital — risking "
                f"₹{actual_risk:,.0f} instead of the ₹{risk_budget:,.0f} you "
                f"asked for. A stop {abs(entry-stop)/entry*100:.1f}% away needs "
                f"a bigger position than the account allows.")
    elif concentrated:
        note = (f"{position_pct:.0f}% of capital in one name. Correct for "
                f"{risk_pct:g}% risk with a {abs(entry-stop)/entry*100:.1f}% "
                f"stop — tight stops need big positions — but size it down if "
                f"a gap through the stop would hurt more than 1R.")
    else:
        note = ""

    return {
        "qty": qty,
        "side": side,
        "entry": entry,
        "stop": stop,
        "r_per_share": round(per_share, 2),
        "risk_amount": round(actual_risk, 2),        # what you ACTUALLY risk
        "risk_budget": round(risk_budget, 2),        # what you allowed
        "position_value": round(position_value, 2),
        "position_pct": round(position_pct, 2),
        "capped": capped,
        "concentrated": concentrated,
        "risk_pct": risk_pct,
        "account": account,
        "note": note,
    }


def targets(entry: float, stop: float, multiples=(1, 2, 3)) -> dict:
    """Price levels at N× risk. Answers 'where is this trade worth 2R?'

    Not a prediction — just arithmetic on the risk unit, so you can see before
    entering whether a plausible target is even 1R away. If the nearest
    resistance sits at 0.6R, the trade is priced badly no matter how good the
    setup looks.
    """
    per_share = r_value(entry, stop)
    long_ = side_of(entry, stop) == "long"
    return {f"{m}R": round(entry + (per_share * m if long_ else -per_share * m), 2)
            for m in multiples}


def atr_stop(candles: list[dict], entry: float | None = None,
             side: str = "long", period: int = 14, mult: float = 2.0) -> dict | None:
    """Suggest a stop `mult` ATRs away, for when you don't have a level in mind.

    A volatility-based stop adapts to the instrument: the same 2×ATR is a wide
    stop on a quiet counter and a tight one on a volatile one, which is the
    behaviour you want. It is NOT better than a structural stop below a real
    swing low — it's the fallback when there isn't an obvious one.
    """
    if not candles or len(candles) < period + 1:
        return None
    a = ind.atr(candles, period)
    latest = next((v for v in reversed(a) if v is not None), None)
    if latest is None or latest <= 0:
        return None

    ref = entry if entry is not None else candles[-1]["close"]
    stop = ref - mult * latest if side == "long" else ref + mult * latest
    if stop <= 0:
        return None
    return {
        "stop": round(stop, 2),
        "atr": round(latest, 2),
        "mult": mult,
        "period": period,
        "distance_pct": round(abs(ref - stop) / ref * 100, 2),
        "ref": round(ref, 2),
    }


def r_multiple(entry: float, stop: float, exit_price: float,
               side: str | None = None) -> float:
    """How many R a completed trade made or lost.

    This is the unit the journal stores and the backtester's expectancy speaks
    in, so live and backtested results can be compared without unit conversion.
    +2.0 means the trade made twice what it risked; −1.0 means the stop did
    exactly its job.
    """
    per_share = r_value(entry, stop)
    side = side or side_of(entry, stop)
    move = (exit_price - entry) if side == "long" else (entry - exit_price)
    return round(move / per_share, 2)


def check_limits(realised_r_today: float, open_risk_r: float = 0.0,
                 daily_loss_r: float = 2.0,
                 open_positions: int = 0, max_positions: int = 5) -> dict:
    """Should you be taking another trade right now?

    Two separate ideas, both worth having:

      * A daily loss limit exists because losing days cluster. Not from any
        mystical property of markets — from you. Three stops in a morning and
        the next trade is usually about the previous three rather than about
        the setup. The limit's job is to make that decision in advance, when
        you're calm, instead of at the moment you're least able to make it.
      * `open_risk_r` counts risk already live in open positions. A daily
        limit that only counts *realised* losses will happily let you open
        four more trades while three are underwater — the day's real exposure
        is the sum of both.

    Returns a decision plus the reason, so a caller can show the number rather
    than just a refusal. This is policy, not arithmetic: a caller is free to
    ignore it, and the point is that ignoring it becomes a visible choice.
    """
    reasons = []
    total_r = realised_r_today - abs(open_risk_r)

    if realised_r_today <= -abs(daily_loss_r):
        reasons.append(
            f"daily loss limit hit: {realised_r_today:+.2f}R realised against a "
            f"{-abs(daily_loss_r):.2f}R limit")
    elif total_r <= -abs(daily_loss_r):
        reasons.append(
            f"{realised_r_today:+.2f}R realised plus {abs(open_risk_r):.2f}R at "
            f"risk in open positions reaches the {abs(daily_loss_r):.2f}R daily limit")

    if open_positions >= max_positions:
        reasons.append(f"{open_positions} positions already open (max {max_positions})")

    remaining = abs(daily_loss_r) + realised_r_today
    return {
        "ok": not reasons,
        "reasons": reasons,
        "realised_r": round(realised_r_today, 2),
        "open_risk_r": round(abs(open_risk_r), 2),
        "effective_r": round(total_r, 2),
        "daily_loss_r": abs(daily_loss_r),
        "remaining_r": round(max(0.0, remaining), 2),
        "open_positions": open_positions,
        "max_positions": max_positions,
    }


def format_size(s: dict) -> str:
    """Phone-sized rendering of a `size()` result."""
    if s["qty"] == 0:
        return f"⚠ no position — {s['note']}"
    lines = [
        f"{s['side'].upper()} {s['qty']} @ {s['entry']:g}  (SL {s['stop']:g})",
        f"risk ₹{s['risk_amount']:,.0f} · 1R = ₹{s['r_per_share']:g}/sh",
        f"value ₹{s['position_value']:,.0f} · {s['position_pct']:g}% of capital",
    ]
    t = targets(s["entry"], s["stop"])
    lines.append("targets  " + " · ".join(f"{k} {v:g}" for k, v in t.items()))
    if s["capped"] or s["concentrated"]:
        lines.append(f"⚠ {s['note']}")
    return "\n".join(lines)
