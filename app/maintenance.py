"""The nightly job: back up, top up, prune.

Boring by design. It exists so that the two failures that actually lose work —
a corrupted database and a candle store that quietly fell behind — are handled
while you're asleep rather than discovered on a Monday morning.

Backups are the part worth being careful about, because a backup taken wrongly
is worse than none: it looks like insurance and isn't.

  * **SQLite** is copied through the online backup API, not `cp`. In WAL mode
    the `.db` file alone is an incomplete picture — recent commits live in
    `-wal` until a checkpoint — so copying the three files separately can
    capture them mid-transaction and produce a database that opens fine and is
    missing yesterday's trades. The backup API takes a consistent snapshot of
    a live database, which is exactly the situation here: the services are
    running.

  * **DuckDB** has no such API, so instead we take a read-only connection and
    copy the file while holding it. DuckDB allows only one process to hold the
    file at a time, so for as long as that handle is open no writer can start,
    and the bytes can't shift under the copy.

Run:  python -m app.cli maintain [--dry]
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import candles, config, db, instruments, watchlist

BACKUP_DIR = config.DATA_DIR / "backups"


# ------------------------------------------------------------------ backups
def backup_sqlite(dest_dir: Path) -> dict:
    """Consistent snapshot of the live SQLite database."""
    dest = dest_dir / config.DB_PATH.name
    src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return {"file": dest.name, "bytes": dest.stat().st_size}


def backup_duckdb(dest_dir: Path) -> dict | None:
    """Copy the candle store while holding the file's only lock."""
    if not candles.DB.exists():
        return None
    dest = dest_dir / candles.DB.name
    with candles._conn(read_only=True):
        # The handle is doing the work, not the body: while it's open no other
        # process can open the store, so nothing can write mid-copy.
        shutil.copy2(candles.DB, dest)
    return {"file": dest.name, "bytes": dest.stat().st_size}


def prune_backups(keep_days: int) -> list[str]:
    """Drop dated folders older than the cutoff. Returns what went."""
    if not BACKUP_DIR.exists():
        return []
    cutoff = datetime.now().date() - timedelta(days=keep_days)
    removed = []
    for folder in sorted(BACKUP_DIR.iterdir()):
        if not folder.is_dir():
            continue
        try:
            when = datetime.strptime(folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue                              # not ours; leave it alone
        if when < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
    return removed


def _watchlist_symbols() -> list[str]:
    return [r["tradingsymbol"] for r in watchlist.items()]


# ------------------------------------------------------------------ candles
def topup_candles(kite, symbols: list[str], interval: str = "day",
                  days: int = 400) -> dict:
    """Bring the store forward for each symbol.

    One symbol failing must not end the run — a delisted name in the watchlist
    shouldn't cost you the backup that runs afterwards.
    """
    done, failed = 0, {}
    for symbol in symbols:
        inst = instruments.lookup(symbol)
        if not inst:
            failed[symbol] = "not in the instrument index"
            continue
        try:
            candles.ensure(kite, inst["instrument_token"], interval, days)
            done += 1
        except Exception as e:                    # noqa: BLE001
            failed[symbol] = str(e)[:120]
    return {"updated": done, "failed": failed}


# ---------------------------------------------------------------------- run
def run(kite=None, dry: bool = False, keep_days: int | None = None,
        skip_candles: bool = False) -> dict:
    """Everything, in the order that matters if it gets interrupted.

    Backups go FIRST. A run that dies halfway should still have left you with
    a good copy of last night's state; topping up candles first and crashing
    means the one job you actually needed didn't happen.
    """
    started = time.time()
    keep_days = int(keep_days if keep_days is not None
                    else config.get("BACKUP_KEEP_DAYS", 14))
    stamp = datetime.now().strftime("%Y-%m-%d")
    dest = BACKUP_DIR / stamp

    report: dict = {"when": datetime.now().isoformat(timespec="seconds"),
                    "dry": dry, "dest": str(dest), "keep_days": keep_days,
                    "errors": []}

    if dry:
        symbols = _watchlist_symbols()
        report.update({
            "would_backup": [config.DB_PATH.name] +
                            ([candles.DB.name] if candles.DB.exists() else []),
            "would_topup": len(symbols),
            "would_prune": _prunable(keep_days),
        })
        report["seconds"] = round(time.time() - started, 1)
        return report

    dest.mkdir(parents=True, exist_ok=True)

    try:
        report["sqlite"] = backup_sqlite(dest)
    except Exception as e:                        # noqa: BLE001
        report["errors"].append(f"sqlite backup: {e}")
    try:
        report["duckdb"] = backup_duckdb(dest)
    except Exception as e:                        # noqa: BLE001
        report["errors"].append(f"duckdb backup: {e}")

    if not skip_candles:
        if kite is None:
            report["errors"].append("no Kite session — candles not topped up")
        else:
            symbols = _watchlist_symbols()
            try:
                report["candles"] = topup_candles(kite, symbols)
            except Exception as e:                # noqa: BLE001
                report["errors"].append(f"candle top-up: {e}")

    try:
        report["pruned"] = prune_backups(keep_days)
    except Exception as e:                        # noqa: BLE001
        report["errors"].append(f"prune: {e}")

    report["seconds"] = round(time.time() - started, 1)
    db.set_meta("maintenance_last_run", report["when"])
    return report


def _prunable(keep_days: int) -> list[str]:
    if not BACKUP_DIR.exists():
        return []
    cutoff = datetime.now().date() - timedelta(days=keep_days)
    out = []
    for folder in sorted(BACKUP_DIR.iterdir()):
        if not folder.is_dir():
            continue
        try:
            when = datetime.strptime(folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if when < cutoff:
            out.append(folder.name)
    return out


def format_report(r: dict) -> str:
    """Short enough to read on a phone at 03:00 without scrolling."""
    if r.get("dry"):
        return "\n".join([
            "Maintenance (dry run)",
            f"  would back up : {', '.join(r.get('would_backup', [])) or '—'}",
            f"  would top up  : {r.get('would_topup', 0)} symbols",
            f"  would prune   : {len(r.get('would_prune', []))} old folders",
            f"  keeping       : {r['keep_days']} days",
        ])

    lines = ["Maintenance done" if not r["errors"] else "Maintenance — WITH ERRORS"]
    if r.get("sqlite"):
        lines.append(f"  sqlite : {_mb(r['sqlite']['bytes'])}")
    if r.get("duckdb"):
        lines.append(f"  candles: {_mb(r['duckdb']['bytes'])}")
    c = r.get("candles")
    if c:
        bit = f"  topped up {c['updated']} symbols"
        if c["failed"]:
            bit += f", {len(c['failed'])} failed"
        lines.append(bit)
    if r.get("pruned"):
        lines.append(f"  pruned {len(r['pruned'])} folders older than "
                     f"{r['keep_days']}d")
    for e in r["errors"]:
        lines.append(f"  ! {e}")
    lines.append(f"  {r['seconds']}s → {r['dest']}")
    return "\n".join(lines)


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"
