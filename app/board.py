"""The Board: a live, ranked scanner of the NSE F&O universe.

For every F&O underlying it looks for *displacement* — a 5-minute candle whose
body is several ATRs long and that breaks the last swing high/low — and the
*order block* that launched it: the last opposite-colour candle before the
displacement, valid until price trades back through it. Each stock is scored
on how hard it moved, what the futures OI says about who is moving it, volume,
trend strength, and how close price now sits to its unmitigated order block.

Two halves:

  * Pure maths (top of the file) works on plain candle dicts and never touches
    Kite, so tests/test_board.py drives it with synthetic candles.
  * I/O (bottom) gathers quotes, stored 5m candles and futures OI, caches the
    board for 60s during market hours and serves the last session's board
    otherwise.

Tunables are the constants just below. Everything else follows from them.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import indicators as ind

# ------------------------------------------------------------------ tunables
K_ATR = 1.5              # displacement: candle body > K_ATR × ATR
ATR_LEN = 14             # ATR period on the 5m bars
SWING_LEFT = 2           # pivot confirmation bars (left / right)
SWING_RIGHT = 2
OB_LOOKBACK = 10         # how far back from a displacement to find its OB
EQ_TOL_ATR = 0.15        # equal highs/lows: within this many ATRs
ADX_LEN = 14
ADX_CHOPPY = 18          # ADX below this = choppy → score 0
VOL_LEN = 20             # volume vs its N-bar average
OB_NEAR_ATR = 3.0        # proximity credit fades to 0 this many ATRs away
MOVE_FULL_ATR = 3.0      # a displacement this many ATRs long earns full credit
ADX_FULL = 40.0          # ADX at which the trend component maxes out
VOL_FULL = 3.0           # volume this many × average earns full credit
WEIGHTS = {              # relative weights of the score components
    "move": 30,
    "oi": 20,
    "volume": 15,
    "adx": 15,
    "proximity": 20,
}
TOP_N = 20               # rows per side on the board
CACHE_SECS = 60          # rebuild at most this often during market hours
BARS_DAYS = 5            # 5m history used for detection (≈ 5 sessions)
TRAP_RECENT = 12         # a sweep this many bars old (1h of 5m) still counts
REVERSAL_NEAR_ATR = 0.5  # "at the zone": within this many ATRs of the OB
REVERSAL_OI_MIN = 0.6    # OI_SUPPORT at or above this backs a reversal

# How each OI read supports a bullish / bearish thesis (0..1).
OI_SUPPORT = {
    "bull": {"long_buildup": 1.0, "short_covering": 0.6,
             "long_unwinding": 0.1, "short_buildup": 0.0},
    "bear": {"short_buildup": 1.0, "long_unwinding": 0.6,
             "short_covering": 0.1, "long_buildup": 0.0},
}
OI_UNKNOWN = 0.3

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                     "NIFTYNXT50", "SENSEX", "BANKEX"}


# ================================================================ pure maths
def _bull(c) -> bool:
    return c["close"] > c["open"]


def _bear(c) -> bool:
    return c["close"] < c["open"]


def find_displacements(candles: list[dict], atr_vals: list | None = None,
                       k: float = K_ATR, left: int = SWING_LEFT,
                       right: int = SWING_RIGHT) -> list[dict]:
    """Every displacement candle, oldest first.

    A displacement is a candle whose body exceeds k × ATR *and* closes beyond
    the most recent swing point confirmed before it (a high for a bullish one,
    a low for a bearish one). The swing must be confirmed by bar i — a pivot
    needs `right` later bars, so pivots the displacement itself would confirm
    don't count.
    """
    if atr_vals is None:
        atr_vals = ind.atr(candles, ATR_LEN)
    highs, lows = ind.swing_points(candles, left, right)
    out: list[dict] = []
    for i, c in enumerate(candles):
        a = atr_vals[i] if i < len(atr_vals) else None
        if not a:
            continue
        body = abs(c["close"] - c["open"])
        if body <= k * a:
            continue
        if _bull(c):
            sh = [j for j in highs if j + right < i]
            if not sh:
                continue
            level = candles[sh[-1]]["high"]
            if c["close"] > level:
                out.append({"i": i, "dir": "bull", "body": body, "atr": a,
                            "atr_mult": body / a, "broke": level,
                            "time": c["time"]})
        elif _bear(c):
            sl = [j for j in lows if j + right < i]
            if not sl:
                continue
            level = candles[sl[-1]]["low"]
            if c["close"] < level:
                out.append({"i": i, "dir": "bear", "body": body, "atr": a,
                            "atr_mult": body / a, "broke": level,
                            "time": c["time"]})
    return out


def order_block_for(candles: list[dict], disp: dict,
                    lookback: int = OB_LOOKBACK) -> dict | None:
    """The last opposite-colour candle before a displacement.

    A bullish displacement's order block is the last bearish candle before it
    (where the buying started); a bearish one's is the last bullish candle.
    The zone is that candle's full high–low range.
    """
    want = _bear if disp["dir"] == "bull" else _bull
    for j in range(disp["i"] - 1, max(-1, disp["i"] - 1 - lookback), -1):
        c = candles[j]
        if want(c):
            return {"i": j, "dir": disp["dir"], "top": c["high"],
                    "bottom": c["low"], "time": c["time"],
                    "disp_i": disp["i"]}
    return None


def is_mitigated(candles: list[dict], ob: dict) -> bool:
    """True once price has traded back *through* the zone after the
    displacement. A bullish OB dies when a later low goes below its bottom; a
    bearish one when a later high goes above its top. Touching it — the
    retest — does not kill it; that's the trade."""
    after = candles[ob["disp_i"] + 1:]
    if ob["dir"] == "bull":
        return any(c["low"] < ob["bottom"] for c in after)
    return any(c["high"] > ob["top"] for c in after)


