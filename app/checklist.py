"""Pre-trade checklist — the warnings you'd want before an entry, on the record.

Runs at the moment a trade is logged (/log, the web journal, the CLI) and when
a semi-auto order is proposed. It never blocks — the hard limits live in
`risk.check_limits` and `semiauto.check` — it WARNS, and the warnings are
stored on the trade. That's the part that matters: months later the journal
can answer "what did ignoring the earnings warning actually cost me?" with
your own numbers instead of a rule of thumb.

The checks:

  results_soon    results due within RESULTS_WARN_DAYS — a gap risk the stop
                  can't contain
  low_rr          a target was given and reward:risk is under MIN_RR
  against_regime  long while Nifty is trending down or below its 200-DMA;
                  short while it's trending up
  high_vol        the tape is in its high-volatility regime — stops get run
  correlated      60-session return correlation ≥ CORR_WARN with a position
                  already open on the same side: more of the same bet
  insider_selling promoters/directors sold on market, or a pledge was invoked,
                  in the last 90 days (longs only)

Every check is isolated: one that can't get its data is skipped, never fatal.
A checklist that stopped you logging a trade because NSE was down would be
the wrong way round.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime

from . import candles, config, instruments

CHECKS = {
    "results_soon": "Results due soon",
    "low_rr": "Reward:risk below minimum",
    "against_regime": "Against the market regime",
    "high_vol": "High-volatility tape",
    "correlated": "Correlated with an open position",
    "insider_selling": "Insider selling / pledge invoked",
}
RESULTS_WARN_DAYS = 5
MIN_RR = 1.5
CORR_WARN = 0.7
CORR_SESSIONS = 60


def _cfg(key: str, default: float) -> float:
    try:
        v = config.get(key, default)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ checks
def check_results(symbol: str, today: date | None = None) -> str | None:
    from . import results as R
    today = today or date.today()
    card = R.card(symbol, today=today)
    if not card or card.get("released") or not card.get("date"):
        return None
    try:
        when = datetime.fromisoformat(str(card["date"])[:10]).date()
    except ValueError:
        return None
    days = (when - today).days
    limit = int(_cfg("RESULTS_WARN_DAYS", RESULTS_WARN_DAYS))
    if 0 <= days <= limit:
        return ("results today" if days == 0
                else f"results in {days} day{'s' if days != 1 else ''} ({when:%d %b})")
    return None


def check_rr(entry: float, stop: float | None, target: float | None,
             side: str) -> str | None:
    if stop is None or target is None:
        return None
    risk = abs(entry - stop)
    reward = (target - entry) if side == "long" else (entry - target)
    if risk <= 0:
        return None
    rr = reward / risk
    floor = _cfg("MIN_RR", MIN_RR)
    if rr < floor:
        return f"reward:risk {rr:.2f} (minimum {floor:g})"
    return None


def check_regime(side: str, reg: dict | None) -> tuple[str | None, str | None]:
    """-> (against_regime detail, high_vol detail). Pure given `reg`."""
    if not reg:
        return None, None
    lab = reg.get("label")
    against = None
    if side == "long":
        if lab == "trend-down":
            against = "long while Nifty trends down"
        elif reg.get("above_200") is False:
            against = "long while Nifty is below its 200-DMA"
    elif side == "short" and lab == "trend-up":
        against = "short while Nifty trends up"
    hv = "Nifty volatility in the top 20% of its year" if lab == "high-vol" else None
    return against, hv


def _returns(symbol: str, n: int, kite=None) -> dict[str, float]:
    inst = instruments.lookup(symbol)
    if not inst:
        return {}
    tok = inst["instrument_token"]
    if kite:
        try:
            candles.ensure(kite, tok, "day", n * 2 + 10)
        except Exception:                                # noqa: BLE001
            pass
    c = candles.get(tok, "day", n * 2 + 10)[-(n + 1):]
    out = {}
    for i in range(1, len(c)):
        if c[i - 1]["close"]:
            d = datetime.fromtimestamp(c[i]["time"]).strftime("%Y-%m-%d")
            out[d] = c[i]["close"] / c[i - 1]["close"] - 1
    return out


def correlation(a: dict[str, float], b: dict[str, float]) -> float | None:
    """Pearson on the dates both series share. Pure."""
    days = sorted(set(a) & set(b))
    if len(days) < 20:
        return None
    xs, ys = [a[d] for d in days], [b[d] for d in days]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def check_correlated(symbol: str, side: str, opens: list[dict],
                     kite=None) -> str | None:
    others = sorted({t["symbol"].upper() for t in opens
                     if t["side"] == side and t["symbol"].upper() != symbol.upper()})
    if not others:
        return None
    mine = _returns(symbol, CORR_SESSIONS, kite)
    if not mine:
        return None
    floor = _cfg("CORR_WARN", CORR_WARN)
    hits = []
    for o in others:
        r = correlation(mine, _returns(o, CORR_SESSIONS, kite))
        if r is not None and r >= floor:
            hits.append((r, o))
    if not hits:
        return None
    hits.sort(reverse=True)
    return "moves with " + ", ".join(f"{o} ({r:.2f})" for r, o in hits[:3])


def check_insider(symbol: str, side: str) -> str | None:
    if side != "long":
        return None
    from . import insider
    n = insider.net(symbol, days=90)
    if n["invoked"]:
        return "a lender invoked a promoter pledge in the last 90 days"
    if n["trades"] and n["value"] < 0:
        return f"insiders net sold {insider._money(-n['value'])} on market in 90 days"
    return None


# --------------------------------------------------------------------- run
def run(symbol: str, entry: float, stop: float | None = None,
        side: str = "long", target: float | None = None,
        kite=None, opens: list[dict] | None = None,
        reg: dict | None = None, today: date | None = None) -> list[dict]:
    """Every warning that applies. [] means a clean entry.

    `opens` and `reg` can be passed in (tests, or a caller that already has
    them); otherwise they're read here.
    """
    from . import journal
    side = side.lower()
    out: list[dict] = []

    def add(key: str, detail: str | None) -> None:
        if detail:
            out.append({"key": key, "label": CHECKS[key], "detail": detail})

    def safe(fn, *a):
        try:
            return fn(*a)
        except (Exception, SystemExit):                  # noqa: BLE001
            return None

    add("results_soon", safe(check_results, symbol, today))
    add("low_rr", safe(check_rr, entry, stop, target, side))
    if reg is None:
        from . import regime
        reg = safe(regime.current, kite)
    against, hv = check_regime(side, reg)
    add("against_regime", against)
    add("high_vol", hv)
    if opens is None:
        opens = safe(journal.open_positions) or []
    add("correlated", safe(check_correlated, symbol, side, opens, kite))
    add("insider_selling", safe(check_insider, symbol, side))
    return out


def encode(warnings: list[dict]) -> str:
    """For the `checks` column. An EMPTY list is stored too — "checked and
    clean" is what the comparison needs as its baseline, and it's different
    from NULL, "never checked"."""
    return json.dumps([{"key": w["key"], "detail": w["detail"]} for w in warnings])


def decode(raw: str | None) -> list[dict] | None:
    if raw is None:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except ValueError:
        return None


def as_lines(warnings: list[dict]) -> list[str]:
    return [f"⚠ {w['label']}: {w['detail']}" for w in warnings]


# ------------------------------------------------------------------ review
def report(since: str | None = None) -> dict:
    """What each warning has cost, measured on your closed trades.

    For every check: stats on closed trades that carried the warning, against
    closed trades that were checked and came back clean. Trades logged before
    the checklist existed (checks IS NULL) are left out of both sides —
    they're not clean, they're unknown.
    """
    from . import journal
    rows = journal.closed_trades(since=since, limit=100_000,
                                 exclude_source="paper")
    checked = [(t, decode(t.get("checks"))) for t in rows]
    checked = [(t, w) for t, w in checked if w is not None]
    clean = [t for t, w in checked if not w]
    out = {"checked": len(checked), "unchecked": len(rows) - len(checked),
           "clean": journal.stats(clean), "checks": []}
    for key, label in CHECKS.items():
        flagged = [t for t, w in checked if any(x.get("key") == key for x in w)]
        s = journal.stats(flagged)
        out["checks"].append({"key": key, "label": label, "flagged": s,
                              "cost_r": (round(s["expectancy_r"]
                                               - out["clean"]["expectancy_r"], 2)
                                         if s["expectancy_r"] is not None
                                         and out["clean"]["expectancy_r"] is not None
                                         else None)})
    return out


def report_text(rep: dict | None = None) -> str:
    rep = rep or report()
    c = rep["clean"]
    lines = [f"Checklist review — {rep['checked']} checked trade(s)"
             + (f", {rep['unchecked']} from before the checklist"
                if rep["unchecked"] else "")]
    if not rep["checked"]:
        lines.append("Nothing to compare yet — close some trades logged "
                     "since the checklist went in.")
        return "\n".join(lines)
    ce = "—" if c["expectancy_r"] is None else f"{c['expectancy_r']:+.2f}R"
    lines.append(f"  clean entries   {c['trades']:>3}t  {ce}/trade")
    for row in rep["checks"]:
        s = row["flagged"]
        if not s["trades"]:
            continue
        e = f"{s['expectancy_r']:+.2f}R" if s["expectancy_r"] is not None else "—"
        cost = (f"  ({row['cost_r']:+.2f}R vs clean)"
                if row["cost_r"] is not None else "")
        lines.append(f"  {row['label'][:24]:<24} {s['trades']:>3}t  {e}/trade{cost}")
    lines.append("Few trades per row = noise; read it after ~20 per warning.")
    return "\n".join(lines)
