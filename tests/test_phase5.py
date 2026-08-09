"""Phase 5 tail: settings overrides, the nightly job, and launchd agents.

The three share a theme — each one is a place where being slightly wrong is
invisible until it costs you. A setting that silently doesn't apply, a backup
that opens fine and is missing yesterday, a plist that runs the system Python.
"""
from __future__ import annotations

import os
import plistlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import config, db, maintenance, service, settings


@pytest.fixture(autouse=True)
def clean_settings():
    db.init()
    for spec in settings.SETTINGS:
        db.set_meta(settings.PREFIX + spec.key, "")
    yield
    for spec in settings.SETTINGS:
        db.set_meta(settings.PREFIX + spec.key, "")


# =========================================================== settings layers
def test_override_beats_env():
    settings.set_value("RISK_PCT", "0.5")
    assert config.get("RISK_PCT") == "0.5"
    assert settings.source("RISK_PCT") == "override"


def test_clearing_reverts_rather_than_freezing_the_current_value():
    """Storing .env's value on revert would let the two copies drift the next
    time .env changes — the override would keep answering with a stale copy."""
    settings.set_value("RISK_PCT", "0.5")
    settings.clear("RISK_PCT")
    assert settings.override("RISK_PCT") is None
    assert settings.source("RISK_PCT") != "override"


def test_env_shows_as_env(monkeypatch):
    monkeypatch.setenv("MAX_POSITIONS", "7")
    assert settings.source("MAX_POSITIONS") == "env"


def test_unset_shows_as_default(monkeypatch):
    monkeypatch.delenv("BACKUP_KEEP_DAYS", raising=False)
    assert settings.source("BACKUP_KEEP_DAYS") == "default"
    assert config.get("BACKUP_KEEP_DAYS") == "14"


def test_config_get_survives_a_missing_database(monkeypatch):
    """config.get runs during startup, before the DB necessarily exists. If
    the override lookup can raise, nothing starts."""
    def boom(_key):
        raise RuntimeError("no such table")

    monkeypatch.setattr(db, "get_meta", boom)
    assert config.get("RISK_PCT")            # must not raise


# ----------------------------------------------------------------- validation
def test_rejects_out_of_range():
    with pytest.raises(settings.SettingError):
        settings.set_value("RISK_PCT", "999")
    with pytest.raises(settings.SettingError):
        settings.set_value("RISK_PCT", "0")


def test_rejects_non_numbers():
    with pytest.raises(settings.SettingError):
        settings.set_value("ACCOUNT_SIZE", "half a lakh")


def test_rejects_unknown_choice():
    with pytest.raises(settings.SettingError):
        settings.set_value("NEWS_CURATOR", "gpt")
    assert settings.set_value("NEWS_CURATOR", "ollama") == "ollama"


def test_rejects_keys_that_are_not_editable():
    """The allowlist is the security boundary — an unlisted key must not become
    settable just because someone posts it."""
    for key in ("NTFY_TOPIC", "KITE_API_SECRET", "NTFY_CMD_PIN"):
        with pytest.raises(settings.SettingError):
            settings.set_value(key, "anything")


def test_credentials_are_not_in_the_editable_set():
    editable = set(settings.BY_KEY)
    assert not editable & {"KITE_API_KEY", "KITE_API_SECRET", "NTFY_TOPIC",
                           "NTFY_TOKEN", "NTFY_CMD_PIN", "ANTHROPIC_API_KEY"}


def test_blank_is_rejected_not_stored_as_empty():
    with pytest.raises(settings.SettingError):
        settings.set_value("RISK_PCT", "  ")


def test_integers_stay_integers():
    """`5.0` in a form that says 'max open positions' looks like a bug."""
    assert settings.set_value("MAX_POSITIONS", "5") == "5"


# --------------------------------------------------------------- whole form
def test_apply_all_writes_nothing_when_one_field_is_bad():
    """A half-saved form leaves a config you didn't ask for and no way back."""
    before = config.get("MAX_POSITIONS")
    res = settings.apply_all({"MAX_POSITIONS": "9", "RISK_PCT": "999"})
    assert res["ok"] is False
    assert "RISK_PCT" in res["errors"]
    assert config.get("MAX_POSITIONS") == before


def test_apply_all_saves_everything_when_valid():
    res = settings.apply_all({"MAX_POSITIONS": "9", "RISK_PCT": "0.75"})
    assert res["ok"] is True
    assert sorted(res["saved"]) == ["MAX_POSITIONS", "RISK_PCT"]
    assert config.get("RISK_PCT") == "0.75"


def test_effective_reports_the_fallback_for_every_row():
    """The page shows 'revert to X' — X has to be real, or revert is a leap."""
    missing = [r["key"] for r in settings.effective() if r["env_value"] == ""]
    assert not missing, f"no visible fallback for: {missing}"


# ================================================================ maintenance
def test_sqlite_backup_is_a_consistent_snapshot(tmp_path, monkeypatch):
    """Copied through the online backup API, not `cp`.

    In WAL mode the .db file alone is incomplete — recent commits sit in -wal
    until a checkpoint — so a file copy can produce a database that opens fine
    and is missing the last trades you logged.
    """
    db.init()
    db.set_meta("canary", "written-just-now")     # lands in the WAL
    dest = tmp_path / "backup"
    dest.mkdir()
    maintenance.backup_sqlite(dest)

    copied = sqlite3.connect(dest / config.DB_PATH.name)
    got = copied.execute("SELECT value FROM meta WHERE key='canary'").fetchone()
    copied.close()
    assert got and got[0] == "written-just-now"


