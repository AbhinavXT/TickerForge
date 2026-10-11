"""The home-side alert watcher: polls quotes, fires armed alerts to your phone.

Efficiency: one quote() call per cycle covers every symbol that has an armed
alert. Day-static levels (PDH/PDL/pivots) and the opening range are computed
once and cached, so a cycle is essentially a single network round-trip.

Robustness for the office use-case: a session heartbeat notices when the Kite
token has expired (it dies daily ~7:30am) and pings you once to log in, instead
of going silently blind.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from . import alerts, charts, instruments, intraday, kite_auth, notify

_MO = intraday.MARKET_OPEN
_MC = intraday.MARKET_CLOSE


def should_watch(now=None) -> bool:
    lt = now or datetime.now()
    if lt.weekday() >= 5:
        return False
    hm = lt.hour * 60 + lt.minute
    return _MO[0] * 60 + _MO[1] <= hm <= _MC[0] * 60 + _MC[1]


_INTERVAL_SECONDS = {
    "minute": 60, "3minute": 180, "5minute": 300, "10minute": 600,
    "15minute": 900, "30minute": 1800, "60minute": 3600, "day": 86400,
}

# How many bars a cached candle set may fall behind before we stop trusting it.
_MAX_STALE_BARS = 2
_WARN_REPEAT = 1800          # same warning at most once per 30 min

_warned: dict[str, float] = {}


def _login_link() -> str | None:
    """Only expiry warnings get a click target — a push that opens the login
    page for an unrelated problem trains you to tap without reading."""
    try:
        from . import kite_auth
        return kite_auth.login_click_url()
    except Exception:                             # noqa: BLE001
        return None


def _warn(key: str, msg: str, click_url: str | None = None) -> None:
    """Terminal AND phone, deduped by `key`. Anything the watcher can't do is
    invisible by construction — nobody is watching the terminal during market
    hours, that's the entire reason the phone push exists."""
    now = time.time()
    if now - _warned.get(key, 0) < _WARN_REPEAT:
        return
    _warned[key] = now
    print(f"[{datetime.now():%H:%M:%S}] {msg}")
    try:
        notify.send(msg, title="TickerForge", priority="high", tags="warning",
                    click_url=click_url)
    except (Exception, SystemExit) as e:
        print(f"  (warn push failed: {e})")