def latest_unmitigated_ob(candles: list[dict], side: str,
                          disps: list[dict] | None = None) -> tuple:
    """(order block, its displacement) for the newest live OB on `side`, or
    (None, None)."""
    disps = disps if disps is not None else find_displacements(candles)
    for d in reversed([d for d in disps if d["dir"] == side]):
        ob = order_block_for(candles, d)
        if ob and not is_mitigated(candles, ob):
            return ob, d
    return None, None


def cluster(values: list[float], tol: float) -> list[list[float]]:
    """Group sorted values whose neighbours sit within `tol` of each other."""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def _session_start(candles: list[dict]) -> int:
    """Index of the first bar of the last session in `candles`. Bar times are
    IST-shifted epochs, so whole days divide cleanly."""
    if not candles:
        return 0
    day = candles[-1]["time"] // 86400
    i = len(candles) - 1
    while i > 0 and candles[i - 1]["time"] // 86400 == day:
        i -= 1
    return i


def liquidity_levels(candles: list[dict], atr_now: float | None,
                     pdh: float | None = None, pdl: float | None = None,
                     tol_atr: float = EQ_TOL_ATR) -> list[dict]:
    """Resting liquidity: equal highs / equal lows (two or more swing points
    within tol_atr × ATR) plus the previous day's high and low.

    `formed` is the first bar at which the level was known — the bar that
    confirmed the last swing of the cluster, or today's open for PDH/PDL —
    so a sweep is only looked for after it."""
    out: list[dict] = []
    if atr_now:
        highs, lows = ind.swing_points(candles, SWING_LEFT, SWING_RIGHT)
        tol = tol_atr * atr_now
        for idx, kind, pick in ((highs, "eqh", max), (lows, "eql", min)):
            key = "high" if kind == "eqh" else "low"
            for g in _cluster_idx([(candles[i][key], i) for i in idx], tol):
                if len(g) >= 2:
                    out.append({"price": pick(v for v, _ in g), "kind": kind,
                                "touches": len(g),
                                "formed": max(i for _, i in g) + SWING_RIGHT})
    start = _session_start(candles)
    if pdh is not None:
        out.append({"price": pdh, "kind": "pdh", "touches": 1, "formed": start})
    if pdl is not None:
        out.append({"price": pdl, "kind": "pdl", "touches": 1, "formed": start})
    return sorted(out, key=lambda x: x["price"])


def _cluster_idx(pairs: list[tuple], tol: float) -> list[list[tuple]]:
    """cluster() for (value, index) pairs."""
    groups: list[list[tuple]] = []
    for p in sorted(pairs):
        if groups and p[0] - groups[-1][-1][0] <= tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def liquidity_traps(candles: list[dict], levels: list[dict]) -> list[dict]:
    """Liquidity sweeps: a bar that runs a level and closes back inside it.

    Above a high (EQH/PDH) that traps breakout buyers and points down; below a
    low (EQL/PDL) it traps breakdown sellers and points up. Only the first
    interaction after the level formed counts: once a bar has *closed* beyond
    it the level was broken, not swept, and its liquidity is spent either way.
    Newest first.
    """
    out: list[dict] = []
    for lv in levels:
        above = lv["kind"] in ("eqh", "pdh")
        p = lv["price"]
        for i in range(max(lv.get("formed", 0), 0), len(candles)):
            c = candles[i]
            ran = c["high"] > p if above else c["low"] < p
            if not ran:
                continue
            back = c["close"] < p if above else c["close"] > p
            if back:
                out.append({"i": i, "time": c["time"], "price": p,
                            "kind": lv["kind"], "dir": "bear" if above else "bull",
                            "extreme": c["high"] if above else c["low"]})
            break
    return sorted(out, key=lambda t: -t["i"])


