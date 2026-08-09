"""Paper broker — the edge, tested against reality, with nothing at risk.

Every number the backtester produces is a claim about a past you already know
the answer to. This is the first thing in the project that makes a claim about
a future it hasn't seen: signals fire, orders queue, the next session fills
them, and after a month the journal's live-vs-backtest table says whether the
edge survived contact with an actual sequence of days.

The four rules that make it worth anything
------------------------------------------
**Fill at the NEXT open, never the close that generated the signal.** This is
the whole discipline. A flip computed from tonight's close could not have been
traded at tonight's close — you didn't know until it had already happened.
Backtests that fill at the signal bar's close are the single most common way
to manufacture an edge that evaporates live, and the gap between close and
next open is where a real strategy's profit quietly goes.

**Orders persist.** The window between signal and fill is overnight, which is
exactly when the watcher gets restarted or the token expires. An in-memory
queue would drop precisely the trades the experiment exists to measure — and
drop them silently, leaving a month of results that look fine and aren't.

**Costs on both legs**, on the same basis the backtester charges them, or the
comparison it exists to feed is meaningless.

**Sized by the real risk model.** A paper run that bets the account on every
signal tests a strategy nobody would trade. It uses `risk.size`, the same
function `/size` uses, so the position is the position you'd actually have
taken.

What it deliberately does NOT model
-----------------------------------
Slippage beyond the open price, partial fills, and impact. On liquid Nifty-200
names at open, in the sizes a personal account trades, those are small — but
they are not zero, and results here should be read as a mild over-estimate
rather than a floor. `SLIPPAGE_BPS` exists if you want to be harsher.

Paper rows live in the journal with `source='paper'` so they can be compared
against real ones on identical terms, and every stats query has to opt in or
out explicitly. Blending them would be the most misleading number the app
could produce.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import (candles, config, db, instruments, journal, notify,
               risk, signals)

SOURCE = "paper"
SLIPPAGE_BPS = 0.0        # extra cost on top of the open, if you want it harsher


class PaperError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cost_bps() -> float:
    return float(config.get("COST_BPS", 10) or 10)


# ------------------------------------------------------------------ queueing
def queue_order(symbol: str, kind: str, tag: str, signal_price: float,
                qty: int | None = None, stop: float | None = None,
                reason: str = "signal", note: str | None = None) -> int:
    """Record an intention. Nothing is filled until the next session opens."""
    if kind not in ("entry", "exit"):
        raise PaperError("kind must be 'entry' or 'exit'")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO paper_orders (symbol, kind, tag, qty, stop, reason, "
            "signal_time, signal_price, status) "
            "VALUES (?,?,?,?,?,?,?,?,'pending')",
            [symbol.upper(), kind, tag, qty, stop, reason, _now(),
             signal_price])
        return cur.lastrowid


def pending(symbol: str | None = None) -> list[dict]:
    q = "SELECT * FROM paper_orders WHERE status='pending'"
    args: list = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol.upper())
    q += " ORDER BY id"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def orders(limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?",
            [limit]).fetchall()]


def cancel(order_id: int, why: str = "cancelled") -> bool:
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE paper_orders SET status='cancelled', note=? "
            "WHERE id=? AND status='pending'", [why, order_id]).rowcount
    return n > 0


# ------------------------------------------------------------------ filling
def _next_open(symbol: str, after_iso: str) -> tuple[float, str] | None:
    """The first open that occurred strictly AFTER the signal.

    Returns None while no such bar exists yet — which is the normal state
    between the close that produced a signal and the open that fills it, not
    an error. Callers keep the order pending and try again.
    """
    inst = instruments.lookup(symbol)
    if not inst:
        return None
    try:
        bars = candles.get(inst["instrument_token"], "day", 30)
    except Exception:                             # noqa: BLE001
        return None
    signal_ts = datetime.fromisoformat(after_iso).timestamp()
    for bar in bars:
        # Strictly after: a bar stamped the same moment as the signal is the
        # bar the signal was computed from, and filling there is the lookahead
        # this whole module exists to avoid.
        if bar["time"] > signal_ts:
            return bar["open"], datetime.fromtimestamp(bar["time"]).isoformat(
                timespec="seconds")
    return None


def _fill_price(raw: float, kind: str) -> float:
    """Apply optional slippage against you, whichever way you're going."""
    if not SLIPPAGE_BPS:
        return raw
    adj = raw * SLIPPAGE_BPS / 10000.0
    return raw + adj if kind == "entry" else raw - adj


