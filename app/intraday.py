"""Intraday map for research-from-your-phone.

Two kinds of number:
  * static for the day — PDH/PDL/PDC, CPR (TC/P/BC), classic pivots R1-3/S1-3,
    computed once from the previous trading day's daily candle.
  * live — LTP, VWAP (Kite's day average_price), day O/H/L, %change, volume,
    from one quote() snapshot. ORB is the first N-minute range of the session.

The pure maths (pivots, position) take plain numbers so they're testable without
a Kite session; only the fetchers need the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
ORB_MINUTES = 15


# --------------------------------------------------------------- pure maths
def pivots(pdh: float, pdl: float, pdc: float) -> dict:
    """Central Pivot Range + classic floor pivots from prev-day H/L/C."""
    p = (pdh + pdl + pdc) / 3
    bc = (pdh + pdl) / 2
    tc = 2 * p - bc
    tc, bc = max(tc, bc), min(tc, bc)           # TC is the upper edge
    rng = pdh - pdl
    return {
        "P": p, "TC": tc, "BC": bc,
        "R1": 2 * p - pdl, "S1": 2 * p - pdh,
        "R2": p + rng,     "S2": p - rng,
        "R3": pdh + 2 * (p - pdl), "S3": pdl - 2 * (pdh - p),
    }


def position(ltp: float, lv: dict) -> list[str]:
    """Short human read of where price sits vs the key levels."""
    out = []
    v = lv.get("vwap")
    if v:
        out.append("above VWAP" if ltp >= v else "below VWAP")
    piv = lv.get("pivots") or {}
    if piv.get("TC") and piv.get("BC"):
        if ltp > piv["TC"]:
            out.append("above CPR")
        elif ltp < piv["BC"]:
            out.append("below CPR")
        else:
            out.append("inside CPR")
    if lv.get("pdh") and ltp > lv["pdh"]:
        out.append("above PDH")
    if lv.get("pdl") and ltp < lv["pdl"]:
        out.append("below PDL")
    orb = lv.get("orb")
    if orb and orb.get("high"):
        if ltp > orb["high"]:
            out.append("above ORB")
        elif ltp < orb["low"]:
            out.append("below ORB")
    return out


def gap_pct(day_open: float | None, pdc: float | None) -> float | None:
    if not day_open or not pdc:
        return None
    return round((day_open - pdc) / pdc * 100, 2)


# --------------------------------------------------------------- fetchers
def _today_start() -> datetime:
    n = datetime.now()
    return n.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                     second=0, microsecond=0)


def prev_day_hlc(kite, token: int) -> tuple[float, float, float] | None:
    """Previous trading day's (high, low, close) from daily candles."""
    end = datetime.now()
    start = end - timedelta(days=10)
    try:
        rows = kite.historical_data(token, start, end, "day")
    except Exception:
        return None
    today = datetime.now().date()
    prev = [r for r in rows if r["date"].date() < today]
    if not prev:
        return None
    r = prev[-1]
    return float(r["high"]), float(r["low"]), float(r["close"])


def opening_range(kite, token: int, minutes: int = ORB_MINUTES
                  ) -> dict | None:
    """High/low of the first `minutes` of today's session."""
    start = _today_start()
    cutoff = start + timedelta(minutes=minutes)
    if datetime.now() < cutoff:
        return None                     # range not complete yet
    try:
        rows = kite.historical_data(token, start, cutoff, "minute")
    except Exception:
        return None
    rows = [r for r in rows if r["date"].date() == start.date()]
    if not rows:
        return None
    return {"high": max(r["high"] for r in rows),
            "low": min(r["low"] for r in rows),
            "minutes": minutes}


def quote_snapshot(kite, symbol: str, exchange: str = "NSE") -> dict | None:
    """One live quote -> normalized dict, or None if unavailable."""
    key = f"{exchange}:{symbol.upper()}"
    try:
        data = kite.quote([key])
    except Exception:
        return None
    q = data.get(key)
    return normalize_quote(q) if q else None


def normalize_quote(q: dict) -> dict:
    """Kite quote record -> the snapshot fields we use."""
    ohlc = q.get("ohlc") or {}
    ltp = q.get("last_price")
    pdc = ohlc.get("close")
    chg = q.get("net_change")
    chg_pct = round(chg / pdc * 100, 2) if (chg is not None and pdc) else None
    return {
        "ltp": ltp, "vwap": q.get("average_price"),
        "open": ohlc.get("open"), "high": ohlc.get("high"),
        "low": ohlc.get("low"), "prev_close": pdc,
        "volume": q.get("volume"), "net_change": chg, "chg_pct": chg_pct,
    }


def quote_batch(kite, keys: list[str]) -> dict:
    """One quote() call for many 'EXCH:SYM' keys; {} on failure."""
    try:
        return kite.quote(keys) or {}
    except Exception:
        return {}