def reversal_setup(side: str, ob: dict | None, d_atr: float | None,
                   oi: str | None, traps: list[dict], n_bars: int,
                   recent: int = TRAP_RECENT) -> dict | None:
    """Reversal build-up: price is back at its live order block and something
    says the side is defending it — OI on that side, or a fresh sweep of the
    opposite liquidity pointing the same way. None when it isn't."""
    if side not in ("bull", "bear") or not ob or d_atr is None \
            or d_atr > REVERSAL_NEAR_ATR:
        return None
    why = []
    if oi and OI_SUPPORT[side].get(oi, 0) >= REVERSAL_OI_MIN:
        why.append("oi")
    if any(t["dir"] == side and t["i"] >= n_bars - recent for t in traps):
        why.append("trap")
    return {"at_zone": True, "why": why} if why else None


def oi_read(price_chg_pct: float | None, oi_chg_pct: float | None) -> str | None:
    """The four classic price × open-interest reads."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    if price_chg_pct >= 0:
        return "long_buildup" if oi_chg_pct >= 0 else "short_covering"
    return "short_buildup" if oi_chg_pct >= 0 else "long_unwinding"


def volume_ratio(candles: list[dict], n: int = VOL_LEN) -> float | None:
    """Last bar's volume against the average of the n bars before it."""
    if len(candles) < n + 1:
        return None
    base = [c.get("volume") or 0 for c in candles[-n - 1:-1]]
    avg = sum(base) / n
    return (candles[-1].get("volume") or 0) / avg if avg else None


def proximity(ltp: float | None, ob: dict | None, atr_now: float | None) -> tuple:
    """(credit 0..1, distance in ATRs, signed % distance) from LTP to the
    zone. Inside the zone is full credit and zero distance."""
    if ltp is None or not ob or not atr_now:
        return 0.0, None, None
    if ob["bottom"] <= ltp <= ob["top"]:
        return 1.0, 0.0, 0.0
    edge = ob["top"] if ltp > ob["top"] else ob["bottom"]
    d_atr = abs(ltp - edge) / atr_now
    return max(0.0, 1 - d_atr / OB_NEAR_ATR), d_atr, (edge - ltp) / ltp * 100


def score(side: str, *, move_atr: float | None, oi: str | None,
          vol_ratio: float | None, adx: float | None,
          prox: float, weights: dict = WEIGHTS) -> dict:
    """0–100 for `side`, with the per-component credits that produced it.

    ADX below ADX_CHOPPY (or unknown) is choppy and scores 0 outright: order
    blocks in a range get run through both ways, so ranking them is noise.
    """
    if adx is None or adx < ADX_CHOPPY:
        return {"score": 0, "choppy": True, "parts": {}}
    parts = {
        "move": min(1.0, (move_atr or 0) / MOVE_FULL_ATR),
        "oi": OI_SUPPORT[side].get(oi, OI_UNKNOWN) if oi else OI_UNKNOWN,
        "volume": min(1.0, max(0.0, ((vol_ratio or 0) - 1) / (VOL_FULL - 1))),
        "adx": min(1.0, max(0.0, (adx - ADX_CHOPPY) / (ADX_FULL - ADX_CHOPPY))),
        "proximity": max(0.0, min(1.0, prox)),
    }
    total = sum(weights.values()) or 1
    val = sum(weights[k] * parts[k] for k in weights) / total * 100
    return {"score": int(round(val)), "choppy": False,
            "parts": {k: round(v, 3) for k, v in parts.items()}}


def nearest_levels(candles: list[dict], ltp: float | None) -> dict:
    """Nearest confirmed swing high above and swing low below the LTP."""
    if ltp is None or not candles:
        return {"resistance": None, "support": None}
    highs, lows = ind.swing_points(candles, SWING_LEFT, SWING_RIGHT)
    above = [candles[i]["high"] for i in highs if candles[i]["high"] > ltp]
    below = [candles[i]["low"] for i in lows if candles[i]["low"] < ltp]
    return {"resistance": min(above) if above else None,
            "support": max(below) if below else None}