def fill_pending(account_size: float | None = None,
                 risk_pct: float | None = None) -> dict:
    """Try to fill every pending order at its next available open.

    Safe to call as often as you like: an order that has no next bar yet stays
    pending and is picked up whenever one appears.
    """
    account = float(account_size if account_size is not None
                    else config.get("ACCOUNT_SIZE", 0) or 0)
    pct = float(risk_pct if risk_pct is not None
                else config.get("RISK_PCT", 1) or 1)
    cost = _cost_bps()

    filled, waiting, problems = [], 0, []
    for order in pending():
        nxt = _next_open(order["symbol"], order["signal_time"])
        if nxt is None:
            waiting += 1
            continue
        raw, when = nxt
        price = _fill_price(raw, order["kind"])

        try:
            if order["kind"] == "entry":
                rec = _fill_entry(order, price, when, account, pct, cost)
            else:
                rec = _fill_exit(order, price, when, cost)
        except PaperError as e:
            problems.append(f"{order['symbol']}: {e}")
            cancel(order["id"], str(e))
            continue
        if rec is None:
            waiting += 1
            continue
        filled.append(rec)

    return {"filled": filled, "still_pending": waiting, "problems": problems}


def _fill_entry(order, price, when, account, pct, cost) -> dict | None:
    qty = order["qty"]
    stop = order["stop"]
    if not qty:
        if not stop or not account:
            raise PaperError("no quantity and nothing to size from "
                             "(need a stop and ACCOUNT_SIZE)")
        # The same sizing function /size uses, so the paper position is the
        # position you'd actually have taken.
        sized = risk.size(entry=price, stop=stop, account=account, risk_pct=pct)
        qty = sized["qty"]
        if qty < 1:
            raise PaperError("sized to zero shares at this price")

    charges = journal.estimate_charges(price, price, qty, cost_bps=cost)
    trade_id = journal.open_trade(
        order["symbol"], entry=price, qty=qty, stop=stop,
        tag=order["tag"], source=SOURCE, entry_time=when,
        notes=f"paper fill at next open; signalled at "
              f"{order['signal_price']} on {order['signal_time'][:10]}")
    _mark(order["id"], price, when, trade_id)
    return {"order_id": order["id"], "symbol": order["symbol"],
            "kind": "entry", "qty": qty, "price": round(price, 2),
            "when": when, "trade_id": trade_id, "charges": round(charges, 2)}


def _fill_exit(order, price, when, cost) -> dict | None:
    open_rows = journal.open_positions(order["symbol"], source=SOURCE)
    if not open_rows:
        raise PaperError("no open paper position to exit")
    trade = open_rows[0]
    charges = journal.estimate_charges(trade["entry"], price, trade["qty"],
                                       cost_bps=cost)
    closed = journal.close_trade(trade["id"], exit_price=price,
                                 reason=order["reason"], charges=charges,
                                 exit_time=when)
    _mark(order["id"], price, when, trade["id"])
    return {"order_id": order["id"], "symbol": order["symbol"], "kind": "exit",
            "qty": trade["qty"], "price": round(price, 2), "when": when,
            "trade_id": trade["id"], "r_multiple": closed.get("r_multiple"),
            "charges": round(charges, 2)}


def _mark(order_id: int, price: float, when: str, trade_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET status='filled', fill_price=?, "
            "fill_time=?, trade_id=? WHERE id=?",
            [price, when, trade_id, order_id])


