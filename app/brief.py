"""Pre-market brief — one push at 08:45, before the open.

Almost pure formatting over modules that already exist. The value isn't new
data, it's that the things you'd otherwise open five pages to check arrive in
one message while you still have time to act on them.

Ordered by what changes a decision soonest:

  1. Where you stand — open positions and live R. What you already own
     outranks anything you might buy.
  2. The tape — Nifty against its 200-DMA, and how much of your watchlist is
     above its own. A breakout system in a risk-off tape is where accounts
     bleed, and the tape is easiest to ignore when you haven't looked at it.
  3. Results due today — the one scheduled event that can gap a position.
  4. Alerts close to firing — the ones likely to matter in the next few hours.
  5. Signal flips from yesterday's close.
  6. Overnight headlines.

Everything degrades independently. A section whose data source is down is
omitted with a note; it never takes the brief with it, because a brief that
fails to arrive is worse than one missing its news line.

Not included, deliberately: gap prediction. The roadmap wanted watchlist gaps
from GIFT Nifty direction, but GIFT Nifty trades on NSE IX and isn't in the
Kite instrument dump — there's no source for it here. Rather than invent a
number, section 4 lists the alerts sitting closest to their trigger, which is
the actionable half of what a gap forecast would have told you.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import (alerts, candles, config, db, indicators, instruments, journal,
               kite_auth, notify, watchlist)

NIFTY = "NIFTY 50"
NEAR_ALERT_PCT = 2.0        # an alert within this % of the last close is "close"
MAX_LINES_PER_SECTION = 6   # a brief you scroll is a brief you skim


def _kite():
    try:
        return kite_auth.get_kite()
    except (Exception, SystemExit):
        return None


def _closes(kite, token: int, days: int = 260) -> list[dict]:
    try:
        if kite:
            candles.ensure(kite, token, "day", days)
        return candles.get(token, "day", days)
    except Exception:
        return []


# ------------------------------------------------------------------ pieces
def positions_section() -> list[str]:
    """What you already own, in R. First because it outranks any new idea."""
    opens = journal.open_positions()
    if not opens:
        return []
    kite = _kite()
    lines, total_r = [], 0.0
    for t in opens[:MAX_LINES_PER_SECTION]:
        inst = instruments.lookup(t["symbol"])
        last = None
        if inst:
            c = _closes(kite, inst["instrument_token"], 5)
            last = c[-1]["close"] if c else None
        r = journal.live_r(t, last) if last is not None else None
        if r is not None:
            total_r += r
        bit = f"  {t['symbol']} {t['side']} {t['qty']} @ {t['entry']:g}"
        if t.get("stop"):
            bit += f" · SL {t['stop']:g}"
        if r is not None:
            bit += f" · {r:+.2f}R"
        lines.append(bit)
    head = f"POSITIONS ({len(opens)})"
    if total_r:
        head += f" · {total_r:+.2f}R open"
    return [head] + lines


def regime_section() -> list[str]:
    """Nifty vs its 200-DMA, plus how much of the watchlist is above its own.

    This is 2.1's core, folded in early because the brief is where it costs
    nothing and is read at the right moment — before the first trade, not
    after it.
    """
    kite = _kite()
    inst = instruments.lookup(NIFTY)
    if not inst:
        return ["TAPE  · Nifty 50 not in the instrument index (run sync-instruments)"]
    c = _closes(kite, inst["instrument_token"], 320)
    if len(c) < 200:
        return ["TAPE  · not enough Nifty history yet"]

    closes = [b["close"] for b in c]
    sma200 = indicators.sma(closes, 200)
    last, ma = closes[-1], sma200[-1]
    if ma is None:
        return ["TAPE  · 200-DMA unavailable"]
    above = last > ma
    dist = (last - ma) / ma * 100

    # breadth: share of the watchlist trading above its own 200-DMA
    total = hits = 0
    for w in watchlist.items():
        i2 = instruments.lookup(w["tradingsymbol"])
        if not i2:
            continue
        c2 = _closes(kite, i2["instrument_token"], 320)
        if len(c2) < 200:
            continue
        cl = [b["close"] for b in c2]
        m2 = indicators.sma(cl, 200)[-1]
        if m2 is None:
            continue
        total += 1
        hits += 1 if cl[-1] > m2 else 0
    breadth = (hits / total * 100) if total else None

    if above and (breadth is None or breadth >= 60):
        verdict = "risk-on"
    elif not above and (breadth is None or breadth < 40):
        verdict = "risk-off"
    else:
        verdict = "neutral"

    line = (f"TAPE  {verdict.upper()} · Nifty {last:,.0f} "
            f"{'above' if above else 'below'} 200-DMA ({dist:+.1f}%)")
    if breadth is not None:
        line += f" · {hits}/{total} of watchlist above theirs"
    out = [line]
    if verdict == "risk-off":
        out.append("  long breakouts bleed in this tape — size down or sit out")
    return out


def results_today_section() -> list[str]:
    """Watchlist results due today — the one scheduled event that gaps you."""
    from . import results as R
    today = datetime.now().date()
    due = []
    for w in watchlist.items():
        try:
            card = R.card(w["tradingsymbol"], today=today)
        except Exception:
            continue
        if not card or card.get("released"):
            continue
        d = card.get("date")
        if not d:
            continue
        try:
            when = datetime.fromisoformat(str(d)[:10]).date()
        except ValueError:
            continue
        if when == today:
            due.append(card["symbol"])
    if not due:
        return []
    return [f"RESULTS TODAY  {', '.join(due[:10])}",
            "  a held name reporting today can gap through your stop"]


def near_alerts_section() -> list[str]:
    """Armed alerts sitting closest to their trigger — likeliest to fire."""
    rows = alerts.active()
    if not rows:
        return []
    kite = _kite()
    near, other = [], 0
    for r in rows:
        params = {}
        try:
            import json
            params = json.loads(r["params"] or "{}")
        except ValueError:
            pass
        level = params.get("x")
        tok = r.get("token")
        if level is None or not tok:
            other += 1
            continue
        c = _closes(kite, tok, 5)
        if not c:
            other += 1
            continue
        last = c[-1]["close"]
        gap = (level - last) / last * 100
        if abs(gap) <= NEAR_ALERT_PCT:
            near.append((abs(gap), f"  {alerts.label(r)} · last {last:g} "
                                   f"({gap:+.1f}% away)"))
        else:
            other += 1
    near.sort()
    out = [f"ALERTS ({len(rows)} armed)"]
    out += [t for _, t in near[:MAX_LINES_PER_SECTION]]
    if not near:
        out.append(f"  none within {NEAR_ALERT_PCT:g}% of their level")
    elif other:
        out.append(f"  + {other} further out")
    return out


def flips_section() -> list[str]:
    """Strategy stances that changed on yesterday's close."""
    cutoff = (datetime.now() - timedelta(days=4)).isoformat()
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, strategy, state, updated_at FROM signal_state "
                "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 20",
                (cutoff,)).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    out = ["SIGNALS (recent stance changes)"]
    for r in list(rows)[:MAX_LINES_PER_SECTION]:
        long_ = bool(r["state"])
        out.append(f"  {'▲' if long_ else '▽'} {r['symbol']} "
                   f"{r['strategy']} → {'LONG' if long_ else 'FLAT'}")
    return out if len(out) > 1 else []


