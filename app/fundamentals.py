"""Fundamentals: screener.in ingestion + our own ratio computation.

Philosophy: store the RAW statement tables (quarterly results, annual P&L,
balance sheet, screener's ratio history, cash flow), then compute ratios
ourselves from primary numbers. Screener's headline ROCE is also kept, so
you can cross-check their definition against yours — everyone computes
capital employed differently, and owning that choice is the point.

Politeness: one page per fetch, cached in SQLite with fetched_at; refresh
is on-demand or when stale (>7 days). Consolidated first, standalone
fallback. Personal use, gentle pace — a guest, not a crawler.
"""
import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from . import db, watchlist

BASE = "https://www.screener.in"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36")}
SECTIONS = ("quarters", "profit-loss", "balance-sheet", "ratios",
            "cash-flow", "shareholding")
STALE_AFTER = timedelta(days=7)


# ------------------------------------------------------------------- slug
def slug_for(symbol: str) -> str:
    """The screener.in URL slug for a symbol.

    Usually identical to the NSE ticker, but not always — screener keys some
    companies by a different code, and BSE-only names by their numeric scrip
    id. When they differ, every fetch 404s and the stock silently ends up with
    no fundamentals at all. Overrides live in `meta` so a single bad mapping
    is a one-line fix rather than a code change.
    """
    return db.get_meta(f"fund_slug:{symbol.upper()}") or symbol.upper()


def set_slug(symbol: str, slug: str) -> None:
    db.set_meta(f"fund_slug:{symbol.upper()}", slug.strip())


# ------------------------------------------------------------------ fetch
def fetch_html(symbol: str, consolidated: bool = True) -> tuple[str, str]:
    """Return (html, variant). Falls back consolidated -> standalone, and
    retries transient screener throttling (429/5xx) with backoff so a busy
    moment doesn't surface as a dead 502."""
    variants = (["consolidated", "standalone"] if consolidated
                else ["standalone"])
    slug = slug_for(symbol)
    last_err = None
    for v in variants:
        url = (f"{BASE}/company/{slug}/consolidated/" if v == "consolidated"
               else f"{BASE}/company/{slug}/")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
            except requests.RequestException as e:
                last_err = f"network error for {url}: {e}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200 and _looks_like_company(r.text):
                return r.text, v
            if r.status_code == 200:
                # 200 but not the page we expect (redirect/interstitial)
                last_err = f"unexpected page shape at {url}"
                break                       # try next variant, don't hammer
            if r.status_code in (429, 500, 502, 503, 504):
                # transient — back off and retry the same URL
                wait = 2.0 * (attempt + 1)
                last_err = f"HTTP {r.status_code} for {url} (throttled?)"
                time.sleep(wait)
                continue
            # 404 or other hard failure -> next variant
            last_err = f"HTTP {r.status_code} for {url}"
            break
    raise RuntimeError(last_err or f"no screener page for {symbol}")


def _looks_like_company(html: str) -> bool:
    """Accept a screener company page on any of a few stable markers, not just
    one class name that could change."""
    return any(m in html for m in
               ("top-ratios", "company-ratios", "/company/", "Quarterly Results",
                "data-company-id"))


# ------------------------------------------------------------------ parse
def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("%", "").replace("₹", "").strip()
    if t in ("", "-", "--"):
        return None
    m = re.search(r"-?\d+\.?\d*", t)
    return float(m.group()) if m else None


def _classification(soup) -> dict:
    """{"Sector": ..., "Industry": ...} from the peers block, if it's there.

    Screener writes it as one line — "Sector: Consumer Durables Industry:
    Consumer Durables - Domestic Appliances" — with each half linked. Read from
    the links where they exist, falling back to a regex over the text, because
    the two failure modes differ: markup changing around the links is likely,
    the words "Sector" and "Industry" disappearing is not.

    Absent stays absent. A company with no classification must not end up
    grouped under "" with every other unclassified name, which would produce
    an industry median assembled from an industry that doesn't exist.
    """
    section = soup.find("section", id="peers")
    if not section:
        return {}
    text = " ".join(section.get_text(" ", strip=True).split())
    m = re.search(r"Sector\s*:?\s*(.+?)\s+Industry\s*:?\s*([^|]+?)\s*$",
                  text[:400])
    if not m:
        m = re.search(r"Sector\s*:?\s*(.+?)\s+Industry\s*:?\s*(.+)", text[:400])
    if not m:
        return {}
    out = {}
    for key, raw in (("Sector", m.group(1)), ("Industry", m.group(2))):
        value = raw.strip(" :-\u00a0")
        # A whole paragraph means the regex swallowed the page, not a label.
        if value and len(value) <= 80:
            out[key] = value
    return out


