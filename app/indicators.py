"""Technical indicators — pure maths on candle lists, no network, no Kite.

Everything here takes plain lists (closes, or candle dicts with
open/high/low/close/volume) and returns a *series* the same length as the
input, with None where the indicator hasn't warmed up yet. Keeping the series
(not just the last value) is what lets the alert engine detect a *cross*:
compare the last two points.

Wilder's smoothing is used for RSI/ATR/ADX (the standard on Indian charting
platforms), so numbers line up with what you see on Kite/TradingView.
"""
from __future__ import annotations


# ------------------------------------------------------------------ helpers
def _closes(candles) -> list[float]:
    if not candles:
        return []
    if isinstance(candles[0], dict):
        return [c["close"] for c in candles]
    return list(candles)


def last(series, n: int = 1):
    """Last non-None value (n=1), or the n-th from the end."""
    if not series or len(series) < n:
        return None
    return series[-n]


def crossed(series, level_series_or_value) -> int:
    """+1 if `series` crossed ABOVE, -1 if crossed BELOW, 0 otherwise.

    Compares the last two points, so it fires exactly once at the crossing.
    `level_series_or_value` may be a constant or a parallel series.
    """
    if not series or len(series) < 2:
        return 0
    cur, prev = series[-1], series[-2]
    if cur is None or prev is None:
        return 0
    if isinstance(level_series_or_value, (list, tuple)):
        lv = level_series_or_value
        if len(lv) < 2 or lv[-1] is None or lv[-2] is None:
            return 0
        cur_l, prev_l = lv[-1], lv[-2]
    else:
        cur_l = prev_l = level_series_or_value
    if prev <= prev_l and cur > cur_l:
        return 1
    if prev >= prev_l and cur < cur_l:
        return -1
    return 0


# ------------------------------------------------------------------ averages
def sma(values, period: int) -> list:
    vals = _closes(values)
    out: list = [None] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    run = sum(vals[:period])
    out[period - 1] = run / period
    for i in range(period, len(vals)):
        run += vals[i] - vals[i - period]
        out[i] = run / period
    return out


def ema(values, period: int) -> list:
    """Standard EMA seeded with an SMA of the first `period` values."""
    vals = _closes(values)
    out: list = [None] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(vals[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: list, period: int) -> list:
    """Wilder's smoothing (used by RSI/ATR/ADX)."""
    out: list = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# ------------------------------------------------------------------ momentum
def rsi(values, period: int = 14) -> list:
    """Wilder RSI. Returns a series aligned to the input."""
    vals = _closes(values)
    n = len(vals)
    out: list = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = _wilder(gains, period)
    al = _wilder(losses, period)
    for i in range(len(ag)):
        g, l = ag[i], al[i]
        if g is None or l is None:
            continue
        if l == 0:
            out[i + 1] = 100.0
        else:
            rs = g / l
            out[i + 1] = 100 - (100 / (1 + rs))
    return out


def macd(values, fast: int = 12, slow: int = 26, signal: int = 9):
    """-> (macd_line, signal_line, histogram) series."""
    vals = _closes(values)
    ef, es = ema(vals, fast), ema(vals, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig_tail = ema(valid, signal)
    sig: list = [None] * len(line)
    off = len(line) - len(sig_tail)
    for i, v in enumerate(sig_tail):
        sig[off + i] = v
    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, sig)]
    return line, sig, hist


# ------------------------------------------------------------------ volatility
def true_range(candles) -> list:
    out: list = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c["high"] - c["low"])
            continue
        pc = candles[i - 1]["close"]
        out.append(max(c["high"] - c["low"], abs(c["high"] - pc),
                       abs(c["low"] - pc)))
    return out


def atr(candles, period: int = 14) -> list:
    if not candles:
        return []
    return _wilder(true_range(candles), period)


