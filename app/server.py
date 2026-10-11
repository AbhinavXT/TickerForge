"""TickerForge server (Phase 1).

Run:  python -m app.cli serve   →  http://127.0.0.1:5000

Routes
  GET  /                    UI — and Kite login-redirect catcher: when Zerodha
                            redirects here with ?request_token=..., we complete
                            the session automatically. Login becomes one click.
  GET  /api/search?q=
  GET  /api/candles?symbol=&interval=&days=
  GET  /api/watchlist       POST /api/watchlist/{symbol}   DELETE same
  WS   /ws/ticks?token=     live quote stream for one instrument
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from kiteconnect.exceptions import KiteException

from . import config, db, kite_auth, instruments, watchlist, candles, fundamentals, news, events, valuation, backtest, calendar_export, results, intraday, alerts, charts, latest_results, signals, risk, journal, portfolio, batch, heatmap, scanner, universe, walkforward, settings, maintenance, montecarlo, drawings, rs, screen, paper, semiauto, query
from .ticker import hub

WEB_DIR = Path(__file__).parent / "web"
app = FastAPI(title="TickerForge")

# --------------------------------------------------------------- CSRF guard
#
# The server binds to 127.0.0.1 and has no login, which is fine for a local
# tool and NOT the same as being unreachable. Write endpoints take query
# parameters, so a plain cross-origin <form> POST is a "simple request" — no
# CORS preflight, no way for the browser to block it — and any page you happen
# to be visiting can arm alerts, write journal trades or edit the watchlist.
# The response is unreadable to the attacker, but the side effect has already
# happened.
#
# Today that costs you junk data. It stops being cosmetic at roadmap 6.2, when
# order placement arrives behind these same routes, so the guard goes in
# before the capability does rather than after.
#
# Two checks, both cheap:
#
#   Origin — browsers send it on every request whose method isn't GET/HEAD,
#   including same-origin ones. So a mismatched Origin is a genuine cross-site
#   write and is refused. A MISSING Origin is not a browser at all (curl, the
#   CLI, the test client) and is allowed: CSRF needs a browser to exist.
#
#   Host — refuses anything not addressed to loopback, which is what stops DNS
#   rebinding, where an attacker's domain resolves to 127.0.0.1 and turns their
#   page into a same-origin one.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Mutable on purpose. The test client addresses the app as "testserver", which
# is neither loopback nor a real host, and hard-coding it here would put test
# scaffolding in the security boundary. tests/conftest.py adds it instead, so
# the shipped default stays strict and the seam is visible.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _hostname(value: str) -> str:
    """Strip the port. Handles bare IPv6 in brackets."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def _origin_ok(origin: str, host_header: str) -> bool:
    """The request's own Host is the only origin allowed to write."""
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    if not parsed.hostname:
        return False
    if parsed.hostname.lower() not in LOCAL_HOSTS:
        return False
    # Same host AND same port — a different local service is still not us.
    return parsed.netloc.lower() == (host_header or "").strip().lower()


@app.middleware("http")
async def _csrf_and_host_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if _hostname(host) not in LOCAL_HOSTS:
        return JSONResponse(
            {"detail": "TickerForge only serves loopback addresses."},
            status_code=421,
        )
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and not _origin_ok(origin, host):
            return JSONResponse(
                {"detail": "Cross-site write refused."}, status_code=403)
    return await call_next(request)



@app.on_event("startup")
def _startup():
    db.init()


@app.get("/login")
def login_redirect():
    """Stable entry point for the daily login.

    Worth its own route because it's the one URL you want on a notification:
    it survives an API-key change, it's short enough to type, and the redirect
    lands back here with ?request_token=, which the root route completes.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(kite_auth.login_url(), status_code=302)


# ---------------------------------------------------------------- login + UI
@app.get("/", response_class=HTMLResponse)
def root(request_token: str | None = None, status: str | None = None):
    if request_token:
        kite_auth.complete_login(request_token)
        return HTMLResponse(
            "<h3>Kite login complete — token cached for today.</h3>"
            "<p><a href='/'>Open TickerForge</a></p>"
        )
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/session")
def session_status():
    return {"kite": kite_auth.token_status(),
            "login_url": kite_auth.login_url()}


# ---------------------------------------------------------------- extra pages
@app.get("/fundamentals", response_class=HTMLResponse)
def page_fundamentals():
    return FileResponse(WEB_DIR / "fundamentals.html")


@app.get("/news", response_class=HTMLResponse)
def page_news():
    return FileResponse(WEB_DIR / "news.html")


@app.get("/actions", response_class=HTMLResponse)
def page_actions():
    return FileResponse(WEB_DIR / "actions.html")


@app.get("/results", response_class=HTMLResponse)
def page_results():
    return FileResponse(WEB_DIR / "results.html")


@app.get("/desk", response_class=HTMLResponse)
def page_desk():
    return FileResponse(WEB_DIR / "desk.html")


@app.get("/valuation", response_class=HTMLResponse)
def page_valuation():
    return FileResponse(WEB_DIR / "valuation.html")


@app.get("/metrics", response_class=HTMLResponse)
def page_metrics():
    return FileResponse(WEB_DIR / "metrics.html")


@app.get("/backtest", response_class=HTMLResponse)
def page_backtest():
    return FileResponse(WEB_DIR / "backtest.html")


@app.get("/journal", response_class=HTMLResponse)
def page_journal():
    return FileResponse(WEB_DIR / "journal.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Served from the ROOT deliberately: a worker's scope can't reach above
    # its own path, and one parked at /static/sw.js could never control /desk.
    return FileResponse(
        WEB_DIR / "sw.js", media_type="application/javascript",
        # No-store on the worker itself. Everything it caches is versioned by
        # CACHE inside it, but if the browser caches the WORKER you can never
        # ship a new version — including one that fixes the caching.
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "Service-Worker-Allowed": "/"})


@app.get("/icons/{name}")
def icon(name: str):
    path = (WEB_DIR / "icons" / name).resolve()
    if path.parent != (WEB_DIR / "icons").resolve() or not path.is_file():
        raise HTTPException(404, "no such icon")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(WEB_DIR / "icons" / "favicon-32.png",
                        media_type="image/png")


@app.get("/api/watchlist/sparks")
def api_watchlist_sparks(days: int = 30):
    return watchlist.sparks(days)


# A backtest result the chart page should show. Kept server-side rather than
# passed through the URL: a trade list is far too long for a query string, and
# putting it in browser storage would mean it vanishes on the one machine
# switch where you most wanted to see it again.
@app.post("/api/backtest/pin")
async def api_backtest_pin(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or "symbol" not in body:
        raise HTTPException(400, "expected {symbol, interval, markers}")
    db.set_meta("pinned_backtest", json.dumps({
        "symbol": str(body["symbol"]).upper(),
        "interval": body.get("interval", "day"),
        "strategy": body.get("strategy", ""),
        "markers": body.get("markers", [])[:2000],
    }))
    return {"ok": True}


@app.get("/api/backtest/pinned")
def api_backtest_pinned():
    raw = db.get_meta("pinned_backtest")
    return json.loads(raw) if raw else {}


@app.get("/paper", response_class=HTMLResponse)
def page_paper():
    return FileResponse(WEB_DIR / "paper.html")


@app.get("/api/semiauto")
def api_semiauto():
    ok, why = semiauto.enabled()
    return {"enabled": ok, "why": why, "pending": semiauto.pending(),
            "recent": semiauto.recent(20)}


@app.post("/api/semiauto/approve")
def api_semiauto_approve(token: str):
    """Placing an order from a URL.

    Reachable only over loopback or your tailnet, behind the same Origin/Host
    guard as everything else, and the token is single-use — so a link that
    leaks is a link that has already been spent. The ntfy Approve button
    targets this; the phone command path needs no inbound access at all.
    """
    try:
        return semiauto.approve(token)
    except semiauto.Blocked as e:
        raise HTTPException(409, str(e)) from None
    except semiauto.SemiAutoError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/semiauto/reject")
def api_semiauto_reject(token: str):
    try:
        return semiauto.reject(token)
    except semiauto.SemiAutoError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/paper")
def api_paper():
    return paper.state()


@app.post("/api/paper/signals")
def api_paper_from_signals(stop_pct: float | None = None):
    """Turn today's flips into pending orders."""
    try:
        paper.start()
        return paper.from_signals(stop_pct=stop_pct)
    except SystemExit as e:
        raise HTTPException(401, str(e)) from None


