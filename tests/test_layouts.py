"""Tests for saved chart layouts.

Run:  python -m pytest tests/test_layouts.py
"""
import json
import tempfile
from pathlib import Path

import pytest

from app import config

_DB = Path(tempfile.mkdtemp()) / "lay.db"
config.DB_PATH = _DB

from app import db  # noqa: E402

db.init()

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402

# The host guard only answers loopback; TestClient defaults to "testserver".
client = TestClient(server.app, base_url="http://127.0.0.1:5000")

SPEC = {"interval": "day", "range": "1y", "events": ["st_flip", "rsi_ob"],
        "indicators": [{"key": "super", "p": {"length": 10, "mult": 3}},
                       {"key": "rsi", "p": {"length": 14},
                        "s": {"colors": {"RSI": "#ff0000"}}}]}


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH = _DB
    with db.connect() as c:
        c.execute("DELETE FROM chart_layouts")
    yield


def save(name="mine", spec=None, default=False):
    p = {"spec": json.dumps(spec or SPEC)}
    if default:
        p["is_default"] = "true"
    return client.post(f"/api/layouts/{name}", params=p)


def test_save_and_list():
    assert save().status_code == 200
    d = client.get("/api/layouts").json()
    assert len(d["layouts"]) == 1
    assert d["layouts"][0]["name"] == "mine"


def test_spec_round_trips_intact():
    """Indicator params and per-instance style overrides must survive — a
    layout that loses your colours isn't a saved layout."""
    save()
    got = client.get("/api/layouts").json()["layouts"][0]["spec"]
    assert got == SPEC
    assert got["indicators"][1]["s"]["colors"]["RSI"] == "#ff0000"


def test_events_are_part_of_the_layout():
    save()
    got = client.get("/api/layouts").json()["layouts"][0]["spec"]
    assert got["events"] == ["st_flip", "rsi_ob"]


def test_saving_the_same_name_overwrites():
    save("mine")
    save("mine", {**SPEC, "events": []})
    d = client.get("/api/layouts").json()
    assert len(d["layouts"]) == 1
    assert d["layouts"][0]["spec"]["events"] == []


def test_only_one_default_at_a_time():
    """Two defaults would make startup non-deterministic."""
    save("a", default=True)
    save("b", default=True)
    rows = {l["name"]: l["is_default"] for l in
            client.get("/api/layouts").json()["layouts"]}
    assert rows == {"a": 0, "b": 1}


def test_default_is_optional():
    save("a")
    assert client.get("/api/layouts").json()["layouts"][0]["is_default"] == 0


def test_bad_spec_rejected():
    r = client.post("/api/layouts/x", params={"spec": "not json"})
    assert r.status_code == 400
    r = client.post("/api/layouts/x", params={"spec": '["a","list"]'})
    assert r.status_code == 400


def test_delete():
    save("gone")
    assert client.delete("/api/layouts/gone").json()["deleted"] is True
    assert client.get("/api/layouts").json()["layouts"] == []


def test_delete_unknown_is_not_an_error():
    assert client.delete("/api/layouts/ghost").json()["deleted"] is False


def test_updated_at_moves_on_overwrite():
    save("m")
    first = client.get("/api/layouts").json()["layouts"][0]
    with db.connect() as c:
        c.execute("UPDATE chart_layouts SET updated_at='2020-01-01' WHERE name='m'")
    save("m")
    second = client.get("/api/layouts").json()["layouts"][0]
    assert second["updated_at"] != "2020-01-01"
    assert second["created_at"] == first["created_at"]


def test_chart_page_wires_the_layout_ui():
    html = client.get("/").text
    for needle in ("currentLayout()", "applyLayout(", "loadLayouts()",
                   "saveLayout(", "/api/layouts", 'id="laybtn"',
                   'id="layname"'):
        assert needle in html, f"chart page missing {needle}"


def test_chart_page_wires_the_events_ui():
    html = client.get("/").text
    for needle in ("refreshMarkers()", "renderEventMenu()", "setMarkers",
                   'id="evbtn"', "EVENTS"):
        assert needle in html, f"chart page missing {needle}"