def bollinger(values, period: int = 20, mult: float = 2.0):
    """-> (upper, mid, lower) series."""
    vals = _closes(values)
    mid = sma(vals, period)
    upper: list = [None] * len(vals)
    lower: list = [None] * len(vals)
    for i in range(len(vals)):
        if mid[i] is None:
            continue
        window = vals[i - period + 1:i + 1]
        m = mid[i]
        var = sum((v - m) ** 2 for v in window) / period
        sd = var ** 0.5
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def supertrend(candles, period: int = 10, mult: float = 3.0):
    """Classic Supertrend.

    Returns a list of {"value": float|None, "dir": +1|-1|None} where dir is
    +1 for uptrend (price above the line, line sits below price) and -1 for
    downtrend. A change in `dir` between the last two bars is a flip.
    """
    n = len(candles)
    out = [{"value": None, "dir": None} for _ in range(n)]
    if n < period + 1:
        return out
    a = atr(candles, period)
    f_up: list = [None] * n
    f_lo: list = [None] * n
    st: list = [None] * n
    direction: list = [None] * n

    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        b_up = hl2 + mult * a[i]
        b_lo = hl2 - mult * a[i]
        if f_up[i - 1] is None:
            f_up[i], f_lo[i] = b_up, b_lo
            direction[i] = 1 if candles[i]["close"] >= b_lo else -1
            st[i] = b_lo if direction[i] == 1 else b_up
            continue
        pc = candles[i - 1]["close"]
        f_up[i] = b_up if (b_up < f_up[i - 1] or pc > f_up[i - 1]) else f_up[i - 1]
        f_lo[i] = b_lo if (b_lo > f_lo[i - 1] or pc < f_lo[i - 1]) else f_lo[i - 1]
        close = candles[i]["close"]
        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if close < f_lo[i] else 1
        else:
            direction[i] = 1 if close > f_up[i] else -1
        st[i] = f_lo[i] if direction[i] == 1 else f_up[i]

    for i in range(n):
        out[i] = {"value": st[i], "dir": direction[i]}
    return out


def adx(candles, period: int = 14) -> list:
    """Average Directional Index — trend strength (not direction)."""
    n = len(candles)
    if n < period * 2:
        return [None] * n
    plus_dm, minus_dm = [], []
    for i in range(1, n):
        up = candles[i]["high"] - candles[i - 1]["high"]
        dn = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr = true_range(candles)[1:]
    s_tr = _wilder(tr, period)
    s_p = _wilder(plus_dm, period)
    s_m = _wilder(minus_dm, period)
    dx: list = []
    for i in range(len(s_tr)):
        if not s_tr[i] or s_p[i] is None or s_m[i] is None or s_tr[i] == 0:
            dx.append(None)
            continue
        pdi = 100 * s_p[i] / s_tr[i]
        mdi = 100 * s_m[i] / s_tr[i]
        denom = pdi + mdi
        dx.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)
    clean = [v for v in dx if v is not None]
    smoothed = _wilder(clean, period)
    out: list = [None] * n
    off = n - len(smoothed)
    for i, v in enumerate(smoothed):
        out[off + i] = v
    return out


# ------------------------------------------------------------------ anchored VWAP
def anchored_vwap(candles, anchor: int = 0) -> list:
    """Volume-weighted average price cumulated from bar `anchor` onward.

    Unlike session VWAP this can be pinned to a meaningful event — a swing low,
    a breakout bar, an earnings gap — which is what makes it a real
    support/resistance line rather than a daily average.
    """
    n = len(candles)
    out: list = [None] * n
    cum_pv = cum_v = 0.0
    for i in range(max(anchor, 0), n):
        c = candles[i]
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume") or 0
        cum_pv += tp * v
        cum_v += v
        out[i] = (cum_pv / cum_v) if cum_v else None
    return out