def analyze(candles: list[dict], *, ltp: float | None = None,
            chg_pct: float | None = None, oi_chg_pct: float | None = None,
            pdh: float | None = None, pdl: float | None = None) -> dict:
    """Everything the board knows about one stock, from its 5m candles plus
    a quote. Picks the side (bull/bear) that scores higher."""
    if not candles:
        return {"side": "choppy", "score": 0, "choppy": True}
    ltp = ltp if ltp is not None else candles[-1]["close"]
    atr_vals = ind.atr(candles, ATR_LEN)
    atr_now = next((a for a in reversed(atr_vals) if a), None)
    adx_vals = ind.adx(candles, ADX_LEN)
    adx_now = next((a for a in reversed(adx_vals) if a is not None), None)
    disps = find_displacements(candles, atr_vals)
    vr = volume_ratio(candles)
    oi = oi_read(chg_pct, oi_chg_pct)

    best = None
    for side in ("bull", "bear"):
        ob, d = latest_unmitigated_ob(candles, side, disps)
        prox, d_atr, d_pct = proximity(ltp, ob, atr_now)
        sc = score(side, move_atr=d["atr_mult"] if d else None, oi=oi,
                   vol_ratio=vr, adx=adx_now, prox=prox)
        # a side with no live order block has nothing to trade back into
        if not ob and not sc["choppy"]:
            sc = {**sc, "score": int(round(sc["score"] * 0.5))}
        cand = {"side": side, **sc, "ob": ob, "disp": d,
                "ob_dist_atr": None if d_atr is None else round(d_atr, 2),
                "ob_dist_pct": None if d_pct is None else round(d_pct, 2)}
        # tie-break on the day's direction
        if best is None or cand["score"] > best["score"] or (
                cand["score"] == best["score"] and chg_pct is not None
                and ((chg_pct >= 0) == (side == "bull"))):
            best = cand
    if best["choppy"]:
        best = {**best, "side": "choppy", "score": 0}
    lv = nearest_levels(candles, ltp)
    liq = liquidity_levels(candles, atr_now, pdh, pdl)
    traps = liquidity_traps(candles, liq)
    recent = [t for t in traps if t["i"] >= len(candles) - TRAP_RECENT]
    setup = None if best["choppy"] else reversal_setup(
        best["side"], best.get("ob"), best.get("ob_dist_atr"), oi, traps,
        len(candles))
    return {
        **best, "ltp": ltp, "chg_pct": chg_pct, "atr": atr_now,
        "adx": None if adx_now is None else round(adx_now, 1),
        "vol_ratio": None if vr is None else round(vr, 2),
        "oi_read": oi, "oi_chg_pct": oi_chg_pct,
        "move_atr": None if not best.get("disp") else round(best["disp"]["atr_mult"], 2),
        **lv, "liquidity": liq, "traps": traps, "trap": recent[0] if recent else None,
        "reversal": setup,
    }


def rank(rows: list[dict], side: str, n: int = TOP_N) -> list[dict]:
    """Top n rows for a side, best first, numbered."""
    picked = sorted((r for r in rows if r.get("side") == side),
                    key=lambda r: (-r["score"], -(abs(r.get("chg_pct") or 0))))[:n]
    return [{**r, "rank": i + 1} for i, r in enumerate(picked)]


def breadth(rows: list[dict]) -> dict:
    out = {"bull": 0, "choppy": 0, "bear": 0}
    for r in rows:
        out[r.get("side", "choppy")] = out.get(r.get("side", "choppy"), 0) + 1
    return out


def sector_flow(rows: list[dict], sectors: dict[str, str]) -> list[dict]:
    """Average % change per sector, strongest first. Stocks without a known
    sector go under 'Unclassified'."""
    acc: dict[str, list[float]] = {}
    for r in rows:
        if r.get("chg_pct") is None:
            continue
        acc.setdefault(sectors.get(r["symbol"]) or "Unclassified", []).append(r["chg_pct"])
    out = [{"sector": k, "chg_pct": round(sum(v) / len(v), 2), "n": len(v)}
           for k, v in acc.items()]
    return sorted(out, key=lambda x: -x["chg_pct"])


def retest_condition(ob: dict | None) -> str | None:
    """The /api/alerts condition that fires on a retest of the zone: a
    bullish OB is retested from above (price comes down to its top), a
    bearish one from below (price rises to its bottom)."""
    if not ob:
        return None
    if ob["dir"] == "bull":
        return f"< {round(ob['top'], 2):g}"
    return f"> {round(ob['bottom'], 2):g}"


# ======================================================================= I/O
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}
_market_cache: dict = {"at": 0.0, "data": None}
_sweeper: dict = {"thread": None}


def now_ist() -> datetime:
    return datetime.now(IST)


