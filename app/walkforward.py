"""Walk-forward validation — optimise on one window, test on the next.

Everything else in this app can only *warn* about selection bias. The batch
backtest tells you the top row of a ranked table is probably luck; the heatmap
tells you a lone bright cell is probably a curve-fit. Neither can tell you what
the strategy would actually have earned, because both optimise and measure on
the same data.

Walk-forward can, because it never does. Each fold optimises parameters on an
in-sample window and then applies them, unchanged, to a window the optimiser
never saw. Stitch those out-of-sample segments together and you have an equity
curve produced entirely by decisions made without hindsight. That curve is the
honest one, and it is usually much worse than the optimised backtest — which is
the finding, not a failure of the method.

Two numbers carry most of the value:

  * **Efficiency** = out-of-sample result ÷ in-sample result. Near 1 means the
    optimiser found something that persisted. Near 0 — or negative — means it
    fitted noise, and the impressive single backtest you started from was an
    artefact of choosing parameters with the answers in front of you.
  * **Parameter stability**. If the optimiser picks fast=5, then fast=30, then
    fast=12 across three folds, there is no optimum to find. A strategy whose
    best setting jumps around is one whose best setting is noise, regardless of
    how good any individual fold looks.

Warm-up is the detail that decides whether this is honest at all. A 200-day SMA
evaluated on a 60-day out-of-sample slice is undefined for every bar, so each
fold computes indicators over in-sample PLUS out-of-sample and then measures
only the out-of-sample segment. The optimiser still never sees out-of-sample
outcomes — it only ever ranks parameters on in-sample bars — but the indicators
that drive it are warm, exactly as they would be in live trading.
"""
from __future__ import annotations

from itertools import product
from statistics import median, pstdev

from . import backtest, candles, instruments, kite_auth
from .heatmap import METRICS, _values

MIN_IS_BARS = 120           # below this an "optimisation" is fitting a handful of trades
MIN_OOS_BARS = 20
MAX_COMBOS = 200            # per fold; folds multiply this


def _grid(param_grid: dict) -> list[dict]:
    """Expand {param: values} into every combination.

    Values use the same forms as the heatmap: a list is literal, a
    'start:stop:step' string is a range.
    """
    if not param_grid:
        return [{}]
    names = list(param_grid)
    axes = [_values(param_grid[n]) for n in names]
    combos = [dict(zip(names, vals)) for vals in product(*axes)]
    if len(combos) > MAX_COMBOS:
        raise ValueError(
            f"{len(combos)} parameter combinations per fold exceeds the "
            f"{MAX_COMBOS} limit — narrow the grid")
    return combos


def _score(stats: dict, metric: str) -> float | None:
    return stats.get(METRICS[metric]["key"])


def _folds(n_bars: int, is_bars: int, oos_bars: int, mode: str) -> list[tuple]:
    """-> [(fold_start, is_end, oos_end)] as indices into the candle series.

    `rolling` keeps the in-sample window a fixed length, so old regimes drop
    out. `anchored` grows it from the start, so every fold optimises on all
    history to date. Rolling adapts faster; anchored has more data. Neither is
    right in general, which is why both are here.
    """
    out = []
    is_end = is_bars
    while is_end + oos_bars <= n_bars:
        start = 0 if mode == "anchored" else max(0, is_end - is_bars)
        out.append((start, is_end, min(is_end + oos_bars, n_bars)))
        is_end += oos_bars
    return out