def swing_points(candles, left: int = 2, right: int = 2):
    """Indices of confirmed pivot highs/lows -> (highs, lows).

    A pivot needs `right` bars after it to confirm, so the most recent bars
    are deliberately excluded — an unconfirmed pivot isn't a pivot.
    """
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if all(h >= candles[j]["high"] for j in range(i - left, i)) and \
           all(h > candles[j]["high"] for j in range(i + 1, i + right + 1)):
            highs.append(i)
        if all(l <= candles[j]["low"] for j in range(i - left, i)) and \
           all(l < candles[j]["low"] for j in range(i + 1, i + right + 1)):
            lows.append(i)
    return highs, lows


def anchor_index(candles, spec: str = "low", left: int = 2, right: int = 2):
    """Resolve an anchor spec to a bar index.

    'low'/'high'  most recent confirmed swing low / high
    'session'     first bar of the last calendar day present
    'start'       first bar
    '<N>'         N bars back from the end
    """
    n = len(candles)
    if not n:
        return None
    spec = str(spec).lower().strip()
    if spec.isdigit():
        return max(0, n - int(spec))
    if spec in ("start", "first"):
        return 0
    if spec in ("session", "today", "day"):
        stamps = [c.get("time") for c in candles]
        if stamps and stamps[-1] is not None:
            try:
                day = int(stamps[-1]) // 86400
                for i in range(n - 1, -1, -1):
                    if int(stamps[i]) // 86400 != day:
                        return i + 1
                return 0
            except (TypeError, ValueError):
                return 0
        return 0
    if spec in ("lowest", "major", "majorlow"):
        return min(range(n), key=lambda i: candles[i]["low"])
    if spec in ("highest", "majorhigh"):
        return max(range(n), key=lambda i: candles[i]["high"])
    highs, lows = swing_points(candles, left, right)
    if spec in ("high", "swinghigh", "peak"):
        return highs[-1] if highs else None
    return lows[-1] if lows else None


# ------------------------------------------------------------------ exits
def chandelier(candles, period: int = 22, mult: float = 3.0):
    """Chandelier exit: stop hung off the highest high (long) / lowest low
    (short) by a multiple of ATR. -> (long_stop, short_stop) series."""
    n = len(candles)
    a = atr(candles, period)
    long_stop: list = [None] * n
    short_stop: list = [None] * n
    for i in range(n):
        if a[i] is None or i + 1 < period:
            continue
        window = candles[i - period + 1:i + 1]
        hh = max(c["high"] for c in window)
        ll = min(c["low"] for c in window)
        long_stop[i] = hh - mult * a[i]
        short_stop[i] = ll + mult * a[i]
    return long_stop, short_stop


def trailing_stop(candles, side: str = "long", period: int = 22,
                  mult: float = 3.0, entry_idx: int = 0,
                  initial: float | None = None) -> dict:
    """Ratcheting ATR trailing stop for a live position.

    For an open trade the stop hangs off the best price reached *since entry*
    (not a rolling lookback) — otherwise, entering just after a downswing puts
    the stop above your entry and it reads as instantly hit. The stop only ever
    moves in your favour.
    """
    n = len(candles)
    a = atr(candles, period)
    out: list = [None] * n
    cur = initial
    hit_idx = None
    best = None
    for i in range(max(entry_idx, 0), n):
        c = candles[i]
        best = (c["high"] if best is None else max(best, c["high"])) \
            if side == "long" else \
            (c["low"] if best is None else min(best, c["low"]))
        if a[i] is None:
            out[i] = cur
            continue
        raw = (best - mult * a[i]) if side == "long" else (best + mult * a[i])
        if cur is None:
            cur = raw
        elif side == "long":
            cur = max(cur, raw)          # ratchet up only
        else:
            cur = min(cur, raw)          # ratchet down only
        out[i] = cur
        if hit_idx is None:
            close = c["close"]
            if (side == "long" and close < cur) or \
               (side != "long" and close > cur):
                hit_idx = i
    return {"series": out, "stop": cur, "hit": hit_idx is not None,
            "hit_idx": hit_idx, "side": side,
            "best": best, "atr": last(a)}


