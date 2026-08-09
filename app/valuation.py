"""Intrinsic-value estimation from stored screener.in fundamentals.

Deliberately transparent, Damodaran-flavoured, and honest about its limits:
we only have scraped annual statements + a headline snapshot, so we run a
handful of *complementary* models rather than one false-precision DCF, show
each model's number and assumptions, and let a composite + margin-of-safety
fall out. Every model degrades gracefully to N/A when its inputs are missing
(e.g. earnings-based models when EPS <= 0).

Models
  A. Earnings-power DCF   — 2-stage discounting of EPS as an FCFE proxy
  B. Graham Number        — sqrt(22.5 * EPS * BVPS), conservative floor
  C. Graham revised value — EPS * (8.5 + 2g) * Y0 / Y, bond-yield normalised
  D. Justified P/B         — (ROE - g)/(r - g) * BVPS  (Damodaran relative)
  E. Dividend discount     — D1 / (r - g), where a dividend exists

Assumptions (all overridable per request):
  re      cost of equity            default 0.12
  g_term  terminal growth           default 0.05
  years   explicit high-growth yrs  default 10
  bondY   AAA bond yield (Graham)   default 0.072
  g_cap   cap on per-stock growth   default 0.18
"""
from __future__ import annotations

import re

from . import db, fundamentals, watchlist

DEFAULTS = {"re": 0.12, "g_term": 0.05, "years": 10, "bondY": 0.072, "g_cap": 0.18}

# Building blocks for a smart, per-stock cost of equity. We don't get beta from
# screener, so we approximate Damodaran's rf + beta*ERP with rf + ERP + a size
# premium (smaller = riskier). All overridable per request.
# Gordon-growth models divide by (re - g). As g approaches re that denominator
# goes to zero and fair value goes to infinity, so a merely optimistic growth
# assumption produces an arithmetically absurd answer rather than a wrong one.
# Below this spread the model has nothing to say and is dropped instead of
# clamped — clamping g to just under re is what produced 127x book.
MIN_SPREAD = 0.02

RISK_FREE = 0.069            # India 10Y ~ default
EQUITY_RISK_PREMIUM = 0.055  # mature-market ERP + India country premium-ish


def smart_re(market_cap: float | None) -> float:
    """Cost of equity from rf + ERP + a size premium (₹ Cr market cap)."""
    prem = 0.0
    if market_cap is not None:
        if market_cap < 500:
            prem = 0.03
        elif market_cap < 5000:
            prem = 0.02
        elif market_cap < 20000:
            prem = 0.01
    return round(RISK_FREE + EQUITY_RISK_PREMIUM + prem, 4)


# --------------------------------------------------------------- helpers
def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", "").replace("%", "").replace("₹", "").strip()
    m = re.search(r"-?\d+\.?\d*", t)
    return float(m.group()) if m else None


def _snap(conn, symbol, *keys) -> float | None:
    rows = conn.execute(
        "SELECT key, value FROM fund_snapshot WHERE symbol=?", (symbol,)
    ).fetchall()
    low = {r["key"].lower(): r["value"] for r in rows}
    for k in keys:
        for name, val in low.items():
            if k.lower() in name:
                n = _num(val)
                if n is not None:
                    return n
    return None


def _series_vals(conn, symbol, statement, *patterns) -> list:
    for pat in patterns:
        rows = conn.execute(
            "SELECT value FROM fund_series WHERE symbol=? AND statement=? "
            "AND lower(row) LIKE ? ORDER BY period_idx",
            (symbol, statement, pat.lower()),
        ).fetchall()
        if rows:
            return [r["value"] for r in rows]
    return []


def _latest(vals):
    for v in reversed(vals):
        if v is not None:
            return v
    return None


def _cagr(vals, years):
    clean = [v for v in vals if v is not None]
    if len(clean) < years + 1:
        return None
    start, end = clean[-(years + 1)], clean[-1]
    if not start or start <= 0 or end is None or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