@app.post("/api/paper/fill")
def api_paper_fill():
    return paper.fill_pending()


@app.delete("/api/paper/order/{order_id}")
def api_paper_cancel(order_id: int):
    return {"cancelled": paper.cancel(order_id)}


@app.post("/api/paper/reset")
def api_paper_reset(confirm: bool = False):
    try:
        return paper.reset(confirm=confirm)
    except paper.PaperError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/query", response_class=HTMLResponse)
def page_query():
    return FileResponse(WEB_DIR / "query.html")


@app.get("/api/query/fields")
def api_query_fields():
    return {
        "fields": query.fields(),
        # Flat list kept for anything already consuming it; the grouped view
        # is what the reference panel renders.
        "examples": [{"query": q, "means": m} for q, m in query.EXAMPLES],
        "example_groups": [
            {"title": title,
             "items": [{"query": q, "means": m} for q, m in items]}
            for title, items in query.EXAMPLE_GROUPS],
    }


@app.get("/api/query/check")
def api_query_check(q: str):
    """Parse without running. What the editor calls as you type."""
    try:
        return {"ok": True, "parsed": query.render_plan(query.plan(q))}
    except query.QueryError as e:
        return {"ok": False, "error": str(e), "col": e.col}


@app.get("/api/query")
def api_query(q: str, universe_name: str | None = None, days: int = 420,
              limit: int = 200, why: int = 0):
    syms = (universe.symbols(universe_name) if universe_name
            else [r["tradingsymbol"] for r in watchlist.items()])
    if not syms:
        raise HTTPException(400, "nothing to scan")
    try:
        return query.run(q, syms, days=days, limit=limit, explain_misses=why)
    except query.QueryError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/query/saved")
def api_query_saved():
    return {"saved": query.saved()}


@app.post("/api/query/saved")
async def api_query_save(request: Request):
    body = await request.json()
    try:
        return query.save(str(body["name"]), str(body["expression"]),
                          body.get("universe"), bool(body.get("alert")))
    except KeyError as e:
        raise HTTPException(400, f"missing {e.args[0]}") from None
    except query.QueryError as e:
        raise HTTPException(400, str(e)) from None


@app.delete("/api/query/saved/{name}")
def api_query_forget(name: str):
    return {"deleted": query.forget(name)}


@app.post("/api/query/alerts/check")
def api_query_check_alerts():
    return query.check_alerts()


@app.get("/api/query/backtest")
def api_query_backtest(q: str, symbol: str, days: int = 730,
                       cost_bps: float = 10.0, sl_pct: float | None = None,
                       trail_mult: float | None = None):
    risk = {k: v for k, v in dict(sl_pct=sl_pct,
                                  trail_mult=trail_mult).items() if v}
    try:
        return query.backtest(q, symbol, days=days, cost_bps=cost_bps,
                              risk=risk)
    except query.QueryError as e:
        raise HTTPException(400, str(e)) from None
    except SystemExit as e:
        raise HTTPException(401, str(e)) from None
    except Exception as e:                            # noqa: BLE001
        raise HTTPException(502, f"backtest failed: {e}") from None


@app.get("/api/rs")
def api_rs(universe_name: str | None = None, symbols: str | None = None,
           days: int = 420):
    # named universe_name, not universe: a parameter called `universe` would
    # shadow the imported module inside this function
    """Relative strength, ranked. Reads stored candles — no fetching."""
    if symbols:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    elif universe_name:
        syms = universe.symbols(universe_name)
    else:
        syms = [r["tradingsymbol"] for r in watchlist.items()]
    if not syms:
        raise HTTPException(400, "nothing to rank")
    try:
        return {"index": rs.INDEX, "rows": rs.rank(syms, days=days)}
    except rs.RSError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/screen", response_class=HTMLResponse)
def page_screen():
    return FileResponse(WEB_DIR / "screen.html")


@app.get("/api/screen")
def api_screen(universe_name: str | None = None, min_rs: float = 70.0,
               verdicts: str = "undervalued,fair", require_long: bool = True,
               days: int = 420):
    syms = universe.symbols(universe_name) if universe_name else None
    try:
        return screen.run(
            symbols=syms, min_rs=min_rs,
            verdicts=tuple(v.strip() for v in verdicts.split(",") if v.strip()),
            require_long=require_long, days=days)
    except SystemExit as e:
        raise HTTPException(401, str(e)) from None


@app.get("/api/drawings")
def api_drawings(symbol: str, interval: str = "day"):
    return drawings.for_chart(symbol, interval)


