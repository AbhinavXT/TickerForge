"""Offline tests for the watcher's indicator cache — no Kite, no network.

Run:  python -m pytest tests/test_watcher.py
"""
import time
from unittest import mock

from app import watcher


def bars(n=30):
    return [{"time": i * 300, "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 1000} for i in range(n)]


def run_cache(cache, store, tok=1, interval="5minute", at=None):
    """Call cache.candles() with the candle store swapped out, optionally
    pretending the clock has moved on."""
    with mock.patch("app.candles.ensure", store["ensure"]), \
         mock.patch("app.candles.get", store["get"]), \
         mock.patch.object(watcher, "notify"):
        if at is not None:
            with mock.patch.object(time, "time", lambda: at):
                return cache.candles(None, tok, interval)
        return cache.candles(None, tok, interval)


def ok_store(rows):
    return {"ensure": lambda *a, **k: None, "get": lambda *a, **k: rows}


def broken_store(rows):
    def boom(*a, **k):
        raise RuntimeError("kite session gone")
    return {"ensure": boom, "get": lambda *a, **k: rows}


def setup_function():
    watcher._warned.clear()


def test_serves_fresh_candles():
    c = watcher._IndCache()
    rows = bars()
    assert run_cache(c, ok_store(rows), at=1_000_000) == rows


def test_brief_failure_keeps_serving_recent_bars():
    """One failed fetch shouldn't drop an alert — the bars are a bar old."""
    c = watcher._IndCache()
    rows = bars()
    t = 1_000_000
    run_cache(c, ok_store(rows), at=t)
    got = run_cache(c, broken_store(rows), at=t + 300)      # one bar later
    assert got == rows


def test_persistent_failure_stops_serving_stale_bars():
    """The bug: a fetch that keeps failing left indicators running forever on
    frozen candles with a live price patched on — confidently wrong, silent."""
    c = watcher._IndCache()
    rows = bars()
    t = 1_000_000
    run_cache(c, ok_store(rows), at=t)

    pushed = []
    with mock.patch.object(watcher.notify, "send",
                           lambda m, **k: pushed.append(m)), \
         mock.patch("app.candles.ensure", broken_store(rows)["ensure"]), \
         mock.patch("app.candles.get", lambda *a, **k: rows):
        for n in range(1, 6):                              # five bars of failure
            with mock.patch.object(time, "time", lambda n=n: t + 300 * n):
                got = c.candles(None, 1, "5minute")

    assert got == [], "stale candles were still being served"
    assert pushed, "going stale was not reported to the phone"
    assert "stale" in pushed[0].lower() or "paused" in pushed[0].lower()


def test_recovery_clears_the_warning():
    c = watcher._IndCache()
    rows = bars()
    t = 1_000_000
    run_cache(c, ok_store(rows), at=t)
    with mock.patch.object(watcher, "notify"):
        for n in range(1, 6):
            run_cache(c, broken_store(rows), at=t + 300 * n)
    assert watcher._warned, "should have warned while stale"

    got = run_cache(c, ok_store(rows), at=t + 300 * 6)      # fetch works again
    assert got == rows
    assert not any(k.startswith("stale:1:") for k in watcher._warned), \
        "warning state not cleared after recovery"


def test_warn_is_deduped():
    watcher._warned.clear()
    pushed = []
    with mock.patch.object(watcher.notify, "send",
                           lambda m, **k: pushed.append(m)):
        watcher._warn("k", "first")
        watcher._warn("k", "second")          # inside the repeat window
    assert len(pushed) == 1


def test_warn_survives_a_dead_network():
    watcher._warned.clear()

    def no_network(msg, **k):
        raise OSError("no route to host")

    with mock.patch.object(watcher.notify, "send", no_network):
        watcher._warn("k2", "should not raise")
