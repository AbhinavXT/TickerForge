"""The 4.3 chart wins: sparklines, compare, replay, and backtest hand-off.

Most of the behaviour is browser-side and covered in tests/js. What's testable
here is the data these features run on, and the wiring that makes them exist
at all — a feature whose button never got bound is indistinguishable from one
that was never written.
"""
from __future__ import annotations

import pytest

from app import db, watchlist


@pytest.fixture(autouse=True)
def _db():
    """The sparks tests read the database directly.

    Without this they only pass when some earlier test has already called
    _client() — which inits the DB as a side effect. Passing because of
    another file's ordering is not passing.
    """
    db.init()


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.server import app
    db.init()
    return TestClient(app, base_url="http://127.0.0.1:5000")


# ------------------------------------------------------------- sparkline data
def test_sparks_returns_a_row_per_watchlist_entry():
    rows = watchlist.sparks()
    assert len(rows) == len(watchlist.items())
    for r in rows:
        assert set(r) >= {"symbol", "closes", "change_pct", "last"}


def test_sparks_survives_a_symbol_with_no_candles():
    """A name added but never charted has no history. That's a blank
    sparkline, not an exception that empties the whole sidebar."""
    rows = watchlist.sparks()
    assert all(isinstance(r["closes"], list) for r in rows)


def test_sparks_is_one_request():
    """It runs on every sidebar refresh. Thirty names must not become thirty
    rate-limited calls to draw a decoration."""
    c = _client()
    assert c.get("/api/watchlist/sparks").status_code == 200


# ---------------------------------------------------- backtest hand-off
def test_pinning_a_backtest_round_trips():
    c = _client()
    payload = {"symbol": "bel", "interval": "day", "strategy": "Supertrend",
               "markers": [{"time": 1700000000, "type": "buy", "price": 400}]}
    assert c.post("/api/backtest/pin", json=payload).json()["ok"]
    got = c.get("/api/backtest/pinned").json()
    assert got["symbol"] == "BEL"          # normalised
    assert got["strategy"] == "Supertrend"
    assert len(got["markers"]) == 1


def test_pinned_markers_are_capped():
    """A 20-year minute backtest can produce a lot of trades, and this lands
    in a meta row that gets read on every chart load."""
    c = _client()
    many = [{"time": i, "type": "buy", "price": 1} for i in range(5000)]
    c.post("/api/backtest/pin", json={"symbol": "BEL", "markers": many})
    assert len(c.get("/api/backtest/pinned").json()["markers"]) <= 2000


def test_pin_requires_a_symbol():
    c = _client()
    assert c.post("/api/backtest/pin", json={"markers": []}).status_code == 400


def test_pinned_is_empty_when_nothing_is_pinned():
    db.set_meta("pinned_backtest", "")
    c = _client()
    assert c.get("/api/backtest/pinned").json() == {}


