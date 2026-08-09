"""Instrument master: nightly sync from Kite + local search.

Kite has no server-side search API — you download the full instrument dump
(~90k rows) and query your own copy. `sync` replaces the table atomically;
`search` powers the CLI (and later the web UI's search box).
"""
from datetime import datetime

from . import config, db


def sync(kite) -> int:
    """Fetch the full instrument dump and rebuild the local index."""
    rows = []
    for ex in config.EXCHANGES:
        rows.extend(kite.instruments(exchange=ex))

    with db.connect() as conn:
        conn.execute("DELETE FROM instruments")
        conn.executemany(
            """INSERT OR REPLACE INTO instruments
               (instrument_token, exchange_token, tradingsymbol, name,
                exchange, segment, instrument_type, lot_size, tick_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["instrument_token"], r["exchange_token"], r["tradingsymbol"],
                    r.get("name") or "", r["exchange"], r.get("segment") or "",
                    r.get("instrument_type") or "", r.get("lot_size") or 0,
                    r.get("tick_size") or 0.0,
                )
                for r in rows
            ],
        )
    db.set_meta("instruments_synced_at", datetime.now().isoformat(timespec="seconds"))
    return len(rows)


def search(query: str, limit: int = 15, equities_only: bool = True):
    """Search by symbol or company name. Exact symbol matches rank first."""
    q = query.strip().upper()
    like = f"%{q}%"
    where = "(tradingsymbol LIKE ? OR UPPER(name) LIKE ?)"
    args: list = [like, like]
    if equities_only:
        where += " AND instrument_type = 'EQ'"
    sql = f"""
        SELECT tradingsymbol, name, exchange, instrument_token, instrument_type
        FROM instruments
        WHERE {where}
        ORDER BY
            CASE WHEN tradingsymbol = ? THEN 0
                 WHEN tradingsymbol LIKE ? THEN 1
                 ELSE 2 END,
            LENGTH(tradingsymbol),
            CASE exchange WHEN 'NSE' THEN 0 ELSE 1 END
        LIMIT ?
    """
    args += [q, f"{q}%", limit]
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def lookup(tradingsymbol: str, exchange: str | None = None):
    """Exact lookup; prefers NSE when exchange isn't specified."""
    sql = ("SELECT * FROM instruments WHERE tradingsymbol = ? "
           "AND instrument_type = 'EQ'")
    args: list = [tradingsymbol.upper()]
    if exchange:
        sql += " AND exchange = ?"
        args.append(exchange.upper())
    sql += " ORDER BY CASE exchange WHEN 'NSE' THEN 0 ELSE 1 END LIMIT 1"
    with db.connect() as conn:
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None
