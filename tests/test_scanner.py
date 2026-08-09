"""Offline tests for the EOD scanner and universes — no Kite, no network.

Run:  python -m pytest tests/test_scanner.py
"""
import random
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "scan.db"
config.DB_PATH = _DB

from app import db, scanner, universe  # noqa: E402

db.init()

STORE: dict = {}


def bars(n=400, drift=0.0, seed=1, vol=1000):
    rnd = random.Random(seed)
    c, out = 100.0, []
    for i in range(n):
        c *= 1 + drift + rnd.gauss(0, 0.010)
        w = c * 0.008
        out.append({"time": i * 86400, "open": c, "high": c + w, "low": c - w,
                    "close": c, "volume": vol})
    return out


def flat(n=400, price=100.0, vol=1000):
    return [{"time": i * 86400, "open": price, "high": price * 1.002,
             "low": price * 0.998, "close": price, "volume": vol}
            for i in range(n)]


def append(c, close, high=None, low=None, vol=1000):
    c = list(c)
    c.append({"time": c[-1]["time"] + 86400, "open": close,
              "high": high if high is not None else close * 1.005,
              "low": low if low is not None else close * 0.995,
              "close": close, "volume": vol})
    return c


_next_token = [1]


def seed_universe(mapping, name="test"):
    """mapping: {symbol: candles}.

    Tokens come from a running counter, never restarting at 1. The instruments
    table keys on instrument_token, so reusing tokens across fixtures makes a
    later INSERT OR IGNORE silently keep the EARLIER symbol — and the test then
    scans one symbol's candles under another's name.
    """
    STORE.clear()
    with db.connect() as conn:
        conn.execute("DELETE FROM universe WHERE name=?", (name,))
        for sym, c in mapping.items():
            i = _next_token[0]
            _next_token[0] += 1
            conn.execute("DELETE FROM instruments WHERE tradingsymbol=?", (sym,))
            conn.execute("INSERT OR REPLACE INTO instruments (instrument_token,"
                         " tradingsymbol, name, exchange, instrument_type)"
                         " VALUES (?,?,?,'NSE','EQ')", (i, sym, sym))
            conn.execute("INSERT OR REPLACE INTO universe (name, tradingsymbol,"
                         " exchange, token) VALUES (?,?,'NSE',?)", (name, sym, i))
            STORE[i] = c


def offline():
    return mock.patch.multiple(
        scanner,
        candles=mock.MagicMock(**{"get.side_effect":
                                  lambda tok, iv, d: STORE.get(tok, [])}),
        drop_forming_bar=lambda c, now=None: c)


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    yield


# ------------------------------------------------------------- conditions
def test_52w_high_fires_only_on_a_new_high():
    base = flat(300, 100.0)
    assert scanner.c_52w_high(append(base, 105.0)) is not None
    assert scanner.c_52w_high(append(base, 99.0)) is None


def test_52w_high_reports_how_far_above():
    d = scanner.c_52w_high(append(flat(300, 100.0), 110.0))
    assert d["above_pct"] > 9


def test_volume_surge_needs_the_multiple():
    base = flat(60, 100.0, vol=1000)
    assert scanner.c_volume_surge(append(base, 100.0, vol=2500)) is not None
    assert scanner.c_volume_surge(append(base, 100.0, vol=1500)) is None


def test_volume_surge_multiple_is_configurable():
    base = flat(60, 100.0, vol=1000)
    c = append(base, 100.0, vol=2500)
    assert scanner.c_volume_surge(c, {"mult": 3.0}) is None
    assert scanner.c_volume_surge(c, {"mult": 2.0}) is not None


def test_golden_cross_fires_on_the_cross_not_the_state():
    """A cross is an event. If it fired every day the fast MA was above the
    slow one, the scan would return the same 40 names forever."""
    rise = [{"time": i * 86400, "open": 100 + i * 0.5, "high": 101 + i * 0.5,
             "low": 99 + i * 0.5, "close": 100 + i * 0.5, "volume": 1000}
            for i in range(400)]
    fired = [i for i in range(250, 400)
             if scanner.c_golden_cross(rise[:i]) is not None]
    assert len(fired) <= 2, f"fired {len(fired)} times — that's a state, not a cross"


def test_donchian_break_needs_a_new_high():
    base = flat(60, 100.0)
    assert scanner.c_donchian_break(append(base, 105.0, high=105.5)) is not None
    assert scanner.c_donchian_break(append(base, 100.0)) is None


def test_above_sma_reports_distance():
    d = scanner.c_above_sma(append(flat(250, 100.0), 120.0), {"period": 200})
    assert d and d["above_pct"] > 15


