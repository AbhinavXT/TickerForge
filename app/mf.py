"""Mutual funds beyond the units: SIPs, and what the funds actually own.

Two halves, and they differ in one important way — where the data comes from.

**SIPs come from Kite.** `mf_sips()` is a read like any other, so this is
plumbing: what's instructed, how much, and — the part worth surfacing — when
the next instalment lands and whether the money is there.

**Composition does not come from Kite, or from anywhere else automatically.**
This is worth stating plainly rather than discovering later. Kite Connect's
mutual-fund API is `mf_holdings`, `mf_orders`, `mf_sips` and `mf_instruments`;
the last is scheme *metadata* — name, ISIN, NAV, minimum purchase — not a
portfolio. No endpoint anywhere in the API returns the stocks a fund holds.

What exists instead is SEBI's monthly portfolio disclosure: every AMC publishes
a spreadsheet of its schemes' holdings, on its own site, in its own layout.
There is no single feed. So the import here is deliberately a local file
reader, tolerant about layout, and the workflow is one download per fund per
month rather than an integration that would quietly rot.

That constraint shapes the whole module. **Overlap is computed only from what
you have imported**, every number carries the disclosure month it came from,
and a fund with no imported portfolio is reported as *missing* rather than as
*no overlap* — those two look identical in a result table and mean opposite
things.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta

_ONE_DAY = timedelta(days=1)

from . import db, instruments, kite_auth

# Column headers vary by AMC but the vocabulary doesn't much. Matched
# loosely — lowercase substring — because "Market Value (Rs. in Lakhs)" and
# "Market/Fair Value (Rs. in Lacs)" are the same column with different
# punctuation, and hardcoding either is a parser that works for one fund house.
_HEADERS = {
    "name": ("name of the instrument", "name of instrument", "instrument",
             "company name", "security name", "name"),
    "isin": ("isin",),
    "quantity": ("quantity", "no. of shares", "units", "no of shares"),
    "market_value": ("market value", "market/fair value", "fair value",
                     "amount"),
    "pct_to_nav": ("% to nav", "% of nav", "percentage to nav", "% to net",
                   "weightage", "% of net assets"),
}

# Rows that are not companies. A disclosure sheet is mostly headings, and a
# subtotal counted as a holding would show up as the fund's largest position.
_NON_HOLDINGS = re.compile(
    r"^\s*(?:"
    # Subtotals are anchored to end-of-cell: a bare \b would also drop
    # TotalEnergies, which is a company someone's fund may well hold.
    r"(?:sub\s*|grand\s*)?total\s*$|"
    r"(?:net|other)\s*(?:current\s*)?assets?\b|"
    r"cash\b|tri?[\s-]*party\b|treps\b|reverse\s*repo\b|"
    r"clearing\s*corporation\b|"
    r"listed\s*/|unlisted\b|awaiting\s*listing\b|"
    r"equity\s*(?:&|and)\s*equity\b|"
    r"debt\s+instruments?\b|money\s*market\b|"
    r"derivatives?\b|margin\s*(?:deposit|money)?\s*$|"
    r"notes?\s*:|portfolio\s*statement\b|scheme\s*name\b"
    r")", re.I)


class MFError(RuntimeError):
    """Something the caller should show as a sentence, not a stack trace."""


# ------------------------------------------------------------------- SIPs
def raw_sips(kite=None) -> tuple[list[dict], str | None]:
    """Instructed SIPs. -> (rows, reason it's empty).

    Same shape as portfolio.raw_mf_holdings, and for the same reason: the
    mutual-fund API is a separate Kite subscription, so an empty list means
    "none set up" for some accounts and "not entitled" for others. Reporting
    both as silence would be a lie of omission on a page about money leaving
    your account.
    """
    try:
        k = kite or kite_auth.get_kite()
        return k.mf_sips() or [], None
    except SystemExit as e:
        return [], str(e)
    except Exception as e:                           # noqa: BLE001
        text = str(e).lower()
        if "permission" in text or "403" in text or "subscribe" in text:
            return [], ("Kite's mutual-fund API is a separate subscription "
                        "from the equity one")
        return [], f"could not read SIPs: {e}"


def _next_instalment(sip: dict, today: date | None = None) -> tuple[str | None,
                                                                    int | None]:
    """(next date, days away) for a monthly/weekly/quarterly instruction.

    Kite reports `next_instalment` on most SIPs; where it doesn't, this
    reconstructs it from the day-of-month. The reconstruction is the fiddly
    bit: a SIP set for the 31st doesn't skip February, it lands on the last
    day, and rolling it forward to March would tell you the money leaves a
    month later than it does.
    """
    today = today or date.today()
    given = sip.get("next_instalment")
    if given:
        try:
            nxt = datetime.fromisoformat(str(given)[:10]).date()
            return nxt.isoformat(), (nxt - today).days
        except ValueError:
            pass

    day = sip.get("instalment_day")
    try:
        day = int(day)
    except (TypeError, ValueError):
        return None, None

    def clamp(y: int, m: int) -> date:
        # Last day of the month, so the 31st doesn't roll into the next one.
        nxt_month = date(y + (m == 12), (m % 12) + 1, 1)
        last = (nxt_month - _ONE_DAY).day
        return date(y, m, min(day, last))

    candidate = clamp(today.year, today.month)
    if candidate < today:
        y, m = (today.year + (today.month == 12), (today.month % 12) + 1)
        candidate = clamp(y, m)
    return candidate.isoformat(), (candidate - today).days


def sips(kite=None, today: date | None = None) -> dict:
    """Every SIP, soonest first, with what it costs a month."""
    rows, note = raw_sips(kite)
    out = []
    for s in rows:
        nxt, days = _next_instalment(s, today)
        amount = _num(s.get("instalment_amount"))
        freq = (s.get("frequency") or "monthly").lower()
        out.append({
            "sip_id": s.get("sip_id"),
            "fund": s.get("fund") or s.get("tradingsymbol") or "",
            "isin": (s.get("tradingsymbol") or "").upper(),
            "amount": round(amount, 2),
            "frequency": freq,
            "day": s.get("instalment_day"),
            "next_date": nxt,
            "days_away": days,
            "completed": s.get("completed_instalments"),
            "pending": s.get("pending_instalments"),
            "status": (s.get("status") or "").upper(),
            "monthly_cost": round(amount * _PER_MONTH.get(freq, 1.0), 2),
        })
    # Unknown next dates last: a SIP whose date couldn't be worked out is not
    # a SIP that's due today.
    out.sort(key=lambda s: (s["days_away"] is None, s["days_away"] or 0))
    active = [s for s in out if s["status"] != "CANCELLED"]
    return {
        "sips": out,
        "note": note,
        "monthly_total": round(sum(s["monthly_cost"] for s in active), 2),
        "active": len(active),
    }


# How many instalments a frequency produces per month, for the running cost.
_PER_MONTH = {"weekly": 52 / 12, "monthly": 1.0, "quarterly": 1 / 3,
              "fortnightly": 26 / 12, "daily": 30.0}


def _num(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def due_within(days: int = 7, kite=None, today: date | None = None) -> list:
    """SIPs debiting in the next N days — for the pre-market brief."""
    return [s for s in sips(kite, today)["sips"]
            if s["days_away"] is not None and 0 <= s["days_away"] <= days
            and s["status"] != "CANCELLED"]


# ------------------------------------------------- scheme composition
def _header_map(row: list[str]) -> dict:
    """Which column is which, or {} if this row isn't a header."""
    found = {}
    for i, cell in enumerate(row):
        text = re.sub(r"\s+", " ", str(cell or "")).strip().lower()
        if not text:
            continue
        for field, needles in _HEADERS.items():
            if field in found:
                continue
            if any(n in text for n in needles):
                found[field] = i
                break
    # A header row has to identify the instrument AND something quantitative,
    # or a stray sentence containing the word "name" becomes the header.
    if "name" in found and ({"pct_to_nav", "market_value", "quantity"} & set(found)):
        return found
    return {}


def parse_portfolio(text: str) -> list[dict]:
    """Rows of a monthly disclosure, from CSV text.

    Tolerant on purpose. These files are laid out for a PDF, so the header is
    rarely on line 1, columns are named differently by every AMC, and the body
    is interrupted by section headings and subtotals. Anything that isn't a
    company is dropped rather than imported as a very large holding.
    """
    reader = csv.reader(io.StringIO(text))
    cols, out = {}, []
    for row in reader:
        if not any(str(c).strip() for c in row):
            continue
        if not cols:
            cols = _header_map(row)
            continue
        def cell(field):
            i = cols.get(field)
            return row[i] if i is not None and i < len(row) else None

        name = re.sub(r"\s+", " ", str(cell("name") or "")).strip()
        if not name or _NON_HOLDINGS.match(name):
            continue
        isin = (str(cell("isin") or "").strip().upper() or None)
        pct = _num(cell("pct_to_nav"), None)
        mv = _num(cell("market_value"), None)
        if pct is None and mv is None:
            continue                      # a heading that survived the regex
        out.append({
            "name": name,
            "isin": isin if isin and isin.startswith("IN") else None,
            "pct_to_nav": pct,
            "market_value": mv,
            "quantity": _num(cell("quantity"), None),
        })
    return out


def import_portfolio(scheme: str, text: str, as_of: str | None = None) -> dict:
    """Store one fund's disclosed holdings for one month.

    `as_of` is the disclosure month, not today. Every overlap number carries
    it, because two funds compared across different months is a comparison of
    two different portfolios presented as one fact.
    """
    if not scheme.strip():
        raise MFError("the fund needs a name to file this under")
    as_of = (as_of or date.today().strftime("%Y-%m"))[:7]
    if not re.fullmatch(r"\d{4}-\d{2}", as_of):
        raise MFError(f"as_of should look like 2026-07, not {as_of!r}")

    rows = parse_portfolio(text)
    if not rows:
        raise MFError(
            "no holdings found — the header row wasn't recognised. It needs a "
            "name column and one of: % to NAV, market value, quantity.")

    resolved = 0
    with db.connect() as conn:
        conn.execute("DELETE FROM mf_scheme_holdings WHERE scheme=? AND as_of=?",
                     [scheme, as_of])
        for r in rows:
            symbol = _resolve_symbol(r["name"])
            resolved += bool(symbol)
            conn.execute(
                "INSERT OR REPLACE INTO mf_scheme_holdings "
                "(scheme, as_of, isin, name, symbol, pct_to_nav, "
                " market_value, quantity) VALUES (?,?,?,?,?,?,?,?)",
                [scheme, as_of, r["isin"], r["name"], symbol,
                 r["pct_to_nav"], r["market_value"], r["quantity"]])
    return {"scheme": scheme, "as_of": as_of, "rows": len(rows),
            "resolved": resolved}


_SUFFIXES = re.compile(
    r"\s*(limited|ltd\.?|ltd|corporation|corp\.?|company|co\.?|"
    r"\(india\)|india|inc\.?|plc)\.?\s*$", re.I)


def _clean_name(name: str) -> str:
    prev = None
    out = re.sub(r"[^\w\s&().-]", " ", name)
    while out != prev:                    # "X Company Limited" -> "X"
        prev = out
        out = _SUFFIXES.sub("", out).strip()
    return re.sub(r"\s+", " ", out).strip().lower()


def _resolve_symbol(name: str) -> str | None:
    """Company name -> NSE symbol, or None.

    Best effort and deliberately conservative: an ambiguous match returns
    nothing. A wrong symbol here silently attributes one company's fund
    weight to another, which is worse than an unresolved row you can see.
    """
    want = _clean_name(name)
    if not want:
        return None
    try:
        rows = instruments.search(want, limit=8) or []
    except Exception:                                # noqa: BLE001
        return None
    exact = [r for r in rows
             if _clean_name(r.get("name") or "") == want
             and (r.get("exchange") or "NSE") == "NSE"]
    if len(exact) == 1:
        return (exact[0].get("tradingsymbol") or "").upper() or None
    return None


def schemes() -> list[dict]:
    """What's been imported, and for when."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT scheme, as_of, COUNT(*) n, "
            "       SUM(CASE WHEN symbol IS NOT NULL THEN 1 ELSE 0 END) matched "
            "FROM mf_scheme_holdings GROUP BY scheme, as_of "
            "ORDER BY scheme, as_of DESC").fetchall()
    return [dict(r) for r in rows]


def holdings_of(scheme: str, as_of: str | None = None) -> list[dict]:
    """One scheme's holdings, latest disclosure unless asked otherwise."""
    with db.connect() as conn:
        if as_of is None:
            got = conn.execute(
                "SELECT MAX(as_of) FROM mf_scheme_holdings WHERE scheme=?",
                [scheme]).fetchone()
            as_of = got[0] if got else None
        if not as_of:
            return []
        rows = conn.execute(
            "SELECT * FROM mf_scheme_holdings WHERE scheme=? AND as_of=? "
            "ORDER BY pct_to_nav DESC NULLS LAST", [scheme, as_of]).fetchall()
    return [dict(r) for r in rows]


def _key(row: dict) -> str:
    """What two funds are joined on.

    ISIN where both sides have one, because it's the only identifier that
    survives "Reliance Industries Ltd" versus "Reliance Industries Limited".
    Cleaned name otherwise — imperfect, but the alternative is treating the
    same company as two and reporting an overlap of zero.
    """
    return (row.get("isin") or "").upper() or _clean_name(row.get("name") or "")


def overlap(a: str, b: str, as_of: str | None = None) -> dict:
    """How much two funds are the same fund.

    Reports both the count of shared names and the weight they carry, because
    those answer different questions: twenty shared small positions is a
    different problem from three shared positions at 8% each.

    The headline number is the **minimum-weight sum** — for each shared
    company, the smaller of its two weights. That's the standard measure and
    the defensible one: it's the portion of each fund you'd be buying twice.
    Averaging the two weights instead inflates it whenever the funds hold
    very different amounts of the same name.
    """
    ha, hb = holdings_of(a, as_of), holdings_of(b, as_of)
    missing = [n for n, h in ((a, ha), (b, hb)) if not h]
    if missing:
        # Not "no overlap". Those look identical in a table and mean opposite
        # things, and the difference is a download away.
        return {"a": a, "b": b, "missing": missing, "shared": [],
                "overlap_pct": None, "shared_count": 0,
                "note": f"no imported portfolio for: {', '.join(missing)}"}

    by_a = {_key(r): r for r in ha if _key(r)}
    by_b = {_key(r): r for r in hb if _key(r)}
    shared = []
    for k in set(by_a) & set(by_b):
        wa = by_a[k].get("pct_to_nav") or 0.0
        wb = by_b[k].get("pct_to_nav") or 0.0
        shared.append({
            "name": by_a[k]["name"],
            "symbol": by_a[k].get("symbol") or by_b[k].get("symbol"),
            "isin": by_a[k].get("isin") or by_b[k].get("isin"),
            "pct_a": round(wa, 2), "pct_b": round(wb, 2),
            "common_pct": round(min(wa, wb), 2),
        })
    shared.sort(key=lambda s: -s["common_pct"])
    months = {r["as_of"] for r in ha} | {r["as_of"] for r in hb}
    return {
        "a": a, "b": b,
        "as_of": sorted(months),
        "shared": shared,
        "shared_count": len(shared),
        "overlap_pct": round(sum(s["common_pct"] for s in shared), 2),
        "a_count": len(by_a), "b_count": len(by_b),
        "missing": [],
        # Two funds compared across different disclosure months is a
        # comparison of two different portfolios presented as one fact.
        "note": ("disclosures are from different months: "
                 + ", ".join(sorted(months))) if len(months) > 1 else None,
    }


def look_through(kite=None, as_of: str | None = None) -> dict:
    """Your true exposure to each company, funds plus direct holdings.

    The question a fund investor can't otherwise answer: you own an index
    fund, a flexi-cap fund and some shares directly, and the honest position
    size in any one company is the sum of all three. A 4% direct holding that
    is also 8% of a fund worth as much again is a 12% position that no screen
    on this platform would have flagged.

    Only funds with an imported portfolio contribute; the rest are listed as
    unmapped, with their value, so you can see how much of the total this is
    blind to.
    """
    from . import portfolio as pf

    snap = pf.snapshot(kite, annotate_rows=False)
    exposure: dict[str, dict] = {}

    for h in snap["holdings"]:
        e = exposure.setdefault(h["symbol"], {
            "symbol": h["symbol"], "name": h["name"],
            "direct": 0.0, "via_funds": 0.0, "funds": []})
        e["direct"] += h["value"]

    unmapped = []
    for m in snap.get("mf") or []:
        rows = holdings_of(m["name"], as_of)
        if not rows:
            unmapped.append({"fund": m["name"], "value": m["value"]})
            continue
        for r in rows:
            sym = r.get("symbol")
            pct = r.get("pct_to_nav")
            if not sym or not pct:
                continue
            value = m["value"] * pct / 100.0
            e = exposure.setdefault(sym, {
                "symbol": sym, "name": r["name"],
                "direct": 0.0, "via_funds": 0.0, "funds": []})
            e["via_funds"] += value
            e["funds"].append({"fund": m["name"], "value": round(value, 2)})

    rows = []
    for e in exposure.values():
        total = e["direct"] + e["via_funds"]
        rows.append({**e, "direct": round(e["direct"], 2),
                     "via_funds": round(e["via_funds"], 2),
                     "total": round(total, 2)})
    grand = sum(r["total"] for r in rows)
    for r in rows:
        r["pct"] = round(r["total"] / grand * 100, 2) if grand else None
    rows.sort(key=lambda r: -r["total"])

    return {
        "exposure": rows,
        "total": round(grand, 2),
        "unmapped_funds": unmapped,
        "unmapped_value": round(sum(u["value"] for u in unmapped), 2),
        "note": (f"{len(unmapped)} fund(s) have no imported portfolio — "
                 f"₹{sum(u['value'] for u in unmapped):,.0f} not looked "
                 f"through") if unmapped else None,
    }


# ------------------------------------------------------------------ output
def sips_text(data: dict, limit: int = 10) -> str:
    rows = data["sips"]
    if not rows:
        return f"No SIPs.{(' ' + data['note']) if data.get('note') else ''}"
    lines = [f"SIPs — ₹{data['monthly_total']:,.0f}/month across "
             f"{data['active']}", ""]
    for s in rows[:limit]:
        when = (f"in {s['days_away']}d" if s["days_away"] is not None
                else "date unknown")
        if s["days_away"] == 0:
            when = "today"
        lines.append(f"  {_short(s['fund'])} · ₹{s['amount']:,.0f} · {when}")
    if len(rows) > limit:
        lines.append(f"  + {len(rows) - limit} more")
    return "\n".join(lines)


def overlap_text(o: dict) -> str:
    if o.get("missing"):
        return (f"{o['a']} vs {o['b']}: {o['note']}\n"
                f"Import one with `tf mf import`.")
    lines = [f"{_short(o['a'])} vs {_short(o['b'])}",
             f"  {o['overlap_pct']:.1f}% overlap · {o['shared_count']} shared "
             f"of {o['a_count']}/{o['b_count']}"]
    for s in o["shared"][:8]:
        lines.append(f"  {_short(s['name'], 26)} {s['pct_a']:.1f}/"
                     f"{s['pct_b']:.1f} → {s['common_pct']:.1f}")
    if o.get("note"):
        lines.append(f"  ({o['note']})")
    return "\n".join(lines)


def _short(name: str, width: int = 30) -> str:
    out = name
    for noise in (" - Direct Plan", " Direct Plan", " - Growth", " Growth",
                  " - Direct", " Direct", " Option", " Fund"):
        out = out.replace(noise, "")
    out = " ".join(out.split())
    return out if len(out) <= width else out[:width - 1] + "…"
