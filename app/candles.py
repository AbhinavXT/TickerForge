"""Candle storage (DuckDB) + incremental backfill from Kite historical API.

Strategy: on each request we top up the store from the last stored candle to
now, chunked to Kite's per-request date-range caps and throttled under the
3 req/sec historical rate limit. First load of a symbol/interval backfills
the default window; later loads are near-instant.
"""
import time
from datetime import datetime, timedelta, timezone

import duckdb

from . import config

DB = config.DATA_DIR / "candles.duckdb"

# Kite's max date-range per historical request, by interval.
INTERVAL_MAX_DAYS = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}
# Default lookback fetched the first time a symbol/interval is opened.
DEFAULT_DAYS = {
    "minute": 7, "5minute": 30, "15minute": 90,
    "60minute": 365, "day": 365 * 5,
}
IST_OFFSET = 19800  # lightweight-charts renders unix time as UTC; shift so it reads as IST.

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    token    INTEGER NOT NULL,
    interval VARCHAR NOT NULL,
    ts       TIMESTAMP NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candles ON candles (token, interval, ts);

-- How far back we have already ASKED Kite for, per series. Distinct from the
-- oldest candle we hold: a stock listed in 2021 will never return 2019 data,
-- and without this watermark every 5-year request would re-fetch that empty
-- window forever, burning a rate-limited call on each chart load.
CREATE TABLE IF NOT EXISTS coverage (
    token    INTEGER NOT NULL,
    interval VARCHAR NOT NULL,
    earliest TIMESTAMP NOT NULL,
    PRIMARY KEY (token, interval)
);
"""


LOCK_RETRIES = 6
LOCK_BACKOFF = 0.25          # seconds; doubles, capped at 2s


def _conn(read_only: bool = False):
    """Open the candle store, waiting out another process's lock.

    DuckDB allows exactly ONE process to hold this file. A writer excludes
    everyone, and even a read-only connection is refused while a writer is
    open — I checked, it isn't a reader/writer split like SQLite's WAL. Worse,
    it doesn't queue: it raises immediately.

    Four processes share this store. Immediate failure turns ordinary overlap
    — the chart topping up while the watcher reads — into a crash, and inside
    `signals.scan`, which catches per-symbol errors, into a symbol silently
    missing from the scan. That's the failure mode worth avoiding: a scan that
    looks complete and isn't.

    So do the waiting DuckDB won't. Callers that only read should pass
    read_only=True, which lets concurrent readers share the file when no
    writer is active.
    """
    if read_only and not DB.exists():
        _conn().close()                      # create file + schema once
    delay, last_err = LOCK_BACKOFF, None
    for _ in range(LOCK_RETRIES):
        try:
            c = duckdb.connect(str(DB), read_only=read_only)
            if not read_only:
                c.execute(SCHEMA)
            return c
        except duckdb.IOException as e:
            if "lock" not in str(e).lower():
                raise                        # a real IO problem, not contention
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    raise last_err


def _session_floor(now: datetime) -> datetime:
    """Midnight of the day in progress.

    Any stored bar at or after this may have been written while it was still
    forming — a daily bar fetched at 11:00 carries an 11:00 close, and the
    unique index means `INSERT OR IGNORE` would preserve that snapshot for
    ever. Everything from here forward is therefore treated as provisional and
    re-read, which costs nothing: the forward top-up was already making that
    request.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _fetch_chunks(kite, token: int, interval: str, start: datetime, end: datetime):
    """Yield historical candles between start..end honoring range caps + rate limit."""
    step = timedelta(days=INTERVAL_MAX_DAYS.get(interval, 60))
    frm = start
    while frm < end:
        to = min(frm + step, end)
        rows = kite.historical_data(token, frm, to, interval)
        for r in rows:
            yield r
        frm = to + timedelta(seconds=1)
        time.sleep(0.35)  # stay under 3 req/sec


def _asked_from(c, token: int, interval: str):
    """Oldest start date we have previously requested for this series."""
    row = c.execute(
        "SELECT earliest FROM coverage WHERE token=? AND interval=?",
        [token, interval],
    ).fetchone()
    return row[0] if row else None


def _mark_asked(c, token: int, interval: str, frm: datetime) -> None:
    prev = _asked_from(c, token, interval)
    if prev is not None and prev <= frm:
        return
    c.execute("DELETE FROM coverage WHERE token=? AND interval=?", [token, interval])
    c.execute("INSERT INTO coverage VALUES (?, ?, ?)", [token, interval, frm])