def headlines_section(hours: int = 14) -> list[str]:
    """Overnight news on watchlist names. Last, because it's context not action."""
    try:
        from . import news
        grouped = news.collect(hours=hours)
    except Exception as e:
        return [f"NEWS  · unavailable ({e})"]
    items = [(sym, it) for sym, lst in (grouped or {}).items() for it in lst]
    if not items:
        return []
    out = ["OVERNIGHT"]
    for sym, it in items[:MAX_LINES_PER_SECTION]:
        title = (it.get("title") or "").strip()
        if len(title) > 84:
            title = title[:81] + "…"
        out.append(f"  {sym}: {title}")
    if len(items) > MAX_LINES_PER_SECTION:
        out.append(f"  + {len(items) - MAX_LINES_PER_SECTION} more — /news")
    return out


def budget_section() -> list[str]:
    """Yesterday's damage and today's remaining room."""
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    g = journal.guard(daily_loss_r=float(config.get("DAILY_LOSS_R", 2)),
                      max_positions=int(config.get("MAX_POSITIONS", 5)))
    yr = journal.realised_r_today(y)
    bits = []
    if yr:
        bits.append(f"yesterday {yr:+.2f}R")
    bits.append(f"today's budget {g['daily_loss_r']:.0f}R")
    if g["open_risk_r"]:
        bits.append(f"{g['open_risk_r']:.2f}R already live")
    acct = float(config.get("ACCOUNT_SIZE", 0) or 0)
    if acct:
        bits.append(f"1R = ₹{acct * float(config.get('RISK_PCT', 1)) / 100:,.0f}")
    return ["RISK  " + " · ".join(bits)]


