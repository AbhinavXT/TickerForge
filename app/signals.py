"""Daily strategy-signal scanner — closes the backtest -> live loop.

The backtester tells you which STRATEGIES rules survive on a stock; this module
then runs those exact rules (same functions, same indicator maths) over the
watchlist every evening and tells your phone when one flips:

    ▲ BEL — Supertrend flipped LONG @ 412.30
    ▽ INFY — EMA crossover flipped FLAT @ 1,498.10

Honesty rules, mirrored from the engine:
  * Signals are evaluated on CLOSED daily bars only. During market hours the
    forming candle is dropped, so a signal can never appear intraday and then
    un-happen by the close — what you're notified of is exactly what the
    backtest would have traded.
  * A flip is `pos[-1] != pos[-2]` on the same series the backtester uses.
  * Dedup is persistent (SQLite): each (symbol, strategy) remembers the last
    bar it notified for, so re-running a scan never re-sends old flips.

Config: db meta key `signals_config` as JSON, e.g.
    {"strategies": [{"key": "supertrend"},
                    {"key": "ema_cross", "params": {"fast": 21, "slow": 55}}]}
Absent that, DEFAULT_SCAN below applies. Keep the list to rules you have
actually backtested on your names — the scanner is a messenger, not an edge.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import candles, db, instruments, kite_auth, notify, watchlist
from .backtest import STRATEGIES

# strategies scanned when no `signals_config` is set: one per school, defaults
DEFAULT_SCAN = ["supertrend", "ema_cross", "donchian_breakout",
                "momentum", "pullback_trend", "macd_zero"]

_CLOSE_HM = (15, 35)          # treat today's daily bar as closed after 15:35 IST
_LOOKBACK_DAYS = 900          # calendar days -> ~600 bars, enough for SMA200 etc.
_MIN_BARS = 60


# ---------------------------------------------------------------- config
def scan_config() -> list[tuple[str, dict]]:
    """[(strategy_key, params)] to scan, from db meta or defaults."""
    raw = db.get_meta("signals_config")
    if raw:
        try:
            cfg = json.loads(raw)
            out = []
            for s in cfg.get("strategies", []):
                key = s.get("key")
                if key in STRATEGIES:
                    out.append((key, {**STRATEGIES[key]["params"],
                                      **(s.get("params") or {})}))
            if out:
                return out
        except (ValueError, AttributeError):
            pass
    return [(k, dict(STRATEGIES[k]["params"])) for k in DEFAULT_SCAN]


def set_scan_config(strategies: list[dict]) -> None:
    db.set_meta("signals_config", json.dumps({"strategies": strategies}))


# ---------------------------------------------------------------- evaluate
def drop_forming_bar(c: list[dict], now: datetime | None = None) -> list[dict]:
    """Remove today's candle while the session is still open.

    Candle times are unix + IST offset (see candles.IST_OFFSET), so reading
    them as UTC yields IST wall time.
    """
    if not c:
        return c
    now = now or datetime.now()
    last = datetime.fromtimestamp(c[-1]["time"], tz=timezone.utc)
    if last.date() == now.date() and \
            (now.hour, now.minute) < _CLOSE_HM:
        return c[:-1]
    return c


def evaluate(c: list[dict], config: list[tuple[str, dict]]) -> dict:
    """Pure: run each configured strategy and report its current stance.

    -> {key: {"pos": 0|1, "prev": 0|1, "since": idx, "bars_ago": int,
              "name": str, "params": dict}}

    `since` is the index of the bar on which the current stance BEGAN, which
    is not always the last bar. A flip that happened on Tuesday is still the
    same flip when you look on Thursday, and the price that matters is
    Tuesday's close, not Thursday's — so the caller can report where the
    signal actually fired rather than where the tape happens to be now.
    """
    out = {}
    for key, params in config:
        spec = STRATEGIES.get(key)
        if not spec or len(c) < 3:
            continue
        pos = spec["fn"](c, params)
        since = len(pos) - 1
        while since > 0 and pos[since - 1] == pos[-1]:
            since -= 1
        out[key] = {"pos": pos[-1], "prev": pos[-2],
                    "since": since, "bars_ago": len(pos) - 1 - since,
                    "name": spec["name"], "params": params}
    return out


# ---------------------------------------------------------------- state
def _state(symbol: str, strategy: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state, bar_time FROM signal_state "
            "WHERE symbol=? AND strategy=?", (symbol, strategy)).fetchone()
        return (row["state"], row["bar_time"]) if row else (None, None)


def _store(symbol: str, strategy: str, state: int, bar_time: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO signal_state (symbol, strategy, state, bar_time, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(symbol, strategy) DO UPDATE SET "
            "state=excluded.state, bar_time=excluded.bar_time, "
            "updated_at=excluded.updated_at",
            (symbol, strategy, state, bar_time))


# ---------------------------------------------------------------- scan
def _flip_line(f: dict) -> str:
    """One flip, phone-sized. A stale flip must say so — otherwise a signal
    found three days late reads exactly like one that fired at today's close,
    and you'd size a fresh entry off a price the market has long left."""
    arrow = "▲" if f["action"] == "LONG" else "▽"
    line = (f"{arrow} {f['symbol']} — {f['name']} flipped {f['action']} "
            f"@ {f['price']}")
    ago = f.get("bars_ago", 0)
    if ago:
        line += (f"  ({ago} bar{'s' if ago > 1 else ''} ago; "
                 f"now {f.get('last_price', '?')})")
    return line