@app.post("/api/drawings")
async def api_drawings_add(request: Request):
    body = await request.json()
    try:
        return drawings.add(body["symbol"], body.get("interval", "day"),
                            body["kind"], body.get("points"),
                            body.get("style"))
    except KeyError as e:
        raise HTTPException(400, f"missing {e.args[0]}") from None
    except drawings.DrawingError as e:
        raise HTTPException(400, str(e)) from None


@app.patch("/api/drawings/{drawing_id}")
async def api_drawings_move(drawing_id: int, request: Request):
    body = await request.json()
    try:
        return drawings.move(drawing_id, body.get("points"),
                             body.get("style"))
    except drawings.DrawingError as e:
        raise HTTPException(400, str(e)) from None


@app.delete("/api/drawings/{drawing_id}")
def api_drawings_remove(drawing_id: int):
    return {"deleted": drawings.remove(drawing_id)}


@app.delete("/api/drawings")
def api_drawings_clear(symbol: str, interval: str | None = None):
    return {"deleted": drawings.clear(symbol, interval)}


@app.get("/settings", response_class=HTMLResponse)
def page_settings():
    return FileResponse(WEB_DIR / "settings.html")


@app.get("/api/settings")
def api_settings():
    return settings.effective()


@app.post("/api/settings")
async def api_settings_save(request: Request):
    """Whole-form save: validate everything, then write, or write nothing.

    A partial save leaves a config you didn't ask for and no obvious way back
    — worse than a rejected form, because it looks like it worked.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected an object of key -> value")
    return settings.apply_all({str(k): str(v) for k, v in body.items()})


@app.delete("/api/settings/{key}")
def api_settings_clear(key: str):
    try:
        settings.clear(key)
    except settings.SettingError as e:
        raise HTTPException(404, str(e)) from None
    return {"ok": True, "key": key, "source": settings.source(key)}


@app.get("/api/maintenance")
def api_maintenance_status():
    return {"last_run": db.get_meta("maintenance_last_run"),
            "preview": maintenance.run(dry=True)}


@app.get("/portfolio", response_class=HTMLResponse)
def page_portfolio():
    return FileResponse(WEB_DIR / "portfolio.html")


@app.get("/app.css")
def page_css():
    return FileResponse(WEB_DIR / "app.css", media_type="text/css")


@app.get("/ui.js")
def page_ui_js():
    """The shared shell (rail, top bar, theme) and UI helpers."""
    return FileResponse(WEB_DIR / "ui.js", media_type="text/javascript")


@app.get("/board", response_class=HTMLResponse)
def page_board():
    return FileResponse(WEB_DIR / "board.html")


# ---------------------------------------------------------------- search
@app.get("/api/search")
def api_search(q: str):
    return instruments.search(q, limit=12)


# ---------------------------------------------------------------- candles
@app.get("/api/candles")
def api_candles(symbol: str, interval: str = "day", days: int | None = None):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    if interval not in candles.INTERVAL_MAX_DAYS:
        raise HTTPException(400, f"Bad interval {interval}")
    days = days or candles.DEFAULT_DAYS.get(interval, 365)
    try:
        kite = kite_auth.get_kite()
        candles.ensure(kite, inst["instrument_token"], interval, days)
    except SystemExit as e:               # no session today
        raise HTTPException(401, str(e))
    except KiteException as e:            # e.g. no historical-data subscription
        raise HTTPException(402, f"Kite API: {e}")
    data = candles.get(inst["instrument_token"], interval, days)
    return {"symbol": inst["tradingsymbol"], "name": inst["name"],
            "token": inst["instrument_token"], "interval": interval,
            "candles": data}


# ---------------------------------------------------------------- watchlist
@app.get("/api/watchlist")
def api_watchlist():
    return watchlist.items()


@app.post("/api/watchlist/{symbol}")
def api_watch_add(symbol: str):
    # autofetch fundamentals+results in the background so the add stays instant
    try:
        return watchlist.add(symbol, background=True)
    except SystemExit as e:
        # add() exits CLI-style on an unknown symbol; in the server that would
        # stop the whole process, so it's a 404 here.
        raise HTTPException(404, str(e))


@app.delete("/api/watchlist/{symbol}")
def api_watch_remove(symbol: str):
    return {"removed": watchlist.remove(symbol)}


# ---------------------------------------------------------------- fundamentals
@app.get("/api/fundamentals")
def api_fund(symbol: str):
    s = fundamentals.summary(symbol)
    return s if s else {"missing": True, "symbol": symbol.upper()}


@app.post("/api/fundamentals/refresh")
def api_fund_refresh(symbol: str):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    try:
        return fundamentals.fetch_and_store(inst["tradingsymbol"])
    except SystemExit as e:
        # config.require raises SystemExit, not Exception — a missing key must
        # become an actionable 401 rather than escaping the handler entirely
        raise HTTPException(401, str(e))
    except Exception as e:
        # surface the real upstream reason (throttle vs 404 vs parse) so the
        # UI shows something actionable instead of a bare 502
        msg = str(e) or e.__class__.__name__
        hint = ""
        if "429" in msg or "throttl" in msg.lower():
            hint = " — screener is rate-limiting; wait a minute and retry"
        elif "404" in msg:
            hint = " — no screener page at that symbol"
        raise HTTPException(502, f"screener fetch failed for "
                            f"{inst['tradingsymbol']}: {msg}{hint}")


@app.get("/api/fundamentals/full")
def api_fund_full(symbol: str):
    s = fundamentals.full(symbol)
    return s if s else {"missing": True, "symbol": symbol.upper()}


# ---------------------------------------------------------------- results
@app.get("/api/results")
def api_results(scope: str = "watch", sort: str = "latest",
                symbols: str | None = None):
    """Latest-quarter result cards: released numbers, or just the awaited date.
    scope=watch (your watchlist) | stored (everything we hold).
    sort=latest | sales | op | np.
    """
    syms = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    cur, prev, yoy = results.expected_quarter()
    return {"quarter": cur, "prev_quarter": prev, "yoy_quarter": yoy,
            "cards": results.cards(symbols=syms, sort=sort, scope=scope)}


@app.get("/api/results/card")
def api_results_card(symbol: str):
    inst = instruments.lookup(symbol)
    c = results.card(inst["tradingsymbol"] if inst else symbol)
    return c if c else {"missing": True, "symbol": symbol.upper()}


@app.get("/api/results/latest")
def api_results_latest(scope: str = "all", bucket: str | None = None,
                       sort: str = "latest", refresh: bool = False):
    if refresh or latest_results.is_stale():
        try:
            latest_results.refresh()
        except Exception as e:
            # serve whatever's cached; report the failure alongside
            return {"rows": latest_results.rows(scope, bucket, sort),
                    "fetched_at": latest_results.fetched_at(),
                    "stale": True, "error": str(e)}
    data = latest_results.rows(scope, bucket, sort)
    total = len(latest_results.rows("all"))
    return {"rows": data, "total": total,
            "fetched_at": latest_results.fetched_at(),
            "stale": latest_results.is_stale()}


@app.post("/api/results/latest/refresh")
def api_results_latest_refresh(sort: str = "latest"):
    try:
        n = latest_results.refresh(sort=sort)
        return {"refreshed": n, "fetched_at": latest_results.fetched_at()}
    except Exception as e:
        raise HTTPException(502, f"screener fetch failed: {e}")


# ---------------------------------------------------------------- intraday
# ---------------------------------------------------------------- the Board
# Read-only. The board builds from one quote call plus the stored 5m candles
# (a background sweep keeps those fresh during market hours) and is cached
# for 60s; outside market hours it serves the last session's board.
@app.get("/api/board")
def api_board():
    from . import board
    try:
        return board.get(kite_auth.get_kite)
    except SystemExit as e:
        raise HTTPException(401, str(e))


@app.get("/api/board/market")
def api_board_market():
    """Top-bar status for every page. Never 401s: without a Kite session it
    still answers open/closed and the IST time, with NIFTY omitted."""
    from . import board
    return board.market(kite_auth.get_kite)


@app.get("/api/board/{symbol}")
def api_board_detail(symbol: str):
    from . import board
    try:
        kite = kite_auth.get_kite()
    except SystemExit as e:
        raise HTTPException(401, str(e))
    d = board.detail(kite, symbol)
    if d is None:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    return d


@app.get("/api/intraday")
def api_intraday(symbol: str):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    try:
        kite = kite_auth.get_kite()
    except SystemExit as e:
        raise HTTPException(401, str(e))
    return intraday.levels(kite, inst["tradingsymbol"],
                           inst["instrument_token"])


@app.get("/api/intraday/chart")
def api_intraday_chart(symbol: str):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    try:
        kite = kite_auth.get_kite()
    except SystemExit as e:
        raise HTTPException(401, str(e))
    S, tok = inst["tradingsymbol"], inst["instrument_token"]
    lv = intraday.levels(kite, S, tok)
    png = charts.intraday_png(lv, intraday.today_minutes(kite, tok, "5minute"),
                              subtitle=datetime.now().strftime("%H:%M"))
    return Response(content=png, media_type="image/png")


@app.get("/api/confluence")
def api_confluence(symbol: str):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    try:
        kite = kite_auth.get_kite()
    except SystemExit as e:
        raise HTTPException(401, str(e))
    from . import watcher
    S, tok = inst["tradingsymbol"], inst["instrument_token"]
    lv = intraday.levels(kite, S, tok)
    conf = watcher.confluence_for(kite, watcher._IndCache(), tok, lv.get("ltp"))
    if not conf:
        return {"symbol": S, "score_pct": None, "verdict": "no data"}
    return {"symbol": S, **conf}


# ---------------------------------------------------------------- alerts
@app.get("/api/alerts")
def api_alerts(symbol: str | None = None, all: bool = False):
    rows = alerts.all_rows(symbol) if all else alerts.active(symbol)
    for r in rows:                       # decorate with a readable label
        r["label"] = alerts.label(r)
    return {"alerts": rows}


@app.post("/api/alerts")
def api_alerts_add(symbol: str, cond: str):
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    try:
        kind, params, lbl = alerts.parse(cond.split())
    except ValueError as e:
        raise HTTPException(400, str(e))
    aid = alerts.add(inst["tradingsymbol"], inst["instrument_token"],
                     kind, {**params, "label": lbl})
    return {"id": aid, "symbol": inst["tradingsymbol"], "label": lbl}


@app.patch("/api/alerts/{alert_id}")
def api_alerts_move(alert_id: int, price: float, last_price: float | None = None):
    """Move a price alert to a new level — what dragging its line does.

    `last_price` lets the direction follow the drag: a line pulled from above
    spot to below it becomes a break down rather than a greater-than that
    fires immediately.
    """
    try:
        row = alerts.retarget(alert_id, price, last_price)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, f"no alert #{alert_id}")
    return {"id": alert_id, "label": alerts.label(row),
            "symbol": row["symbol"], "price": price}


@app.delete("/api/alerts/{target}")
def api_alerts_remove(target: str):
    return {"removed": alerts.remove(target)}


# ---------------------------------------------------------------- news page
@app.get("/api/news")
def api_news(hours: int = 48):
    return news.aggregate(hours=hours)


# ---------------------------------------------------------------- events page
@app.get("/api/events")
def api_events(limit: int = 300, source: str | None = None,
              category: str | None = None, symbol: str | None = None):
    return {"events": events.recent(limit=limit, source=source,
                                    category=category, symbol=symbol),
            "categories": list(events.CATEGORIES),
            "sources": list(events.FEED_LABEL.keys())}


@app.post("/api/events/poll")
def api_events_poll():
    """Ingest new NSE filings on demand (no phone push from the browser)."""
    try:
        return events.poll_once(push=False)
    except SystemExit as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"NSE poll failed: {e}")


@app.get("/api/insider")
def api_insider(symbol: str | None = None, days: int = 30, scope: str = "all",
                fetch: bool = False):
    """Promoter/director trades and pledges. scope=mine limits to the names
    you follow; fetch=1 pulls one symbol from NSE first."""
    from . import insider, portfolio
    if fetch and symbol:
        try:
            insider.fetch_symbol(symbol, days=max(days, 30))
        except Exception as e:
            raise HTTPException(502, f"NSE insider fetch failed: {e}")
    syms = list(portfolio.followed()) if scope == "mine" else None
    rows = insider.recent(symbol=symbol, days=days, symbols=syms)
    for r in rows:
        r["market"] = insider.is_market(r)
    return {"rows": rows,
            "net": insider.net(symbol, days=days) if symbol else None}


@app.post("/api/insider/poll")
def api_insider_poll():
    from . import insider
    try:
        return insider.poll_once(push=False)
    except SystemExit as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"NSE insider poll failed: {e}")


@app.get("/api/delivery/{symbol}")
def api_delivery(symbol: str):
    from . import delivery
    return delivery.stats(symbol) or {}


@app.get("/api/preopen")
def api_preopen():
    from . import preopen
    try:
        return preopen.check()
    except SystemExit as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"pre-open data unavailable: {e}")


# -------------------------------------------------- market-wide calendar (all stocks)
@app.get("/api/calendar")
def api_calendar():
    """All-stocks corporate calendar (results/dividends/actions), JSON preview."""
    try:
        rows = calendar_export.build()
        return {"rows": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(502, f"NSE calendar fetch failed: {e}")


@app.get("/api/calendar.xlsx")
def api_calendar_xlsx():
    """Same calendar as a downloadable Excel workbook."""
    try:
        rows = calendar_export.build()
        data = calendar_export.to_xlsx(rows)
    except ModuleNotFoundError:
        raise HTTPException(500, "openpyxl not installed — run: pip install openpyxl")
    except Exception as e:
        raise HTTPException(502, f"calendar export failed: {e}")
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="corporate-calendar-{stamp}.xlsx"'},
    )


# ---------------------------------------------------------------- valuation
def _val_assumptions(re: float | None, g_term: float | None, years: int | None,
                     bondY: float | None, g_cap: float | None) -> dict:
    a = {}
    if re is not None: a["re"] = re
    if g_term is not None: a["g_term"] = g_term
    if years is not None: a["years"] = years
    if bondY is not None: a["bondY"] = bondY
    if g_cap is not None: a["g_cap"] = g_cap
    return a


@app.get("/api/valuation")
def api_valuation(symbol: str, re: float | None = None, g_term: float | None = None,
                  years: int | None = None, bondY: float | None = None,
                  g_cap: float | None = None, rich: bool = False):
    inst = instruments.lookup(symbol)
    sym = inst["tradingsymbol"] if inst else symbol
    v = valuation.value(sym, _val_assumptions(re, g_term, years, bondY, g_cap),
                        rich=rich)
    return v if v else {"missing": True, "symbol": symbol.upper()}


@app.get("/api/valuation/screen")
def api_valuation_screen(symbols: str | None = None, re: float | None = None,
                         g_term: float | None = None, years: int | None = None,
                         bondY: float | None = None, g_cap: float | None = None):
    syms = [s.strip() for s in symbols.split(",")] if symbols else None
    return valuation.screen(syms, _val_assumptions(re, g_term, years, bondY, g_cap))


# ---------------------------------------------------------------- backtest
@app.get("/api/backtest/strategies")
def api_bt_strategies():
    return {"strategies": backtest.catalog(),
            "intervals": list(backtest._BPY.keys())}


@app.get("/api/backtest")
def api_backtest(symbol: str, strategy: str, interval: str = "day",
                 days: int = 730, cost_bps: float = 10.0, capital: float = 100000.0,
                 params: str | None = None,          # JSON, e.g. {"fast":20,"slow":50}
                 # optional risk overlay (blank/0 = off)
                 sl_pct: float | None = None, tp_pct: float | None = None,
                 trail_mult: float | None = None, trail_period: int | None = None,
                 max_bars: int | None = None,
                 # 3.5 engine extensions
                 sizing: str | None = None, risk_pct: float | None = None,
                 atr_mult: float | None = None,
                 max_position_pct: float | None = None,
                 regime: bool = False,
                 # legacy individual params, kept for back-compat
                 fast: int | None = None, slow: int | None = None,
                 length: int | None = None, signal: int | None = None,
                 mult: float | None = None, low: float | None = None,
                 high: float | None = None):
    p = {k: v for k, v in dict(fast=fast, slow=slow, length=length,
         signal=signal, mult=mult, low=low, high=high).items() if v is not None}
    if params:
        try:
            extra = json.loads(params)
            if not isinstance(extra, dict):
                raise ValueError
            p.update({k: v for k, v in extra.items() if v is not None})
        except ValueError:
            raise HTTPException(400, "params must be a JSON object")
    risk = {k: v for k, v in dict(sl_pct=sl_pct, tp_pct=tp_pct,
            trail_mult=trail_mult, trail_period=trail_period,
            max_bars=max_bars, sizing=sizing, risk_pct=risk_pct,
            atr_mult=atr_mult,
            max_position_pct=max_position_pct).items() if v is not None}
    try:
        return backtest.run(symbol, strategy, interval=interval, days=days,
                            cost_bps=cost_bps, capital=capital, params=p,
                            risk=risk, regime=regime)
    except backtest.SizingError as e:
        raise HTTPException(400, str(e))
    except SystemExit as e:
        raise HTTPException(401, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"backtest failed: {e}")


@app.get("/api/backtest/batch")
def api_backtest_batch(strategy: str, symbols: str | None = None,
                       interval: str = "day", days: int = 730,
                       cost_bps: float = 10.0, capital: float = 100_000.0,
                       params: str | None = None, sort: str = "expectancy_pct",
                       sl_pct: float | None = None, tp_pct: float | None = None,
                       trail_mult: float | None = None,
                       trail_period: int | None = None,
                       max_bars: int | None = None):
    """One strategy across many symbols (default: the watchlist).

    Returns per-symbol rows PLUS breadth statistics. Read the aggregate before
    the ranking — see batch.CAVEAT.
    """
    syms = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
            if symbols else None)
    p = None
    if params:
        try:
            p = json.loads(params)
            if not isinstance(p, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, "params must be a JSON object")
    risk = {k: v for k, v in dict(sl_pct=sl_pct, tp_pct=tp_pct,
            trail_mult=trail_mult, trail_period=trail_period,
            max_bars=max_bars).items() if v is not None}
    try:
        out = batch.run(strategy, symbols=syms, interval=interval, days=days,
                        cost_bps=cost_bps, capital=capital, params=p,
                        risk=risk or None, sort=sort)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out["caveat"] = batch.CAVEAT
    return out


@app.get("/api/backtest/heatmap")
def api_heatmap(symbol: str, strategy: str, x_param: str, x_values: str,
                y_param: str | None = None, y_values: str | None = None,
                metric: str = "sharpe", interval: str = "day", days: int = 730,
                cost_bps: float = 10.0, capital: float = 100_000.0,
                params: str | None = None,
                sl_pct: float | None = None, tp_pct: float | None = None,
                trail_mult: float | None = None,
                trail_period: int | None = None,
                max_bars: int | None = None):
    """Sweep two parameters and report the shape of the result.

    `x_values` / `y_values` are comma-separated numbers, or `start:stop:step`.
    Read `verdict` before `analysis.best` — see heatmap.CAVEAT.
    """
    def parse(spec: str | None):
        """Comma list -> explicit values. 'a:b:c' passes through as a string,
        which heatmap._values expands — keeping the two forms distinguishable
        all the way down instead of collapsing them into one ambiguous list."""
        if not spec:
            return None
        spec = spec.strip()
        if ":" in spec:
            return spec
        try:
            return [int(v) if float(v).is_integer() else float(v)
                    for v in spec.split(",") if v.strip()]
        except ValueError:
            raise HTTPException(400, f"bad value list '{spec}' — use "
                                     f"'5,10,15' or 'start:stop:step'")

    base = None
    if params:
        try:
            base = json.loads(params)
            if not isinstance(base, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, "params must be a JSON object")

    risk = {k: v for k, v in dict(sl_pct=sl_pct, tp_pct=tp_pct,
            trail_mult=trail_mult, trail_period=trail_period,
            max_bars=max_bars).items() if v is not None}
    try:
        out = heatmap.sweep(symbol, strategy, x_param, parse(x_values),
                            y_param, parse(y_values), metric=metric,
                            interval=interval, days=days, cost_bps=cost_bps,
                            capital=capital, base_params=base,
                            risk=risk or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except SystemExit as e:
        raise HTTPException(401, str(e))
    out["caveat"] = heatmap.CAVEAT
    return out


@app.get("/api/backtest/csv")
def api_backtest_csv(symbol: str, strategy: str, interval: str = "day",
                     days: int = 730, cost_bps: float = 10.0,
                     sizing: str | None = None, risk_pct: float = 1.0,
                     sl_pct: float | None = None, regime: bool = False):
    """The trade list as CSV, in the journal's columns."""
    try:
        res = backtest.run(symbol.upper(), strategy, interval=interval,
                           days=days, cost_bps=cost_bps, regime=regime,
                           risk={"sizing": sizing, "risk_pct": risk_pct,
                                 "sl_pct": sl_pct})
    except SystemExit as e:
        # kite_auth exits the process when there's no session — correct for a
        # CLI command, fatal for a request handler. Every other route maps it
        # to a 401 and this one must too.
        raise HTTPException(401, str(e)) from None
    except (ValueError, backtest.SizingError) as e:
        raise HTTPException(400, str(e)) from None
    except Exception as e:                            # noqa: BLE001
        # A broker or network failure is upstream's fault, not the caller's.
        # /api/backtest already maps these to 502; without the same clause a
        # Kite error escapes this route as an unhandled 500.
        raise HTTPException(502, f"backtest failed: {e}") from None
    name = f"{symbol.upper()}-{strategy}-trades.csv"
    return Response(
        backtest.trades_csv(res), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/backtest/montecarlo")
async def api_montecarlo(request: Request):
    """Resample a trade list the caller already has.

    Takes the trades rather than re-running the backtest on purpose: the page
    is showing a specific result, and re-simulating risks quietly analysing a
    DIFFERENT one if a parameter drifted between the two requests. Analysing
    exactly what's on screen is worth the round trip.
    """
    body = await request.json()
    trades = body.get("trades")
    if not isinstance(trades, list):
        raise HTTPException(400, "expected {trades: [...]}")
    try:
        result = montecarlo.run(
            trades,
            sims=int(body.get("sims", montecarlo.DEFAULT_SIMS)),
            mode=str(body.get("mode", "shuffle")),
            seed=body.get("seed"),
        )
    except montecarlo.MonteCarloError as e:
        raise HTTPException(400, str(e)) from None
    return result


@app.get("/api/backtest/walkforward")
def api_walkforward(symbol: str, strategy: str, grid: str | None = None,
                    is_bars: int = 250, oos_bars: int = 60,
                    mode: str = "rolling", metric: str = "sharpe",
                    interval: str = "day", days: int = 1460,
                    cost_bps: float = 10.0, capital: float = 100_000.0,
                    sl_pct: float | None = None, tp_pct: float | None = None,
                    trail_mult: float | None = None,
                    trail_period: int | None = None,
                    max_bars: int | None = None):
    """Optimise on each in-sample window, test on the next untouched one.

    `grid` is JSON: {"fast": "5:25:5", "slow": [30, 50, 70]}. Omit it for
    plain out-of-sample testing with the strategy's default parameters.
    """
    g = None
    if grid:
        try:
            g = json.loads(grid)
            if not isinstance(g, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(400, 'grid must be a JSON object, e.g. '
                                     '{"fast":"5:25:5","slow":[30,50,70]}')
    risk = {k: v for k, v in dict(sl_pct=sl_pct, tp_pct=tp_pct,
            trail_mult=trail_mult, trail_period=trail_period,
            max_bars=max_bars).items() if v is not None}
    try:
        out = walkforward.run(symbol, strategy, g, is_bars=is_bars,
                              oos_bars=oos_bars, mode=mode, metric=metric,
                              interval=interval, days=days, cost_bps=cost_bps,
                              capital=capital, risk=risk or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except SystemExit as e:
        raise HTTPException(401, str(e))
    out["caveat"] = walkforward.CAVEAT
    return out


@app.get("/api/backtest/metrics")
def api_heatmap_metrics():
    return {"metrics": [{"key": k, **v} for k, v in heatmap.METRICS.items()]}


# ----------------------------------------------------------- chart layouts
@app.get("/api/layouts")
def api_layouts():
    with db.connect() as conn:
        rows = []
        for r in conn.execute("SELECT * FROM chart_layouts ORDER BY name"):
            d = dict(r)
            try:
                d["spec"] = json.loads(d["spec"])
            except ValueError:
                d["spec"] = {}
            rows.append(d)
    return {"layouts": rows}


@app.post("/api/layouts/{name}")
def api_layout_save(name: str, spec: str, is_default: bool = False):
    """Save (or overwrite) a chart layout: indicators, their params and style
    overrides, enabled event markers, and the interval."""
    try:
        d = json.loads(spec)
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "spec must be a JSON object")
    with db.connect() as conn:
        if is_default:
            conn.execute("UPDATE chart_layouts SET is_default=0")
        conn.execute(
            "INSERT INTO chart_layouts (name, spec, is_default) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec, "
            "is_default=excluded.is_default, "
            "updated_at=datetime('now','localtime')",
            (name, json.dumps(d), 1 if is_default else 0))
    return {"saved": name, "is_default": is_default}


@app.delete("/api/layouts/{name}")
def api_layout_delete(name: str):
    with db.connect() as conn:
        return {"deleted": conn.execute(
            "DELETE FROM chart_layouts WHERE name=?", (name,)).rowcount > 0}


# ------------------------------------------------------------------ scanner
@app.get("/api/scanner/conditions")
def api_scan_conditions():
    return {"conditions": [{"key": k, "label": v["label"],
                            "params": v["params"]}
                           for k, v in scanner.CONDITIONS.items()]}


@app.get("/api/scanner")
def api_scan(conditions: str, universe_name: str = universe.DEFAULT_UNIVERSE,
             symbols: str | None = None, days: int = scanner.DEFAULT_LOOKBACK,
             limit: int = scanner.MAX_HITS):
    """Run a scan. `conditions` is JSON: a list of keys or {key, ...params}.

    Uses stored candles only — `POST /api/universe/{name}/backfill` is the job
    that fetches.
    """
    try:
        conds = json.loads(conditions)
        if not isinstance(conds, list):
            raise ValueError
    except ValueError:
        raise HTTPException(400, 'conditions must be a JSON list, e.g. '
                                 '["52w_high",{"key":"volume_surge","mult":3}]')
    syms = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
            if symbols else None)
    try:
        return scanner.scan(conds, name=universe_name, symbols=syms,
                            days=days, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/universe")
def api_universes():
    return {"universes": universe.names(),
            "default": universe.DEFAULT_UNIVERSE}


@app.get("/api/universe/{name}")
def api_universe(name: str):
    return {"name": name, "members": universe.members(name),
            "last_backfill": universe.last_backfill(name)}


@app.post("/api/universe/{name}")
def api_universe_set(name: str, symbols: str, replace: bool = True):
    syms = [s.strip().upper() for s in symbols.replace("\n", ",").split(",")
            if s.strip()]
    if not syms:
        raise HTTPException(400, "no symbols given")
    return universe.set_members(name, syms, replace=replace)


@app.delete("/api/universe/{name}")
def api_universe_delete(name: str, symbol: str | None = None):
    return {"removed": universe.remove(name, symbol)}


@app.get("/api/universe/{name}/coverage")
def api_universe_coverage(name: str, days: int = universe.BACKFILL_DAYS):
    return universe.coverage(name, days=days)


@app.post("/api/universe/{name}/backfill")
def api_universe_backfill(name: str, only_missing: bool = True,
                          days: int = universe.BACKFILL_DAYS):
    """Fetch candles for the universe. Slow by design — it paces itself under
    Kite's rate limit. Resumable, so a timeout costs you nothing."""
    try:
        out = universe.backfill(name, days=days, only_missing=only_missing)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(401, str(e))
    universe.mark_backfilled(name)
    return out


@app.get("/api/scans")
def api_scans():
    return {"scans": universe.saved_scans()}


@app.post("/api/scans/{name}")
def api_scan_save(name: str, spec: str):
    try:
        d = json.loads(spec)
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "spec must be a JSON object")
    universe.save_scan(name, d)
    return {"saved": name, "spec": d}


@app.post("/api/scans/{name}/run")
def api_scan_run(name: str):
    try:
        return scanner.run_saved(name)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/scans/{name}")
def api_scan_delete(name: str):
    return {"deleted": universe.delete_scan(name)}


@app.get("/scanner", response_class=HTMLResponse)
def page_scanner():
    return FileResponse(WEB_DIR / "scanner.html")


# ---------------------------------------------------------------- signals
@app.get("/api/signals")
def api_signals(symbols: str | None = None, push: bool = False):
    """Run the configured strategy scan on closed daily bars.

    symbols: comma-separated override; default = the watchlist.
    push=true also sends fresh flips to the phone (the EOD watcher hook
    normally owns that; from the UI leave it false).
    """
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
    try:
        return signals.scan(symbols=syms, push=push)
    except SystemExit as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"signal scan failed: {e}")