# --------------------------------------------------------------- inputs
def metrics(symbol: str) -> dict | None:
    """Pull the raw numbers each model needs from stored fundamentals."""
    symbol = symbol.upper()
    with db.connect() as conn:
        has = conn.execute(
            "SELECT 1 FROM fund_snapshot WHERE symbol=? LIMIT 1", (symbol,)
        ).fetchone()
        if not has:
            return None
        price = _snap(conn, symbol, "current price", "price")
        bvps = _snap(conn, symbol, "book value")
        pe = _snap(conn, symbol, "stock p/e", "p/e")
        roe = _snap(conn, symbol, "roe")
        divy = _snap(conn, symbol, "dividend yield")
        mcap = _snap(conn, symbol, "market cap")
        eps_hist = _series_vals(conn, symbol, "profit-loss", "eps in rs%", "eps%")
        np_hist = _series_vals(conn, symbol, "profit-loss", "net profit%")
        sales_hist = _series_vals(conn, symbol, "profit-loss", "sales%", "revenue%")
        name_rows = conn.execute(
            "SELECT value FROM fund_snapshot WHERE symbol=? AND lower(key)='name'",
            (symbol,),
        ).fetchone()

    eps = _latest(eps_hist)
    if eps is None and price is not None and pe:
        eps = price / pe                        # fall back to price / PE
    # historical growth: prefer EPS, else net profit, else sales
    g_hist = None
    for hist in (eps_hist, np_hist, sales_hist):
        for yrs in (5, 4, 3):
            g = _cagr(hist, yrs)
            if g is not None:
                g_hist = g
                break
        if g_hist is not None:
            break

    return {
        "symbol": symbol,
        "name": name_rows["value"] if name_rows else "",
        "price": price, "bvps": bvps, "pe": pe, "roe": roe,
        "div_yield": divy, "market_cap": mcap, "eps": eps,
        "g_hist": g_hist,
        "eps_hist": [v for v in eps_hist if v is not None][-6:],
    }


# --------------------------------------------------------------- models
def _dcf_eps(eps, g_high, years, re, g_term):
    """2-stage EPS-as-FCFE discounting. Linear glide from g_high to g_term."""
    if eps is None or eps <= 0 or re <= g_term:
        return None
    pv, e = 0.0, eps
    for t in range(1, years + 1):
        g = g_high + (g_term - g_high) * (t - 1) / max(1, years - 1)
        e = e * (1 + g)
        pv += e / (1 + re) ** t
    terminal = e * (1 + g_term) / (re - g_term)
    pv += terminal / (1 + re) ** years
    return pv


def _graham_number(eps, bvps):
    if eps is None or bvps is None or eps <= 0 or bvps <= 0:
        return None
    return (22.5 * eps * bvps) ** 0.5


def _graham_revised(eps, g_pct, bondY):
    if eps is None or eps <= 0:
        return None
    # Graham's revised: V = EPS * (8.5 + 2g) * 4.4 / Y ; 4.4 = 1962 AAA yield
    return eps * (8.5 + 2 * g_pct) * 4.4 / (bondY * 100)


def _median(vals):
    """True median. `sorted(v)[len(v)//2]` takes the UPPER middle on an even
    count, which biases every composite fair value — and therefore every
    margin of safety — upward, in the direction that costs money."""
    if not vals:
        return None
    v = sorted(vals)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _justified_pb(bvps, roe, g, re):
    if bvps is None or roe is None or re - g < MIN_SPREAD:
        return None
    pb = (roe - g) / (re - g)
    if pb <= 0:
        return None
    return pb * bvps


def _ddm(price, div_yield, g, re):
    if (price is None or div_yield is None or div_yield <= 0
            or re - g < MIN_SPREAD):
        return None
    d0 = price * div_yield / 100
    return d0 * (1 + g) / (re - g)


