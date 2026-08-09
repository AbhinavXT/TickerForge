"""Monte Carlo over the trade sequence.

A backtest hands you one number for max drawdown. That number is a single draw
from a distribution: the trades happened in one particular order, and a
different order — the same trades, differently arranged — would have produced
a different worst moment. Sometimes very different. A run whose losers happened
to be spread out looks survivable; the same losers clustered would have taken
you out.

So this reshuffles the trade list a thousand times and reports the spread.
"95% of orderings stayed above −18%" is a sentence you can size a position
against. "Max drawdown was −11%" is not, because you have no idea whether −11%
was typical or the luckiest arrangement available.

Two modes, answering two different questions
--------------------------------------------
**shuffle** — permute the actual trades. Same trades, new order. Answers: *was
my drawdown just lucky sequencing?*

Note what this CANNOT tell you: under compounding, the final equity is the
product of the same per-trade factors, and multiplication is commutative. Every
ordering ends at the identical number. Shuffling therefore says nothing about
return, only about the path taken to get there — and the report says so rather
than printing a return distribution with zero width and letting you think it
means something.

**bootstrap** — resample with replacement. Treats your trades as a sample from
some underlying distribution and asks what a *different* sample would look
like. Answers: *what if the next hundred trades resemble these but aren't
these?* Return varies here, because the draw varies.

Bootstrap is the more honest forecast and the more pessimistic one. Use
shuffle to interrogate the backtest you have; use bootstrap to set expectations
for the one you haven't run yet.

What it assumes, and where that bites
-------------------------------------
Both modes assume trades are independent — that the order carries no
information. If your strategy genuinely streaks (trend systems often do: they
win repeatedly in a trend and lose repeatedly in a chop), shuffling breaks up
those clusters and **understates** real drawdown. The result is a floor on how
bad things get, not a ceiling. Treat a comfortable number as necessary rather
than sufficient.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

# Percentiles worth naming. The low end of the drawdown distribution is the
# interesting end — nobody was ever ruined by a mild one.
PCTS = (5, 25, 50, 75, 95)
DEFAULT_SIMS = 1000
MAX_SIMS = 20_000


class MonteCarloError(ValueError):
    pass


@dataclass(frozen=True)
class Path:
    """One simulated ordering."""
    max_dd: float            # as a negative %, e.g. -18.4
    final_pct: float         # total return %
    longest_dd: int          # trades spent below a previous peak


def _closed(trades: list[dict]) -> list[float]:
    """Net returns of completed trades, as fractions.

    Open trades are excluded for the same reason the backtester excludes them
    from its statistics: they have no exit, so their contribution isn't known
    yet. Including a mark-to-market number here would let an unrealised gain
    flatter every simulated path.
    """
    return [t["net_pct"] / 100.0 for t in trades if not t.get("open")]


def _walk(returns: list[float]) -> Path:
    """Compound one ordering and measure the damage."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    under = 0
    longest = 0
    for r in returns:
        equity *= (1 + r)
        if equity >= peak:
            peak = equity
            under = 0
        else:
            under += 1
            longest = max(longest, under)
            dd = equity / peak - 1
            max_dd = min(max_dd, dd)
    return Path(max_dd=max_dd * 100, final_pct=(equity - 1) * 100,
                longest_dd=longest)