# ------------------------------------------------------------ risk & journal
@app.get("/api/risk/size")
def api_size(entry: float, stop: float, account: float | None = None,
             risk_pct: float | None = None, lot_size: int = 1):
    """Position size from entry, stop and risk %. Pure calculation, no writes."""
    account = account if account is not None else float(config.get("ACCOUNT_SIZE", 0) or 0)
    risk_pct = risk_pct if risk_pct is not None else float(config.get("RISK_PCT", 1) or 1)
    if account <= 0:
        raise HTTPException(400, "no account size — set ACCOUNT_SIZE or pass account=")
    try:
        out = risk.size(entry, stop, account, risk_pct,
                        max_position_pct=float(config.get("MAX_POSITION_PCT", 100)),
                        lot_size=lot_size)
    except risk.RiskError as e:
        raise HTTPException(400, str(e))
    out["targets"] = risk.targets(entry, stop)
    return out


@app.get("/api/risk/atr-stop")
def api_atr_stop(symbol: str, side: str = "long", entry: float | None = None,
                 period: int = 14, mult: float = 2.0, interval: str = "day"):
    """Suggest a volatility-based stop when you don't have a level in mind."""
    inst = instruments.lookup(symbol)
    if not inst:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        kite = kite_auth.get_kite()
        candles.ensure(kite, inst["instrument_token"], interval, 120)
        c = candles.get(inst["instrument_token"], interval, 120)
    except SystemExit as e:
        raise HTTPException(401, str(e))
    out = risk.atr_stop(c, entry=entry, side=side, period=period, mult=mult)
    if not out:
        raise HTTPException(422, "not enough history for an ATR stop")
    return out


