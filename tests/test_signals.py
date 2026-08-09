"""Offline tests for the signal scanner — no Kite, temp DB.

Run:  python -m tests.test_signals
"""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import config

# point the app DB at a temp file BEFORE anything touches it
_tmp = tempfile.TemporaryDirectory()
config.DB_PATH = Path(_tmp.name) / "test.db"

from app import db, signals  # noqa: E402  (after DB_PATH override)

db.init()


def C(c, t, o=None, h=None, l=None, v=1000):
    o = c if o is None else o
    return {"time": t, "open": o, "high": h if h is not None else max(o, c)+1,
            "low": l if l is not None else min(o, c)-1, "close": c, "volume": v}


def mk(closes, t0=0):
    return [C(c, t0 + i*86400) for i, c in enumerate(closes)]


def test_drop_forming_bar():
    # last bar dated "today" at 11:00 -> dropped; at 16:00 -> kept
    today = datetime(2026, 7, 24)                      # a Friday
    ts_today = int(datetime(2026, 7, 24).timestamp()) - \
        int(datetime(1970, 1, 1).timestamp())
    ts_today = int(datetime(2026, 7, 24).replace(tzinfo=None).timestamp())
    # candle "time" is IST-shifted epoch read back as UTC wall time; emulate:
    import calendar
    ts_today = calendar.timegm(today.timetuple())
    c = mk([100, 101], t0=ts_today - 86400)
    kept = signals.drop_forming_bar(c, now=today.replace(hour=11))
    assert len(kept) == 1
    kept = signals.drop_forming_bar(c, now=today.replace(hour=16))
    assert len(kept) == 2
    # last bar from yesterday at 11:00 -> kept
    c2 = mk([100, 101], t0=ts_today - 2*86400)
    assert len(signals.drop_forming_bar(c2, now=today.replace(hour=11))) == 2


def test_evaluate_flip_detection():
    # price_sma(3): rises above the SMA on the last bar -> flip to long
    closes = [100, 100, 100, 100, 99, 106]
    ev = signals.evaluate(mk(closes), [("price_sma", {"length": 3})])
    v = ev["price_sma"]
    assert v["pos"] == 1 and v["prev"] == 0


def test_state_roundtrip_and_default_config():
    signals._store("TESTSYM", "supertrend", 1, 12345)
    assert signals._state("TESTSYM", "supertrend") == (1, 12345)
    signals._store("TESTSYM", "supertrend", 0, 99999)      # upsert
    assert signals._state("TESTSYM", "supertrend") == (0, 99999)
    cfg = signals.scan_config()
    assert [k for k, _ in cfg] == signals.DEFAULT_SCAN
    signals.set_scan_config([{"key": "ema_cross", "params": {"fast": 21, "slow": 55}}])
    cfg = signals.scan_config()
    assert cfg == [("ema_cross", {"fast": 21, "slow": 55, "signal": 9})] or \
           cfg[0][0] == "ema_cross" and cfg[0][1]["fast"] == 21 and cfg[0][1]["slow"] == 55
    db.set_meta("signals_config", "")                      # back to defaults


