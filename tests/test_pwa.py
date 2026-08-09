"""The PWA pass on Desk.

Most of this is assets and headers, which sounds trivial and isn't: a manifest
with a bad start_url installs an app that opens the wrong page, and a service
worker with the wrong caching rule shows you an hour-old quote that looks
exactly like a live one.

That last point is the reason for the bulk of this file. On a page whose whole
purpose is a number that changes, "cached" and "wrong" are the same word.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "app" / "web"


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app import db
    from app.server import app
    db.init()
    return TestClient(app, base_url="http://127.0.0.1:5000")


# ================================================================= manifest
def test_manifest_is_served_with_the_right_type():
    c = _client()
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]


def test_manifest_opens_the_desk_not_the_chart():
    """The icon on the home screen is for the phone-in-the-office use case.
    Landing on the full chart page would be the wrong page every time."""
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    assert m["start_url"] == "/desk"


def test_manifest_is_standalone():
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    assert m["display"] == "standalone"


def test_manifest_has_the_sizes_installers_need():
    """192 and 512 are the two Android actually requires."""
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_manifest_has_a_maskable_icon():
    """Without one, launchers that crop to a circle cut the artwork."""
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    assert any("maskable" in (i.get("purpose") or "") for i in m["icons"])


def test_every_declared_icon_exists_and_is_a_png():
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    c = _client()
    for icon in m["icons"]:
        r = c.get(icon["src"])
        assert r.status_code == 200, f"{icon['src']} is declared but missing"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n", f"{icon['src']} is not a PNG"


def test_icon_dimensions_match_what_the_manifest_claims():
    """A manifest that lies about sizes gets the icon rejected or rescaled."""
    import struct
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    for icon in m["icons"]:
        raw = (WEB / icon["src"].lstrip("/")).read_bytes()
        w, h = struct.unpack(">II", raw[16:24])
        want = int(icon["sizes"].split("x")[0])
        assert (w, h) == (want, want), f"{icon['src']} is {w}x{h}, claims {want}"


def test_icon_route_refuses_traversal():
    c = _client()
    assert c.get("/icons/..%2f..%2fserver.py").status_code == 404


# =========================================================== service worker
def test_worker_is_served_from_the_root():
    """A worker's scope can't reach above its own path — one parked under a
    static directory could never control /desk."""
    c = _client()
    r = c.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_worker_itself_is_never_cached():
    """If the browser caches the worker you can never ship a new one —
    including the one that fixes the caching."""
    c = _client()
    cc = c.get("/sw.js").headers.get("cache-control", "")
    assert "no-store" in cc or "no-cache" in cc


def test_worker_refuses_to_cache_market_data():
    """THE rule. A cached quote is indistinguishable from a live one, and you
    would size a position against it."""
    sw = (WEB / "sw.js").read_text()
    assert 'url.pathname.startsWith("/api/")' in sw
    body = sw[sw.index('startsWith("/api/")'):]
    # the api branch must bail out before any caching happens
    assert body[:200].strip().splitlines()[0].endswith("return;") or \
        "return;" in body[:120]


def test_worker_caches_the_shell():
    sw = (WEB / "sw.js").read_text()
    for asset in ("/desk", "/app.css", "/manifest.webmanifest"):
        assert asset in sw, f"{asset} is not in the precache list"


def test_worker_versions_its_cache():
    """Without a version bump, old clients keep the old CSS for ever."""
    sw = (WEB / "sw.js").read_text()
    assert "const CACHE =" in sw
    assert "caches.delete" in sw, "old caches are never cleaned up"


def test_worker_prefers_the_network_for_navigations():
    """Cache-first on pages means a deploy never reaches a phone that has the
    app installed."""
    sw = (WEB / "sw.js").read_text()
    nav = sw[sw.index('req.mode === "navigate"'):]
    assert nav.index("fetch(req)") < nav.index("caches.match"), \
        "navigation is answered from cache before trying the network"


def test_worker_install_survives_a_missing_asset():
    """cache.addAll is all-or-nothing: one 404 leaves the worker uninstalled
    and the app permanently non-offline."""
    sw = (WEB / "sw.js").read_text()
    # the CALL, not the word — the comment above it explains why it isn't used
    assert "addAll(" not in sw.replace("addAll is all-or-nothing", "")
    assert "cache.add(url).catch" in sw


def test_worker_is_valid_javascript():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", "--check", str(WEB / "sw.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ================================================================ the page
def test_desk_links_the_manifest_and_ios_tags():
    """iOS ignores the manifest for add-to-home-screen and wants its own."""
    c = _client()
    html = c.get("/desk").text
    for needle in ('rel="manifest"', "apple-touch-icon",
                   "apple-mobile-web-app-capable", 'name="theme-color"'):
        assert needle in html, f"desk missing {needle}"


def test_desk_registers_the_worker():
    c = _client()
    html = c.get("/desk").text
    assert "serviceWorker" in html and "/sw.js" in html


def test_desk_puts_alerts_first_on_a_phone():
    """The reordering is the point of the pass: on the phone you're arming
    and checking alerts, not reading the levels grid."""
    c = _client()
    html = c.get("/desk").text
    assert "max-width:700px" in html
    phone = html[html.index("max-width:700px"):]
    assert "order:1" in phone and "order:2" in phone


def test_desk_has_thumb_sized_targets_on_a_phone():
    c = _client()
    phone = c.get("/desk").text
    phone = phone[phone.index("max-width:700px"):]
    assert "min-height:44px" in phone


def test_desk_inputs_are_16px_on_a_phone():
    """Under 16px, iOS zooms the entire page when a field takes focus, and
    the user has to pinch back out every single time."""
    c = _client()
    phone = c.get("/desk").text
    phone = phone[phone.index("max-width:700px"):]
    assert "font-size:16px" in phone


def test_desk_says_so_when_it_goes_stale():
    """A quote that quietly stopped updating looks exactly like a live one."""
    c = _client()
    html = c.get("/desk").text
    assert 'id="offline"' in html
    assert "markStale" in html and "markFresh" in html
    assert "window.addEventListener('offline'" in html


def test_desk_stamps_the_data_with_its_age():
    c = _client()
    html = c.get("/desk").text
    assert 'id="stamp"' in html and "function ago(" in html


def test_desk_handles_the_notch_in_standalone_mode():
    c = _client()
    html = c.get("/desk").text
    assert "viewport-fit=cover" in html
    assert "env(safe-area-inset-top)" in html
