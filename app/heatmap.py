"""Parameter heatmap — sweep two parameters, read the shape of the result.

A single backtest tells you what one parameter pair did. It cannot tell you
whether that pair was skill or luck. Sweeping a grid can, because the *shape*
of the surface is the answer:

  * A **plateau** — a broad region where nearby settings all work — means the
    strategy is capturing something real. You could have picked any of those
    settings and done roughly as well, which is exactly what you want, because
    you are going to be wrong about the exact number.
  * A **sharp peak** — one bright cell surrounded by dull ones — means the
    result depends on hitting a specific value. That is the signature of
    curve-fitting. The peak is real in the sample and will not survive out of
    sample, because next year's data won't put its peak in the same place.

This module quantifies that rather than leaving it to the eye. `analyse()`
compares the best cell to its immediate neighbours and to the grid as a whole,
and `verdict()` says which of the two you're looking at.

Candles are fetched once and reused across every cell — a 7×7 sweep otherwise
means 49 identical fetches against a rate-limited API — via
`backtest.evaluate_on`, which is the same code path `backtest.run` uses, so a
cell and a click-through single run agree.
"""
from __future__ import annotations

from statistics import median

from . import backtest, candles, instruments, kite_auth

# Metrics worth optimising over. Each maps to a key in backtest's stats dict.
# Sharpe first because it prices risk; total return alone rewards whichever
# setting happened to sit through the biggest move.
METRICS = {
    "sharpe": {"label": "Sharpe", "key": "sharpe", "higher": True},
    "profit_factor": {"label": "Profit factor", "key": "profit_factor", "higher": True},
    "expectancy_pct": {"label": "Expectancy %/trade", "key": "expectancy_pct", "higher": True},
    "total_return_pct": {"label": "Total return %", "key": "total_return_pct", "higher": True},
    "cagr_pct": {"label": "CAGR %", "key": "cagr_pct", "higher": True},
    "max_drawdown_pct": {"label": "Max drawdown %", "key": "max_drawdown_pct", "higher": True},
    "win_rate_pct": {"label": "Win rate %", "key": "win_rate_pct", "higher": True},
}

MAX_CELLS = 400          # a 20x20 sweep; beyond this it's a batch job, not a page
PLATEAU_TOLERANCE = 0.80  # a cell within 80% of the best counts as "as good as"
MIN_TRADES = 10           # below this a cell's stats are noise


def _values(spec) -> list:
    """Turn a sweep spec into an explicit list of parameter values.

    Two forms, deliberately distinguishable:

      * a **list** — always the literal values. `[5, 10, 15]` is three cells.
      * a **string** `"start:stop:step"` — an inclusive range. `"5:15:5"` is
        also three cells, but says so differently.

    An earlier version accepted `[5, 10, 15]` as either, resolving any
    three-number list as a range. That silently turned three requested values
    into one cell — the worst kind of bug, because the grid still renders and
    nothing looks wrong.
    """
    if isinstance(spec, str):
        parts = spec.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"bad range '{spec}' — use 'start:stop:step', e.g. '5:30:5'")
        try:
            start, stop, step = (float(p) for p in parts)
        except ValueError:
            raise ValueError(f"bad range '{spec}' — values must be numbers")
        if step <= 0:
            raise ValueError("step must be positive")
        if stop < start:
            raise ValueError(f"bad range '{spec}' — stop is below start")
        whole = all(float(p).is_integer() for p in parts)
        out, v = [], start
        while v <= stop + 1e-9:
            out.append(int(v) if whole else round(v, 6))
            v += step
        return out
    if isinstance(spec, dict):
        return _values(f"{spec['start']}:{spec['stop']}:{spec['step']}")
    if isinstance(spec, (list, tuple)):
        if not spec:
            raise ValueError("empty value list")
        return list(spec)
    raise ValueError("parameter values must be a list or a 'start:stop:step' string")