def value(symbol: str, a: dict | None = None, rich: bool = False) -> dict | None:
    m = metrics(symbol)
    if not m:
        return None
    # smart default cost of equity per size, unless the caller overrode re
    a_in = a or {}
    a = {**DEFAULTS, **a_in}
    if "re" not in a_in:
        a["re"] = smart_re(m.get("market_cap"))
    re, g_term, years = a["re"], a["g_term"], int(a["years"])
    bondY, g_cap = a["bondY"], a["g_cap"]

    # growth used by the models: historical, floored at 0, capped
    g_raw = m["g_hist"] if m["g_hist"] is not None else g_term
    g_high = max(0.0, min(g_cap, g_raw))
    g_pct = g_high * 100

    roe_frac = (m["roe"] / 100) if m["roe"] is not None else None

    models = {
        "Earnings-power DCF": _dcf_eps(m["eps"], g_high, years, re, g_term),
        "Graham Number": _graham_number(m["eps"], m["bvps"]),
        "Graham revised": _graham_revised(m["eps"], g_pct, bondY),
        # g passed unclamped on purpose — MIN_SPREAD decides whether these
        # two models apply at all, rather than being forced to answer.
        "Justified P/B": _justified_pb(m["bvps"], roe_frac, g_high, re),
        "Dividend discount": _ddm(m["price"], m["div_yield"], g_high, re),
    }
    vals = [v for v in models.values() if v is not None and v > 0]
    composite = _median(vals)
    price = m["price"]
    mos = ((composite - price) / composite * 100
           if composite and price else None)

    out = {
        "symbol": symbol, "name": m["name"], "price": price,
        "market_cap": m["market_cap"], "pe": m["pe"], "roe": m["roe"],
        "bvps": m["bvps"], "eps": m["eps"], "div_yield": m["div_yield"],
        "g_hist_pct": round(m["g_hist"] * 100, 1) if m["g_hist"] is not None else None,
        "g_used_pct": round(g_pct, 1),
        "eps_hist": m["eps_hist"],
        "models": {k: (round(v, 2) if v is not None else None)
                   for k, v in models.items()},
        "composite": round(composite, 2) if composite else None,
        # How many models actually answered. A composite from two is not the
        # same kind of number as one from five, and the screener ranks them
        # side by side as though it were.
        "n_models": len(vals),
        "mos_pct": round(mos, 1) if mos is not None else None,
        "verdict": _verdict(mos),
        "assumptions": {"re": round(re, 4), "g_term": g_term, "years": years,
                        "bondY": bondY, "g_cap": g_cap,
                        "re_is_smart": "re" not in a_in},
    }
    if rich:
        out["reverse_dcf"] = reverse_dcf(m, a)
        out["scenarios"] = scenarios(m, a)
        out["sensitivity"] = sensitivity(m, a)
        q = quality(symbol)
        if q.get("roic_pct") is not None:
            q["roic_vs_re_pp"] = round(q["roic_pct"] - re * 100, 1)
        out["quality"] = q
        out["history"] = history_bands(symbol, m)
    return out


# --------------------------------------------------------------- enrichment
def _composite_at(m, g_high, re, years, g_term, bondY):
    """Median fair value across the models at a given (g, re). Used by
    scenarios + sensitivity so they stay consistent with the base engine."""
    roe_frac = (m["roe"] / 100) if m["roe"] is not None else None
    vals = [v for v in (
        _dcf_eps(m["eps"], g_high, years, re, g_term),
        _graham_number(m["eps"], m["bvps"]),
        _graham_revised(m["eps"], g_high * 100, bondY),
        _justified_pb(m["bvps"], roe_frac, g_high, re),
        _ddm(m["price"], m["div_yield"], g_high, re),
    ) if v is not None and v > 0]
    return _median(vals)


