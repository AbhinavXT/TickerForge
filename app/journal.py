"""Trade journal — what actually happened, in the same units as the backtest.

The tooling generates signals faster than discipline absorbs them. This module
is the counterweight: it records what you did, prices it in R, and puts those
numbers beside the backtest's for the same strategy tag. Live vs backtested,
per strategy, is the one comparison that says whether an edge survived contact
with reality — and it only means anything if both sides are measured the same
way.

Two rules make that true, and both are easy to get wrong:

  * **R is measured against the ORIGINAL stop.** If you trail a stop up and
    exit at the trailed level, the trade is still scored against the risk you
    took at entry. Re-basing R onto the trailed stop would inflate every winner
    and make expectancy meaningless — so the stop lives in two columns:
    `stop` moves as you trail it, `initial_stop` is written once and is what
    every R in this module is measured against.
  * **R is NET of charges.** The backtester charges cost_bps on both legs. A
    journal recording gross R against a backtest recording net would make live
    look systematically better than backtested, and you'd conclude the edge was
    holding up when it was the accounting that differed.

Statistics count CLOSED trades only, for the same reason `backtest._trade_stats`
does: an open position has no exit, so counting its mark-to-market books an
unrealised number as a result.
"""
from __future__ import annotations

from datetime import datetime

from . import db, risk

# Cost model used when actual charges aren't supplied. Matches the backtester's
# default so the two sides of the comparison start from the same assumption.
DEFAULT_COST_BPS = 10.0

VALID_SIDES = ("long", "short")
VALID_REASONS = ("stop", "target", "signal", "manual", "trail", "time")


class JournalError(ValueError):
    """Bad input — unknown trade, already closed, nonsense prices."""


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def estimate_charges(entry: float, exit_price: float, qty: int,
                     cost_bps: float = DEFAULT_COST_BPS) -> float:
    """Round-trip cost when the real contract-note figure isn't to hand.

    Charged on both legs at their own traded values, which is what actually
    happens — brokerage and STT apply to each side separately.
    """
    return (entry * qty + exit_price * qty) * cost_bps / 10_000.0


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["open"] = d.get("exit_time") is None
    return d


