"""EOD scanner — run conditions across a universe on closed daily bars.

`signals.py` answers "what did my watchlist do?". This answers "what is
anything doing?", which is a different question and needs a different shape:
many symbols, few conditions, and a result that ranks rather than reports.

Every condition is a pure function of candles, so the whole engine is testable
without Kite. They deliberately reuse `indicators` and `backtest.STRATEGIES` —
a Donchian break here is the same code the backtester trades, so a scan hit is
something you can actually test rather than a separate reimplementation that
drifts from it.

**Closed bars only.** The forming daily candle is dropped, exactly as
`signals.scan` does. A scanner that includes today's partial bar will show you
a 52-week high at 10:15 that isn't one by 15:30, and you'll act on it.

A word on what this is for: a scanner widens the funnel, and a wider funnel
without a filter is just more noise arriving faster. Hits are ranked, capped,
and reported with the numbers behind them so you can dismiss most of them
quickly. That's the intended workflow — dismissing fast, not acting on all.
"""
from __future__ import annotations

from datetime import datetime

from . import candles, db, indicators, instruments, universe
from .signals import drop_forming_bar

MIN_BARS = 60
DEFAULT_LOOKBACK = 400          # ~18 months of daily bars
MAX_HITS = 60                   # a scan returning 300 rows is not a scan


# ------------------------------------------------------------- conditions
# Each returns None (no hit) or a dict of the numbers that made it a hit.
# The dict becomes the row's detail, so a hit is always self-explaining.

def c_52w_high(c, p=None):
    """New 52-week high on the close."""
    p = p or {}
    win = int(p.get("window", 252))
    if len(c) < win:
        return None
    closes = [b["close"] for b in c[-win:]]
    last = closes[-1]
    prior = max(closes[:-1])
    if last <= prior:
        return None
    return {"close": round(last, 2), "prior_high": round(prior, 2),
            "above_pct": round((last / prior - 1) * 100, 2)}


def c_52w_low(c, p=None):
    p = p or {}
    win = int(p.get("window", 252))
    if len(c) < win:
        return None
    closes = [b["close"] for b in c[-win:]]
    last, prior = closes[-1], min(closes[:-1])
    if last >= prior:
        return None
    return {"close": round(last, 2), "prior_low": round(prior, 2),
            "below_pct": round((last / prior - 1) * 100, 2)}


def c_volume_surge(c, p=None):
    """Volume at least N× its own recent average."""
    p = p or {}
    mult = float(p.get("mult", 2.0))
    win = int(p.get("window", 20))
    if len(c) < win + 1:
        return None
    vols = [b.get("volume") or 0 for b in c]
    avg = sum(vols[-win - 1:-1]) / win
    if avg <= 0 or vols[-1] < avg * mult:
        return None
    return {"volume": int(vols[-1]), "avg_volume": int(avg),
            "times": round(vols[-1] / avg, 2)}


def c_golden_cross(c, p=None):
    """Fast SMA crosses above slow SMA on the latest closed bar."""
    p = p or {}
    fast, slow = int(p.get("fast", 50)), int(p.get("slow", 200))
    if len(c) < slow + 2:
        return None
    closes = [b["close"] for b in c]
    f, s = indicators.sma(closes, fast), indicators.sma(closes, slow)
    if None in (f[-1], s[-1], f[-2], s[-2]):
        return None
    if not (f[-2] <= s[-2] and f[-1] > s[-1]):
        return None
    return {f"sma{fast}": round(f[-1], 2), f"sma{slow}": round(s[-1], 2),
            "close": round(closes[-1], 2)}


def c_death_cross(c, p=None):
    p = p or {}
    fast, slow = int(p.get("fast", 50)), int(p.get("slow", 200))
    if len(c) < slow + 2:
        return None
    closes = [b["close"] for b in c]
    f, s = indicators.sma(closes, fast), indicators.sma(closes, slow)
    if None in (f[-1], s[-1], f[-2], s[-2]):
        return None
    if not (f[-2] >= s[-2] and f[-1] < s[-1]):
        return None
    return {f"sma{fast}": round(f[-1], 2), f"sma{slow}": round(s[-1], 2)}


def c_donchian_break(c, p=None):
    """Close breaks above the highest high of the prior N bars."""
    p = p or {}
    n = int(p.get("window", 20))
    if len(c) < n + 2:
        return None
    prior = max(b["high"] for b in c[-n - 1:-1])
    last = c[-1]["close"]
    if last <= prior:
        return None
    return {"close": round(last, 2), "channel_high": round(prior, 2),
            "window": n}


