"""Intraday alerts you arm from your phone; the home watcher fires them.

Supported (set with `/alert SYM ...`):
  > 410        price crosses above 410
  < 395        price crosses below 395
  vwap         price crosses the day VWAP (either way)
  pdh / pdl    price breaks the previous-day high / low
  orb          price breaks the opening-range high / low
  2%           price moves 2% from the previous close (either way)

Alerts are one-shot by default: they fire once, then disarm, so results season
in your pocket isn't a buzz storm. `state` carries the little bit of memory a
cross needs (which side we were on last).
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import db

KIND_LABEL = {
    "price_gt": "crosses above", "price_lt": "crosses below",
    "vwap_cross": "crosses VWAP", "break_pdh": "breaks PDH",
    "break_pdl": "breaks PDL", "break_orb": "breaks ORB",
    "move_pct": "moves",
}


# timeframe aliases -> Kite interval
TIMEFRAMES = {
    "1m": "minute", "min": "minute", "minute": "minute",
    "3m": "3minute", "5m": "5minute", "10m": "10minute",
    "15m": "15minute", "30m": "30minute",
    "1h": "60minute", "60m": "60minute", "hour": "60minute",
    "d": "day", "1d": "day", "day": "day", "daily": "day",
}
DEFAULT_TF = "5minute"
TF_LABEL = {v: k for k, v in
            (("1m", "minute"), ("5m", "5minute"), ("15m", "15minute"),
             ("1h", "60minute"), ("1d", "day"))}


def _strip_modifiers(tokens: list[str]) -> tuple[list[str], str, bool]:
    """Pull the timeframe + `repeat` flag out of the token list."""
    tf, repeat, rest = DEFAULT_TF, False, []
    for t in tokens:
        tl = t.lower()
        if tl in TIMEFRAMES:
            tf = TIMEFRAMES[tl]
        elif tl in ("repeat", "always", "recurring", "loop"):
            repeat = True
        else:
            rest.append(t)
    return rest, tf, repeat


def _tf_label(tf: str) -> str:
    return TF_LABEL.get(tf, tf)


def _f(tok: str) -> float | None:
    try:
        return float(str(tok).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_indicator(tokens: list[str], tf: str, repeat: bool):
    """Indicator conditions. Returns (kind, params, label) or None."""
    t = [x.lower() for x in tokens]
    joined = "".join(t)
    tl = _tf_label(tf)
    base = {"tf": tf, "repeat": repeat}

    def done(kind, extra, label):
        return kind, {**base, **extra}, f"{label} [{tl}]" + (" ·rpt" if repeat else "")

    # ---- Supertrend -------------------------------------------------
    if t[0] in ("supertrend", "st", "super"):
        want = None
        for w in t[1:]:
            if w in ("up", "buy", "green", "long"):
                want = 1
            elif w in ("down", "sell", "red", "short"):
                want = -1
        period = next((_f(x) for x in t[1:] if _f(x) and _f(x) > 3), None)
        extra = {"dir": want}
        if period:
            extra["period"] = int(period)
        lbl = ("Supertrend flip" if want is None else
               f"Supertrend → {'up' if want == 1 else 'down'}")
        return done("st_flip", extra, lbl)

    # ---- RSI --------------------------------------------------------
    if t[0] in ("rsi", "rsi14"):
        rest = t[1:]
        if not rest:
            raise ValueError("RSI needs a level, e.g. rsi > 70 | rsi < 30 | rsi 50")
        # rsi 45-55  (band entry)
        for tok in rest:
            if "-" in tok and not tok.startswith("-"):
                lo, _, hi = tok.partition("-")
                flo, fhi = _f(lo), _f(hi)
                if flo is not None and fhi is not None:
                    return done("rsi_zone", {"lo": flo, "hi": fhi},
                                f"RSI enters {flo:g}–{fhi:g}")
        op = rest[0]
        lvl = _f(rest[1]) if len(rest) > 1 else None
        if op in (">", ">=", "above", "over") and lvl is not None:
            return done("rsi_gt", {"level": lvl}, f"RSI > {lvl:g}")
        if op in ("<", "<=", "below", "under") and lvl is not None:
            return done("rsi_lt", {"level": lvl}, f"RSI < {lvl:g}")
        if op.startswith(">"):
            lvl = _f(op[1:])
            if lvl is not None:
                return done("rsi_gt", {"level": lvl}, f"RSI > {lvl:g}")
        if op.startswith("<"):
            lvl = _f(op[1:])
            if lvl is not None:
                return done("rsi_lt", {"level": lvl}, f"RSI < {lvl:g}")
        lvl = _f(op)
        if lvl is not None:
            return done("rsi_cross", {"level": lvl}, f"RSI crosses {lvl:g}")
        raise ValueError(f"couldn't read RSI condition: {' '.join(rest)}")

    # ---- EMA cross of EMA (e.g. ema20x50 / ema 20 x 50) -------------
    m = re.match(r"ema(\d+)x(?:ema)?(\d+)", joined)
    if m:
        f_, s_ = int(m.group(1)), int(m.group(2))
        return done("ema_x_ema", {"fast": f_, "slow": s_},
                    f"EMA{f_} crosses EMA{s_}")

    # ---- price vs a single EMA (ema20 / above ema50 / below ema20) ---
    m = re.search(r"ema\s*(\d+)", joined)
    if m and ("zone" not in joined and "stack" not in joined):
        period = int(m.group(1))
        want = None
        if any(w in t for w in ("above", "over", "up", "cross-above")):
            want = 1
        elif any(w in t for w in ("below", "under", "down", "cross-below")):
            want = -1
        lbl = (f"price crosses EMA{period}" if want is None else
               f"price crosses {'above' if want == 1 else 'below'} EMA{period}")
        return done("ema_cross", {"period": period, "dir": want}, lbl)

    # ---- EMA zone/stack: above both, between 20 & 50, below both ----
    if t[0] in ("zone", "stack", "emazone", "emastack") or "zone" in t:
        fast = 20
        slow = 50
        nums = [int(x) for x in re.findall(r"\d+", joined)]
        if len(nums) >= 2:
            fast, slow = nums[0], nums[1]
        return done("ema_zone", {"fast": fast, "slow": slow},
                    f"EMA{fast}/{slow} zone change")

    # ---- MACD -------------------------------------------------------
    if t[0] in ("macd",):
        want = None
        if any(w in t for w in ("up", "bull", "above")):
            want = 1
        elif any(w in t for w in ("down", "bear", "below")):
            want = -1
        lbl = ("MACD crosses signal" if want is None else
               f"MACD crosses {'above' if want == 1 else 'below'} signal")
        return done("macd_cross", {"dir": want}, lbl)

    # ---- Bollinger --------------------------------------------------
    if t[0] in ("bb", "bollinger", "band", "bands"):
        want = None
        if any(w in t for w in ("upper", "up", "high")):
            want = 1
        elif any(w in t for w in ("lower", "down", "low")):
            want = -1
        lbl = ("Bollinger band break" if want is None else
               f"breaks {'upper' if want == 1 else 'lower'} Bollinger")
        return done("bb_break", {"dir": want}, lbl)

    # ---- ADX --------------------------------------------------------
    if t[0] == "adx":
        lvl = next((_f(x) for x in t[1:] if _f(x) is not None), 25.0)
        return done("adx_gt", {"level": lvl}, f"ADX > {lvl:g}")

    # ---- anchored VWAP ---------------------------------------------
    if t[0] in ("avwap", "anchored", "avw"):
        anchor = "low"
        want = None
        for w in t[1:]:
            if w in ("high", "peak"):
                anchor = "high"
            elif w in ("low", "trough"):
                anchor = "low"
            elif w in ("session", "today", "day", "start", "lowest",
                       "major", "highest"):
                anchor = w
            elif w.isdigit():
                anchor = w
            elif w in ("above", "over", "up"):
                want = 1
            elif w in ("below", "under", "down"):
                want = -1
        lbl = ("price crosses AVWAP" if want is None else
               f"price crosses {'above' if want == 1 else 'below'} AVWAP")
        return done("avwap_cross", {"anchor": anchor, "dir": want},
                    f"{lbl} (from {anchor})")

    # ---- ATR trailing-stop exit ------------------------------------
    if t[0] in ("trail", "trailing", "exit", "stop", "chandelier", "ce"):
        side = "long"
        mult, period, entry, init = 3.0, 22, None, None
        for i, w in enumerate(t[1:]):
            if w in ("short", "sell"):
                side = "short"
            elif w in ("long", "buy"):
                side = "long"
            elif w.endswith("x") and _f(w[:-1]):
                mult = _f(w[:-1])
            elif w.startswith("entry") and "=" in w:
                entry = _f(w.split("=")[1])
            elif w.startswith("sl") and "=" in w:
                init = _f(w.split("=")[1])
            elif w.startswith("atr") and "=" in w:
                period = int(_f(w.split("=")[1]) or 22)
            elif _f(w) and entry is None:
                entry = _f(w)
        extra = {"side": side, "mult": mult, "period": period}
        if entry is not None:
            extra["entry"] = entry
        if init is not None:
            extra["init"] = init
        lbl = f"{side} trailing stop {mult:g}×ATR"
        if entry is not None:
            lbl += f" from {entry:g}"
        return done("trail_stop", extra, lbl)

    # ---- multi-timeframe confluence --------------------------------
    if t[0] in ("confluence", "conf", "mtf", "align", "aligned"):
        rest = t[1:]
        if t[0] in ("align", "aligned") or "aligned" in rest:
            return done("conf_aligned", {}, "timeframes align")
        op = rest[0] if rest else ">"
        lvl = None
        for w in rest:
            v = _f(w)
            if v is not None:
                lvl = v
        if lvl is None:
            lvl = 60.0
        if op in ("<", "<=", "below", "under") or (op.startswith("<")):
            return done("conf_lt", {"level": -abs(lvl)},
                        f"confluence ≤ -{abs(lvl):g}%")
        return done("conf_gt", {"level": abs(lvl)},
                    f"confluence ≥ {abs(lvl):g}%")

    # ---- volume spike ----------------------------------------------
    if t[0] in ("vol", "volume"):
        mult = 2.0
        for tok in t[1:]:
            v = _f(tok.replace("x", ""))
            if v:
                mult = v
        return done("vol_spike", {"mult": mult}, f"volume ≥ {mult:g}× avg")

    return None


# --------------------------------------------------------------- parse
def parse(tokens: list[str]) -> tuple[str, dict, str]:
    """Args after the symbol -> (kind, params, human_label). Raises ValueError."""
    if not tokens:
        raise ValueError("condition missing")
    tokens, tf, repeat = _strip_modifiers(tokens)
    if not tokens:
        raise ValueError("condition missing")
    ind = _parse_indicator(tokens, tf, repeat)
    if ind:
        return ind
    t0 = tokens[0].lower()
    joined = "".join(tokens).lower()

    if t0 in (">", ">=", "above", "over") and len(tokens) > 1:
        x = float(tokens[1]); return "price_gt", {"x": x}, f"> {x:g}"
    if t0 in ("<", "<=", "below", "under") and len(tokens) > 1:
        x = float(tokens[1]); return "price_lt", {"x": x}, f"< {x:g}"
    if joined.startswith(">"):
        x = float(joined[1:]); return "price_gt", {"x": x}, f"> {x:g}"
    if joined.startswith("<"):
        x = float(joined[1:]); return "price_lt", {"x": x}, f"< {x:g}"
    if t0 == "vwap":
        return "vwap_cross", {}, "VWAP cross"
    if t0 == "pdh":
        return "break_pdh", {}, "break PDH"
    if t0 == "pdl":
        return "break_pdl", {}, "break PDL"
    if t0 == "orb":
        return "break_orb", {}, "ORB break"
    if joined.endswith("%"):
        x = float(joined.rstrip("%")); return "move_pct", {"pct": abs(x)}, f"±{x:g}%"
    # bare number -> treat as ">= that price" is ambiguous; ask instead
    raise ValueError(f"unrecognized condition: {' '.join(tokens)}")


# --------------------------------------------------------------- store
def add(symbol: str, token: int | None, kind: str, params: dict) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (symbol, token, kind, params, state, active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (symbol.upper(), token, kind, json.dumps(params), "{}"))
        return cur.lastrowid


def set_level(alert_id: int, price: float) -> dict | None:
    """Move a price alert to a new level. Returns the updated row, or None.

    Only price-threshold alerts have a level to move; an RSI or Supertrend
    alert has no single price, so those are refused rather than silently
    ignored. Dragging a line on the chart is the one caller.

    Two things happen beyond the number:

      * `state` is cleared. Alerts track whether price was already above or
        below the level so a cross fires once rather than on every tick. Move
        the level without clearing that and the alert compares against where
        price stood relative to the OLD level — it can fire immediately, or
        stay silent through a genuine cross.
      * The alert is re-armed if it had already fired. Dragging a triggered
        line to a new price obviously means you want it live again.

    The id survives, so the alert keeps its place in `/alerts` and anything
    holding a reference stays valid.
    """
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?",
                           (alert_id,)).fetchone()
        if not row:
            return None
        if row["kind"] not in ("price_gt", "price_lt"):
            raise ValueError(
                f"alert #{alert_id} is {row['kind']} — it has no single price "
                f"level to move. Remove it and arm a new one.")

        params = json.loads(row["params"] or "{}")
        params["x"] = float(price)
        # The direction has to follow the level. Dragging a "> 410" line down
        # below spot turns it into a break the other way; keeping it as a
        # greater-than would make it fire instantly.
        params.pop("label", None)

        conn.execute(
            "UPDATE alerts SET params=?, state='{}', active=1, "
            "triggered_at=NULL, reason=NULL WHERE id=?",
            (json.dumps(params), alert_id))
        out = conn.execute("SELECT * FROM alerts WHERE id=?",
                           (alert_id,)).fetchone()
    return dict(out)


def retarget(alert_id: int, price: float, last_price: float | None = None) -> dict | None:
    """Move a price alert, flipping its direction to match where price now is.

    A line dragged from above spot to below spot should become a break DOWN,
    not a greater-than that fires the instant it's set. With no `last_price`
    the direction is left alone — the caller knows the market, this function
    doesn't.
    """
    with db.connect() as conn:
        row = conn.execute("SELECT kind FROM alerts WHERE id=?",
                           (alert_id,)).fetchone()
    if not row:
        return None
    if last_price is not None and row["kind"] in ("price_gt", "price_lt"):
        want = "price_gt" if price > last_price else "price_lt"
        if want != row["kind"]:
            with db.connect() as conn:
                conn.execute("UPDATE alerts SET kind=? WHERE id=?",
                             (want, alert_id))
    return set_level(alert_id, price)


def active(symbol: str | None = None) -> list[dict]:
    q = "SELECT * FROM alerts WHERE active=1"
    args: list = []
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper())
    q += " ORDER BY symbol, id"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def all_rows(symbol: str | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM alerts"
    args: list = []
    if symbol:
        q += " WHERE symbol=?"; args.append(symbol.upper())
    q += " ORDER BY active DESC, id DESC LIMIT ?"; args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def remove(target: str) -> int:
    """Remove by id, by symbol (all its alerts), or 'all'."""
    with db.connect() as conn:
        if target.lower() == "all":
            cur = conn.execute("DELETE FROM alerts")
        elif target.isdigit():
            cur = conn.execute("DELETE FROM alerts WHERE id=?", (int(target),))
        else:
            cur = conn.execute("DELETE FROM alerts WHERE symbol=?",
                               (target.upper(),))
        return cur.rowcount


def _set_state(conn, alert_id: int, state: dict) -> None:
    conn.execute("UPDATE alerts SET state=? WHERE id=?",
                 (json.dumps(state), alert_id))


def rearm(alert_id: int, state: dict, cooldown_s: int = 0) -> None:
    """Keep a `repeat` alert armed after it fires, with a short cooldown so a
    single event can't machine-gun your phone."""
    st = dict(state or {})
    if cooldown_s:
        st["cool_until"] = datetime.now().timestamp() + cooldown_s
    with db.connect() as conn:
        conn.execute(
            "UPDATE alerts SET state=?, triggered_at=? WHERE id=?",
            (json.dumps(st), datetime.now().isoformat(timespec="seconds"),
             alert_id))