@app.get("/api/journal")
def api_journal(symbol: str | None = None, tag: str | None = None,
                limit: int = 100):
    """Open positions (with live R where a price is available), closed history,
    overall stats and the daily guard — everything the page needs in one call."""
    opens = journal.open_positions(symbol)
    for t in opens:
        try:
            inst = instruments.lookup(t["symbol"])
            c = candles.get(inst["instrument_token"], "day", 5) if inst else []
            t["last_price"] = c[-1]["close"] if c else None
            t["live_r"] = journal.live_r(t, t["last_price"]) if t["last_price"] else None
        except Exception:
            t["last_price"] = t["live_r"] = None
    return {
        "open": opens,
        "closed": journal.closed_trades(symbol=symbol, tag=tag, limit=limit),
        "stats": journal.stats(),
        "by_tag": journal.by_tag(),
        "guard": journal.guard(
            daily_loss_r=float(config.get("DAILY_LOSS_R", 2)),
            max_positions=int(config.get("MAX_POSITIONS", 5))),
        "settings": {
            "account": float(config.get("ACCOUNT_SIZE", 0) or 0),
            "risk_pct": float(config.get("RISK_PCT", 1) or 1),
            "daily_loss_r": float(config.get("DAILY_LOSS_R", 2)),
            "max_positions": int(config.get("MAX_POSITIONS", 5)),
        },
    }