def c_bb_squeeze_fire(c, p=None):
    """Bollinger bandwidth was in its own lowest decile, and price has just
    broken out of the band. A squeeze alone is not a signal — it's a coiled
    spring with no direction. The break is what makes it one."""
    p = p or {}
    period = int(p.get("period", 20))
    mult = float(p.get("mult", 2.0))
    look = int(p.get("lookback", 100))
    if len(c) < max(period, look) + 2:
        return None
    closes = [b["close"] for b in c]
    upper, mid, lower = indicators.bollinger(closes, period, mult)
    widths = [(u - l) / m if None not in (u, m, l) and m else None
              for u, m, l in zip(upper, mid, lower)]
    hist = [w for w in widths[-look:-1] if w is not None]
    if len(hist) < 20 or widths[-2] is None:
        return None
    threshold = sorted(hist)[max(0, int(len(hist) * 0.1))]
    if widths[-2] > threshold:
        return None                      # wasn't squeezed on the prior bar
    last, up, lo = closes[-1], upper[-1], lower[-1]
    if up is None or lo is None:
        return None
    if last > up:
        side = "up"
    elif last < lo:
        side = "down"
    else:
        return None
    return {"side": side, "close": round(last, 2),
            "bandwidth": round(widths[-2], 4),
            "decile": round(threshold, 4)}


def c_above_sma(c, p=None):
    p = p or {}
    n = int(p.get("period", 200))
    if len(c) < n + 1:
        return None
    closes = [b["close"] for b in c]
    m = indicators.sma(closes, n)[-1]
    if m is None or closes[-1] <= m:
        return None
    return {"close": round(closes[-1], 2), f"sma{n}": round(m, 2),
            "above_pct": round((closes[-1] / m - 1) * 100, 2)}


def c_rsi_range(c, p=None):
    p = p or {}
    lo = float(p.get("low", 0))
    hi = float(p.get("high", 30))
    n = int(p.get("period", 14))
    if len(c) < n + 2:
        return None
    v = indicators.rsi([b["close"] for b in c], n)[-1]
    if v is None or not (lo <= v <= hi):
        return None
    return {"rsi": round(v, 1)}


def c_strategy_long(c, p=None):
    """Any backtester strategy that has just flipped long.

    This is the bridge the roadmap wanted: the scan and the backtest run the
    same rule, so a hit is directly testable rather than a lookalike.
    """
    from . import backtest as bt
    p = p or {}
    key = p.get("strategy", "donchian_breakout")
    spec = bt.STRATEGIES.get(key)
    if not spec or len(c) < 60:
        return None
    params = {**spec["params"], **{k: v for k, v in p.items()
                                   if k != "strategy"}}
    pos = spec["fn"](c, params)
    if len(pos) < 2 or not (pos[-1] == 1 and pos[-2] == 0):
        return None
    return {"strategy": key, "name": spec["name"],
            "close": round(c[-1]["close"], 2)}


def c_delivery_spike(c, p=None):
    """Delivery qty at least N× its own average on an up day — accumulation.

    Needs the symbol, not just candles: delivery isn't in Kite's bars. The
    engine passes it as `_symbol`. The delivery session must be the same day
    as the last closed candle, or a stale file would vouch for today's bar.
    """
    from . import delivery
    p = p or {}
    sym = p.get("_symbol")
    if not sym or not c:
        return None
    s = delivery.stats(sym, window=int(p.get("window", delivery.WINDOW)))
    if not s or not s["deliv_ratio"]:
        return None
    bar_day = datetime.fromtimestamp(c[-1]["time"]).strftime("%Y-%m-%d")
    if s["day"] != bar_day:
        return None
    if s["deliv_ratio"] < float(p.get("mult", delivery.SPIKE_RATIO)):
        return None
    if float(p.get("min_pct", 0)) and (s["deliv_pct"] or 0) < float(p["min_pct"]):
        return None
    if len(c) > 1 and c[-1]["close"] <= c[-2]["close"]:
        return None
    return {"times": s["deliv_ratio"], "deliv_pct": s["deliv_pct"],
            "avg_deliv_pct": s["avg_deliv_pct"], "day": s["day"]}