# -------------------------------------------------------------------- write
def open_trade(symbol: str, entry: float, qty: int, stop: float | None = None,
               side: str = "long", target: float | None = None,
               tag: str | None = None, notes: str | None = None,
               source: str = "manual", entry_time: str | None = None,
               checks: str | None = None) -> int:
    """Record a position you've taken. Returns the trade id.

    `stop` is optional but strongly wanted: without it there is no 1R, so the
    trade can never be scored in R and it drops out of every statistic that
    matters. The journal accepts it anyway — recording an undisciplined trade
    is more useful than refusing to record it — but `close_trade` will say so.

    `checks` is the pre-trade checklist's verdict, already encoded
    (checklist.encode). NULL means the checklist never ran on this trade.
    """
    symbol = symbol.upper()
    side = side.lower()
    if side not in VALID_SIDES:
        raise JournalError(f"side must be one of {VALID_SIDES}")
    if entry <= 0:
        raise JournalError("entry must be positive")
    if qty <= 0:
        raise JournalError("qty must be positive")
    if stop is not None:
        if stop <= 0:
            raise JournalError("stop must be positive")
        # A long stopped ABOVE entry (or a short stopped below) is almost
        # always a typo or a mixed-up side, and it would flip the sign of every
        # R this trade ever reports. Cheaper to catch here.
        if side == "long" and stop >= entry:
            raise JournalError(
                f"long stop {stop:g} is at or above entry {entry:g} — "
                f"did you mean side=short?")
        if side == "short" and stop <= entry:
            raise JournalError(
                f"short stop {stop:g} is at or below entry {entry:g} — "
                f"did you mean side=long?")

    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO trades (symbol, side, qty, entry, stop, initial_stop, "
            "target, entry_time, tag, notes, source, checks) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, side, qty, entry, stop, stop, target,
             entry_time or _now(), tag, notes, source, checks))
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, reason: str = "manual",
                charges: float | None = None, cost_bps: float = DEFAULT_COST_BPS,
                exit_time: str | None = None, notes: str | None = None) -> dict:
    """Close a position and compute its R.

    `charges` is the real round-trip cost if you have it from the contract
    note. Left out, it's estimated at `cost_bps` per side — the same model the
    backtester uses, so the comparison stays honest either way.
    """
    if exit_price <= 0:
        raise JournalError("exit price must be positive")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            raise JournalError(f"no trade #{trade_id}")
        if row["exit_time"] is not None:
            raise JournalError(
                f"trade #{trade_id} already closed at {row['exit']:g} "
                f"on {row['exit_time']}")

        t = dict(row)
        cost = (charges if charges is not None
                else estimate_charges(t["entry"], exit_price, t["qty"], cost_bps))

        r_mult = None
        basis = t.get("initial_stop") if t.get("initial_stop") is not None else t["stop"]
        if basis is not None:
            # Net R against the stop as it stood AT ENTRY, never the trailed
            # one. Costs become a per-share price drag, so charges shrink a
            # winner and deepen a loser — exactly what they do in the account.
            per_share_cost = cost / t["qty"] if t["qty"] else 0.0
            effective_exit = (exit_price - per_share_cost if t["side"] == "long"
                              else exit_price + per_share_cost)
            r_mult = risk.r_multiple(t["entry"], basis, effective_exit,
                                     side=t["side"])

        merged_notes = t["notes"]
        if notes:
            merged_notes = f"{t['notes']}\n{notes}" if t["notes"] else notes

        conn.execute(
            "UPDATE trades SET exit=?, exit_time=?, reason=?, charges=?, "
            "r_multiple=?, notes=? WHERE id=?",
            (exit_price, exit_time or _now(), reason, round(cost, 2),
             r_mult, merged_notes, trade_id))
        out = conn.execute("SELECT * FROM trades WHERE id=?",
                           (trade_id,)).fetchone()

    result = _row_to_dict(out)
    # Closing a trade is the only moment realised R changes, so it's the
    # earliest point the limit can be newly breached — and the moment the
    # notice is most useful. Wrapped because a failed push must never leave a
    # trade unrecorded: the close already committed above.
    try:
        notify_if_breached()
    except (Exception, SystemExit) as e:
        # SystemExit, not just Exception: notify.send calls config.require,
        # which raises SystemExit when NTFY_TOPIC isn't set — and SystemExit
        # inherits from BaseException. Without this, an unconfigured ntfy
        # topic would crash every single trade close.
        print(f"[journal] guard check after close failed: {e}")
    return result


def update_trade(trade_id: int, **fields) -> dict:
    """Amend a trade. Only a safe subset of columns is writable."""
    allowed = {"stop", "target", "tag", "notes", "qty", "entry"}
    bad = set(fields) - allowed
    if bad:
        raise JournalError(f"cannot update {', '.join(sorted(bad))}")
    if not fields:
        raise JournalError("nothing to update")
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as conn:
        n = conn.execute(f"UPDATE trades SET {sets} WHERE id=?",
                         (*fields.values(), trade_id)).rowcount
        if not n:
            raise JournalError(f"no trade #{trade_id}")
        row = conn.execute("SELECT * FROM trades WHERE id=?",
                           (trade_id,)).fetchone()
    return _row_to_dict(row)


def delete_trade(trade_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM trades WHERE id=?",
                            (trade_id,)).rowcount > 0


# --------------------------------------------------------------------- read
def get(trade_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?",
                           (trade_id,)).fetchone()
    return _row_to_dict(row) if row else None


def open_positions(symbol: str | None = None,
                   source: str | None = None) -> list[dict]:
    """Open trades. `source` filters manual/paper/import — see `closed_trades`
    for why that separation matters."""
    q = "SELECT * FROM trades WHERE exit_time IS NULL"
    args: list = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol.upper())
    if source:
        q += " AND source=?"
        args.append(source)
    q += " ORDER BY entry_time DESC, id DESC"
    with db.connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(q, args).fetchall()]


