"""Kite portfolio — read-only.

Holdings and positions pulled from the broker and annotated with everything
TickerForge already knows about each name: what your own valuation models say
it's worth, where the strategies stand, where a chandelier stop would sit, and
when the next results land.

The point isn't the P&L — Kite shows you that. It's that the P&L sits next to
the reasons to act on it. A holding down 12% reads differently depending on
whether your valuation says it's now undervalued, whether the trend signal has
flipped flat, and whether it reports on Thursday.

**Read-only, deliberately and permanently.** This module imports nothing that
places, modifies or cancels an order, and it never will. Order placement is
roadmap Phase 6, gated behind a paper broker and an approval step; when it
arrives it belongs in its own module with its own guard rails, not bolted onto
the thing that renders a table. If you find yourself adding `place_order` here,
that's the signal to stop and go read Phase 6.

Caching: holdings change on settlement, not tick by tick, and Kite rate-limits.
The broker call is cached briefly so a page refresh doesn't cost an API call.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from . import (alerts, candles, db, indicators, instruments, journal,
               kite_auth, valuation, watchlist)

CACHE_SECONDS = 60
_cache: dict[str, tuple[float, list]] = {}

CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.0


class PortfolioError(RuntimeError):
    """Broker unreachable, or no session."""


# ------------------------------------------------------------------ fetch
def _cached(key: str, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    _cache.clear()


def raw_holdings(kite=None) -> list[dict]:
    """Settled equity holdings (the demat pile)."""
    def go():
        k = kite or kite_auth.get_kite()
        return k.holdings() or []
    try:
        return _cached("holdings", go)
    except SystemExit as e:
        raise PortfolioError(str(e)) from e
    except Exception as e:
        raise PortfolioError(f"could not read holdings: {e}") from e


def raw_positions(kite=None) -> list[dict]:
    """Open intraday/F&O positions. `net` only — `day` double-counts anything
    carried over, which makes the same trade look like two."""
    def go():
        k = kite or kite_auth.get_kite()
        p = k.positions() or {}
        return p.get("net", []) if isinstance(p, dict) else list(p)
    try:
        return _cached("positions", go)
    except SystemExit as e:
        raise PortfolioError(str(e)) from e
    except Exception as e:
        raise PortfolioError(f"could not read positions: {e}") from e


# ------------------------------------------------------------- annotation
def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _chandelier(kite, token: int, avg_price: float) -> dict | None:
    """Where an ATR trailing stop would sit, and how far price is above it.

    Uses the rolling chandelier rather than the from-entry ratchet, because a
    holding's true entry date isn't in the Kite payload — `avg_price` is a
    blended cost across every purchase. A rolling stop is the honest answer to
    "where would I get out if I were managing this actively", which is the
    question the column is for.
    """
    try:
        c = candles.get(token, "day", 200)
        if len(c) < CHANDELIER_PERIOD + 2:
            return None
        long_stop, _ = indicators.chandelier(c, CHANDELIER_PERIOD, CHANDELIER_MULT)
        stop = next((v for v in reversed(long_stop) if v is not None), None)
        if stop is None:
            return None
        last = c[-1]["close"]
        return {
            "stop": round(stop, 2),
            "distance_pct": round((last - stop) / last * 100, 2),
            "below_cost": stop < avg_price if avg_price else None,
        }
    except Exception:
        return None


def _signal_stance(symbol: str) -> dict | None:
    """What the configured strategies currently say. Read from stored state
    rather than recomputed — the EOD scan owns that, and re-running six
    strategies per holding on a page load would be slow and wasteful."""
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT strategy, state FROM signal_state WHERE symbol=?",
                (symbol.upper(),)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    longs = [r["strategy"] for r in rows if r["state"]]
    return {"long": longs, "total": len(rows),
            "stance": ("long" if len(longs) * 2 > len(rows)
                       else "flat" if not longs else "mixed")}


def _valuation_verdict(symbol: str) -> dict | None:
    try:
        v = valuation.value(symbol)
    except Exception:
        return None
    if not v:
        return None
    return {"verdict": v.get("verdict"), "composite": v.get("composite"),
            "mos_pct": v.get("mos_pct")}


def _next_result(symbol: str) -> str | None:
    try:
        from . import results as R
        card = R.card(symbol, today=date.today())
    except Exception:
        return None
    if not card or card.get("released"):
        return None
    return card.get("date")


def annotate(row: dict, kite=None) -> dict:
    """Attach everything TickerForge knows to one holding."""
    sym = row["symbol"]
    out = dict(row)
    out["valuation"] = _valuation_verdict(sym)
    out["signals"] = _signal_stance(sym)
    out["results_due"] = _next_result(sym)
    if row.get("token"):
        out["chandelier"] = _chandelier(kite, row["token"], row.get("avg_price") or 0)
    out["alerts"] = len(alerts.active(sym))
    out["journal_open"] = len(journal.open_positions(sym))
    return out


# -------------------------------------------------------------- normalise
def _norm_holding(h: dict) -> dict:
    sym = (h.get("tradingsymbol") or "").upper()
    qty = int(_num(h.get("quantity")) + _num(h.get("t1_quantity")))
    avg = _num(h.get("average_price"))
    last = _num(h.get("last_price"))
    inst = instruments.lookup(sym)
    value = qty * last
    cost = qty * avg
    return {
        "symbol": sym,
        "name": (inst or {}).get("name") or sym,
        "token": (inst or {}).get("instrument_token") or h.get("instrument_token"),
        "exchange": h.get("exchange") or "NSE",
        "qty": qty,
        "avg_price": round(avg, 2),
        "last_price": round(last, 2),
        "value": round(value, 2),
        "cost": round(cost, 2),
        "pnl": round(value - cost, 2),
        "pnl_pct": round((last / avg - 1) * 100, 2) if avg else None,
        "day_change_pct": round(_num(h.get("day_change_percentage")), 2),
        "kind": "holding",
    }


def _norm_position(p: dict) -> dict:
    sym = (p.get("tradingsymbol") or "").upper()
    qty = int(_num(p.get("quantity")))
    avg = _num(p.get("average_price"))
    last = _num(p.get("last_price"))
    inst = instruments.lookup(sym)
    return {
        "symbol": sym,
        "name": (inst or {}).get("name") or sym,
        "token": (inst or {}).get("instrument_token") or p.get("instrument_token"),
        "exchange": p.get("exchange") or "NSE",
        "qty": qty,
        "avg_price": round(avg, 2),
        "last_price": round(last, 2),
        "value": round(abs(qty) * last, 2),
        "cost": round(abs(qty) * avg, 2),
        "pnl": round(_num(p.get("pnl")), 2),
        "pnl_pct": round((last / avg - 1) * 100 * (1 if qty >= 0 else -1), 2)
                   if avg else None,
        "product": p.get("product"),
        "side": "long" if qty > 0 else "short" if qty < 0 else "flat",
        "kind": "position",
    }


def raw_mf_holdings(kite=None) -> tuple[list[dict], str | None]:
    """Mutual fund units held through Kite. -> (rows, reason it's empty).

    Returns a reason rather than raising, because a missing MF feed must not
    take the equity portfolio down with it. Two ways it comes back empty and
    they mean opposite things:

    * **Not subscribed.** Kite Connect's mutual-fund API is a separate add-on
      from the equity one. Without it this 403s, which is a billing fact, not
      an error in your setup — so it's reported as a sentence rather than a
      stack trace, once, and the rest of the page renders.
    * **Genuinely no funds.** Also empty, and it would be wrong to show the
      same message for it.
    """
    def go():
        k = kite or kite_auth.get_kite()
        return k.mf_holdings() or []
    try:
        return _cached("mf", go), None
    except SystemExit as e:
        return [], str(e)
    except Exception as e:                           # noqa: BLE001
        text = str(e).lower()
        if "permission" in text or "403" in text or "subscribe" in text:
            return [], ("Kite's mutual-fund API is a separate subscription "
                        "from the equity one — holdings can't be read without "
                        "it")
        return [], f"could not read mutual funds: {e}"


def _norm_mf(h: dict) -> dict:
    """One fund, in the same shape as an equity row where that makes sense.

    Units are fractional and stay float: MF allotments run to three decimals,
    and int() here would quietly write off part of a holding.

    There is no instrument token, so no candles, so none of the annotations
    apply — no chandelier stop, no signal stance, no results date. A fund is
    not a company and the page should not imply the same tooling reaches it.
    """
    units = _num(h.get("quantity"))
    avg = _num(h.get("average_price"))
    nav = _num(h.get("last_price"))
    value, cost = units * nav, units * avg
    return {
        "symbol": (h.get("tradingsymbol") or "").upper(),   # the ISIN
        "name": h.get("fund") or h.get("tradingsymbol") or "",
        "folio": h.get("folio"),
        "units": round(units, 3),
        "avg_price": round(avg, 4),
        "nav": round(nav, 4),
        # NAVs are struck once a day, so this is the date the number is FOR.
        # Printed next to live equity prices without it, a three-day-old NAV
        # looks like a quote.
        "nav_date": h.get("last_price_date"),
        "value": round(value, 2),
        "cost": round(cost, 2),
        "pnl": round(value - cost, 2),
        "pnl_pct": round((nav / avg - 1) * 100, 2) if avg else None,
        "pledged": _num(h.get("pledged_quantity")) or None,
        "kind": "mf",
    }


# ------------------------------------------------------------------ build
def snapshot(kite=None, annotate_rows: bool = True) -> dict:
    """The whole picture: holdings, positions, totals, allocation, warnings.

    Raises PortfolioError if the broker can't be reached — callers decide
    whether that's a 401, a phone message or a skipped section.
    """
    # get_kite() raises SystemExit without a session. Outside this try it
    # escaped the route's `except PortfolioError` and stopped the server.
    try:
        kite = kite or kite_auth.get_kite()
    except SystemExit as e:
        raise PortfolioError(str(e)) from e

    holdings = [_norm_holding(h) for h in raw_holdings(kite)]
    holdings = [h for h in holdings if h["qty"]]
    positions = [_norm_position(p) for p in raw_positions(kite)]
    positions = [p for p in positions if p["qty"]]

    if annotate_rows:
        holdings = [annotate(h, kite) for h in holdings]
        positions = [annotate(p, kite) for p in positions]

    mf_rows, mf_note = raw_mf_holdings(kite)
    mf = [_norm_mf(m) for m in mf_rows]
    mf = [m for m in mf if m["units"]]
    mf.sort(key=lambda m: -m["value"])
    mf_value = sum(m["value"] for m in mf)
    mf_cost = sum(m["cost"] for m in mf)

    total_value = sum(h["value"] for h in holdings)
    total_cost = sum(h["cost"] for h in holdings)
    for h in holdings:
        h["weight_pct"] = (round(h["value"] / total_value * 100, 2)
                           if total_value else None)

    holdings.sort(key=lambda h: -h["value"])
    positions.sort(key=lambda p: -abs(p["value"]))

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "holdings": holdings,
        "positions": positions,
        # Funds are kept out of the equity numbers on purpose. Folding them in
        # would move every holding's weight_pct and quietly defuse the
        # concentration flag — a 30% position stops looking like one the
        # moment an index fund is added to the denominator. They're a separate
        # total, with a combined figure alongside for when you want the whole
        # picture.
        "mf": mf,
        "mf_note": mf_note,
        "mf_totals": {
            "value": round(mf_value, 2),
            "cost": round(mf_cost, 2),
            "pnl": round(mf_value - mf_cost, 2),
            "pnl_pct": (round((mf_value / mf_cost - 1) * 100, 2)
                        if mf_cost else None),
            "count": len(mf),
        },
        "totals": {
            "value": round(total_value, 2),
            "cost": round(total_cost, 2),
            "pnl": round(total_value - total_cost, 2),
            "pnl_pct": (round((total_value / total_cost - 1) * 100, 2)
                        if total_cost else None),
            "day_pnl": round(sum(
                h["value"] * (h["day_change_pct"] or 0) / 100
                for h in holdings), 2),
            "positions_pnl": round(sum(p["pnl"] for p in positions), 2),
            "count": len(holdings),
            "net_worth": round(total_value + mf_value, 2),
        },
        "flags": flags(holdings),
    }


def flags(holdings: list[dict]) -> list[str]:
    """Things worth noticing before you look at anything else.

    Concentration, price below a chandelier stop, results imminent, and a
    valuation that has turned against a holding. Each is a prompt to look, not
    a recommendation to act.
    """
    out = []
    for h in holdings:
        w = h.get("weight_pct")
        if w and w >= 25:
            out.append(f"{h['symbol']} is {w:g}% of the portfolio")
        ch = h.get("chandelier")
        if ch and ch["distance_pct"] < 0:
            out.append(f"{h['symbol']} is below its {CHANDELIER_MULT:g}×ATR "
                       f"stop ({ch['stop']:g})")
        if h.get("results_due"):
            out.append(f"{h['symbol']} reports {h['results_due']}")
        v = h.get("valuation") or {}
        if v.get("verdict") == "overvalued" and (h.get("pnl_pct") or 0) > 0:
            out.append(f"{h['symbol']} is up {h['pnl_pct']:g}% and your models "
                       f"now read overvalued")
    return out


# ------------------------------------------------------------- watchlist
def sync_watchlist(kite=None) -> dict:
    """Add any holding not already on the watchlist.

    Something you own should be something you're watching — signals, events and
    news all iterate over the watchlist, so an unwatched holding is one you get
    no alerts about. Additive only: it never removes, because a watchlist entry
    you added on purpose shouldn't vanish when you sell.
    """
    have = {w["tradingsymbol"].upper() for w in watchlist.items()}
    added, failed = [], []
    for h in raw_holdings(kite):
        sym = (h.get("tradingsymbol") or "").upper()
        if not sym or sym in have:
            continue
        try:
            watchlist.add(sym, h.get("exchange"), autofetch=False)
            added.append(sym)
        except (Exception, SystemExit) as e:
            # watchlist.add raises SystemExit for a symbol missing from the
            # local instrument index — which happens with a stale index, a
            # BSE-only holding, or a recent listing. One unknown symbol must
            # not abort the sync for everything else you own.
            failed.append(f"{sym}: {e}")
    return {"added": added, "failed": failed, "already": len(have)}


# ---------------------------------------------------------------- render
def _verdict_mark(v: dict | None) -> str:
    if not v or not v.get("verdict"):
        return ""
    return {"undervalued": "▼val", "fair": "=val",
            "rich": "▲val", "overvalued": "▲▲val"}.get(v["verdict"], "")


def _short_fund(name: str, width: int = 34) -> str:
    """Fund names are long and end in boilerplate. "Direct Growth" is on every
    line of a Kite portfolio, so on a phone it's width spent saying nothing."""
    out = name
    for noise in (" - Direct Plan", " Direct Plan", " - Growth", " Growth",
                  " - Direct", " Direct", " Option"):
        out = out.replace(noise, "")
    out = " ".join(out.split())
    return out if len(out) <= width else out[:width - 1] + "\u2026"


def as_text(snap: dict, limit: int = 12) -> str:
    """Phone-sized. Sorted by value, so the positions that matter lead."""
    t = snap["totals"]
    lines = [
        f"Portfolio — ₹{t['value']:,.0f}  ({t['pnl']:+,.0f}, "
        f"{t['pnl_pct']:+.1f}%)" if t["pnl_pct"] is not None
        else f"Portfolio — ₹{t['value']:,.0f}",
    ]
    if t["day_pnl"]:
        lines.append(f"today {t['day_pnl']:+,.0f}")
    lines.append("")

    for h in snap["holdings"][:limit]:
        bits = [f"{h['symbol']} {h['qty']}"]
        if h["pnl_pct"] is not None:
            bits.append(f"{h['pnl_pct']:+.1f}%")
        if h.get("weight_pct"):
            bits.append(f"{h['weight_pct']:g}%wt")
        mark = _verdict_mark(h.get("valuation"))
        if mark:
            bits.append(mark)
        sig = h.get("signals")
        if sig:
            bits.append(f"sig:{sig['stance']}")
        ch = h.get("chandelier")
        if ch:
            bits.append(f"stop {ch['stop']:g} ({ch['distance_pct']:+.1f}%)")
        lines.append("  " + " · ".join(bits))
    if len(snap["holdings"]) > limit:
        lines.append(f"  + {len(snap['holdings']) - limit} more")

    if snap["positions"]:
        lines.append("")
        lines.append(f"Positions ({len(snap['positions'])}):")
        for p in snap["positions"][:6]:
            lines.append(f"  {p['symbol']} {p['side']} {abs(p['qty'])} "
                         f"· {p['pnl']:+,.0f}")

    mf = snap.get("mf") or []
    if mf:
        mt = snap["mf_totals"]
        head = f"Mutual funds \u2014 \u20b9{mt['value']:,.0f}"
        if mt["pnl_pct"] is not None:
            head += f" ({mt['pnl']:+,.0f}, {mt['pnl_pct']:+.1f}%)"
        lines.append("")
        lines.append(head)
        for m in mf[:6]:
            bits = [_short_fund(m["name"]), f"\u20b9{m['value']:,.0f}"]
            if m["pnl_pct"] is not None:
                bits.append(f"{m['pnl_pct']:+.1f}%")
            lines.append("  " + " \u00b7 ".join(bits))
        if len(mf) > 6:
            lines.append(f"  + {len(mf) - 6} more")
        if t.get("net_worth"):
            lines.append(f"  combined \u20b9{t['net_worth']:,.0f}")
    elif snap.get("mf_note"):
        # Said once, quietly. Silence here is indistinguishable from owning
        # no funds, and the two want different reactions from you.
        lines.append("")
        lines.append(f"Mutual funds: {snap['mf_note']}")

    if snap["flags"]:
        lines.append("")
        lines.append("Worth a look:")
        lines += [f"  · {f}" for f in snap["flags"][:6]]
    return "\n".join(lines)