def test_rsi_range_band():
    down = [{"time": i * 86400, "open": 100 - i * 0.5, "high": 100 - i * 0.5,
             "low": 99 - i * 0.5, "close": 100 - i * 0.5, "volume": 1000}
            for i in range(60)]
    assert scanner.c_rsi_range(down, {"low": 0, "high": 40}) is not None
    assert scanner.c_rsi_range(down, {"low": 60, "high": 100}) is None


def test_strategy_long_uses_the_backtester_rules():
    """The bridge that matters: a scan hit must be the same rule the
    backtester trades, not a lookalike."""
    from app import backtest as bt
    c = bars(400, drift=0.002, seed=9)
    spec = bt.STRATEGIES["donchian_breakout"]
    pos = spec["fn"](c, spec["params"])
    hit = scanner.c_strategy_long(c, {"strategy": "donchian_breakout"})
    expected = len(pos) > 1 and pos[-1] == 1 and pos[-2] == 0
    assert bool(hit) == expected


def test_conditions_return_none_on_short_history():
    short = flat(10)
    for key, spec in scanner.CONDITIONS.items():
        assert spec["fn"](short, spec["params"]) is None, key


# ---------------------------------------------------------------- engine
def test_conditions_are_anded():
    """Either alone is a list you can't read; both is a shortlist."""
    hi_only = append(flat(300, 100.0), 110.0, vol=1000)
    both = append(flat(300, 100.0), 110.0, vol=5000)
    assert scanner.evaluate(hi_only, ["52w_high"]) is not None
    assert scanner.evaluate(hi_only, ["52w_high", "volume_surge"]) is None
    assert scanner.evaluate(both, ["52w_high", "volume_surge"]) is not None


def test_unknown_condition_raises():
    with pytest.raises(ValueError, match="unknown condition"):
        scanner.evaluate(flat(300), ["not_a_condition"])


def test_condition_params_pass_through():
    c = append(flat(60, 100.0), 100.0, vol=2500)
    assert scanner.evaluate(c, [{"key": "volume_surge", "mult": 3}]) is None
    assert scanner.evaluate(c, [{"key": "volume_surge", "mult": 2}]) is not None


def test_scan_ranks_strongest_first():
    seed_universe({
        "BIG": append(flat(300, 100.0), 130.0),
        "SMALL": append(flat(300, 100.0), 101.0),
    })
    with offline():
        r = scanner.scan(["52w_high"], name="test")
    assert [h["symbol"] for h in r["hits"]] == ["BIG", "SMALL"]


def test_scan_reports_skipped_symbols():
    """A symbol with no candles must not look like 'no match' — otherwise an
    empty scan can mean nothing was looked at."""
    seed_universe({"GOOD": append(flat(300, 100.0), 110.0), "THIN": flat(5)})
    with offline():
        r = scanner.scan(["52w_high"], name="test")
    assert r["skipped"] == ["THIN"]
    assert r["scanned"] == 1


def test_scan_requires_conditions():
    seed_universe({"A": flat(300)})
    with pytest.raises(ValueError, match="a scan with no filter"):
        scanner.scan([], name="test")


def test_scan_on_an_empty_universe_explains_itself():
    with pytest.raises(ValueError, match="is empty"):
        scanner.scan(["52w_high"], name="doesnotexist")


def test_scan_truncates_and_says_so():
    # +1 so every symbol genuinely exceeds the flat 100.0 prior high
    seed_universe({f"S{i}": append(flat(300, 100.0), 101.0 + i)
                   for i in range(12)})
    with offline():
        r = scanner.scan(["52w_high"], name="test", limit=5)
    assert len(r["hits"]) == 5 and r["truncated"] and r["total_hits"] == 12


def test_scan_accepts_an_explicit_symbol_list():
    seed_universe({"A": append(flat(300, 100.0), 110.0),
                   "B": append(flat(300, 100.0), 110.0)})
    with offline():
        r = scanner.scan(["52w_high"], symbols=["A"])
    assert len(r["hits"]) == 1 and r["universe"] is None


def test_scan_drops_the_forming_bar():
    """Delegated to signals.drop_forming_bar — a scanner that includes today's
    partial candle shows a 52-week high at 10:15 that isn't one at 15:30."""
    import inspect
    assert "drop_forming_bar" in inspect.getsource(scanner.scan)


# -------------------------------------------------------------- universes
def test_set_members_reports_unknown_symbols():
    """A universe quietly 12 names short scans 12 names short forever."""
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token,"
                     " tradingsymbol, name, exchange, instrument_type)"
                     " VALUES (900,'REAL','Real Co','NSE','EQ')")
    r = universe.set_members("u1", ["REAL", "NOTREAL"])
    assert r["added"] == 1 and r["unknown"] == ["NOTREAL"]


def test_set_members_replaces_by_default():
    universe.set_members("u2", ["REAL"])
    universe.set_members("u2", [])
    assert universe.symbols("u2") == []


