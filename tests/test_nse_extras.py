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
from app import insider
from app import preopen
from app import checklist


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


# ------------------------------------------------------------------ insider
def rec(**kw):
    base = {"symbol": "BEL", "acqName": "Promoter Holdings Ltd",
            "personCategory": "Promoter Group", "tdpTransactionType": "Buy",
            "acqMode": "Market Purchase", "secAcq": "100000",
            "secVal": "40000000", "befAcqSharesPer": "51.2",
            "afterAcqSharesPer": "51.3", "acqfromDt": "08-Oct-2026",
            "acqtoDt": "09-Oct-2026", "intimDt": "10-Oct-2026"}
    base.update(kw)
    return base


@pytest.mark.parametrize("txn,mode,kind", [
    ("Buy", "Market Purchase", "buy"),
    ("Sell", "Market Sale", "sell"),
    ("Pledge Creation", "", "pledge"),
    ("Revokation of Pledge", "", "revoke"),
    ("Sell", "Invocation of pledge", "invoke"),
    ("", "ESOP", "other"),
])
def test_kind_classification(txn, mode, kind):
    assert insider._kind(txn, mode) == kind


def test_normalize_reads_numbers_and_dates():
    ev = insider.normalize(rec())
    assert ev["category"] == "promoter" and ev["kind"] == "buy"
    assert ev["value"] == 4e7 and ev["qty"] == 1e5
    assert ev["disclosed"] == "2026-10-10"
    assert insider.is_market(ev)
    assert not insider.is_market({"mode": "Off Market"})


def test_push_rule():
    big = insider.normalize(rec())
    small = insider.normalize(rec(secVal="500000"))
    sale = insider.normalize(rec(tdpTransactionType="Sell", acqMode="Market Sale"))
    invoke = insider.normalize(rec(acqMode="Invocation of pledge"))
    other = insider.normalize(rec(personCategory="Designated Employee"))
    assert insider.worth_pushing(big, followed=False)
    assert not insider.worth_pushing(small, followed=False)
    assert insider.worth_pushing(small, followed=True)
    assert not insider.worth_pushing(sale, followed=False)
    assert insider.worth_pushing(sale, followed=True)
    assert insider.worth_pushing(invoke, followed=False)
    assert not insider.worth_pushing(other, followed=True)


class _Feed:
    def __init__(self, rows):
        self.rows = rows

    def get_json(self, url):
        return {"data": self.rows}


def test_first_poll_seeds_silently_then_pushes_new():
    sent = []
    with mock.patch.object(insider.notify, "send",
                           side_effect=lambda *a, **k: sent.append((a, k))):
        r = insider.poll_once(_Feed([rec()]), followed={"BEL": {"watchlist"}})
        assert r["note"].startswith("seeded") and not sent
        r = insider.poll_once(_Feed([rec(), rec(intimDt="11-Oct-2026")]),
                              followed={"BEL": {"watchlist"}})
    assert r["new"] == 1 and len(sent) == 1
    assert "BEL" in sent[0][1]["title"]


def test_empty_market_feed_falls_back_to_followed_symbols(monkeypatch):
    monkeypatch.setattr(insider._time, "sleep", lambda s: None)
    asked = []

    class Feed:
        def get_json(self, url):
            asked.append(url)
            return {"data": [rec()] if "symbol=BEL" in url else []}

    r = insider.poll_once(Feed(), followed={"BEL": {"watchlist"}})
    assert r["fallback"] and r["new"] == 1
    n = len(asked)
    r = insider.poll_once(Feed(), followed={"BEL": {"watchlist"}})
    assert not r["fallback"] and len(asked) == n + 1   # hourly, not every poll


def test_net_and_checklist_insider_warning():
    insider.poll_once(_Feed([]), followed={})        # seed
    with mock.patch.object(insider.notify, "send"):
        insider.poll_once(_Feed([rec(tdpTransactionType="Sell",
                                     acqMode="Market Sale",
                                     intimDt=date.today().strftime("%d-%b-%Y"))]),
                          followed={})
    n = insider.net("BEL", days=90)
    assert n["value"] < 0 and n["sold"]
    assert "net sold" in checklist.check_insider("BEL", "long")
    assert checklist.check_insider("BEL", "short") is None


# ------------------------------------------------------------------ preopen
PRE = {"data": [
    {"metadata": {"symbol": "BEL", "iep": 380.0, "previousClose": 400.0},
     "detail": {"preOpenMarket": {"totalBuyQuantity": 10, "totalSellQuantity": 90}}},
    {"metadata": {"symbol": "TCS", "lastPrice": 3510.0, "previousClose": 3500.0},
     "detail": {"preOpenMarket": {}}},
    {"metadata": {"symbol": "BAD"}},
]}


def test_preopen_parse():
    p = preopen.parse(PRE)
    assert set(p) == {"BEL", "TCS"}
    assert p["BEL"]["gap_pct"] == -5.0 and p["BEL"]["sell_qty"] == 90