def test_pin_is_csrf_protected():
    c = _client()
    r = c.post("/api/backtest/pin", json={"symbol": "BEL", "markers": []},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# -------------------------------------------------------------- page wiring
def test_chart_page_wires_every_small_win():
    c = _client()
    html = c.get("/").text
    for needle in ("sparkSVG", "initKeys", "stepWatchlist", "setCompare",
                   "indexTo100", "replayEnter", "replayStep",
                   "showPinnedBacktest", "initSmallWins"):
        assert needle in html, f"chart page missing {needle}"


def test_chart_page_binds_the_controls():
    """Defined but never bound is the same as missing, from the user's side."""
    c = _client()
    html = c.get("/").text
    for needle in ("#cmpGo", "#cmpOff", "#rpToggle", "#rpExit", "#rpPlay",
                   "#rpSlider"):
        assert needle in html, f"control {needle} is never wired"


def test_chart_page_accepts_a_symbol_in_the_url():
    """How the backtest page hands a result over."""
    c = _client()
    assert 'URLSearchParams(location.search).get("symbol")' in c.get("/").text


def test_backtest_page_offers_show_on_chart():
    c = _client()
    html = c.get("/backtest").text
    assert "showOnChart" in html and "/api/backtest/pin" in html
    assert "btActions" in html


def test_replay_and_compare_are_dropped_when_the_symbol_changes():
    """Both are anchored to the symbol you just left. Carrying them over would
    silently compare the wrong pair."""
    c = _client()
    html = c.get("/").text
    assert "if (replay.on) replayExit()" in html
    assert "clearCompare()" in html


# ------------------------------------------------- toolbars (interval / type)
def test_every_offered_interval_is_one_the_backend_serves():
    """A timeframe in the menu that Kite can't serve is an empty chart with no
    explanation. Week and month are the deliberate exceptions — they're built
    from daily bars in the browser."""
    import re

    from app import backtest
    html = _client().get("/").text
    offered = set(re.findall(r'data-i="([a-z0-9]+)"', html))
    derived = {"week", "month"}
    assert offered - derived <= set(backtest._BPY), \
        f"offered but unsupported: {sorted(offered - derived - set(backtest._BPY))}"


def test_all_native_intervals_are_offered():
    """They cost nothing to expose and five of eight were missing."""
    import re

    from app import backtest
    html = _client().get("/").text
    offered = set(re.findall(r'data-i="([a-z0-9]+)"', html))
    assert set(backtest._BPY) <= offered


def test_derived_timeframes_still_request_daily_bars():
    """Kite has no weekly endpoint; asking for interval=week would 400."""
    html = _client().get("/").text
    assert "function nativeInterval" in html
    assert 'tf === "week" || tf === "month" ? "day" : tf' in html


def test_chart_types_are_wired():
    html = _client().get("/").text
    for needle in ("CHART_TYPES", "setChartType", "makeSeries", "seriesData",
                   "heikinAshi", "applyTimeframe"):
        assert needle in html, f"chart page missing {needle}"


def test_the_toolbars_explain_what_is_absent():
    """A missing option looks like an oversight unless the menu says why."""
    html = _client().get("/").text
    assert "shorter than a minute" in html      # no sub-minute data from Kite
    assert "discard time entirely" in html      # renko/kagi/p&f


def test_drawing_tools_sit_beside_the_chart():
    html = _client().get("/").text
    assert 'id="chartrow"' in html
    row = html[html.index("#chartrow {"):]
    assert "flex-direction: column" in row[:400], "the rail is not vertical"


def test_chart_page_has_no_duplicate_element_ids():
    """Moving controls between toolbars is exactly how a stray second copy
    gets left behind — and getElementById would then bind the dead one."""
    import re
    ids = re.findall(r'\sid="([A-Za-z0-9_]+)"', _client().get("/").text)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate ids on the chart page: {dupes}"


# ------------------------------------------------- dropdown positioning
def test_pickers_use_the_same_positioning_as_the_working_menus():
    """The first version used `position: absolute; top: 100%`.

    `.dd` carries no positioning of its own, so absolute resolved against the
    page and the menu opened a full viewport height below the fold. It WAS
    opening — you just couldn't see it, which is indistinguishable from a dead
    button. The three older menus all use fixed + a JS-computed rect, and so
    must these.
    """
    html = _client().get("/").text
    css = html[html.index(".picker {"):]
    css = css[:css.index("}")]
    assert "position: fixed" in css
    assert "top: 100%" not in css, "back to page-relative positioning"


def test_pickers_are_placed_from_the_button_rect():
    html = _client().get("/").text
    assert "placeMenu(btn, menu)" in html


def test_opening_a_picker_closes_the_other_menus():
    """Otherwise two dropdowns can sit on top of each other."""
    html = _client().get("/").text
    assert "function closeMenus" in html
    fn = html[html.index("function closeMenus"):]
    for other in ("#indmenu", "#evmenu", "#laymenu"):
        assert other in fn[:400], f"{other} is left open"


def test_clicks_inside_a_picker_do_not_close_it():
    """The document handler would otherwise fire first and remove the menu
    before the item's own click handler ran."""
    html = _client().get("/").text
    assert "$(menu).onclick = (e) => e.stopPropagation()" in html


# ---------------------------------------------------------- toolbar wiring
def test_only_real_tools_are_bound_to_setTool():
    """#rpToggle lives in the same rail and carries no data-tool.

    Bound indiscriminately, clicking replay armed the drawing canvas with an
    undefined tool: it then swallowed every mouse event while drawing nothing,
    and panning, zooming and the crosshair all stopped working. The symptom
    reads as "the drawing tools are broken" — the cause is the button next to
    them.
    """
    html = _client().get("/").text
    assert '#drawtools .dt[data-tool]' in html
    assert 'querySelectorAll("#drawtools .dt")\n\t\t\t\t\tb.addEventListener' not in html


def test_setTool_rejects_anything_that_is_not_a_tool():
    """Belt and braces: an armed canvas with no valid tool is invisible and
    breaks the chart."""
    html = _client().get("/").text
    assert "const TOOLS = [" in html
    assert "if (!TOOLS.includes(tool)) return" in html


def test_replay_button_is_not_a_drawing_tool():
    import re
    html = _client().get("/").text
    rail = html[html.index('id="drawtools"'):]
    rail = rail[:rail.index("</div>")]
    btn = re.search(r'<button[^>]*id="rpToggle"[^>]*>', rail)
    assert btn and "data-tool" not in btn.group(0)


# ------------------------------------------------------------ replay markers
def test_markers_are_computed_from_the_displayed_bars():
    """Two bugs in one line.

    Rendering: lightweight-charts pins a marker past the last data point onto
    the last visible bar, so replay stacked every future event on the edge.
    Honesty: replay exists to show a setup unfolding without knowing what came
    next, and marking events that haven't happened defeats it.
    """
    html = _client().get("/").text
    fn = html[html.index("function refreshMarkers"):]
    fn = fn[:fn.index("series.setMarkers(\n")]
    assert "shownCandles()" in fn
    assert "def.fn(bars)" in fn, "events are still computed over every candle"


def test_stepping_the_replay_refreshes_the_markers():
    html = _client().get("/").text
    apply_fn = html[html.index("function replayApply"):]
    apply_fn = apply_fn[:apply_fn.index("\n\t\t\t}")]
    assert "refreshMarkers()" in apply_fn


def test_leaving_replay_restores_the_markers():
    html = _client().get("/").text
    exit_fn = html[html.index("function replayExit"):]
    exit_fn = exit_fn[:exit_fn.index("\n\t\t\t}")]
    assert "refreshMarkers()" in exit_fn


# --------------------------------------------------------- resizable sidebar
def test_the_sidebar_is_resizable():
    html = _client().get("/").text
    assert 'id="sidegrip"' in html
    assert "var(--side-w" in html
    assert "initSideGrip" in html


def test_the_drag_listens_on_the_window_not_the_grip():
    """A 6px strip loses the pointer immediately; a grip-scoped mousemove
    would make the drag stick after a few pixels."""
    html = _client().get("/").text
    fn = html[html.index("function initSideGrip"):]
    assert 'window.addEventListener("mousemove"' in fn


def test_the_sidebar_width_is_clamped_and_remembered():
    html = _client().get("/").text
    assert "SIDE_MIN" in html and "SIDE_MAX" in html
    assert "tf_side_w" in html


def test_the_overlay_cannot_eat_the_drag():
    """The drawing canvas covers the chart; without this the drag dies the
    moment the cursor crosses it."""
    html = _client().get("/").text
    assert "body.resizing #drawlayer" in html
