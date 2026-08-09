"""Watchlist: the shared spine of the system.

Phase 2 (corporate events), Phase 3 (news digest), and Phase 4 (fundamentals)
all iterate over this table. Kept intentionally simple: symbol + exchange.

Adding a stock also fetches its screener.in fundamentals by default — and since
the quarterly *results* live on that same screener page, one fetch primes both
the Fundamentals and the Results views for the new stock. Disable with
AUTO_FETCH_ON_ADD=0 in .env. The web add does it on a background thread so the
UI stays snappy; the CLI does it inline so you can see it happen.
"""
import threading

from . import config, db, instruments

_TRUTHY = ("1", "true", "yes", "on")


def autofetch_enabled() -> bool:
    return str(config.get("AUTO_FETCH_ON_ADD", "1")).lower() in _TRUTHY


def add(tradingsymbol: str, exchange: str | None = None,
        autofetch: bool | None = None, background: bool = False) -> dict:
    inst = instruments.lookup(tradingsymbol, exchange)
    if not inst:
        raise SystemExit(
            f"'{tradingsymbol}' not found in the instrument index. "
            f"Run `sync-instruments` first, or check the symbol with `search`."
        )
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO watchlist (tradingsymbol, exchange, name) "
            "VALUES (?, ?, ?)",
            (inst["tradingsymbol"], inst["exchange"], inst["name"]),
        )
        newly = cur.rowcount > 0
    if autofetch is None:
        autofetch = autofetch_enabled()
    if autofetch:
        # only_if_missing so a re-add of an already-primed stock doesn't
        # needlessly hit screener again.
        ensure_fundamentals(inst["tradingsymbol"], background=background,
                            only_if_missing=True)
    return {**inst, "added": newly}


def ensure_fundamentals(symbol: str, background: bool = False,
                        only_if_missing: bool = False,
                        notify_done: bool | None = None) -> str | None:
    """Fetch + store screener.in fundamentals (and thereby quarterly results)
    for one symbol. Best-effort: a screener hiccup never breaks the add.

    background=True  -> runs on a daemon thread, returns immediately (web).
    background=False -> runs inline, returns a short status string (CLI).
    """
    from . import fundamentals            # lazy: fundamentals imports watchlist
    sym = symbol.upper()
    if only_if_missing and fundamentals.summary(sym):
        return "already stored"

    def _run() -> str:
        try:
            fundamentals.fetch_and_store(sym)
        except (Exception, SystemExit) as e:             # blocked / layout change / offline
            msg = f"fundamentals fetch failed for {sym}: {e}"
            print(f"[watchlist] {msg}")
            # On the web path this runs on a background thread, so a print
            # goes to a server terminal nobody is watching — the stock lands
            # on the watchlist looking fine and the fundamentals silently
            # never arrive. Say it out loud.
            try:
                from . import notify
                notify.send(
                    f"{sym}: fundamentals fetch failed.\n{e}\n"
                    f"Diagnose: /fund {sym} or `cli fund diagnose {sym}`",
                    title="TickerForge", priority="default", tags="warning")
            except (Exception, SystemExit):
                pass
            return msg
        nd = (str(config.get("AUTO_FETCH_NOTIFY", "0")).lower() in _TRUTHY
              if notify_done is None else notify_done)
        if nd:
            try:
                from . import notify
                notify.send(f"{sym}: fundamentals + results fetched.",
                            title="TickerForge", priority="low",
                            tags="chart_with_upwards_trend")
            except (Exception, SystemExit):
                pass
        return "stored"

    if background:
        threading.Thread(target=_run, name=f"fund-{sym}", daemon=True).start()
        return None
    return _run()


def fetch_missing(background: bool = False) -> dict:
    """Prime fundamentals+results for every watchlist stock that lacks them.
    Handy after enabling the feature, to backfill stocks added earlier."""
    out = {}
    for w in items():
        out[w["tradingsymbol"]] = ensure_fundamentals(
            w["tradingsymbol"], background=background, only_if_missing=True)
    return out


def remove(tradingsymbol: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE tradingsymbol = ?",
            (tradingsymbol.upper(),),
        )
        return cur.rowcount


def items() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT tradingsymbol, exchange, name, added_at "
            "FROM watchlist ORDER BY tradingsymbol"
        ).fetchall()
        return [dict(r) for r in rows]


def sparks(days: int = 30) -> list[dict]:
    """Watchlist rows with a tiny price history and the day's change.

    Read from the local candle store, not Kite: this runs on every sidebar
    refresh, and a watchlist of thirty names would otherwise be thirty
    rate-limited calls to draw something decorative. Stale by up to one bar is
    the right trade for a sparkline — it exists to show shape, and shape
    doesn't change in the last five minutes.
    """
    from . import candles, instruments

    out = []
    for row in items():
        symbol = row["tradingsymbol"]
        entry = {"symbol": symbol, "name": row.get("name") or "",
                 "closes": [], "change_pct": None, "last": None}
        inst = instruments.lookup(symbol)
        if inst:
            try:
                bars = candles.get(inst["instrument_token"], "day", days + 5)
            except Exception:                     # noqa: BLE001
                bars = []
            closes = [b["close"] for b in bars][-days:]
            entry["closes"] = closes
            if len(closes) >= 2:
                entry["last"] = closes[-1]
                entry["change_pct"] = round(
                    (closes[-1] / closes[-2] - 1) * 100, 2)
            elif closes:
                entry["last"] = closes[-1]
        out.append(entry)
    return out
