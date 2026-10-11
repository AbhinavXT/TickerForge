"""Delivery volume — how much of a day's volume actually changed hands.

Kite's candles carry total volume, which mixes intraday churn (bought and sold
the same session, no position left behind) with delivery (shares that moved
into someone's demat). NSE publishes the split every evening in the full
bhavcopy, `sec_bhavdata_full_DDMMYYYY.csv`, and that's the only source here.

The signal people want from it is accumulation: price up, volume above
average, AND an unusually large share of that volume taken into delivery.
Rising price on rising intraday churn is speculation; rising price on rising
delivery is somebody building a position.

Two numbers, because each misleads alone:

  * `deliv_pct` — delivery as a % of the day's volume. A large-cap that always
    runs 60% delivery isn't accumulating at 62%.
  * `deliv_ratio` — today's delivery QUANTITY against its own N-day average.
    This is the spike. It's measured on quantity rather than on the %, so a
    quiet day with 80% delivery of almost nothing doesn't read as one.

Data lands after the close — NSE posts the file somewhere between 18:00 and
20:00 — so these are always the last COMPLETED session's numbers. The Board
labels them that way during market hours.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from . import db

URL = ("https://nsearchives.nseindia.com/products/content/"
       "sec_bhavdata_full_{d:%d%m%Y}.csv")
WINDOW = 20                 # days in the "average" a spike is measured against
SPIKE_RATIO = 2.0           # delivery qty this many × average is a spike
BACKFILL_DAYS = 45          # calendar days — enough for a 20-session average
PUBLISH_AFTER = (18, 30)    # don't ask before this; the file isn't there yet
RETRY_MINUTES = 30


# ------------------------------------------------------------------ parsing
def _f(v):
    v = (v or "").strip()
    if v in ("", "-"):
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def parse(text: str) -> tuple[str | None, list[dict]]:
    """Bhavcopy CSV -> (session day, EQ rows). Pure, for the tests.

    NSE pads every header and value with a leading space (`SYMBOL, SERIES,
    DATE1, ...`), so keys and values are both stripped.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip().upper() for h in next(reader)]
    except StopIteration:
        return None, []
    ix = {h: i for i, h in enumerate(header)}
    need = ("SYMBOL", "SERIES", "DATE1", "CLOSE_PRICE", "TTL_TRD_QNTY",
            "DELIV_QTY", "DELIV_PER")
    if any(k not in ix for k in need):
        raise ValueError(f"bhavcopy header changed: {header[:16]}")

    day, rows = None, []
    for rec in reader:
        if len(rec) < len(header):
            continue
        if rec[ix["SERIES"]].strip().upper() != "EQ":
            continue
        if day is None:
            try:
                day = datetime.strptime(rec[ix["DATE1"]].strip(),
                                        "%d-%b-%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
        vol = _f(rec[ix["TTL_TRD_QNTY"]])
        dq = _f(rec[ix["DELIV_QTY"]])
        dp = _f(rec[ix["DELIV_PER"]])
        if vol is None or dq is None:
            continue
        rows.append({"symbol": rec[ix["SYMBOL"]].strip().upper(),
                     "close": _f(rec[ix["CLOSE_PRICE"]]),
                     "volume": int(vol), "deliv_qty": int(dq),
                     "deliv_pct": dp})
    return day, rows


# ------------------------------------------------------------------- store
def store(day: str, rows: list[dict]) -> int:
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO delivery "
            "(symbol, day, close, volume, deliv_qty, deliv_pct) "
            "VALUES (?,?,?,?,?,?)",
            [(r["symbol"], day, r["close"], r["volume"], r["deliv_qty"],
              r["deliv_pct"]) for r in rows])
    return len(rows)


def stored_days() -> set[str]:
    with db.connect() as conn:
        return {r["day"] for r in
                conn.execute("SELECT DISTINCT day FROM delivery").fetchall()}


def last_day() -> str | None:
    with db.connect() as conn:
        r = conn.execute("SELECT MAX(day) d FROM delivery").fetchone()
    return r["d"] if r else None


# ------------------------------------------------------------------- fetch
def fetch_day(d: date, client=None) -> int | None:
    """Fetch and store one session. None when NSE has no file (holiday, or
    not published yet); otherwise the number of rows stored."""
    from .nse import NSEClient
    client = client or NSEClient()
    text = client.get_text(URL.format(d=d))
    if not text or not text.strip():
        return None
    day, rows = parse(text)
    if not rows:
        return None
    return store(day or d.isoformat(), rows)


def sync(days: int = BACKFILL_DAYS, client=None, today: date | None = None) -> dict:
    """Fill every missing weekday in the last `days` calendar days.

    Weekends are skipped without asking. Holidays cost one 404 each, and are
    remembered in `meta` so the next sync doesn't ask again.
    """
    from .nse import NSEClient
    client = client or NSEClient()
    today = today or date.today()
    have = stored_days()
    holidays = set((db.get_meta("delivery_holidays") or "").split(","))
    got, missing, errors = 0, [], []
    for back in range(days, -1, -1):
        d = today - timedelta(days=back)
        iso = d.isoformat()
        if d.weekday() >= 5 or iso in have or iso in holidays:
            continue
        try:
            n = fetch_day(d, client)
        except Exception as e:                          # noqa: BLE001
            errors.append(f"{iso}: {e}")
            continue
        if n is None:
            # Today's file is often just not out yet — only past days are
            # holidays.
            if d < today:
                holidays.add(iso)
            missing.append(iso)
        else:
            got += 1
    db.set_meta("delivery_holidays",
                ",".join(sorted(h for h in holidays if h)[-60:]))
    return {"fetched": got, "no_file": missing, "errors": errors,
            "last_day": last_day()}


