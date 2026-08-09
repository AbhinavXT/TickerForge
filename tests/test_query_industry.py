"""Industry P/E, and comparing names rather than numbers.

    pe < industry_pe * 0.8 and roe > 15
    industry contains "bank" and pb < 1.5

Screener publishes an industry P/E; this computes one, from the P/Es already
stored for 657 companies. Computing beats scraping here — it refreshes
whenever the fundamentals do, and there's no second number to reconcile with
the first.

The classification itself did have to be scraped, and that half is unverified
against a live page — the same caveat as shareholding, and the same benign
failure mode: no classification stored means `industry_pe` reads blank and
nothing matches, rather than a wrong grouping producing a wrong median.

These are also the engine's first non-numeric values, so there's a small
amount of new language surface: `sector` and `industry` compare with `==`,
`!=` and `contains`, and with nothing else.
"""
from __future__ import annotations

import pytest

from app import query as Q


BASE_TS = 1767571200


def bars(n=60, close=100.0):
    return [{"time": BASE_TS + i * 86400, "open": close, "high": close * 1.01,
             "low": close * 0.99, "close": close, "volume": 1000}
            for i in range(n)]


def ev(q, scalars):
    return Q.evaluate(Q.plan(q).condition, bars(), scalars)


# ================================================== reading the classification
def test_sector_and_industry_are_read_from_the_peers_block():
    from app import fundamentals

    html = """
    <html><body>
      <section id="peers"><h2>Peer comparison</h2>
        <p class="sub">Sector: <a href="/x">Consumer Durables</a>
           Industry: <a href="/y">Consumer Durables - Domestic Appliances</a></p>
      </section>
    </body></html>
    """
    snap = fundamentals.parse(html)["snapshot"]
    assert snap["Sector"] == "Consumer Durables"
    assert snap["Industry"] == "Consumer Durables - Domestic Appliances"


def test_they_ride_the_existing_snapshot_path():
    """Landing in `snapshot` means they store, cache and expire exactly as
    "Stock P/E" does — a new kind of fact with no new schema."""
    from app import fundamentals

    html = ('<html><body><section id="peers">'
            '<p>Sector: Software Industry: IT Services</p>'
            '</section></body></html>')
    assert "Sector" in fundamentals.parse(html)["snapshot"]


def test_a_page_without_a_peers_block_yields_nothing():
    """Absent, not blank. A company filed under "" would join every other
    unclassified name in an industry that doesn't exist, and the median of
    that group would be a real-looking number meaning nothing."""
    from app import fundamentals
    assert fundamentals.parse("<html><body></body></html>")["snapshot"] == {}


def test_a_runaway_match_is_rejected():
    """If the markup changes and the regex swallows a paragraph, that's a
    parse failure wearing the shape of a sector name."""
    from app import fundamentals

    html = ('<html><body><section id="peers"><p>Sector: ' + "x" * 200
            + ' Industry: ' + "y" * 200 + '</p></section></body></html>')
    assert fundamentals.parse(html)["snapshot"] == {}


# ======================================================== the median itself
def test_the_median_is_taken_not_the_mean(monkeypatch):
    """One loss-making name at 400x should not decide what its industry is
    worth."""
    import statistics
    pes = [10.0, 12.0, 14.0, 400.0]
    assert statistics.median(pes) == 13.0
    assert sum(pes) / len(pes) > 100


def test_group_pe_ignores_companies_with_no_earnings(monkeypatch):
    """A negative P/E is not a cheap one, and a company with no earnings has
    no P/E to contribute."""
    from app import fundamentals

    rows = [
        {"symbol": "A", "key": "Industry", "value": "Banks"},
        {"symbol": "A", "key": "Stock P/E", "value": "10"},
        {"symbol": "B", "key": "Industry", "value": "Banks"},
        {"symbol": "B", "key": "Stock P/E", "value": "20"},
        {"symbol": "C", "key": "Industry", "value": "Banks"},
        {"symbol": "C", "key": "Stock P/E", "value": "-5"},
    ]

    class FakeConn:
        def execute(self, *a, **k):
            return self

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.db.connect", lambda: FakeConn())
    got = fundamentals.group_pe()
    assert got["industry"]["Banks"] == 15.0, "the negative P/E was counted"
    assert got["of"]["C"] == (None, "Banks")


