"""API tests for the risk + journal endpoints, through the real FastAPI app.

Run:  python -m pytest tests/test_journal_api.py
"""
import tempfile
from pathlib import Path

import pytest
from unittest import mock

from app import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "api.db"
config.ACCOUNT_SIZE = 500_000
config.RISK_PCT = 1
config.DAILY_LOSS_R = 2
config.MAX_POSITIONS = 5
config.MAX_POSITION_PCT = 100

from app import db  # noqa: E402

db.init()

pytest.importorskip("fastapi")
# TestClient needs httpx, which is a test-only dependency and not in
# requirements.txt. Skip rather than error on a machine that only has runtime
# deps installed.
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

# The host guard only answers loopback; TestClient defaults to "testserver".
client = TestClient(server.app, base_url="http://127.0.0.1:5000")


def clear():
    with db.connect() as c:
        c.execute("DELETE FROM trades")


def setup_function():
    clear()


def open_trade(**kw):
    params = {"symbol": "BEL", "entry": 405, "qty": 714, "stop": 398}
    params.update(kw)
    r = client.post("/api/journal", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------- risk API
def test_size_endpoint():
    r = client.get("/api/risk/size", params={"entry": 405, "stop": 398}).json()
    assert r["qty"] == 714
    assert r["targets"]["2R"] == 419.0


def test_size_endpoint_uses_config_defaults():
    a = client.get("/api/risk/size", params={"entry": 405, "stop": 398}).json()
    b = client.get("/api/risk/size",
                   params={"entry": 405, "stop": 398, "account": 500_000,
                           "risk_pct": 1}).json()
    assert a["qty"] == b["qty"]


def test_size_endpoint_rejects_bad_input():
    r = client.get("/api/risk/size", params={"entry": 405, "stop": 405})
    assert r.status_code == 400
    assert "undefined risk" in r.json()["detail"]


def test_size_endpoint_writes_nothing():
    client.get("/api/risk/size", params={"entry": 405, "stop": 398})
    assert client.get("/api/journal").json()["open"] == []


# ------------------------------------------------------------ journal API
def test_open_close_roundtrip():
    t = open_trade(tag="st_adx")
    assert t["initial_stop"] == 398
    r = client.post(f"/api/journal/{t['id']}/close",
                    params={"exit_price": 419, "reason": "target",
                            "charges": 0}).json()
    assert abs(r["r_multiple"] - 2.0) < 0.02


def test_trail_keeps_r_basis():
    t = open_trade()
    p = client.patch(f"/api/journal/{t['id']}", params={"stop": 411}).json()
    assert p["stop"] == 411 and p["initial_stop"] == 398
    r = client.post(f"/api/journal/{t['id']}/close",
                    params={"exit_price": 419, "charges": 0}).json()
    assert abs(r["r_multiple"] - 2.0) < 0.02      # not 8R off the trailed stop


def test_double_close_is_400():
    t = open_trade()
    client.post(f"/api/journal/{t['id']}/close", params={"exit_price": 419})
    r = client.post(f"/api/journal/{t['id']}/close", params={"exit_price": 420})
    assert r.status_code == 400 and "already closed" in r.json()["detail"]


def test_bad_side_stop_is_400():
    r = client.post("/api/journal", params={"symbol": "BEL", "entry": 405,
                                            "qty": 10, "stop": 412,
                                            "side": "long"})
    assert r.status_code == 400 and "side=short" in r.json()["detail"]


def test_delete_trade():
    t = open_trade()
    assert client.delete(f"/api/journal/{t['id']}").json()["deleted"] is True
    assert client.get("/api/journal").json()["open"] == []


def test_journal_payload_shape():
    open_trade(tag="st_adx")
    j = client.get("/api/journal").json()
    assert set(j) >= {"open", "closed", "stats", "by_tag", "guard", "settings"}
    assert j["guard"]["open_risk_r"] == 1.0
    assert j["settings"]["risk_pct"] == 1


def test_guard_blocks_after_two_stops():
    for _ in range(2):
        t = open_trade(entry=400, stop=390, qty=10)
        client.post(f"/api/journal/{t['id']}/close",
                    params={"exit_price": 390, "charges": 0})
    g = client.get("/api/journal").json()["guard"]
    assert not g["ok"] and g["realised_r"] == -2.0


def test_compare_route_survives_no_kite_session():
    """The comparison runs backtests, which need candles. Without a session it
    must degrade to a note on the row rather than 500 the whole page.

    Specifically: backtest.run calls config.require, which raises SystemExit —
    a BaseException, so an `except Exception` in the route would not catch it.
    No session is the normal state every morning before login.
    """
    t = open_trade(tag="st_adx")
    client.post(f"/api/journal/{t['id']}/close",
                params={"exit_price": 419, "charges": 0})
    with mock.patch("app.kite_auth.get_kite",
                    side_effect=SystemExit("Missing config: KITE_API_KEY")):
        r = client.get("/api/journal/compare")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["tag"] == "st_adx"
    assert row["live"]["trades"] == 1
    assert row["note"]                      # explains why the backtest side is empty
    assert "caveat" in body


def test_compare_warns_on_small_samples():
    """Expectancy over a handful of trades is barely a number; say so."""
    t = open_trade(tag="st_adx")
    client.post(f"/api/journal/{t['id']}/close",
                params={"exit_price": 419, "charges": 0})
    row = client.get("/api/journal/compare").json()["rows"][0]
    assert "too few" in row["note"]


def test_compare_ignores_untagged_trades():
    t = open_trade()                          # no tag
    client.post(f"/api/journal/{t['id']}/close",
                params={"exit_price": 419, "charges": 0})
    assert client.get("/api/journal/compare").json()["rows"] == []


# ---------------------------------------------------------------- routing
def test_compare_is_not_shadowed_by_the_id_route():
    """/api/journal/compare must not be parsed as trade id 'compare'."""
    r = client.get("/api/journal/compare")
    assert r.status_code == 200


def test_journal_page_served():
    assert client.get("/journal").status_code == 200


def test_journal_tab_present_on_every_page():
    for path in ("/", "/desk", "/backtest", "/valuation", "/results",
                 "/metrics", "/news", "/actions", "/fundamentals"):
        assert 'href="/journal"' in client.get(path).text, f"missing nav on {path}"