def build(symbol: str, token: int, snap: dict, static: dict,
          orb: dict | None = None) -> dict:
    """Assemble a levels dict from an already-fetched snapshot + cached statics.
    Used by the watcher so a whole cycle is one quote() call, no per-symbol I/O."""
    lv = {
        "symbol": symbol.upper(), "token": token,
        "ltp": snap.get("ltp"), "vwap": snap.get("vwap"),
        "open": snap.get("open"), "high": snap.get("high"),
        "low": snap.get("low"), "prev_close": snap.get("prev_close"),
        "volume": snap.get("volume"), "chg_pct": snap.get("chg_pct"),
        **static, "orb": orb,
    }
    lv["gap_pct"] = gap_pct(lv.get("open"), lv.get("pdc"))
    if lv.get("ltp"):
        lv["position"] = position(lv["ltp"], lv)
    return lv


def today_minutes(kite, token: int, interval: str = "5minute") -> list[dict]:
    """Today's intraday candles for charting (empty list on any failure)."""
    start = _today_start()
    try:
        rows = kite.historical_data(token, start, datetime.now(), interval)
    except Exception:
        return []
    return [{"time": r["date"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"],
             "volume": r.get("volume") or 0}
            for r in rows if r["date"].date() == start.date()]


# --------------------------------------------------------------- orchestration
def levels(kite, symbol: str, token: int, exchange: str = "NSE",
           orb_minutes: int = ORB_MINUTES, static: dict | None = None) -> dict:
    """Full intraday picture for one symbol.

    Pass `static` (from levels_static) to skip the daily-candle fetch when you
    already computed the day-static bits — the watcher does this per cycle.
    """
    snap = quote_snapshot(kite, symbol, exchange) or {}
    if static is None:
        static = levels_static(kite, token)
    lv = {
        "symbol": symbol.upper(), "token": token,
        "ltp": snap.get("ltp"), "vwap": snap.get("vwap"),
        "open": snap.get("open"), "high": snap.get("high"),
        "low": snap.get("low"), "prev_close": snap.get("prev_close"),
        "volume": snap.get("volume"), "chg_pct": snap.get("chg_pct"),
        **static,
        "orb": opening_range(kite, token, orb_minutes),
    }
    lv["gap_pct"] = gap_pct(lv.get("open"), lv.get("pdc"))
    if lv.get("ltp"):
        lv["position"] = position(lv["ltp"], lv)
    return lv


def levels_static(kite, token: int) -> dict:
    """Day-static levels (PDH/PDL/PDC + pivots). Compute once per day."""
    hlc = prev_day_hlc(kite, token)
    if not hlc:
        return {"pdh": None, "pdl": None, "pdc": None, "pivots": {}}
    pdh, pdl, pdc = hlc
    return {"pdh": pdh, "pdl": pdl, "pdc": pdc,
            "pivots": pivots(pdh, pdl, pdc)}


# --------------------------------------------------------------- formatting
def _n(v, dp=2) -> str:
    return "—" if v is None else f"{v:,.{dp}f}"


def humanize_vol(v) -> str:
    if not v:
        return "—"
    if v >= 1e7:
        return f"{v / 1e7:.1f}Cr"
    if v >= 1e5:
        return f"{v / 1e5:.1f}L"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return str(v)


def arrow(chg_pct) -> str:
    if chg_pct is None:
        return ""
    return "▲" if chg_pct >= 0 else "▼"


def as_text(lv: dict) -> str:
    """Clean, sectioned levels readout for a phone reply."""
    sym = lv["symbol"]
    ltp, cp = lv.get("ltp"), lv.get("chg_pct")
    head = f"{sym} — {_n(ltp)}"
    if cp is not None:
        head += f"  {arrow(cp)} {cp:+.2f}%"
    lines = [head]
    lines.append(f"VWAP {_n(lv.get('vwap'))} · O {_n(lv.get('open'),1)} "
                 f"H {_n(lv.get('high'),1)} L {_n(lv.get('low'),1)} · "
                 f"Vol {humanize_vol(lv.get('volume'))}")
    if lv.get("pdc"):
        g = lv.get("gap_pct")
        gtxt = f"  (gap {g:+.2f}%)" if g is not None else ""
        lines.append(f"Prev: H {_n(lv['pdh'],1)} L {_n(lv['pdl'],1)} "
                     f"C {_n(lv['pdc'],1)}{gtxt}")
    piv = lv.get("pivots") or {}
    if piv:
        lines.append(f"CPR: TC {_n(piv['TC'],1)} P {_n(piv['P'],1)} "
                     f"BC {_n(piv['BC'],1)}")
        lines.append(f"Pivots: R1 {_n(piv['R1'],1)} R2 {_n(piv['R2'],1)} · "
                     f"S1 {_n(piv['S1'],1)} S2 {_n(piv['S2'],1)}")
    orb = lv.get("orb")
    if orb:
        lines.append(f"ORB({orb['minutes']}m): {_n(orb['high'],1)} / "
                     f"{_n(orb['low'],1)}")
    pos = lv.get("position")
    if pos:
        lines.append("Position: " + ", ".join(pos))
    return "\n".join(lines)


def one_line(lv: dict) -> str:
    """Compact single-line summary (for /brief and image captions)."""
    sym, ltp, cp = lv["symbol"], lv.get("ltp"), lv.get("chg_pct")
    s = f"{sym} {_n(ltp)}"
    if cp is not None:
        s += f" {arrow(cp)}{cp:+.1f}%"
    pos = lv.get("position")
    if pos:
        s += "  [" + " · ".join(pos[:2]) + "]"
    return s