def closed_trades(symbol: str | None = None, tag: str | None = None,
                  since: str | None = None, limit: int = 200,
                  source: str | None = None,
                  exclude_source: str | None = None) -> list[dict]:
    """Closed trades, optionally filtered by where they came from.

    The separation matters more than it looks. Paper trades sit in this same
    table so they can be compared against real ones on identical terms — but
    blending them into one expectancy would be the single most misleading
    number the app could produce: a month of flawless simulated trading would
    quietly lift the figure you use to judge your actual results.

    So: `source='paper'` to look at simulated only, `exclude_source='paper'`
    for real money only. Neither is the default, because a silent default here
    is exactly the kind of thing you'd forget and then trust.
    """
    q = "SELECT * FROM trades WHERE exit_time IS NOT NULL"
    args: list = []
    if source:
        q += " AND source=?"; args.append(source)
    if exclude_source:
        q += " AND (source IS NULL OR source != ?)"; args.append(exclude_source)
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper())
    if tag:
        q += " AND tag=?"; args.append(tag)
    if since:
        q += " AND exit_time >= ?"; args.append(since)
    q += " ORDER BY exit_time DESC, id DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(q, args).fetchall()]


def live_r(trade: dict, last_price: float) -> float | None:
    """Where an open trade stands right now, in R. Not a result — a position.

    Measured against the entry-time stop like everything else, so the number
    you watch intraday is on the same scale as the one the trade will be
    scored at when it closes.
    """
    basis = trade.get("initial_stop") or trade.get("stop")
    if basis is None or last_price is None:
        return None
    return risk.r_multiple(trade["entry"], basis, last_price,
                           side=trade["side"])


def open_risk_r(symbol: str | None = None) -> float:
    """Total R still at risk across open positions, for the daily guard.

    Measured against the CURRENT stop, not a flat 1R per position. A trade
    whose stop has been trailed above entry has no downside left to spend, and
    counting it as a full R eats the day's budget with risk that no longer
    exists — the guard then stands you down while you are, in fact, risking
    nothing.

    The unit is the trade's ORIGINAL 1R, so the number stays on the same scale
    as everything else in the journal: a stop moved halfway to breakeven
    contributes 0.5, at breakeven 0, and beyond it 0 rather than a negative
    (locked-in profit is not a licence to lose more elsewhere — that would let
    one good trade fund an unlimited bad afternoon).

    A position logged without a stop contributes nothing, because there is no
    defined risk to add up. That is the same exclusion `stats` makes, and the
    same reason: undefined risk cannot be measured, only regretted.
    """
    total = 0.0
    for t in open_positions(symbol):
        stop = t.get("stop")
        basis = t.get("initial_stop")
        basis = basis if basis is not None else stop
        if stop is None or basis is None:
            continue
        # What the trade scores AT its current stop, in original-R units. That
        # is a loss while the stop sits below entry, so negate it to get the
        # risk still on the table. Reusing r_multiple keeps long and short on
        # one code path.
        at_stop = risk.r_multiple(t["entry"], basis, stop, side=t["side"])
        if at_stop is None:
            continue
        total += max(0.0, -at_stop)
    return round(total, 2)


# --------------------------------------------------------------- statistics
def stats(trades: list[dict] | None = None, tag: str | None = None,
          since: str | None = None, source: str | None = None,
          exclude_source: str | None = None) -> dict:
    """Expectancy and friends over CLOSED trades, measured in net R.

    Deliberately mirrors `backtest._trade_stats`: same exclusions, same net
    basis, so the two can be printed side by side without a footnote
    explaining why they aren't comparable.
    """
    rows = trades if trades is not None else closed_trades(
        tag=tag, since=since, limit=100_000,
        source=source, exclude_source=exclude_source)
    scored = [t for t in rows if t.get("r_multiple") is not None]
    unscored = len(rows) - len(scored)

    if not scored:
        return {"trades": 0, "unscored": unscored, "win_rate_pct": None,
                "expectancy_r": None, "profit_factor": None, "total_r": 0.0,
                "avg_win_r": None, "avg_loss_r": None, "best_r": None,
                "worst_r": None, "max_consec_wins": 0, "max_consec_losses": 0,
                "max_drawdown_r": 0.0}

    rs = [t["r_multiple"] for t in scored]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    won = sum(wins)
    lost = -sum(losses)

    # streaks, oldest first
    ordered = sorted(scored, key=lambda t: (t.get("exit_time") or "", t["id"]))
    mw = ml = cw = cl = 0
    for t in ordered:
        if t["r_multiple"] > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        mw, ml = max(mw, cw), max(ml, cl)

    # R drawdown along the realised equity curve
    peak = cum = 0.0
    max_dd = 0.0
    for t in ordered:
        cum += t["r_multiple"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "trades": len(scored),
        "unscored": unscored,                 # logged without a stop
        "win_rate_pct": round(len(wins) / len(rs) * 100, 1),
        "expectancy_r": round(sum(rs) / len(rs), 2),
        "profit_factor": round(won / lost, 2) if lost > 0 else None,
        "total_r": round(sum(rs), 2),
        "avg_win_r": round(won / len(wins), 2) if wins else None,
        "avg_loss_r": round(-lost / len(losses), 2) if losses else None,
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
        "max_consec_wins": mw,
        "max_consec_losses": ml,
        "max_drawdown_r": round(max_dd, 2),
    }