def market_open(now: datetime | None = None) -> bool:
    """Weekday 09:15–15:30 IST. Exchange holidays aren't known here."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE


def universe() -> list[dict]:
    """F&O underlyings: the stock futures in the instrument index, each with
    its NSE equity token and the nearest-expiry (current-month) future.
    Index futures are skipped — the board ranks stocks."""
    from . import db
    today = date.today().isoformat()
    with db.connect() as conn:
        futs = conn.execute(
            "SELECT name, tradingsymbol, instrument_token, expiry FROM instruments "
            "WHERE exchange='NFO' AND instrument_type='FUT' "
            "AND (expiry IS NULL OR expiry >= ?) ORDER BY name, expiry",
            (today,)).fetchall()
        eq = {r["tradingsymbol"]: r["instrument_token"] for r in conn.execute(
            "SELECT tradingsymbol, instrument_token FROM instruments "
            "WHERE exchange='NSE' AND instrument_type='EQ'").fetchall()}
    out, seen = [], set()
    for f in futs:
        name = (f["name"] or "").upper()
        if not name or name in seen or name in INDEX_UNDERLYINGS or name not in eq:
            continue
        seen.add(name)
        out.append({"symbol": name, "token": eq[name],
                    "fut": f["tradingsymbol"], "fut_token": f["instrument_token"]})
    return out


def _prev_oi(kite, futs: list[dict]) -> dict[int, float]:
    """Previous session's closing OI per future, fetched once a day
    (historical_data with oi=True) and kept in SQLite."""
    from . import db
    today = date.today().isoformat()
    with db.connect() as conn:
        have = {r["token"]: r["oi"] for r in conn.execute(
            "SELECT token, oi FROM board_oi_prev WHERE day=?", (today,)).fetchall()}
    missing = [f for f in futs if f["fut_token"] not in have]
    rows = []
    for f in missing:
        try:
            bars = kite.historical_data(f["fut_token"], date.today() - timedelta(days=7),
                                        date.today() - timedelta(days=1), "day", oi=True)
        except Exception:
            continue
        if bars:
            have[f["fut_token"]] = bars[-1].get("oi") or 0
            rows.append((f["fut_token"], today, have[f["fut_token"]]))
        time.sleep(0.35)  # Kite: ~3 historical calls / second
    if rows:
        with db.connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO board_oi_prev VALUES (?, ?, ?)", rows)
    return have


def _cached_prev_oi() -> dict[int, float]:
    from . import db
    with db.connect() as conn:
        return {r["token"]: r["oi"] for r in conn.execute(
            "SELECT token, oi FROM board_oi_prev WHERE day=?",
            (date.today().isoformat(),)).fetchall()}


_oi_job: dict = {"day": None, "thread": None}


def _ensure_prev_oi(kite, uni: list[dict]) -> None:
    """Fill today's previous-session OI in the background (≈1 call per
    future, once a day). Until it lands the board shows OI as unknown."""
    today = date.today().isoformat()
    t = _oi_job["thread"]
    if _oi_job["day"] == today or (t is not None and t.is_alive()):
        return
    def run():
        _prev_oi(kite, uni)
        _oi_job["day"] = today
    _oi_job["thread"] = threading.Thread(target=run, daemon=True, name="board-oi")
    _oi_job["thread"].start()


def _sweep(kite, uni: list[dict]) -> None:
    """Keep the 5m (and daily) candle store topped up for the universe,
    gently: one symbol at a time under Kite's historical rate limit. Runs
    only while the market is open."""
    from . import candles
    while market_open():
        for u in uni:
            if not market_open():
                break
            try:
                candles.ensure(kite, u["token"], "5minute", BARS_DAYS)
            except Exception:
                pass
            time.sleep(0.4)
    _sweeper["thread"] = None


def _ensure_sweeper(kite, uni) -> None:
    t = _sweeper.get("thread")
    if market_open() and (t is None or not t.is_alive()):
        t = threading.Thread(target=_sweep, args=(kite, uni), daemon=True,
                             name="board-sweep")
        _sweeper["thread"] = t
        t.start()


def _sectors() -> dict[str, str]:
    try:
        from . import fundamentals
        return {s: v[0] for s, v in fundamentals.classification().items() if v[0]}
    except Exception:
        return {}


def _row(u: dict, bars: list[dict], q: dict, fq: dict, prev_oi: float | None,
         daily: list[dict]) -> dict:
    from . import intraday
    snap = intraday.normalize_quote(q) if q else {}
    oi_now = (fq or {}).get("oi")
    oi_chg = ((oi_now - prev_oi) / prev_oi * 100) if (oi_now and prev_oi) else None
    pdh = daily[-2]["high"] if len(daily) >= 2 else None
    pdl = daily[-2]["low"] if len(daily) >= 2 else None
    a = analyze(bars, ltp=snap.get("ltp"), chg_pct=snap.get("chg_pct"),
                oi_chg_pct=oi_chg, pdh=pdh, pdl=pdl)
    ob = a.get("ob")
    return {
        "symbol": u["symbol"], "side": a["side"], "score": a["score"],
        "ltp": a.get("ltp"), "chg_pct": a.get("chg_pct"),
        "oi_read": a.get("oi_read"),
        "oi_chg_pct": None if oi_chg is None else round(oi_chg, 2),
        "ob": None if not ob else {"top": ob["top"], "bottom": ob["bottom"],
                                   "time": ob["time"], "dir": ob["dir"]},
        "ob_dist_pct": a.get("ob_dist_pct"), "ob_dist_atr": a.get("ob_dist_atr"),
        "move_atr": a.get("move_atr"), "vol_ratio": a.get("vol_ratio"),
        "adx": a.get("adx"), "parts": a.get("parts", {}),
        "trap": _trap_brief(a.get("trap")), "reversal": a.get("reversal"),
    }


def _trap_brief(t: dict | None) -> dict | None:
    return None if not t else {k: t[k] for k in ("time", "price", "kind", "dir")}


def build(kite) -> dict:
    """Compute the whole board now (one quote call + stored candles)."""
    from . import candles, intraday
    uni = universe()
    if not uni:
        return {"built_at": now_ist().isoformat(timespec="seconds"),
                "live": market_open(), "rows": [], "bull": [], "bear": [],
                "breadth": breadth([]), "sectors": [],
                "note": "No F&O futures in the instrument index — run "
                        "`python -m app.cli sync-instruments` once to load NFO."}
    _ensure_sweeper(kite, uni)
    keys = [f"NSE:{u['symbol']}" for u in uni] + [f"NFO:{u['fut']}" for u in uni]
    quotes: dict = {}
    for i in range(0, len(keys), 450):            # Kite: ≤500 keys per call
        quotes.update(intraday.quote_batch(kite, keys[i:i + 450]))
    _ensure_prev_oi(kite, uni)
    prev = _cached_prev_oi()
    rows = []
    for u in uni:
        bars = candles.get(u["token"], "5minute", BARS_DAYS)
        daily = candles.get(u["token"], "day", 10)
        rows.append(_row(u, bars, quotes.get(f"NSE:{u['symbol']}"),
                         quotes.get(f"NFO:{u['fut']}"), prev.get(u["fut_token"]), daily))
    sect = _sectors()
    flow = sector_flow(rows, sect)
    return {
        "built_at": now_ist().isoformat(timespec="seconds"),
        "live": market_open(),
        "rows": rows,
        "bull": rank(rows, "bull"),
        "bear": rank(rows, "bear"),
        "breadth": breadth(rows),
        "sectors": flow,
        "leading": flow[0] if flow else None,
        "lagging": flow[-1] if flow else None,
        "tunables": {"k_atr": K_ATR, "eq_tol_atr": EQ_TOL_ATR,
                     "adx_choppy": ADX_CHOPPY, "weights": WEIGHTS},
    }


def _save_snapshot(data: dict) -> None:
    if not data["rows"]:          # an empty board would outlive the sync that fixes it
        return
    from . import db
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO board_snapshot VALUES (1, ?, ?)",
                     (data["built_at"], json.dumps(data)))


def _load_snapshot() -> dict | None:
    from . import db
    with db.connect() as conn:
        r = conn.execute("SELECT data FROM board_snapshot WHERE id=1").fetchone()
    return json.loads(r["data"]) if r else None


def get(kite_factory) -> dict:
    """The board. During market hours a cached build no older than
    CACHE_SECS; outside them the last session's board (built once and kept
    in SQLite so a restart serves it too). `kite_factory` is only called
    when a build is actually needed."""
    with _lock:
        fresh = _cache["data"] and time.time() - _cache["at"] < CACHE_SECS
        if market_open():
            if not fresh:
                _cache["data"] = build(kite_factory())
                _cache["at"] = time.time()
                _save_snapshot(_cache["data"])
            return {**_cache["data"], "stale": False}
        if _cache["data"] and _cache["data"]["rows"]:
            return {**_cache["data"], "stale": True}
        snap = _load_snapshot()
        if snap is None or not snap["rows"]:
            snap = build(kite_factory())
            _save_snapshot(snap)
        _cache["data"], _cache["at"] = snap, time.time()
        return {**snap, "stale": True}


def detail(kite, symbol: str) -> dict | None:
    """Zone view for one stock: recent 5m candles, the live order block,
    levels, liquidity and the score breakdown."""
    from . import candles, intraday
    sym = symbol.upper()
    u = next((x for x in universe() if x["symbol"] == sym), None)
    if u is None:
        from . import instruments
        inst = instruments.lookup(sym, "NSE")
        if not inst:
            return None
        u = {"symbol": sym, "token": inst["instrument_token"], "fut": None, "fut_token": None}
    try:
        candles.ensure(kite, u["token"], "5minute", BARS_DAYS)
        candles.ensure(kite, u["token"], "day", 15)
    except Exception:
        pass
    bars = candles.get(u["token"], "5minute", BARS_DAYS)
    daily = candles.get(u["token"], "day", 10)
    keys = [f"NSE:{sym}"] + ([f"NFO:{u['fut']}"] if u["fut"] else [])
    quotes = intraday.quote_batch(kite, keys)
    snap = intraday.normalize_quote(quotes[f"NSE:{sym}"]) if quotes.get(f"NSE:{sym}") else {}
    oi_chg = None
    if u["fut"]:
        prev = _prev_oi(kite, [u]).get(u["fut_token"])
        oi_now = (quotes.get(f"NFO:{u['fut']}") or {}).get("oi")
        if oi_now and prev:
            oi_chg = (oi_now - prev) / prev * 100
    pdh = daily[-2]["high"] if len(daily) >= 2 else None
    pdl = daily[-2]["low"] if len(daily) >= 2 else None
    a = analyze(bars, ltp=snap.get("ltp"), chg_pct=snap.get("chg_pct"),
                oi_chg_pct=oi_chg, pdh=pdh, pdl=pdl)
    ob = a.get("ob")
    return {
        "symbol": sym, "fut": u["fut"], "side": a["side"], "score": a["score"],
        "choppy": a.get("choppy", False), "ltp": a.get("ltp"),
        "chg_pct": a.get("chg_pct"), "atr": a.get("atr"),
        "candles": bars[-150:],
        "ob": None if not ob else {"top": ob["top"], "bottom": ob["bottom"],
                                   "time": ob["time"], "dir": ob["dir"]},
        "resistance": a.get("resistance"), "support": a.get("support"),
        "liquidity": a.get("liquidity", []), "pdh": pdh, "pdl": pdl,
        "traps": [_trap_brief(t) for t in a.get("traps", [])
                  if t["time"] >= bars[-150:][0]["time"]] if bars else [],
        "trap": _trap_brief(a.get("trap")), "reversal": a.get("reversal"),
        "why": {"move_atr": a.get("move_atr"),
                "oi_chg_pct": None if oi_chg is None else round(oi_chg, 2),
                "oi_read": a.get("oi_read"), "vol_ratio": a.get("vol_ratio"),
                "adx": a.get("adx"), "ob_dist_atr": a.get("ob_dist_atr"),
                "ob_dist_pct": a.get("ob_dist_pct"), "parts": a.get("parts", {})},
        "alert_cond": retest_condition(ob),
        "weights": WEIGHTS,
    }


def market(kite_factory) -> dict:
    """Shell top-bar status: open/closed, IST time, NIFTY last and % change.
    NIFTY is cached 30s and simply omitted when there's no Kite session."""
    now = now_ist()
    out = {"open": market_open(now), "ist": now.strftime("%H:%M:%S"), "nifty": None}
    with _lock:
        if _market_cache["data"] is not None and time.time() - _market_cache["at"] < 30:
            out["nifty"] = _market_cache["data"]
            return out
    try:
        kite = kite_factory()
        from . import intraday
        q = intraday.quote_batch(kite, ["NSE:NIFTY 50"]).get("NSE:NIFTY 50")
        if q:
            s = intraday.normalize_quote(q)
            out["nifty"] = {"last": s["ltp"], "chg_pct": s["chg_pct"]}
    except (SystemExit, Exception):
        out["nifty"] = None
    with _lock:
        _market_cache["data"], _market_cache["at"] = out["nifty"], time.time()
    return out


