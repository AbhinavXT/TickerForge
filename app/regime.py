"""Market regime — what kind of tape a trade was taken in.

One label per day, from Nifty 50's daily bars:

  * **high-vol** — Nifty's 20-day realised volatility sits in the top
    VOL_PCTILE of its own trailing year. Checked first, because a volatile
    tape behaves like itself whatever the trend: stops get run, gaps get
    big, and both trend and mean-reversion systems take damage.
  * **trend-up / trend-down** — ADX(14) at or above TREND_ADX, with the
    direction from close against its 50-day average. ADX measures strength
    only; the average supplies the side.
  * **choppy** — everything else: no strong trend, normal volatility.

ADX 20 rather than the textbook 25 because this is an index, not a stock —
Nifty's ADX spends long stretches between 15 and 30, and 25 calls most real
trends choppy.

The point is splitting results, not forecasting. A strategy with +0.4R
expectancy overall might be +1.1R in trends and −0.6R in chop, and that's
a strategy with an off switch, not a mediocre one.

**No lookahead**, same rule as `backtest.regime_gate`: a bar is labelled by
the most recent index bar that had already closed by the day before.
"""
from __future__ import annotations

import math

from . import indicators as ind

LABELS = ("trend-up", "trend-down", "choppy", "high-vol")
TREND_ADX = 20.0
VOL_WINDOW = 20
VOL_LOOKBACK = 252
VOL_PCTILE = 80.0           # top 20% of the trailing year = high-vol
INDEX = "NIFTY 50"


def _realised_vol(closes: list[float], n: int) -> list:
    out: list = [None] * len(closes)
    rets = [None] + [math.log(closes[i] / closes[i - 1])
                     if closes[i - 1] and closes[i] else None
                     for i in range(1, len(closes))]
    for i in range(n, len(closes)):
        w = [r for r in rets[i - n + 1:i + 1] if r is not None]
        if len(w) < n - 1:
            continue
        m = sum(w) / len(w)
        out[i] = math.sqrt(sum((r - m) ** 2 for r in w) / len(w)) * math.sqrt(252)
    return out


def _pctile_rank(values: list, i: int, lookback: int) -> float | None:
    v = values[i]
    if v is None:
        return None
    hist = [x for x in values[max(0, i - lookback + 1):i + 1] if x is not None]
    if len(hist) < lookback // 2:
        return None              # not enough history to call anything extreme
    # Mid-rank for ties: with `<=`, a perfectly steady volatility ranks at
    # 100% of its own history and reads as extreme.
    below = sum(1 for x in hist if x < v)
    ties = sum(1 for x in hist if x == v)
    return (below + ties / 2) / len(hist) * 100


def classify(nifty: list[dict]) -> list[str | None]:
    """One label per index bar (None during warm-up). Pure."""
    n = len(nifty)
    if n < 60:
        return [None] * n
    closes = [b["close"] for b in nifty]
    adx = ind.adx(nifty, 14)
    sma50 = ind.sma(closes, 50)
    vol = _realised_vol(closes, VOL_WINDOW)
    out: list[str | None] = []
    for i in range(n):
        pr = _pctile_rank(vol, i, VOL_LOOKBACK)
        if pr is not None and pr >= VOL_PCTILE:
            out.append("high-vol")
            continue
        if adx[i] is None or sma50[i] is None:
            out.append(None)
            continue
        if adx[i] >= TREND_ADX:
            out.append("trend-up" if closes[i] >= sma50[i] else "trend-down")
        else:
            out.append("choppy")
    return out


def labels_for(times: list[int], nifty: list[dict], lag_days: int = 1) -> list:
    """Align index labels onto another series' bar times, without lookahead.
    Same walk as `backtest.regime_gate`."""
    if not nifty:
        return [None] * len(times)
    labels = classify(nifty)
    stamps = [b["time"] for b in nifty]
    out, j = [], -1
    lag = lag_days * 86400
    for t in times:
        while j + 1 < len(stamps) and stamps[j + 1] <= t - lag:
            j += 1
        out.append(labels[j] if j >= 0 else None)
    return out


def split_trades(trades: list[dict], times: list[int], labels: list,
                 stats_fn) -> dict:
    """Group trades by the regime on their ENTRY bar and run `stats_fn` on
    each group. -> {label: stats + {"share_pct"}} in LABELS order, plus
    "unknown" for warm-up entries."""
    at = dict(zip(times, labels))
    groups: dict[str, list] = {}
    for t in trades:
        lab = at.get(t["entry_time"]) or "unknown"
        t["regime"] = lab
        groups.setdefault(lab, []).append(t)
    closed_total = sum(1 for t in trades if not t.get("open")) or 1
    days_total = sum(1 for x in labels if x) or 1
    out = {}
    for lab in LABELS + ("unknown",):
        if lab not in groups and lab == "unknown":
            continue
        g = groups.get(lab, [])
        s = stats_fn(g)
        s["share_pct"] = round(sum(1 for t in g if not t.get("open"))
                               / closed_total * 100, 1)
        s["days_pct"] = (round(sum(1 for x in labels if x == lab)
                               / days_total * 100, 1)
                         if lab != "unknown" else None)
        out[lab] = s
    return out


def verdict(split: dict, key: str = "expectancy_pct", min_trades: int = 5,
            unit: str = "%") -> str | None:
    """One sentence on whether the edge depends on the regime. None when
    there aren't enough trades in at least two regimes to say."""
    rows = [(lab, s) for lab, s in split.items()
            if lab in LABELS and (s.get("trades") or 0) >= min_trades
            and s.get(key) is not None]
    if len(rows) < 2:
        return None
    best = max(rows, key=lambda r: r[1][key])
    worst = min(rows, key=lambda r: r[1][key])
    if best[1][key] > 0 and worst[1][key] < 0:
        return (f"Edge depends on the regime: {best[1][key]:+.2f}{unit} per "
                f"trade in {best[0]}, {worst[1][key]:+.2f}{unit} in {worst[0]}. "
                f"Consider switching it off in {worst[0]}.")
    if worst[1][key] > 0:
        return "Positive in every regime with enough trades to judge."
    if best[1][key] < 0:
        return "Negative in every regime with enough trades to judge."
    return None


def current(kite=None) -> dict | None:
    """Today's label from the last CLOSED Nifty bar."""
    from datetime import datetime

    from . import candles, instruments
    inst = instruments.lookup(INDEX)
    if not inst:
        return None
    tok = inst["instrument_token"]
    try:
        if kite:
            candles.ensure(kite, tok, "day", 420)
    except Exception:                                    # noqa: BLE001
        pass
    c = candles.get(tok, "day", 420)
    today = datetime.now().strftime("%Y-%m-%d")
    c = [b for b in c
         if datetime.fromtimestamp(b["time"]).strftime("%Y-%m-%d") != today]
    labels = classify(c)
    if not labels or labels[-1] is None:
        return None
    closes = [b["close"] for b in c]
    sma200 = ind.sma(closes, 200)
    return {"label": labels[-1],
            "above_200": (closes[-1] > sma200[-1]) if sma200[-1] else None,
            "close": closes[-1]}