def by_tag(since: str | None = None) -> dict:
    """Stats per strategy tag — the left-hand column of the live-vs-backtest
    table. Untagged trades are grouped under '(untagged)' rather than dropped,
    because a pile of untagged trades is itself worth seeing."""
    rows = closed_trades(since=since, limit=100_000)
    groups: dict[str, list] = {}
    for t in rows:
        groups.setdefault(t.get("tag") or "(untagged)", []).append(t)
    return {k: stats(v) for k, v in sorted(groups.items())}


def realised_r_today(day: str | None = None) -> float:
    """Sum of R closed today. The daily loss guard's input."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(r_multiple), 0) s FROM trades "
            "WHERE exit_time IS NOT NULL AND r_multiple IS NOT NULL "
            "AND substr(exit_time, 1, 10) = ?", (day,)).fetchone()
    return round(row["s"], 2)


def guard(daily_loss_r: float = 2.0, max_positions: int = 5,
          day: str | None = None) -> dict:
    """Should you take another trade? Journal numbers into the risk model."""
    return risk.check_limits(
        realised_r_today=realised_r_today(day),
        open_risk_r=open_risk_r(),
        daily_loss_r=daily_loss_r,
        open_positions=len(open_positions()),
        max_positions=max_positions,
    )


def _stand_down_text(g: dict) -> str:
    """Factual, not scolding. The limit is a decision you already made when
    calm; this is just the reminder that it's been reached."""
    lines = [f"Daily limit reached: {g['realised_r']:+.2f}R realised "
             f"against your {g['daily_loss_r']:.0f}R limit."]
    if g["open_risk_r"]:
        lines.append(f"{g['open_risk_r']:.2f}R still live in "
                     f"{g['open_positions']} open position(s).")
    lines.append("Open trades are untouched — manage them as planned. "
                 "This is about not opening new ones.")
    return "\n".join(lines)


def notify_if_breached(daily_loss_r: float | None = None,
                       max_positions: int | None = None,
                       day: str | None = None, force: bool = False) -> str | None:
    """Push a stand-down notice the first time the limit is breached today.

    Safe to call from anywhere and as often as you like — the "already told
    you" flag lives in `meta` keyed by date, so it survives a watcher restart
    and resets on its own at midnight. Without persistence a crash-restart
    loop would re-push the same notice all afternoon.

    Returns the message if one was sent, else None.

    This is the one place the journal reaches out rather than being asked, and
    that's deliberate: a loss limit you have to remember to check is a limit
    that works on the days you don't need it and fails on the days you do.
    """
    from . import config, notify

    dl = daily_loss_r if daily_loss_r is not None else float(config.get("DAILY_LOSS_R", 2))
    mp = max_positions if max_positions is not None else int(config.get("MAX_POSITIONS", 5))
    day = day or datetime.now().strftime("%Y-%m-%d")

    g = guard(daily_loss_r=dl, max_positions=mp, day=day)
    # Only the loss limit warrants an unprompted push. Hitting the max-position
    # count is a normal state you can see coming, not news worth a buzz.
    breached = g["realised_r"] <= -abs(dl) or g["effective_r"] <= -abs(dl)
    if not breached:
        return None
    if not force and db.get_meta("journal_stand_down") == day:
        return None

    msg = _stand_down_text(g)
    try:
        notify.send(msg, title="TickerForge — stand down",
                    priority="high", tags="octagonal_sign")
    except (Exception, SystemExit) as e:
        # Deliberately NOT marking as notified: a transient failure here would
        # otherwise silently eat the day's only warning.
        print(f"[journal] stand-down push failed: {e}")
        return None
    db.set_meta("journal_stand_down", day)
    return msg