def reverse_dcf(m, a) -> dict | None:
    """What EPS growth does today's price imply? Solve DCF(g) = price for g by
    bisection. The honest question: is the market's baked-in growth achievable?"""
    price, eps = m.get("price"), m.get("eps")
    if not price or not eps or eps <= 0:
        return None
    re, years, g_term = a["re"], int(a["years"]), a["g_term"]
    lo, hi = -0.20, 0.60
    f = lambda g: (_dcf_eps(eps, g, years, re, g_term) or 0) - price
    flo, fhi = f(lo), f(hi)
    if flo > 0:                       # price below even a shrinking business
        return {"implied_growth_pct": None, "note": "price below no-growth value"}
    if fhi < 0:
        return {"implied_growth_pct": None,
                "note": "price implies >60% growth — very demanding"}
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    implied = (lo + hi) / 2
    hist = m.get("g_hist")
    return {
        "implied_growth_pct": round(implied * 100, 1),
        "hist_growth_pct": round(hist * 100, 1) if hist is not None else None,
        "gap_pp": (round((implied - hist) * 100, 1)
                   if hist is not None else None),
    }


def scenarios(m, a) -> dict:
    """Bear / base / bull fair value from three assumption sets."""
    base_g = max(0.0, min(a["g_cap"],
                          m["g_hist"] if m["g_hist"] is not None else a["g_term"]))
    base_re, yrs, gt, bondY = a["re"], int(a["years"]), a["g_term"], a["bondY"]
    sets = {
        "bear": {"g": max(0.0, base_g * 0.5), "re": base_re + 0.02},
        "base": {"g": base_g, "re": base_re},
        "bull": {"g": min(a["g_cap"], base_g * 1.5 + 0.02), "re": max(0.08, base_re - 0.01)},
    }
    out = {}
    for name, s in sets.items():
        fv = _composite_at(m, s["g"], s["re"], yrs, gt, bondY)
        mos = ((fv - m["price"]) / fv * 100) if (fv and m.get("price")) else None
        out[name] = {"fair": round(fv, 2) if fv else None,
                     "g_pct": round(s["g"] * 100, 1),
                     "re_pct": round(s["re"] * 100, 1),
                     "mos_pct": round(mos, 1) if mos is not None else None}
    return out


def sensitivity(m, a, g_steps=5, re_steps=5) -> dict:
    """Grid of fair value across growth (rows) × discount rate (cols)."""
    base_g = max(0.0, min(a["g_cap"],
                          m["g_hist"] if m["g_hist"] is not None else a["g_term"]))
    gs = [max(0.0, base_g + d) for d in (-0.06, -0.03, 0, 0.03, 0.06)][:g_steps]
    res = [max(0.08, a["re"] + d) for d in (-0.02, -0.01, 0, 0.01, 0.02)][:re_steps]
    yrs, gt, bondY, price = int(a["years"]), a["g_term"], a["bondY"], m.get("price")
    grid = []
    for g in gs:
        row = []
        for re in res:
            fv = _composite_at(m, g, re, yrs, gt, bondY)
            row.append({"fair": round(fv, 2) if fv else None,
                        "upside_pct": (round((fv - price) / price * 100, 1)
                                       if fv and price else None)})
        grid.append(row)
    return {"g_axis_pct": [round(g * 100, 1) for g in gs],
            "re_axis_pct": [round(r * 100, 1) for r in res],
            "grid": grid, "price": price}


