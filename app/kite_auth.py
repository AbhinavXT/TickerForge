"""Kite Connect authentication.

Kite access tokens expire every morning (~7:30 AM IST), so once per trading
day you run `python -m app.cli login`, open the printed URL, log in, and paste
back the request_token from the redirect URL. The resulting access token is
cached in data/kite_token.json and reused for the rest of the day.
"""
import json
from datetime import date

from kiteconnect import KiteConnect

from . import config


def _save_token(access_token: str) -> None:
    config.TOKEN_PATH.write_text(
        json.dumps({"access_token": access_token, "login_date": date.today().isoformat()})
    )


def _load_token() -> str | None:
    if not config.TOKEN_PATH.exists():
        return None
    try:
        blob = json.loads(config.TOKEN_PATH.read_text())
    except json.JSONDecodeError:
        return None
    # Tokens are invalidated early next morning; treat same-calendar-day as valid.
    if blob.get("login_date") != date.today().isoformat():
        return None
    return blob.get("access_token")


def login_interactive() -> KiteConnect:
    """Run the daily login dance on the terminal."""
    config.require("KITE_API_KEY", "KITE_API_SECRET")
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    print("\n1. Open this URL in a browser and log in to Zerodha:\n")
    print(f"   {kite.login_url()}\n")
    print("If the server is running (python -m app.cli serve), the redirect")
    print("completes the login automatically and you can Ctrl+C this prompt.")
    print("Otherwise copy `request_token` from the redirect URL.\n")
    request_token = input("Paste request_token here: ").strip()
    session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    kite.set_access_token(session["access_token"])
    _save_token(session["access_token"])
    print(f"\nLogged in as {session.get('user_name', session.get('user_id'))}. "
          f"Token cached for today.")
    return kite


def complete_login(request_token: str) -> None:
    """Exchange a request_token for a session (used by the server's
    redirect-catcher route, making login one click)."""
    config.require("KITE_API_KEY", "KITE_API_SECRET")
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    _save_token(session["access_token"])


def login_url() -> str:
    return KiteConnect(api_key=config.KITE_API_KEY).login_url()


def login_click_url() -> str | None:
    """A URL worth attaching to the expiry push, so it's one tap not a trek.

    Prefers the local server's /login, which redirects to Kite and — because
    the redirect comes BACK to the server — completes the session in place.
    Set PUBLIC_BASE_URL to a Tailscale name and that works from the phone too;
    left unset it points at loopback, which only helps at the desk.

    Falls back to Kite's own URL, which at least opens the login screen.
    """
    base = config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/login"
    try:
        return login_url()
    except Exception:                             # noqa: BLE001
        return None


def load_token() -> str | None:
    """Today's cached access token, if any (public alias)."""
    return _load_token()


def get_kite() -> KiteConnect:
    """Return an authenticated client, or exit with instructions."""
    config.require("KITE_API_KEY")
    token = _load_token()
    if not token:
        raise SystemExit(
            "No valid Kite session for today. Run: python -m app.cli login"
        )
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    kite.set_access_token(token)
    return kite


def token_status() -> str:
    return "valid for today" if _load_token() else "absent/expired (run login)"