# ------------------------------------------------------ the signal → order step
def _trail_config() -> tuple[float | None, int]:
    """(multiplier, ATR period) for the trailing stop, or (None, _) if off.

    Defaults mirror `backtest._apply_risk_defaults` — 22-bar ATR — because a
    paper account trailing on a different period from the backtest it is being
    compared against is measuring a different system.
    """
    try:
        mult = float(config.get("PAPER_TRAIL_MULT", 0) or 0)
    except (TypeError, ValueError):
        mult = 0.0
    try:
        period = max(2, int(config.get("PAPER_TRAIL_PERIOD", 22) or 22))
    except (TypeError, ValueError):
        period = 22
    return (mult if mult > 0 else None), period


def _effective_stop(trade: dict, bars: list[dict], upto: int) -> float | None:
    """Where the stop actually sits today: the initial one, ratcheted up.

    `highest high since entry - mult x ATR`, never below the entry stop and
    never moving down — the same construction the backtester uses, so the two
    exit in the same places.

    **The journal's stored stop is not changed.** R is measured from the stop
    the position was sized against; rewriting it as the trail moves would
    rescale every trade's R as it ran, and a system's expectancy would depend
    on how long you held. The backtester keeps `entry_stop` separate for
    exactly this reason and paper follows it.

    Without a trail configured this returns the entry stop unchanged, which is
    the behaviour before any of this existed.
    """
    from . import indicators as ind

    base = trade.get("stop")
    mult, period = _trail_config()
    if mult is None or base is None:
        return base

    entry_i = _entry_index(bars, trade.get("entry_time"))
    if entry_i is None or upto <= entry_i:
        return base
    atr = ind.atr(bars, period)
    stop = base
    for i in range(entry_i, upto + 1):
        if atr[i] is None:
            continue
        high = max(b["high"] for b in bars[entry_i:i + 1]
                   if b.get("high") is not None)
        stop = max(stop, high - mult * atr[i])
    # A trail is a ratchet: it may sit above the entry price, locking a gain
    # in, but it never gives ground.
    return stop


def _entry_index(bars: list[dict], entry_time) -> int | None:
    if not entry_time:
        return None
    try:
        day = datetime.fromisoformat(str(entry_time)[:19]).date()
    except ValueError:
        return None
    for i, b in enumerate(bars):
        if datetime.fromtimestamp(b["time"], tz=timezone.utc).date() >= day:
            return i
    return None


def check_stops(now=None, cost: float | None = None) -> dict:
    """Close any open paper position whose stop was breached.

    Without this the paper broker never honours its own stop: the stop is
    recorded when the trade opens, used to size it, and then ignored for ever.
    Exits only ever came from a strategy flip, so a position that fell 40%
    stayed open until the strategy happened to turn — and every expectancy
    figure the journal produced was flattered by losers that would have been
    cut in reality.

    **Gap-through matters.** If the day opened below the stop you did not get
    the stop, you got the open. Filling at the stop price regardless is the
    single most common way a simulator invents money, and the backtester
    already refuses to do it — this keeps the two in agreement, which is the
    whole point of running paper alongside a backtest.

    Checked against the day's low, on closed bars only. Intraday stop hunting
    isn't modelled: with daily bars there is no honest way to know whether the
    low came before or after the close.
    """
    from . import candles, instruments

    cost = cost if cost is not None else _cost_bps()
    closed, problems = [], []
    for trade in journal.open_positions(source=SOURCE):
        stop = trade.get("stop")
        if not stop:
            continue
        inst = instruments.lookup(trade["symbol"])
        if not inst:
            continue
        try:
            bars = candles.get(inst["instrument_token"], "day", 10)
        except Exception as e:                       # noqa: BLE001
            problems.append(f"{trade['symbol']}: {e}")
            continue
        idx = _closed_bar_index(bars, trade.get("entry_time"), now)
        if idx is None:
            continue
        bar = bars[idx]
        # The trail, not the entry stop. `stop` above stays the level R is
        # measured from; this is where the exit actually sits today.
        level = _effective_stop(trade, bars, idx) or stop
        if bar.get("low") is None or bar["low"] > level:
            continue
        stop = level

        # Gapped through, or traded through? Different fills, and the
        # difference is real money in a real account.
        price = min(bar["open"], stop) if bar.get("open") else stop
        gapped = bool(bar.get("open") and bar["open"] < stop)
        when = _bar_time(bar)
        charges = journal.estimate_charges(trade["entry"], price,
                                           trade["qty"], cost_bps=cost)
        done = journal.close_trade(
            trade["id"], exit_price=price,
            reason="gap through stop" if gapped else "stop",
            charges=charges, exit_time=when)
        closed.append({"symbol": trade["symbol"], "qty": trade["qty"],
                       "price": round(price, 2), "stop": stop,
                       "gapped": gapped, "when": when,
                       "r_multiple": done.get("r_multiple")})
    return {"stopped": closed, "problems": problems}


