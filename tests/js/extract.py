"""Pull JS blocks out of index.html by brace matching, for the node tests.

Regexes over 3000 lines of HTML break every time the file is touched; brace
matching only breaks if the function it names actually goes away.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app" / "web" / "index.html").read_text()


def block(marker: str) -> str:
    i = HTML.index(marker)
    depth = 0
    for k in range(HTML.index("{", i), len(HTML)):
        if HTML[k] == "{":
            depth += 1
        elif HTML[k] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:k + 1]
    raise ValueError(f"unterminated block: {marker}")


HELPERS = re.search(
    r"const _cl = \(c\)[\s\S]*?_tp = \(c\) => c\.map\(\(x\) => "
    r"\(x\.high \+ x\.low \+ x\.close\) / 3\)", HTML).group(0)

BLOCKS = [
    # chart colours: extracted functions read PAL.up / PAL.down at call time
    block("const PAL = {"),
    HELPERS,
    block("function _sma(d, p)"),
    block("function _ema(d, p)"),
    block("function _std(d, p)"),
    block("function supertrend(data, period = 10, mult = 3)"),
    block("function I_super(c, p)"),
    block("function distToSegment(px, py, x1, y1, x2, y2)"),
    block("function distToRay(px, py, x1, y1, x2, y2)"),
    block("function distToRect(px, py, x1, y1, x2, y2)"),
    block("function fibLevels(p0, p1)"),
    block("function hitDistance(kind, pts, x, y, width)"),
    block("function nearestHandle(pts, x, y)"),
    block("function sparkPath(closes, w, h)"),
    block("function indexTo100(closes)"),
    block("function heikinAshi(c)"),
    block("function weekKey(ts)"),
    block("function monthKey(ts)"),
    block("function resample(c, keyFn)"),
    block("function applyTimeframe(c, tf)"),
    "const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]",
    "const DRAW_HANDLE_PX = 5",
    block("function _rsiSeries(closes, period)"),
    block("function _crossEvents("),
    block("function _atr(c, p)"),
    block("function _pts(c, arr)"),
    block("function obSwings(c, L = 2, R = 2)"),
    block("function obScan(c, opt)"),
    block("function obPrevDay(c)"),
    block("function I_obliq(c, p)"),
    block("const EVENTS = {"),
]

if __name__ == "__main__":
    sys.stdout.write("\n".join(b.replace("\t", "  ") for b in BLOCKS))
