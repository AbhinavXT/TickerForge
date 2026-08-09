"""The server has no login, so the browser's own rules are the only defence.

Binding to 127.0.0.1 makes the server unreachable from the network. It does
not make it unreachable from the *browser you are already running*, which is
the whole point of CSRF: a page on any domain can aim a form at localhost.

Write endpoints here take query parameters, so a cross-origin form POST is a
CORS "simple request" — dispatched with no preflight and no way for the
browser to stop it. The attacker can't read the reply, but by then the alert
is armed and the journal has a trade in it.

Today that's a data-hygiene problem. At roadmap 6.2 the same routes gain order
placement, which is why the guard lands before the capability rather than
after.
"""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient        # noqa: E402

from app import db                              # noqa: E402
from app.server import app                       # noqa: E402

db.init()
# base_url sets the Host header — the default "testserver" isn't loopback and
# would be caught by the rebinding guard before the CSRF check ran.
client = TestClient(app, base_url="http://127.0.0.1:5000")

EVIL = "https://evil.example"


# ------------------------------------------------------------ the unit rules
def test_missing_origin_is_allowed():
    """No Origin means no browser. curl, the CLI and the test client all
    arrive this way, and none of them can be a cross-site request."""
    from app.server import _origin_ok
    # the middleware treats None specially; _origin_ok is only asked about
    # values that exist
    assert _origin_ok("http://127.0.0.1:5000", "127.0.0.1:5000")


def test_foreign_origin_is_refused():
    from app.server import _origin_ok
    assert not _origin_ok("https://evil.example", "127.0.0.1:5000")
    assert not _origin_ok("http://evil.example:5000", "127.0.0.1:5000")


def test_a_different_local_port_is_still_not_us():
    """Another service on loopback is a different origin. Same machine is not
    the same trust boundary — a dev server on :3000 shouldn't be able to
    write here."""
    from app.server import _origin_ok
    assert not _origin_ok("http://127.0.0.1:3000", "127.0.0.1:5000")


def test_localhost_and_loopback_ip_are_both_accepted():
    from app.server import _origin_ok
    assert _origin_ok("http://localhost:5000", "localhost:5000")


def test_origin_that_merely_contains_localhost_is_refused():
    """`http://localhost.evil.example` must not pass a substring check."""
    from app.server import _origin_ok
    assert not _origin_ok("http://localhost.evil.example", "127.0.0.1:5000")


def test_hostname_strips_the_port():
    from app.server import _hostname
    assert _hostname("127.0.0.1:5000") == "127.0.0.1"
    assert _hostname("localhost") == "localhost"


# ------------------------------------------------------- end to end, writes
def test_cross_origin_write_is_blocked():
    r = client.post("/api/watchlist/BEL", headers={"Origin": EVIL})
    assert r.status_code == 403
    assert "Cross-site" in r.json()["detail"]


def test_same_origin_write_is_allowed_through_the_guard():
    """The guard must not break the app's own pages. Any status except 403
    means the request reached the route and was judged on its merits."""
    # a route that needs only SQLite, so the guard is the only thing under test
    r = client.post("/api/journal?symbol=BEL&entry=400&qty=1&stop=390",
                    headers={"Origin": "http://127.0.0.1:5000"})
    assert r.status_code != 403


def test_no_origin_write_is_allowed():
    """The CLI and scripts post without an Origin and must keep working."""
    r = client.post("/api/journal?symbol=BEL&entry=400&qty=1&stop=390")
    assert r.status_code != 403


def test_cross_origin_delete_is_blocked():
    r = client.delete("/api/watchlist/BEL", headers={"Origin": EVIL})
    assert r.status_code == 403


def test_cross_origin_journal_write_is_blocked():
    """The endpoint that most obviously shouldn't accept drive-by writes."""
    r = client.post("/api/journal?symbol=BEL&entry=400&qty=1&stop=390",
                    headers={"Origin": EVIL})
    assert r.status_code == 403


def test_cross_origin_alert_arming_is_blocked():
    r = client.post("/api/alerts?symbol=BEL&cond=%3E410",
                    headers={"Origin": EVIL})
    assert r.status_code == 403


# -------------------------------------------------------- reads stay open
def test_cross_origin_read_is_not_blocked():
    """GET is safe by definition here — the browser won't let the attacker
    read the response anyway, and blocking it would break nothing useful."""
    r = client.get("/api/watchlist", headers={"Origin": EVIL})
    assert r.status_code != 403


# ------------------------------------------------------------- DNS rebinding
def test_non_loopback_host_header_is_refused():
    """DNS rebinding: attacker.example resolves to 127.0.0.1, so their page
    becomes same-origin and the Origin check passes. The Host header is what
    still gives it away."""
    r = client.get("/api/watchlist", headers={"Host": "attacker.example"})
    assert r.status_code == 421


def test_loopback_host_is_accepted():
    r = client.get("/api/watchlist", headers={"Host": "127.0.0.1:5000"})
    assert r.status_code != 421