def _closed_bar_index(bars: list[dict], entry_time, now=None):
    """The most recent settled bar, never one from before the entry.

    A stop can't be hit by a bar that predates the position — checking the
    whole window would close a trade on the previous week's low the moment it
    opened.
    """
    if not bars:
        return None
    today = (now or datetime.now()).date()
    entry_day = None
    if entry_time:
        try:
            entry_day = datetime.fromisoformat(str(entry_time)[:19]).date()
        except ValueError:
            entry_day = None
    for i in range(len(bars) - 1, -1, -1):
        # UTC on purpose: candles.get already shifted these into IST, so
        # reading them back as UTC reconstructs the IST wall-clock date.
        # Localtime here would shift them a second time.
        day = datetime.fromtimestamp(bars[i]["time"], tz=timezone.utc).date()
        if day >= today:
            continue                     # today's bar is still forming
        if entry_day and day < entry_day:
            return None
        return i
    return None


def _bar_time(bar) -> str:
    return (datetime.fromtimestamp(bar["time"], tz=timezone.utc)
            .replace(tzinfo=None).isoformat(timespec="seconds"))


# ---------------------------------------------------------------- autopilot
# After the EOD scan, which is at 15:35. Five minutes is enough for it to have
# finished and not so long that a laptop closed at 16:00 misses the day.
_QUEUE_HM = (15, 40)


def auto_enabled() -> bool:
    return str(config.get("PAPER_AUTO", "0")).strip().lower() in (
        "1", "true", "yes", "on")


def auto_strategies() -> list[str]:
    """Which rules the paper trader follows.

    Defaults to whatever `signals` is configured to scan, and that default is
    load-bearing: from_signals reads the same scan the EOD push reads, so the
    trades the paper account takes and the alerts you actually receive can
    never disagree about what fired.

    Overriding PAPER_STRATEGIES breaks that on purpose — reasonable if you
    want the paper account testing `rsi_revert` while your alerts stay on
    trend rules — but it means the two are answering different questions, and
    status() says so rather than leaving you to notice.
    """
    raw = (config.get("PAPER_STRATEGIES", "signals") or "").strip()
    if not raw or raw.lower() == "signals":
        return [k for k, _p in signals.scan_config()]
    from .backtest import STRATEGIES
    return [k.strip() for k in raw.split(",")
            if k.strip() in STRATEGIES]


def _max_positions() -> int:
    try:
        return max(1, int(config.get("PAPER_MAX_POSITIONS", 8) or 8))
    except (TypeError, ValueError):
        return 8


def _auto_stop_pct() -> float | None:
    raw = config.get("PAPER_STOP_PCT", "") or ""
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_gates(now=None) -> tuple[bool, str]:
    """(block new entries?, why). Exits are never gated.

    The daily-loss guard already existed and pushed a stand-down notice; the
    autopilot ignored it and kept queueing, which made the guard advice rather
    than a limit. A paper account that doesn't honour its own risk rules is
    not modelling the system you'd trade.

    The regime gate is the roadmap's Nifty filter, now one line of the query
    language against the same benchmark `rs` ranks on. Long signals in a
    risk-off tape are where breakout systems bleed, and gating here means the
    journal can eventually say by how much.
    """
    try:
        limit = float(config.get("DAILY_LOSS_R", 2) or 2)
        g = journal.guard(daily_loss_r=limit, max_positions=_max_positions())
        if not g.get("ok", True):
            return True, (g.get("reason")
                          or f"daily loss limit ({limit:g}R) reached")
    except (Exception, SystemExit):
        pass                              # a broken guard must not block exits

    if str(config.get("PAPER_REGIME_GATE", "0")).strip() in ("1", "true",
                                                             "yes", "on"):
        risk_on, note = _regime_ok()
        if risk_on is False:
            return True, note
    return False, ""


