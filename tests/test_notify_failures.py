"""notify reports failure by return value; these pin the callers and the
Retry-After handling that depend on that."""
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from app import notify


def test_retry_after_seconds():
    assert notify._retry_after("7", 0) == 7.0


def test_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=20)
    secs = notify._retry_after(format_datetime(when, usegmt=True), 0)
    assert 15 < secs <= 21


def test_retry_after_garbage_or_past_falls_back_to_backoff():
    assert notify._retry_after("soon", 1) == 10
    assert notify._retry_after("Mon, 01 Jan 2001 00:00:00 GMT", 0) == 5
    assert notify._retry_after(None, 2) == 20


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers = code, headers or {}

    def raise_for_status(self):
        pass


def test_long_retry_after_drops_instead_of_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(notify.time, "sleep", slept.append)
    monkeypatch.setattr(notify, "_note_refusal", lambda s: None)
    calls = []

    def post(url, **kw):
        calls.append(1)
        return _Resp(429, {"Retry-After": "600"})

    assert notify._post("u", data=b"", headers={}, timeout=1, method=post) is False
    assert len(calls) == 1
    assert not any(s >= 30 for s in slept)


def test_wait_turn_does_not_sleep_under_lock(monkeypatch):
    monkeypatch.setattr(notify, "_MIN_GAP_SECONDS", 5.0)
    monkeypatch.setattr(notify, "_last_sent", time.time())
    seen = []
    monkeypatch.setattr(notify.time, "sleep", lambda s: seen.append(notify._lock.locked()))
    notify._wait_turn()
    assert seen == [False]


def test_query_alert_records_undelivered_push(monkeypatch):
    import inspect
    from app import query
    src = inspect.getsource(query)
    assert 'if not notify.send(' in src and '"push failed: not delivered"' in src


def test_watcher_falls_back_to_text_when_image_not_sent(monkeypatch):
    from app import watcher
    monkeypatch.setattr(watcher.intraday, "one_line", lambda lv: "x")
    monkeypatch.setattr(watcher.intraday, "as_text", lambda lv: "levels")
    monkeypatch.setattr(watcher.intraday, "today_minutes", lambda *a: [])
    monkeypatch.setattr(watcher.charts, "intraday_png", lambda *a, **k: b"png")
    monkeypatch.setattr(watcher.notify, "send_image", lambda *a, **k: False)
    texts = []
    monkeypatch.setattr(watcher.notify, "send",
                        lambda body, **k: texts.append(body) or True)
    watcher._fire(None, {}, 1, "crossed")
    assert texts and texts[0].startswith("crossed")