CONDITIONS = {
    "52w_high": {"fn": c_52w_high, "label": "New 52-week high",
                 "params": {"window": 252}},
    "52w_low": {"fn": c_52w_low, "label": "New 52-week low",
                "params": {"window": 252}},
    "volume_surge": {"fn": c_volume_surge, "label": "Volume ≥ N× average",
                     "params": {"mult": 2.0, "window": 20}},
    "golden_cross": {"fn": c_golden_cross, "label": "Golden cross",
                     "params": {"fast": 50, "slow": 200}},
    "death_cross": {"fn": c_death_cross, "label": "Death cross",
                    "params": {"fast": 50, "slow": 200}},
    "donchian_break": {"fn": c_donchian_break, "label": "Donchian breakout",
                       "params": {"window": 20}},
    "bb_squeeze": {"fn": c_bb_squeeze_fire, "label": "Bollinger squeeze firing",
                   "params": {"period": 20, "mult": 2.0, "lookback": 100}},
    "above_sma": {"fn": c_above_sma, "label": "Trading above its SMA",
                  "params": {"period": 200}},
    "rsi_range": {"fn": c_rsi_range, "label": "RSI in a band",
                  "params": {"period": 14, "low": 0, "high": 30}},
    "strategy_long": {"fn": c_strategy_long, "label": "Strategy flipped long",
                      "params": {"strategy": "donchian_breakout"}},
    "delivery_spike": {"fn": c_delivery_spike,
                       "label": "Delivery ≥ N× average on an up day",
                       "params": {"mult": 2.0, "window": 20, "min_pct": 0},
                       "needs_symbol": True},
}


# ------------------------------------------------------------------ engine
def evaluate(c: list[dict], conditions: list,
             symbol: str | None = None) -> dict | None:
    """Run conditions against one symbol's candles.

    Conditions are ANDed: a scan for "52-week high AND volume surge" should
    return only names doing both, because either alone is a list you can't
    read. Returns None unless every condition hits.
    """
    if len(c) < MIN_BARS:
        return None
    detail = {}
    for cond in conditions:
        key = cond if isinstance(cond, str) else cond.get("key")
        params = {} if isinstance(cond, str) else {
            k: v for k, v in cond.items() if k != "key"}
        spec = CONDITIONS.get(key)
        if not spec:
            raise ValueError(f"unknown condition '{key}'")
        if spec.get("needs_symbol"):
            params["_symbol"] = symbol
        hit = spec["fn"](c, params)
        if not hit:
            return None
        detail[key] = hit
    return detail


def _rank_value(detail: dict) -> float:
    """A single number to sort hits by, so the strongest surface first.

    Prefers whatever the conditions actually measured — how far above the old
    high, how many times average volume — rather than an arbitrary score.
    """
    for key, best in (("delivery_spike", "times"),
                      ("volume_surge", "times"),
                      ("52w_high", "above_pct"),
                      ("donchian_break", "close"),
                      ("above_sma", "above_pct"),
                      ("52w_low", "below_pct")):
        d = detail.get(key)
        if d and best in d:
            return abs(float(d[best]))
    return 0.0