def parse(html: str) -> dict:
    """-> {snapshot: {name: text}, tables: {section: {periods, rows}}}"""
    soup = BeautifulSoup(html, "html.parser")
    out = {"snapshot": {}, "tables": {}}

    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        val_el = li.select_one(".value") or li.select_one(".nowrap.value")
        if name_el and val_el:
            out["snapshot"][name_el.get_text(strip=True)] = \
                val_el.get_text(" ", strip=True)

    # Sector and industry live in a line of prose under the peers heading
    # rather than in any table, so they get their own reader. They land in the
    # snapshot dict, which means they store, cache and expire through exactly
    # the same path as "Stock P/E" — no schema change for a new kind of fact.
    for key, value in _classification(soup).items():
        out["snapshot"][key] = value

    for sec_id in SECTIONS:
        sec = soup.find("section", id=sec_id)
        table = sec.find("table") if sec else None
        if not table or not table.find("thead"):
            continue
        periods = [th.get_text(strip=True)
                   for th in table.find("thead").find_all("th")][1:]
        rows = {}
        body = table.find("tbody")
        for tr in (body.find_all("tr") if body else []):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = re.sub(r"[+\s]+$", "",
                           tds[0].get_text(" ", strip=True)).strip()
            if not label:
                continue
            rows[label] = [_num(td.get_text()) for td in tds[1:]]
        if periods and rows:
            out["tables"][sec_id] = {"periods": periods, "rows": rows}
    return out


