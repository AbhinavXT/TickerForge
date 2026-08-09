"""Market-wide 'Latest Quarterly Results' feed — every company that just
reported, with the numbers — scraped from a public screener.in screen.

Screener's screen pages are a clean HTML table (Name, CMP, P/E, Mar Cap,
Div Yld, NP Qtr + YoY%, Sales Qtr + YoY%, ROCE, PAT). We read it the same
polite way fundamentals.py reads company pages, cache the rows in SQLite,
and serve them to the web list + a compact phone view.

The screen is configurable (LATEST_RESULTS_SCREEN) so if that public screen
ever changes you can point at another without touching code.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from . import config, db, watchlist
from .fundamentals import BASE, HEADERS, _num

# JOHNNY's "All latest quarterly results with profits" — market-wide reporters.
DEFAULT_SCREEN = "608536"
MAX_PAGES = 8                     # safety cap; ~25 rows/page
STALE_MINUTES = 30

# header text (normalized) -> our field name
_COLMAP = {
    "name": "name",
    "cmp": "cmp", "cmp rs": "cmp",
    "p/e": "pe", "pe": "pe",
    "mar cap": "mcap", "mar cap rs cr": "mcap", "market cap": "mcap",
    "div yld": "div_yld", "div yld %": "div_yld",
    "np qtr": "np_qtr", "np qtr rs cr": "np_qtr",
    "qtr profit var": "np_yoy", "qtr profit var %": "np_yoy",
    "sales qtr": "sales_qtr", "sales qtr rs cr": "sales_qtr",
    "qtr sales var": "sales_yoy", "qtr sales var %": "sales_yoy",
    "roce": "roce", "roce %": "roce",
    "pat qtr": "pat_qtr", "pat qtr rs cr": "pat_qtr",
}
# our sort keys -> screener's ?sort= parameter
SORT_PARAM = {
    "latest": None,
    "np": "yoy quarterly profit growth",
    "sales": "yoy quarterly sales growth",
    "np_qtr": "net profit latest quarter",
    "mcap": "market capitalization",
    "pe": "price to earning",
}


# ------------------------------------------------------------------ schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS latest_results (
    rank       INTEGER,
    symbol     TEXT,
    name       TEXT,
    cmp        REAL, pe REAL, mcap REAL, div_yld REAL,
    np_qtr     REAL, np_yoy REAL,
    sales_qtr  REAL, sales_yoy REAL,
    roce       REAL, pat_qtr REAL,
    fetched_at TEXT
);
"""


def _ensure_table(conn):
    conn.executescript(SCHEMA)


# ------------------------------------------------------------------ scrape
def _norm(text: str) -> str:
    return (text or "").replace("Rs.", "").replace(".", "").replace("%", " %") \
        .replace("  ", " ").strip().lower().rstrip(".").strip()


def _symbol_from_href(href: str) -> str:
    # /company/KSOLVES/ -> KSOLVES ; /company/TCS/consolidated/ -> TCS ;
    # /company/526071/ -> 526071 (BSE code, no NSE symbol)
    parts = [p for p in (href or "").split("/") if p]
    if "company" in parts:
        i = parts.index("company")
        if i + 1 < len(parts):
            return parts[i + 1].upper()
    return ""


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        head = " ".join(th.get_text(" ", strip=True).lower()
                        for th in t.find_all("th"))
        if "cmp" in head and ("np qtr" in head or "sales qtr" in head
                              or "name" in head):
            table = t
            break
    if table is None:
        return []

    headers = [_norm(th.get_text(" ", strip=True))
               for th in table.find_all("th")]
    # map each column index -> our field (best substring match)
    idx_field: dict[int, str] = {}
    for i, h in enumerate(headers):
        for key, field in _COLMAP.items():
            if h == key or h.startswith(key):
                idx_field[i] = field
                break

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        rec: dict = {"symbol": "", "name": ""}
        rank = None
        for i, td in enumerate(tds):
            field = idx_field.get(i)
            a = td.find("a")
            if i == 0:
                rank = _num(td.get_text(strip=True))
            if field == "name" or (a and "/company/" in (a.get("href") or "")):
                rec["name"] = td.get_text(" ", strip=True)
                if a:
                    rec["symbol"] = _symbol_from_href(a.get("href"))
            elif field:
                rec[field] = _num(td.get_text(strip=True))
        if rec.get("name"):
            rec["rank"] = int(rank) if rank else None
            rows.append(rec)
    return rows