# ============================================================ phone text
OI_SHORT = {"long_buildup": "LB", "short_buildup": "SB",
            "short_covering": "SC", "long_unwinding": "LU"}


def _pct(v) -> str:
    return "—" if v is None else f"{'▲' if v > 0 else '▼' if v < 0 else '■'}{v:+.2f}%"


def _flags(r: dict) -> str:
    """Short suffix for the reversal and trap badges."""
    out = ""
    if r.get("reversal"):
        out += " · REV"
    t = r.get("trap")
    if t:
        out += f" · trap {'▲' if t['dir'] == 'bull' else '▼'}{t['kind'].upper()}"
    return out


def as_text(b: dict, n: int = 5) -> str:
    """/board: breadth plus the top n each side, one line per stock."""
    br = b.get("breadth") or {}
    head = (f"Board {'(last session) ' if b.get('stale') else ''}"
            f"{(b.get('built_at') or '')[11:16]} IST\n"
            f"▲{br.get('bull', 0)} bull · {br.get('choppy', 0)} choppy · "
            f"▼{br.get('bear', 0)} bear")
    if b.get("leading"):
        head += f"\nlead {b['leading']['sector']} {_pct(b['leading']['chg_pct'])}"
    if b.get("lagging") and b.get("lagging") is not b.get("leading"):
        head += f" · lag {b['lagging']['sector']} {_pct(b['lagging']['chg_pct'])}"
    lines = [head]
    for side, title in (("bull", "BULLISH"), ("bear", "BEARISH")):
        rows = (b.get(side) or [])[:n]
        lines.append(f"\n{title}")
        if not rows:
            lines.append("  none")
        for r in rows:
            ob = ("in OB" if r.get("ob_dist_pct") == 0 else
                  f"OB {r['ob_dist_pct']:+.1f}%" if r.get("ob_dist_pct") is not None else "no OB")
            lines.append(f"{r.get('rank', '·')}. {r['symbol']} {r['score']} "
                         f"{_pct(r.get('chg_pct'))} {OI_SHORT.get(r.get('oi_read'), '—')} {ob}"
                         f"{_flags(r)}")
    if b.get("note"):
        lines.append("\n" + b["note"])
    return "\n".join(lines)


