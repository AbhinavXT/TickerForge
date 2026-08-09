"""Tests for the fundamentals slug override and the fetch diagnostic."""
import tempfile
from pathlib import Path
from unittest import mock

from app import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "fund.db"

from app import db, fundamentals as F  # noqa: E402

db.init()

FULL = {
    "snapshot": {"Market Cap": "1"},
    "tables": {s: {"periods": ["2024"], "rows": {
        "Sales": [100], "Net Profit": [10], "Profit before tax": [12],
        "Interest": [1], "OPM %": [20], "Equity Capital": [5],
        "Reserves": [45], "Borrowings": [10], "ROCE %": [18]}}
        for s in F.SECTIONS},
}


def test_slug_defaults_to_the_symbol():
    assert F.slug_for("BEL") == "BEL"


def test_slug_override_round_trip():
    F.set_slug("ODDCO", "543210")
    assert F.slug_for("ODDCO") == "543210"
    assert F.slug_for("oddco") == "543210"


def test_slug_override_is_used_in_the_url():
    """The whole point: a symbol whose screener code differs must still fetch."""
    seen = []

    class R:
        status_code = 200
        text = "<html>top-ratios</html>"

    F.set_slug("ODDCO", "543210")
    with mock.patch.object(F.requests, "get",
                           side_effect=lambda u, **k: seen.append(u) or R()):
        F.fetch_html("ODDCO")
    assert "543210" in seen[0] and "ODDCO" not in seen[0]


def test_diagnose_names_the_http_stage_when_fetch_fails():
    with mock.patch.object(F, "fetch_html",
                           side_effect=RuntimeError("HTTP 404 for …")):
        d = F.diagnose("NOPE")
    http = [s for s in d["stages"] if s["stage"] == "http"][0]
    assert http["ok"] is False
    assert "could not be fetched" in d["verdict"]
    assert "fund slug NOPE" in d["fix"]        # tells you the actual fix


def test_diagnose_flags_a_layout_change():
    with mock.patch.object(F, "fetch_html", return_value=("<html/>", "consolidated")), \
         mock.patch.object(F, "parse", return_value={"snapshot": {}, "tables": {}}):
        d = F.diagnose("NOPE")
    assert "no statement tables parsed" in d["verdict"]
    assert "layout" in d["fix"]


def test_diagnose_flags_missing_ratio_rows():
    partial = {"snapshot": {"Market Cap": "1"},
               "tables": {"profit-loss": {"periods": ["2024"],
                                          "rows": {"Sales": [100]}}}}
    with mock.patch.object(F, "fetch_html", return_value=("<html/>", "consolidated")), \
         mock.patch.object(F, "parse", return_value=partial):
        d = F.diagnose("NOPE")
    inputs = [s for s in d["stages"] if s["stage"] == "ratio-inputs"][0]
    assert inputs["ok"] is False
    assert "balance-sheet/Reserves" in inputs["detail"]


def test_diagnose_reports_healthy():
    F.store("GOODCO", FULL, "consolidated")
    with mock.patch.object(F, "fetch_html", return_value=("<html/>", "consolidated")), \
         mock.patch.object(F, "parse", return_value=FULL):
        d = F.diagnose("GOODCO")
    assert all(s["ok"] for s in d["stages"]), d["stages"]
    assert "healthy" in d["verdict"]


def test_compute_produces_ratios_from_a_full_parse():
    F.store("GOODCO", FULL, "consolidated")
    r = F.compute("GOODCO")
    assert r["ROE %"] == 20.0                  # 10 / (5+45)
    assert r["Debt / Equity"] == 0.2
    assert r["ROCE % (screener)"] == 18


def test_failed_autofetch_pushes_to_the_phone():
    """A background fetch that fails must not die in a terminal nobody reads."""
    from app import watchlist
    pushed = []
    with mock.patch.object(F, "fetch_and_store",
                           side_effect=RuntimeError("HTTP 404")), \
         mock.patch("app.notify.send", lambda m, **k: pushed.append(m)):
        watchlist.ensure_fundamentals("NOPE", background=False)
    assert pushed and "NOPE" in pushed[0] and "404" in pushed[0]


# ------------------------------------------------- summary() ↔ consumers
def test_summary_shape_is_what_consumers_expect():
    """The contract. /fundamentals once read s['ratios']['pe'] — keys that have
    never existed — so it reported 'no ratio fields stored' for every symbol
    regardless of data. Pin the real shape so that can't recur silently."""
    F.store("SHAPECO", FULL, "consolidated")
    s = F.summary("SHAPECO")
    assert set(s) >= {"symbol", "variant", "fetched_at", "stale",
                      "snapshot", "computed", "quarters"}
    assert "ratios" not in s, "no such key — consumers must read snapshot/computed"
    assert isinstance(s["snapshot"], dict) and isinstance(s["computed"], dict)


def test_fundamentals_command_renders_stored_data():
    from app import commands
    F.store("SHAPECO", FULL, "consolidated")
    with mock.patch.object(commands, "_ensure_fund", lambda x: ("SHAPECO", None)), \
         mock.patch.object(commands, "fundamentals", F):
        out = commands.handle("/fundamentals SHAPECO")
    assert "no ratio fields" not in out
    assert "Market Cap" in out                    # from the snapshot
    assert "ROE %" in out                         # from computed
    assert "consolidated" in out


def test_fundamentals_command_points_at_the_diagnostic_when_empty():
    from app import commands
    with mock.patch.object(commands, "_ensure_fund", lambda x: ("VOIDCO", None)), \
         mock.patch.object(commands, "fundamentals", F):
        out = commands.handle("/fundamentals VOIDCO")
    assert "diagnose" in out


def test_fundamentals_command_surfaces_unexpected_snapshot_fields():
    """If screener adds a header field, show it rather than dropping it."""
    from app import commands
    odd = {**FULL, "snapshot": {"Market Cap": "₹ 1 Cr.", "PEG Ratio": "1.4"}}
    F.store("ODDFIELD", odd, "consolidated")
    with mock.patch.object(commands, "_ensure_fund", lambda x: ("ODDFIELD", None)), \
         mock.patch.object(commands, "fundamentals", F):
        out = commands.handle("/fundamentals ODDFIELD")
    assert "PEG Ratio: 1.4" in out


def test_fundamentals_command_flags_stale_data():
    from app import commands
    F.store("STALECO", FULL, "consolidated")
    with db.connect() as c:
        c.execute("UPDATE fund_snapshot SET fetched_at='2020-01-01T00:00:00' "
                  "WHERE symbol='STALECO'")
    with mock.patch.object(commands, "_ensure_fund", lambda x: ("STALECO", None)), \
         mock.patch.object(commands, "fundamentals", F):
        out = commands.handle("/fundamentals STALECO")
    assert "stale" in out