def test_preopen_flags_gap_through_stop_and_big_gaps():
    prices = preopen.parse(PRE)
    followed = {"BEL": {"position"}, "TCS": {"watchlist"}, "INFY": {"holding"}}
    positions = [{"id": 7, "symbol": "BEL", "side": "long", "entry": 405.0,
                  "stop": 390.0, "initial_stop": 390.0}]
    res = preopen.assess(prices, followed, positions,
                         atrs={"BEL": 8.0, "TCS": 40.0}, gap_atr=1.0)
    assert [r["symbol"] for r in res["through_stop"]] == ["BEL"]
    assert res["through_stop"][0]["r_at_open"] == pytest.approx(-25 / 15, abs=0.01)
    assert [r["symbol"] for r in res["gaps"]] == ["BEL"]      # -2.5 ATR; TCS 0.25
    assert res["missing"] == ["INFY"]
    text = preopen.as_text(res)
    assert "THROUGH YOUR STOP" in text and "#7" in text


def test_preopen_short_through_stop():
    prices = {"X": {"iep": 110.0, "prev_close": 100.0, "gap_pct": 10.0}}
    res = preopen.assess(prices, {"X": {"position"}},
                         [{"id": 1, "symbol": "X", "side": "short",
                           "entry": 100.0, "stop": 105.0}], {"X": 2.0})
    assert res["through_stop"]


def test_preopen_sends_once_per_day_in_window(monkeypatch):
    calls = []
    monkeypatch.setattr(preopen, "check", lambda client=None: calls.append(1) or
                        {"through_stop": [], "gaps": []})
    mon = datetime(2026, 10, 12, 9, 9)
    assert not preopen.maybe_send(datetime(2026, 10, 12, 9, 5))
    assert preopen.maybe_send(mon)
    assert not preopen.maybe_send(mon + timedelta(minutes=1))
    assert not preopen.maybe_send(datetime(2026, 10, 11, 9, 9))   # Sunday
    assert calls == [1]


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


# ---------------------------------------------------------------- checklist
def test_rr_check():
    assert checklist.check_rr(100, 95, 105, "long").startswith("reward:risk 1.00")
    assert checklist.check_rr(100, 95, 110, "long") is None
    assert checklist.check_rr(100, 105, 90, "short") is None
    assert checklist.check_rr(100, 95, None, "long") is None


def test_regime_check():
    assert checklist.check_regime("long", {"label": "trend-down"})[0]
    assert checklist.check_regime("long", {"label": "choppy",
                                           "above_200": False})[0]
    assert checklist.check_regime("long", {"label": "trend-up",
                                           "above_200": True}) == (None, None)
    assert checklist.check_regime("short", {"label": "trend-up"})[0]
    assert checklist.check_regime("long", {"label": "high-vol"})[1]


def test_correlation_maths():
    a = {f"d{i}": math.sin(i) for i in range(40)}
    assert checklist.correlation(a, a) == pytest.approx(1.0)
    neg = {k: -v for k, v in a.items()}
    assert checklist.correlation(a, neg) == pytest.approx(-1.0)
    assert checklist.correlation(a, {"d1": 1.0}) is None   # too little overlap


def test_run_never_raises_and_collects(monkeypatch):
    monkeypatch.setattr(checklist, "check_results",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(checklist, "check_correlated", lambda *a: "moves with X (0.90)")
    monkeypatch.setattr(checklist, "check_insider", lambda *a: None)
    w = checklist.run("BEL", 100, 95, "long", 102, opens=[],
                      reg={"label": "trend-down"})
    keys = [x["key"] for x in w]
    assert keys == ["low_rr", "against_regime", "correlated"]


def test_report_compares_flagged_with_clean():
    def trade(r, checks):
        tid = journal.open_trade("BEL", entry=100, qty=1, stop=99,
                                 checks=None if checks is None else json.dumps(checks))
        journal.close_trade(tid, exit_price=100 + r, charges=0)

    for _ in range(3):
        trade(2, [])
        trade(-1, [{"key": "results_soon", "detail": "x"}])
    trade(5, None)                                   # pre-checklist: ignored
    rep = checklist.report()
    assert rep["checked"] == 6 and rep["unchecked"] == 1
    assert rep["clean"]["expectancy_r"] == pytest.approx(2.0, abs=0.05)
    row = next(r for r in rep["checks"] if r["key"] == "results_soon")
    assert row["flagged"]["trades"] == 3
    assert row["cost_r"] == pytest.approx(-3.0, abs=0.1)
    assert "Results due soon" in checklist.report_text(rep)


def test_open_trade_stores_checks():
    tid = journal.open_trade("BEL", entry=100, qty=1, stop=99,
                             checks=checklist.encode([{"key": "low_rr",
                                                       "label": "", "detail": "d"}]))
    assert checklist.decode(journal.get(tid)["checks"])[0]["key"] == "low_rr"
