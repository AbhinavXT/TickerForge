"""Corporate events pipeline: NSE feeds -> watchlist filter -> dedupe -> phone.

Design notes
- NSE's JSON schemas drift; normalization uses fallback key chains and keeps
  going if a record is odd rather than crashing the poller.
- Dedupe: every pushed/seen event gets a stable id hashed from its salient
  fields, stored in SQLite. Restarting the poller never re-notifies.
- First run per feed seeds silently (marks the current backlog as seen)
  so your phone isn't blasted with two weeks of history.
"""
import hashlib
import json

from . import db, notify, watchlist
from .nse import NSEClient

FEED_LABEL = {
    "announcement": "Announcement",
    "corp_action": "Corporate action",
    "board_meeting": "Board meeting",
}
FEED_TAGS = {
    "announcement": "loudspeaker",
    "corp_action": "money_with_wings",
    "board_meeting": "calendar",
}

# Market-wide curation: inclusion-based. An event must match a category to
# survive; everything else (ESOP allotments, trading-window notices,
# newspaper publications, share-certificate paperwork...) drops silently.
CATEGORIES = ("RESULTS", "DIVIDEND", "CONCALL", "CORP ACTION")
_CATEGORY_KEYWORDS = {
    "RESULTS": ("financial result", "unaudited", "audited result",
                "results for the quarter", "outcome of board meeting"),
    "DIVIDEND": ("dividend",),
    "CONCALL": ("conference call", "earnings call", "con. call", "concall",
                "analyst", "investor meet", "investors meet",
                "institutional investor"),
    "CORP ACTION": ("bonus", "split", "sub-division", "subdivision",
                    "buyback", "buy-back", "rights issue", "record date",
                    "amalgamation", "demerger"),
}
_CATEGORY_EXCLUDE = ("newspaper", "publication", "loss of share",
                     "duplicate share", "trading window", "esop",
                     "investor complaint", "reg. 74")


def classify(ev: dict) -> str | None:
    text = f"{ev['headline']} {ev['detail']}".lower()
    if any(x in text for x in _CATEGORY_EXCLUDE):
        return None
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in text for k in kws):
            return cat
    return None


def _first(rec: dict, *keys, default=""):
    for k in keys:
        v = rec.get(k)
        if v not in (None, "", "-"):
            return str(v).strip()
    return default


def normalize(kind: str, rec: dict) -> dict | None:
    """Map a raw feed record to {symbol, headline, detail, when, url}."""
    if kind == "announcement":
        symbol = _first(rec, "symbol", "sm_symbol")
        headline = _first(rec, "desc", "subject")
        detail = _first(rec, "attchmntText", "sm_desc")[:300]
        when = _first(rec, "an_dt", "sort_date", "exchdisstime")
        url = _first(rec, "attchmntFile", default="")
    elif kind == "corp_action":
        symbol = _first(rec, "symbol")
        headline = _first(rec, "subject", "purpose")
        ex = _first(rec, "exDate", "exdate")
        detail = f"Ex-date: {ex}" if ex else ""
        when = ex or _first(rec, "recDate")
        url = ""
    else:  # board_meeting
        symbol = _first(rec, "bm_symbol", "symbol")
        headline = _first(rec, "bm_purpose", "purpose")
        detail = _first(rec, "bm_desc", "desc")[:300]
        when = _first(rec, "bm_date", "meetingDate", "date")
        url = _first(rec, "attachment", default="")
    if not symbol or not headline:
        return None
    return {"symbol": symbol.upper(), "headline": headline,
            "detail": detail, "when": when, "url": url}


def event_id(kind: str, ev: dict) -> str:
    raw = "|".join((kind, ev["symbol"], ev["headline"], ev["when"]))
    return hashlib.sha1(raw.encode()).hexdigest()


