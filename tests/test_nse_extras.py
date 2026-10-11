"""Delivery volume, insider trades, pre-open gaps, market regimes and the
pre-trade checklist.

Everything network-facing is driven through stub clients; the maths is
tested on synthetic bars.
"""
import json
import math
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

from app import config, db, journal, query, scanner
from app import regime
from app import delivery


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init()
    yield


# ----------------------------------------------------------------- delivery
BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
BEL, EQ, 10-Oct-2026, 400.00, 401.00, 410.00, 399.00, 408.00, 408.50, 405.12, 1000000, 4051.2, 20000, 600000, 60.00
BEL, BE, 10-Oct-2026, 400.00, 401.00, 410.00, 399.00, 408.00, 408.50, 405.12, 10, 0.1, 2, 10, 100.00
TCS, EQ, 10-Oct-2026, 3500.00, 3500.00, 3510.00, 3490.00, 3505.00, 3505.00, 3501.00, 200000, 7002.0, 9000, -, -
"""


def test_bhavcopy_parse_keeps_eq_and_strips_padding():
    day, rows = delivery.parse(BHAV)
    assert day == "2026-10-10"
    # BE is trade-to-trade (always 100%); TCS has no delivery figure
    assert [r["symbol"] for r in rows] == ["BEL"]
    r = rows[0]
    assert r["volume"] == 1_000_000 and r["deliv_qty"] == 600_000
    assert r["deliv_pct"] == 60.0 and r["close"] == 408.5


def test_bhavcopy_header_change_is_loud():
    with pytest.raises(ValueError):
        delivery.parse("SYMBOL,SERIES,WHATEVER\nBEL,EQ,1\n")


def _seed_delivery(symbol, qtys, closes=None, start=date(2026, 9, 1)):
    d = start
    for i, q in enumerate(qtys):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = (closes or [100 + i] * len(qtys))[i]
        delivery.store(d.isoformat(), [{"symbol": symbol, "close": close,
                                        "volume": q * 2, "deliv_qty": q,
                                        "deliv_pct": 50.0}])
        d += timedelta(days=1)
    return d - timedelta(days=1)


def test_ratio_is_against_the_prior_average_not_including_today():
    _seed_delivery("BEL", [100] * 20 + [300])
    s = delivery.stats("BEL")
    assert s["avg_deliv_qty"] == 100
    assert s["deliv_ratio"] == 3.0 and s["spike"]
    assert s["change_pct"] > 0


def test_many_skips_symbols_missing_from_the_latest_file():
    _seed_delivery("BEL", [100] * 21)
    _seed_delivery("OLD", [100] * 5)            # stopped trading early
    got = delivery.many(["BEL", "OLD"])
    assert "BEL" in got and "OLD" not in got


def test_sync_skips_weekends_and_remembers_holidays():
    asked = []

    class Client:
        def get_text(self, url):
            asked.append(url)
            return None                          # every day a "holiday"

    today = date(2026, 10, 12)                   # a Monday
    delivery.sync(days=6, client=Client(), today=today)
    n = len(asked)
    assert n == 5                                # Tue..Fri + Mon, no Sat/Sun
    delivery.sync(days=6, client=Client(), today=today)
    assert len(asked) == n + 1                   # only today is asked again


def test_scanner_delivery_spike_needs_same_session_and_an_up_day():
    last = _seed_delivery("BEL", [100] * 20 + [300])
    t = int(datetime.combine(last, datetime.min.time()).timestamp()) + 3600
    c = [{"time": t - 86400 * (70 - i), "open": 100, "high": 101, "low": 99,
          "close": 100.0, "volume": 1} for i in range(70)]
    c[-1] = {**c[-1], "time": t, "close": 102.0}
    hit = scanner.evaluate(c, ["delivery_spike"], symbol="BEL")
    assert hit and hit["delivery_spike"]["times"] == 3.0
    # down day: no hit
    c[-1] = {**c[-1], "close": 98.0}
    assert scanner.evaluate(c, ["delivery_spike"], symbol="BEL") is None
    # stale: delivery file is for a different session than the last bar
    c[-1] = {**c[-1], "close": 102.0, "time": t + 86400 * 3}
    assert scanner.evaluate(c, ["delivery_spike"], symbol="BEL") is None


def test_query_delivery_fields_are_scalars():
    assert query.DELIVERY_FIELDS <= query.SCALAR_FIELDS
    bars = [{"time": i * 86400, "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 1} for i in range(30)]
    node = query.parse("deliv_ratio > 2 and deliv_pct > 40")
    assert query.evaluate(node, bars, {"deliv_ratio": 3.1, "deliv_pct": 55})
    assert not query.evaluate(node, bars, {"deliv_ratio": 1.2, "deliv_pct": 55})
    # unknown stays "no", never zero
    assert not query.evaluate(query.parse("deliv_ratio < 5"), bars, {})


# ------------------------------------------------------------------- regime
def _bars(closes, spread=1.0):
    return [{"time": 1_700_000_000 + i * 86400, "open": c, "high": c + spread,
             "low": c - spread, "close": c, "volume": 1}
            for i, c in enumerate(closes)]


def test_regime_trend_and_chop():
    up = _bars([100 + i * 1.5 for i in range(300)])
    assert regime.classify(up)[-1] == "trend-up"
    down = _bars([600 * 0.995 ** i for i in range(300)])
    assert regime.classify(down)[-1] == "trend-down"
    chop = _bars([100 + (1 if i % 2 else -1) * 0.5 for i in range(300)])
    assert regime.classify(chop)[-1] == "choppy"


def test_regime_high_vol_overrides_trend():
    closes = [100 + i * 0.5 for i in range(280)]
    closes += [closes[-1] * (1.06 if i % 2 else 0.95) for i in range(20)]
    assert regime.classify(_bars(closes))[-1] == "high-vol"


def test_regime_labels_have_no_lookahead():
    idx = _bars([100 + i for i in range(200)])
    labels = regime.classify(idx)
    # a stock bar on the same day as index bar k reads bar k-1's label
    k = 150
    out = regime.labels_for([idx[k]["time"]], idx)
    assert out == [labels[k - 1]]


def test_split_and_verdict():
    times = [1, 2, 3, 4]
    labels = ["trend-up", "trend-up", "choppy", "choppy"]
    trades = ([{"entry_time": 1, "net_pct": 2.0, "bars": 1}] * 6 +
              [{"entry_time": 3, "net_pct": -1.0, "bars": 1}] * 6)
    from app import backtest
    split = regime.split_trades([dict(t) for t in trades], times, labels,
                                backtest._trade_stats)
    assert split["trend-up"]["expectancy_pct"] == 2.0
    assert split["choppy"]["expectancy_pct"] == -1.0
    assert split["trend-up"]["share_pct"] == 50.0
    v = regime.verdict(split)
    assert "switching it off in choppy" in v


