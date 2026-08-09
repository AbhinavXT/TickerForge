"""Drawing persistence and its API.

The geometry is asserted in tests/js/drawings.body.js — this covers the half
that outlives the browser: what gets stored, what gets refused, and the fact
that a drawing is anchored in prices and timestamps rather than pixels.
"""
from __future__ import annotations

import pytest

from app import db, drawings


@pytest.fixture(autouse=True)
def clean():
    db.init()
    drawings.clear("BEL")
    drawings.clear("TCS")
    yield
    drawings.clear("BEL")
    drawings.clear("TCS")


def pts(*pairs):
    return [{"t": t, "p": p} for t, p in pairs]


LINE = ((1700000000, 400.0), (1700600000, 430.0))


# --------------------------------------------------------------- round trip
def test_add_and_read_back():
    made = drawings.add("BEL", "day", "trendline", pts(*LINE))
    got = drawings.for_chart("BEL", "day")
    assert len(got) == 1
    assert got[0]["id"] == made["id"]
    assert got[0]["points"] == [{"t": 1700000000, "p": 400.0},
                                {"t": 1700600000, "p": 430.0}]


def test_symbol_is_normalised():
    drawings.add("bel", "day", "hline", pts((1700000000, 410.0)))
    assert len(drawings.for_chart("BEL", "day")) == 1


def test_points_are_stored_as_time_and_price_not_pixels():
    """The decision the whole feature rests on. Pixels are correct at exactly
    one zoom level, one window size and one screen."""
    made = drawings.add("BEL", "day", "trendline", pts(*LINE))
    for point in made["points"]:
        assert set(point) == {"t", "p"}
        assert point["t"] > 1_000_000_000        # a unix timestamp
        assert point["p"] > 0                    # a price


# ------------------------------------------------------------------ scoping
def test_intervals_do_not_bleed_into_each_other():
    """A trendline across daily bars connects levels that don't exist on a
    5-minute chart. Showing it there is worse than not persisting it."""
    drawings.add("BEL", "day", "trendline", pts(*LINE))
    assert drawings.for_chart("BEL", "5minute") == []
    assert len(drawings.for_chart("BEL", "day")) == 1


def test_symbols_do_not_bleed_into_each_other():
    drawings.add("BEL", "day", "hline", pts((1700000000, 410.0)))
    assert drawings.for_chart("TCS", "day") == []


def test_clear_without_an_interval_wipes_every_timeframe():
    drawings.add("BEL", "day", "hline", pts((1700000000, 410.0)))
    drawings.add("BEL", "5minute", "hline", pts((1700000000, 411.0)))
    assert drawings.clear("BEL") == 2


def test_clear_with_an_interval_spares_the_others():
    drawings.add("BEL", "day", "hline", pts((1700000000, 410.0)))
    drawings.add("BEL", "5minute", "hline", pts((1700000000, 411.0)))
    drawings.clear("BEL", "day")
    assert len(drawings.for_chart("BEL", "5minute")) == 1


# --------------------------------------------------------------- validation
@pytest.mark.parametrize("kind,n", sorted(drawings.SHAPES.items()))
def test_each_shape_accepts_its_own_point_count(kind, n):
    made = drawings.add("BEL", "day", kind,
                        pts(*[(1700000000 + i * 600, 400.0 + i) for i in range(n)]))
    assert len(made["points"]) == n


def test_wrong_point_count_is_refused():
    """A two-point shape given three points breaks the renderer at paint time,
    which is the worst place to find out."""
    with pytest.raises(drawings.DrawingError) as e:
        drawings.add("BEL", "day", "trendline", pts((1, 400.0)))
    assert "exactly 2 points" in str(e.value)


def test_unknown_kind_is_refused():
    with pytest.raises(drawings.DrawingError):
        drawings.add("BEL", "day", "spiral", pts(*LINE))


def test_malformed_points_are_refused():
    for bad in ([{"t": 1}], [{"p": 400}], ["nope"], "points", None):
        with pytest.raises(drawings.DrawingError):
            drawings.add("BEL", "day", "hline", bad)


def test_non_positive_prices_are_refused():
    with pytest.raises(drawings.DrawingError):
        drawings.add("BEL", "day", "hline", pts((1700000000, 0.0)))
    with pytest.raises(drawings.DrawingError):
        drawings.add("BEL", "day", "hline", pts((1700000000, -5.0)))


def test_style_is_validated():
    with pytest.raises(drawings.DrawingError):
        drawings.add("BEL", "day", "hline", pts((1, 400.0)),
                     style={"color": "javascript:alert(1)"})
    with pytest.raises(drawings.DrawingError):
        drawings.add("BEL", "day", "hline", pts((1, 400.0)),
                     style={"width": 99})
    made = drawings.add("BEL", "day", "hline", pts((1, 400.0)),
                        style={"color": "#4fc3f7", "width": 2, "dash": True})
    assert made["style"] == {"color": "#4fc3f7", "width": 2, "dash": True}


