"""One-off repair: re-fetch daily bars that were stored while still forming.

Why this exists
---------------
The candle store's unique index on (token, interval, ts) meant `INSERT OR
IGNORE` silently preserved the first version of a bar it ever saw. Opening a
chart at 11:00 stored today's daily bar with an 11:00 close, and every later
top-up was ignored as a duplicate. So any date on which you looked at a chart
during market hours is frozen at whatever the price happened to be at that
moment — wrong close, wrong high, wrong low, wrong volume.

`candles.ensure()` no longer does this. But fixing the code cannot fix bars
already on disk, and every backtest, scan and signal that reads those dates is
reading a snapshot instead of a session.

What it does
------------
For each symbol, deletes the last `--days` days of DAILY bars and lets the
normal backfill path fetch them again. Work happens one symbol at a time, so
an interrupted run leaves every other symbol untouched and re-running picks up
where it stopped. Intraday intervals are left alone: they matter far less to
the EOD path and are cheap to rebuild on demand.

Usage
-----
    python -m app.cli login                        # if the session is stale
    python scripts_repair_forming_bars.py --dry    # look before you leap
    python scripts_repair_forming_bars.py          # the real thing

Costs one rate-limited Kite request per symbol. For a large universe that is
tens of minutes — run it after the close, or overnight.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timedelta

from app import candles, kite_auth


def tokens(interval: str = "day") -> list[int]:
    with candles._conn(read_only=True) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT token FROM candles WHERE interval=? ORDER BY token",
            [interval]).fetchall()]


def affected(cutoff: datetime) -> int:
    with candles._conn(read_only=True) as c:
        return c.execute(
            "SELECT count(*) FROM candles WHERE interval='day' AND ts >= ?",
            [cutoff]).fetchone()[0]


def backup() -> str:
    """Copy the store before touching it. Cheap insurance against a bad night."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{candles.DB}.{stamp}.bak"
    shutil.copy2(candles.DB, dest)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-read daily bars that may have been stored mid-session.")
    ap.add_argument("--days", type=int, default=400,
                    help="how far back to re-read (default 400)")
    ap.add_argument("--dry", action="store_true",
                    help="report what would happen, change nothing")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the pre-flight copy of candles.duckdb")
    ap.add_argument("--symbols", type=int, nargs="*", metavar="TOKEN",
                    help="repair only these instrument tokens")
    args = ap.parse_args()

    if not candles.DB.exists():
        print(f"no candle store at {candles.DB} — nothing to repair")
        return 0

    toks = args.symbols or tokens()
    if not toks:
        print("no daily series in the store — nothing to repair")
        return 0

    cutoff = datetime.now() - timedelta(days=args.days)
    n = affected(cutoff)
    print(f"store   : {candles.DB}")
    print(f"series  : {len(toks)} daily")
    print(f"window  : since {cutoff:%Y-%m-%d} ({args.days} days)")
    print(f"bars    : {n:,} will be deleted and re-fetched")

    if args.dry:
        print("\n--dry: nothing changed. Drop the flag to run it.")
        return 0

    if not args.no_backup:
        dest = backup()
        print(f"backup  : {dest}")

    try:
        kite = kite_auth.get_kite()
    except Exception as e:                            # noqa: BLE001
        print(f"\nno usable Kite session ({e}).")
        print("Run: python -m app.cli login")
        return 1

    print()
    ok = failed = 0
    t0 = time.perf_counter()
    for i, tok in enumerate(toks, 1):
        try:
            # Delete and refetch ONE symbol at a time. A bulk delete up front
            # would leave every unprocessed symbol with a hole in it if this
            # run died halfway — which, at one rate-limited request per symbol
            # over several hundred symbols, is a real possibility.
            with candles._conn() as c:
                c.execute(
                    "DELETE FROM candles WHERE token=? AND interval='day' "
                    "AND ts >= ?", [tok, cutoff])
            candles.ensure(kite, tok, "day", args.days + 5)
            ok += 1
            print(f"  [{i}/{len(toks)}] {tok} ok")
        except KeyboardInterrupt:
            print("\ninterrupted — symbols already done are repaired, "
                  "the rest are untouched. Re-run to continue.")
            return 130
        except Exception as e:                        # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(toks)}] {tok} FAILED: {e}")

    mins = (time.perf_counter() - t0) / 60
    print(f"\ndone in {mins:.1f} min — {ok} repaired, {failed} failed")
    if failed:
        print("Re-run to retry the failures; repaired symbols are cheap to redo.")
    print("Re-run any backtest whose numbers you were relying on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
