"""Batch backtest — one strategy across many symbols.

Answers "where does this edge actually live?" — and, more usefully, whether it
lives anywhere at all.

A ranked table is a selection-bias machine. Run one strategy over sixteen
symbols, sort by expectancy, and the top row will look excellent whether or not
the strategy works: even coin flips produce a best-of-sixteen. Trading that top
symbol because it topped the table is precisely how curve-fitting happens, and
the table itself does nothing to stop you.

So the ranking is here, but it is not the headline. The headline is **breadth**:

  * the MEDIAN symbol's result, not the best one — half the sample did better,
    half worse, and no single lucky name can move it;
  * the share of symbols that were profitable at all;
  * how far the best result sits above the median, because a large gap means
    the top row is an outlier rather than a representative.

An edge that works shows up broadly. If a strategy is profitable on three names
out of sixteen, you have not found an edge — you have found three symbols that
happened to trend during the window you chose. `verdict()` says so in words.

None of this replaces walk-forward testing (roadmap 3.3), which is the real
answer to selection bias. It just refuses to pretend the ranking is one.
"""
from __future__ import annotations

from statistics import median

from . import backtest, watchlist

# Below this many symbols, breadth statistics are noise dressed as insight.
MIN_FOR_BREADTH = 5
# A top result this many times the median is an outlier, not a representative.
OUTLIER_RATIO = 3.0


def _row(symbol: str, res: dict) -> dict:
    """Flatten one backtest into a table row. Trade stats are already net of
    costs and exclude the still-open position — see backtest._trade_stats."""
    s = res["stats"]
    return {
        "symbol": symbol,
        "trades": s.get("trades"),
        "open_trades": s.get("open_trades"),
        "expectancy_pct": s.get("expectancy_pct"),
        "win_rate_pct": s.get("win_rate_pct"),
        "profit_factor": s.get("profit_factor"),
        "total_return_pct": s.get("total_return_pct"),
        "cagr_pct": s.get("cagr_pct"),
        "max_drawdown_pct": s.get("max_drawdown_pct"),
        "sharpe": s.get("sharpe"),
        "exposure_pct": s.get("exposure_pct"),
        "vs_buyhold_pct": s.get("vs_buyhold_pct"),
        "bars": res.get("bars"),
    }


def run(strategy: str, symbols: list[str] | None = None,
        interval: str = "day", days: int = 730, cost_bps: float = 10.0,
        capital: float = 100_000.0, params: dict | None = None,
        risk: dict | None = None, sort: str = "expectancy_pct") -> dict:
    """Run `strategy` over `symbols` (default: the watchlist).

    One symbol failing — no candles, unknown ticker, too little history —
    never stops the batch. Failures are listed rather than dropped, because a
    table showing 9 of 16 symbols without saying so overstates its own breadth.
    """
    if strategy not in backtest.STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")

    syms = ([s.strip().upper() for s in symbols if s and s.strip()]
            if symbols else [w["tradingsymbol"] for w in watchlist.items()])
    if not syms:
        raise ValueError("no symbols — pass a list or add some to the watchlist")

    rows, errors, pooled = [], [], []
    for sym in syms:
        try:
            res = backtest.run(sym, strategy, interval=interval, days=days,
                               cost_bps=cost_bps, capital=capital,
                               params=params, risk=risk)
            rows.append(_row(sym, res))
            pooled += [t for t in res.get("trades", []) if t.get("regime")]
        except (Exception, SystemExit) as e:
            # SystemExit: backtest.run reaches for a Kite session, and
            # config.require raises it when there isn't one.
            errors.append({"symbol": sym, "error": str(e)})

    rows = _sort(rows, sort)
    agg = aggregate(rows)
    return {
        "strategy": strategy,
        "name": backtest.STRATEGIES[strategy]["name"],
        "params": {**backtest.STRATEGIES[strategy]["params"], **(params or {})},
        "interval": interval, "days": days, "cost_bps": cost_bps,
        "risk": risk or {},
        "requested": len(syms),
        "rows": rows,
        "errors": errors,
        "aggregate": agg,
        "verdict": verdict(agg, len(rows)),
        "sort": sort,
        "regimes": pooled_regimes(pooled),
    }


