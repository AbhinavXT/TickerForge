"""SQLite storage. One file DB for app state (instruments index, watchlist).

Candle/tick data will live in DuckDB/Parquet from Phase 1 onward — this DB is
deliberately only for small relational state.
"""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token INTEGER PRIMARY KEY,
    exchange_token   INTEGER,
    tradingsymbol    TEXT NOT NULL,
    name             TEXT,
    exchange         TEXT NOT NULL,
    segment          TEXT,
    instrument_type  TEXT,
    lot_size         INTEGER,
    tick_size        REAL
);
CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments (tradingsymbol);
CREATE INDEX IF NOT EXISTS idx_instruments_name   ON instruments (name);

-- The Board (app/board.py): the last built board, so a restart outside
-- market hours still serves the previous session; and each future's
-- previous-session open interest, fetched once a day.
CREATE TABLE IF NOT EXISTS board_snapshot (
    id       INTEGER PRIMARY KEY,
    built_at TEXT,
    data     TEXT
);
CREATE TABLE IF NOT EXISTS board_oi_prev (
    token INTEGER NOT NULL,
    day   TEXT NOT NULL,
    oi    REAL,
    PRIMARY KEY (token, day)
);
CREATE TABLE IF NOT EXISTS watchlist (
    tradingsymbol TEXT NOT NULL,
    exchange      TEXT NOT NULL,
    name          TEXT,
    added_at      TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (tradingsymbol, exchange)
);

