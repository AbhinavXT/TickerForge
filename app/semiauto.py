"""Semi-automatic execution: the signal proposes, you approve, the box places.

This is the first module in the project that spends money. Everything in it is
arranged around that fact, and the arrangement is deliberately inconvenient in
places where convenience would be dangerous.

Two switches, both off
----------------------
`LIVE_TRADING` and `SEMIAUTO` must BOTH be set. Two flags rather than one
because a single switch is something you flip while debugging and forget; two
is a decision. Nothing here places an order unless both are on, and the
refusal says which is missing.

Why a token, and why it expires
-------------------------------
Approval arrives over a public ntfy topic. A message on a topic can be
observed and replayed by anyone who has ever seen one — the review of the
command channel noted exactly this gap, and for a `/quote` command it's
harmless. For an order it is not. So each proposal carries a **single-use**
token: the first approval consumes it, and a replay finds nothing to approve.

The token also **expires** (default 15 minutes). An approval tapped the next
morning would execute against a setup that stopped existing overnight — the
price has moved, the stop is meaningless, and the trade you agreed to is not
the trade you would get.

The guardrails run TWICE
------------------------
Once when proposing, and again at the moment of approval. This is the single
most important line in the file. Between the push and your tap, minutes pass:
you may have opened another position, hit the daily loss limit, or the market
may have closed. Checking only at proposal time would let a stale approval
walk straight through limits that were breached after it was sent.

The stop goes on with the entry
-------------------------------
A filled entry with no stop attached is the failure mode that empties
accounts. The GTT is placed immediately after the entry, and if it fails the
push says so LOUDLY rather than quietly leaving you naked — this module would
rather wake you up than let you believe you are protected.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from . import config, db, journal, notify, risk

TOKEN_TTL_MIN = 15
TOKEN_BYTES = 8                  # ~13 chars of base32; typable, not guessable

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 20)          # stop proposing near the close


class SemiAutoError(RuntimeError):
    pass


class Blocked(SemiAutoError):
    """A guardrail said no. Not an error in the code — the system working."""


# ------------------------------------------------------------------ switches
def enabled() -> tuple[bool, str]:
    """Both flags, or nothing happens. Returns (ok, why not)."""
    if str(config.get("LIVE_TRADING", "")).lower() not in ("1", "true", "yes"):
        return False, "LIVE_TRADING is not enabled"
    if str(config.get("SEMIAUTO", "")).lower() not in ("1", "true", "yes"):
        return False, "SEMIAUTO is not enabled"
    return True, ""


def _now() -> datetime:
    return datetime.now()


def market_open(now: datetime | None = None) -> bool:
    now = now or _now()
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return MARKET_OPEN <= hm <= MARKET_CLOSE


# ---------------------------------------------------------------- guardrails
def check(symbol: str, qty: int, price: float, stop: float | None,
          now: datetime | None = None) -> list[str]:
    """Every reason this order should not be placed. Empty list means proceed.

    Returns ALL failures rather than the first, because a proposal blocked for
    three reasons and one reason look identical if you only report one, and
    the difference matters when you're deciding whether to override.
    """
    problems: list[str] = []
    ok, why = enabled()
    if not ok:
        problems.append(why)

    if not market_open(now):
        problems.append("market is closed")

    if stop is None:
        # Non-negotiable. An entry without a stop has no defined risk, cannot
        # be sized honestly, and cannot be scored afterwards.
        problems.append("no stop — refusing to place an unbounded trade")
    elif stop >= price:
        problems.append("stop is at or above the entry price")

    if qty < 1:
        problems.append("quantity is zero")

    account = float(config.get("ACCOUNT_SIZE", 0) or 0)
    if account:
        value = qty * price
        cap = float(config.get("MAX_POSITION_PCT", 100) or 100)
        if value > account * cap / 100.0:
            problems.append(
                f"position is {value / account * 100:.0f}% of capital, "
                f"over the {cap:.0f}% cap")

    g = journal.guard(
        daily_loss_r=float(config.get("DAILY_LOSS_R", 2) or 2),
        max_positions=int(float(config.get("MAX_POSITIONS", 5) or 5)))
    if not g["ok"]:
        problems.append(g.get("reason") or "daily risk guard says stand down")

    if journal.open_positions(symbol):
        problems.append(f"already holding {symbol.upper()}")

    return problems


# ----------------------------------------------------------------- proposing
def propose(symbol: str, qty: int, price: float, stop: float,
            tag: str = "signal", side: str = "buy",
            target: float | None = None, reason: str = "",
            push: bool = True, now: datetime | None = None) -> dict:
    """Create a proposal and push it for approval. Places nothing.

    `now` is threaded through rather than read here so the whole module runs
    off ONE clock — the expiry written at proposal time and the expiry checked
    at approval time must be measured against the same one, or a token is
    valid for a window nobody chose.
    """
    symbol = symbol.upper()
    now = now or _now()
    problems = check(symbol, qty, price, stop, now)
    token = secrets.token_hex(TOKEN_BYTES)
    status = "blocked" if problems else "pending"

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO order_proposals (token, symbol, side, qty, ref_price,"
            " stop, target, tag, reason, status, created_at, expires_at, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [token, symbol, side, qty, price, stop, target, tag, reason,
             status, now.isoformat(timespec="seconds"),
             (now + timedelta(minutes=TOKEN_TTL_MIN)).isoformat(
                 timespec="seconds"),
             "; ".join(problems) or None])

    out = {"token": token, "symbol": symbol, "qty": qty, "price": price,
           "stop": stop, "status": status, "problems": problems,
           "expires_in_min": TOKEN_TTL_MIN}

    if problems:
        # A blocked proposal is still worth telling you about: silence would
        # look identical to no signal, and "the system stopped me" is
        # information.
        if push:
            _push(f"Signal on {symbol} NOT proposed\n" +
                  "\n".join(f"• {p}" for p in problems), tags="no_entry")
        return out

    if push:
        _push_proposal(out, tag)
    return out


def _risk_line(qty: int, price: float, stop: float) -> str:
    r = abs(price - stop) * qty
    account = float(config.get("ACCOUNT_SIZE", 0) or 0)
    pct = f" ({r / account * 100:.2f}% of capital)" if account else ""
    return f"risk ₹{r:,.0f}{pct} · 1R = ₹{abs(price - stop):.2f}/sh"


def _push_proposal(p: dict, tag: str) -> None:
    body = (f"{p['symbol']} — buy {p['qty']} @ ~{p['price']:.2f}\n"
            f"stop {p['stop']:.2f} · {tag}\n"
            f"{_risk_line(p['qty'], p['price'], p['stop'])}\n\n"
            f"Approve within {TOKEN_TTL_MIN} min:\n"
            f"  /approve {p['token']} pin=YOUR_PIN\n"
            f"  /reject {p['token']}")
    actions = None
    base = config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        # Only offered when the box is reachable from the phone. The command
        # path always works and needs no inbound access.
        actions = (f"http, Approve, {base}/api/semiauto/approve"
                   f"?token={p['token']}, method=POST")
    _push(body, title="Order awaiting approval", priority="high",
          tags="warning", actions=actions)


def _push(message: str, title: str = "TickerForge", priority: str = "default",
          tags: str | None = None, actions: str | None = None) -> None:
    try:
        notify.send(message, title=title, priority=priority, tags=tags,
                    actions=actions)
    except (Exception, SystemExit) as e:                  # noqa: BLE001
        # Never let a failed push abort an order decision in either direction.
        print(f"[semiauto] could not push: {e}")


# ----------------------------------------------------------------- approving
def get(token: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM order_proposals WHERE token=?",
                           [token]).fetchone()
    return dict(row) if row else None


def pending(now: datetime | None = None) -> list[dict]:
    expire_stale(now)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM order_proposals WHERE status='pending' "
            "ORDER BY id DESC").fetchall()]


def expire_stale(now: datetime | None = None) -> int:
    now = (now or _now()).isoformat(timespec="seconds")
    with db.connect() as conn:
        return conn.execute(
            "UPDATE order_proposals SET status='expired', decided_at=? "
            "WHERE status='pending' AND expires_at < ?", [now, now]).rowcount


def reject(token: str, why: str = "rejected") -> dict:
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE order_proposals SET status='rejected', decided_at=?, "
            "note=? WHERE token=? AND status='pending'",
            [_now().isoformat(timespec="seconds"), why, token]).rowcount
    if not n:
        raise SemiAutoError("no pending proposal with that token")
    return {"ok": True, "token": token, "status": "rejected"}


def _consume(token: str, now: datetime) -> dict:
    """Claim the token atomically. Single-use is enforced HERE.

    The UPDATE is the lock: it matches only rows still pending, so a replayed
    approval — or two taps racing — finds nothing to claim and the second one
    fails rather than placing a second order.
    """
    stamp = now.isoformat(timespec="seconds")
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE order_proposals SET status='approved', decided_at=? "
            "WHERE token=? AND status='pending' AND expires_at >= ?",
            [stamp, token, stamp]).rowcount
    if not n:
        row = get(token)
        if row is None:
            raise SemiAutoError("unknown token")
        if row["status"] == "approved" or row["status"] == "placed":
            raise SemiAutoError("that proposal was already approved")
        if row["status"] == "expired" or row["expires_at"] < stamp:
            raise SemiAutoError("that proposal has expired — signal again")
        raise SemiAutoError(f"proposal is {row['status']}")
    return get(token)


def approve(token: str, broker=None, now: datetime | None = None) -> dict:
    """Claim the token, RE-CHECK the guardrails, then place.

    The re-check is the point. Minutes passed between the push and this call,
    and the world moved: another position opened, the loss limit hit, the
    session closed. A proposal that was safe when sent is not necessarily safe
    when tapped.
    """
    now = now or _now()
    p = _consume(token, now)

    problems = check(p["symbol"], p["qty"], p["ref_price"], p["stop"], now)
    if problems:
        _finish(token, "blocked", note="; ".join(problems))
        _push(f"{p['symbol']} approval REFUSED at execution:\n" +
              "\n".join(f"• {x}" for x in problems), priority="high",
              tags="no_entry")
        raise Blocked("; ".join(problems))

    broker = broker or _kite()
    try:
        order_id = broker.place_order(
            variety="regular", exchange="NSE", tradingsymbol=p["symbol"],
            transaction_type="BUY" if p["side"] == "buy" else "SELL",
            quantity=p["qty"], product="CNC", order_type="MARKET")
    except Exception as e:                                # noqa: BLE001
        _finish(token, "failed", note=str(e)[:200])
        _push(f"{p['symbol']} order FAILED: {e}", priority="high",
              tags="rotating_light")
        raise SemiAutoError(f"broker rejected the order: {e}") from None

    # --- the stop, immediately. A filled entry with no stop attached is the
    # failure mode that empties accounts.
    gtt_id, stop_note = None, ""
    try:
        gtt_id = _place_stop(broker, p)
    except Exception as e:                                # noqa: BLE001
        stop_note = f"STOP NOT PLACED: {e}"

    trade_id = journal.open_trade(
        p["symbol"], entry=p["ref_price"], qty=p["qty"], stop=p["stop"],
        tag=p["tag"], source="semiauto",
        notes=f"order {order_id}" + (f"; gtt {gtt_id}" if gtt_id else ""))

    _finish(token, "placed", order_id=str(order_id),
            gtt_id=str(gtt_id) if gtt_id else None, trade_id=trade_id,
            note=stop_note or None)

    if stop_note:
        _push(f"⚠ {p['symbol']} entry placed ({order_id}) but the STOP DID "
              f"NOT GO ON.\n{stop_note}\nYou are unprotected — set it "
              f"manually now.", priority="max", tags="rotating_light")
    else:
        _push(f"{p['symbol']} bought {p['qty']} @ ~{p['ref_price']:.2f}\n"
              f"stop {p['stop']:.2f} attached (GTT {gtt_id})\n"
              f"{_risk_line(p['qty'], p['ref_price'], p['stop'])}",
              title="Order placed", tags="white_check_mark")

    return {"ok": True, "token": token, "order_id": order_id,
            "gtt_id": gtt_id, "trade_id": trade_id,
            "stop_attached": gtt_id is not None, "warning": stop_note}


def _place_stop(broker, p: dict):
    """A GTT sell that triggers at the stop. Kite's own bracket for CNC."""
    return broker.place_gtt(
        trigger_type="single", tradingsymbol=p["symbol"], exchange="NSE",
        trigger_values=[p["stop"]], last_price=p["ref_price"],
        orders=[{"transaction_type": "SELL", "quantity": p["qty"],
                 "order_type": "LIMIT", "product": "CNC",
                 # A limit a touch below the trigger, not a market order: a
                 # market sell on a gap can fill far below anything sensible.
                 "price": round(p["stop"] * 0.995, 1)}])