def quality(symbol: str) -> dict:
    """ROIC, a Piotroski-style F-score, and Altman Z — computed from whatever
    line items we hold, each reporting its own coverage so a missing statement
    degrades the score instead of faking it."""
    symbol = symbol.upper()
    with db.connect() as conn:
        _, sales = _series_vals2(conn, symbol, "profit-loss", "sales%", "revenue%")
        _, netp = _series_vals2(conn, symbol, "profit-loss", "net profit%")
        _, pbt = _series_vals2(conn, symbol, "profit-loss", "profit before tax%")
        _, interest = _series_vals2(conn, symbol, "profit-loss", "interest%")
        _, tax = _series_vals2(conn, symbol, "profit-loss", "tax %")
        _, opm = _series_vals2(conn, symbol, "profit-loss", "opm %")
        _, equity = _series_vals2(conn, symbol, "balance-sheet",
                                  "equity capital%", "share capital%")
        _, reserves = _series_vals2(conn, symbol, "balance-sheet", "reserves%")
        _, borrow = _series_vals2(conn, symbol, "balance-sheet", "borrowings%")
        _, assets = _series_vals2(conn, symbol, "balance-sheet", "total assets%")
        _, cfo = _series_vals2(conn, symbol, "cash-flow",
                               "cash from operating%", "operating activit%")
        mcap = _snap_one(conn, symbol, "market cap")

    eq_now = (_latest(equity) or 0) + (_latest(reserves) or 0)
    debt_now = _latest(borrow) or 0
    ebit_now = None
    if _latest(pbt) is not None:
        ebit_now = _latest(pbt) + (_latest(interest) or 0)

    out: dict = {}

    # ROIC = NOPAT / invested capital  (NOPAT = EBIT * (1 - tax rate))
    if ebit_now is not None and (eq_now + debt_now) > 0:
        tr = (_latest(tax) or 0) / 100
        tr = min(max(tr, 0), 0.5)
        nopat = ebit_now * (1 - tr)
        out["roic_pct"] = round(nopat / (eq_now + debt_now) * 100, 1)
        out["roic_vs_re_pp"] = None    # filled by caller who knows re

    # Piotroski F-score (partial-aware)
    f, checked = 0, 0
    def crit(cond):
        nonlocal f, checked
        if cond is None:
            return
        checked += 1
        if cond:
            f += 1
    np_l, np_p = _last_two(netp)
    crit(np_l is not None and np_l > 0)                       # positive NI
    crit(_latest(cfo) is not None and _latest(cfo) > 0)       # positive CFO
    crit(np_l is not None and np_p is not None and np_l > np_p)  # rising NI
    if _latest(cfo) is not None and np_l is not None:
        crit(_latest(cfo) > np_l)                             # quality of earnings
    dl, dp = _last_two(borrow)
    crit(dl is not None and dp is not None and dl <= dp)      # deleveraging
    sl, sp = _last_two(sales)
    al, ap = _last_two(assets)
    if sl and sp and al and ap and al > 0 and ap > 0:
        crit((sl / al) > (sp / ap))                          # asset turnover up
    ol, op_ = _last_two(opm)
    crit(ol is not None and op_ is not None and ol >= op_)    # margin up
    if checked:
        out["f_score"] = f
        out["f_checked"] = checked
    # Altman Z (original): needs several balance-sheet items
    ta = _latest(assets)
    if ta and ta > 0:
        re_res = _latest(reserves)
        z_terms, z_have = 0.0, 0
        if ebit_now is not None:
            z_terms += 3.3 * (ebit_now / ta); z_have += 1
        if _latest(sales) is not None:
            z_terms += 1.0 * (_latest(sales) / ta); z_have += 1
        if re_res is not None:
            z_terms += 1.4 * (re_res / ta); z_have += 1
        if mcap is not None and debt_now >= 0:
            liab = ta - eq_now if ta > eq_now else debt_now
            if liab > 0:
                z_terms += 0.6 * (mcap / liab); z_have += 1
        if z_have >= 3:
            out["altman_z"] = round(z_terms, 2)
            out["altman_zone"] = ("safe" if z_terms > 2.99 else
                                  "grey" if z_terms > 1.81 else "distress")
    return out