def sweep(symbol: str, strategy: str, x_param: str, x_values,
          y_param: str | None = None, y_values=None,
          metric: str = "sharpe", interval: str = "day", days: int = 730,
          cost_bps: float = 10.0, capital: float = 100_000.0,
          base_params: dict | None = None, risk: dict | None = None) -> dict:
    """Run `strategy` over every combination of two parameters.

    `y_param` may be omitted for a 1-D sweep, which renders as a single row.
    """
    if strategy not in backtest.STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric}; try {', '.join(METRICS)}")

    spec = backtest.STRATEGIES[strategy]
    known = set(spec["params"])
    for name in (x_param, y_param):
        if name and name not in known:
            raise ValueError(
                f"{strategy} has no parameter '{name}' — it takes "
                f"{', '.join(sorted(known)) or 'none'}")

    xs = _values(x_values)
    ys = _values(y_values) if y_param else [None]
    if not xs or not ys:
        raise ValueError("no parameter values to sweep")
    if len(xs) * len(ys) > MAX_CELLS:
        raise ValueError(
            f"{len(xs)}×{len(ys)} = {len(xs) * len(ys)} cells exceeds the "
            f"{MAX_CELLS} limit — narrow the ranges or widen the step")

    inst = instruments.lookup(symbol)
    if not inst:
        raise ValueError(f"unknown symbol {symbol}")

    # ONE fetch for the whole grid.
    kite = kite_auth.get_kite()
    candles.ensure(kite, inst["instrument_token"], interval, days)
    c = candles.get(inst["instrument_token"], interval, days)
    if len(c) < 30:
        raise ValueError(f"not enough candles ({len(c)}) to sweep")

    key = METRICS[metric]["key"]
    grid, errors = [], []
    for y in ys:
        row = []
        for x in xs:
            p = {**(base_params or {}), x_param: x}
            if y_param:
                p[y_param] = y
            try:
                ev = backtest.evaluate_on(c, strategy, interval=interval,
                                          cost_bps=cost_bps, capital=capital,
                                          params=p, risk=risk)
                st = ev["stats"]
                row.append({"x": x, "y": y, "value": st.get(key),
                            "trades": st.get("trades"),
                            "thin": (st.get("trades") or 0) < MIN_TRADES})
            except (Exception, SystemExit) as e:
                errors.append({"x": x, "y": y, "error": str(e)})
                row.append({"x": x, "y": y, "value": None, "trades": None,
                            "thin": True})
        grid.append(row)

    a = analyse(grid, metric)
    return {
        "symbol": inst["tradingsymbol"], "name": inst["name"],
        "strategy": strategy, "strategy_name": spec["name"],
        "metric": metric, "metric_label": METRICS[metric]["label"],
        "x_param": x_param, "x_values": xs,
        "y_param": y_param, "y_values": ys if y_param else [],
        "interval": interval, "days": days, "bars": len(c),
        "cost_bps": cost_bps, "base_params": base_params or {},
        "risk": risk or {},
        "grid": grid, "errors": errors,
        "analysis": a, "verdict": verdict(a),
        "cells": len(xs) * len(ys),
    }


def _neighbours(grid, r, ci) -> list[float]:
    out = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, ci + dc
            if 0 <= rr < len(grid) and 0 <= cc < len(grid[rr]):
                v = grid[rr][cc]["value"]
                if v is not None:
                    out.append(v)
    return out


def analyse(grid: list[list[dict]], metric: str) -> dict:
    """Peak or plateau? Everything here is about the shape, not the maximum.

    `neighbour_ratio` is the number to read: the average of the cells
    immediately around the best one, divided by the best. Near 1 means the
    surface is flat where it matters — pick any of those settings and you'd
    have done about as well. Near 0 means the best cell is a spike, and the
    only reason it's the best is that this particular sample rewarded it.
    """
    cells = [c for row in grid for c in row if c["value"] is not None]
    if not cells:
        return {"scored": 0, "best": None, "best_at": None,
                "neighbour_ratio": None, "plateau_cells": 0,
                "plateau_pct": None, "positive_pct": None,
                "median": None, "spread": None, "thin_pct": None}

    vals = [c["value"] for c in cells]
    best_cell = max(cells, key=lambda c: c["value"])
    best = best_cell["value"]

    # locate the best cell to inspect its surroundings
    br = bc = 0
    for r, row in enumerate(grid):
        for ci, cell in enumerate(row):
            if cell is best_cell:
                br, bc = r, ci

    nb = _neighbours(grid, br, bc)
    nb_ratio = None
    if nb and best not in (0, None):
        nb_ratio = round((sum(nb) / len(nb)) / best, 3) if best > 0 else None

    thresh = best * PLATEAU_TOLERANCE if best > 0 else None
    plateau = sum(1 for v in vals if thresh is not None and v >= thresh)

    return {
        "scored": len(cells),
        "best": round(best, 4),
        "best_at": {"x": best_cell["x"], "y": best_cell["y"]},
        "best_trades": best_cell.get("trades"),
        "neighbour_ratio": nb_ratio,
        "neighbours_scored": len(nb),
        "plateau_cells": plateau,
        "plateau_pct": round(plateau / len(cells) * 100, 1),
        "positive_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        "median": round(median(vals), 4),
        "spread": round(max(vals) - min(vals), 4),
        "thin_pct": round(sum(1 for c in cells if c["thin"]) / len(cells) * 100, 1),
    }


