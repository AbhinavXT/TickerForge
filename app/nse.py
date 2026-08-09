"""NSE website JSON API client.

nseindia.com serves the same JSON its own frontend uses, but gates it behind
bot protection: you must present browser-like headers and carry cookies
acquired from the homepage. Cookies go stale after a while; on 401/403/parse
failure we re-acquire once and retry. Be a polite guest — poll gently.
"""
import time

import requests

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/",
}

# Market-wide feeds; we filter to the watchlist locally. One request each per
# poll regardless of watchlist size.
FEEDS = {
    "announcement": f"{BASE}/api/corporate-announcements?index=equities",
    "corp_action":  f"{BASE}/api/corporates-corporateActions?index=equities",
    "board_meeting": f"{BASE}/api/corporate-board-meetings?index=equities",
}


class NSEClient:
    def __init__(self):
        self._session: requests.Session | None = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        # Homepage visit sets the cookies the API endpoints require.
        s.get(BASE, timeout=15)
        time.sleep(0.5)
        return s

    def get_json(self, url: str):
        if self._session is None:
            self._session = self._new_session()
        for attempt in (1, 2):
            try:
                r = self._session.get(url, timeout=15)
                if r.status_code in (401, 403) or not r.text.strip():
                    raise ValueError(f"blocked (HTTP {r.status_code})")
                return r.json()
            except (ValueError, requests.RequestException):
                if attempt == 2:
                    raise
                # cookies stale / blocked — start a fresh session and retry once
                self._session = self._new_session()

    def fetch_feed(self, kind: str):
        data = self.get_json(FEEDS[kind])
        # Feeds usually return a bare list; some wrap in {"data": [...]}.
        if isinstance(data, dict):
            data = data.get("data") or []
        return data if isinstance(data, list) else []