@app.post("/api/journal")
def api_journal_open(symbol: str, entry: float, qty: int,
                     stop: float | None = None, side: str = "long",
                     target: float | None = None, tag: str | None = None,
                     notes: str | None = None):
    inst = instruments.lookup(symbol)
    try:
        tid = journal.open_trade(
            inst["tradingsymbol"] if inst else symbol.upper(),
            entry=entry, qty=qty, stop=stop, side=side, target=target,
            tag=tag, notes=notes, source="web")
    except journal.JournalError as e:
        raise HTTPException(400, str(e))
    return journal.get(tid)


@app.post("/api/journal/{trade_id}/close")
def api_journal_close(trade_id: int, exit_price: float, reason: str = "manual",
                      charges: float | None = None, notes: str | None = None):
    try:
        return journal.close_trade(trade_id, exit_price=exit_price,
                                   reason=reason, charges=charges, notes=notes)
    except journal.JournalError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/journal/{trade_id}")
def api_journal_update(trade_id: int, stop: float | None = None,
                       target: float | None = None, tag: str | None = None,
                       notes: str | None = None):
    fields = {k: v for k, v in dict(stop=stop, target=target, tag=tag,
                                    notes=notes).items() if v is not None}
    try:
        return journal.update_trade(trade_id, **fields)
    except journal.JournalError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/journal/{trade_id}")