# ------------------------------------------------------------------ store
def store(symbol: str, parsed: dict, variant: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("DELETE FROM fund_snapshot WHERE symbol=?", (symbol,))
        conn.executemany(
            "INSERT INTO fund_snapshot (symbol, key, value, variant, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(symbol, k, v, variant, now)
             for k, v in parsed["snapshot"].items()],
        )
        conn.execute("DELETE FROM fund_series WHERE symbol=?", (symbol,))
        rows = []
        for sec, t in parsed["tables"].items():
            for label, values in t["rows"].items():
                for idx, (period, val) in enumerate(zip(t["periods"], values)):
                    rows.append((symbol, sec, label, idx, period, val))
        conn.executemany(
            "INSERT INTO fund_series "
            "(symbol, statement, row, period_idx, period, value) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)


def fetch_and_store(symbol: str) -> dict:
    html, variant = fetch_html(symbol)
    parsed = parse(html)
    if not parsed["tables"]:
        raise RuntimeError(f"screener page for {symbol} parsed empty — "
                           f"layout may have changed")
    store(symbol, parsed, variant)
    return summary(symbol)


# ------------------------------------------------------------------ query
def _series(conn, symbol: str, statement: str, *label_patterns: str):
    """Latest-first-matching row of a statement -> (periods, values)."""
    for pat in label_patterns:
        rows = conn.execute(
            "SELECT period, value FROM fund_series "
            "WHERE symbol=? AND statement=? AND lower(row) LIKE ? "
            "ORDER BY period_idx", (symbol, statement, pat.lower()),
        ).fetchall()
        if rows:
            return ([r["period"] for r in rows],
                    [r["value"] for r in rows])
    return [], []


def _latest(vals: list) -> float | None:
    for v in reversed(vals):
        if v is not None:
            return v
    return None


def _cagr(vals: list, years: int) -> float | None:
    clean = [v for v in vals if v is not None]
    if len(clean) < years + 1:
        return None
    start, end = clean[-(years + 1)], clean[-1]
    if not start or start <= 0 or end is None or end <= 0:
        return None
    return round(((end / start) ** (1 / years) - 1) * 100, 1)


def compute(symbol: str) -> dict:
    """Our own ratios from raw statements (annual, latest column, ₹ Cr)."""
    with db.connect() as conn:
        _, sales = _series(conn, symbol, "profit-loss", "sales%", "revenue%")
        _, netp = _series(conn, symbol, "profit-loss", "net profit%")
        _, pbt = _series(conn, symbol, "profit-loss", "profit before tax%")
        _, interest = _series(conn, symbol, "profit-loss", "interest%")
        _, opm = _series(conn, symbol, "profit-loss", "opm%")
        _, equity = _series(conn, symbol, "balance-sheet",
                            "equity capital%", "share capital%")
        _, reserves = _series(conn, symbol, "balance-sheet", "reserves%")
        _, borrow = _series(conn, symbol, "balance-sheet", "borrowings%")
        _, scr_roce = _series(conn, symbol, "ratios", "roce%")

    eq = (_latest(equity) or 0) + (_latest(reserves) or 0)
    debt = _latest(borrow) or 0
    ebit = None
    if _latest(pbt) is not None:
        ebit = _latest(pbt) + (_latest(interest) or 0)

    out = {}
    np_l = _latest(netp)
    if np_l is not None and eq > 0:
        out["ROE %"] = round(np_l / eq * 100, 1)
    if ebit is not None and (eq + debt) > 0:
        out["ROCE % (ours: EBIT / eq+debt)"] = round(ebit / (eq + debt) * 100, 1)
    if _latest(scr_roce) is not None:
        out["ROCE % (screener)"] = _latest(scr_roce)
    if eq > 0:
        out["Debt / Equity"] = round(debt / eq, 2)
    if ebit is not None and _latest(interest):
        out["Interest coverage"] = round(ebit / _latest(interest), 1)
    if _latest(opm) is not None:
        out["OPM % (latest FY)"] = _latest(opm)
    for yrs in (3, 5):
        c = _cagr(sales, yrs)
        if c is not None:
            out[f"Sales CAGR {yrs}y %"] = c
        c = _cagr(netp, yrs)
        if c is not None:
            out[f"Profit CAGR {yrs}y %"] = c
    return out


# --------------------------------------------------------- bulk statement access
# Annual statements, in the order a bare row name is searched. Screener puts
# the same label in more than one table ("ROCE %" is in ratios, "Sales" is in
# both profit-loss and quarters), so the order is the tie-break and is
# deliberately the one a person means: the P&L first.
ANNUAL = ("profit-loss", "balance-sheet", "cash-flow", "ratios")
# Both are reported per quarter, so `q("...")` searches both. Shareholding
# is a separate table only because screener renders it in its own section.
QUARTERLY = ("quarters", "shareholding")

TTM_PERIOD = "TTM"


def row_catalogue() -> dict[str, list[str]]:
    """Every row label actually stored, per statement.

    Read from the data rather than hardcoded, because the labels are
    screener's and they drift — banks report "Revenue" where manufacturers
    report "Sales", and a row that appears next quarter should be queryable
    without a code change.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT statement, row FROM fund_series").fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["statement"], []).append(r["row"])
    for v in out.values():
        v.sort()
    return out


def resolve_row(label: str, statements: tuple[str, ...],
                catalogue: dict | None = None) -> list[tuple[str, str]]:
    """(statement, row) candidates for a human-typed label, best first.

    Exact match wins, then prefix, then substring — so "sales" finds "Sales"
    rather than being beaten to it by "Sales growth", and a label that is
    genuinely ambiguous returns all of its candidates for the caller to
    complain about by name.
    """
    cat = catalogue if catalogue is not None else row_catalogue()
    want = label.strip().lower()
    exact, prefix, sub = [], [], []
    for stmt in statements:
        for row in cat.get(stmt, []):
            low = row.lower()
            if low == want:
                exact.append((stmt, row))
            elif low.startswith(want):
                prefix.append((stmt, row))
            elif want in low:
                sub.append((stmt, row))
    return exact or prefix or sub


def series_for(symbols: list[str],
               rows: list[tuple[str, str]]) -> dict[str, dict]:
    """Statement rows for many symbols in one pass.

    -> {symbol: {(statement, row): (periods, values)}}, oldest period first.

    In bulk because the alternative is a round trip per symbol, and a universe
    screen has two thousand of them. Chunked because SQLite's parameter limit
    is not two thousand.
    """
    out: dict[str, dict] = {s: {} for s in symbols}
    if not symbols or not rows:
        return out

    pairs = list(dict.fromkeys(rows))
    with db.connect() as conn:
        for i in range(0, len(symbols), 400):
            chunk = symbols[i:i + 400]
            clause = " OR ".join(["(statement=? AND row=?)"] * len(pairs))
            params: list = list(chunk)
            for stmt, row in pairs:
                params += [stmt, row]
            got = conn.execute(
                f"SELECT symbol, statement, row, period, value "
                f"FROM fund_series "
                f"WHERE symbol IN ({','.join('?' * len(chunk))}) "
                f"AND ({clause}) ORDER BY symbol, statement, row, period_idx",
                params).fetchall()
            for r in got:
                key = (r["statement"], r["row"])
                bucket = out.setdefault(r["symbol"], {}).setdefault(
                    key, ([], []))
                bucket[0].append(r["period"])
                bucket[1].append(r["value"])
    return out


def classification() -> dict[str, tuple]:
    """{symbol: (sector, industry)} for everything stored."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, key, value FROM fund_snapshot "
            "WHERE key IN ('Sector', 'Industry')").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["symbol"], {})[r["key"]] = r["value"]
    return {s: (d.get("Sector"), d.get("Industry")) for s, d in out.items()}


