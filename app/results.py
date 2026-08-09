"""Latest quarterly results — the numbers if they're out, else just the date.

For every stock you care about, this answers one question: are *this* quarter's
results released yet?

  * Released  -> a Sales / Operating Profit / Net Profit table with YoY & QoQ
                 growth and the three comparable quarters (Screener-style card,
                 the thing you screenshotted).
  * Not yet   -> just the date: the board-meeting / results date pulled from the
                 NSE corporate-events feed (or "not announced" if we don't hold
                 it yet — the `events run` poller fills these in over time).

Nothing new is fetched here; it reads what TickerForge already stores:
  * fundamentals `fund_series` (statement 'quarters') -> the released numbers
  * `fund_snapshot` (Market Cap, Stock P/E)           -> the card header
  * events.recent (board_meeting / RESULTS)            -> the awaited date

"Which quarter is current" is derived from the calendar (expected_quarter),
so this keeps working next quarter without a code change.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime

from . import db, events, instruments, watchlist

# The three lines every results card shows, in display order.
ROWS = ("Sales", "Operating Profit", "Net Profit")
# Screener sometimes labels the top line "Revenue"; try both.
_ROW_ALIASES = {"Sales": ("Sales", "Revenue")}


# ------------------------------------------------------------- quarter maths
def _qlabel(month: int, year: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def expected_quarter(today: date | None = None) -> tuple[str, str, str]:
    """Return (current, previous, year-ago) quarter labels for `today`.

    Screener labels a quarter by the month it ENDS (Apr-Jun -> 'Jun 2026').
    We report on the most recently ended quarter, so as of mid-Jul 2026 the
    current quarter is 'Jun 2026', previous 'Mar 2026', year-ago 'Jun 2025'.
    """
    today = today or date.today()
    m, y = today.month, today.year
    if m <= 3:      em, ey = 12, y - 1
    elif m <= 6:    em, ey = 3, y
    elif m <= 9:    em, ey = 6, y
    else:           em, ey = 9, y
    pm, py = (em - 3, ey) if em > 3 else (12, ey - 1)   # quarter before
    return _qlabel(em, ey), _qlabel(pm, py), _qlabel(em, ey - 1)


def growth(cur: float | None, base: float | None) -> float | None:
    """Percentage change, sign-correct even when the base is negative.

    (cur - base) / |base|: a swing from -100 to +50 reads as +150%, and a
    profit turning from positive to negative reads negative, as you'd expect.
    """
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, 2)


# ------------------------------------------------------------- data access
def _quarters(conn, symbol: str) -> dict[str, dict[str, float | None]]:
    """{display_row_label: {period_label: value}} for the 3 lines we show."""
    out: dict[str, dict[str, float | None]] = {}
    for disp in ROWS:
        wants = _ROW_ALIASES.get(disp, (disp,))
        got: dict[str, float | None] = {}
        for label in wants:
            rows = conn.execute(
                "SELECT period, value FROM fund_series "
                "WHERE symbol=? AND statement='quarters' AND lower(row)=? "
                "ORDER BY period_idx", (symbol, label.lower())).fetchall()
            if rows:
                got = {r["period"]: r["value"] for r in rows}
                break
        out[disp] = got
    return out


def _snapshot(conn, symbol: str) -> dict:
    rows = conn.execute(
        "SELECT key, value, fetched_at FROM fund_snapshot WHERE symbol=?",
        (symbol,)).fetchall()
    snap = {r["key"]: r["value"] for r in rows}
    return {
        "mcap": snap.get("Market Cap"),
        "pe": snap.get("Stock P/E"),
        "price": snap.get("Current Price"),
        "fetched_at": rows[0]["fetched_at"] if rows else None,
    }


def _result_date(symbol: str) -> str | None:
    """Best-effort results/board-meeting date for a symbol, from the NSE feed.

    Prefers a board-meeting date, then any RESULTS-category filing. Returns the
    raw `when` string (NSE formats vary) or None if we haven't seen one.
    """
    rows = events.recent(limit=30, symbol=symbol.upper())
    if not rows:
        return None
    def _rank(ev):                       # board meetings first, then results
        src = ev.get("source")
        if src == "board_meeting":
            return 0
        if ev.get("category") == "RESULTS":
            return 1
        return 2
    for ev in sorted(rows, key=_rank):
        if _rank(ev) <= 1 and ev.get("when"):
            return ev["when"]
    return None


def _last_reported(quarters: dict[str, dict]) -> str | None:
    """Newest period label that actually has a Sales value (for context)."""
    sales = quarters.get("Sales") or {}
    # fund_series is inserted in period order; dict preserves it.
    last = None
    for period, val in sales.items():
        if val is not None:
            last = period
    return last


# ------------------------------------------------------------- the card
def card(symbol: str, today: date | None = None) -> dict | None:
    """One results card. None if we hold no fundamentals for the symbol at all.

    released == True  -> `rows` holds the Sales/OP/NP table + YoY/QoQ growth.
    released == False -> `date` holds the awaited date (or None) and
                          `last_reported` the newest quarter we do have.
    """
    symbol = symbol.upper()
    cur, prev, yoy = expected_quarter(today)
    inst = instruments.lookup(symbol)
    name = (inst or {}).get("name") or symbol

    with db.connect() as conn:
        quarters = _quarters(conn, symbol)
        snap = _snapshot(conn, symbol)

    has_any = any(quarters[r] for r in ROWS)
    if not has_any and not snap["fetched_at"]:
        return None                      # nothing stored for this stock

    released = any((quarters[r].get(cur) is not None) for r in ROWS)

    base = {
        "symbol": symbol, "name": name,
        "released": released,
        "quarter": cur, "prev_quarter": prev, "yoy_quarter": yoy,
        "mcap": snap["mcap"], "pe": snap["pe"], "price": snap["price"],
        "fetched_at": snap["fetched_at"],
        "stale": _is_stale(snap["fetched_at"]),
    }

    if released:
        rows = []
        for disp in ROWS:
            series = quarters[disp]
            c, p, y = series.get(cur), series.get(prev), series.get(yoy)
            rows.append({
                "label": disp,
                "cur": c, "prev": p, "yoy": y,
                "yoy_growth": growth(c, y),
                "qoq_growth": growth(c, p),
            })
        base["rows"] = rows
        base["date"] = _result_date(symbol)         # announced date, if known
    else:
        base["rows"] = []
        base["last_reported"] = _last_reported(quarters)
        base["date"] = _result_date(symbol)         # awaited date, if known
    return base


def _is_stale(fetched_at: str | None, days: int = 7) -> bool:
    if not fetched_at:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(fetched_at)).days > days
    except ValueError:
        return False


# ------------------------------------------------------------- many cards
def _sort_cards(cards: list[dict], sort: str) -> list[dict]:
    """Screenshot's sort options. 'latest' = released first; the YoY sorts
    rank released cards by that line's YoY growth, unreleased sink to the end."""
    key_row = {"sales": "Sales", "op": "Operating Profit",
               "np": "Net Profit"}.get(sort)
    if not key_row:                       # "latest" / default
        cards.sort(key=lambda c: (0 if c["released"] else 1, c["symbol"]))
        return cards

    def g(c):
        for r in c.get("rows", []):
            if r["label"] == key_row and r["yoy_growth"] is not None:
                return r["yoy_growth"]
        return None
    cards.sort(key=lambda c: (0 if g(c) is not None else 1, -(g(c) or 0)))
    return cards


