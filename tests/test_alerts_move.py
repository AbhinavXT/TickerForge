"""Tests for moving a price alert — what dragging its line on the chart does.

Run:  python -m pytest tests/test_alerts_move.py
"""
import json
import tempfile
from pathlib import Path

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "alerts.db"
config.DB_PATH = _DB

from app import alerts, db  # noqa: E402

db.init()


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    with db.connect() as c:
        c.execute("DELETE FROM alerts")
    yield


def arm(kind="price_gt", **params):
    return alerts.add("BEL", 1, kind, {"x": 410, **params})


def row(aid):
    with db.connect() as c:
        return dict(c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())


def test_set_level_moves_the_price():
    aid = arm()
    out = alerts.set_level(aid, 420)
    assert json.loads(out["params"])["x"] == 420


def test_id_survives_the_move():
    """The alert keeps its place in /alerts and any held reference stays valid."""
    aid = arm()
    assert alerts.set_level(aid, 420)["id"] == aid


def test_move_clears_cross_state():
    """State tracks which side of the level price was on, so a cross fires
    once. Move the level without clearing it and the alert compares against
    the OLD level — it can fire instantly or miss a real cross."""
    aid = arm()
    with db.connect() as c:
        c.execute("UPDATE alerts SET state=? WHERE id=?",
                  (json.dumps({"was_above": True}), aid))
    alerts.set_level(aid, 420)
    assert row(aid)["state"] == "{}"


def test_move_rearms_a_triggered_alert():
    aid = arm()
    with db.connect() as c:
        c.execute("UPDATE alerts SET active=0, triggered_at='2026-07-01', "
                  "reason='hit' WHERE id=?", (aid,))
    alerts.set_level(aid, 420)
    r = row(aid)
    assert r["active"] == 1 and r["triggered_at"] is None


def test_stale_label_is_dropped():
    """A cached label saying '> 410' must not survive a move to 420."""
    aid = alerts.add("BEL", 1, "price_gt", {"x": 410, "label": "> 410"})
    alerts.set_level(aid, 420)
    assert "label" not in json.loads(row(aid)["params"])
    assert "420" in alerts.label(row(aid))


def test_move_refuses_indicator_alerts():
    """An RSI alert has no single price to drag — refuse rather than corrupt."""
    aid = alerts.add("BEL", 1, "rsi_gt", {"level": 70, "tf": "5minute"})
    with pytest.raises(ValueError, match="no single price"):
        alerts.set_level(aid, 420)


def test_move_unknown_alert_returns_none():
    assert alerts.set_level(9999, 420) is None


# --------------------------------------------------------------- retarget
def test_retarget_flips_to_below_when_dragged_under_spot():
    """A '> 410' line dragged to 390 while price is 400 must become a break
    DOWN — leaving it as greater-than would fire the instant it's set."""
    aid = arm("price_gt")
    alerts.retarget(aid, 390, last_price=400)
    r = row(aid)
    assert r["kind"] == "price_lt"
    assert json.loads(r["params"])["x"] == 390


def test_retarget_flips_to_above_when_dragged_over_spot():
    aid = arm("price_lt")
    alerts.retarget(aid, 415, last_price=400)
    assert row(aid)["kind"] == "price_gt"


def test_retarget_leaves_direction_alone_without_a_price():
    """No last_price means the caller doesn't know the market — don't guess."""
    aid = arm("price_gt")
    alerts.retarget(aid, 390)
    assert row(aid)["kind"] == "price_gt"


def test_retarget_keeps_direction_when_side_is_unchanged():
    aid = arm("price_gt")
    alerts.retarget(aid, 430, last_price=400)
    assert row(aid)["kind"] == "price_gt"


def test_retarget_unknown_alert_returns_none():
    assert alerts.retarget(9999, 420, last_price=400) is None


# ------------------------------------------------------------------ API
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

client = TestClient(server.app)


def test_patch_route_moves_the_alert():
    aid = arm()
    r = client.patch(f"/api/alerts/{aid}", params={"price": 425})
    assert r.status_code == 200
    assert r.json()["price"] == 425
    assert json.loads(row(aid)["params"])["x"] == 425


def test_patch_route_flips_direction_with_last_price():
    aid = arm("price_gt")
    client.patch(f"/api/alerts/{aid}", params={"price": 390, "last_price": 400})
    assert row(aid)["kind"] == "price_lt"


def test_patch_route_rejects_indicator_alerts():
    aid = alerts.add("BEL", 1, "rsi_gt", {"level": 70})
    r = client.patch(f"/api/alerts/{aid}", params={"price": 420})
    assert r.status_code == 400 and "no single price" in r.json()["detail"]


def test_patch_route_404s_on_unknown_alert():
    assert client.patch("/api/alerts/999999", params={"price": 420}).status_code == 404


def test_delete_route_accepts_a_numeric_id():
    """The chart's remove button passes the raw id."""
    aid = arm()
    assert client.delete(f"/api/alerts/{aid}").json()["removed"] == 1


def test_chart_page_wires_the_alert_layer():
    """The page must actually call the functions, not just define them —
    a defined-but-uncalled hook is the classic way this silently ships dead."""
    html = client.get("/").text
    for needle in ("initChartAlerts()", "refreshAlertLines()",
                   "createPriceLine", "removePriceLine",
                   'addEventListener("contextmenu"', "coordinateToPrice"):
        assert needle in html, f"chart page missing {needle}"