def fetch(screen: str | None = None, max_pages: int = MAX_PAGES,
          sort: str = "latest", delay: float = 1.2) -> list[dict]:
    """Scrape all pages of the screen. Returns the parsed rows (no DB write)."""
    screen = screen or config.get("LATEST_RESULTS_SCREEN", DEFAULT_SCREEN)
    sort_param = SORT_PARAM.get(sort)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"page": page}
        if sort_param:
            params["sort"] = sort_param
            params["order"] = "desc"
        url = f"{BASE}/screens/{screen}/"
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"screener screen HTTP {r.status_code}")
            break
        rows = _parse_page(r.text)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 5:            # last (short) page
            break
        time.sleep(delay)
    return out


def refresh(screen: str | None = None, sort: str = "latest") -> int:
    """Scrape + replace the cache. Returns row count."""
    rows = fetch(screen=screen, sort=sort)
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM latest_results")
        conn.executemany(
            "INSERT INTO latest_results (rank, symbol, name, cmp, pe, mcap, "
            "div_yld, np_qtr, np_yoy, sales_qtr, sales_yoy, roce, pat_qtr, "
            "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r.get("rank"), r.get("symbol"), r.get("name"), r.get("cmp"),
              r.get("pe"), r.get("mcap"), r.get("div_yld"), r.get("np_qtr"),
              r.get("np_yoy"), r.get("sales_qtr"), r.get("sales_yoy"),
              r.get("roce"), r.get("pat_qtr"), now) for r in rows])
    db.set_meta("latest_results_fetched_at", now)
    return len(rows)


# ------------------------------------------------------------------ read
def _bucket(mcap: float | None) -> str:
    if mcap is None:
        return "—"
    if mcap >= 20000:
        return "Large"
    if mcap >= 5000:
        return "Mid"
    if mcap >= 500:
        return "Small"
    if mcap >= 100:
        return "Micro"
    return "Nano"


def fetched_at() -> str | None:
    return db.get_meta("latest_results_fetched_at")


def is_stale() -> bool:
    ts = fetched_at()
    if not ts:
        return True
    try:
        age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        return age > STALE_MINUTES * 60
    except ValueError:
        return True


def rows(scope: str = "all", bucket: str | None = None, sort: str = "latest",
         limit: int | None = None) -> list[dict]:
    """Read the cached feed with optional watchlist scope, mcap bucket, sort."""
    with db.connect() as conn:
        _ensure_table(conn)
        data = [dict(r) for r in conn.execute(
            "SELECT * FROM latest_results").fetchall()]

    if scope == "watch":
        watched = {w["tradingsymbol"].upper() for w in watchlist.items()}
        data = [r for r in data if (r.get("symbol") or "").upper() in watched]
    for r in data:
        r["bucket"] = _bucket(r.get("mcap"))
    if bucket and bucket != "All":
        data = [r for r in data if r["bucket"] == bucket]

    def key(r):
        if sort == "np":
            return -(r.get("np_yoy") if r.get("np_yoy") is not None else -1e9)
        if sort == "sales":
            return -(r.get("sales_yoy") if r.get("sales_yoy") is not None else -1e9)
        if sort == "mcap":
            return -(r.get("mcap") or 0)
        if sort == "pe":
            return (r.get("pe") if r.get("pe") is not None else 1e9)
        return r.get("rank") or 1e9          # 'latest' = screen order
    data.sort(key=key)
    return data[:limit] if limit else data


# ------------------------------------------------------------------ format
def _f(v, dp=2):
    return "—" if v is None else f"{v:,.{dp}f}"


def _pct(v):
    return "" if v is None else f"{v:+.1f}%"


def one_line(r: dict) -> str:
    """Compact single-line row for the phone list."""
    nm = (r.get("name") or r.get("symbol") or "?")[:20]
    return (f"{nm}  NP {_f(r.get('np_qtr'),0)} {_pct(r.get('np_yoy'))} · "
            f"Sales {_f(r.get('sales_qtr'),0)} {_pct(r.get('sales_yoy'))}")


def as_text(data: list[dict], sort: str = "latest", scope: str = "all",
            total: int | None = None, limit: int = 15) -> str:
    if not data:
        return ("No latest-results feed yet. Refresh it at home with "
                "`results fetch-latest` (or open the Results page).")
    sort_label = {"np": "Net Profit YoY", "sales": "Sales YoY",
                  "mcap": "Mcap", "latest": "latest"}.get(sort, sort)
    head = f"Latest results ({'watchlist' if scope == 'watch' else 'market'}, "
    head += f"by {sort_label}) — {total or len(data)} cos"
    lines = [head, ""]
    for r in data[:limit]:
        lines.append("• " + one_line(r))
    if (total or len(data)) > limit:
        lines.append(f"  +{(total or len(data)) - limit} more — see the "
                     f"Results page")
    ts = fetched_at()
    if ts:
        lines.append(f"\nas of {ts[11:16]}")
    return "\n".join(lines)
