"""Offline tests for the candle store — no Kite, temp DuckDB file.

Run:  python -m pytest tests/test_candles.py
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from app import candles

_tmp = tempfile.TemporaryDirectory()
candles.DB = Path(_tmp.name) / "test.duckdb"

TOKEN = 999
NOW = datetime.now()


class FakeKite:
    """Records the windows asked for; serves one daily bar per day requested."""

    def __init__(self, available_from: datetime | None = None):
        self.calls: list[tuple[datetime, datetime]] = []
        self.available_from = available_from

    def historical_data(self, token, frm, to, interval):
        self.calls.append((frm, to))
        start = frm if self.available_from is None else max(frm, self.available_from)
        rows, d = [], start
        while d < to:
            rows.append({"date": d, "open": 100.0, "high": 101.0,
                         "low": 99.0, "close": 100.0, "volume": 1000})
            d += timedelta(days=1)
        return rows


def seed(days_back: int, count: int) -> None:
    """Put `count` daily bars into the store, ending `days_back` days ago."""
    with candles._conn() as c:
        for i in range(count):
            ts = NOW - timedelta(days=days_back + count - i)
            c.execute("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                      [TOKEN, "day", ts, 100, 101, 99, 100, 1000])


def stored_span() -> tuple[datetime, datetime, int]:
    with candles._conn() as c:
        return c.execute(
            "SELECT min(ts), max(ts), count(*) FROM candles "
            "WHERE token=? AND interval='day'", [TOKEN]).fetchone()


def reset() -> None:
    with candles._conn() as c:
        c.execute("DELETE FROM candles WHERE token=?", [TOKEN])
        c.execute("DELETE FROM coverage WHERE token=?", [TOKEN])


def test_empty_store_fetches_whole_window():
    reset()
    k = FakeKite()
    candles.ensure(k, TOKEN, "day", days=100)
    first, last, n = stored_span()
    assert n > 90
    assert (NOW - first).days >= 99


def test_longer_request_backfills_earlier_history():
    """The bug this guards: a store seeded by a short request must not
    silently satisfy a longer one with whatever it already holds."""
    reset()
    seed(days_back=0, count=200)                  # ~200 days of history
    _, _, before = stored_span()

    k = FakeKite()
    candles.ensure(k, TOKEN, "day", days=365 * 5)  # now ask for five years

    first, _, after = stored_span()
    assert after > before + 1000, f"only {after} bars after a 5y request"
    assert (NOW - first).days > 1700, "history did not reach back five years"
    # and it actually asked Kite for the old window, not just today
    assert any((NOW - frm).days > 1700 for frm, _ in k.calls)


def test_forward_topup_only_asks_for_the_tail():
    reset()
    seed(days_back=3, count=400)
    k = FakeKite()
    candles.ensure(k, TOKEN, "day", days=365)     # window already covered at the far end
    assert k.calls, "should still top up to now"
    assert all((NOW - frm).days < 400 for frm, _ in k.calls)


def test_missing_history_is_asked_for_once():
    """A stock listed recently has no pre-listing data. We should ask once,
    get nothing, and never spend a rate-limited call on it again."""
    reset()
    listed = NOW - timedelta(days=300)
    k = FakeKite(available_from=listed)

    candles.ensure(k, TOKEN, "day", days=365 * 5)
    deep_first = [c for c in k.calls if (NOW - c[0]).days > 1000]
    assert deep_first, "first 5y request must reach back"

    k.calls.clear()
    candles.ensure(k, TOKEN, "day", days=365 * 5)  # same request again
    deep_again = [c for c in k.calls if (NOW - c[0]).days > 1000]
    assert not deep_again, "pre-listing window was re-requested"


# ------------------------------------------------- the session in progress
class FormingKite:
    """Serves ONE settled daily bar for today, whatever window is asked for.

    Stands in for Kite after the close: the bar it returns is final, and the
    store must end up holding that one rather than whatever snapshot it took
    of the same bar earlier in the session.
    """

    FINAL = {"open": 100.0, "high": 125.0, "low": 98.0,
             "close": 120.0, "volume": 9999}

    def __init__(self):
        self.calls: list[tuple[datetime, datetime]] = []

    def historical_data(self, token, frm, to, interval):
        self.calls.append((frm, to))
        today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        return [{"date": today, **self.FINAL}] if frm <= today <= to else []


def _today_floor() -> datetime:
    return NOW.replace(hour=0, minute=0, second=0, microsecond=0)


def test_forming_bar_is_replaced_not_ignored():
    """A daily bar stored mid-session must be re-read, not kept for ever.

    Opening a chart at 11:00 stores today's bar with an 11:00 close. The
    unique index on (token, interval, ts) means a later `INSERT OR IGNORE`
    silently keeps that snapshot — so the day's high, low, close and volume
    stay frozen at 11:00 in every backtest, scan and signal that runs
    afterwards. The store has to drop the session in progress before topping
    up.
    """
    reset()
    today = _today_floor()
    with candles._conn() as c:                     # the 11:00 snapshot
        c.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)",
                  [TOKEN, "day", today, 100.0, 103.0, 99.0, 102.0, 1000])

    candles.ensure(FormingKite(), TOKEN, "day", days=5)

    bar = candles.get(TOKEN, "day", 5)[-1]
    assert bar["close"] == 120.0, f"stale close survived: {bar['close']}"
    assert bar["high"] == 125.0, f"stale high survived: {bar['high']}"
    assert bar["low"] == 98.0
    assert bar["volume"] == 9999


def test_settled_history_is_left_alone():
    """Only today is provisional. Yesterday and earlier must not be refetched
    — that would turn every call into a full re-download."""
    reset()
    seed(days_back=1, count=300)
    _, _, before = stored_span()

    k = FormingKite()
    candles.ensure(k, TOKEN, "day", days=365)

    _, _, after = stored_span()
    assert after >= before, "settled bars were deleted"
    assert all(frm >= NOW - timedelta(days=370) for frm, _ in k.calls)


# ------------------------------------------------------ cross-process locking
def _hold_lock(seconds: float):
    """A separate PROCESS holding the write lock. The connection must be bound
    to a name — an unassigned duckdb.connect() is collected immediately and
    the lock evaporates before the test can observe it."""
    import subprocess
    import sys
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import duckdb, time; c = duckdb.connect(r'{candles.DB}'); "
         f"time.sleep({seconds})"])


def test_reader_waits_out_a_competing_writer():
    """DuckDB refuses a second connection outright — it must not reach callers.

    A writer excludes everyone, including read-only connections, and raises
    immediately rather than queueing. Four processes share this store, so
    without a retry an ordinary overlap surfaces as an IOException — and
    inside signals.scan, which catches per-symbol errors, as a symbol quietly
    missing from the results.
    """
    import time

    reset()
    holder = _hold_lock(1.0)
    try:
        time.sleep(0.4)                      # let it take the lock
        t0 = time.perf_counter()
        with candles._conn(read_only=True) as c:
            c.execute("SELECT count(*) FROM candles").fetchone()
        assert time.perf_counter() - t0 > 0.2, \
            "returned too fast to have waited for the lock"
    finally:
        holder.wait()


def test_lock_retry_gives_up_eventually():
    """Retrying is not the same as hanging: a lock held for ever must raise."""
    import time

    reset()
    original = candles.LOCK_RETRIES
    candles.LOCK_RETRIES = 2                 # ~0.75s of patience
    holder = _hold_lock(10)
    try:
        time.sleep(0.4)
        with pytest.raises(duckdb.IOException):
            candles._conn(read_only=True)
    finally:
        candles.LOCK_RETRIES = original
        holder.kill()
        holder.wait()


def test_ensure_does_not_hold_the_lock_across_the_fetch():
    """The write lock must be released while the network fetch runs.

    Holding it across `_fetch_chunks` — which sleeps between rate-limited
    requests — is what turns a 300ms overlap into a multi-second one for
    every other process.
    """
    reset()
    held = []

    class SlowKite:
        def historical_data(self, token, frm, to, interval):
            # mid-fetch: can anyone else open the store right now?
            try:
                c = duckdb.connect(str(candles.DB), read_only=True)
                c.close()
                held.append(False)
            except Exception:
                held.append(True)
            return []

    candles.ensure(SlowKite(), TOKEN, "day", days=30)
    assert held, "kite was never called"
    assert not any(held), "the write lock was held across the network fetch"