# ------------------------------------------------------------------ compose
def sips_section(days: int = 5) -> list[str]:
    """SIPs debiting in the next few days.

    Here rather than anywhere else because it's the one mutual-fund fact with
    a date attached, and a debit you'd forgotten about is a debit that lands
    the morning you were planning to deploy the same cash.
    """
    from . import mf

    due = mf.due_within(days)
    if not due:
        return []
    out = ["SIPs due:"]
    for s in due:
        when = ("today" if s["days_away"] == 0
                else f"in {s['days_away']}d")
        out.append(f"  {mf._short(s['fund'], 26)} \u00b7 "
                   f"\u20b9{s['amount']:,.0f} \u00b7 {when}")
    return out


SECTIONS = (
    ("positions", positions_section),
    ("risk", budget_section),
    ("regime", regime_section),
    ("results", results_today_section),
    ("sips", sips_section),
    ("alerts", near_alerts_section),
    ("signals", flips_section),
    ("news", headlines_section),
)


def compose(skip: tuple[str, ...] = ()) -> str:
    """Build the brief. Each section is isolated: one failing source costs you
    that section and nothing else, because a brief that doesn't arrive is
    worse than one missing its news line."""
    blocks, failed = [], []
    for name, fn in SECTIONS:
        if name in skip:
            continue
        try:
            lines = fn()
        except (Exception, SystemExit) as e:
            # SystemExit as well: sections reach for a Kite session and a push
            # topic, both of which raise it via config.require when unset.
            # "Isolated" has to mean isolated from everything, or the one
            # morning your token expired is the morning the brief vanishes.
            failed.append(f"{name} ({e})")
            continue
        if lines:
            blocks.append("\n".join(lines))

    head = f"Pre-market — {datetime.now():%a %d %b}"
    if not blocks:
        return head + "\n\nNothing to report (no positions, alerts or watchlist data)."
    body = "\n\n".join(blocks)
    if failed:
        body += "\n\n(unavailable: " + ", ".join(failed) + ")"
    return f"{head}\n\n{body}"


def send(dry: bool = False) -> str:
    """Compose and push. `dry` prints without sending or marking."""
    text = compose()
    if dry:
        return text
    try:
        notify.send(text, title="Pre-market brief", priority="default",
                    tags="sunrise")
        db.set_meta("brief_last_sent", datetime.now().strftime("%Y-%m-%d"))
    except (Exception, SystemExit) as e:
        print(f"[brief] push failed: {e}")
        return f"push failed: {e}"
    return text


def maybe_send(now: datetime | None = None, at: str = "08:45") -> bool:
    """Send once per weekday at `at`. Returns True if it fired.

    Marks the day BEFORE composing, like the EOD signal scan does — composing
    hits several network sources and can be slow, and a retry storm at 08:45
    is worse than a missed brief. The failure path pushes, so a silent miss
    isn't silent.
    """
    now = now or datetime.now()
    if now.weekday() >= 5:                       # markets shut
        return False
    today = now.strftime("%Y-%m-%d")
    if db.get_meta("brief_last_sent") == today:
        return False
    hh, _, mm = at.partition(":")
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    # a window, not an instant — the caller's loop ticks every 10-30s and a
    # restart shouldn't lose the day's brief
    if not (target <= now < target + timedelta(minutes=45)):
        return False
    db.set_meta("brief_last_sent", today)
    try:
        send()
    except (Exception, SystemExit) as e:
        print(f"[brief] failed: {e}")
        try:
            notify.send(f"Pre-market brief failed: {e}",
                        title="TickerForge", priority="low", tags="warning")
        except (Exception, SystemExit):
            pass
    return True
