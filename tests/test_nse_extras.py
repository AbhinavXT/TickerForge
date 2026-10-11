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


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init()
    yield


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