def _is_result(ev: dict) -> bool:
    """Does this filing look like a quarterly-results announcement?"""
    text = f"{ev.get('headline','')} {ev.get('detail','')}".lower()
    return any(k in text for k in
               ("financial result", "unaudited", "audited result",
                "results for the quarter", "quarterly result",
                "outcome of board meeting", "standalone", "consolidated"))


def _fmt_cr(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _result_numbers(ev: dict) -> str | None:
    """If this filing is a result and we hold the screener numbers, return a
    compact Sales / Op Profit / Net Profit block with YoY growth. Best-effort:
    returns None if it isn't a result or we have no released quarter stored."""
    if not _is_result(ev):
        return None
    try:
        from . import results
        card = results.card(ev["symbol"])
    except Exception:
        return None
    if not card or not card.get("released") or not card.get("rows"):
        return None
    q = card["quarter"]
    lines = [f"{q} (₹ Cr):"]
    short = {"Sales": "Sales", "Operating Profit": "Op Profit",
             "Net Profit": "Net Profit"}
    for r in card["rows"]:
        g = r.get("yoy_growth")
        gtxt = f"  ({g:+.1f}% YoY)" if g is not None else ""
        lines.append(f"  {short.get(r['label'], r['label'])}: "
                     f"{_fmt_cr(r.get('cur'))}{gtxt}")
    if card.get("pe") or card.get("mcap"):
        tail = []
        if card.get("mcap"):
            tail.append(f"Mcap {card['mcap']}")
        if card.get("pe"):
            tail.append(f"PE {card['pe']}")
        lines.append("  " + " · ".join(tail))
    return "\n".join(lines)


def _feed_seeded(kind: str) -> bool:
    return db.get_meta(f"events_seeded_{kind}") == "1"


def _mark_seeded(kind: str) -> None:
    db.set_meta(f"events_seeded_{kind}", "1")


def poll_once(client: NSEClient | None = None, push: bool = True) -> dict:
    """Fetch all feeds. Watchlist events -> instant per-event push.
    Market-wide events matching a category -> instant, but batched into ONE
    grouped push per poll cycle (so results season isn't 50 separate buzzes).
    Also still stored for the on-page list and any scheduled digest.
    Returns per-feed counts."""
    client = client or NSEClient()
    watched = {w["tradingsymbol"] for w in watchlist.items()}
    summary = {}
    mkt_seeding = not (db.get_meta("events_mkt_seeded") == "1")
    mkt_new = 0
    mkt_fresh: list[dict] = []          # new market-wide events to push this cycle
    for kind in FEED_LABEL:
        try:
            records = client.fetch_feed(kind)
        except Exception as e:
            summary[kind] = f"fetch failed: {e}"
            continue

        seeding = not _feed_seeded(kind)
        new = 0
        with db.connect() as conn:
            for rec in records:
                ev = normalize(kind, rec) if isinstance(rec, dict) else None
                if not ev:
                    continue
                if ev["symbol"] not in watched:
                    # ---- market-wide tier: classify or drop ----
                    cat = classify(ev)
                    if not cat:
                        continue
                    eid = event_id(kind, ev)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO seen_events "
                        "(id, source, symbol, payload, category, digested) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (eid, kind, ev["symbol"], json.dumps(ev), cat,
                         1 if mkt_seeding else 0),
                    )
                    if cur.rowcount:
                        mkt_new += 1
                        if not mkt_seeding:
                            mkt_fresh.append({**ev, "kind": kind, "cat": cat})
                    continue
                eid = event_id(kind, ev)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO seen_events "
                    "(id, source, symbol, payload, category, digested) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (eid, kind, ev["symbol"], json.dumps(ev),
                     classify(ev) or kind),
                )
                if cur.rowcount == 0:      # already seen
                    continue
                new += 1
                if push and not seeding:
                    body = ev["detail"] or ev["headline"]
                    if ev["when"]:
                        body += f"\n{ev['when']}"
                    numbers = _result_numbers(ev)     # profit table if a result
                    if numbers:
                        body += "\n\n" + numbers
                    notify.send(
                        body,
                        title=f"{ev['symbol']} — {FEED_LABEL[kind]}: {ev['headline'][:80]}",
                        priority="high" if kind == "announcement" else "default",
                        tags=FEED_TAGS[kind],
                        click_url=ev["url"] or None,
                    )
        if seeding:
            _mark_seeded(kind)
            summary[kind] = f"seeded {new} existing (no push)"
        else:
            summary[kind] = f"{new} new"

    # market-wide instant push: mark as digested (so a later digest won't repeat)
    if mkt_fresh:
        if push:
            _push_market_batch(mkt_fresh)
        ids = [event_id(e["kind"], e) for e in mkt_fresh]
        with db.connect() as conn:
            conn.executemany("UPDATE seen_events SET digested=1 WHERE id=?",
                             [(i,) for i in ids])

    if mkt_seeding:
        db.set_meta("events_mkt_seeded", "1")
        summary["market"] = f"seeded {mkt_new} (no push)"
    else:
        summary["market"] = f"{mkt_new} pushed" if mkt_fresh else "0 new"
    return summary