def test_duckdb_backup_copies_the_store(tmp_path, monkeypatch):
    from app import candles
    monkeypatch.setattr(candles, "DB", tmp_path / "c.duckdb")
    candles._conn().close()
    dest = tmp_path / "backup"
    dest.mkdir()
    out = maintenance.backup_duckdb(dest)
    assert out and (dest / "c.duckdb").exists()


def test_duckdb_backup_is_skipped_when_absent(tmp_path, monkeypatch):
    from app import candles
    monkeypatch.setattr(candles, "DB", tmp_path / "nope.duckdb")
    assert maintenance.backup_duckdb(tmp_path) is None


def test_prune_removes_only_old_dated_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path)
    today = datetime.now().date()
    old = (today - timedelta(days=40)).strftime("%Y-%m-%d")
    recent = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    for name in (old, recent, "notes", "2020-13-45"):
        (tmp_path / name).mkdir()

    removed = maintenance.prune_backups(keep_days=14)

    assert removed == [old]
    assert (tmp_path / recent).exists()
    assert (tmp_path / "notes").exists(), "pruned a folder that wasn't ours"
    assert (tmp_path / "2020-13-45").exists(), "pruned an unparseable name"


def test_topup_survives_one_bad_symbol():
    """A delisted name shouldn't cost you the rest of the run."""
    class Kite:
        pass

    from app import instruments
    real = instruments.lookup
    try:
        instruments.lookup = lambda s: None
        out = maintenance.topup_candles(Kite(), ["GHOST"])
    finally:
        instruments.lookup = real
    assert out["updated"] == 0
    assert "GHOST" in out["failed"]


def test_run_dry_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "b")
    r = maintenance.run(dry=True)
    assert r["dry"] is True
    assert not (tmp_path / "b").exists()


def test_run_backs_up_before_topping_up(tmp_path, monkeypatch):
    """Order matters if it gets interrupted: a run that dies halfway should
    still have left a good copy."""
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "b")
    order = []
    monkeypatch.setattr(maintenance, "backup_sqlite",
                        lambda d: order.append("sqlite") or {"file": "x", "bytes": 1})
    monkeypatch.setattr(maintenance, "backup_duckdb",
                        lambda d: order.append("duckdb"))
    monkeypatch.setattr(maintenance, "topup_candles",
                        lambda *a, **k: order.append("candles") or
                        {"updated": 0, "failed": {}})
    monkeypatch.setattr(maintenance, "_watchlist_symbols", lambda: ["BEL"])
    maintenance.run(kite=object(), skip_candles=False)
    assert order.index("sqlite") < order.index("candles")


def test_run_records_errors_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "b")

    def boom(_d):
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "backup_sqlite", boom)
    r = maintenance.run(skip_candles=True)
    assert any("disk full" in e for e in r["errors"])


def test_run_without_a_session_still_backs_up(tmp_path, monkeypatch):
    """03:00 is after the token died and before anyone logged in. The backup
    is the part that matters and it needs no session."""
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "b")
    r = maintenance.run(kite=None, skip_candles=False)
    assert r.get("sqlite")
    assert any("no Kite session" in e for e in r["errors"])


def test_format_report_is_short():
    r = maintenance.run(dry=True)
    assert len(maintenance.format_report(r).splitlines()) <= 8


# =================================================================== launchd
def test_plists_are_valid_and_use_this_interpreter(tmp_path):
    """launchd has no shell profile, so a bare `python` finds the system one
    and none of the dependencies."""
    res = service.install(directory=tmp_path)
    assert res["supported"]
    for path in res["paths"]:
        d = plistlib.load(open(path, "rb"))
        assert d["ProgramArguments"][0] == service._python()
        assert d["ProgramArguments"][0].startswith("/")


def test_up_agent_restarts_and_throttles(tmp_path):
    d = service.up_plist()
    assert d["KeepAlive"] is True
    assert d["RunAtLoad"] is True
    assert d["ThrottleInterval"] >= 10, \
        "a supervisor that can't start would be relaunched in a tight loop"


def test_maintenance_agent_is_scheduled_not_kept_alive():
    d = service.maintenance_plist(hour=4)
    assert d["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert d["RunAtLoad"] is False
    assert "KeepAlive" not in d, "a nightly job kept alive would run forever"


def test_agents_are_separate():
    """A nightly job that wedges must not be able to take the alert engine
    down with it."""
    assert service.up_plist()["Label"] != service.maintenance_plist()["Label"]


def test_environment_is_explicit(tmp_path):
    """launchd starts with almost nothing — HOME matters because the Kite
    token lives under it."""
    d = service.up_plist()
    env = d["EnvironmentVariables"]
    assert env["HOME"] == str(Path.home())
    assert "PATH" in env


def test_install_does_not_load_anything(tmp_path):
    """Writing a plist is reversible; registering a background service without
    being asked is not."""
    res = service.install(directory=tmp_path)
    assert all("launchctl" in c for c in res["commands"])
    text = service.format_install(res)
    assert "Nothing is running yet" in text
    assert "unload" in text


def test_non_darwin_gets_real_advice(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    res = service.install()
    assert not res["supported"]
    assert "systemd" in res["note"] and "cron" in res["note"]