def cooling(row: dict) -> bool:
    """Is this alert inside its post-fire cooldown?"""
    try:
        st = json.loads(row["state"] or "{}")
    except (TypeError, ValueError):
        return False
    until = st.get("cool_until")
    return bool(until and datetime.now().timestamp() < until)


def disarm(alert_id: int, reason: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE alerts SET active=0, triggered_at=?, reason=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), reason, alert_id))


def label(row: dict) -> str:
    """Human one-liner for an alert row, e.g. 'BEL > 410'."""
    params = json.loads(row["params"] or "{}")
    if params.get("label"):                    # indicator alerts store theirs
        return f"{row['symbol']} {params['label']}"
    k = row["kind"]
    if k == "price_gt":  return f"{row['symbol']} > {params['x']:g}"
    if k == "price_lt":  return f"{row['symbol']} < {params['x']:g}"
    if k == "move_pct":  return f"{row['symbol']} ±{params['pct']:g}%"
    tail = {"vwap_cross": "VWAP", "break_pdh": "PDH", "break_pdl": "PDL",
            "break_orb": "ORB"}.get(k, k)
    return f"{row['symbol']} {tail}"


# --------------------------------------------------------------- evaluate
INDICATOR_KINDS = {
    "st_flip", "rsi_gt", "rsi_lt", "rsi_cross", "rsi_zone", "ema_cross",
    "ema_x_ema", "ema_zone", "macd_cross", "bb_break", "adx_gt", "vol_spike",
    "avwap_cross", "trail_stop",
}


