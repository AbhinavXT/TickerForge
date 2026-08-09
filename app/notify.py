"""Phone push notifications via ntfy.

Setup (one-time, ~2 minutes):
  1. Install the ntfy app on your phone (Play Store).
  2. Pick a long, unguessable topic name (it acts as the password),
     e.g. `tf-a8x1-market-4kq9`, and subscribe to it in the app.
  3. Put the same name in .env as NTFY_TOPIC.

Every later phase (corporate events, news digests, price alerts) funnels
through send() below.
"""
import requests

from . import config


def _auth_headers() -> dict:
    """Bearer token for ntfy servers that have auth turned on. Absent by
    default, in which case the topic name is the only credential."""
    tok = config.get("NTFY_TOKEN", "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def send(message: str, title: str = "TickerForge",
         priority: str = "default", tags: str | None = None,
         click_url: str | None = None, actions: str | None = None) -> None:
    """Push a notification. priority: min|low|default|high|urgent."""
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
    resp = requests.post(
        f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()


def send_image(image: bytes, caption: str = "", title: str = "TickerForge",
               filename: str = "chart.png", priority: str = "default",
               tags: str | None = None, click_url: str | None = None) -> None:
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
    resp = requests.put(
        f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
        data=image,
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()