def test_style_drops_unknown_keys():
    """An allowlist, so a stored style can't smuggle anything into the canvas
    call it ends up feeding."""
    made = drawings.add("BEL", "day", "hline", pts((1, 400.0)),
                        style={"color": "#ffffff", "onload": "boom"})
    assert "onload" not in made["style"]


def test_there_is_a_ceiling():
    """A runaway mousemove handler shouldn't be able to fill the database."""
    for i in range(drawings.MAX_PER_CHART):
        drawings.add("TCS", "day", "hline", pts((1700000000 + i, 400.0)))
    with pytest.raises(drawings.DrawingError) as e:
        drawings.add("TCS", "day", "hline", pts((1, 400.0)))
    assert "already" in str(e.value)


# -------------------------------------------------------------------- moving
def test_move_re_anchors():
    made = drawings.add("BEL", "day", "trendline", pts(*LINE))
    drawings.move(made["id"], pts((1700000000, 405.0), (1700600000, 445.0)))
    got = drawings.for_chart("BEL", "day")[0]
    assert got["points"][1]["p"] == 445.0


def test_move_validates_against_the_stored_kind():
    """You can't turn a two-point line into a one-point one by dragging."""
    made = drawings.add("BEL", "day", "trendline", pts(*LINE))
    with pytest.raises(drawings.DrawingError):
        drawings.move(made["id"], pts((1700000000, 405.0)))


def test_moving_something_that_is_gone_is_an_error_not_a_crash():
    with pytest.raises(drawings.DrawingError):
        drawings.move(99999, pts(*LINE))


def test_remove():
    made = drawings.add("BEL", "day", "trendline", pts(*LINE))
    assert drawings.remove(made["id"]) == 1
    assert drawings.for_chart("BEL", "day") == []
    assert drawings.remove(made["id"]) == 0


# ----------------------------------------------------------------- fib maths
def test_fib_prices_anchor_at_zero_and_one():
    levels = drawings.fib_prices(100.0, 200.0)
    assert levels[0]["price"] == 100.0
    assert levels[-1]["price"] == 200.0


def test_fib_prices_mirror_when_drawn_the_other_way():
    """Direction encodes which way you think the move went."""
    down = drawings.fib_prices(200.0, 100.0)
    assert down[0]["price"] == 200.0
    assert down[-1]["price"] == 100.0


def test_fib_includes_the_half():
    assert any(l["level"] == 0.5 for l in drawings.fib_prices(1.0, 2.0))


def test_python_and_javascript_agree_on_the_levels():
    """Two implementations of the same ladder is one too many to leave
    unchecked — the browser draws them, the API reports them."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "web" / "index.html").read_text()
    js = re.search(r"const FIB_LEVELS = \[([^\]]+)\]", html).group(1)
    assert [float(x) for x in js.split(",")] == list(drawings.FIB_LEVELS)


# --------------------------------------------------------------------- API
def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.server import app
    return TestClient(app, base_url="http://127.0.0.1:5000")


def test_api_round_trip():
    c = _client()
    r = c.post("/api/drawings", json={"symbol": "BEL", "interval": "day",
                                      "kind": "trendline", "points": pts(*LINE)})
    assert r.status_code == 200
    made = r.json()
    listed = c.get("/api/drawings?symbol=BEL&interval=day").json()
    assert [d["id"] for d in listed] == [made["id"]]

    r = c.patch(f"/api/drawings/{made['id']}",
                json={"points": pts((1700000000, 401.0), (1700600000, 431.0))})
    assert r.status_code == 200

    assert c.delete(f"/api/drawings/{made['id']}").json()["deleted"] == 1


def test_api_rejects_bad_shapes():
    c = _client()
    r = c.post("/api/drawings", json={"symbol": "BEL", "kind": "fib",
                                      "points": pts((1, 400.0))})
    assert r.status_code == 400
    assert "2 points" in r.json()["detail"]


def test_api_reports_a_missing_field_clearly():
    c = _client()
    r = c.post("/api/drawings", json={"symbol": "BEL"})
    assert r.status_code == 400
    assert "kind" in r.json()["detail"]


def test_api_writes_are_csrf_protected():
    """These routes write, so the guard has to cover them like the rest."""
    c = _client()
    r = c.post("/api/drawings",
               json={"symbol": "BEL", "kind": "hline", "points": pts((1, 400.0))},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_page_wires_the_drawing_layer():
    c = _client()
    html = c.get("/").text
    for needle in ("drawlayer", "drawtools", "initDrawing", "redrawDrawings",
                   "/api/drawings", "distToSegment", "fibLevels"):
        assert needle in html, f"chart page missing {needle}"


def test_overlay_does_not_swallow_the_mouse_by_default():
    """The canvas sits ON the chart. If it ate pointer events unconditionally,
    panning, zooming and the crosshair would all stop working."""
    c = _client()
    html = c.get("/").text
    assert "pointer-events: none" in html
    assert "#drawlayer.active" in html