def group_pe() -> dict:
    """Median P/E per sector and per industry, over everything stored.

    -> {"sector": {name: pe}, "industry": {name: pe}, "of": {symbol: (s, i)}}

    **Over the whole store, not over whatever a query happens to scan.** An
    industry P/E that moved when you changed universe would be a different
    number under the same name in two screens, and the comparison
    `pe < industry_pe` would quietly mean something different each time.
    Screener publishes it as a fixed figure and so does this.

    Median rather than mean: one loss-making company with a P/E of 400 should
    not decide what its industry is worth. Non-positive P/Es are dropped
    entirely — a company with no earnings has no P/E, and a negative one is
    not a cheap one.
    """
    import statistics

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, key, value FROM fund_snapshot "
            "WHERE key IN ('Sector', 'Industry', 'Stock P/E', 'P/E')"
        ).fetchall()
    by_symbol: dict[str, dict] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], {})[r["key"]] = r["value"]

    of: dict[str, tuple] = {}
    buckets: dict[str, dict[str, list]] = {"sector": {}, "industry": {}}
    for symbol, d in by_symbol.items():
        sector, industry = d.get("Sector"), d.get("Industry")
        of[symbol] = (sector, industry)
        pe = _num(str(d.get("Stock P/E") or d.get("P/E") or ""))
        if pe is None or pe <= 0:
            continue
        for kind, name in (("sector", sector), ("industry", industry)):
            if name:
                buckets[kind].setdefault(name, []).append(pe)

    return {
        "sector": {k: round(statistics.median(v), 2)
                   for k, v in buckets["sector"].items()},
        "industry": {k: round(statistics.median(v), 2)
                     for k, v in buckets["industry"].items()},
        "of": of,
    }


def summary(symbol: str) -> dict | None:
    symbol = symbol.upper()
    with db.connect() as conn:
        snap = conn.execute(
            "SELECT key, value, variant, fetched_at FROM fund_snapshot "
            "WHERE symbol=?", (symbol,)).fetchall()
        if not snap:
            return None
        qp, q_sales = _series(conn, symbol, "quarters", "sales%", "revenue%")
        _, q_np = _series(conn, symbol, "quarters", "net profit%")
        _, q_opm = _series(conn, symbol, "quarters", "opm%")
    keep = 6
    return {
        "symbol": symbol,
        "variant": snap[0]["variant"],
        "fetched_at": snap[0]["fetched_at"],
        "stale": (datetime.now() - datetime.fromisoformat(snap[0]["fetched_at"])
                  > STALE_AFTER),
        "snapshot": {r["key"]: r["value"] for r in snap},
        "computed": compute(symbol),
        "quarters": {
            "periods": qp[-keep:],
            "Sales": q_sales[-keep:],
            "Net Profit": q_np[-keep:],
            "OPM %": q_opm[-keep:],
        },
    }


def full(symbol: str) -> dict | None:
    """summary() plus every stored statement table, for the full page.
    -> {...summary, statements: {section: {periods:[...], rows:{label:[vals]}}}}
    """
    symbol = symbol.upper()
    base = summary(symbol)
    if not base:
        return None
    statements: dict[str, dict] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT statement, row, period_idx, period, value FROM fund_series "
            "WHERE symbol=? ORDER BY statement, row, period_idx", (symbol,)).fetchall()
    for r in rows:
        st = statements.setdefault(r["statement"], {"periods": {}, "rows": {}})
        st["periods"][r["period_idx"]] = r["period"]
        st["rows"].setdefault(r["row"], {})[r["period_idx"]] = r["value"]
    # flatten the idx->period / idx->value maps into aligned ordered lists
    out_st: dict[str, dict] = {}
    for sec, st in statements.items():
        idxs = sorted(st["periods"])
        out_st[sec] = {
            "periods": [st["periods"][i] for i in idxs],
            "rows": {label: [vals.get(i) for i in idxs]
                     for label, vals in st["rows"].items()},
        }
    base["statements"] = out_st
    return base


