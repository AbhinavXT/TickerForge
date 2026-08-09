"""Guards against the SystemExit-through-`except Exception` class of bug.

Three real crashes came from this in one week — the daily loss guard, the
live-vs-backtest route, and the fundamentals refresh — and all three were found
by accident rather than by a test. The pattern is worth a permanent check
because it is invisible in normal operation: every one of these blocks was
written to be defensive, and every one failed only in the exact condition it
was written to survive.

Run:  python -m pytest tests/test_systemexit_safety.py
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _systemexit_audit import audit  # noqa: E402

from app import config  # noqa: E402


def test_no_unsafe_except_exception_blocks():
    """Static sweep. If this fails, a new block catches Exception around a
    call that can raise SystemExit — widen it to (Exception, SystemExit), or
    in a server route add `except SystemExit -> HTTPException(401)`."""
    hits = audit("app")
    detail = "\n".join(f"  {f}:{ln} wraps {c}" for f, ln, c in hits)
    assert not hits, (
        f"{len(hits)} block(s) catch Exception around a SystemExit-capable "
        f"call:\n{detail}")


def test_config_require_still_raises_systemexit():
    """The premise of the whole audit. If config.require ever switches to a
    normal exception, this test should fail loudly so the audit can be
    retired rather than quietly guarding nothing."""
    with mock.patch.object(config, "TOTALLY_MISSING_KEY", "", create=True):
        with pytest.raises(SystemExit):
            config.require("TOTALLY_MISSING_KEY")
    assert not issubclass(SystemExit, Exception), \
        "SystemExit is no longer a BaseException-only type"


# ------------------------------------------------- behavioural spot checks
def test_journal_close_survives_unconfigured_ntfy():
    """The bug that started this: an unset NTFY_TOPIC crashed every close."""
    import tempfile
    config.DB_PATH = Path(tempfile.mkdtemp()) / "se.db"
    from app import db, journal
    db.init()

    def boom(*a, **k):
        raise SystemExit("Missing config: NTFY_TOPIC")

    with mock.patch("app.notify.send", boom):
        tid = journal.open_trade("BEL", entry=400, qty=1, stop=390)
        t = journal.close_trade(tid, exit_price=380, charges=0)
    assert t["exit"] == 380 and t["r_multiple"] is not None


def test_watchlist_autofetch_survives_missing_session():
    from app import watchlist, fundamentals

    def boom(*a, **k):
        raise SystemExit("Missing config: KITE_API_KEY")

    with mock.patch.object(fundamentals, "fetch_and_store", boom), \
         mock.patch("app.notify.send", lambda *a, **k: None):
        out = watchlist.ensure_fundamentals("NOPE", background=False)
    assert out and "failed" in out.lower()


def test_brief_survives_missing_session_and_topic():
    """Realistic path: no Kite session, so get_kite raises SystemExit."""
    from app import brief

    def boom(*a, **k):
        raise SystemExit("Missing config: KITE_API_KEY")

    with mock.patch("app.kite_auth.get_kite", boom), \
         mock.patch("app.notify.send", lambda *a, **k: None):
        out = brief.compose(skip=("news",))
    assert out.startswith("Pre-market —")


def test_brief_section_isolation_covers_systemexit():
    """Belt and braces: even if a section raises SystemExit directly — which
    the static audit can't see, because compose() calls sections indirectly —
    the brief still comes out."""
    from app import brief

    def boom():
        raise SystemExit("anything at all")

    with mock.patch.object(brief, "SECTIONS",
                           (("regime", boom),
                            ("risk", brief.budget_section))):
        out = brief.compose()
    assert out.startswith("Pre-market —")
    assert "RISK" in out
    assert "unavailable: regime" in out