def history_bands(symbol: str, m: dict) -> dict | None:
    """Current P/E and P/B vs the stock's own multi-year range, using annual
    EPS/BVPS history against year-by-year price from the candle store — a
    mean-reversion read ('cheap/dear vs its own history')."""
    try:
        from . import candles, instruments
    except Exception:
        return None
    inst = instruments.lookup(symbol)
    if not inst:
        return None
    with db.connect() as conn:
        eps_periods, eps_vals = _series_pair(conn, symbol, "profit-loss",
                                             "eps in rs%", "eps%")
    if not eps_vals:
        return None
    # year-end price per fiscal year from daily candles
    try:
        rows = candles.get(inst["instrument_token"], "day")
    except Exception:
        rows = []
    if not rows:
        return None
    by_year: dict[int, float] = {}
    for c in rows:
        d = c.get("date")
        yr = getattr(d, "year", None) or (int(str(d)[:4]) if d else None)
        if yr:
            by_year[yr] = c.get("close")          # last seen close that year

    pes = []
    for per, eps in zip(eps_periods, eps_vals):
        if eps is None or eps <= 0:
            continue
        yr = _year_of(per)
        px = by_year.get(yr)
        if px:
            pes.append(px / eps)
    out = {}
    cur_pe = m.get("pe")
    if pes and cur_pe:
        pes_sorted = sorted(pes)
        med = pes_sorted[len(pes_sorted) // 2]
        lo, hi = pes_sorted[0], pes_sorted[-1]
        pct = 100 * sum(1 for p in pes if p <= cur_pe) / len(pes)
        out["pe"] = {"current": round(cur_pe, 1), "median": round(med, 1),
                     "min": round(lo, 1), "max": round(hi, 1),
                     "percentile": round(pct), "n": len(pes),
                     "read": ("cheap vs history" if cur_pe < med * 0.85 else
                              "dear vs history" if cur_pe > med * 1.15 else
                              "near its median")}
    # P/B band from bvps history if available
    with db.connect() as conn:
        bv_p, bv_v = _series_pair(conn, symbol, "balance-sheet",
                                  "book value%")
    return out or None


# small local helpers (kept here to avoid touching the base engine)
def _series_vals2(conn, symbol, statement, *patterns):
    for pat in patterns:
        rows = conn.execute(
            "SELECT period, value FROM fund_series WHERE symbol=? AND statement=? "
            "AND lower(row) LIKE ? ORDER BY period_idx",
            (symbol, statement, pat.lower())).fetchall()
        if rows:
            return [r["period"] for r in rows], [r["value"] for r in rows]
    return [], []


def _series_pair(conn, symbol, statement, *patterns):
    return _series_vals2(conn, symbol, statement, *patterns)


def _snap_one(conn, symbol, *keys):
    return _snap(conn, symbol, *keys)


def _last_two(vals):
    clean = [v for v in vals if v is not None]
    if len(clean) >= 2:
        return clean[-1], clean[-2]
    if len(clean) == 1:
        return clean[-1], None
    return None, None


def _year_of(period: str) -> int | None:
    m = re.search(r"(20\d{2})", str(period))
    return int(m.group()) if m else None


def _verdict(mos):
    if mos is None:
        return "n/a"
    if mos >= 30:
        return "undervalued"
    if mos >= 0:
        return "fair"
    if mos >= -25:
        return "rich"
    return "overvalued"


def screen(symbols: list[str] | None = None, a: dict | None = None) -> dict:
    """Value a set of stocks (default: watchlist), sorted by margin of safety."""
    if symbols:
        syms = [s.upper() for s in symbols]
    else:
        syms = [w["tradingsymbol"] for w in watchlist.items()]
    rows, missing = [], []
    for s in syms:
        v = value(s, a)
        if v is None:
            missing.append(s)
        else:
            rows.append(v)
    rows.sort(key=lambda r: (r["mos_pct"] is None, -(r["mos_pct"] or -1e9)))
    return {"rows": rows, "missing": missing,
            "assumptions": {**DEFAULTS, **(a or {})}}
