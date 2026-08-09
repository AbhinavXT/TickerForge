"""Value with momentum — the join nothing off the shelf can do for you.

Three questions that are usually asked by three different tools:

  * **Is it cheap?**    your own Damodaran valuation, not a screener's P/E
  * **Is it working?**  the signal stance from your own strategies
  * **Is it winning?**  relative strength against the index

Each alone is a well-known way to lose money. Cheap alone is a value trap —
the market is often right that a business is worth less. Momentum alone buys
whatever went up, including the part that went up because it was overvalued.
Signal alone is a timing tool with nothing to say about what it's timing.

The intersection is the interesting bit, and it's small on purpose. A screen
returning forty names hasn't screened. If this comes back with three, that's
the honest answer to "what is simultaneously cheap, strong and turning".

Nothing here computes anything new. It's a join over work already done, which
is why it was a [S] on the roadmap and why it's cheap to keep current.
"""
from __future__ import annotations

from . import rs, signals, valuation

# Defaults chosen to be restrictive. The failure mode of a screen is a long
# list, because a long list gets skimmed and skimming defeats the purpose.
DEFAULT_MIN_RS = 70.0
DEFAULT_VERDICTS = ("undervalued", "fair")


def _stance(symbol: str, matrix: dict) -> tuple[int, int, str]:
    """How many of the configured strategies are long on this name."""
    row = matrix.get(symbol) or {}
    total = len(row)
    long_ = sum(1 for v in row.values() if v)
    if not total:
        return 0, 0, "—"
    if long_ == total:
        label = "all long"
    elif long_ == 0:
        label = "all flat"
    else:
        label = f"{long_}/{total} long"
    return long_, total, label


def run(symbols: list[str] | None = None, min_rs: float = DEFAULT_MIN_RS,
        verdicts: tuple[str, ...] = DEFAULT_VERDICTS,
        require_long: bool = True, days: int = 420) -> dict:
    """Score every symbol on all three axes and return those passing all of them.

    Returns both the passing rows AND everything considered, because the near
    misses are where the useful thinking happens: a name that fails on one
    axis by a whisker is worth more attention than the twentieth pass.
    """
    val = valuation.screen(symbols)
    rows_val = {r["symbol"]: r for r in val.get("rows", [])}
    universe = symbols or sorted(rows_val)

    # push=False: a screen is a question, not an event. Firing the EOD alert
    # push every time someone opens a page would train you to ignore it.
    try:
        sig = signals.scan(symbols=universe, push=False, record=False)
        matrix = sig.get("matrix", {})
    except (Exception, SystemExit):               # noqa: BLE001
        # SystemExit is deliberate: signals.scan exits the process when there
        # is no Kite session, which is right for a CLI command and fatal
        # inside a request handler. Losing the signal axis degrades this
        # screen to value-plus-strength, which `_note` says out loud — it
        # should never take the whole page down.
        matrix = {}

    try:
        strength = rs.for_symbols(universe, days=days)
    except rs.RSError:
        strength = {}

    considered = []
    for symbol in universe:
        v = rows_val.get(symbol, {})
        r = strength.get(symbol, {})
        long_, total, label = _stance(symbol, matrix)

        verdict = v.get("verdict")
        rs_pct = r.get("rs")
        row = {
            "symbol": symbol,
            "name": v.get("name"),
            "price": v.get("price"),
            "fair": v.get("composite"),
            "mos_pct": v.get("mos_pct"),
            "verdict": verdict,
            "n_models": v.get("n_models"),
            "rs": rs_pct,
            "r3m": r.get("r3m"),
            "r6m": r.get("r6m"),
            "stance": label,
            "long_count": long_,
            "strategies": total,
        }
        # Recorded per axis so the UI can say WHY something failed rather than
        # just omitting it — an invisible rejection teaches you nothing.
        row["pass_value"] = verdict in verdicts
        row["pass_rs"] = rs_pct is not None and rs_pct >= min_rs
        row["pass_signal"] = (long_ > 0) if require_long else True
        row["passes"] = (row["pass_value"] and row["pass_rs"]
                         and row["pass_signal"])
        row["failed"] = [k for k, ok in (("value", row["pass_value"]),
                                         ("strength", row["pass_rs"]),
                                         ("signal", row["pass_signal"]))
                         if not ok]
        considered.append(row)

    hits = [r for r in considered if r["passes"]]
    # Best margin of safety first among the passes: once all three gates are
    # satisfied, price is the remaining variable you control.
    hits.sort(key=lambda r: (r["mos_pct"] is None, -(r["mos_pct"] or 0)))
    considered.sort(key=lambda r: (len(r["failed"]), -(r["rs"] or 0)))

    return {
        "filters": {"min_rs": min_rs, "verdicts": list(verdicts),
                    "require_long": require_long},
        "considered": len(considered),
        "hits": hits,
        "near_misses": [r for r in considered if len(r["failed"]) == 1][:15],
        "rows": considered,
        "note": _note(len(hits), len(considered), strength, matrix),
    }


def _note(hits: int, total: int, strength: dict, matrix: dict) -> str:
    """Say plainly when a screen is empty for a boring reason.

    An empty table has two very different causes — nothing qualifies, or an
    input is missing — and they demand opposite responses. Conflating them
    means you conclude the market has nothing to offer when actually you never
    backfilled the candles.
    """
    if not strength:
        return ("No relative strength data — backfill the universe's daily "
                "candles, then re-run. Every row is failing on strength for "
                "that reason alone.")
    if not matrix:
        return ("No signal stances available — the screen is running on value "
                "and strength only.")
    if hits == 0:
        return (f"Nothing passed all three filters out of {total} considered. "
                f"That is a normal result; check the near misses.")
    return f"{hits} of {total} passed all three."


def summary(result: dict, top: int = 8) -> str:
    """CLI / phone rendering."""
    hits = result["hits"]
    lines = [f"Value + momentum — {len(hits)} of {result['considered']}", ""]
    if not hits:
        lines.append(result["note"])
        for r in result["near_misses"][:5]:
            lines.append(f"  ~ {r['symbol']:<12} failed on {r['failed'][0]}")
        return "\n".join(lines)
    for r in hits[:top]:
        mos = f"{r['mos_pct']:+.0f}%" if r["mos_pct"] is not None else "—"
        rs_ = f"{r['rs']:.0f}" if r["rs"] is not None else "—"
        lines.append(f"  {r['symbol']:<12} MoS {mos:>6}  RS {rs_:>4}  "
                     f"{r['stance']}")
    return "\n".join(lines)
