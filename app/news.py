"""Watchlist news digest.

Pipeline: Google News RSS (one query per watchlist stock) -> window filter
-> cross-digest dedupe (seen_news) -> near-duplicate collapse -> curation
-> one concise ntfy push.

Curation backends (NEWS_CURATOR in .env):
  none    grouped plain-text list (default, zero deps)
  ollama  local LLM via Ollama — free, private
  claude  Anthropic API — best quality, needs ANTHROPIC_API_KEY
LLM curation is best-effort: any failure falls back to the plain list.

Lesson from Phase 2 baked in: items are marked seen only AFTER the push
succeeds, so a failed send never swallows news.
"""
import hashlib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import requests

from . import config, db, notify, watchlist

MAX_PER_STOCK = 6
DIGEST_BYTE_CAP = 3800          # ntfy message limit is 4096


# ------------------------------------------------------------------ fetch
def _rss_url(company_name: str, hours: int) -> str:
    days = max(1, round(hours / 24))
    q = f'"{company_name}" when:{days}d'
    return ("https://news.google.com/rss/search?q="
            + urllib.parse.quote(q) + "&hl=en-IN&gl=IN&ceid=IN:en")


def _fetch_items(company_name: str, hours: int) -> list[dict]:
    r = requests.get(_rss_url(company_name, hours),
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        pub = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            when = None
        if not title or (when and when < cutoff):
            continue
        # Google News titles end with " - Publisher"; split it off if present.
        if not src and " - " in title:
            title, src = title.rsplit(" - ", 1)
        items.append({"title": title.strip(), "source": src, "link": link,
                      "published": when.isoformat() if when else ""})
    return items


# ----------------------------------------------------- Moneycontrol scrape
_MC_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/json,*/*",
    "Referer": "https://www.moneycontrol.com/",
}
_MC_CACHE: dict[str, str] = {}      # company/symbol -> news page url (per process)


def _mc_news_url(company_name: str, symbol: str | None) -> str | None:
    """Resolve a stock to its Moneycontrol news page via the autosuggest API."""
    key = (symbol or company_name).upper()
    if key in _MC_CACHE:
        return _MC_CACHE[key] or None
    try:
        q = urllib.parse.quote(symbol or company_name)
        url = ("https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php"
               f"?classic=true&query={q}&type=1&format=json&callback=")
        r = requests.get(url, headers=_MC_HEADERS, timeout=12)
        txt = r.text.strip().lstrip("(").rstrip(")")
        import json as _json
        data = _json.loads(txt)
        rows = data if isinstance(data, list) else data.get("data") or []
        for row in rows:
            link = row.get("link_src") or row.get("link") or ""
            if "/india/stockpricequote/" in link or "/company-" in link:
                # stock page -> its news tab lives under /company-article/.../news/<scid>
                news_url = link
                _MC_CACHE[key] = news_url
                return news_url
    except Exception:
        pass
    _MC_CACHE[key] = ""
    return None


def _fetch_mc_items(company_name: str, symbol: str | None, hours: int) -> list[dict]:
    """Best-effort Moneycontrol headlines for a stock. Never raises."""
    from bs4 import BeautifulSoup
    page = _mc_news_url(company_name, symbol)
    if not page:
        return []
    try:
        r = requests.get(page, headers=_MC_HEADERS, timeout=15)
        if r.status_code != 200 or not r.text:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        # MC stock pages render news as anchors inside the "News" / stock-info
        # blocks; collect plausible news links and their text.
        for a in soup.select("a[href*='/news/']"):
            title = a.get_text(strip=True)
            href = a.get("href") or ""
            if len(title) < 25 or not href.startswith("http"):
                continue
            items.append({"title": title, "source": "Moneycontrol",
                          "link": href, "published": ""})
            if len(items) >= MAX_PER_STOCK:
                break
        # de-dupe within MC by title
        seen, out = set(), []
        for it in items:
            k = _norm(it["title"])[:80]
            if k in seen:
                continue
            seen.add(k); out.append(it)
        return out
    except Exception:
        return []


def _fetch_all_sources(company_name: str, symbol: str | None, hours: int) -> list[dict]:
    """Google News + Moneycontrol, merged. Google failing propagates (it's the
    primary); Moneycontrol is additive and swallows its own errors."""
    items = _fetch_items(company_name, hours)
    if config.get("NEWS_MONEYCONTROL", "1") not in ("0", "false", "no"):
        items += _fetch_mc_items(company_name, symbol, hours)
    return items


# ------------------------------------------------------------------ dedupe
def _norm(t: str) -> str:
    return "".join(ch for ch in t.lower() if ch.isalnum() or ch == " ")


def _collapse_near_dupes(items: list[dict]) -> list[dict]:
    """Collapse near-duplicate headlines. The survivor remembers the ids of
    the dupes merged into it (_dupe_ids) so they get marked seen alongside
    it — otherwise a collapsed dupe resurfaces as a 'fresh' singleton in the
    next digest cycle."""
    kept: list[dict] = []
    for it in items:
        n = _norm(it["title"])
        match = next((k for k in kept
                      if SequenceMatcher(None, n, _norm(k["title"])).ratio() > 0.75),
                     None)
        if match is not None:
            if it.get("_id"):
                match.setdefault("_dupe_ids", []).append(it["_id"])
            continue
        kept.append(it)
    return kept


def _item_id(symbol: str, it: dict) -> str:
    return hashlib.sha1(f"{symbol}|{_norm(it['title'])}".encode()).hexdigest()


def collect(hours: int = 24) -> dict[str, list[dict]]:
    """Fresh, unseen, de-duplicated items grouped by watchlist symbol.
    Fetch failures are collected into collect.last_errors, never silent."""
    grouped: dict[str, list[dict]] = {}
    errors: list[str] = []
    with db.connect() as conn:
        for w in watchlist.items():
            name = w["name"] or w["tradingsymbol"]
            try:
                items = _fetch_items(name, hours)
            except Exception as e:
                errors.append(f"{w['tradingsymbol']}: {type(e).__name__}: {e}")
                continue
            unseen = []
            for it in items:
                eid = _item_id(w["tradingsymbol"], it)
                row = conn.execute(
                    "SELECT 1 FROM seen_news WHERE id=?", (eid,)).fetchone()
                if not row:
                    it["_id"] = eid
                    unseen.append(it)
            unseen = _collapse_near_dupes(unseen)[:MAX_PER_STOCK]
            if unseen:
                grouped[w["tradingsymbol"]] = unseen
            time.sleep(0.5)      # be polite to Google
    collect.last_errors = errors
    return grouped


# ------------------------------------------------------------------ aggregate
def aggregate(hours: int = 48) -> dict:
    """Live watchlist news for the web page: fetch + within-stock near-dupe
    collapse, grouped by symbol. Deliberately does NOT read or write
    seen_news, so browsing here never interferes with the push digest.
    """
    grouped: dict[str, list[dict]] = {}
    errors: list[str] = []
    wl = watchlist.items()
    for w in wl:
        name = w["name"] or w["tradingsymbol"]
        try:
            items = _collapse_near_dupes(_fetch_items(name, hours))[:MAX_PER_STOCK]
        except Exception as e:
            errors.append(f"{w['tradingsymbol']}: {type(e).__name__}: {e}")
            continue
        if items:
            grouped[w["tradingsymbol"]] = [
                {"title": it["title"], "source": it["source"],
                 "link": it["link"], "published": it["published"]} for it in items]
        time.sleep(0.3)      # be polite to Google
    return {"hours": hours,
            "watchlist": [w["tradingsymbol"] for w in wl],
            "grouped": grouped, "errors": errors}


# ------------------------------------------------------------------ curate
_PROMPT = """You curate a twice-daily stock-news digest for a personal watchlist.
Below are fresh headlines grouped by stock symbol. Produce a CONCISE plain-text
digest:
- Group by symbol, symbol name in CAPS on its own line.
- One short line per distinct story; merge stories that cover the same event.
- Drop fluff: listicles, "top picks", price-target spam, astrology-grade tips.
- Prefix genuinely material items (results, large orders, regulatory action,
  M&A, management changes) with "! ".
- No preamble, no markdown, no links. Plain text only.

Headlines:
{payload}"""


def _headlines_payload(grouped: dict) -> str:
    lines = []
    for sym, items in grouped.items():
        lines.append(f"{sym}:")
        lines += [f"  - {it['title']} ({it['source']})" for it in items]
    return "\n".join(lines)


def _curate_plain(grouped: dict) -> str:
    parts = []
    for sym, items in grouped.items():
        parts.append(sym)
        parts += [f"• {it['title']} ({it['source']})" if it["source"]
                  else f"• {it['title']}" for it in items]
        parts.append("")
    return "\n".join(parts).strip()


def _curate_ollama(grouped: dict) -> str:
    r = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={"model": config.OLLAMA_MODEL, "stream": False,
              "messages": [{"role": "user",
                            "content": _PROMPT.format(
                                payload=_headlines_payload(grouped))}]},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _curate_claude(grouped: dict) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": config.ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"},
        json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1200,
              "messages": [{"role": "user",
                            "content": _PROMPT.format(
                                payload=_headlines_payload(grouped))}]},
        timeout=60,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def curate(grouped: dict) -> str:
    from . import llm
    text = llm.complete(_PROMPT.format(payload=_headlines_payload(grouped)))
    return text if text else _curate_plain(grouped)


# ------------------------------------------------------------------ digest
def send_digest(hours: int = 24, dry: bool = False) -> str:
    grouped = collect(hours)
    errs = getattr(collect, "last_errors", [])
    if errs:
        print("fetch errors: " + "; ".join(errs))
    if not grouped:
        return "no fresh news"
    text = curate(grouped)
    if len(text.encode()) > DIGEST_BYTE_CAP:
        text = text.encode()[:DIGEST_BYTE_CAP].decode(errors="ignore") + "\n…"
    n = sum(len(v) for v in grouped.values())
    if dry:
        print(text)
        return f"dry run — {n} items across {len(grouped)} stocks (not sent, not marked seen)"
    notify.send(text, title=f"News digest — {len(grouped)} stocks",
                tags="newspaper")
    # Mark seen only after the push succeeded (Phase 2 lesson).
    with db.connect() as conn:
        for sym, items in grouped.items():
            rows = []
            for it in items:
                rows.append((it["_id"], sym, it["title"], it["link"]))
                rows += [(did, sym, f"(dupe of) {it['title']}", "")
                         for did in it.get("_dupe_ids", [])]
            conn.executemany(
                "INSERT OR IGNORE INTO seen_news (id, symbol, title, link) "
                "VALUES (?, ?, ?, ?)", rows)
    return f"sent — {n} items across {len(grouped)} stocks"
