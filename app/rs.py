"""Relative strength: how a stock is doing *against the index*, ranked.

Absolute return is nearly useless for choosing between stocks, because most of
it is the market. A name up 18% in a year the Nifty rose 22% is a laggard
wearing a gain. Relative strength strips the market out and leaves the part
that was the company.

Three windows and why
---------------------
3, 6 and 12 months, blended. One window is a coin flip on your choice of
window — a stock can top a 3-month list on one earnings pop and a 12-month
list on something that finished six months ago. Agreement across horizons is
the actual signal.

The 12-month figure **skips the most recent month**. This is not fussiness:
short-horizon reversal is one of the most reliable effects in equities, and
the classic momentum factor is defined as 12-minus-1 precisely because
including the last month contaminates a year of trend with a few weeks of
mean reversion. The 3-month window keeps its recent bars, so recency is still
represented — it just isn't allowed to poison the long horizon too.

Ranking, not scoring
--------------------
The blended number means nothing on its own; "RS of 0.14" is not a fact anyone
can act on. What's usable is the percentile across a universe: RS 85 means
stronger than 85% of the Nifty 200. That's the form the classic pairing needs
— a breakout scan filtered to RS > 70.
"""
from __future__ import annotations

from . import candles, instruments

INDEX = "NIFTY 50"

# Trading days, roughly. Approximate on purpose: a window defined in calendar
# months would change length with holidays, and comparing a 61-bar return to a
# 64-bar one across stocks is a subtler error than the imprecision it fixes.
LOOKBACKS = {"r3m": 63, "r6m": 126, "r12m": 252}

# Recent horizons carry more weight, but not so much that the blend becomes
# the 3-month number with extra steps.
WEIGHTS = {"r3m": 0.4, "r6m": 0.3, "r12m": 0.3}

SKIP_RECENT = 21          # ~1 month, excluded from the 12m window only
MIN_BARS = 130            # below this, only the short windows are meaningful


class RSError(ValueError):
    pass


def _aligned(stock: list[dict], index: list[dict]) -> tuple[list[float], list[float]]:
    """Closes for the days BOTH series traded.

    Aligning by position instead would silently compare different dates the
    moment one series is missing a bar — a suspension, a late listing, an
    exchange holiday one of them observed. The bug that produces is invisible:
    every number still looks plausible.
    """
    by_time = {b["time"]: b["close"] for b in index}
    s, i = [], []
    for bar in stock:
        hit = by_time.get(bar["time"])
        if hit is not None:
            s.append(bar["close"])
            i.append(hit)
    return s, i


def _rel(stock: list[float], index: list[float], bars: int,
         skip: int = 0) -> float | None:
    """Ratio-based relative return over a window ending `skip` bars ago.

    Ratio rather than subtraction because returns compound: a stock up 50%
    while the index is up 20% has outperformed by 25%, not 30%.
    """
    end = len(stock) - skip
    start = end - bars
    if start < 0 or end <= start:
        return None
    s0, s1 = stock[start], stock[end - 1]
    i0, i1 = index[start], index[end - 1]
    if s0 <= 0 or i0 <= 0:
        return None
    return ((s1 / s0) / (i1 / i0)) - 1


def score_one(stock: list[dict], index: list[dict]) -> dict:
    """Relative returns and their blend for one stock."""
    s, i = _aligned(stock, index)
    out: dict = {"bars": len(s)}
    for key, bars in LOOKBACKS.items():
        skip = SKIP_RECENT if key == "r12m" else 0
        out[key] = _rel(s, i, bars, skip)

    # Blend over whatever windows exist, re-weighted so a young listing isn't
    # penalised for having no 12-month history — it's ranked on what it has,
    # and `bars` is reported so the thinness is visible.
    parts = [(WEIGHTS[k], out[k]) for k in LOOKBACKS if out[k] is not None]
    total = sum(w for w, _ in parts)
    out["score"] = (round(sum(w * v for w, v in parts) / total, 4)
                    if total else None)
    return out


def percentile_ranks(scores: dict[str, float | None]) -> dict[str, float | None]:
    """Percentile-rank scores across a universe, 0–100.

    Ties share the midpoint of the band they occupy, so twenty identical
    scores don't hand one of them an arbitrary advantage over the others.
    Unscorable names get None rather than 0 — no history is not the same fact
    as terrible performance, and 0 would rank them below genuine laggards.

    Bisected rather than counted. The counting form is O(n²), which was fine
    when this only ever saw a watchlist; the query engine's `percentile()`
    calls it on whole universes, where 2,000 names is four million
    comparisons per expression. Same numbers, same tie rule — `usable` is
    already sorted, so the two counts are exactly the two bisect points.
    """
    import bisect

    usable = sorted(v for v in scores.values() if v is not None)
    n = len(usable)
    out: dict[str, float | None] = {}
    for symbol, value in scores.items():
        if value is None or n == 0:
            out[symbol] = None
            continue
        below = bisect.bisect_left(usable, value)
        equal = bisect.bisect_right(usable, value) - below
        out[symbol] = round((below + equal / 2) / n * 100, 1)
    return out


def _series(symbol: str, days: int) -> list[dict]:
    inst = instruments.lookup(symbol)
    if not inst:
        return []
    try:
        return candles.get(inst["instrument_token"], "day", days)
    except Exception:                             # noqa: BLE001
        return []


def rank(symbols: list[str], days: int = 420) -> list[dict]:
    """Score and rank a list of symbols. Reads stored candles only.

    No fetching: this runs over a couple of hundred names and is meant to be
    cheap enough to sit as a column on the scanner. Backfill the universe
    first — `universe backfill` — and this is instant afterwards.
    """
    index = _series(INDEX, days)
    if not index:
        raise RSError(f"no stored candles for {INDEX} — backfill it first")

    rows, scores = [], {}
    for symbol in symbols:
        stock = _series(symbol, days)
        if not stock:
            rows.append({"symbol": symbol, "score": None, "bars": 0,
                         "r3m": None, "r6m": None, "r12m": None})
            scores[symbol] = None
            continue
        s = score_one(stock, index)
        scores[symbol] = s["score"]
        rows.append({"symbol": symbol, **s})

    pct = percentile_ranks(scores)
    for row in rows:
        row["rs"] = pct[row["symbol"]]
        for k in ("r3m", "r6m", "r12m"):
            if row.get(k) is not None:
                row[k] = round(row[k] * 100, 1)      # report as %
    # Strongest first; unscorable names sink rather than sorting as zero.
    rows.sort(key=lambda r: (r["rs"] is None, -(r["rs"] or 0)))
    return rows


def for_symbols(symbols: list[str], days: int = 420) -> dict[str, dict]:
    """Same as `rank`, keyed by symbol — for joining onto another table."""
    return {r["symbol"]: r for r in rank(symbols, days)}


def summary(rows: list[dict], top: int = 10) -> str:
    """CLI / phone rendering."""
    live = [r for r in rows if r["rs"] is not None]
    if not live:
        return "No relative strength — backfill the universe's candles first."
    lines = [f"Relative strength vs {INDEX} ({len(live)} names)", ""]
    for r in live[:top]:
        lines.append(f"  {r['rs']:>5.1f}  {r['symbol']:<12} "
                     f"3m {r['r3m']:+.0f}%  6m {r['r6m']:+.0f}%")
    return "\n".join(lines)