# ======================================================== phone payload
def _num(v) -> str:
    """A compact number for the wire: no trailing zeros, empty for None."""
    if v is None:
        return ""
    return f"{round(float(v), 2):g}"


def _wire(text: str) -> str:
    """A free-text field made safe for the row and field separators."""
    return str(text).replace(";", " ").replace("|", " ").strip()


def _payload_row(r: dict) -> str:
    """One ranked stock, positionally:
    rank|symbol|score|ltp|chg%|oi|ob bottom|ob top|ob dist%|rev|trap|vol×|adx|move ATR

    `oi` is the two-letter read (LB/SB/SC/LU), `rev` is 1 for a reversal
    build-up, and `trap` is the sweep's direction (+ bull, - bear) followed by
    the level kind, e.g. `+PDL`."""
    ob = r.get("ob") or {}
    t = r.get("trap")
    trap = f"{'+' if t['dir'] == 'bull' else '-'}{t['kind'].upper()}" if t else ""
    return "|".join([
        str(r.get("rank", "")), _wire(r["symbol"]), str(r.get("score", 0)),
        _num(r.get("ltp")), _num(r.get("chg_pct")),
        OI_SHORT.get(r.get("oi_read"), ""),
        _num(ob.get("bottom")), _num(ob.get("top")), _num(r.get("ob_dist_pct")),
        "1" if r.get("reversal") else "", trap,
        _num(r.get("vol_ratio")), _num(r.get("adx")), _num(r.get("move_atr")),
    ])