class _IndCache:
    """Candles per (token, interval), refetched only when a new bar opens.

    This is what keeps indicator alerts cheap AND fast: the expensive
    historical_data call happens once per bar, while every poll re-evaluates
    the indicators against the *live* price patched into the forming bar — so
    a cross is caught the moment price moves, not at the next bar close.
    """

    def __init__(self):
        self.data: dict[tuple, dict] = {}

    def candles(self, kite, token: int, interval: str) -> list[dict]:
        from . import candles as cstore
        key = (token, interval)
        secs = _INTERVAL_SECONDS.get(interval, 300)
        cur_bar = int(time.time() // secs)
        ent = self.data.get(key)
        if ent and ent["bar"] == cur_bar:
            return ent["candles"]
        rows = ent["candles"] if ent else []
        good_bar = ent.get("good_bar") if ent else None
        try:
            cstore.ensure(kite, token, interval)
            days = 400 if interval == "day" else 12
            fetched = cstore.get(token, interval, days)
            if fetched:
                rows = fetched
                good_bar = cur_bar
                _warned.pop(f"stale:{token}:{interval}", None)
        except Exception as e:
            # Serving the previous bars is right for a blip — one failed call
            # shouldn't drop an alert. But indicators computed on candles that
            # stopped updating, with a live price patched onto the end, are
            # confidently wrong rather than merely late, and the old code would
            # do that forever in silence. So there's a limit.
            behind = cur_bar - good_bar if good_bar is not None else None
            if behind is None or behind > _MAX_STALE_BARS:
                _warn(f"stale:{token}:{interval}",
                      f"Candle fetch failing for token {token} ({interval}) — "
                      f"indicator alerts on it are paused rather than run on "
                      f"stale bars. Last error: {e}")
                rows = []          # bundle() returns None; the alert is skipped
            else:
                print(f"[{datetime.now():%H:%M:%S}] candle fetch failed for "
                      f"{token} ({interval}), using bars from {behind} bar(s) "
                      f"ago: {e}")
        self.data[key] = {"candles": rows, "bar": cur_bar, "good_bar": good_bar}
        return rows

    def bundle(self, kite, token: int, interval: str, ltp) -> dict | None:
        """Indicator set computed on closed bars + the live price."""
        from . import indicators
        rows = self.candles(kite, token, interval)
        if not rows:
            return None
        series = [dict(r) for r in rows]
        if ltp is not None:
            b = series[-1]
            series[-1] = {**b, "close": ltp,
                          "high": max(b.get("high", ltp), ltp),
                          "low": min(b.get("low", ltp), ltp)}
        try:
            return indicators.compute(series)
        except Exception:
            return None


class _DayCache:
    """Per-day cache of static levels + opening range, keyed by token."""
    def __init__(self):
        self.day = None
        self.static: dict[int, dict] = {}
        self.orb: dict[int, dict | None] = {}

    def roll(self):
        today = datetime.now().date()
        if self.day != today:
            self.day, self.static, self.orb = today, {}, {}

    def statics(self, kite, token):
        if token not in self.static:
            self.static[token] = intraday.levels_static(kite, token)
        return self.static[token]

    def opening(self, kite, token):
        # None until the range completes; re-check until we get it, then cache.
        if self.orb.get(token) is None:
            self.orb[token] = intraday.opening_range(kite, token)
        return self.orb.get(token)


def confluence_for(kite, ind_cache, token: int, ltp,
                   bundles: dict | None = None, tfs=None) -> dict | None:
    """Build a multi-timeframe confluence read for one symbol, reusing any
    indicator bundles already computed this cycle."""
    from . import confluence as C
    bundles = bundles if bundles is not None else {}
    tfs = tfs or C.DEFAULT_TFS
    per: dict[str, dict] = {}
    for tf in tfs:
        key = (token, tf)
        if key not in bundles:
            bundles[key] = ind_cache.bundle(kite, token, tf, ltp)
        b = bundles.get(key)
        if b:
            per[tf] = b
    if not per:
        return None
    return C.build(per)


def _fire(kite, lv: dict, token: int, reason: str, highlight=None) -> None:
    """Push the alert — with a chart image if we can render one, else text."""
    caption = f"🔔 {reason}  ·  {intraday.one_line(lv)}"
    if lv.get("ind"):
        from . import indicators
        extra = indicators.summary_line(lv["ind"])
        if extra:
            caption += f"  ·  {extra}"
    # An alert is a nudge toward a trade. If today's loss limit is already
    # gone, the alert should say so on its face rather than arriving looking
    # like an invitation — the moment you're deciding whether to "make it
    # back" is exactly when the reminder has to be in front of you.
    try:
        from . import journal
        if journal.stood_down_today():
            caption += "\n⛔ daily loss limit already reached today"
    except Exception:
        pass
    try:
        candles = intraday.today_minutes(kite, token, "5minute")
        png = charts.intraday_png(lv, candles, highlight=highlight,
                                  subtitle=datetime.now().strftime("%H:%M"))
        # send_image reports failure by return value, not by raising; turn
        # that into the same text fallback a chart error gets.
        if not notify.send_image(png, caption=caption, title="TickerForge alert",
                                 tags="rotating_light", priority="high"):
            raise RuntimeError("image push failed")
    except (Exception, SystemExit):
        body = reason + "\n\n" + intraday.as_text(lv)
        notify.send(body, title="TickerForge alert",
                    tags="rotating_light", priority="high")


def poll_alerts(kite, cache: _DayCache, ind_cache: "_IndCache | None" = None) -> int:
    """One cycle. Returns the number of alerts fired."""
    rows = alerts.active()
    if not rows:
        return 0
    cache.roll()

    # resolve tokens + batch one quote() for all symbols
    keymap: dict[str, tuple[str, int]] = {}     # "NSE:SYM" -> (SYM, token)
    for r in rows:
        sym = r["symbol"]
        tok = r["token"]
        if not tok:
            inst = instruments.lookup(sym)
            tok = inst["instrument_token"] if inst else None
        if tok:
            keymap[f"NSE:{sym}"] = (sym, tok)
    quotes = intraday.quote_batch(kite, list(keymap)) if keymap else {}

    # build a levels snapshot per symbol once
    lv_by_sym: dict[str, dict] = {}
    for key, (sym, tok) in keymap.items():
        q = quotes.get(key)
        if not q:
            continue
        snap = intraday.normalize_quote(q)
        lv_by_sym[sym] = intraday.build(
            sym, tok, snap, cache.statics(kite, tok), cache.opening(kite, tok))

    fired = 0
    bundles: dict[tuple, dict] = {}         # (token, interval) -> indicators
    for r in rows:
        lv = lv_by_sym.get(r["symbol"])
        if not lv:
            continue
        if alerts.cooling(r):               # repeating alert, still cooling off
            continue

        params = {}
        try:
            params = json.loads(r["params"] or "{}")
        except (TypeError, ValueError):
            pass

        if r["kind"] in alerts.CONFLUENCE_KINDS and ind_cache is not None:
            tok = r["token"] or lv.get("token")
            conf = confluence_for(kite, ind_cache, tok, lv.get("ltp"), bundles)
            if not conf:
                continue
            lv = {**lv, "conf": conf}
        elif r["kind"] in alerts.INDICATOR_KINDS and ind_cache is not None:
            tok = r["token"] or lv.get("token")
            tf = params.get("tf", alerts.DEFAULT_TF)
            key = (tok, tf)
            if key not in bundles:
                bundles[key] = ind_cache.bundle(kite, tok, tf, lv.get("ltp"))
            ind = bundles.get(key)
            if not ind:
                continue
            lv = {**lv, "ind": ind}

        hit, reason, new_state = alerts.evaluate(r, lv)
        if new_state is not None and not hit:
            alerts.apply_state(r["id"], new_state)     # seed cross/zone side
        if hit:
            _fire(kite, lv, r["token"] or lv["token"], reason,
                  highlight=lv.get("ltp"))
            if params.get("repeat"):
                secs = _INTERVAL_SECONDS.get(params.get("tf"), 300)
                alerts.rearm(r["id"], new_state or {}, cooldown_s=secs)
            else:
                alerts.disarm(r["id"], reason)
            fired += 1
    return fired


_STALL_SECONDS = 300        # no tick for this long, mid-session, is suspicious
_STALL_REPEAT = 1800        # don't re-warn about the same drought inside 30 min


def run_ticks(always: bool = False, throttle: float = 1.0) -> None:
    """Tick-driven watcher — the lowest-latency mode.

    Instead of polling quotes on a timer, this subscribes to Kite's websocket
    and re-evaluates a symbol's alerts the moment its price moves. Indicators
    are recomputed against the live price on each tick (throttled per symbol so
    a fast-moving stock can't peg the CPU), with the underlying candles still
    refetched only once per bar.

    Three things can take this watcher off the air without saying so, and all
    three are covered here:

      * The Kite token dies every morning around 07:30. The cached token is
        re-read each cycle, so logging in revives the stream in place — no
        restart — and a session that's simply gone pings the phone once a day
        rather than leaving a dead socket reconnecting into the void.
      * The stream can be up and silent. If nothing arrives for five minutes
        while the market is open and symbols are subscribed, that's a push,
        not a log line nobody reads.
      * The daily signal scan runs from here too. It used to live only in the
        polling watcher, so anyone running the tick watcher — the mode worth
        running — quietly got no EOD signals at all.
    """
    from kiteconnect import KiteTicker
    from . import config, kite_auth, signals

    token_str = kite_auth.load_token()
    api_key = config.get("KITE_API_KEY")
    if not token_str or not api_key:
        print("No Kite session — run `login` first."); return

    cache, ind_cache = _DayCache(), _IndCache()
    kite = kite_auth.get_kite()
    last_eval: dict[int, float] = {}
    sub_tokens: set[int] = set()
    lock = threading.Lock()
    last_tick = time.time()

    def nag(key: str, msg: str) -> None:
        """Say it on the terminal AND on the phone — the terminal is the one
        place we know nobody is looking when this matters."""
        _warn(f"ticks:{key}", msg,
              click_url=_login_link() if key == "session" else None)


    def armed_map() -> dict[int, list]:
        """token -> its armed alert rows."""
        out: dict[int, list] = {}
        for r in alerts.active():
            tok = r["token"]
            if not tok:
                inst = instruments.lookup(r["symbol"])
                tok = inst["instrument_token"] if inst else None
            if tok:
                out.setdefault(tok, []).append(r)
        return out

    def handle_tick(t):
        tok = t.get("instrument_token")
        now = time.time()
        if now - last_eval.get(tok, 0) < throttle:
            return
        last_eval[tok] = now
        rows = armed.get(tok) or []
        if not rows:
            return
        ohlc = t.get("ohlc") or {}
        snap = {
            "ltp": t.get("last_price"), "vwap": t.get("average_price"),
            "open": ohlc.get("open"), "high": ohlc.get("high"),
            "low": ohlc.get("low"), "prev_close": ohlc.get("close"),
            "volume": t.get("volume_traded") or t.get("volume"),
            "chg_pct": (round(t["change"], 2) if t.get("change") is not None
                        else None),
        }
        sym = rows[0]["symbol"]
        lv = intraday.build(sym, tok, snap, cache.statics(kite, tok),
                            cache.opening(kite, tok))
        bundles: dict[str, dict] = {}
        for r in rows:
            if alerts.cooling(r):
                continue
            try:
                params = json.loads(r["params"] or "{}")
            except (TypeError, ValueError):
                params = {}
            lv_r = lv
            if r["kind"] in alerts.CONFLUENCE_KINDS:
                conf = confluence_for(kite, ind_cache, tok, lv.get("ltp"))
                if not conf:
                    continue
                lv_r = {**lv, "conf": conf}
            elif r["kind"] in alerts.INDICATOR_KINDS:
                tf = params.get("tf", alerts.DEFAULT_TF)
                if tf not in bundles:
                    b = ind_cache.bundle(kite, tok, tf, lv.get("ltp"))
                    if not b:
                        continue
                    bundles[tf] = b
                lv_r = {**lv, "ind": bundles[tf]}
            hit, reason, new_state = alerts.evaluate(r, lv_r)
            if new_state is not None and not hit:
                alerts.apply_state(r["id"], new_state)
            if hit:
                _fire(kite, lv_r, tok, reason, highlight=lv_r.get("ltp"))
                if params.get("repeat"):
                    secs = _INTERVAL_SECONDS.get(params.get("tf"), 300)
                    alerts.rearm(r["id"], new_state or {}, cooldown_s=secs)
                else:
                    alerts.disarm(r["id"], reason)
                print(f"[{datetime.now():%H:%M:%S}] {reason}")

    armed = armed_map()

    def on_connect(ws, response):
        toks = list(armed.keys())
        with lock:
            sub_tokens.clear()          # a fresh socket carries no subscriptions
            if toks:
                ws.subscribe(toks)
                ws.set_mode(ws.MODE_QUOTE, toks)
                sub_tokens.update(toks)
        print(f"streaming {len(toks)} symbol(s)")

    def on_ticks(ws, ticks):
        nonlocal last_tick
        last_tick = time.time()         # proof of life, throttling notwithstanding
        for t in ticks:
            try:
                handle_tick(t)
            except Exception as e:
                print(f"tick error: {e}")

    def build_ticker(access_token: str):
        ws = KiteTicker(api_key, access_token)
        ws.on_connect = on_connect
        ws.on_ticks = on_ticks
        ws.on_close = lambda w, code, reason: print(f"stream closed: {reason}")
        ws.on_reconnect = lambda w, attempts: print(f"reconnecting ({attempts})…")
        ws.on_error = lambda w, code, reason: print(f"stream error {code}: {reason}")
        ws.connect(threaded=True)
        return ws

    kws = build_ticker(token_str)
    print("Tick watcher up — alerts fire the moment price moves. Ctrl+C to stop.")

    session_nagged = None       # date we last nagged about a missing session
    stall_nagged = 0.0          # when we last warned about a silent stream
    was_watching = should_watch()

    try:
        while True:
            time.sleep(10)

            # The EOD signal scan fires at 15:35 — five minutes AFTER
            # should_watch() goes false — so it has to run before the
            # market-hours gate, not after it.
            try:
                signals.maybe_eod_scan()
            except (Exception, SystemExit) as e:
                print(f"[signals] EOD hook error: {e}")

            # The other end of the same loop: last night's queue fills at
            # this morning's open, at 09:20, without anyone being at the
            # machine. Same gate reasoning as the scan above — it runs before
            # the market-hours check, and no-ops cheaply the rest of the day.
            try:
                from . import paper
                paper.maybe_morning_fill()
            except (Exception, SystemExit) as e:
                print(f"[paper] morning fill hook error: {e}")

            # Trades are closed from the phone, the CLI and the web, none of
            # which run here — so the limit can be breached without this
            # process ever seeing it. Re-check each cycle; the notice itself
            # is deduped per day, so this is cheap and idempotent.
            try:
                from . import journal
                journal.notify_if_breached()
            except (Exception, SystemExit) as e:
                print(f"[journal] guard check error: {e}")

            # Session. The token dies daily; re-reading it here means a login
            # revives the stream in place instead of needing a restart.
            current = kite_auth.load_token()
            today = datetime.now().date()
            if not current:
                if session_nagged != today:
                    session_nagged = today
                    nag("session", "Kite session expired — log in to keep tick alerts live.")
                continue
            if current != token_str:
                print("new Kite session — rebuilding the stream")
                token_str = current
                try:
                    kws.close()
                except Exception:
                    pass
                with lock:
                    sub_tokens.clear()
                kws = build_ticker(token_str)
                kite = kite_auth.get_kite()
                last_tick = time.time()
                session_nagged = None
                continue

            watching = should_watch()
            if watching and not was_watching:
                last_tick = time.time()      # fresh clock at the open, or no
            was_watching = watching          # ticks overnight reads as a stall
            if not (always or watching):
                continue

            armed = armed_map()                      # pick up new/removed alerts
            want = set(armed.keys())
            with lock:
                new = want - sub_tokens
                gone = sub_tokens - want
                if new:
                    kws.subscribe(list(new))
                    kws.set_mode(kws.MODE_QUOTE, list(new))
                    sub_tokens.update(new)
                if gone:
                    try:
                        kws.unsubscribe(list(gone))
                    except Exception:
                        pass
                    sub_tokens.difference_update(gone)

            # Subscribed, market open, nothing arriving: the socket can be up
            # and useless, which looks exactly like a quiet tape from here.
            now_s = time.time()
            if watching and want and now_s - last_tick > _STALL_SECONDS \
                    and now_s - stall_nagged > _STALL_REPEAT:
                stall_nagged = now_s
                nag("stall",
                    f"No ticks for {int(now_s - last_tick)//60} min on "
                    f"{len(want)} subscribed symbol(s) during market hours — "
                    f"alerts may not be firing.")
    except KeyboardInterrupt:
        print("\nwatcher stopped.")
        try:
            kws.close()
        except Exception:
            pass


def run(every: int = 5, always: bool = False) -> None:
    """Loop forever. Polls every `every`s during market hours (or always)."""
    print(f"Intraday alert watcher up — polling every {every}s "
          f"({'24/7' if always else 'market hours'}). Ctrl+C to stop.")
    cache = _DayCache()
    ind_cache = _IndCache()
    warned_session = None          # date we last nagged about login
    idle_note = False
    while True:
        try:
            from . import signals, journal
            signals.maybe_eod_scan()      # once per trading day, after close
            try:
                journal.notify_if_breached()   # deduped per day
            except (Exception, SystemExit) as e:
                print(f"[journal] guard check error: {e}")
            if not (always or should_watch()):
                if not idle_note:
                    print(f"[{datetime.now():%H:%M}] outside market hours — idling")
                    idle_note = True
                time.sleep(30)
                continue
            idle_note = False
            try:
                kite = kite_auth.get_kite()
            except SystemExit:
                today = datetime.now().date()
                if warned_session != today:
                    warned_session = today
                    try:
                        notify.send("Kite session expired — tap to log in and "
                                    "keep intraday alerts live.",
                                    click_url=_login_link(),
                                    title="TickerForge", priority="high",
                                    tags="warning")
                    except (Exception, SystemExit):
                        pass
                    print("no Kite session; nagged once, will keep checking")
                time.sleep(60)
                continue
            fired = poll_alerts(kite, cache, ind_cache)
            if fired:
                print(f"[{datetime.now():%H:%M:%S}] fired {fired} alert(s)")
            time.sleep(every)
        except KeyboardInterrupt:
            print("\nwatcher stopped."); return
        except (Exception, SystemExit) as e:
            # SystemExit too: an expired token makes get_kite raise it, and
            # that is exactly the morning this loop must survive rather than
            # exit and leave every armed alert dead.
            print(f"cycle error: {e}; retrying in {every}s")
            time.sleep(every)