def run(symbol: str, strategy: str, param_grid: dict | None = None,
        is_bars: int = 250, oos_bars: int = 60, mode: str = "rolling",
        metric: str = "sharpe", interval: str = "day", days: int = 1460,
        cost_bps: float = 10.0, capital: float = 100_000.0,
        risk: dict | None = None) -> dict:
    """Walk `strategy` forward over `symbol`, optimising `param_grid` each fold.

    With no `param_grid` this becomes plain out-of-sample testing: the same
    default parameters applied to every window, which still answers "did this
    work on data I didn't look at" even though there's nothing to optimise.
    """
    if strategy not in backtest.STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric}; try {', '.join(METRICS)}")
    if mode not in ("rolling", "anchored"):
        raise ValueError("mode must be 'rolling' or 'anchored'")
    if is_bars < MIN_IS_BARS:
        raise ValueError(
            f"in-sample window of {is_bars} bars is too short to optimise on "
            f"(minimum {MIN_IS_BARS}) — you'd be fitting a handful of trades")
    if oos_bars < MIN_OOS_BARS:
        raise ValueError(f"out-of-sample window must be at least {MIN_OOS_BARS} bars")

    spec = backtest.STRATEGIES[strategy]
    known = set(spec["params"])
    for name in (param_grid or {}):
        if name not in known:
            raise ValueError(
                f"{strategy} has no parameter '{name}' — it takes "
                f"{', '.join(sorted(known)) or 'none'}")

    combos = _grid(param_grid or {})

    inst = instruments.lookup(symbol)
    if not inst:
        raise ValueError(f"unknown symbol {symbol}")
    kite = kite_auth.get_kite()
    candles.ensure(kite, inst["instrument_token"], interval, days)
    c = candles.get(inst["instrument_token"], interval, days)

    splits = _folds(len(c), is_bars, oos_bars, mode)
    if not splits:
        raise ValueError(
            f"{len(c)} bars isn't enough for {is_bars} in-sample + "
            f"{oos_bars} out-of-sample — fetch more history or shrink the "
            f"windows")

    folds, oos_rets, chosen = [], [], []
    for start, is_end, oos_end in splits:
        is_slice = c[start:is_end]

        # Optimise on in-sample only.
        best, best_score, best_stats = None, None, None
        for p in combos:
            try:
                ev = backtest.evaluate_on(is_slice, strategy, interval=interval,
                                          cost_bps=cost_bps, capital=capital,
                                          params=p, risk=risk)
            except (Exception, SystemExit):
                continue
            sc = _score(ev["stats"], metric)
            if sc is None:
                continue
            if best_score is None or sc > best_score:
                best, best_score, best_stats = p, sc, ev["stats"]
        if best is None:
            folds.append({"start": start, "is_end": is_end, "oos_end": oos_end,
                          "params": None, "is_score": None, "oos_score": None,
                          "error": "no parameter set produced a result in-sample"})
            continue

        # Apply it, unchanged, to bars the optimiser never saw. Indicators are
        # computed over IS+OOS so they're warm; only the OOS segment is scored.
        full = c[start:oos_end]
        try:
            ev = backtest.evaluate_on(full, strategy, interval=interval,
                                      cost_bps=cost_bps, capital=capital,
                                      params=best, risk=risk)
        except (Exception, SystemExit) as e:
            folds.append({"start": start, "is_end": is_end, "oos_end": oos_end,
                          "params": best, "is_score": round(best_score, 4),
                          "oos_score": None, "error": str(e)})
            continue

        cut = is_end - start                      # first OOS bar within `full`
        seg_rets = ev["sim"]["rets"][cut:] if len(ev["sim"]["rets"]) > cut else []
        oos_stats = _segment_stats(seg_rets, interval)
        oos_rets.extend(seg_rets)
        chosen.append(best)

        # A fold where the strategy simply never held a position out-of-sample
        # produces all-zero returns and an undefined Sharpe. That's a real
        # outcome, not an error — but a bare blank in the table looks like a
        # bug, so say which it is.
        active = sum(1 for r in seg_rets if r != 0)
        oos_score = oos_stats.get(METRICS[metric]["key"])
        note = None
        if oos_score is None and not active:
            note = "flat out-of-sample — the strategy never took a position"

        folds.append({
            "start": start, "is_end": is_end, "oos_end": oos_end,
            "is_from": c[start]["time"], "is_to": c[is_end - 1]["time"],
            "oos_from": c[is_end]["time"], "oos_to": c[oos_end - 1]["time"],
            "is_bars": is_end - start, "oos_bars": oos_end - is_end,
            "params": best,
            "is_score": round(best_score, 4),
            "is_trades": (best_stats or {}).get("trades"),
            "oos_score": oos_score,
            "oos_active_bars": active,
            "oos_return_pct": oos_stats.get("total_return_pct"),
            "error": None,
            "note": note,
        })

    agg = aggregate(folds, oos_rets, chosen, metric, interval)
    return {
        "symbol": inst["tradingsymbol"], "name": inst["name"],
        "strategy": strategy, "strategy_name": spec["name"],
        "metric": metric, "metric_label": METRICS[metric]["label"],
        "mode": mode, "is_bars": is_bars, "oos_bars": oos_bars,
        "interval": interval, "days": days, "bars": len(c),
        "cost_bps": cost_bps, "risk": risk or {},
        "grid": param_grid or {}, "combos": len(combos),
        "folds": folds, "aggregate": agg,
        "verdict": verdict(agg, len(folds)),
        "oos_equity": _curve(oos_rets, capital),
    }