def as_payload(b: dict, limit: int = 4096) -> str:
    """/board nf: the board as one structured message for the NewsForge app.

    Same conventions as /candles: the phone's reader flattens JSON to dotted
    keys and skips arrays, so the rows travel as strings (`;` between rows,
    `|` between fields). Everything is sized to `limit`: the weakest rows go
    first, alternating from the longer side, then the quietest sectors — a
    board that arrives short is still a board; one that overflows is nothing.
    """
    try:
        ts = int(datetime.fromisoformat(b.get("built_at")).timestamp())
    except (TypeError, ValueError):
        ts = int(time.time())
    br = b.get("breadth") or {}
    bull = [_payload_row(r) for r in b.get("bull") or []]
    bear = [_payload_row(r) for r in b.get("bear") or []]
    sectors = [f"{_wire(s['sector'])}|{_num(s['chg_pct'])}|{s.get('n', '')}"
               for s in b.get("sectors") or []]

    def render() -> str:
        return json.dumps({
            "nf": 1, "kind": "board", "ts": ts,
            "live": 1 if b.get("live") else 0,
            "stale": 1 if b.get("stale") else 0,
            "breadth": f"{br.get('bull', 0)}|{br.get('choppy', 0)}|{br.get('bear', 0)}",
            "sectors": ";".join(sectors),
            "bull": ";".join(bull), "bear": ";".join(bear),
        }, separators=(",", ":"), ensure_ascii=False)

    out = render()
    while len(out.encode()) > limit and (bull or bear or sectors):
        if bull or bear:
            (bull if len(bull) >= len(bear) else bear).pop()
        else:
            # Strongest and weakest are the ones worth keeping; drop from the middle.
            sectors.pop(len(sectors) // 2)
        out = render()
    return out


def detail_text(d: dict) -> str:
    """/ob SYM: the zone, its levels and why it ranks."""
    side = {"bull": "▲ bullish", "bear": "▼ bearish"}.get(d.get("side"), "choppy")
    f = lambda v: "—" if v is None else f"{v:,.2f}"
    lines = [f"{d['symbol']} {f(d.get('ltp'))} {_pct(d.get('chg_pct'))} · {side} · score {d.get('score', 0)}"]
    ob = d.get("ob")
    lines.append(f"OB {f(ob['bottom'])}–{f(ob['top'])} ({ob['dir']})" if ob else "No live order block")
    lines.append(f"R {f(d.get('resistance'))} · S {f(d.get('support'))}")
    liq = d.get("liquidity") or []
    if liq:
        lines.append("liq " + " · ".join(f"{l['kind'].upper()} {f(l['price'])}" for l in liq[:4]))
    t = d.get("trap")
    if t:
        swept = "highs" if t["dir"] == "bear" else "lows"
        lines.append(f"liquidity trap: swept {t['kind'].upper()} {f(t['price'])} "
                     f"({swept}) → {'▲' if t['dir'] == 'bull' else '▼'}")
    rv = d.get("reversal")
    if rv:
        lines.append("reversal build-up at the OB (" + " + ".join(
            {"oi": "OI", "trap": "sweep"}[k] for k in rv["why"]) + ")")
    w = d.get("why") or {}
    lines.append(
        f"disp {w.get('move_atr') or '—'}×ATR · OI {_pct(w.get('oi_chg_pct'))} "
        f"{OI_SHORT.get(w.get('oi_read'), '')} · vol {w.get('vol_ratio') or '—'}× · ADX {w.get('adx') or '—'}")
    if d.get("choppy"):
        lines.append("ADX < 18: choppy — not ranked.")
    if d.get("alert_cond"):
        lines.append(f"retest alert: /alert {d['symbol']} {d['alert_cond']}")
    return "\n".join(lines)
