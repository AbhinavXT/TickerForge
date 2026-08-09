"""Scanner universes — the symbols you search, and getting candles for them.

Distinct from the watchlist on purpose. The watchlist is what you follow; a
universe is what you look through. The entire value of a scanner is surfacing
names you *aren't* already watching, so scanning your watchlist would be
scanning the answer you already have.

The hard part isn't the scan, it's the candles. Two hundred symbols × 400 daily
bars is 200 rate-limited Kite calls, and the roadmap's "one overnight job" only
works if it can be interrupted and resumed — a backfill that loses three hours
of work to one timeout at 2am is a backfill you'll stop running.

So `backfill()`:

  * records progress per symbol as it goes, and skips what's already covered
    on a re-run, making it safe to kill and restart;
  * paces itself under Kite's historical-data limit rather than discovering
    the limit by being throttled;
  * keeps going past a failing symbol, and reports which ones failed rather
    than aborting the other 199.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from . import candles, db, instruments, kite_auth

DEFAULT_UNIVERSE = "nifty200"

# Kite allows ~3 historical requests/second. 0.4s is comfortably under it —
# an overnight job has no reason to run near a limit whose penalty is a ban.
FETCH_INTERVAL = 0.4
BACKFILL_DAYS = 500          # ~2 trading years: enough for a 200-DMA and 52w


# ------------------------------------------------------------- membership
def names() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT name, COUNT(*) n FROM universe GROUP BY name ORDER BY name")]


def members(name: str = DEFAULT_UNIVERSE) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT tradingsymbol, exchange, token FROM universe "
            "WHERE name=? ORDER BY tradingsymbol", (name,))]


def symbols(name: str = DEFAULT_UNIVERSE) -> list[str]:
    return [m["tradingsymbol"] for m in members(name)]


def set_members(name: str, syms: list[str], exchange: str = "NSE",
                replace: bool = True) -> dict:
    """Define a universe from a list of symbols.

    Unknown symbols are reported rather than silently dropped: a universe
    quietly 12 names short scans 12 names short forever, and nothing about the
    output would tell you.
    """
    added, unknown = [], []
    with db.connect() as conn:
        if replace:
            conn.execute("DELETE FROM universe WHERE name=?", (name,))
        for s in syms:
            s = (s or "").strip().upper()
            if not s:
                continue
            inst = instruments.lookup(s, exchange)
            if not inst:
                unknown.append(s)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO universe (name, tradingsymbol, "
                "exchange, token) VALUES (?,?,?,?)",
                (name, inst["tradingsymbol"], inst.get("exchange", exchange),
                 inst["instrument_token"]))
            added.append(inst["tradingsymbol"])
    return {"universe": name, "added": len(added), "unknown": unknown,
            "symbols": added}


def add_members(name: str, syms: list[str], exchange: str = "NSE") -> dict:
    return set_members(name, syms, exchange, replace=False)


def remove(name: str, symbol: str | None = None) -> int:
    with db.connect() as conn:
        if symbol:
            return conn.execute(
                "DELETE FROM universe WHERE name=? AND tradingsymbol=?",
                (name, symbol.upper())).rowcount
        return conn.execute("DELETE FROM universe WHERE name=?",
                            (name,)).rowcount


def from_watchlist(name: str = "watchlist") -> dict:
    """Seed a universe from the watchlist — a starting point when you don't
    have an index list to hand."""
    from . import watchlist
    return set_members(name, [w["tradingsymbol"] for w in watchlist.items()])


def from_instruments(name: str, limit: int = 500, exchange: str = "NSE") -> dict:
    """Build a universe from the local instrument index.

    A blunt instrument — the index has no market-cap or liquidity data, so
    this can't give you "the Nifty 200". It gives you N equities, which is
    useful for testing the machinery and not much else. Prefer pasting a real
    index constituent list.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT tradingsymbol FROM instruments WHERE exchange=? "
            "AND instrument_type='EQ' ORDER BY tradingsymbol LIMIT ?",
            (exchange, limit)).fetchall()
    return set_members(name, [r["tradingsymbol"] for r in rows], exchange)


# Series suffixes and name patterns that aren't ordinary equities. Screening
# these wastes a backfill slot each and produces hits you can't act on.
_NOT_EQUITY = ("-RE", "-BE", "-BZ", "-SM", "-ST", "-IT", "-GS", "-N1", "-N2",
               "-N3", "-N4", "-N5", "-N6", "-N7", "-N8", "-N9", "-Y1", "-NA")
_FUND_WORDS = ("ETF", "BEES", "LIQUID", "GILT", "NIFTY", "SENSEX", "INDEX",
               "IVZIN", "MAFANG", "GOLDBEES", "SILVER")