def _segment_stats(rets: list[float], interval: str) -> dict:
    """Metrics for one out-of-sample segment, from its per-bar returns."""
    if not rets:
        return {}
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    return backtest._metrics(eq, rets, backtest._BPY[interval])


def _curve(rets: list[float], capital: float) -> list[dict]:
    eq, v = [], capital
    for i, r in enumerate(rets):
        v *= (1 + r)
        eq.append({"i": i, "value": round(v, 2)})
    return eq


def _stability(chosen: list[dict]) -> dict:
    """How much the optimiser's answer moved between folds.

    Reported per parameter as the coefficient of variation — spread relative to
    size, so a fast MA moving 5→30 and a slow MA moving 100→125 are comparable.
    A strategy whose best setting jumps around doesn't have one.
    """
    if not chosen:
        return {"per_param": {}, "score": None, "read": "no folds"}
    per = {}
    for name in chosen[0]:
        vals = [c[name] for c in chosen if isinstance(c.get(name), (int, float))]
        if len(vals) < 2:
            continue
        m = sum(vals) / len(vals)
        per[name] = {
            "values": vals,
            "distinct": len(set(vals)),
            "cv": round(pstdev(vals) / abs(m), 3) if m else None,
        }
    cvs = [v["cv"] for v in per.values() if v["cv"] is not None]
    score = round(1 - min(1.0, sum(cvs) / len(cvs)), 3) if cvs else None
    read = ("stable" if score is not None and score >= 0.75 else
            "drifting" if score is not None and score >= 0.4 else
            "unstable" if score is not None else "n/a")
    return {"per_param": per, "score": score, "read": read}


def aggregate(folds: list[dict], oos_rets: list[float], chosen: list[dict],
              metric: str, interval: str) -> dict:
    scored = [f for f in folds if f.get("oos_score") is not None]
    is_scores = [f["is_score"] for f in scored if f["is_score"] is not None]
    oos_scores = [f["oos_score"] for f in scored]

    is_mean = sum(is_scores) / len(is_scores) if is_scores else None
    oos_mean = sum(oos_scores) / len(oos_scores) if oos_scores else None
    # Efficiency only means anything when in-sample was positive. Dividing by a
    # negative in-sample score produces a number that looks like a ratio and
    # isn't one.
    eff = (round(oos_mean / is_mean, 3)
           if is_mean and is_mean > 0 and oos_mean is not None else None)

    overall = _segment_stats(oos_rets, interval)
    return {
        "folds": len(folds),
        "scored": len(scored),
        "is_mean": round(is_mean, 4) if is_mean is not None else None,
        "oos_mean": round(oos_mean, 4) if oos_mean is not None else None,
        "oos_median": round(median(oos_scores), 4) if oos_scores else None,
        "efficiency": eff,
        "oos_positive": sum(1 for v in oos_scores if v > 0),
        "oos_positive_pct": (round(sum(1 for v in oos_scores if v > 0)
                                   / len(oos_scores) * 100, 1)
                             if oos_scores else None),
        "oos_total_return_pct": overall.get("total_return_pct"),
        "oos_max_drawdown_pct": overall.get("max_drawdown_pct"),
        "oos_sharpe": overall.get("sharpe"),
        "oos_bars": len(oos_rets),
        "stability": _stability(chosen),
    }