def _regime_ok() -> tuple[bool | None, str]:
    """Is the tape above its own 200-day? None when we can't tell.

    None, not False: an unreadable benchmark must not silently look like a
    bear market and stop the account trading for a week.
    """
    from . import instruments, candles, query as Q

    try:
        inst = instruments.lookup(Q.DEFAULT_BENCHMARK)
        if not inst:
            return None, ""
        bars = candles.get(inst["instrument_token"], "day", 400)
        if len(bars) < 200:
            return None, ""
        node = Q.plan("close > sma(200)").condition
        ok = Q.evaluate(node, bars)
        return ok, ("" if ok else
                    f"{Q.DEFAULT_BENCHMARK} is below its 200-day — "
                    f"regime gate is on")
    except (Exception, SystemExit):
        return None, ""


def maybe_evening_queue(now=None, kite=None) -> bool:
    """Queue today's flips as paper orders, once, after the close.

    This is the half that was still manual, and it was manual because the stop
    is a decision. PAPER_STOP_PCT makes it a setting instead, which is the
    honest version: a self-executing trader that skips the stop isn't
    automating your process, it's automating a different one.

    Runs only the strategies in auto_strategies(), over the watchlist that
    signals.scan already uses. Guardrails apply even here — this is play
    money, but the numbers it produces are the evidence for going live, and
    evidence gathered without limits doesn't say anything about a system that
    will have them.
    """
    now = now or datetime.now()
    if not auto_enabled():
        return False
    if now.weekday() >= 5 or (now.hour, now.minute) < _QUEUE_HM:
        return False
    today = now.date().isoformat()
    if db.get_meta("paper_last_queue") == today:
        return False
    db.set_meta("paper_last_queue", today)

    start()
    stopped = check_stops(now=now)

    # Two gates the autopilot did not have, both built from parts that
    # already existed. Exits are never blocked by either: a limit that stops
    # you closing a losing position is not risk control, it's a trap.
    blocked, why = _entry_gates(now)

    room = 0 if blocked else (
        _max_positions() - len(journal.open_positions(source=SOURCE)))
    try:
        scan = signals.scan(push=False, kite=kite)
    except (Exception, SystemExit) as e:
        print(f"[paper] auto queue: scan failed: {e}")
        return False

    keep = set(auto_strategies())
    flips = [f for f in scan.get("flips", [])
             if (f.get("strategy") or f.get("tag")) in keep]
    # Exits first, then entries — an exit frees a slot, and processing
    # entries first would turn a rotation into a rejection.
    flips.sort(key=lambda f: 0 if f.get("to") not in (1, "long", True) else 1)

    allowed, refused = [], []
    for f in flips:
        if f.get("to") in (1, "long", True):
            if blocked:
                refused.append(f"{f.get('symbol')}: {why}")
                continue
            if room <= 0:
                refused.append(f"{f.get('symbol')}: at the {_max_positions()}-"
                               f"position limit")
                continue
            room -= 1
        allowed.append(f)

    out = from_signals({"flips": allowed}, stop_pct=_auto_stop_pct(),
                       kite=kite)
    out["skipped"] += refused
    out["stopped"] = stopped["stopped"]
    _push_auto(out, stopped)
    return True


def _push_auto(out: dict, stopped: dict) -> None:
    lines = []
    for s in stopped["stopped"]:
        how = "gapped through" if s["gapped"] else "stopped"
        r = s.get("r_multiple")
        lines.append(f"{how} {s['symbol']} @ {s['price']}"
                     + (f"  {r:+.2f}R" if r is not None else ""))
    for q in out["queued"]:
        lines.append(f"queued {q['kind']} {q['symbol']} ({q['tag']})")
    if not lines:
        return                            # a quiet day says nothing
    lines.append("fills at tomorrow's open")
    try:
        notify.send("\n".join(lines), title="Paper autopilot")
    except (Exception, SystemExit):
        print("[paper] autopilot ran but could not push")


