"""Chart drawings — trendlines, rays, horizontal lines, rectangles, Fibs.

Everything here is anchored in **data space**: a point is a unix timestamp and
a price, never an x/y pixel. That's the one decision the rest of the feature
hangs off. A drawing stored in pixels is correct at exactly one zoom level, one
window size and one screen; the moment any of those change it's pointing at a
different bar and quietly lying. Anchoring in data means the browser converts
to pixels at paint time and a trendline drawn on a laptop lands on the same
bars on a phone.

Drawings are keyed by symbol AND interval. A trendline across daily bars means
nothing on a 5-minute chart — the levels it connects aren't levels there — so
showing it would be worse than not persisting it at all.

Validation lives here rather than in the browser because the browser isn't the
only client: the API is open on loopback, and a drawing with three points on a
two-point shape would break the renderer at paint time, which is the worst
place to discover it.
"""
from __future__ import annotations

import json

from . import db

# kind -> how many anchor points it needs. A rectangle and a Fib are both
# defined by two opposite corners; the renderer decides what to do with them.
SHAPES = {
    "trendline": 2,     # segment between two points
    "ray": 2,           # starts at p0, continues past p1 for ever
    "hline": 1,         # horizontal, spans the whole chart
    "rect": 2,          # opposite corners
    "fib": 2,           # retracement anchored high→low or low→high
}

# Standard retracement levels. 0.5 isn't a Fibonacci ratio at all — it's in
# every charting package by convention because price respects halves, and
# leaving it out would surprise everyone.
FIB_LEVELS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)

MAX_PER_CHART = 200          # a runaway loop shouldn't be able to fill the DB


class DrawingError(ValueError):
    pass


def _validate_points(kind: str, points) -> list[dict]:
    want = SHAPES[kind]
    if not isinstance(points, list) or len(points) != want:
        raise DrawingError(
            f"{kind} needs exactly {want} point{'s' if want > 1 else ''}, "
            f"got {len(points) if isinstance(points, list) else type(points).__name__}")
    out = []
    for pt in points:
        if not isinstance(pt, dict) or "t" not in pt or "p" not in pt:
            raise DrawingError("each point needs a time `t` and a price `p`")
        try:
            t, p = int(pt["t"]), float(pt["p"])
        except (TypeError, ValueError):
            raise DrawingError("point times must be integers and "
                               "prices numbers") from None
        if p <= 0:
            raise DrawingError("prices must be positive")
        out.append({"t": t, "p": p})
    return out


def _validate_style(style) -> dict:
    if style is None:
        return {}
    if not isinstance(style, dict):
        raise DrawingError("style must be an object")
    out = {}
    colour = style.get("color")
    if colour is not None:
        if not isinstance(colour, str) or not colour.startswith("#") \
                or len(colour) not in (4, 7, 9):
            raise DrawingError("color must be a hex string like #4fc3f7")
        out["color"] = colour
    width = style.get("width")
    if width is not None:
        try:
            width = int(width)
        except (TypeError, ValueError):
            raise DrawingError("width must be a number") from None
        if not 1 <= width <= 8:
            raise DrawingError("width must be between 1 and 8")
        out["width"] = width
    if style.get("dash"):
        out["dash"] = True
    return out


# ------------------------------------------------------------------ reading
def for_chart(symbol: str, interval: str) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, kind, points, style FROM drawings "
            "WHERE symbol=? AND interval=? ORDER BY id",
            [symbol.upper(), interval]).fetchall()
    return [{"id": r[0], "kind": r[1], "points": json.loads(r[2]),
             "style": json.loads(r[3] or "{}")} for r in rows]


def count(symbol: str, interval: str) -> int:
    with db.connect() as c:
        return c.execute(
            "SELECT count(*) FROM drawings WHERE symbol=? AND interval=?",
            [symbol.upper(), interval]).fetchone()[0]


# ------------------------------------------------------------------ writing
def add(symbol: str, interval: str, kind: str, points, style=None) -> dict:
    if kind not in SHAPES:
        raise DrawingError(f"unknown kind '{kind}'. "
                           f"One of: {', '.join(sorted(SHAPES))}")
    symbol = symbol.upper()
    if count(symbol, interval) >= MAX_PER_CHART:
        raise DrawingError(
            f"{MAX_PER_CHART} drawings on this chart already — clear some "
            f"before adding more")
    pts = _validate_points(kind, points)
    st = _validate_style(style)
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO drawings (symbol, interval, kind, points, style) "
            "VALUES (?,?,?,?,?)",
            [symbol, interval, kind, json.dumps(pts), json.dumps(st)])
        new_id = cur.lastrowid
    return {"id": new_id, "kind": kind, "points": pts, "style": st}


def move(drawing_id: int, points, style=None) -> dict:
    """Re-anchor an existing drawing. Used by every drag."""
    with db.connect() as c:
        row = c.execute("SELECT kind FROM drawings WHERE id=?",
                        [drawing_id]).fetchone()
    if row is None:
        raise DrawingError(f"no drawing {drawing_id}")
    kind = row[0]
    pts = _validate_points(kind, points)
    st = _validate_style(style) if style is not None else None
    with db.connect() as c:
        if st is None:
            c.execute("UPDATE drawings SET points=?, "
                      "updated_at=datetime('now','localtime') WHERE id=?",
                      [json.dumps(pts), drawing_id])
        else:
            c.execute("UPDATE drawings SET points=?, style=?, "
                      "updated_at=datetime('now','localtime') WHERE id=?",
                      [json.dumps(pts), json.dumps(st), drawing_id])
    return {"id": drawing_id, "kind": kind, "points": pts}


def remove(drawing_id: int) -> int:
    with db.connect() as c:
        return c.execute("DELETE FROM drawings WHERE id=?",
                         [drawing_id]).rowcount


def clear(symbol: str, interval: str | None = None) -> int:
    """Wipe a chart. Interval omitted clears every timeframe for the symbol."""
    with db.connect() as c:
        if interval:
            return c.execute(
                "DELETE FROM drawings WHERE symbol=? AND interval=?",
                [symbol.upper(), interval]).rowcount
        return c.execute("DELETE FROM drawings WHERE symbol=?",
                         [symbol.upper()]).rowcount


# ------------------------------------------------------------------ helpers
def fib_prices(p0: float, p1: float) -> list[dict]:
    """Retracement levels between two anchors.

    0 sits at the FIRST anchor and 1 at the second, so dragging high→low and
    low→high give mirrored ladders — which is what you want, because the
    direction you drew in encodes which way you think the move went.
    """
    span = p1 - p0
    return [{"level": lv, "price": round(p0 + span * lv, 4)}
            for lv in FIB_LEVELS]


def summary(symbol: str, interval: str) -> str:
    """For the phone and the CLI."""
    rows = for_chart(symbol, interval)
    if not rows:
        return f"No drawings on {symbol.upper()} {interval}."
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    bits = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    return f"{symbol.upper()} {interval}: {bits}"