def api_journal_delete(trade_id: int):
    return {"deleted": journal.delete_trade(trade_id)}


@app.get("/api/journal/compare")
def api_journal_compare(tag: str | None = None, symbol: str | None = None,
                        interval: str = "day", days: int = 730,
                        cost_bps: float = 10.0):
    """Live results beside the backtest's, per strategy tag.

    This is the point of the journal. For each tag that matches a strategy in
    STRATEGIES, run that strategy over the symbols you actually traded it on
    and report both sets of numbers.

    The two are NOT strictly comparable and the response says so per row. The
    backtest takes every signal the rule generates; you took the ones you saw,
    on the days you were watching, at fills you actually got. A gap is
    information — it just isn't automatically 'the edge decayed'. Small
    samples especially: expectancy over 8 trades is barely a number.
    """
    tags = [tag] if tag else [t for t in journal.by_tag() if t in backtest.STRATEGIES]
    rows = []
    for t in tags:
        live_trades = journal.closed_trades(tag=t, symbol=symbol, limit=100_000)
        live = journal.stats(live_trades)
        row = {"tag": t, "name": backtest.STRATEGIES.get(t, {}).get("name", t),
               "live": live, "backtest": None, "symbols": [], "note": ""}

        if t not in backtest.STRATEGIES:
            row["note"] = "no matching strategy — discretionary tag"
            rows.append(row); continue

        syms = sorted({x["symbol"] for x in live_trades})
        row["symbols"] = syms
        agg, errors = [], []
        for s in syms:
            try:
                r = backtest.run(s, t, interval=interval, days=days,
                                 cost_bps=cost_bps)
                agg.append(r["stats"])
            except (Exception, SystemExit) as e:
                # SystemExit, not just Exception: backtest.run reaches for a
                # Kite session and config.require raises SystemExit when there
                # isn't one — which is the normal state every morning before
                # login. A missing session should empty the backtest column,
                # not 500 the page.
                errors.append(f"{s}: {e}")
        if agg:
            n = sum(a["trades"] for a in agg)
            exp = [a["expectancy_pct"] for a in agg if a["expectancy_pct"] is not None]
            wr = [a["win_rate_pct"] for a in agg if a["win_rate_pct"] is not None]
            pf = [a["profit_factor"] for a in agg if a["profit_factor"] is not None]
            row["backtest"] = {
                "trades": n,
                "expectancy_pct": round(sum(exp) / len(exp), 2) if exp else None,
                "win_rate_pct": round(sum(wr) / len(wr), 1) if wr else None,
                "profit_factor": round(sum(pf) / len(pf), 2) if pf else None,
                "symbols_run": len(agg),
            }
        if errors:
            row["note"] = "; ".join(errors[:3])
        if live["trades"] and live["trades"] < 20:
            row["note"] = ((row["note"] + " · ") if row["note"] else "") + \
                f"only {live['trades']} live trades — too few to conclude much"
        rows.append(row)

    return {
        "rows": rows,
        "caveat": ("Backtest expectancy is in % per trade; live is in R. They "
                   "answer the same question in different units — read the "
                   "direction and the win rate, not the absolute gap. The "
                   "backtest also takes every signal, which you did not."),
    }


