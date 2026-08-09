"""The UI half: controls that match the page, and a reference you can search.

Two problems, both of which got worse as the query engine grew.

**White controls.** `app.css` styled `input.search` and `select.sel` — by
class. Every control that didn't carry the class fell back to the operating
system's widget, which on a dark page means a white dropdown: the universe
picker here, the ATR and stop-loss inputs on the backtest page, and about
forty others. Styling by element instead means a new control is themed by
default rather than when someone remembers.

**The reference panel.** It was two headings and one continuous scroll. That
was fine at twenty fields; the catalogue is now around a hundred and forty
across seven kinds, and fifty examples across eleven groups, so finding `cmf`
meant reading past everything alphabetically before it.
"""
from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "app" / "web"
CSS = (WEB / "app.css").read_text()


def pages():
    return sorted(WEB.glob("*.html"))


# =============================================================== the controls
def test_controls_are_styled_by_element_not_by_class():
    """The whole fix. A rule that only reaches `.sel` is a rule that misses
    every control someone forgot to label."""
    block = CSS[CSS.index("/* ---- controls"):]
    assert re.search(r"^select, textarea,", block, re.M), \
        "select/textarea are still only styled through a class"
    assert "input:not([type=checkbox])" in block


def test_the_page_declares_itself_dark():
    """`color-scheme: dark` is the only thing that reaches what CSS can't —
    the popup list a <select> opens, the date picker, autofill backgrounds
    and scrollbars. Without it those stay white however well the control
    itself is styled."""
    assert "color-scheme: dark" in CSS


def test_checkboxes_and_ranges_are_not_given_a_text_box():
    """They'd become 40px-wide bordered squares. The :not() chain is load
    bearing, so it gets a test rather than a comment."""
    block = CSS[CSS.index("/* ---- controls"):]
    rule = block[:block.index("}")]
    for kind in ("checkbox", "radio", "range", "file"):
        assert f":not([type={kind}])" in rule, kind


def test_the_select_arrow_is_replaced():
    """The native arrow can't be recoloured, so a themed select with a stock
    arrow still reads as an OS widget dropped onto the page."""
    assert "appearance:none" in CSS
    assert "data:image/svg+xml" in CSS


def test_number_spinners_are_removed():
    """Tiny, white, and unusable on a dark panel — and every numeric field on
    the backtest page has them."""
    assert "-webkit-inner-spin-button" in CSS
    assert "-moz-appearance:textfield" in CSS


def test_checkboxes_use_the_accent_colour():
    assert "accent-color:var(--accent)" in CSS


def test_no_page_hardcodes_a_light_control():
    """A single inline `background:#fff` would survive every rule above."""
    offenders = []
    for f in pages():
        for m in re.finditer(r"<(select|input|textarea)[^>]*style=\"([^\"]*)\"",
                             f.read_text()):
            if re.search(r"background\s*:\s*(#fff|#ffffff|white)", m.group(2),
                         re.I):
                offenders.append(f.name)
    assert not offenders, offenders


def test_every_page_loads_the_shared_stylesheet():
    """A page that doesn't is a page where all of the above is untrue."""
    missing = [f.name for f in pages() if "/app.css" not in f.read_text()]
    assert not missing, missing


# ========================================================= the reference panel
QUERY = (WEB / "query.html").read_text()


def test_the_panel_has_tabs_and_a_filter():
    assert 'id="reffilter"' in QUERY
    assert 'data-tab="examples"' in QUERY and 'data-tab="fields"' in QUERY


def test_the_flat_headings_are_gone():
    """If both the old headings and the new tabs survive a merge, the panel
    renders twice and neither is obviously wrong."""
    assert "<h3>Examples</h3>" not in QUERY
    assert "<h3>Fields</h3>" not in QUERY


def test_groups_are_collapsible():
    assert "refgroup" in QUERY and "<details" in QUERY


def test_filtering_opens_the_groups():
    """A collapsed group holding the only match looks exactly like no match
    at all — the search would appear broken precisely when it worked."""
    assert re.search(r"!!q", QUERY), "groups don't open while filtering"


def test_the_panel_scrolls_on_its_own():
    """Sticky with its own overflow, so the results and the reference scroll
    independently. Otherwise a 140-row reference drags the page down past the
    results you ran the query for."""
    assert "position: sticky" in QUERY
    assert "overflow-y: auto" in QUERY


def test_every_single_field_lands_in_a_section():
    """Not "every kind" — every ENTRY. Checking kinds is the weaker test and
    it let 36 statement shorthands fall through a gap between the sections
    while still passing, because their kind was listed and their group
    wasn't. A field in no section is a field nobody can discover.
    """
    from app import query as Q

    listed = set(re.findall(r'\t*\["([^"]+)", "', QUERY))
    homeless = [f["name"] for f in Q.fields()
                if (f.get("group") or f.get("kind") or "field") not in listed]
    assert not homeless, f"not shown in the panel: {homeless}"


def test_the_sections_are_small_enough_to_read():
    """The panel exists because a hundred-and-forty-row list isn't a
    reference. A section holding most of the catalogue would rebuild it."""
    from app import query as Q
    import collections

    sizes = collections.Counter(
        f.get("group") or f.get("kind") or "field" for f in Q.fields())
    biggest, n = sizes.most_common(1)[0]
    assert n <= 45, f"{biggest} holds {n} entries — split it"


# ================================================================= the data
def test_the_grouped_examples_cover_the_flat_list():
    """EXAMPLES is derived from EXAMPLE_GROUPS rather than kept beside it —
    two lists of the same thing drift within a month."""
    from app import query as Q

    flat = [pair for _t, items in Q.EXAMPLE_GROUPS for pair in items]
    assert flat == Q.EXAMPLES


def test_every_group_has_a_title_and_examples():
    from app import query as Q
    for title, items in Q.EXAMPLE_GROUPS:
        assert title.strip() and items, title


def test_the_endpoint_serves_the_groups():
    from app import server

    got = server.api_query_fields()
    assert got["example_groups"]
    served = [(i["query"], i["means"])
              for g in got["example_groups"] for i in g["items"]]
    assert served == [(q, m) for q, m in __import__(
        "app.query", fromlist=["query"]).EXAMPLES]


def test_the_flat_list_is_still_served():
    """Anything already reading `examples` keeps working."""
    from app import server
    assert server.api_query_fields()["examples"]