def from_signals(scan: dict | None = None, stop_pct: float | None = None,
                 kite=None) -> dict:
    """Turn today's signal flips into pending orders.

    Reads the same scan the EOD push reads, so paper trading and the alerts
    you actually receive can never disagree about what fired.
    """
    scan = scan if scan is not None else signals.scan(push=False, kite=kite)
    flips = scan.get("flips", [])
    queued, skipped = [], []

    for flip in flips:
        symbol = flip.get("symbol")
        tag = flip.get("strategy") or flip.get("tag") or "signal"
        to = flip.get("to")
        price = flip.get("price") or flip.get("close")
        if symbol is None or price is None:
            skipped.append(f"{symbol or '?'}: no price on the flip")
            continue

        held = journal.open_positions(symbol, source=SOURCE)
        if to in (1, "long", True):
            if held:
                skipped.append(f"{symbol}: already holding on paper")
                continue
            stop = (price * (1 - stop_pct / 100.0)) if stop_pct else None
            oid = queue_order(symbol, "entry", tag, price, stop=stop)
            queued.append({"symbol": symbol, "kind": "entry", "tag": tag,
                           "order_id": oid})
        else:
            if not held:
                skipped.append(f"{symbol}: nothing to exit on paper")
                continue
            oid = queue_order(symbol, "exit", tag, price, reason="signal")
            queued.append({"symbol": symbol, "kind": "exit", "tag": tag,
                           "order_id": oid})

    return {"queued": queued, "skipped": skipped, "flips": len(flips)}


# ------------------------------------------------------------------ reporting
# The morning fill runs a few minutes after the open, not at 09:15 exactly.
# _next_open reads the STORED candles, so the bar has to have been written
# before this is worth attempting — and a bar requested at 09:15:00 is a
# coin flip on whether the first one exists yet.
_FILL_HM = (9, 20)


def maybe_morning_fill(now=None, kite=None) -> bool:
    """Fill last night's queue at this morning's open. Once a day, on trading
    days, from the watcher loop.

    This exists because the two halves of paper trading land at the two times
    you are least likely to be at the machine: the queue is built after the
    close, and the fill needs a bar that only exists after the open. A paper
    experiment you have to be home for is a paper experiment that quietly
    stops after a fortnight, and the month of live-vs-backtest data it was
    supposed to produce never arrives.

    Tops the candles up first: fill_pending reads stored bars and never
    fetches, so without it every order reports as pending — which reads
    exactly like a morning with nothing to do.

    Attempted at most once per day whether or not it succeeds, for the same
    reason the EOD scan is: a dead session at 09:20 must not become a retry
    storm. Anything left pending is picked up by the next run, since orders
    persist and fill_pending is safe to call repeatedly.
    """
    now = now or datetime.now()
    if now.weekday() >= 5 or (now.hour, now.minute) < _FILL_HM:
        return False
    today = now.date().isoformat()
    if db.get_meta("paper_last_fill") == today:
        return False
    if not pending():
        return False                 # nothing queued; don't burn the day's slot
    db.set_meta("paper_last_fill", today)

    try:
        from . import maintenance, watchlist, kite_auth
        k = kite or kite_auth.get_kite()
        symbols = sorted({o["symbol"] for o in pending()}
                         | {w["tradingsymbol"] for w in watchlist.items()})
        maintenance.topup_candles(k, symbols)
    except (Exception, SystemExit) as e:
        print(f"[paper] top-up before fill failed: {e}")

    try:
        # Stops before entries: a position that gapped through its stop
        # overnight must be out before a new one takes its slot, or the
        # position limit is enforced against a book that isn't real.
        stopped = check_stops(now=now)
        out = fill_pending()
        out["filled"] = ([{"kind": "stop", "symbol": s["symbol"],
                           "qty": s["qty"], "price": s["price"]}
                          for s in stopped["stopped"]] + out["filled"])
    except (Exception, SystemExit) as e:
        print(f"[paper] morning fill failed: {e}")
        return False

    if out["filled"]:
        lines = [f"{f['kind']} {f['symbol']} {f['qty']} @ {f['price']}"
                 for f in out["filled"]]
        if out["still_pending"]:
            lines.append(f"{out['still_pending']} still pending")
        try:
            notify.send("\n".join(lines), title="Paper fills")
        except (Exception, SystemExit):
            # config.require() raises SystemExit on a missing NTFY_TOPIC, and
            # a bare `except Exception` would let that kill the watcher — the
            # audit this project keeps for exactly this mistake caught it.
            print("[paper] filled but could not push")
    print(f"[paper] morning fill: {len(out['filled'])} filled, "
          f"{out['still_pending']} pending")
    return True