def _finish(token: str, status: str, **fields) -> None:
    sets = ["status=?", "decided_at=?"]
    args: list = [status, _now().isoformat(timespec="seconds")]
    for key in ("order_id", "gtt_id", "trade_id", "note"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key}=?")
            args.append(fields[key])
    args.append(token)
    with db.connect() as conn:
        conn.execute(f"UPDATE order_proposals SET {', '.join(sets)} "
                     f"WHERE token=?", args)


def _kite():
    from . import kite_auth
    return kite_auth.get_kite()


# ------------------------------------------------------------------ from signals
def propose_from_signal(symbol: str, price: float, tag: str,
                        stop_pct: float | None = None,
                        push: bool = True) -> dict:
    """Size a flip with the real risk model and propose it."""
    account = float(config.get("ACCOUNT_SIZE", 0) or 0)
    pct = float(config.get("RISK_PCT", 1) or 1)
    if not account:
        raise SemiAutoError("ACCOUNT_SIZE is not set — cannot size an order")
    stop_pct = stop_pct if stop_pct is not None else 4.0
    stop = round(price * (1 - stop_pct / 100.0), 1)
    sized = risk.size(entry=price, stop=stop, account=account, risk_pct=pct)
    if sized["qty"] < 1:
        raise SemiAutoError("sized to zero shares")
    return propose(symbol, sized["qty"], price, stop, tag=tag, push=push)


# ---------------------------------------------------------------- reporting
def recent(limit: int = 30) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM order_proposals ORDER BY id DESC LIMIT ?",
            [limit]).fetchall()]


def summary() -> str:
    ok, why = enabled()
    lines = ["Semi-auto execution",
             f"  {'ARMED' if ok else 'off'}" + (f" — {why}" if not ok else "")]
    p = pending()
    lines.append(f"  {len(p)} awaiting approval")
    for row in p:
        lines.append(f"    {row['symbol']:<12} {row['qty']:>5} @ "
                     f"{row['ref_price']:.2f}  stop {row['stop']:.2f}  "
                     f"expires {row['expires_at'][11:16]}")
    return "\n".join(lines)