def test_add_members_appends():
    universe.set_members("u3", ["REAL"])
    universe.add_members("u3", ["REAL"])
    assert universe.symbols("u3") == ["REAL"]


def test_remove_symbol_and_universe():
    universe.set_members("u4", ["REAL"])
    assert universe.remove("u4", "REAL") == 1
    assert universe.symbols("u4") == []


def test_names_counts_members():
    universe.set_members("u5", ["REAL"])
    assert any(n["name"] == "u5" and n["n"] == 1 for n in universe.names())


# --------------------------------------------------------------- backfill
def test_backfill_skips_covered_symbols():
    """What makes a re-run cheap and an interrupted run safe to restart."""
    seed_universe({"HAVE": flat(400), "NEED": flat(400)}, name="bf")
    toks = {m["tradingsymbol"]: m["token"] for m in universe.members("bf")}
    fetched = []
    with mock.patch.object(universe, "kite_auth",
                           mock.MagicMock(**{"get_kite.return_value": object()})), \
         mock.patch.object(universe, "candles", mock.MagicMock(
             **{"get.side_effect": lambda tok, iv, d: (
                 flat(400) if tok == toks["HAVE"] else []),
                "ensure.side_effect": lambda k, tok, iv, d: fetched.append(tok)})):
        r = universe.backfill("bf", pace=0)
    assert r["skipped"] == 1 and r["fetched"] == 1
    assert fetched == [toks["NEED"]]


def test_backfill_continues_past_a_failure():
    seed_universe({"A": [], "B": [], "C": []}, name="bf2")
    bad = {m["tradingsymbol"]: m["token"]
           for m in universe.members("bf2")}["B"]

    def ensure(k, tok, iv, d):
        if tok == bad:
            raise RuntimeError("kite said no")

    with mock.patch.object(universe, "kite_auth",
                           mock.MagicMock(**{"get_kite.return_value": object()})), \
         mock.patch.object(universe, "candles", mock.MagicMock(
             **{"get.return_value": [], "ensure.side_effect": ensure})):
        r = universe.backfill("bf2", pace=0)
    assert r["fetched"] == 2 and len(r["failed"]) == 1
    assert r["failed"][0]["symbol"] == "B"


def test_backfill_without_a_session_is_a_clean_error():
    seed_universe({"A": []}, name="bf3")
    with mock.patch.object(universe.kite_auth, "get_kite",
                           side_effect=SystemExit("No valid Kite session")):
        with pytest.raises(RuntimeError, match="session"):
            universe.backfill("bf3", pace=0)


def test_backfill_on_an_empty_universe_says_so():
    with pytest.raises(ValueError, match="is empty"):
        universe.backfill("nothing_here", pace=0)


def test_coverage_counts_and_estimates():
    seed_universe({"A": flat(400), "B": flat(5)}, name="cov")
    with mock.patch.object(universe, "candles", mock.MagicMock(
            **{"get.side_effect": lambda tok, iv, d: STORE.get(tok, [])})):
        c = universe.coverage("cov")
    assert c["covered"] == 2 and c["thin"] == ["B"]
    assert c["todo"] == 1 and c["est_seconds"] > 0


# ------------------------------------------------------------ saved scans
def test_save_and_run_a_saved_scan():
    seed_universe({"UP": append(flat(300, 100.0), 120.0)}, name="sv")
    universe.save_scan("highs", {"universe": "sv", "conditions": ["52w_high"]})
    with offline():
        r = scanner.run_saved("highs")
    assert r["saved_as"] == "highs" and len(r["hits"]) == 1
    assert universe.get_scan("highs")["last_hits"] == 1


def test_unknown_saved_scan_raises():
    with pytest.raises(ValueError, match="no saved scan"):
        scanner.run_saved("nope")


def test_delete_saved_scan():
    universe.save_scan("tmp", {"conditions": ["52w_high"]})
    assert universe.delete_scan("tmp") is True
    assert universe.get_scan("tmp") is None


# ---------------------------------------------------------------- render
def test_as_text_explains_an_empty_result():
    seed_universe({"THIN": flat(5)}, name="t2")
    with offline():
        out = scanner.as_text(scanner.scan(["52w_high"], name="t2"))
    assert "Nothing matched" in out and "backfill" in out


def test_as_text_is_phone_sized():
    seed_universe({f"S{i}": append(flat(300, 100.0), 101.0 + i)
                   for i in range(20)}, name="t3")
    with offline():
        out = scanner.as_text(scanner.scan(["52w_high"], name="t3"), limit=8)
    assert len(out.splitlines()) < 18


# ------------------------------------------------------------------- API
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

client = TestClient(server.app)