def cards(symbols: list[str] | None = None, sort: str = "latest",
          scope: str = "watch", today: date | None = None) -> list[dict]:
    """Result cards for a set of stocks.

    symbols  explicit list (overrides scope)
    scope    'watch'  -> your watchlist (default; "stocks I want to see")
             'stored' -> every symbol we hold fundamentals for
    sort     latest | sales | op | np
    """
    include_missing = True
    if symbols:
        syms = [s.upper() for s in symbols]
    elif scope == "stored":
        include_missing = False          # only stocks we actually hold
        with db.connect() as conn:
            syms = [r["symbol"] for r in conn.execute(
                "SELECT DISTINCT symbol FROM fund_snapshot ORDER BY symbol")]
    else:
        syms = [w["tradingsymbol"] for w in watchlist.items()]

    cur, _, _ = expected_quarter(today)
    out = []
    for s in syms:
        c = card(s, today)
        if c:
            out.append(c)
        elif include_missing:            # watched, but no fundamentals stored yet
            inst = instruments.lookup(s)
            out.append({
                "symbol": s, "name": (inst or {}).get("name") or s,
                "released": False, "missing": True, "quarter": cur,
                "rows": [], "mcap": None, "pe": None, "date": None,
                "last_reported": None, "stale": True,
            })
    return _sort_cards(out, sort)


