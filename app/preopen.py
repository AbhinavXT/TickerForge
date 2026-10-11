"""Pre-open gaps — which of your names will open far from last night's close.

NSE runs a pre-open call auction from 09:00 to 09:08. Orders are collected,
and at 09:08 the exchange fixes an equilibrium price (IEP) for each stock:
the price it will actually open at. NSE publishes those prices as JSON, so at
09:08 we know the gaps before the first candle exists.

That's the one moment a gap through your stop can still be planned for. At
09:15 it's a fill; at 09:08 it's a decision — sell in the opening minutes,
widen the stop, or accept the gap knowingly. This module pushes once per
weekday at 09:08 with:

  * open trades whose opening price is THROUGH their stop (max priority —
    the stop order will fill at the open, not at the stop price), and
  * any followed name (positions, holdings, watchlist) gapping by at least
    PREOPEN_GAP_ATR daily ATRs.

ATR rather than percent because a 2% gap is noise on a small-cap and a
shock on a utility. The ATR comes from stored daily candles.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import candles, config, db, indicators, instruments, journal, notify

FEED = "https://www.nseindia.com/api/market-data-pre-open?key=ALL"
GAP_ATR_DEFAULT = 1.0
PUSH_AT = (9, 8)
WINDOW_MIN = 7              # push any time 09:08–09:15 (a restart mustn't lose it)
ATR_PERIOD = 14


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse(data) -> dict[str, dict]:
    """NSE pre-open JSON -> {symbol: {iep, prev_close, gap_pct, buy_qty,
    sell_qty}}. Pure."""
    rows = data.get("data") if isinstance(data, dict) else data
    out = {}
    for rec in rows or []:
        if not isinstance(rec, dict):
            continue
        meta = rec.get("metadata") or {}
        pre = (rec.get("detail") or {}).get("preOpenMarket") or {}
        sym = (meta.get("symbol") or "").strip().upper()
        iep = _num(meta.get("iep")) or _num(pre.get("IEP")) or _num(meta.get("lastPrice"))
        prev = _num(meta.get("previousClose"))
        if not sym or not iep or not prev:
            continue
        out[sym] = {"iep": iep, "prev_close": prev,
                    "gap_pct": round((iep / prev - 1) * 100, 2),
                    "buy_qty": _num(pre.get("totalBuyQuantity")),
                    "sell_qty": _num(pre.get("totalSellQuantity"))}
    return out


def _gap_threshold() -> float:
    try:
        return float(config.get("PREOPEN_GAP_ATR", GAP_ATR_DEFAULT) or GAP_ATR_DEFAULT)
    except (TypeError, ValueError):
        return GAP_ATR_DEFAULT


def _atr(symbol: str, kite=None) -> float | None:
    inst = instruments.lookup(symbol)
    if not inst:
        return None
    tok = inst["instrument_token"]
    try:
        if kite:
            candles.ensure(kite, tok, "day", 60)
    except Exception:                                    # noqa: BLE001
        pass
    c = candles.get(tok, "day", 60)
    # Drop a bar dated today: before the open it can only be a stale partial.
    today = datetime.now().strftime("%Y-%m-%d")
    c = [b for b in c
         if datetime.fromtimestamp(b["time"]).strftime("%Y-%m-%d") != today]
    if len(c) < ATR_PERIOD + 1:
        return None
    a = indicators.atr(c, ATR_PERIOD)
    return next((v for v in reversed(a) if v), None)


def assess(prices: dict[str, dict], followed: dict[str, set[str]],
           positions: list[dict], atrs: dict[str, float | None],
           gap_atr: float = GAP_ATR_DEFAULT) -> dict:
    """Pure: which names matter this morning, and how much.

    -> {"through_stop": [...], "gaps": [...], "missing": [...]}
    """
    through, gaps, missing = [], [], []
    by_sym: dict[str, list[dict]] = {}
    for t in positions:
        by_sym.setdefault(t["symbol"].upper(), []).append(t)

    for sym, why in sorted(followed.items()):
        p = prices.get(sym)
        if not p:
            missing.append(sym)
            continue
        atr = atrs.get(sym)
        g_atr = round((p["iep"] - p["prev_close"]) / atr, 2) if atr else None
        row = {"symbol": sym, "why": sorted(why), **p, "gap_atr": g_atr,
               "atr": round(atr, 2) if atr else None}

        for t in by_sym.get(sym, []):
            stop = t.get("stop")
            if not stop:
                continue
            long_ = t["side"] == "long"
            if (long_ and p["iep"] <= stop) or (not long_ and p["iep"] >= stop):
                through.append({**row, "trade_id": t["id"], "side": t["side"],
                                "stop": stop, "entry": t["entry"],
                                "r_at_open": journal.live_r(t, p["iep"])})

        big = (g_atr is not None and abs(g_atr) >= gap_atr) or \
              (g_atr is None and abs(p["gap_pct"]) >= 3)
        if big:
            gaps.append(row)

    gaps.sort(key=lambda r: -abs(r["gap_atr"] if r["gap_atr"] is not None
                                 else r["gap_pct"]))
    return {"through_stop": through, "gaps": gaps, "missing": missing}


def check(client=None, kite=None) -> dict:
    """Fetch the auction prices and assess every followed name."""
    from . import portfolio
    from .nse import NSEClient
    client = client or NSEClient()
    if kite is None:
        try:
            from . import kite_auth
            kite = kite_auth.get_kite()
        except (Exception, SystemExit):                  # noqa: BLE001
            kite = None
    prices = parse(client.get_json(FEED))
    followed = portfolio.followed(kite)
    atrs = {s: _atr(s, kite) for s in followed if s in prices}
    res = assess(prices, followed, journal.open_positions(), atrs,
                 gap_atr=_gap_threshold())
    res["at"] = datetime.now().isoformat(timespec="seconds")
    res["auction_symbols"] = len(prices)
    return res


def _fmt_gap(r: dict) -> str:
    g = f"{r['gap_pct']:+.1f}%"
    if r.get("gap_atr") is not None:
        g += f" ({r['gap_atr']:+.1f} ATR)"
    return g


def as_text(res: dict) -> str:
    lines = [f"Pre-open — {datetime.now():%a %d %b} · gaps ≥ "
             f"{_gap_threshold():g} ATR"]
    if res["through_stop"]:
        lines.append("")
        lines.append("⚠ OPENING THROUGH YOUR STOP")
        for r in res["through_stop"]:
            rr = f" · {r['r_at_open']:+.2f}R at the open" if r.get("r_at_open") is not None else ""
            lines.append(f"  #{r['trade_id']} {r['symbol']} {r['side']} · stop "
                         f"{r['stop']:g} · opens {r['iep']:g} {_fmt_gap(r)}{rr}")
        lines.append("  a stop-market fills at the open price, not at the stop")
    if res["gaps"]:
        lines.append("")
        for r in res["gaps"][:15]:
            tag = "/".join(w[0].upper() for w in r["why"])     # W/P/H
            lines.append(f"  {r['symbol']:<12} {r['prev_close']:>9,.2f} → "
                         f"{r['iep']:>9,.2f}  {_fmt_gap(r)}  [{tag}]")
        if len(res["gaps"]) > 15:
            lines.append(f"  + {len(res['gaps']) - 15} more")
        lines.append("  W watchlist · P open trade · H holding")
    if not res["through_stop"] and not res["gaps"]:
        lines.append("Nothing you follow is gapping beyond the threshold.")
    return "\n".join(lines)


def maybe_send(now: datetime | None = None, client=None) -> bool:
    """Once per weekday, 09:08–09:15. Date-guarded in the DB like the brief,
    so a restart can't double-send. Silent when nothing crosses the bar."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    today = now.strftime("%Y-%m-%d")
    if db.get_meta("preopen_last_sent") == today:
        return False
    start = now.replace(hour=PUSH_AT[0], minute=PUSH_AT[1], second=0, microsecond=0)
    if not (start <= now < start + timedelta(minutes=WINDOW_MIN)):
        return False
    db.set_meta("preopen_last_sent", today)
    try:
        res = check(client)
    except (Exception, SystemExit) as e:                 # noqa: BLE001
        print(f"[preopen] failed: {e}")
        return True
    if not res["through_stop"] and not res["gaps"]:
        return True
    try:
        notify.send(as_text(res), title="Pre-open gaps",
                    priority="max" if res["through_stop"] else "high",
                    tags="rotating_light" if res["through_stop"] else "zap")
    except (Exception, SystemExit) as e:                 # noqa: BLE001
        print(f"[preopen] push failed: {e}")
    return True