def ensure(kite, token: int, interval: str, days: int | None = None) -> None:
    """Make the local store cover the last `days` days — forwards AND backwards.

    Topping up forwards is the common case (yesterday's close arrived). But the
    store may have been seeded by a *shorter* request — a 200-day backtest, a
    30-day chart — and a later, longer request must not silently run on
    whatever happens to already be there. So we fill both ends: back to the
    requested start, and forward to now.

    The backward fetch is guarded by the `coverage` watermark, so history the
    exchange doesn't have (pre-listing) is asked for once, not on every call.
    """
    days = days or DEFAULT_DAYS.get(interval, 365)
    now = datetime.now()
    want_from = now - timedelta(days=days)

    # Phase 1 — read what we hold, then LET GO. The fetch below is network
    # I/O with rate-limit sleeps in it; holding the write lock across that is
    # what turns a 300ms overlap into a multi-second one for everybody else.
    with _conn(read_only=True) as c:
        # `last` deliberately ignores the session in progress: those bars may
        # have been stored mid-formation, so the newest one we can trust is
        # the last settled one. Phase 3 re-reads today from there.
        first, last = c.execute(
            "SELECT min(ts), max(CASE WHEN ts < ? THEN ts END) FROM candles "
            "WHERE token=? AND interval=?",
            [_session_floor(now), token, interval],
        ).fetchone()
        asked = _asked_from(c, token, interval)

    ranges: list[tuple[datetime, datetime]] = []
    if last is None:
        ranges.append((want_from, now))              # nothing settled stored
    else:
        # how far back we've already been: data held, or a window we asked
        # for and got nothing from, whichever reaches further back
        floor_ = min(first, asked) if asked is not None else first
        if want_from < floor_:
            ranges.append((want_from, floor_ - timedelta(seconds=1)))
        if last < now:
            ranges.append((last + timedelta(seconds=1), now))

    # Phase 2 — the slow part, with no lock held.
    batches = []
    for frm, to in ranges:
        if frm >= to:
            continue
        batches.append([
            (token, interval, r["date"].replace(tzinfo=None),
             r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0)
            for r in _fetch_chunks(kite, token, interval, frm, to)
        ])

    # Phase 3 — write, briefly. The delete and the insert happen together so
    # the forming bar is only ever missing for the length of this block.
    with _conn() as c:
        c.execute(
            "DELETE FROM candles WHERE token=? AND interval=? AND ts >= ?",
            [token, interval, _session_floor(now)],
        )
        for batch in batches:
            if batch:
                c.executemany(
                    "INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
        _mark_asked(c, token, interval, want_from)


def get(token: int, interval: str, days: int) -> list[dict]:
    """Read candles for the chart, shifted to display IST correctly."""
    since = datetime.now() - timedelta(days=days)
    with _conn(read_only=True) as c:
        rows = c.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE token=? AND interval=? AND ts >= ? ORDER BY ts",
            [token, interval, since],
        ).fetchall()
    return [
        {
            "time": int(ts.timestamp()) + IST_OFFSET,
            "open": o, "high": h, "low": l, "close": cl, "volume": v,
        }
        for ts, o, h, l, cl, v in rows
    ]


IST = timezone(timedelta(seconds=IST_OFFSET))


def series_utc(token: int, interval: str, days: int) -> list[dict]:
    """Candles with true UTC epoch seconds — for anything leaving this machine.

    get() is built for the local chart and is wrong for a wire format twice
    over: it adds IST_OFFSET so lightweight-charts renders UTC as IST, and it
    calls .timestamp() on a naive value, which Python resolves in whatever zone
    the box happens to be set to. Locally the two cancel out and nobody notices.

    Stored timestamps are IST wall clock — Kite returns tz-aware datetimes and
    ensure() strips the tzinfo on insert — so the fix is to say so: attach IST
    explicitly and the epoch comes out right on any machine, in any zone, with
    no display shift riding along.
    """
    since = datetime.now() - timedelta(days=days)
    with _conn(read_only=True) as c:
        rows = c.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE token=? AND interval=? AND ts >= ? ORDER BY ts",
            [token, interval, since],
        ).fetchall()
    return [
        {
            "time": int(ts.replace(tzinfo=IST).timestamp()),
            "open": o, "high": h, "low": l, "close": cl, "volume": v,
        }
        for ts, o, h, l, cl, v in rows
    ]