def _zone_of(close, fast_v, slow_v, fast, slow) -> str | None:
    """Where price sits relative to two EMAs."""
    if close is None or fast_v is None or slow_v is None:
        return None
    hi, lo = max(fast_v, slow_v), min(fast_v, slow_v)
    if close > hi:
        return f"above EMA{fast}/{slow}"
    if close < lo:
        return f"below EMA{fast}/{slow}"
    return f"between EMA{fast} and EMA{slow}"


def _evaluate_indicator(row, lv, k, params, state):
    """Indicator alerts. Needs lv['ind'] from indicators.compute()."""
    from . import indicators as I

    ind = lv.get("ind")
    if not ind or not ind.get("closes"):
        return False, "", None
    sym = row["symbol"]
    closes = ind["closes"]
    tf = params.get("tf", DEFAULT_TF)
    tag = f"[{_tf_label(tf)}]"

    if k == "st_flip":
        st = ind.get("supertrend") or []
        if len(st) < 2:
            return False, "", None
        cur, prev = st[-1].get("dir"), st[-2].get("dir")
        if cur is None or prev is None or cur == prev:
            return False, "", None
        want = params.get("dir")
        if want is not None and cur != want:
            return False, "", None
        word = "UP" if cur == 1 else "DOWN"
        val = st[-1].get("value")
        return True, (f"{sym} Supertrend flipped {word} {tag} — "
                      f"price {closes[-1]:g}"
                      + (f", ST {val:,.1f}" if val else "")), None

    if k in ("rsi_gt", "rsi_lt", "rsi_cross"):
        r = ind.get("rsi") or []
        lvl = params.get("level", 70 if k == "rsi_gt" else 30)
        c = I.crossed(r, lvl)
        want = {"rsi_gt": 1, "rsi_lt": -1}.get(k)
        if c == 0 or (want is not None and c != want):
            return False, "", None
        arrow = "above" if c == 1 else "below"
        return True, (f"{sym} RSI crossed {arrow} {lvl:g} {tag} — "
                      f"now {r[-1]:.1f}, price {closes[-1]:g}"), None

    if k == "rsi_zone":
        r = ind.get("rsi") or []
        if len(r) < 2 or r[-1] is None or r[-2] is None:
            return False, "", None
        lo, hi = params.get("lo", 45), params.get("hi", 55)
        inside_now = lo <= r[-1] <= hi
        inside_prev = lo <= r[-2] <= hi
        if inside_now and not inside_prev:
            return True, (f"{sym} RSI entered {lo:g}–{hi:g} {tag} — "
                          f"now {r[-1]:.1f}"), None
        return False, "", None

    if k == "ema_cross":
        period = int(params.get("period", 20))
        series = (ind.get("ema") or {}).get(period)
        if not series:
            return False, "", None
        c = I.crossed(closes, series)
        want = params.get("dir")
        if c == 0 or (want is not None and c != want):
            return False, "", None
        arrow = "above" if c == 1 else "below"
        return True, (f"{sym} price crossed {arrow} EMA{period} {tag} — "
                      f"{closes[-1]:g} vs {series[-1]:,.1f}"), None

    if k == "ema_x_ema":
        fast, slow = int(params.get("fast", 20)), int(params.get("slow", 50))
        emas = ind.get("ema") or {}
        fs, ss = emas.get(fast), emas.get(slow)
        if not fs or not ss:
            return False, "", None
        c = I.crossed(fs, ss)
        if c == 0:
            return False, "", None
        word = "golden cross" if c == 1 else "death cross"
        return True, (f"{sym} EMA{fast} crossed "
                      f"{'above' if c == 1 else 'below'} EMA{slow} {tag} "
                      f"({word}) — price {closes[-1]:g}"), None

    if k == "ema_zone":
        fast, slow = int(params.get("fast", 20)), int(params.get("slow", 50))
        emas = ind.get("ema") or {}
        fs, ss = emas.get(fast), emas.get(slow)
        if not fs or not ss:
            return False, "", None
        zone = _zone_of(closes[-1], fs[-1], ss[-1], fast, slow)
        if zone is None:
            return False, "", None
        prev = state.get("zone")
        if prev is None:
            return False, "", {"zone": zone}          # seed, don't fire
        if zone != prev:
            return True, (f"{sym} moved {prev} → {zone} {tag} — "
                          f"price {closes[-1]:g}"), {"zone": zone}
        return False, "", None

    if k == "macd_cross":
        m = ind.get("macd") or {}
        line, sig = m.get("line"), m.get("signal")
        if not line or not sig:
            return False, "", None
        c = I.crossed(line, sig)
        want = params.get("dir")
        if c == 0 or (want is not None and c != want):
            return False, "", None
        return True, (f"{sym} MACD crossed {'above' if c == 1 else 'below'} "
                      f"signal {tag} — price {closes[-1]:g}"), None

    if k == "bb_break":
        bb = ind.get("bb") or {}
        up, lo = bb.get("upper"), bb.get("lower")
        want = params.get("dir")
        if up and want in (None, 1) and I.crossed(closes, up) == 1:
            return True, (f"{sym} broke ABOVE upper Bollinger {tag} — "
                          f"{closes[-1]:g} > {up[-1]:,.1f}"), None
        if lo and want in (None, -1) and I.crossed(closes, lo) == -1:
            return True, (f"{sym} broke BELOW lower Bollinger {tag} — "
                          f"{closes[-1]:g} < {lo[-1]:,.1f}"), None
        return False, "", None

    if k == "adx_gt":
        a = ind.get("adx") or []
        lvl = params.get("level", 25)
        if I.crossed(a, lvl) == 1:
            return True, (f"{sym} ADX crossed above {lvl:g} {tag} — "
                          f"now {a[-1]:.0f} (trend strengthening)"), None
        return False, "", None

    if k == "vol_spike":
        vols = ind.get("volumes") or []
        mult = params.get("mult", 2.0)
        if len(vols) < 21 or not vols[-1]:
            return False, "", None
        avg = sum(vols[-21:-1]) / 20
        if avg > 0 and vols[-1] >= mult * avg:
            return True, (f"{sym} volume spike {tag} — "
                          f"{vols[-1]/avg:.1f}× the 20-bar average"), None
        return False, "", None

    if k == "avwap_cross":
        cands = ind.get("candles")
        if not cands:
            return False, "", None
        # The anchor must be PINNED when the alert is armed. If we re-resolved
        # "most recent swing low" every cycle the line would jump each time a
        # new swing formed — and a jumping line isn't anchored, it just
        # manufactures fake crossings.
        anchor_ts = state.get("anchor_ts")
        idx = None
        if anchor_ts is not None:
            for i, c in enumerate(cands):
                if c.get("time") == anchor_ts:
                    idx = i
                    break
            if idx is None:                     # anchor scrolled out of range
                idx = 0
        else:
            idx = I.anchor_index(cands, params.get("anchor", "low"))
            if idx is None:
                return False, "", None
            ts = cands[idx].get("time")
            return False, "", {"anchor_ts": ts}   # pin it, don't fire yet
        av = I.anchored_vwap(cands, idx)
        c = I.crossed(closes, av)
        want = params.get("dir")
        if c == 0 or (want is not None and c != want):
            return False, "", None
        arrow = "above" if c == 1 else "below"
        return True, (f"{sym} crossed {arrow} anchored VWAP {tag} — "
                      f"{closes[-1]:g} vs {av[-1]:,.1f}"), None

    if k == "trail_stop":
        cands = ind.get("candles")
        if not cands:
            return False, "", None
        side = params.get("side", "long")
        mult = params.get("mult", 3.0)
        period = int(params.get("period", 22))
        a = I.atr(cands, period)
        if not a or a[-1] is None:
            return False, "", None
        window = cands[-period:] if len(cands) >= period else cands
        if side == "long":
            raw = max(c["high"] for c in window) - mult * a[-1]
        else:
            raw = min(c["low"] for c in window) + mult * a[-1]
        prev = state.get("stop")
        if params.get("init") is not None and prev is None:
            prev = params["init"]
        if prev is None:
            stop = raw
        elif side == "long":
            stop = max(prev, raw)                 # ratchet in your favour only
        else:
            stop = min(prev, raw)
        price = closes[-1]
        entry = params.get("entry")
        # risk is measured from the ORIGINAL stop, not the trailed one
        risk_stop = params.get("init") or state.get("init_stop") or stop
        rr = I.r_multiple(entry, risk_stop, price, side) \
            if entry is not None else None
        hit = (price < stop) if side == "long" else (price > stop)
        if hit:
            msg = (f"{sym} EXIT — {side} trailing stop hit {tag}: "
                   f"{price:g} vs stop {stop:,.1f}")
            if rr is not None:
                msg += f"  ({rr:+.2f}R)"
            return True, msg, None
        new_state = {"stop": round(stop, 2),
                     "init_stop": state.get("init_stop") or round(stop, 2)}
        return False, "", new_state           # persist the ratchet

    return False, "", None