def test_the_population_is_everything_stored_not_everything_scanned():
    """An industry P/E that moved when you changed universe would be a
    different number under the same name in two screens, and `pe <
    industry_pe` would quietly mean something different each time. Screener
    publishes it as a fixed figure; so does this.

    Guarded structurally: group_pe() takes no symbol list, so there is nothing
    for a universe to narrow.
    """
    import inspect
    from app import fundamentals
    assert not inspect.signature(fundamentals.group_pe).parameters


# ============================================================ using it
def test_cheap_against_its_industry():
    assert ev("pe < industry_pe * 0.8",
              {"pe": 10.0, "industry_pe": 20.0}) is True
    assert ev("pe < industry_pe * 0.8",
              {"pe": 18.0, "industry_pe": 20.0}) is False


def test_an_unclassified_company_matches_nothing():
    """Absent stays absent: no industry P/E is not a low one."""
    assert ev("pe < industry_pe", {"pe": 5.0}) is False


def test_sector_pe_is_separate_from_industry_pe():
    """A sector is broader than an industry, and conflating them would make
    the comparison meaningless for anything in a narrow niche."""
    sc = {"sector_pe": 25.0, "industry_pe": 12.0, "pe": 15.0}
    assert ev("pe < sector_pe", sc) is True
    assert ev("pe < industry_pe", sc) is False


# ============================================== comparing names
def test_a_name_compares_with_equals():
    assert ev('sector == "Software"', {"sector": "Software"}) is True
    assert ev('sector == "Banks"', {"sector": "Software"}) is False


def test_contains_is_the_one_you_actually_want():
    """Screener's industries are long — "Consumer Durables - Domestic
    Appliances". Exact matching on those is a typing exercise."""
    sc = {"industry": "Consumer Durables - Domestic Appliances"}
    assert ev('industry contains "domestic"', sc) is True
    assert ev('industry contains "pharma"', sc) is False


def test_name_matching_ignores_case():
    assert ev('industry contains "BANK"', {"industry": "Private Banks"})
    assert ev('sector == "software"', {"sector": "Software"}) is True


def test_not_equal_works():
    assert ev('sector != "Banks"', {"sector": "Software"}) is True


def test_an_unclassified_company_is_not_matched_by_negation():
    """`sector != "Banks"` must not sweep in every company whose sector we
    simply don't know. Unknown reads as no, here as everywhere else."""
    assert ev('sector != "Banks"', {}) is False


def test_a_name_cannot_be_compared_with_an_order():
    for bad in ('sector > "Banks"', "industry < 5", "sector >= sma(20)"):
        with pytest.raises(Q.QueryError):
            Q.plan(bad)


def test_a_name_cannot_be_compared_with_a_number():
    with pytest.raises(Q.QueryError):
        Q.plan("industry == 20")


def test_names_compose_with_everything_else():
    sc = {"industry": "Private Banks", "pe": 10.0, "industry_pe": 18.0,
          "roe": 17.0}
    assert ev('industry contains "bank" and pe < industry_pe and roe > 15',
              sc) is True


def test_they_are_fundamentals():
    assert Q._needs_scalars(Q.plan('sector == "Banks"').condition) is True
    assert Q._needs_scalars(Q.plan("pe < industry_pe").condition) is True
    with pytest.raises(Q.QueryError):
        Q.backtest("pe < industry_pe", "INFY")


def test_they_round_trip_and_are_advertised():
    names = {f["name"] for f in Q.fields()}
    for text in ('industry contains "bank"', 'sector == "Software"',
                 "pe < industry_pe * 0.8"):
        assert Q.render_plan(Q.plan(text)) == text
    assert {"industry_pe", "sector_pe", "sector", "industry"} <= names