def from_market(name: str = "market", exchange: str = "NSE",
                limit: int | None = None, include_funds: bool = False) -> dict:
    """Every ordinary equity on the exchange.

    The whole market, minus the things that aren't companies: rights issues,
    the trade-to-trade and SME series, ETFs and index funds. Those are real
    instruments but they aren't what "find me an undervalued stock" means, and
    each one costs a rate-limited request at backfill time.

    This is deliberately unfiltered on quality — there is no market-cap or
    volume data in the instrument index to filter on. Use `prune` AFTER the
    backfill, when there are actual prices to judge liquidity by.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT tradingsymbol, name FROM instruments "
            "WHERE exchange=? AND instrument_type='EQ' ORDER BY tradingsymbol",
            (exchange,)).fetchall()

    keep = []
    for r in rows:
        sym = r["tradingsymbol"].upper()
        if any(sym.endswith(sfx) for sfx in _NOT_EQUITY):
            continue
        if not include_funds and any(w in sym for w in _FUND_WORDS):
            continue
        keep.append(sym)
        if limit and len(keep) >= limit:
            break
    return set_members(name, keep, exchange)


def prune(name: str, min_price: float = 20.0, min_volume: float = 50_000,
          min_bars: int = 200, dry: bool = False) -> dict:
    """Drop names you couldn't trade even if the screen liked them.

    Run this after the backfill, because liquidity is a fact about prices and
    the instrument index has none. Without it a whole-market screen surfaces
    micro-caps trading four thousand shares a day — technically hits, and
    completely unactionable: the spread eats the edge and your own order moves
    the price.

    `min_bars` also catches recent listings, where a 200-day average is
    computed from whatever exists and means nothing.
    """
    from . import candles, instruments

    kept, dropped = [], {}
    for sym in symbols(name):
        inst = instruments.lookup(sym)
        if not inst:
            dropped[sym] = "not in the instrument index"
            continue
        try:
            bars = candles.get(inst["instrument_token"], "day", 400)
        except Exception as e:                       # noqa: BLE001
            dropped[sym] = str(e)[:60]
            continue
        if len(bars) < min_bars:
            dropped[sym] = f"only {len(bars)} bars"
            continue
        last = bars[-1]
        if last["close"] < min_price:
            dropped[sym] = f"price {last['close']:.1f}"
            continue
        recent = bars[-20:]
        avg_vol = sum(b["volume"] for b in recent) / len(recent)
        if avg_vol < min_volume:
            dropped[sym] = f"volume {avg_vol:,.0f}"
            continue
        kept.append(sym)

    if not dry:
        set_members(name, kept)
    return {"universe": name, "kept": len(kept), "dropped": len(dropped),
            "dry": dry, "why": dict(list(dropped.items())[:40])}


# ---------------------------------------------------------------- backfill
def coverage(name: str = DEFAULT_UNIVERSE, interval: str = "day",
             days: int = BACKFILL_DAYS) -> dict:
    """How much of the universe already has candles. Answers 'do I need to run
    the overnight job, and how long will it take?' before you start it."""
    rows = members(name)
    have, missing, thin = 0, [], []
    for m in rows:
        if not m["token"]:
            missing.append(m["tradingsymbol"])
            continue
        try:
            n = len(candles.get(m["token"], interval, days))
        except Exception:
            n = 0
        if n == 0:
            missing.append(m["tradingsymbol"])
        elif n < 200:
            thin.append(m["tradingsymbol"])
            have += 1
        else:
            have += 1
    todo = len(missing) + len(thin)
    return {
        "universe": name, "symbols": len(rows), "covered": have,
        "missing": missing, "thin": thin,
        "todo": todo,
        "est_seconds": round(todo * (FETCH_INTERVAL + 0.6), 1),
    }


def backfill(name: str = DEFAULT_UNIVERSE, interval: str = "day",
             days: int = BACKFILL_DAYS, only_missing: bool = True,
             progress=None, pace: float = FETCH_INTERVAL) -> dict:
    """Fetch candles for the universe. Resumable, paced, and failure-tolerant.

    `only_missing` skips symbols that already have a reasonable history, which
    is what makes a re-run cheap and an interrupted run safe to restart.
    """
    rows = members(name)
    if not rows:
        raise ValueError(f"universe '{name}' is empty — define it first")

    try:
        kite = kite_auth.get_kite()
    except SystemExit as e:
        raise RuntimeError(str(e)) from e

    done, skipped, failed = [], [], []
    started = time.time()
    for i, m in enumerate(rows, 1):
        sym, tok = m["tradingsymbol"], m["token"]
        if not tok:
            failed.append({"symbol": sym, "error": "no instrument token"})
            continue
        if only_missing:
            try:
                if len(candles.get(tok, interval, days)) >= 200:
                    skipped.append(sym)
                    if progress:
                        progress(i, len(rows), sym, "skip")
                    continue
            except Exception:
                pass
        try:
            candles.ensure(kite, tok, interval, days)
            done.append(sym)
            state = "ok"
        except (Exception, SystemExit) as e:
            failed.append({"symbol": sym, "error": str(e)})
            state = "fail"
        if progress:
            progress(i, len(rows), sym, state)
        # Pace only after a real fetch — skipped symbols cost nothing.
        if state == "ok":
            time.sleep(pace)

    return {
        "universe": name, "symbols": len(rows),
        "fetched": len(done), "skipped": len(skipped), "failed": failed,
        "seconds": round(time.time() - started, 1),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def mark_backfilled(name: str) -> None:
    db.set_meta(f"universe_backfill:{name}",
                datetime.now().strftime("%Y-%m-%d"))


def last_backfill(name: str) -> str | None:
    return db.get_meta(f"universe_backfill:{name}") or None


# ------------------------------------------------------------ saved scans
def save_scan(name: str, spec: dict) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO saved_scans (name, spec) VALUES (?,?)",
            (name, json.dumps(spec)))


def saved_scans() -> list[dict]:
    with db.connect() as conn:
        out = []
        for r in conn.execute("SELECT * FROM saved_scans ORDER BY name"):
            d = dict(r)
            try:
                d["spec"] = json.loads(d["spec"])
            except ValueError:
                d["spec"] = {}
            out.append(d)
        return out


def get_scan(name: str) -> dict | None:
    for s in saved_scans():
        if s["name"] == name:
            return s
    return None


def delete_scan(name: str) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM saved_scans WHERE name=?",
                            (name,)).rowcount > 0


def record_run(name: str, hits: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE saved_scans SET last_run=?, last_hits=? "
                     "WHERE name=?",
                     (datetime.now().isoformat(timespec="seconds"), hits, name))