CONFLUENCE_KINDS = {"conf_gt", "conf_lt", "conf_aligned"}


def _evaluate_confluence(row, lv, k, params, state):
    """Confluence alerts. Needs lv['conf'] from confluence.build()."""
    conf = lv.get("conf")
    if not conf or conf.get("score_pct") is None:
        return False, "", None
    sym = row["symbol"]
    pct = conf["score_pct"]
    detail = f"{conf['verdict']}, {conf.get('n_tf', 0)} timeframes"

    if k == "conf_aligned":
        was = state.get("aligned")
        now = bool(conf.get("aligned"))
        if was is None:
            return False, "", {"aligned": now}
        if now and not was:
            return True, (f"{sym} timeframes now ALIGNED "
                          f"({pct:+.0f}%, {conf['verdict']})"), {"aligned": now}
        if now != was:
            return False, "", {"aligned": now}
        return False, "", None

    level = params.get("level", 60)
    if k == "conf_gt":
        was = state.get("over")
        now = pct >= level
        if was is None:
            return False, "", {"over": now}
        if now and not was:
            return True, (f"{sym} confluence crossed {level:g}% "
                          f"— now {pct:+.0f}% ({detail})"), {"over": now}
        if now != was:
            return False, "", {"over": now}
        return False, "", None

    if k == "conf_lt":
        was = state.get("under")
        now = pct <= level
        if was is None:
            return False, "", {"under": now}
        if now and not was:
            return True, (f"{sym} confluence fell below {level:g}% "
                          f"— now {pct:+.0f}% ({detail})"), {"under": now}
        if now != was:
            return False, "", {"under": now}
        return False, "", None

    return False, "", None