def verdict(a: dict) -> dict:
    """Plain words: is this shape trustworthy?"""
    if not a.get("scored"):
        return {"read": "no result",
                "detail": "No parameter combination produced a usable result."}

    notes = []
    if a.get("thin_pct", 0) >= 40:
        notes.append(f"{a['thin_pct']:g}% of cells traded fewer than "
                     f"{MIN_TRADES} times — those numbers are noise")
    if a.get("best_trades") is not None and a["best_trades"] < MIN_TRADES:
        notes.append(f"the best cell took only {a['best_trades']} trades; "
                     f"it topped the grid on very little evidence")
    if a.get("neighbours_scored", 0) < 3:
        notes.append("the best cell sits on an edge of the grid — widen the "
                     "range so you can see whether it's a peak or a slope")

    nr = a.get("neighbour_ratio")
    if a["best"] <= 0:
        read = "nothing works"
        detail = (f"the best setting still scores {a['best']:g} — no parameter "
                  f"choice makes this strategy work on this symbol")
    elif nr is None:
        read = "unclear"
        detail = ("couldn't compare the best cell to its surroundings — widen "
                  "the sweep")
    elif nr >= 0.7 and a["plateau_pct"] >= 25:
        read = "plateau"
        detail = (f"neighbours average {nr:.0%} of the best and {a['plateau_pct']:g}% "
                  f"of the grid is within {int(PLATEAU_TOLERANCE*100)}% of it — a "
                  f"broad region works, so the exact value isn't load-bearing")
    elif nr >= 0.4:
        read = "ridge"
        detail = (f"neighbours average {nr:.0%} of the best — it degrades away "
                  f"from the peak but not off a cliff. Prefer a setting in the "
                  f"middle of the good region over the peak itself")
    else:
        read = "sharp peak"
        detail = (f"neighbours average only {nr:.0%} of the best — the result "
                  f"depends on hitting this exact value, which is what "
                  f"curve-fitting looks like. Expect it not to hold")

    return {"read": read, "detail": detail, "notes": notes}


CAVEAT = (
    "Pick from the middle of a broad region, not from the brightest cell. The "
    "best cell in any grid is partly luck; a setting whose neighbours also "
    "work is one you can still be roughly right about next year. If the good "
    "region is a single cell, the honest conclusion is that this strategy "
    "doesn't have an edge here — not that you found the magic number."
)


def as_text(res: dict) -> str:
    """A rough ASCII grid, for the phone and the terminal."""
    a, v = res["analysis"], res["verdict"]
    lines = [f"{res['strategy_name']} on {res['symbol']} — {res['metric_label']}",
             f"{v['read'].upper()}: {v['detail']}"]
    if a["scored"]:
        at = a["best_at"]
        where = f"{res['x_param']}={at['x']}"
        if res["y_param"]:
            where += f", {res['y_param']}={at['y']}"
        lines.append(f"best {a['best']:g} at {where} · median {a['median']:g} "
                     f"· {a['positive_pct']:g}% positive")
    lines.append("")

    vals = [c["value"] for row in res["grid"] for c in row if c["value"] is not None]
    if vals:
        lo, hi = min(vals), max(vals)
        # No space in the ramp: a blank cell must mean "no result", not "low
        # result". Reading an empty square as a gap when it's actually the
        # worst score inverts the whole picture.
        ramp = ".:-=+*#@"
        span = (hi - lo) or 1
        lines.append("      " + "".join(f"{str(x)[:4]:>4}" for x in res["x_values"]))
        for r, row in enumerate(res["grid"]):
            label = (str(res["y_values"][r]) if res["y_param"] else "")[:5]
            cells = ""
            for cell in row:
                if cell["value"] is None:
                    cells += "   ·"          # no result
                else:
                    i = int((cell["value"] - lo) / span * (len(ramp) - 1))
                    cells += "   " + ramp[max(0, min(len(ramp) - 1, i))]
            lines.append(f"{label:>5} {cells}")
        lines.append(f"      low {lo:g}  ‹{ramp}›  high {hi:g}   · = no result")
    for n in v.get("notes", []):
        lines.append(f"  ⚠ {n}")
    return "\n".join(lines)