def pooled_regimes(trades: list[dict]) -> dict | None:
    """Every symbol's trades pooled, then split by the regime at entry.

    Pooled rather than averaged per symbol: a regime with three trades on one
    name and none elsewhere shouldn't get a vote equal to one with forty.
    Trades arrive already labelled by backtest.regime_split.
    """
    from . import regime as rg
    if not trades:
        return None
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(t["regime"], []).append(t)
    closed = sum(1 for t in trades if not t.get("open")) or 1
    by = {}
    for lab in rg.LABELS + ("unknown",):
        if lab not in groups:
            continue
        st = backtest._trade_stats(groups[lab])
        st["share_pct"] = round(sum(1 for t in groups[lab] if not t.get("open"))
                                / closed * 100, 1)
        by[lab] = st
    return {"by": by, "order": list(by), "verdict": rg.verdict(by)}


SORTS = {
    "expectancy_pct": lambda r: r.get("expectancy_pct"),
    "total_return_pct": lambda r: r.get("total_return_pct"),
    "profit_factor": lambda r: r.get("profit_factor"),
    "sharpe": lambda r: r.get("sharpe"),
    "win_rate_pct": lambda r: r.get("win_rate_pct"),
    "max_drawdown_pct": lambda r: -(r.get("max_drawdown_pct") or 0),
    "trades": lambda r: r.get("trades"),
    "symbol": lambda r: r.get("symbol"),
}


def _sort(rows: list[dict], key: str) -> list[dict]:
    fn = SORTS.get(key, SORTS["expectancy_pct"])
    if key == "symbol":
        return sorted(rows, key=lambda r: r["symbol"])
    # None sorts last regardless of direction — a symbol with no completed
    # trades hasn't earned a place at either end of the table.
    return sorted(rows, key=lambda r: (fn(r) is None, -(fn(r) or 0)))


def aggregate(rows: list[dict]) -> dict:
    """Breadth, not the best row.

    `median_expectancy_pct` is the number to read first. The mean would let one
    runaway symbol carry the whole sample, which is the exact illusion this
    module exists to prevent.
    """
    scored = [r for r in rows if r.get("expectancy_pct") is not None]
    if not scored:
        return {"symbols": len(rows), "scored": 0, "median_expectancy_pct": None,
                "best_expectancy_pct": None, "worst_expectancy_pct": None,
                "profitable": 0, "profitable_pct": None, "total_trades": 0,
                "median_trades": None, "best_symbol": None,
                "outlier_ratio": None, "lone_winner": False,
                "median_max_dd_pct": None}

    exp = [r["expectancy_pct"] for r in scored]
    med = median(exp)
    best = max(exp)
    profitable = sum(1 for v in exp if v > 0)
    dds = [r["max_drawdown_pct"] for r in scored
           if r.get("max_drawdown_pct") is not None]

    return {
        "symbols": len(rows),
        "scored": len(scored),
        "median_expectancy_pct": round(med, 3),
        "mean_expectancy_pct": round(sum(exp) / len(exp), 3),
        "best_expectancy_pct": round(best, 3),
        "worst_expectancy_pct": round(min(exp), 3),
        "best_symbol": max(scored, key=lambda r: r["expectancy_pct"])["symbol"],
        "profitable": profitable,
        "profitable_pct": round(profitable / len(scored) * 100, 1),
        "total_trades": sum(r["trades"] or 0 for r in scored),
        "median_trades": median([r["trades"] or 0 for r in scored]),
        "median_max_dd_pct": round(median(dds), 2) if dds else None,
        # How far the top row sits above the middle. Large = the winner is an
        # outlier, and ranking by it is picking noise.
        "outlier_ratio": (round(best / med, 1)
                          if med and med > 0 and best > 0 else None),
        # The starker case: the best symbol is profitable while the median
        # isn't. A ratio can't express that (dividing by a negative is
        # meaningless), and it's the situation where the warning matters most
        # — the table's top row is the only thing that worked.
        "lone_winner": bool(best > 0 >= med),
    }