def scan(symbols: list[str] | None = None, push: bool = True,
         kite=None, record: bool | None = None) -> dict:
    """Run the configured strategies over the watchlist on closed daily bars.

    Returns {"flips": [...], "matrix": {sym: {strategy: 0|1}}, ...}.

    A flip is detected by comparing the current stance against the LAST STANCE
    WE RECORDED, not against the previous bar. Bar-to-bar comparison only ever
    sees flips that happened on the most recent close, so any flip that landed
    while the watcher was down — a long weekend, a crashed process, a morning
    without a login — was seen as long-vs-long and reported as nothing, then
    quietly written to state as if it had been handled. Comparing against
    stored state means a Tuesday flip found on Thursday is still reported,
    carrying Tuesday's bar and price plus how stale it is.

    `record` controls whether the new stance is written back, and defaults to
    `push`. State means "the last stance we told you about", so a read-only
    scan (the /signals command, the web page) must not advance it — otherwise
    glancing at the page in the afternoon eats the evening's push.
    """
    kite = kite or kite_auth.get_kite()
    record = push if record is None else record
    cfg = scan_config()
    syms = symbols or [w["tradingsymbol"] for w in watchlist.items()]
    flips, matrix, errors = [], {}, []

    for sym in syms:
        inst = instruments.lookup(sym)
        if not inst:
            errors.append(f"{sym}: unknown symbol")
            continue
        tok = inst["instrument_token"]
        try:
            candles.ensure(kite, tok, "day", _LOOKBACK_DAYS)
            c = drop_forming_bar(candles.get(tok, "day", _LOOKBACK_DAYS))
        except Exception as e:
            errors.append(f"{sym}: {e}")
            continue
        if len(c) < _MIN_BARS:
            errors.append(f"{sym}: only {len(c)} bars")
            continue

        bar_t = c[-1]["time"]
        ev = evaluate(c, cfg)
        matrix[sym] = {k: v["pos"] for k, v in ev.items()}

        for key, v in ev.items():
            prev_state, prev_bar = _state(sym, key)
            if prev_state is None:
                # Never seen this pair before (new symbol, new strategy). No
                # recorded stance to compare against, so fall back to the
                # bar-to-bar test — reporting the whole history as one flip
                # would be noise.
                flipped = v["pos"] != v["prev"]
            else:
                flipped = v["pos"] != prev_state

            if flipped:
                at = c[v["since"]]           # the bar the stance began on
                flips.append({
                    "symbol": sym, "strategy": key, "name": v["name"],
                    "action": "LONG" if v["pos"] == 1 else "FLAT",
                    "price": round(at["close"], 2), "bar_time": at["time"],
                    "bars_ago": v["bars_ago"],
                    "last_price": round(c[-1]["close"], 2),
                })
            if record:
                _store(sym, key, v["pos"], bar_t)

    result = {"time": datetime.now().isoformat(timespec="seconds"),
              "strategies": [{"key": k, "name": STRATEGIES[k]["name"],
                              "params": p} for k, p in cfg],
              "symbols": list(matrix.keys()), "matrix": matrix,
              "flips": flips, "errors": errors}

    if push and flips:
        lines = [_flip_line(f) for f in flips]
        try:
            notify.send("\n".join(lines), title="Strategy signals",
                        tags="chart_with_upwards_trend", priority="high")
        except (Exception, SystemExit) as e:
            result["errors"].append(f"notify: {e}")
    return result