def maybe_sync(now: datetime | None = None, client=None) -> str | None:
    """The evening job. Weekdays after 18:30, every 30 min until today's file
    lands. Returns a line to log when it did something, else None."""
    now = now or datetime.now()
    if now.weekday() >= 5 or (now.hour, now.minute) < PUBLISH_AFTER:
        return None
    today = now.date().isoformat()
    if last_day() == today:
        return None
    tried = db.get_meta("delivery_tried_at")
    if tried:
        try:
            if now - datetime.fromisoformat(tried) < timedelta(minutes=RETRY_MINUTES):
                return None
        except ValueError:
            pass
    db.set_meta("delivery_tried_at", now.isoformat(timespec="seconds"))
    res = sync(client=client, today=now.date())
    if res["last_day"] == today:
        return f"delivery: {today} stored ({res['fetched']} session(s) fetched)"
    return None


# ------------------------------------------------------------------- reads
def _summarise(rows: list[dict], window: int) -> dict | None:
    """rows newest first -> the numbers. Pure."""
    if not rows:
        return None
    cur, prior = rows[0], rows[1:window + 1]
    qtys = [r["deliv_qty"] for r in prior if r["deliv_qty"] is not None]
    pcts = [r["deliv_pct"] for r in prior if r["deliv_pct"] is not None]
    avg_q = sum(qtys) / len(qtys) if qtys else None
    # The average excludes today, so a spike isn't diluted by itself.
    ratio = (round(cur["deliv_qty"] / avg_q, 2)
             if avg_q and cur["deliv_qty"] is not None else None)
    chg = None
    if len(rows) > 1 and rows[1]["close"] and cur["close"]:
        chg = round((cur["close"] / rows[1]["close"] - 1) * 100, 2)
    return {
        "day": cur["day"],
        "close": cur["close"],
        "change_pct": chg,
        "volume": cur["volume"],
        "deliv_qty": cur["deliv_qty"],
        "deliv_pct": cur["deliv_pct"],
        "avg_deliv_qty": round(avg_q) if avg_q else None,
        "avg_deliv_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "deliv_ratio": ratio,
        "sessions": len(prior),
        "spike": bool(ratio and ratio >= SPIKE_RATIO),
    }


def stats(symbol: str, window: int = WINDOW, day: str | None = None) -> dict | None:
    """One symbol's latest session (or `day`), against its own average."""
    q = ("SELECT * FROM delivery WHERE symbol=? "
         + ("AND day<=? " if day else "") + "ORDER BY day DESC LIMIT ?")
    args = [symbol.upper()] + ([day] if day else []) + [window + 1]
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    return _summarise(rows, window)


def many(symbols, window: int = WINDOW) -> dict[str, dict]:
    """Latest stats for many symbols in one read — the query engine and the
    Board call this per run, not per symbol."""
    syms = sorted({s.upper() for s in symbols})
    if not syms:
        return {}
    with db.connect() as conn:
        days = [r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM delivery ORDER BY day DESC LIMIT ?",
            (window + 1,)).fetchall()]
        if not days:
            return {}
        out: dict[str, list] = {}
        for i in range(0, len(syms), 500):
            chunk = syms[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT * FROM delivery WHERE day >= ? AND symbol IN ({marks}) "
                    f"ORDER BY symbol, day DESC", [days[-1], *chunk]).fetchall():
                out.setdefault(r["symbol"], []).append(dict(r))
    latest = days[0]
    res = {}
    for sym, rows in out.items():
        # A symbol missing from the latest file (suspended, moved to BE) must
        # not report a stale day as today's.
        if rows[0]["day"] != latest:
            continue
        s = _summarise(rows, window)
        if s:
            res[sym] = s
    return res


def spikes(symbols=None, ratio: float = SPIKE_RATIO, up_only: bool = True,
           limit: int = 30) -> list[dict]:
    """Latest session's delivery spikes, biggest first. With `symbols=None`
    it ranks everything in the file."""
    if symbols is None:
        with db.connect() as conn:
            d = last_day()
            symbols = [r["symbol"] for r in conn.execute(
                "SELECT symbol FROM delivery WHERE day=?", (d,)).fetchall()] if d else []
    rows = []
    for sym, s in many(symbols).items():
        if not s["deliv_ratio"] or s["deliv_ratio"] < ratio:
            continue
        if up_only and not (s["change_pct"] and s["change_pct"] > 0):
            continue
        rows.append({"symbol": sym, **s})
    rows.sort(key=lambda r: -r["deliv_ratio"])
    return rows[:limit]


def as_text(symbol: str) -> str:
    s = stats(symbol)
    if not s:
        return (f"No delivery data for {symbol.upper()}. "
                f"Run `python -m app.cli delivery sync`.")
    lines = [f"{symbol.upper()} delivery · {s['day']}",
             f"  {s['deliv_pct']:.1f}% of {s['volume']:,} delivered "
             f"({s['deliv_qty']:,} sh)"]
    if s["avg_deliv_pct"] is not None:
        lines.append(f"  {s['sessions']}-day avg {s['avg_deliv_pct']:.1f}% · "
                     f"{s['avg_deliv_qty']:,} sh")
    if s["deliv_ratio"] is not None:
        tag = "  ← spike" if s["spike"] else ""
        lines.append(f"  today {s['deliv_ratio']:.2f}× average delivery{tag}")
    if s["change_pct"] is not None:
        lines.append(f"  close {s['close']:,.2f} ({s['change_pct']:+.2f}%)")
    return "\n".join(lines)