def _push_market_batch(events_list: list[dict]):
    """One grouped push for all new market-wide events found this cycle,
    ordered so RESULTS and DIVIDEND (with ex-dates) lead."""
    order = {c: i for i, c in enumerate(CATEGORIES)}
    events_list.sort(key=lambda e: order.get(e["cat"], 99))
    by_cat: dict[str, list[dict]] = {}
    for e in events_list:
        by_cat.setdefault(e["cat"], []).append(e)
    lines, shown = [], 0
    for cat in CATEGORIES:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"{cat} ({len(items)})")
        for ev in items[:8]:
            extra = f" · {ev['when']}" if ev.get("when") else ""
            lines.append(f"• {ev['symbol']} — {ev['headline'][:60]}{extra}")
            shown += 1
        if len(items) > 8:
            lines.append(f"  +{len(items) - 8} more")
        lines.append("")
    body = "\n".join(lines).strip()[:3800]
    total = len(events_list)
    cats = [c for c in CATEGORIES if by_cat.get(c)]
    title = f"NSE: {total} filing{'s' if total != 1 else ''} — {', '.join(cats)}"
    notify.send(body, title=title, priority="high",
                tags="newspaper", click_url=None)


# ---------------------------------------------------------------- schedule
def should_poll(now=None) -> tuple[bool, str]:
    """Market-hours-aware poll schedule (server clock = IST on the user's Mac).
    Returns (poll_now, reason). The run loop wakes often and asks this."""
    import time as _t
    lt = now or _t.localtime()
    wd, hm = lt.tm_wday, lt.tm_hour * 60 + lt.tm_min      # 0=Mon
    weekday = wd < 5
    # continuous fast polling during the trading session (09:00–16:00 IST)
    if weekday and 9 * 60 <= hm <= 16 * 60:
        return True, "market"
    # a few evening sweeps — results/board-meeting outcomes land after close
    EVENING = {16 * 60 + 30, 18 * 60, 19 * 60 + 30, 21 * 60, 22 * 60 + 30}
    # light weekend sweeps (dividends/board notices do appear on Saturdays)
    WEEKEND = {11 * 60, 18 * 60}
    slots = EVENING if weekday else WEEKEND
    if hm in slots:
        return True, "sweep"
    return False, "idle"


# ---------------------------------------------------------------- query
def fetch_for_symbol(symbol: str, client: NSEClient | None = None) -> int:
    """On-demand: pull the NSE feeds and store any events for THIS symbol,
    regardless of watchlist membership. Lets the phone query corporate actions
    / results / board meetings for any stock. Returns how many new were stored.
    """
    client = client or NSEClient()
    sym = symbol.upper()
    stored = 0
    with db.connect() as conn:
        for kind in FEED_LABEL:
            try:
                records = client.fetch_feed(kind)
            except Exception:
                continue
            for rec in records:
                ev = normalize(kind, rec) if isinstance(rec, dict) else None
                if not ev or ev["symbol"] != sym:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO seen_events "
                    "(id, source, symbol, payload, category, digested) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (event_id(kind, ev), kind, ev["symbol"], json.dumps(ev),
                     classify(ev) or kind))
                stored += cur.rowcount
    return stored