def test_scan_flip_notify_and_dedupe():
    closes = [100]*65 + [96, 97, 98, 99, 110]              # crosses SMA20 on last bar
    cand = mk(closes)
    sent = []
    with mock.patch.object(signals, "kite_auth") as ka, \
         mock.patch.object(signals, "candles") as cs, \
         mock.patch.object(signals, "instruments") as ins, \
         mock.patch.object(signals.notify, "send", lambda m, **k: sent.append(m)), \
         mock.patch.object(signals, "drop_forming_bar", lambda c, now=None: c):
        ka.get_kite.return_value = None
        ins.lookup = lambda s: {"instrument_token": 1, "tradingsymbol": s}
        cs.ensure = lambda *a, **k: None
        cs.get = lambda *a, **k: cand
        db.set_meta("signals_config",
                    '{"strategies":[{"key":"price_sma","params":{"length":20}}]}')

        r1 = signals.scan(symbols=["FLIPCO"], push=True)
        assert len(r1["flips"]) == 1 and r1["flips"][0]["action"] == "LONG"
        assert r1["matrix"]["FLIPCO"]["price_sma"] == 1
        assert len(sent) == 1 and "FLIPCO" in sent[0]

        # same bar again -> deduped, nothing sent
        r2 = signals.scan(symbols=["FLIPCO"], push=True)
        assert r2["flips"] == [] and len(sent) == 1

        # next bar closes back under -> flip FLAT notifies once
        cand2 = mk(closes + [90])
        cs.get = lambda *a, **k: cand2
        r3 = signals.scan(symbols=["FLIPCO"], push=True)
        assert len(r3["flips"]) == 1 and r3["flips"][0]["action"] == "FLAT"
        assert len(sent) == 2
    db.set_meta("signals_config", "")


def _scan_env(sent):
    """Context managers that let scan() run offline against supplied candles."""
    return (
        mock.patch.object(signals, "kite_auth"),
        mock.patch.object(signals, "candles"),
        mock.patch.object(signals, "instruments"),
        mock.patch.object(signals.notify, "send", lambda m, **k: sent.append(m)),
        mock.patch.object(signals, "drop_forming_bar", lambda c, now=None: c),
    )


def test_flip_missed_during_downtime_is_still_reported():
    """The bug: flips were detected as pos[-1] != pos[-2], so a flip that
    landed while the watcher was down looked like long-vs-long and vanished."""
    closes = [100]*65 + [96, 97, 98, 99, 110]      # crosses up on the last bar
    sent = []
    ka, cs, ins, nt, dfb = _scan_env(sent)
    with ka as k, cs as c, ins as i, nt, dfb:
        k.get_kite.return_value = None
        i.lookup = lambda s: {"instrument_token": 2, "tradingsymbol": s}
        c.ensure = lambda *a, **kw: None
        db.set_meta("signals_config",
                    '{"strategies":[{"key":"price_sma","params":{"length":20}}]}')

        # Baseline: record a FLAT stance, as a healthy scan would have.
        c.get = lambda *a, **kw: mk(closes[:-1])
        signals.scan(symbols=["GAPCO"], push=True)
        assert signals._state("GAPCO", "price_sma")[0] == 0

        # Watcher now goes down. The flip happens, then THREE more bars pass
        # with the stance unchanged before anyone scans again.
        c.get = lambda *a, **kw: mk(closes + [111, 112, 113])
        sent.clear()
        r = signals.scan(symbols=["GAPCO"], push=True)

        assert len(r["flips"]) == 1, "flip during downtime was lost"
        f = r["flips"][0]
        assert f["action"] == "LONG"
        assert f["bars_ago"] == 3, f"expected 3 stale bars, got {f['bars_ago']}"
        # the price reported is the FLIP's close, not today's
        assert f["price"] == 110 and f["last_price"] == 113
        assert "3 bars ago" in sent[0]
    db.set_meta("signals_config", "")


def test_unchanged_stance_after_downtime_reports_nothing():
    """The mirror case: no flip happened, so a long gap must stay quiet."""
    closes = [100]*65 + [96, 97, 98, 99, 110]
    sent = []
    ka, cs, ins, nt, dfb = _scan_env(sent)
    with ka as k, cs as c, ins as i, nt, dfb:
        k.get_kite.return_value = None
        i.lookup = lambda s: {"instrument_token": 3, "tradingsymbol": s}
        c.ensure = lambda *a, **kw: None
        db.set_meta("signals_config",
                    '{"strategies":[{"key":"price_sma","params":{"length":20}}]}')

        c.get = lambda *a, **kw: mk(closes)
        signals.scan(symbols=["CALMCO"], push=True)      # records LONG
        sent.clear()
        c.get = lambda *a, **kw: mk(closes + [111, 112, 113])
        r = signals.scan(symbols=["CALMCO"], push=True)
        assert r["flips"] == [] and sent == []
    db.set_meta("signals_config", "")