def verdict(agg: dict, n: int) -> dict:
    """Plain words about what the table does and doesn't show."""
    if not agg.get("scored"):
        return {"read": "no result",
                "detail": "No symbol produced a completed trade in this window."}

    med = agg["median_expectancy_pct"]
    pct = agg["profitable_pct"]
    notes = []

    if n < MIN_FOR_BREADTH:
        notes.append(f"only {n} symbols — too few to read breadth into")
    if agg["median_trades"] is not None and agg["median_trades"] < 20:
        notes.append(f"median of {agg['median_trades']:.0f} trades per symbol; "
                     f"expectancy over that few is barely a number")
    if agg.get("lone_winner"):
        notes.append(f"{agg['best_symbol']} is the only profitable name while "
                     f"the median isn't — the top row is the exception, and "
                     f"trading it because it topped the table is the whole trap")
    elif agg.get("outlier_ratio") and agg["outlier_ratio"] >= OUTLIER_RATIO:
        notes.append(f"{agg['best_symbol']} is {agg['outlier_ratio']:g}× the "
                     f"median — the top row is an outlier, not a preview")

    if med > 0 and pct >= 60:
        read = "broad"
        detail = (f"positive on {pct:g}% of symbols, median "
                  f"{med:+.2f}%/trade — the edge shows up across the sample, "
                  f"which is what an edge looks like")
    elif med > 0:
        read = "narrow"
        detail = (f"median {med:+.2f}%/trade but only {pct:g}% of symbols "
                  f"profitable — this works in places rather than generally")
    elif pct >= 40:
        read = "coin-flip"
        detail = (f"{pct:g}% profitable with a median of {med:+.2f}%/trade — "
                  f"no edge visible; the winners are paying for the losers")
    else:
        read = "negative"
        detail = (f"profitable on {pct:g}% of symbols, median "
                  f"{med:+.2f}%/trade — this loses money across the sample")

    return {"read": read, "detail": detail, "notes": notes}


CAVEAT = (
    "Ranking symbols by result is how curve-fitting starts: the best of N "
    "looks good even when N is noise. Read the median and the profitable "
    "share first — they can't be moved by one lucky name — and treat the top "
    "row as a place to look, not a conclusion. Walk-forward testing is the "
    "real answer to this."
)


def as_text(res: dict, limit: int = 10) -> str:
    """Phone-sized. Leads with breadth, ranks second."""
    a = res["aggregate"]
    v = res["verdict"]
    lines = [f"{res['name']} — {a['scored']}/{res['requested']} symbols, "
             f"{res['days']}d"]
    lines.append(f"{v['read'].upper()}: {v['detail']}")
    if a["scored"]:
        lines.append(f"median {a['median_expectancy_pct']:+.2f}%/trade · "
                     f"{a['profitable']}/{a['scored']} profitable · "
                     f"median DD {a['median_max_dd_pct']}%")
    lines.append("")
    for r in res["rows"][:limit]:
        e = r["expectancy_pct"]
        lines.append(
            f"  {r['symbol']:<12} {('—' if e is None else f'{e:+.2f}%'):>8}"
            f"  {r['trades'] or 0:>3}t"
            f"  PF {r['profit_factor'] if r['profit_factor'] is not None else '—'}"
            f"  DD {r['max_drawdown_pct']}%")
    if len(res["rows"]) > limit:
        lines.append(f"  + {len(res['rows']) - limit} more")
    for n in v.get("notes", []):
        lines.append(f"  ⚠ {n}")
    rg = res.get("regimes")
    if rg:
        lines.append("")
        lines.append("By market regime (all trades pooled):")
        for lab in rg["order"]:
            st = rg["by"][lab]
            e = st.get("expectancy_pct")
            lines.append(f"  {lab:<11} {st['trades']:>4}t  "
                         f"{('—' if e is None else f'{e:+.2f}%'):>8}/trade  "
                         f"win {st.get('win_rate_pct') if st.get('win_rate_pct') is not None else '—'}%")
        if rg.get("verdict"):
            lines.append(f"  → {rg['verdict']}")
    if res["errors"]:
        lines.append(f"  ! {len(res['errors'])} failed: "
                     + ", ".join(e["symbol"] for e in res["errors"][:5]))
    return "\n".join(lines)