def recent(limit: int = 300, source: str | None = None,
           category: str | None = None, symbol: str | None = None) -> list[dict]:
    """Stored corporate events (watchlist + market-wide), newest first."""
    q = ("SELECT id, source, symbol, payload, category, seen_at "
         "FROM seen_events")
    conds, params = [], []
    if source:
        conds.append("source=?"); params.append(source)
    if category:
        conds.append("category=?"); params.append(category)
    if symbol:
        conds.append("symbol=?"); params.append(symbol.upper())
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY seen_at DESC LIMIT ?"
    params.append(limit)
    out = []
    with db.connect() as conn:
        for r in conn.execute(q, params).fetchall():
            try:
                ev = json.loads(r["payload"]) if r["payload"] else {}
            except (TypeError, ValueError):
                ev = {}
            out.append({
                "id": r["id"], "source": r["source"], "symbol": r["symbol"],
                "category": r["category"], "seen_at": r["seen_at"],
                "headline": ev.get("headline", ""), "detail": ev.get("detail", ""),
                "when": ev.get("when", ""), "url": ev.get("url", ""),
            })
    return out


# ---------------------------------------------------------------- digest
_MAX_PER_CATEGORY = 20
_DIGEST_BYTE_CAP = 3800

_DIGEST_PROMPT = """You curate a market-wide digest of Indian stock exchange
filings for an investor. Below are today's filings grouped by category.
Rewrite as a CONCISE plain-text digest:
- Keep the category headers.
- One line per company: "SYMBOL — essence in a few words".
- Merge duplicate filings by the same company.
- Keep dividends' amounts and ex-dates if present.
- No preamble, no markdown. Plain text only.

Filings:
{payload}"""


def _pending(conn) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT id, category, payload FROM seen_events "
        "WHERE digested=0 ORDER BY category, symbol").fetchall()
    for r in rows:
        ev = json.loads(r["payload"])
        ev["_id"] = r["id"]
        grouped.setdefault(r["category"], []).append(ev)
    return grouped


def _format_digest(grouped: dict[str, list[dict]]) -> str:
    parts = []
    for cat in CATEGORIES:
        items = grouped.get(cat, [])
        if not items:
            continue
        parts.append(f"{cat} ({len(items)})")
        for ev in items[:_MAX_PER_CATEGORY]:
            head = ev["headline"][:70]
            parts.append(f"• {ev['symbol']} — {head}")
        if len(items) > _MAX_PER_CATEGORY:
            parts.append(f"  +{len(items) - _MAX_PER_CATEGORY} more")
        parts.append("")
    return "\n".join(parts).strip()


def digest(dry: bool = False) -> str:
    """Send the curated market-wide digest of pending events."""
    from . import llm
    with db.connect() as conn:
        grouped = _pending(conn)
    if not grouped:
        return "no pending market events"
    text = _format_digest(grouped)
    n = sum(len(v) for v in grouped.values())
    curated = llm.complete(_DIGEST_PROMPT.format(payload=text))
    if curated:
        text = curated
    if len(text.encode()) > _DIGEST_BYTE_CAP:
        text = text.encode()[:_DIGEST_BYTE_CAP].decode(errors="ignore") + "\n…"
    if dry:
        print(text)
        return f"dry run — {n} events (not sent, not marked)"
    notify.send(text, title=f"Market events — {n} filings",
                tags="bank")
    with db.connect() as conn:   # mark only after successful send
        conn.executemany(
            "UPDATE seen_events SET digested=1 WHERE id=?",
            [(ev["_id"],) for items in grouped.values() for ev in items])
    return f"sent — {n} events across {len(grouped)} categories"