# ------------------------------------------------------------- phone text
def _grp(n: float | None) -> str:
    """Whole-number ₹Cr with thousands grouping, like the screenshot."""
    return "—" if n is None else f"{n:,.0f}"


def _pct(p: float | None) -> str:
    return "—" if p is None else f"{p:+.1f}%"


def as_text(c: dict) -> str:
    """Render a card for a phone/ntfy reply (plain text, no markdown)."""
    if not c:
        return "No data."
    head = c["name"] if c["name"] != c["symbol"] else c["symbol"]
    if c.get("missing"):
        return (f"{head} — no fundamentals stored yet.\n"
                f"Fetch them first: /fund {c['symbol']} isn't cached "
                f"(run `fund fetch {c['symbol']}` on the box).")
    tags = []
    if c.get("mcap"):
        tags.append(f"M.Cap {c['mcap'].strip()}")
    if c.get("pe"):
        tags.append(f"PE {c['pe']}")
    meta = "  ·  ".join(tags)

    if not c["released"]:
        lines = [f"{head} — {c['quarter']} results not out yet"]
        if c.get("date"):
            lines.append(f"Expected: {c['date']}")
        else:
            lines.append("Date not announced yet")
        if c.get("last_reported"):
            lines.append(f"Last reported: {c['last_reported']}")
        if meta:
            lines.append(meta)
        return "\n".join(lines)

    lines = [f"{head} — {c['quarter']} ✓"]
    if meta:
        lines.append(meta)
    lines.append(f"{'':12}{'YoY':>8}{'QoQ':>8}{c['quarter'].split()[0]:>9}")
    short = {"Sales": "Sales", "Operating Profit": "Op Profit",
             "Net Profit": "Net Profit"}
    for r in c["rows"]:
        lines.append(f"{short[r['label']]:<11}{_pct(r['yoy_growth']):>8}"
                     f"{_pct(r['qoq_growth']):>8}{_grp(r['cur']):>9}")
    if c.get("stale"):
        lines.append("(data may be stale — refresh fundamentals)")
    return "\n".join(lines)


def overview_text(sort: str = "latest") -> str:
    """One-line-per-stock watchlist overview for a bare `/results`."""
    cs = cards(sort=sort)
    if not cs:
        return ("Your watchlist is empty. Add stocks, or send /results SYM.")
    done = [c for c in cs if c["released"]]
    wait = [c for c in cs if not c["released"]]
    lines = [f"Watchlist results — {len(done)}/{len(cs)} reported "
             f"({cs[0]['quarter']})"]
    for c in done:
        np = next((r["yoy_growth"] for r in c["rows"]
                   if r["label"] == "Net Profit"), None)
        lines.append(f"✓ {c['symbol']:<11} NP {_pct(np)} YoY")
    for c in wait:
        if c.get("missing"):
            tail = "no data"
        elif c.get("date"):
            tail = f"due {c['date']}"
        else:
            tail = "awaiting"
        lines.append(f"⏳ {c['symbol']:<11} {tail}")
    return "\n".join(lines)