def r_multiple(entry: float, stop: float, price: float,
               side: str = "long") -> float | None:
    """How many R the trade is up/down. Risk (entry->stop) = 1R."""
    if entry is None or stop is None or price is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    move = (price - entry) if side == "long" else (entry - price)
    return round(move / risk, 2)


# ------------------------------------------------------------------ bundle
def compute(candles, cfg: dict | None = None) -> dict:
    """Compute the whole indicator set once per symbol per cycle.

    Returns series (not just last values) so the alert engine can detect
    crosses, plus a small `now` summary for display.
    """
    cfg = cfg or {}
    closes = _closes(candles)
    if not closes:
        return {}
    ema_periods = cfg.get("ema_periods", (9, 20, 50, 200))
    vols = [c.get("volume") or 0 for c in candles] if isinstance(candles[0], dict) else []
    out: dict = {
        "candles": candles,
        "closes": closes,
        "volumes": vols,
        "rsi": rsi(closes, cfg.get("rsi_period", 14)),
        "supertrend": supertrend(candles, cfg.get("st_period", 10),
                                 cfg.get("st_mult", 3.0)),
        "ema": {p: ema(closes, p) for p in ema_periods},
        "atr": atr(candles, 14),
    }
    line, sig, hist = macd(closes)
    out["macd"] = {"line": line, "signal": sig, "hist": hist}
    up, mid, lo = bollinger(closes, cfg.get("bb_period", 20),
                            cfg.get("bb_mult", 2.0))
    out["bb"] = {"upper": up, "mid": mid, "lower": lo}
    out["adx"] = adx(candles, 14)

    # anchored VWAP, auto-pinned to the most recent confirmed swing low
    if isinstance(candles[0], dict):
        anch = anchor_index(candles, cfg.get("avwap_anchor", "low"))
        if anch is not None:
            out["avwap"] = anchored_vwap(candles, anch)
            out["avwap_anchor"] = anch
        ls, ss = chandelier(candles, cfg.get("ce_period", 22),
                            cfg.get("ce_mult", 3.0))
        out["chandelier"] = {"long": ls, "short": ss}

    st_now = out["supertrend"][-1] if out["supertrend"] else {}
    out["now"] = {
        "close": closes[-1],
        "rsi": last(out["rsi"]),
        "st_value": st_now.get("value"), "st_dir": st_now.get("dir"),
        "ema": {p: last(s) for p, s in out["ema"].items()},
        "macd": last(line), "macd_signal": last(sig), "macd_hist": last(hist),
        "adx": last(out["adx"]),
        "bb_upper": last(up), "bb_lower": last(lo),
        "avwap": last(out.get("avwap") or []),
        "ce_long": last((out.get("chandelier") or {}).get("long") or []),
        "ce_short": last((out.get("chandelier") or {}).get("short") or []),
    }
    return out


def summary_line(ind: dict) -> str:
    """One compact line describing where the indicators stand."""
    if not ind or not ind.get("now"):
        return ""
    n = ind["now"]
    bits = []
    if n.get("rsi") is not None:
        r = n["rsi"]
        zone = "OB" if r >= 70 else "OS" if r <= 30 else ""
        bits.append(f"RSI {r:.0f}{(' ' + zone) if zone else ''}")
    if n.get("st_dir") is not None:
        bits.append(f"ST {'up' if n['st_dir'] == 1 else 'down'}")
    e = n.get("ema") or {}
    c = n.get("close")
    if c and e.get(20) and e.get(50):
        if c > e[20] > e[50]:
            bits.append("above 20>50")
        elif c < e[20] < e[50]:
            bits.append("below 20<50")
        elif e.get(50) and c > e[50]:
            bits.append("mid (20–50)")
    if n.get("adx") is not None:
        bits.append(f"ADX {n['adx']:.0f}")
    return " · ".join(bits)