CREATE TABLE IF NOT EXISTS seen_events (
    id      TEXT PRIMARY KEY,
    source  TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    payload TEXT,
    seen_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS seen_news (
    id      TEXT PRIMARY KEY,
    symbol  TEXT NOT NULL,
    title   TEXT,
    link    TEXT,
    seen_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS fund_snapshot (
    symbol     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    variant    TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, key)
);

CREATE TABLE IF NOT EXISTS fund_series (
    symbol     TEXT NOT NULL,
    statement  TEXT NOT NULL,
    row        TEXT NOT NULL,
    period_idx INTEGER NOT NULL,
    period     TEXT,
    value      REAL,
    PRIMARY KEY (symbol, statement, row, period_idx)
);

CREATE TABLE IF NOT EXISTS mf_scheme_holdings (
    scheme      TEXT NOT NULL,   -- the fund, as you name it locally
    as_of       TEXT NOT NULL,   -- the disclosure month, YYYY-MM
    isin        TEXT,            -- the join key that actually works
    name        TEXT NOT NULL,
    symbol      TEXT,            -- NSE symbol, once resolved
    pct_to_nav  REAL,            -- weight within the fund
    market_value REAL,
    quantity    REAL,
    PRIMARY KEY (scheme, as_of, name)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    token        INTEGER,
    kind         TEXT NOT NULL,
    params       TEXT,
    state        TEXT,
    active       INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now', 'localtime')),
    triggered_at TEXT,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active, symbol);

-- Trade journal. Column names deliberately mirror the backtester's trade dict
-- (entry/exit/entry_time/exit_time/reason/net_pct) so a backtest trade list
-- can be imported here without a translation layer, and so the live-vs-
-- backtest comparison compares like with like.
--
-- `tag` is the join key for that comparison: use the strategy key from
-- STRATEGIES (e.g. 'st_adx') when a trade came from a system, or any label of
-- your own for discretionary ones.
--
-- Costs are stored, not assumed. The backtester charges cost_bps on both legs;
-- if the journal recorded gross R while the backtest recorded net, live would
-- look systematically better than backtested and the whole comparison would
-- mislead. `charges` holds actual brokerage+STT+slippage when known.
--
-- Two stop columns, deliberately. You want to trail `stop` as a trade works,
-- and you want to SEE where it currently sits — but R must always be measured
-- against the risk taken at entry. Re-basing R onto a trailed stop turns every
-- winner into a bigger multiple of a smaller risk and makes expectancy
-- meaningless. `initial_stop` is written once and never updated.
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL DEFAULT 'long',   -- long | short
    qty         INTEGER NOT NULL,
    entry       REAL    NOT NULL,
    stop        REAL,                              -- CURRENT stop; trail freely
    initial_stop REAL,                             -- set once; defines 1R forever
    target      REAL,
    entry_time  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    exit        REAL,
    exit_time   TEXT,
    reason      TEXT,                              -- stop | target | signal | manual
    charges     REAL    DEFAULT 0,                 -- actual costs, both legs
    r_multiple  REAL,                              -- computed on close, net
    tag         TEXT,                              -- strategy key or own label
    notes       TEXT,
    source      TEXT    DEFAULT 'manual',          -- manual | alert | paper | import
    created_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades (exit_time, symbol);
CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades (tag, exit_time);

-- Saved chart layouts: which indicators are on, their parameters and style
-- overrides, which event markers are enabled, and the interval. In SQLite
-- rather than localStorage for the same reason the roadmap gives for drawing
-- tools — a layout you spent time building shouldn't vanish because you opened
-- a different browser.
CREATE TABLE IF NOT EXISTS chart_layouts (
    name       TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,          -- JSON {indicators:[], events:[], interval}
    is_default INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- Scanner universes. The watchlist is what you follow; a universe is what you
-- SEARCH — deliberately separate, because the whole point of scanning is to
-- surface names you aren't already watching. A symbol can be in both.
-- Saved queries, optionally watched.
--
-- `last_hits` is the previous result set, and the diff against it is the whole
-- feature: a query that pushed its entire result every evening would be
-- ignored within a week. What's worth knowing is what just ENTERED.
CREATE TABLE IF NOT EXISTS saved_queries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    expression TEXT NOT NULL,
    universe   TEXT,
    alert      INTEGER NOT NULL DEFAULT 0,
    last_hits  TEXT,
    last_run   TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- Semi-automatic execution: orders PROPOSED to you, awaiting approval.
--
-- Each row carries a single-use token with an expiry. That is not ceremony:
-- the approval arrives over a public ntfy topic, and a message on a topic can
-- be replayed by anyone who has ever seen one. Without single-use tokens, an
-- attacker — or a phone that retried a request — could place the same order
-- repeatedly. Without expiry, an approval tapped tomorrow would execute
-- against a setup that stopped existing last night.
CREATE TABLE IF NOT EXISTS order_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,          -- buy | sell
    qty         INTEGER NOT NULL,
    ref_price   REAL,                   -- price the proposal was reasoned from
    stop        REAL,
    target      REAL,
    tag         TEXT,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
                -- pending|approved|placed|rejected|expired|failed|blocked
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    decided_at  TEXT,
    order_id    TEXT,                   -- broker's id, once placed
    gtt_id      TEXT,                   -- the attached stop
    trade_id    INTEGER,                -- journal row
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON order_proposals(status);

-- Paper broker: virtual orders awaiting a fill.
--
-- Persisted rather than held in memory because an order lives ACROSS a
-- restart by design: a signal computed on tonight's close fills at tomorrow's
-- open, and the gap between those two events is exactly when the watcher gets
-- restarted, the Mac reboots, or the token expires. An in-memory queue would
-- silently drop precisely the trades the experiment exists to measure, and it
-- would drop them without leaving a trace that anything was lost.
CREATE TABLE IF NOT EXISTS paper_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- entry | exit
    tag          TEXT,                   -- strategy that asked for it
    qty          INTEGER,
    stop         REAL,
    reason       TEXT,                   -- signal | stop | target | manual
    signal_time  TEXT NOT NULL,          -- when the flip was seen
    signal_price REAL,                   -- the close it was decided on
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending|filled|cancelled
    fill_time    TEXT,
    fill_price   REAL,
    trade_id     INTEGER,                -- journal row, once filled
    note         TEXT,
    created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_orders(status);

-- Chart drawings: trendlines, rays, rectangles, Fibonacci retracements.
-- Anchored in DATA space (unix time + price), never pixels, so a drawing keeps
-- meaning across zoom levels, window sizes and screens. Storing coordinates
-- would make every line a lie the moment the chart resized.
--
-- Keyed by symbol AND interval: a trendline drawn across daily bars says
-- nothing on a 5-minute chart, and showing it there would be actively
-- misleading rather than merely untidy.
--
-- SQLite rather than localStorage for the reason the roadmap gives — work you
-- spent time on shouldn't vanish because you opened a different browser.
CREATE TABLE IF NOT EXISTS drawings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- trendline|ray|hline|rect|fib
    points     TEXT NOT NULL,          -- JSON [{t: unix, p: price}, ...]
    style      TEXT,                   -- JSON {color, width, dash}
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_drawings_sym
    ON drawings(symbol, interval);

CREATE TABLE IF NOT EXISTS universe (
    name          TEXT NOT NULL,          -- e.g. 'nifty200'
    tradingsymbol TEXT NOT NULL,
    exchange      TEXT NOT NULL DEFAULT 'NSE',
    token         INTEGER,
    added_at      TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (name, tradingsymbol)
);
CREATE INDEX IF NOT EXISTS idx_universe ON universe (name);

-- Saved scans: a named set of conditions you re-run. Stored as JSON rather
-- than columns because the condition list is open-ended and gains members
-- faster than a schema should change.
CREATE TABLE IF NOT EXISTS saved_scans (
    name       TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,             -- JSON: {universe, conditions, ...}
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    last_run   TEXT,
    last_hits  INTEGER
);

-- Delivery volume from NSE's daily bhavcopy (app/delivery.py). EQ series
-- only: BE/BZ trade-to-trade series are 100% delivery by rule, which would
-- make every one of them look like accumulation.
CREATE TABLE IF NOT EXISTS delivery (
    symbol    TEXT NOT NULL,
    day       TEXT NOT NULL,          -- YYYY-MM-DD, the trading session
    close     REAL,
    volume    INTEGER,
    deliv_qty INTEGER,
    deliv_pct REAL,
    PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS idx_delivery_day ON delivery (day);

CREATE TABLE IF NOT EXISTS signal_state (
    symbol     TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    state      INTEGER,
    bar_time   INTEGER,
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (symbol, strategy)
);
"""


@contextmanager
def connect():
    """A short-lived connection, tuned for the fact that four processes write
    to this file (server, events, watcher, remote listener).

    Two pragmas do the work:

      * WAL — readers no longer block the writer and vice versa. Under the
        default rollback journal, the server rendering a page can lock out the
        watcher trying to record an alert firing, which is the one write that
        must not fail.
      * busy_timeout — when two writers do collide, wait and retry for five
        seconds instead of raising `database is locked` immediately. SQLite's
        default is zero: the loser fails instantly.

    WAL is a property of the database file, so it survives; setting it here
    just means any process can be the one to establish it. It needs the file's
    directory to be writable (it creates -wal and -shm alongside), which is
    already true.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # Default (FULL) fsyncs on every commit. Under WAL, NORMAL is durable
        # against process crashes — only a power cut can lose the last commits,
        # and this is a rebuildable cache of market data, not a ledger.
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as e:
        # An older SQLite, or a filesystem that won't do WAL (a network mount).
        # Correctness doesn't depend on these; concurrency just gets worse.
        print(f"[db] pragma setup failed, continuing unturned: {e}")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, col: str, decl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # migrations for DBs created by earlier phases
        _ensure_column(conn, "seen_events", "category", "TEXT")
        # NFO futures carry an expiry; the Board picks the current month by it
        _ensure_column(conn, "instruments", "expiry", "TEXT")
        _ensure_column(conn, "seen_events", "digested", "INTEGER DEFAULT 1")
        # journals created before the stop was split into current/initial:
        # backfill the R basis from whatever stop is on the row.
        _ensure_column(conn, "trades", "initial_stop", "REAL")
        conn.execute("UPDATE trades SET initial_stop = stop "
                     "WHERE initial_stop IS NULL AND stop IS NOT NULL")


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