def summary_text(symbol: str | None = None) -> str:
    """Phone-friendly stance summary for /signals."""
    res = scan(symbols=[symbol.upper()] if symbol else None, push=False)
    if not res["matrix"]:
        return "No watchlist symbols scanned." + \
               (f" ({'; '.join(res['errors'])})" if res["errors"] else "")
    names = {s["key"]: s["name"] for s in res["strategies"]}
    lines = []
    if res["flips"]:
        lines.append("Unreported flips:")
        lines += ["  " + _flip_line(f) for f in res["flips"]]
        lines.append("")
    lines.append("Current stance (● long · ○ flat):")
    for sym, row in sorted(res["matrix"].items()):
        longs = [names[k] for k, v in row.items() if v == 1]
        n_long = len(longs)
        dots = "●"*n_long + "○"*(len(row)-n_long)
        detail = f" — {', '.join(longs)}" if longs else ""
        lines.append(f"  {sym} {dots}{detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------- EOD hook
def _push_saved_scans(limit: int = 6) -> int:
    """Append each saved scan's top hits to the evening push.

    Only scans marked `eod: true` in their spec run here. A scanner that pushes
    every saved scan every night is a scanner you mute — opting in per scan
    keeps the evening message worth reading.

    Isolated from the signal scan entirely: a scanner failure must not cost you
    the flips, which are the part you actually trade.
    """
    from . import scanner, universe as U
    sent = 0
    for s in U.saved_scans():
        if not s["spec"].get("eod"):
            continue
        try:
            res = scanner.run_saved(s["name"])
        except (Exception, SystemExit) as e:
            print(f"[scanner] saved scan '{s['name']}' failed: {e}")
            continue
        if not res["hits"]:
            continue
        try:
            notify.send(scanner.as_text(res, limit=limit),
                        title=f"Scan: {s['name']}",
                        tags="mag", priority="default")
            sent += 1
        except (Exception, SystemExit) as e:
            print(f"[scanner] push failed for '{s['name']}': {e}")
    return sent


def maybe_eod_scan(now: datetime | None = None) -> bool:
    """Run the daily scan once, after market close, on trading days.

    Called from the watcher loop; cheap no-op the rest of the day. Attempted
    at most once per day whether or not it succeeds (a dead Kite session at
    15:35 shouldn't produce a retry storm).
    """
    now = now or datetime.now()
    if now.weekday() >= 5 or (now.hour, now.minute) < _CLOSE_HM:
        return False
    today = now.date().isoformat()
    if db.get_meta("signals_last_eod") == today:
        return False
    db.set_meta("signals_last_eod", today)
    try:
        res = scan(push=True)
        print(f"[signals] EOD scan: {len(res['matrix'])} symbols, "
              f"{len(res['flips'])} flip(s)"
              + (f", errors: {'; '.join(res['errors'])}" if res["errors"] else ""))
        _push_saved_scans()
    except (Exception, SystemExit) as e:
        # The day is already marked done, so this scan is not coming back on
        # its own. A print here is a message to an empty terminal — the whole
        # point of the scan is that it reaches the phone.
        print(f"[signals] EOD scan failed: {e}")
        try:
            notify.send(f"EOD signal scan failed: {e}\nRun /signals to retry.",
                        title="TickerForge", priority="high", tags="warning")
        except (Exception, SystemExit):
            pass
    return True
