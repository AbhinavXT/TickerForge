"""Insider trades — what promoters and directors do with their own shares.

SEBI's insider-trading rules (PIT) make promoters, directors and key managers
disclose their dealings within two trading days, and NSE republishes the
disclosures as JSON. Two kinds of disclosure matter here:

  * **Open-market buys and sells.** A promoter buying on market with their own
    money is one of the few signals where the person knows more than the
    market and is paying to act on it. Sells are noisier — tax, diversifying,
    a house — so they're reported but not dramatised.
  * **Pledges.** Promoter shares pledged for a loan are shares a lender can
    sell. Creation means more of the float can be dumped on a margin call;
    invocation means the lender is already doing it. Revocation is the good
    news. (Pledges file under the takeover code, Reg 31; they reach the PIT
    feed when the pledgor is a designated person, which promoters are.)

Pushes follow the events module's shape:

  * Names you follow (watchlist, open trades, holdings): each promoter or
    director disclosure is its own push, promptly.
  * Everything else: only open-market promoter/director BUYS worth at least
    INSIDER_MIN_VALUE, and pledge invocations, batched into one push per poll.
  * The first poll seeds silently, so turning this on doesn't deliver a
    fortnight of history to your phone.

NSE's field names drift, so normalisation uses fallback key chains and a
record it can't read is skipped rather than fatal.
"""
from __future__ import annotations

import hashlib
import time as _time
from datetime import date, datetime, timedelta

from . import config, db, notify

FEED = ("https://www.nseindia.com/api/corporates-pit?index=equities"
        "&from_date={frm:%d-%m-%Y}&to_date={to:%d-%m-%Y}")
FEED_SYMBOL = FEED + "&symbol={sym}"
LOOKBACK_DAYS = 7
MIN_VALUE_DEFAULT = 1e7          # ₹1 Cr — market-wide buys below this are noise
POLL_EVERY_MIN = 15
FALLBACK_EVERY_MIN = 60     # per-symbol fallback when the market-wide feed is empty
FALLBACK_MAX = 60

KINDS = ("buy", "sell", "pledge", "revoke", "invoke")
_KIND_WORD = {"buy": "bought", "sell": "sold", "pledge": "pledged",
              "revoke": "released pledge on", "invoke": "pledge INVOKED on"}


# --------------------------------------------------------------- normalise
def _first(rec: dict, *keys, default=None):
    for k in keys:
        v = rec.get(k)
        if v not in (None, "", "-", "NIL", "Nil"):
            return v
    return default


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _category(text: str) -> str:
    t = (text or "").lower()
    if "promoter" in t:
        return "promoter"
    if "director" in t:
        return "director"
    if "key managerial" in t or "kmp" in t:
        return "kmp"
    return "other"


def _kind(txn: str, mode: str) -> str:
    """Pledge words first: a pledge invocation is also, technically, a sale,
    and the sale is the less useful thing to call it."""
    t = f"{txn} {mode}".lower()
    if "invoc" in t or "invok" in t:
        return "invoke"
    if "revo" in t or "release" in t:
        return "revoke"
    if "pledge" in t:
        return "pledge"
    if any(w in t for w in ("buy", "purchase", "acqui")):
        return "buy"
    if any(w in t for w in ("sell", "sale", "dispos")):
        return "sell"
    return "other"


