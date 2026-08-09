"""Live tick fan-out.

KiteTicker runs in its own (twisted) thread; browser clients connect to our
FastAPI websocket. This hub bridges the two: ticker callbacks push into
asyncio queues via call_soon_threadsafe. Subscriptions are refcounted so we
only hold Kite subscriptions for symbols someone is actually watching.
"""
import asyncio
import threading

from kiteconnect import KiteTicker


class TickerHub:
    def __init__(self):
        self._kws: KiteTicker | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._connected = False

    # ---- lifecycle -------------------------------------------------------
    def start(self, api_key: str, access_token: str,
              loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            if self._kws:
                return
            self._loop = loop
            kws = KiteTicker(api_key, access_token)
            kws.on_ticks = self._on_ticks
            kws.on_connect = self._on_connect
            kws.on_close = self._on_close
            kws.on_reconnect = lambda ws, attempts: None
            self._kws = kws
            kws.connect(threaded=True)

    # ---- kite callbacks (twisted thread) ---------------------------------
    def _on_connect(self, ws, response):
        self._connected = True
        with self._lock:
            tokens = list(self._subs.keys())
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_QUOTE, tokens)

    def _on_close(self, ws, code, reason):
        self._connected = False

    def _on_ticks(self, ws, ticks):
        if not self._loop:
            return
        for t in ticks:
            token = t.get("instrument_token")
            with self._lock:
                queues = list(self._subs.get(token, ()))
            if not queues:
                continue
            ohlc = t.get("ohlc") or {}
            payload = {
                "token": token,
                "ltp": t.get("last_price"),
                "volume": t.get("volume_traded"),
                "open": ohlc.get("open"), "high": ohlc.get("high"),
                "low": ohlc.get("low"), "prev_close": ohlc.get("close"),
                "change": t.get("change"),
            }
            for q in queues:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)

    # ---- client side (asyncio thread) ------------------------------------
    def subscribe(self, token: int, queue: asyncio.Queue) -> None:
        with self._lock:
            fresh = token not in self._subs
            self._subs.setdefault(token, set()).add(queue)
        if fresh and self._kws and self._connected:
            self._kws.subscribe([token])
            self._kws.set_mode(self._kws.MODE_QUOTE, [token])

    def unsubscribe(self, token: int, queue: asyncio.Queue) -> None:
        with self._lock:
            qs = self._subs.get(token)
            if not qs:
                return
            qs.discard(queue)
            empty = not qs
            if empty:
                del self._subs[token]
        if empty and self._kws and self._connected:
            try:
                self._kws.unsubscribe([token])
            except Exception:
                pass


hub = TickerHub()