def state() -> dict:
    """Everything the page and the phone need in one call."""
    open_rows = journal.open_positions(source=SOURCE)
    closed = journal.closed_trades(source=SOURCE, limit=100_000)
    stats = journal.stats(trades=closed)
    return {
        "open": open_rows,
        "closed": len(closed),
        "pending": pending(),
        "stats": stats,
        "by_tag": _by_tag(closed),
        "started": db.get_meta("paper_started"),
    }


def _by_tag(closed: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for tag in sorted({t.get("tag") or "—" for t in closed}):
        rows = [t for t in closed if (t.get("tag") or "—") == tag]
        out[tag] = journal.stats(trades=rows)
    return out


def start() -> str:
    """Stamp the beginning, so 'a month of paper trading' has a start date."""
    when = db.get_meta("paper_started")
    if not when:
        when = _now()
        db.set_meta("paper_started", when)
    return when


def reset(confirm: bool = False) -> dict:
    """Wipe every paper order and paper journal row. Real trades untouched."""
    if not confirm:
        raise PaperError("reset needs confirm=True — this deletes paper history")
    with db.connect() as conn:
        orders_n = conn.execute("DELETE FROM paper_orders").rowcount
        trades_n = conn.execute("DELETE FROM trades WHERE source=?",
                                [SOURCE]).rowcount
    db.set_meta("paper_started", "")
    return {"orders": orders_n, "trades": trades_n}


def summary(st: dict | None = None) -> str:
    """CLI / phone rendering."""
    st = st or state()
    s = st["stats"]
    lines = ["Paper account"]
    if st["started"]:
        lines.append(f"  running since {st['started'][:10]}")
    if auto_enabled():
        strat = auto_strategies()
        stop = _auto_stop_pct()
        lines.append(
            f"  AUTOPILOT ON \u00b7 {', '.join(strat[:4])}"
            + (f" +{len(strat) - 4}" if len(strat) > 4 else "")
            + f" \u00b7 stop {stop:g}%" if stop else " \u00b7 no stop")
        lines.append(f"  max {_max_positions()} positions \u00b7 "
                     f"queues 15:40, fills 09:20")
        # Say it when they diverge rather than leaving you to notice: the
        # trades taken and the alerts received are then answering different
        # questions.
        if (config.get("PAPER_STRATEGIES", "signals") or "").strip().lower() \
                not in ("", "signals"):
            lines.append("  (own strategy list \u2014 not the same as your "
                         "signal alerts)")
    lines.append(f"  {len(st['open'])} open · {st['closed']} closed · "
                 f"{len(st['pending'])} awaiting the next open")
    if s.get("trades"):
        lines.append(f"  expectancy {s['expectancy_r']:+.2f}R · "
                     f"win {s['win_rate_pct']:.0f}% · "
                     f"total {s['total_r']:+.1f}R")
    else:
        lines.append("  no closed trades yet")
    for tag, ts in (st.get("by_tag") or {}).items():
        if ts.get("trades"):
            lines.append(f"    {tag:<14} {ts['trades']:>3} trades  "
                         f"{ts['expectancy_r']:+.2f}R")
    return "\n".join(lines)