def _day(v) -> str | None:
    """'12-Oct-2026', '12-10-2026 18:31', '2026-10-12' -> '2026-10-12'."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y",
                "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10]


def normalize(rec: dict) -> dict | None:
    sym = _first(rec, "symbol", "sm_symbol")
    if not sym:
        return None
    txn = str(_first(rec, "tdpTransactionType", "transactionType", default=""))
    mode = str(_first(rec, "acqMode", "modeOfAcq", default=""))
    ev = {
        "symbol": str(sym).strip().upper(),
        "person": str(_first(rec, "acqName", "personName", default="")).strip(),
        "category": _category(str(_first(rec, "personCategory", "category",
                                          default=""))),
        "kind": _kind(txn, mode),
        "mode": mode.strip() or txn.strip(),
        "qty": _num(_first(rec, "secAcq", "noOfShareAcq", "buyQuantity",
                           "sellquantity")),
        "value": _num(_first(rec, "secVal", "buyValue", "sellValue")),
        "before_pct": _num(_first(rec, "befAcqSharesPer")),
        "after_pct": _num(_first(rec, "afterAcqSharesPer")),
        "trade_from": _day(_first(rec, "acqfromDt", "acqFromDt")),
        "trade_to": _day(_first(rec, "acqtoDt", "acqToDt")),
        "disclosed": _day(_first(rec, "intimDt", "date", "broadcastDate")),
    }
    return ev


def event_id(ev: dict) -> str:
    raw = "|".join(str(ev.get(k) or "") for k in
                   ("symbol", "person", "kind", "qty", "trade_from",
                    "trade_to", "disclosed"))
    return hashlib.sha1(raw.encode()).hexdigest()


def is_market(ev: dict) -> bool:
    return "market" in (ev.get("mode") or "").lower() and \
        "off" not in (ev.get("mode") or "").lower()


def is_insider(ev: dict) -> bool:
    return ev["category"] in ("promoter", "director", "kmp")


def _min_value() -> float:
    try:
        return float(config.get("INSIDER_MIN_VALUE", MIN_VALUE_DEFAULT)
                     or MIN_VALUE_DEFAULT)
    except (TypeError, ValueError):
        return MIN_VALUE_DEFAULT


def worth_pushing(ev: dict, followed: bool) -> bool:
    """The push rule, pure so it can be tested."""
    if not is_insider(ev) or ev["kind"] not in KINDS:
        return False
    if followed:
        return True
    if ev["kind"] == "invoke":
        return True
    return (ev["kind"] == "buy" and is_market(ev)
            and (ev["value"] or 0) >= _min_value())


# ------------------------------------------------------------------- format
def _money(v) -> str:
    if v is None:
        return "—"
    if v >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:,.1f} L"
    return f"₹{v:,.0f}"


def line(ev: dict) -> str:
    who = ev.get("person") or ev["category"]
    if len(who) > 28:
        who = who[:27] + "…"
    bits = [f"{ev['symbol']} — {who} ({ev['category']}) "
            f"{_KIND_WORD.get(ev['kind'], ev['kind'])}"]
    if ev.get("qty"):
        bits.append(f"{ev['qty']:,.0f} sh")
    if ev.get("value"):
        bits.append(_money(ev["value"]))
    if ev.get("mode") and ev["kind"] in ("buy", "sell"):
        bits.append(ev["mode"].lower())
    if ev.get("before_pct") is not None and ev.get("after_pct") is not None:
        bits.append(f"{ev['before_pct']:g}% → {ev['after_pct']:g}%")
    return " · ".join(bits)


# --------------------------------------------------------------------- poll
def _fetch(client, frm: date, to: date, symbol: str | None = None) -> list[dict]:
    url = (FEED_SYMBOL.format(frm=frm, to=to, sym=symbol.upper()) if symbol
           else FEED.format(frm=frm, to=to))
    data = client.get_json(url)
    if isinstance(data, dict):
        data = data.get("data") or []
    return data if isinstance(data, list) else []


def _store(conn, ev: dict, pushed: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO insider_trades (id, symbol, person, category, "
        "kind, mode, qty, value, before_pct, after_pct, trade_from, trade_to, "
        "disclosed, pushed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id(ev), ev["symbol"], ev["person"], ev["category"], ev["kind"],
         ev["mode"], ev["qty"], ev["value"], ev["before_pct"], ev["after_pct"],
         ev["trade_from"], ev["trade_to"], ev["disclosed"], pushed))
    return bool(cur.rowcount)


def poll_once(client=None, push: bool = True, today: date | None = None,
              followed: dict | None = None) -> dict:
    from .nse import NSEClient
    client = client or NSEClient()
    today = today or date.today()
    frm = today - timedelta(days=LOOKBACK_DAYS)
    records = _fetch(client, frm, today)
    if followed is None:
        from . import portfolio
        followed = portfolio.followed()
    fallback = False
    if not records and followed and _fallback_due():
        # The market-wide query has been seen returning nothing while the
        # per-symbol one works. Names you follow are the ones that must not
        # go quiet, so ask for them one at a time — hourly, capped, gently.
        fallback = True
        db.set_meta("insider_fallback_at", datetime.now().isoformat(timespec="seconds"))
        for sym in sorted(followed)[:FALLBACK_MAX]:
            try:
                records += _fetch(client, frm, today, sym)
            except Exception:                            # noqa: BLE001
                continue
            _time.sleep(0.3)
    seeding = db.get_meta("insider_seeded") != "1"

    new, mine, market = 0, [], []
    with db.connect() as conn:
        for rec in records:
            ev = normalize(rec) if isinstance(rec, dict) else None
            if not ev:
                continue
            is_mine = ev["symbol"] in followed
            want = (not seeding) and worth_pushing(ev, is_mine)
            if not _store(conn, ev, 1 if want else 0):
                continue
            new += 1
            if want:
                (mine if is_mine else market).append(ev)

    if seeding:
        db.set_meta("insider_seeded", "1")
        return {"new": new, "pushed": 0, "note": "seeded (no push)",
                "fallback": fallback}
    if push:
        for ev in mine:
            why = ", ".join(sorted(followed.get(ev["symbol"], ())))
            urgent = ev["kind"] in ("invoke", "pledge") or (
                ev["kind"] == "sell" and "position" in followed.get(ev["symbol"], ()))
            _send(line(ev) + (f"\n({why})" if why else ""),
                  title=f"{ev['symbol']} insider {ev['kind']}",
                  priority="high" if urgent else "default",
                  tags="rotating_light" if ev["kind"] == "invoke" else "bust_in_silhouette")
        if market:
            body = "\n".join("• " + line(e) for e in market[:15])
            if len(market) > 15:
                body += f"\n+{len(market) - 15} more — /insider"
            _send(body, title=f"Insider buys & pledge invocations ({len(market)})",
                  priority="default", tags="bust_in_silhouette")
    return {"new": new, "pushed": len(mine) + (1 if market else 0),
            "fallback": fallback}


def _fallback_due(now: datetime | None = None) -> bool:
    last = db.get_meta("insider_fallback_at")
    if not last:
        return True
    try:
        return (now or datetime.now()) - datetime.fromisoformat(last) >= \
            timedelta(minutes=FALLBACK_EVERY_MIN)
    except ValueError:
        return True


def _send(body: str, **kw) -> None:
    try:
        notify.send(body, **kw)
    except (Exception, SystemExit) as e:                 # noqa: BLE001
        print(f"[insider] push failed: {e}")


_last_poll = 0.0


def maybe_poll(now: datetime | None = None, client=None) -> dict | None:
    """Every 15 minutes, 08:00–22:00 on weekdays and twice on weekends.
    Disclosures land through the evening, well after the close."""
    import time as _t
    global _last_poll
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() < 5:
        if not (8 * 60 <= hm <= 22 * 60):
            return None
        if _t.time() - _last_poll < POLL_EVERY_MIN * 60:
            return None
    elif hm not in (11 * 60, 18 * 60):
        return None
    elif _t.time() - _last_poll < 120:
        return None
    _last_poll = _t.time()
    return poll_once(client)


def fetch_symbol(symbol: str, days: int = 365, client=None) -> int:
    """On demand for one name, watchlist or not. Stored without pushing."""
    from .nse import NSEClient
    client = client or NSEClient()
    today = date.today()
    n = 0
    with db.connect() as conn:
        for rec in _fetch(client, today - timedelta(days=days), today, symbol):
            ev = normalize(rec) if isinstance(rec, dict) else None
            if ev and ev["symbol"] == symbol.upper():
                n += _store(conn, ev, 0)
    return n


# -------------------------------------------------------------------- reads
def recent(symbol: str | None = None, days: int = 30,
           kinds: tuple[str, ...] | None = KINDS, insiders_only: bool = True,
           symbols=None, limit: int = 500) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    q = ("SELECT * FROM insider_trades WHERE COALESCE(disclosed, trade_to, "
         "substr(seen_at,1,10)) >= ?")
    args: list = [since]
    if symbol:
        q += " AND symbol=?"
        args.append(symbol.upper())
    if symbols is not None:
        syms = sorted({s.upper() for s in symbols})
        if not syms:
            return []
        q += f" AND symbol IN ({','.join('?' * len(syms))})"
        args += syms
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if insiders_only:
        q += " AND category IN ('promoter','director','kmp')"
    q += " ORDER BY COALESCE(disclosed, trade_to) DESC, seen_at DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def net(symbol: str, days: int = 90) -> dict:
    """Open-market promoter/director buying minus selling, in rupees and
    shares. The single number a checklist or a page wants."""
    rows = [r for r in recent(symbol, days=days, kinds=("buy", "sell"))
            if is_market(r)]
    val = sum((r["value"] or 0) * (1 if r["kind"] == "buy" else -1) for r in rows)
    qty = sum((r["qty"] or 0) * (1 if r["kind"] == "buy" else -1) for r in rows)
    pledges = recent(symbol, days=days, kinds=("pledge", "invoke"))
    return {"value": val, "qty": qty, "trades": len(rows),
            "sold": any(r["kind"] == "sell" for r in rows),
            "pledged": any(r["kind"] == "pledge" for r in pledges),
            "invoked": any(r["kind"] == "invoke" for r in pledges)}


def as_text(symbol: str, days: int = 180, fetch: bool = True) -> str:
    sym = symbol.upper()
    if fetch:
        try:
            fetch_symbol(sym, days=days)
        except Exception as e:                           # noqa: BLE001
            note = f"(NSE fetch failed: {e} — showing what's stored)\n"
        else:
            note = ""
    else:
        note = ""
    rows = recent(sym, days=days)
    if not rows:
        return note + f"{sym}: no promoter/director trades or pledges in {days} days."
    n = net(sym, days=days)
    head = f"{sym} insiders · last {days}d"
    if n["trades"]:
        head += (f" · net on-market {'+' if n['value'] >= 0 else '−'}"
                 f"{_money(abs(n['value']))}")
    out = [note + head]
    for r in rows[:10]:
        out.append(f"  {r['disclosed'] or r['trade_to'] or ''}  "
                   + line(r).split(" — ", 1)[1])
    if len(rows) > 10:
        out.append(f"  + {len(rows) - 10} more")
    return "\n".join(out)