def test_read_only_scan_does_not_consume_the_flip():
    """Checking /signals on the phone must not eat that evening's EOD push."""
    closes = [100]*65 + [96, 97, 98, 99, 110]
    sent = []
    ka, cs, ins, nt, dfb = _scan_env(sent)
    with ka as k, cs as c, ins as i, nt, dfb:
        k.get_kite.return_value = None
        i.lookup = lambda s: {"instrument_token": 4, "tradingsymbol": s}
        c.ensure = lambda *a, **kw: None
        db.set_meta("signals_config",
                    '{"strategies":[{"key":"price_sma","params":{"length":20}}]}')

        c.get = lambda *a, **kw: mk(closes[:-1])
        signals.scan(symbols=["PEEKCO"], push=True)      # baseline FLAT
        c.get = lambda *a, **kw: mk(closes)

        peek = signals.scan(symbols=["PEEKCO"], push=False)   # the /signals path
        assert len(peek["flips"]) == 1
        assert signals._state("PEEKCO", "price_sma")[0] == 0, \
            "a read-only scan advanced the recorded stance"

        sent.clear()
        eod = signals.scan(symbols=["PEEKCO"], push=True)     # the EOD path
        assert len(eod["flips"]) == 1 and len(sent) == 1
        assert signals._state("PEEKCO", "price_sma")[0] == 1  # now recorded
    db.set_meta("signals_config", "")


def test_evaluate_reports_when_the_stance_began():
    closes = [100]*65 + [96, 97, 98, 99, 110, 111, 112]
    ev = signals.evaluate(mk(closes), [("price_sma", {"length": 20})])
    v = ev["price_sma"]
    assert v["pos"] == 1 and v["bars_ago"] == 2
    assert v["since"] == len(closes) - 3


def test_maybe_eod_once_per_day():
    ran = []
    with mock.patch.object(signals, "scan",
                           lambda **k: ran.append(1) or
                           {"matrix": {}, "flips": [], "errors": []}):
        wed_early = datetime(2026, 7, 22, 14, 0)
        wed_late = datetime(2026, 7, 22, 15, 40)
        sat = datetime(2026, 7, 25, 16, 0)
        db.set_meta("signals_last_eod", "")
        assert signals.maybe_eod_scan(now=wed_early) is False   # market open
        assert signals.maybe_eod_scan(now=wed_late) is True     # runs
        assert signals.maybe_eod_scan(now=wed_late) is False    # once per day
        assert signals.maybe_eod_scan(now=sat) is False         # weekend
    assert len(ran) == 1


def test_eod_failure_is_pushed_not_just_printed():
    """The day is marked done before the scan runs, so a failure never retries
    itself. It has to reach the phone or it's lost entirely."""
    pushed = []

    def boom(**k):
        raise RuntimeError("kite session gone")

    with mock.patch.object(signals, "scan", boom), \
         mock.patch.object(signals.notify, "send",
                           lambda msg, **k: pushed.append(msg)):
        db.set_meta("signals_last_eod", "")
        assert signals.maybe_eod_scan(now=datetime(2026, 7, 22, 15, 40)) is True

    assert len(pushed) == 1
    assert "kite session gone" in pushed[0]


def test_eod_failure_push_failure_is_swallowed():
    """A dead network shouldn't take the watcher loop down with it."""
    def boom(**k):
        raise RuntimeError("scan died")

    def no_network(msg, **k):
        raise OSError("no route to host")

    with mock.patch.object(signals, "scan", boom), \
         mock.patch.object(signals.notify, "send", no_network):
        db.set_meta("signals_last_eod", "")
        assert signals.maybe_eod_scan(now=datetime(2026, 7, 22, 15, 40)) is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