def stood_down_today(day: str | None = None) -> bool:
    """Has the stand-down notice already gone out today? Used to annotate
    alerts, so a signal arriving after the limit says so instead of reading
    like an invitation."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    return db.get_meta("journal_stand_down") == day


# ------------------------------------------------------------------- import
def import_backtest(trades: list[dict], symbol: str, tag: str,
                    qty: int = 1) -> int:
    """Load a backtest trade list into the journal for side-by-side review.

    Marked `source='import'` so imported rows can be excluded from live stats —
    mixing simulated and real trades in one expectancy number would defeat the
    entire purpose of the comparison.
    """
    n = 0
    for t in trades:
        if t.get("open"):
            continue                      # no exit, nothing to score
        tid = open_trade(symbol=symbol, entry=t["entry"], qty=qty,
                         side="long", tag=tag, source="import",
                         entry_time=str(t.get("entry_time")))
        # net_pct already includes the simulator's costs, so charges=0 here
        # avoids double-counting them.
        close_trade(tid, exit_price=t["exit"], reason=t.get("reason", "signal"),
                    charges=0.0, exit_time=str(t.get("exit_time")))
        n += 1
    return n


# ---------------------------------------------------------------- rendering
def format_trade(t: dict, last_price: float | None = None) -> str:
    """One trade, phone-sized."""
    head = f"#{t['id']} {t['symbol']} {t['side']} {t['qty']} @ {t['entry']:g}"
    if t["open"]:
        bits = [head]
        if t.get("stop"):
            bits.append(f"SL {t['stop']:g}")
        if last_price is not None:
            r = live_r(t, last_price)
            bits.append(f"now {last_price:g}" + (f" ({r:+.2f}R)" if r is not None else ""))
        return "  ".join(bits)
    r = t.get("r_multiple")
    return (f"{head} → {t['exit']:g}  "
            f"{f'{r:+.2f}R' if r is not None else 'unscored'}  "
            f"[{t.get('reason') or '?'}]")


def format_stats(s: dict, label: str = "") -> str:
    if not s["trades"]:
        extra = f" ({s['unscored']} logged without a stop)" if s["unscored"] else ""
        return f"{label}no closed trades yet{extra}"
    lines = [
        f"{label}{s['trades']} trade{'s' if s['trades'] != 1 else ''} · "
        f"{s['win_rate_pct']:g}% win · expectancy {s['expectancy_r']:+.2f}R",
        f"total {s['total_r']:+.2f}R · PF {s['profit_factor'] or '—'} · "
        f"maxDD {s['max_drawdown_r']:.2f}R",
        f"avg win {s['avg_win_r']:+.2f}R · avg loss {s['avg_loss_r']:+.2f}R"
        if s["avg_win_r"] is not None and s["avg_loss_r"] is not None else "",
    ]
    if s["unscored"]:
        lines.append(f"⚠ {s['unscored']} trade(s) had no stop — not counted")
    return "\n".join(x for x in lines if x)


# ------------------------------------------------- live against backtested
def compare(tag: str, source: str = "paper", days: int = 730,
            stop_pct: float | None = None, symbols=None) -> dict:
    """The paper-tracking loop this module was written for.

    Live trades on one strategy tag, beside the same strategy backtested over
    the same symbols. It is the whole reason for running a paper account: not
    to see whether the account made money, but whether the edge the backtest
    promised survived contact with a real sequence of days.

    **Both sides are aggregated by stats(), above.** The backtest reports its
    own numbers as percentages per trade and the journal reports R multiples,
    which is why these two tables were never actually comparable and why this
    function did not exist. Feeding the simulator's own trade list back
    through the journal's statistics removes the possibility of the two
    diverging: there is one definition of expectancy, not two that agree
    today.

    That needs the backtest run with a stop model, since R is only defined
    against one — an unsized backtest records no r_multiple, and comparing
    percentage expectancy to R expectancy is comparing two different things
    with the same name.

    **The comparison is over the same symbols.** Backtesting the strategy on
    fifty names and comparing that to eight months of paper on eight would be
    measuring universe selection, not the edge.
    """
    from . import backtest as bt

    live_rows = closed_trades(tag=tag, source=source, limit=100_000)
    live = stats(live_rows)

    syms = list(symbols) if symbols else sorted(
        {t["symbol"] for t in live_rows})
    stop_pct = stop_pct or _paper_stop_pct()

    tested, failed = [], []
    for symbol in syms:
        try:
            res = bt.run(symbol, tag, days=days,
                         risk={"sl_pct": stop_pct, "sizing": "fixed_fractional"})
        except Exception as e:                       # noqa: BLE001
            failed.append(f"{symbol}: {e}")
            continue
        tested += [t for t in res.get("trades", [])
                   if not t.get("open") and t.get("r_multiple") is not None]

    back = stats(tested)
    return {
        "tag": tag,
        "symbols": syms,
        "stop_pct": stop_pct,
        "live": live,
        "backtest": back,
        "failed": failed,
        "verdict": _verdict(live, back),
    }


def _paper_stop_pct() -> float:
    from . import config
    try:
        return float(config.get("PAPER_STOP_PCT", 5) or 5)
    except (TypeError, ValueError):
        return 5.0


def _verdict(live: dict, back: dict) -> dict:
    """Is the difference real, or is it twelve trades?

    The number everyone reads first is the gap in expectancy, and on a small
    sample that gap is mostly noise. So this reports the standard error of the
    live expectancy and whether the backtested figure sits inside two of them.

    It is deliberately hard to get an encouraging answer out of. A paper
    account that has run for six weeks cannot distinguish a good system from a
    lucky one, and saying so is more useful than a number that looks like it
    knows.
    """
    n = live.get("trades") or 0
    le, be = live.get("expectancy_r"), back.get("expectancy_r")
    out = {"trades": n, "gap_r": None, "standard_error": None,
           "consistent": None,
           "note": ""}
    if n < 2 or le is None or be is None:
        out["note"] = ("too few closed paper trades to compare — "
                       f"{n} so far")
        return out

    # Spread of R across the live trades, from the summary rather than the
    # rows: avg win and avg loss with the win rate reconstruct it closely
    # enough for an order-of-magnitude error bar, which is all this claims.
    spread = abs(live.get("avg_win_r") or 0) + abs(live.get("avg_loss_r") or 0)
    se = spread / (n ** 0.5) if spread else None
    out["gap_r"] = round(le - be, 2)
    out["standard_error"] = round(se, 2) if se else None
    if se:
        out["consistent"] = abs(le - be) <= 2 * se
        if n < 30:
            out["note"] = (f"{n} trades is too few to tell a real gap from "
                           f"noise — the error bar is ±{2 * se:.2f}R")
        elif out["consistent"]:
            out["note"] = "live is within noise of the backtest"
        else:
            worse = le < be
            out["note"] = ("live is running BELOW the backtest by more than "
                           "noise explains — check fills, costs and slippage"
                           if worse else
                           "live is running above the backtest, which is also "
                           "worth explaining before trusting it")
    return out


def compare_text(cmp: dict) -> str:
    """Two columns, same units, with the sample size where you can't miss it."""
    live, back = cmp["live"], cmp["backtest"]

    def row(label, key, fmt="{:+.2f}"):
        a, b = live.get(key), back.get(key)
        fa = "—" if a is None else fmt.format(a)
        fb = "—" if b is None else fmt.format(b)
        return f"  {label:<16}{fa:>10}{fb:>12}"

    lines = [
        f"{cmp['tag']} — live vs backtested",
        f"  {len(cmp['symbols'])} symbol(s), {cmp['stop_pct']:g}% stop",
        "",
        f"  {'':<16}{'live':>10}{'backtest':>12}",
        row("trades", "trades", "{:.0f}"),
        row("win rate %", "win_rate_pct", "{:.0f}"),
        row("expectancy R", "expectancy_r"),
        row("avg win R", "avg_win_r"),
        row("avg loss R", "avg_loss_r"),
        row("profit factor", "profit_factor", "{:.2f}"),
        row("max DD R", "max_drawdown_r", "{:.1f}"),
        row("total R", "total_r", "{:+.1f}"),
    ]
    v = cmp["verdict"]
    if v.get("note"):
        lines += ["", f"  {v['note']}"]
    if cmp["failed"]:
        lines.append(f"  ({len(cmp['failed'])} symbol(s) could not be "
                     f"backtested)")
    return "\n".join(lines)
