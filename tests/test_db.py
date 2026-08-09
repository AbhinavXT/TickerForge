"""Offline tests for the SQLite layer's concurrency settings.

Run:  python -m pytest tests/test_db.py
"""
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from app import config

_tmp = tempfile.TemporaryDirectory()
config.DB_PATH = Path(_tmp.name) / "test.db"

from app import db  # noqa: E402  (after DB_PATH override)

db.init()


def test_wal_is_enabled():
    """WAL is what stops a page render from locking out an alert write."""
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_is_set():
    with db.connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_writer_waits_instead_of_failing():
    """The behaviour that matters: with four processes writing, a collision
    should retry, not raise `database is locked` at whichever loses."""
    started = threading.Event()
    done = threading.Event()

    def hold_the_lock():
        # A separate connection takes a write lock and keeps it briefly.
        other = sqlite3.connect(config.DB_PATH, timeout=5.0)
        other.execute("PRAGMA busy_timeout=5000")
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT OR REPLACE INTO meta VALUES ('lockprobe','1')")
        started.set()
        time.sleep(0.4)
        other.commit()
        other.close()
        done.set()

    t = threading.Thread(target=hold_the_lock)
    t.start()
    started.wait(2)

    began = time.time()
    db.set_meta("lockprobe2", "ok")      # must block, then succeed
    waited = time.time() - began

    t.join()
    assert done.is_set()
    assert db.get_meta("lockprobe2") == "ok"
    assert waited < 5.0, "write did not complete within the busy timeout"


def test_pragma_failure_does_not_break_connect(monkeypatch=None):
    """An old SQLite or a filesystem that won't do WAL must degrade, not crash."""
    real_connect = sqlite3.connect

    class NoPragma:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("PRAGMA"):
                raise sqlite3.Error("pragmas unavailable here")
            return self._inner.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    import app.db as dbmod
    orig = dbmod.sqlite3.connect
    dbmod.sqlite3.connect = lambda *a, **k: NoPragma(real_connect(*a, **k))
    try:
        with dbmod.connect() as conn:
            conn.execute("SELECT 1")
    finally:
        dbmod.sqlite3.connect = orig