# ------------------------------------------------------------------ portfolio
@app.get("/api/portfolio")
def api_portfolio(annotate: bool = True, refresh: bool = False):
    """Holdings + positions from Kite, annotated with what we already know.

    Read-only. This route never places, modifies or cancels anything.
    """
    if refresh:
        portfolio.clear_cache()
    try:
        return portfolio.snapshot(annotate_rows=annotate)
    except portfolio.PortfolioError as e:
        # No session is the normal state before the morning login, so this is
        # a 401 the UI can act on rather than a 502 it can only display.
        raise HTTPException(401 if "session" in str(e).lower() else 502, str(e))


@app.post("/api/portfolio/sync-watchlist")
def api_portfolio_sync():
    """Add holdings that aren't on the watchlist yet. Additive only."""
    try:
        return portfolio.sync_watchlist()
    except portfolio.PortfolioError as e:
        raise HTTPException(401, str(e))


# ---------------------------------------------------------------- live ticks
@app.websocket("/ws/ticks")
async def ws_ticks(ws: WebSocket, token: int):
    await ws.accept()
    access = kite_auth.load_token()
    if not access:
        await ws.send_text(json.dumps({"error": "No Kite session — log in first."}))
        await ws.close()
        return
    hub.start(config.KITE_API_KEY, access, asyncio.get_running_loop())
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    hub.subscribe(token, queue)
    try:
        while True:
            tick = await queue.get()
            await ws.send_text(json.dumps(tick))
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(token, queue)