def verdict(agg: dict, n_folds: int) -> dict:
    if not agg.get("scored"):
        return {"read": "no result",
                "detail": "No fold produced an out-of-sample result."}

    notes = []
    if n_folds < 4:
        notes.append(f"only {n_folds} fold(s) — too few to conclude much; "
                     f"lengthen the history or shorten the windows")
    st = agg["stability"]
    if st["read"] == "unstable":
        notes.append("the optimiser picked very different parameters each fold "
                     "— there's no stable optimum here, so the good folds are "
                     "luck rather than a setting you could have chosen")
    elif st["read"] == "drifting":
        notes.append("chosen parameters drift between folds — prefer a value "
                     "near the middle of the range over any single fold's pick")

    eff = agg["efficiency"]
    pos = agg["oos_positive_pct"]

    if agg["oos_mean"] is not None and agg["oos_mean"] <= 0:
        read = "fails out of sample"
        detail = (f"in-sample averaged {agg['is_mean']}, out-of-sample "
                  f"{agg['oos_mean']} — the optimisation didn't survive contact "
                  f"with data it hadn't seen. This is the normal result, and "
                  f"the reason single backtests mislead")
    elif eff is None:
        read = "unclear"
        detail = ("in-sample wasn't positive, so there's no meaningful "
                  "efficiency ratio — read the out-of-sample numbers alone")
    elif eff >= 0.6 and pos >= 60:
        read = "holds up"
        detail = (f"out-of-sample kept {eff:.0%} of the in-sample result and "
                  f"{pos:g}% of folds were positive — this is what an edge that "
                  f"generalises looks like")
    elif eff >= 0.3:
        read = "degrades"
        detail = (f"out-of-sample kept only {eff:.0%} of the in-sample result — "
                  f"there's something there, but a good chunk of the backtest "
                  f"was fitting. Size accordingly")
    else:
        read = "curve-fit"
        detail = (f"out-of-sample kept just {eff:.0%} of the in-sample result — "
                  f"the optimisation was fitting noise, and the impressive "
                  f"backtest was an artefact of choosing parameters with the "
                  f"answers visible")

    return {"read": read, "detail": detail, "notes": notes}


CAVEAT = (
    "The out-of-sample curve is the honest one and it is usually much worse "
    "than the optimised backtest — that gap is the finding, not a fault in the "
    "method. If out-of-sample holds up AND the chosen parameters are stable "
    "across folds, you have something worth trading small. If either fails, "
    "the single backtest that impressed you was measuring your own hindsight."
)


def as_text(res: dict) -> str:
    a, v = res["aggregate"], res["verdict"]
    lines = [
        f"{res['strategy_name']} on {res['symbol']} — walk-forward "
        f"({res['mode']}, {res['is_bars']}/{res['oos_bars']} bars, "
        f"{len(res['folds'])} folds)",
        f"{v['read'].upper()}: {v['detail']}",
    ]
    if a["scored"]:
        lines.append(f"IS {a['is_mean']} → OOS {a['oos_mean']}"
                     + (f" · efficiency {a['efficiency']:.0%}"
                        if a["efficiency"] is not None else "")
                     + f" · {a['oos_positive']}/{a['scored']} folds positive")
        lines.append(f"stitched OOS: {a['oos_total_return_pct']}% return, "
                     f"{a['oos_max_drawdown_pct']}% max DD")
        st = a["stability"]
        if st["score"] is not None:
            lines.append(f"parameter stability: {st['read']} ({st['score']:.2f})")
    lines.append("")
    for i, f in enumerate(res["folds"], 1):
        if f.get("error"):
            lines.append(f"  {i}. — {f['error']}")
            continue
        p = " ".join(f"{k}={v}" for k, v in (f["params"] or {}).items()) or "defaults"
        oos = f["oos_score"] if f["oos_score"] is not None else "—"
        line = f"  {i}. {p:<24} IS {f['is_score']:>7} → OOS {oos:>7}"
        if f.get("note"):
            line += f"   ({f['note']})"
        lines.append(line)
    for n in v.get("notes", []):
        lines.append(f"  ⚠ {n}")
    return "\n".join(lines)