def evaluate(row: dict, lv: dict) -> tuple[bool, str, dict | None]:
    """Does this alert fire against snapshot `lv`?

    Returns (fired, reason, new_state|None). new_state is set for cross alerts
    that need their side persisted even when they don't fire yet.
    """
    params = json.loads(row["params"] or "{}")
    state = json.loads(row["state"] or "{}")
    k = row["kind"]
    if k in CONFLUENCE_KINDS:
        return _evaluate_confluence(row, lv, k, params, state)
    if k in INDICATOR_KINDS:
        return _evaluate_indicator(row, lv, k, params, state)
    ltp = lv.get("ltp")
    if ltp is None:
        return False, "", None

    if k == "price_gt":
        if ltp >= params["x"]:
            return True, f"{row['symbol']} {ltp:g} ≥ {params['x']:g}", None
        return False, "", None
    if k == "price_lt":
        if ltp <= params["x"]:
            return True, f"{row['symbol']} {ltp:g} ≤ {params['x']:g}", None
        return False, "", None
    if k == "move_pct":
        cp = lv.get("chg_pct")
        if cp is not None and abs(cp) >= params["pct"]:
            return True, f"{row['symbol']} {cp:+.2f}% from prev close", None
        return False, "", None
    if k == "break_pdh":
        pdh = lv.get("pdh")
        if pdh and ltp > pdh:
            return True, f"{row['symbol']} broke PDH {pdh:g} → {ltp:g}", None
        return False, "", None
    if k == "break_pdl":
        pdl = lv.get("pdl")
        if pdl and ltp < pdl:
            return True, f"{row['symbol']} broke PDL {pdl:g} → {ltp:g}", None
        return False, "", None
    if k == "break_orb":
        orb = lv.get("orb") or {}
        hi, lo = orb.get("high"), orb.get("low")
        if hi and ltp > hi:
            return True, f"{row['symbol']} broke ORB high {hi:g} → {ltp:g}", None
        if lo and ltp < lo:
            return True, f"{row['symbol']} broke ORB low {lo:g} → {ltp:g}", None
        return False, "", None
    if k == "vwap_cross":
        v = lv.get("vwap")
        if not v:
            return False, "", None
        side = "above" if ltp >= v else "below"
        prev = state.get("side")
        if prev is None:
            return False, "", {"side": side}          # seed, don't fire
        if side != prev:
            return (True, f"{row['symbol']} crossed {side} VWAP {v:g} "
                    f"(now {ltp:g})", {"side": side})
        return False, "", None
    return False, "", None


def apply_state(alert_id: int, new_state: dict) -> None:
    with db.connect() as conn:
        _set_state(conn, alert_id, new_state)