def _percentile(values: list[float], pct: float) -> float:
    """Linear interpolation between order statistics.

    Not `statistics.quantiles`, because that needs n >= 2 and silently changes
    method between versions — and this number ends up in a sentence someone
    sizes a position against.
    """
    if not values:
        raise MonteCarloError("no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _rank_pct(values: list[float], observed: float) -> float:
    """Where `observed` falls in `values`, 0–100.

    For drawdowns (negative), a LOW percentile means the observed run was
    among the worst — so the actual backtest was unlucky. A high percentile
    means it was among the mildest, i.e. the backtest flattered itself.
    """
    if not values:
        raise MonteCarloError("no values")
    below = sum(1 for v in values if v < observed)
    equal = sum(1 for v in values if v == observed)
    return (below + equal / 2) / len(values) * 100


def run(trades: list[dict], sims: int = DEFAULT_SIMS, mode: str = "shuffle",
        seed: int | None = None) -> dict:
    """Resample the trade sequence and describe the spread of outcomes."""
    if mode not in ("shuffle", "bootstrap"):
        raise MonteCarloError("mode must be 'shuffle' or 'bootstrap'")
    if not 1 <= sims <= MAX_SIMS:
        raise MonteCarloError(f"sims must be between 1 and {MAX_SIMS:,}")

    returns = _closed(trades)
    if len(returns) < 5:
        raise MonteCarloError(
            f"only {len(returns)} closed trades — a distribution built from "
            f"that many is noise. Backtest a longer window first.")

    rng = random.Random(seed)
    observed = _walk(returns)

    paths: list[Path] = []
    n = len(returns)
    for _ in range(sims):
        if mode == "shuffle":
            draw = returns[:]
            rng.shuffle(draw)
        else:
            draw = [returns[rng.randrange(n)] for _ in range(n)]
        paths.append(_walk(draw))

    dds = [p.max_dd for p in paths]
    finals = [p.final_pct for p in paths]
    longests = [float(p.longest_dd) for p in paths]

    out = {
        "mode": mode,
        "sims": sims,
        "trades": n,
        "seed": seed,
        "observed": {
            "max_dd_pct": round(observed.max_dd, 2),
            "final_pct": round(observed.final_pct, 2),
            "longest_dd": observed.longest_dd,
        },
        "max_dd_pct": {f"p{p}": round(_percentile(dds, p), 2) for p in PCTS},
        "longest_dd": {f"p{p}": round(_percentile(longests, p), 1)
                       for p in PCTS},
        # The headline. p5 of the drawdown distribution is the level 95% of
        # orderings stayed above.
        "dd_95_floor_pct": round(_percentile(dds, 5), 2),
        "worst_dd_pct": round(min(dds), 2),
        "observed_dd_percentile": round(_rank_pct(dds, observed.max_dd), 1),
        "loss_probability_pct": round(
            sum(1 for f in finals if f <= 0) / sims * 100, 1),
    }

    if mode == "bootstrap":
        out["final_pct"] = {f"p{p}": round(_percentile(finals, p), 2)
                            for p in PCTS}
    else:
        # Deliberately absent, with the reason attached. A return distribution
        # of zero width isn't a finding, it's an artefact of commutativity, and
        # printing it invites people to read meaning into it.
        out["final_pct"] = None
        out["final_note"] = (
            "Shuffling can't change the final return — compounding the same "
            "trades in any order lands on the same number. Use bootstrap for "
            "a return distribution.")

    # Bucketed here, where the raw samples live. Shipping 20 counts instead of
    # 20,000 floats keeps the response small enough to not think about.
    out["dd_histogram"] = histogram(dds)
    if mode == "bootstrap":
        out["final_histogram"] = histogram(finals)

    out["verdict"] = _verdict(out)
    return out


def _verdict(r: dict) -> str:
    """One sentence you can act on."""
    floor = r["dd_95_floor_pct"]
    pct = r["observed_dd_percentile"]
    observed = r["observed"]["max_dd_pct"]

    bits = [f"95% of orderings stayed above {floor:.1f}%."]

    # Whether the backtest's own drawdown was representative is the thing most
    # worth saying, because that's the number the user has been anchoring on.
    if pct >= 75:
        bits.append(
            f"The backtest's {observed:.1f}% was mild by comparison "
            f"({pct:.0f}th percentile) — it got a kind ordering, so plan "
            f"around the floor above, not that figure.")
    elif pct <= 25:
        bits.append(
            f"The backtest's {observed:.1f}% was among the worse orderings "
            f"({pct:.0f}th percentile) — typical sequencing would have been "
            f"gentler.")
    else:
        bits.append(f"The backtest's {observed:.1f}% was about typical "
                    f"({pct:.0f}th percentile).")

    if r["mode"] == "bootstrap" and r["loss_probability_pct"] > 0:
        bits.append(f"{r['loss_probability_pct']:.0f}% of resampled runs "
                    f"finished at a loss.")
    return " ".join(bits)


def format_report(r: dict) -> str:
    """CLI and phone rendering. Short enough to read on a lock screen."""
    lines = [
        f"Monte Carlo — {r['mode']}, {r['sims']:,} runs, {r['trades']} trades",
        "",
        "Max drawdown across orderings:",
    ]
    dd = r["max_dd_pct"]
    lines.append(f"  best  p95 {dd['p95']:>7.1f}%")
    lines.append(f"  typical p50 {dd['p50']:>5.1f}%")
    lines.append(f"  bad   p5  {dd['p5']:>7.1f}%")
    lines.append(f"  worst seen {r['worst_dd_pct']:>6.1f}%")
    lines.append("")
    if r.get("final_pct"):
        f = r["final_pct"]
        lines.append("Final return:")
        lines.append(f"  p5 {f['p5']:.1f}%  ·  p50 {f['p50']:.1f}%  "
                     f"·  p95 {f['p95']:.1f}%")
        lines.append(f"  {r['loss_probability_pct']:.0f}% of runs lost money")
        lines.append("")
    lines.append(r["verdict"])
    if r.get("final_note"):
        lines.append("")
        lines.append(r["final_note"])
    return "\n".join(lines)


def histogram(r_values: list[float], bins: int = 20) -> dict:
    """Bucket counts for the chart. Plain lists, no plotting library."""
    if not r_values:
        return {"edges": [], "counts": []}
    lo, hi = min(r_values), max(r_values)
    if lo == hi:
        return {"edges": [lo, hi], "counts": [len(r_values)]}
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in r_values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    edges = [round(lo + i * width, 3) for i in range(bins + 1)]
    return {"edges": edges, "counts": counts}