def scan(conditions: list, name: str = universe.DEFAULT_UNIVERSE,
         symbols: list[str] | None = None, days: int = DEFAULT_LOOKBACK,
         limit: int = MAX_HITS, now=None) -> dict:
    """Run a scan over a universe using ALREADY-STORED candles.

    Deliberately does not fetch. A scan that quietly pulls 200 symbols would
    take minutes and hit the rate limit; `universe.backfill()` is the job that
    fetches, and it's meant to run overnight. Symbols without enough stored
    history are reported as skipped so the gap is visible rather than looking
    like an absence of hits.
    """
    if not conditions:
        raise ValueError("no conditions — a scan with no filter is a list")

    # Validate names BEFORE touching any candles. Otherwise a typo only
    # surfaces when a symbol is actually evaluated — and if every symbol is
    # skipped for thin history, it never surfaces at all: you get a confident
    # "0 hits" for a condition that doesn't exist.
    for cond in conditions:
        key = cond if isinstance(cond, str) else cond.get("key")
        if key not in CONDITIONS:
            raise ValueError(
                f"unknown condition '{key}' — available: "
                + ", ".join(sorted(CONDITIONS)))

    if symbols:
        rows = []
        for s in symbols:
            inst = instruments.lookup(s)
            if inst:
                rows.append({"tradingsymbol": inst["tradingsymbol"],
                             "token": inst["instrument_token"]})
    else:
        rows = universe.members(name)
    if not rows:
        raise ValueError(
            f"universe '{name}' is empty — build it with "
            f"`cli universe set {name} SYM ...` or from the watchlist")

    hits, skipped, errors = [], [], []
    for m in rows:
        tok = m.get("token")
        if not tok:
            skipped.append(m["tradingsymbol"])
            continue
        try:
            c = drop_forming_bar(candles.get(tok, "day", days), now=now)
        except Exception as e:
            errors.append({"symbol": m["tradingsymbol"], "error": str(e)})
            continue
        if len(c) < MIN_BARS:
            skipped.append(m["tradingsymbol"])
            continue
        try:
            detail = evaluate(c, conditions, symbol=m["tradingsymbol"])
        except ValueError:
            raise
        except Exception as e:
            errors.append({"symbol": m["tradingsymbol"], "error": str(e)})
            continue
        if detail:
            hits.append({
                "symbol": m["tradingsymbol"],
                "close": round(c[-1]["close"], 2),
                "change_pct": (round((c[-1]["close"] / c[-2]["close"] - 1) * 100, 2)
                               if len(c) > 1 and c[-2]["close"] else None),
                "volume": int(c[-1].get("volume") or 0),
                "bar_time": c[-1]["time"],
                "detail": detail,
                "rank": _rank_value(detail),
            })

    hits.sort(key=lambda h: -h["rank"])
    return {
        "universe": None if symbols else name,
        "conditions": conditions,
        "scanned": len(rows) - len(skipped),
        "skipped": skipped,
        "errors": errors,
        "hits": hits[:limit],
        "total_hits": len(hits),
        "truncated": len(hits) > limit,
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def run_saved(name: str) -> dict:
    s = universe.get_scan(name)
    if not s:
        raise ValueError(f"no saved scan '{name}'")
    spec = s["spec"]
    res = scan(spec.get("conditions", []),
               name=spec.get("universe", universe.DEFAULT_UNIVERSE),
               days=int(spec.get("days", DEFAULT_LOOKBACK)),
               limit=int(spec.get("limit", MAX_HITS)))
    universe.record_run(name, len(res["hits"]))
    res["saved_as"] = name
    return res


# ---------------------------------------------------------------- render
def as_text(res: dict, limit: int = 12) -> str:
    conds = ", ".join(c if isinstance(c, str) else c.get("key", "?")
                      for c in res["conditions"])
    head = (f"Scan: {conds}" +
            (f" · {res['universe']}" if res.get("universe") else ""))
    lines = [head,
             f"{res['total_hits']} hit(s) from {res['scanned']} symbols"]
    if not res["hits"]:
        lines.append("")
        lines.append("Nothing matched." +
                     (f" ({len(res['skipped'])} skipped for thin history — "
                      f"run the backfill)" if res["skipped"] else ""))
        return "\n".join(lines)
    lines.append("")
    for h in res["hits"][:limit]:
        bits = [f"{h['symbol']:<12} {h['close']:>9,.2f}"]
        if h["change_pct"] is not None:
            bits.append(f"{h['change_pct']:+.1f}%")
        for k, d in h["detail"].items():
            if k == "volume_surge":
                bits.append(f"{d['times']}× vol")
            elif k == "delivery_spike":
                bits.append(f"{d['times']}× deliv ({d['deliv_pct']:.0f}%)")
            elif k == "52w_high":
                bits.append(f"52wH +{d['above_pct']}%")
            elif k == "52w_low":
                bits.append(f"52wL {d['below_pct']}%")
            elif k == "bb_squeeze":
                bits.append(f"squeeze {d['side']}")
            elif k == "donchian_break":
                bits.append(f"{d['window']}d break")
            elif k == "strategy_long":
                bits.append(d["name"])
            elif k == "golden_cross":
                bits.append("golden cross")
            elif k == "death_cross":
                bits.append("death cross")
        lines.append("  " + "  ".join(bits))
    if res["truncated"] or len(res["hits"]) > limit:
        lines.append(f"  + {res['total_hits'] - min(limit, len(res['hits']))} more")
    if res["skipped"]:
        lines.append(f"  ⚠ {len(res['skipped'])} symbols skipped — too little "
                     f"stored history. Run the universe backfill.")
    return "\n".join(lines)