def test_conditions_route_lists_every_condition():
    d = client.get("/api/scanner/conditions").json()
    assert {c["key"] for c in d["conditions"]} == set(scanner.CONDITIONS)


def test_scan_route():
    seed_universe({"UP": append(flat(300, 100.0), 120.0)}, name="api")
    with offline():
        r = client.get("/api/scanner",
                       params={"conditions": '["52w_high"]',
                               "universe_name": "api"})
    assert r.status_code == 200 and len(r.json()["hits"]) == 1


def test_scan_route_rejects_bad_json():
    r = client.get("/api/scanner", params={"conditions": "not json"})
    assert r.status_code == 400


def test_scan_route_rejects_an_unknown_condition():
    seed_universe({"A": flat(300)}, name="api2")
    r = client.get("/api/scanner", params={"conditions": '["nope"]',
                                           "universe_name": "api2"})
    assert r.status_code == 400


def test_universe_routes_round_trip():
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO instruments (instrument_token,"
                     " tradingsymbol, name, exchange, instrument_type)"
                     " VALUES (99001,'APIX','Api Co','NSE','EQ')")
    r = client.post("/api/universe/apitest", params={"symbols": "APIX"}).json()
    assert r["added"] == 1
    assert client.get("/api/universe/apitest").json()["members"][0][
        "tradingsymbol"] == "APIX"
    assert client.delete("/api/universe/apitest").json()["removed"] == 1


def test_backfill_route_401s_without_a_session():
    seed_universe({"BF": []}, name="bfapi")
    with mock.patch.object(universe.kite_auth, "get_kite",
                           side_effect=SystemExit("No valid Kite session")):
        r = client.post("/api/universe/bfapi/backfill")
    assert r.status_code == 401


def test_saved_scan_routes():
    seed_universe({"S": append(flat(300, 100.0), 120.0)}, name="svapi")
    spec = '{"universe":"svapi","conditions":["52w_high"]}'
    assert client.post("/api/scans/apiscan", params={"spec": spec}).status_code == 200
    with offline():
        r = client.post("/api/scans/apiscan/run")
    assert r.status_code == 200 and r.json()["saved_as"] == "apiscan"
    assert client.delete("/api/scans/apiscan").json()["deleted"] is True


def test_running_an_unknown_saved_scan_404s():
    assert client.post("/api/scans/ghost/run").status_code == 404


def test_scanner_page_is_wired():
    html = client.get("/scanner").text
    for needle in ("/api/scanner/conditions", "/api/scanner?", "loadCoverage",
                   "runBackfill", "conditionSpec"):
        assert needle in html, f"page missing {needle}"


def test_scanner_tab_on_every_page():
    for path in ("/", "/desk", "/backtest", "/journal", "/portfolio"):
        assert 'href="/scanner"' in client.get(path).text, path


def test_unknown_condition_is_caught_before_any_work():
    """A typo must not hide behind 'every symbol was skipped'."""
    seed_universe({"THIN": flat(5)}, name="early")
    with offline():
        with pytest.raises(ValueError, match="unknown condition"):
            scanner.scan(["nope"], name="early")


# ------------------------------------------------------------- EOD push
def test_eod_pushes_only_scans_marked_eod():
    """A scanner that pushes everything nightly is one you mute."""
    from app import signals
    seed_universe({"UP": append(flat(300, 100.0), 130.0)}, name="eodu")
    universe.save_scan("opted-in", {"universe": "eodu",
                                    "conditions": ["52w_high"], "eod": True})
    universe.save_scan("opted-out", {"universe": "eodu",
                                     "conditions": ["52w_high"]})
    pushed = []
    with offline(), mock.patch.object(signals.notify, "send",
                                      lambda m, **k: pushed.append(k.get("title"))):
        n = signals._push_saved_scans()
    assert n == 1 and pushed == ["Scan: opted-in"]
    universe.delete_scan("opted-in")
    universe.delete_scan("opted-out")


def test_eod_skips_scans_with_no_hits():
    from app import signals
    seed_universe({"FLAT": flat(300)}, name="eodq")
    universe.save_scan("quiet", {"universe": "eodq",
                                 "conditions": ["52w_high"], "eod": True})
    pushed = []
    with offline(), mock.patch.object(signals.notify, "send",
                                      lambda m, **k: pushed.append(m)):
        assert signals._push_saved_scans() == 0
    assert pushed == []
    universe.delete_scan("quiet")


def test_a_failing_scan_does_not_cost_you_the_flips():
    """The flips are what you trade; a scanner problem must not take them."""
    from app import signals
    universe.save_scan("broken", {"universe": "nonexistent",
                                  "conditions": ["52w_high"], "eod": True})
    with mock.patch.object(signals.notify, "send", lambda m, **k: None):
        assert signals._push_saved_scans() == 0      # logged, not raised
    universe.delete_scan("broken")
