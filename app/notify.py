"""Phone push notifications via ntfy.

Setup (one-time, ~2 minutes):
  1. Install the ntfy app on your phone (Play Store).
  2. Pick a long, unguessable topic name (it acts as the password),
     e.g. `tf-a8x1-market-4kq9`, and subscribe to it in the app.
  3. Put the same name in .env as NTFY_TOPIC.

Every later phase (corporate events, news digests, price alerts) funnels
through send() below.
"""
import threading
import time

import requests

from . import config


# --------------------------------------------------------------------- pacing
# ntfy.sh allows a burst and then replenishes slowly, and every service in this
# process tree publishes through here — the watcher, the events poller, the
# remote listener, the supervisor. A candles reply alone is five messages fired
# back to back, so without pacing one phone tap can spend a meaningful share of
# the burst in under a second.
# Only the public instance needs this. ntfy.sh replenishes a shared burst
# slowly and every service in this tree publishes through here, so a candles
# reply could spend a real share of it in under a second. A self-hosted server
# has no such budget, and pacing there would just make the app wait.
_MIN_GAP_SECONDS = 1.2 if "ntfy.sh" in config.NTFY_SERVER else 0.0

# How long to keep slowing down after a refusal. Once the server has said no,
# the burst is gone and the replenish rate is what is left, so pushing at the
# normal pace just collects more refusals.
_COOLDOWN_SECONDS = 120.0
_COOLDOWN_GAP_SECONDS = 6.0

_lock = threading.Lock()
_last_sent = 0.0
_throttled_until = 0.0


def _wait_turn() -> None:
    """Block until enough time has passed since the previous publish."""
    global _last_sent
    with _lock:
        gap = _COOLDOWN_GAP_SECONDS if time.time() < _throttled_until else _MIN_GAP_SECONDS
        wait = _last_sent + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_sent = time.time()


def _note_refusal(retry_after: float) -> None:
    global _throttled_until
    with _lock:
        _throttled_until = max(_throttled_until,
                               time.time() + max(retry_after, _COOLDOWN_SECONDS))


def _auth_headers() -> dict:
    """Bearer token for ntfy servers that have auth turned on. Absent by
    default, in which case the topic name is the only credential."""
    tok = config.get("NTFY_TOKEN", "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def send(message: str, title: str = "TickerForge",
         priority: str = "default", tags: str | None = None,
         click_url: str | None = None, actions: str | None = None) -> bool:
    """Push a notification. priority: min|low|default|high|urgent.

    @return True when the message went out. False is normal and survivable —
    a rate limit, usually — and callers sending a batch should stop rather
    than push the rest into a closed door.
    """
    config.require("NTFY_TOPIC")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags          # e.g. "chart_with_upwards_trend"
    if click_url:
        headers["Click"] = click_url    # opens this URL when tapped
    if actions:
        # ntfy action buttons, e.g.
        #   "http, Approve, https://host/approve?token=…, method=POST"
        # Only used by semi-auto trading, and only when the box is reachable
        # from the phone — otherwise approval comes back as a command instead.
        headers["Actions"] = actions
    # HTTP header values go out as latin-1, which can't hold ₹, ↩, emoji or
    # accented company names. Reinterpret the UTF-8 bytes as latin-1 so the raw
    # UTF-8 is sent on the wire; ntfy decodes headers as UTF-8 and shows them
    # correctly. Without this, one unicode char in a title crashes the push.
    headers = {k: v.encode("utf-8").decode("latin-1") for k, v in headers.items()}
    headers.update(_auth_headers())     # after the latin-1 pass: ASCII already
    return _post(
        f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=10,
    )


# Attempts before a message is given up on. Small: the caller is usually in a
# loop of its own, and a long retry inside one send just delays the next.
_ATTEMPTS = 3


def _post(url: str, *, data, headers, timeout: int, method=requests.post) -> bool:
    """Publish, backing off on refusal.

    Returns True when it went, False when the server refused and would not take
    it after a wait.

    Never raises for a rate limit, which is the important part. It used to, and
    the refusal landed on the banner that `listen()` publishes at startup — so
    the listener died, the supervisor restarted it, the restart published the
    banner again, and each attempt spent another token against a limit that was
    already exhausted. A recoverable condition had made itself permanent. Rate
    limiting is an operating condition here, not a programming error, and it
    should not be able to take a service down.
    """
    for attempt in range(_ATTEMPTS):
        _wait_turn()
        try:
            resp = method(url, data=data, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            print(f"  ntfy: publish failed ({e})")
            return False

        if resp.status_code != 429:
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                # Anything else is a real problem — a bad topic, a dead server —
                # and hiding it would be worse than the noise.
                print(f"  ntfy: {e}")
                return False
            return True

        # The server says wait. Honour its figure where it gives one.
        retry_after = float(resp.headers.get("Retry-After") or 0) or (5 * 2 ** attempt)
        _note_refusal(retry_after)
        if attempt == _ATTEMPTS - 1:
            print(f"  ntfy: rate limited, dropping message (retry-after {retry_after:.0f}s)")
            return False
        print(f"  ntfy: rate limited, waiting {retry_after:.0f}s")
        time.sleep(retry_after)
    return False


def send_image(image: bytes, caption: str = "", title: str = "TickerForge",
               filename: str = "chart.png", priority: str = "default",
               tags: str | None = None, click_url: str | None = None) -> bool:
    """Push an image attachment via ntfy. The image is the body, so the caption
    rides in the Message header (single line — ntfy headers can't hold newlines).
    """
    config.require("NTFY_TOPIC")
    headers = {"Title": title, "Filename": filename, "Priority": priority}
    if caption:
        headers["Message"] = caption.replace("\n", " · ")
    if tags:
        headers["Tags"] = tags
    if click_url:
        headers["Click"] = click_url
    headers = {k: v.encode("utf-8").decode("latin-1") for k, v in headers.items()}
    headers.update(_auth_headers())
    return _post(
        f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
        data=image,
        headers=headers,
        timeout=20,
        method=requests.put,
    )