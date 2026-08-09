"""Multi-timeframe confluence — do the timeframes agree?

A signal on the 5-minute chart means very different things depending on whether
the 15-minute, hourly and daily are pointing the same way. This scores each
timeframe from its indicators, weights the slower ones more heavily (they carry
more information about where price is actually going), and reduces it to one
number plus a readable breakdown.

The point is to *filter*, not to generate: it should stop trades more often
than it starts them.
"""
from __future__ import annotations

from . import indicators as I

# slower timeframes carry more weight
TF_WEIGHT = {
    "minute": 0.5, "3minute": 0.7, "5minute": 1.0, "10minute": 1.2,
    "15minute": 1.5, "30minute": 1.8, "60minute": 2.0, "day": 2.5,
}
DEFAULT_TFS = ("5minute", "15minute", "60minute", "day")


def score_timeframe(ind: dict) -> dict | None:
    """Score one timeframe's indicator bundle from -1 (bearish) to +1."""
    if not ind or not ind.get("now"):
        return None
    n = ind["now"]
    close = n.get("close")
    if close is None:
        return None
    parts: dict[str, int] = {}

    e = n.get("ema") or {}
    e20, e50 = e.get(20), e.get(50)
    if e20 is not None:
        parts["price vs EMA20"] = 1 if close > e20 else -1
    if e20 is not None and e50 is not None:
        parts["EMA20 vs EMA50"] = 1 if e20 > e50 else -1
    elif e50 is not None:
        parts["price vs EMA50"] = 1 if close > e50 else -1

    if n.get("st_dir") is not None:
        parts["Supertrend"] = 1 if n["st_dir"] == 1 else -1

    r = n.get("rsi")
    if r is not None:
        parts["RSI"] = 1 if r >= 55 else (-1 if r <= 45 else 0)

    m, sig = n.get("macd"), n.get("macd_signal")
    if m is not None and sig is not None:
        parts["MACD"] = 1 if m >= sig else -1

    av = n.get("avwap")
    if av is not None:
        parts["vs anchored VWAP"] = 1 if close > av else -1

    if not parts:
        return None
    raw = sum(parts.values()) / len(parts)

    adx_v = n.get("adx")
    # ADX doesn't change direction, it changes conviction
    conviction = 1.0
    if adx_v is not None:
        conviction = 0.6 if adx_v < 20 else (1.0 if adx_v < 25 else 1.2)

    return {
        "score": round(raw, 3),
        "adjusted": round(max(-1.0, min(1.0, raw * conviction)), 3),
        "parts": parts,
        "adx": adx_v,
        "trending": (adx_v is not None and adx_v >= 25),
        "close": close,
    }


def verdict(pct: float, aligned: bool) -> str:
    if aligned and pct >= 60:
        return "strong bullish"
    if aligned and pct <= -60:
        return "strong bearish"
    if pct >= 35:
        return "bullish"
    if pct <= -35:
        return "bearish"
    return "mixed"


def build(bundles: dict[str, dict]) -> dict:
    """bundles: {timeframe: indicator_bundle} -> overall confluence."""
    per: dict[str, dict] = {}
    num = den = 0.0
    for tf, ind in bundles.items():
        s = score_timeframe(ind)
        if not s:
            continue
        per[tf] = s
        w = TF_WEIGHT.get(tf, 1.0)
        num += s["adjusted"] * w
        den += w
    if not per:
        return {"score_pct": None, "verdict": "no data", "per_tf": {}}
    pct = round(num / den * 100, 1)
    signs = {(1 if s["adjusted"] > 0.15 else -1 if s["adjusted"] < -0.15 else 0)
             for s in per.values()}
    aligned = len(signs) == 1 and 0 not in signs
    return {
        "score_pct": pct,
        "aligned": aligned,
        "verdict": verdict(pct, aligned),
        "per_tf": per,
        "n_tf": len(per),
    }


def as_text(sym: str, conf: dict, tf_label=lambda t: t) -> str:
    """Phone-ready readout."""
    if not conf or conf.get("score_pct") is None:
        return f"{sym}: not enough candle history to score confluence."
    pct = conf["score_pct"]
    bar = "▲" if pct > 0 else "▼" if pct < 0 else "·"
    lines = [f"{sym} — confluence {bar} {pct:+.0f}%  ({conf['verdict']}"
             + (", aligned" if conf.get("aligned") else "") + ")"]
    for tf, s in conf["per_tf"].items():
        agree = "+" if s["adjusted"] > 0.15 else "-" if s["adjusted"] < -0.15 else "="
        bits = ", ".join(k for k, v in s["parts"].items() if v == 1) or "—"
        lines.append(f"  {tf_label(tf):>4} {agree} {s['adjusted']*100:+.0f}"
                     + (f"  ADX {s['adx']:.0f}" if s.get("adx") is not None else ""))
    bulls = []
    bears = []
    for tf, s in conf["per_tf"].items():
        for k, v in s["parts"].items():
            (bulls if v == 1 else bears if v == -1 else []).append(k)
    if bulls:
        lines.append("  for: " + ", ".join(sorted(set(bulls))))
    if bears:
        lines.append("  against: " + ", ".join(sorted(set(bears))))
    return "\n".join(lines)