def diagnose(symbol: str) -> dict:
    """Why did this symbol end up with no ratios? Reports stage by stage.

    A fetch can fail in five distinct places and they need different fixes,
    but they all surface identically as "nothing stored". This walks the
    pipeline — URL, HTTP, page shape, section parse, ratio inputs — and names
    the first thing that broke.
    """
    symbol = symbol.upper()
    slug = slug_for(symbol)
    out = {"symbol": symbol, "slug": slug,
           "slug_overridden": slug != symbol,
           "stages": [], "verdict": "", "fix": ""}

    def stage(name, ok, detail=""):
        out["stages"].append({"stage": name, "ok": ok, "detail": detail})
        return ok

    # 1. what's already in the DB
    with db.connect() as conn:
        n_snap = conn.execute("SELECT COUNT(*) c FROM fund_snapshot WHERE symbol=?",
                              (symbol,)).fetchone()["c"]
        secs = [r["statement"] for r in conn.execute(
            "SELECT DISTINCT statement FROM fund_series WHERE symbol=?",
            (symbol,))]
    stage("stored", bool(n_snap or secs),
          f"{n_snap} snapshot keys, sections: {', '.join(secs) or 'none'}")

    # 2. fetch
    try:
        html, variant = fetch_html(symbol)
        stage("http", True, f"{variant} page, {len(html):,} bytes")
    except Exception as e:
        stage("http", False, str(e))
        out["verdict"] = "screener page could not be fetched"
        out["fix"] = (
            f"Check https://www.screener.in/company/{slug}/ opens in a browser. "
            f"If the real page lives at a different code, set the override:\n"
            f"  python -m app.cli fund slug {symbol} <actual-slug>\n"
            f"If it 429s, screener is rate-limiting — wait and retry.")
        return out

    # 3. parse
    parsed = parse(html)
    got = list(parsed["tables"])
    stage("snapshot", bool(parsed["snapshot"]),
          f"{len(parsed['snapshot'])} keys")
    stage("sections", bool(got), f"parsed: {', '.join(got) or 'none'}")
    missing = [s for s in SECTIONS if s not in got]
    if missing:
        # "shareholding" was added to SECTIONS after most companies were
        # already stored, so it is missing for every one of them until a
        # re-fetch. That is a stale cache, not a broken parser, and saying so
        # here saves an hour of looking for a layout change that didn't happen.
        why = (f"missing: {', '.join(missing)}"
               + ("  (re-fetch this symbol: sections added since it was "
                  "last stored)" if len(missing) < len(SECTIONS) else ""))
        stage("sections-complete", False, why)

    if not got:
        out["verdict"] = "page fetched but no statement tables parsed"
        out["fix"] = ("screener's layout likely changed — the section ids in "
                      "SECTIONS no longer match. Compare against the page HTML.")
        return out

    # 4. the specific rows compute() needs
    needed = {
        "profit-loss": ["Sales", "Net Profit", "Profit before tax",
                        "Interest", "OPM %"],
        "balance-sheet": ["Equity Capital", "Reserves", "Borrowings"],
        "ratios": ["ROCE %"],
    }
    absent = []
    for sec, labels in needed.items():
        have = {l.lower() for l in parsed["tables"].get(sec, {}).get("rows", {})}
        for want in labels:
            if not any(h.startswith(want.lower()[:6]) for h in have):
                absent.append(f"{sec}/{want}")
    stage("ratio-inputs", not absent,
          f"missing rows: {', '.join(absent)}" if absent else "all present")

    # 5. what compute() actually produces from what's stored
    ratios = compute(symbol)
    stage("computed", bool(ratios), f"{len(ratios)} ratios")

    if absent and not ratios:
        out["verdict"] = "page parsed, but the rows the ratios need aren't there"
        out["fix"] = ("Banks and NBFCs genuinely lack some of these — ROCE and "
                      "Debt/Equity don't apply to lenders. For a non-financial, "
                      "this suggests a row was renamed on screener.")
    elif not ratios:
        out["verdict"] = "nothing stored yet for this symbol"
        out["fix"] = f"Run: python -m app.cli fund fetch {symbol}"
    else:
        out["verdict"] = f"looks healthy — {len(ratios)} ratios computed"
    return out


def fetch_all(delay: float = 3.0):
    """Fetch fundamentals for the whole watchlist, politely."""
    results = {}
    for w in watchlist.items():
        sym = w["tradingsymbol"]
        try:
            fetch_and_store(sym)
            results[sym] = "ok"
        except (Exception, SystemExit) as e:
            results[sym] = f"failed: {e}"
        time.sleep(delay)
    return results
