"""Render a clean intraday chart to PNG bytes for a phone (ntfy attachment).

Deliberately minimal: candles + VWAP + the handful of levels that matter, with
the triggered level highlighted. A picture you can read at a glance beats a web
UI the office network can't reach.
"""
from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")                    # headless
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

# GitHub-dark palette, matching the app.
BG = "#0e1116"; PANEL = "#161b22"; GRID = "#2d333b"; FG = "#e6edf3"
DIM = "#8b949e"; UP = "#26a69a"; DOWN = "#ef5350"; VWAP_C = "#e4a541"
ACCENT = "#4184e4"


def _hline(ax, y, color, label, x1, lw=1.0, ls="-", alpha=0.9):
    if y is None:
        return
    ax.axhline(y, color=color, lw=lw, ls=ls, alpha=alpha)
    ax.annotate(f"{label} {y:,.1f}", xy=(x1, y), xytext=(4, 0),
                textcoords="offset points", va="center", ha="left",
                fontsize=7.5, color=color)


def intraday_png(lv: dict, candles: list[dict], highlight: float | None = None,
                 subtitle: str = "") -> bytes:
    """lv: an intraday.levels() dict. candles: today's [{time,o,h,l,c}]."""
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    fig.patch.set_facecolor(BG); ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=7.5)
    ax.grid(True, color=GRID, lw=0.4, alpha=0.5)

    n = len(candles)
    if n:
        w = 0.6
        for i, c in enumerate(candles):
            up = c["close"] >= c["open"]
            col = UP if up else DOWN
            ax.plot([i, i], [c["low"], c["high"]], color=col, lw=0.8, zorder=2)
            lo = min(c["open"], c["close"]); hi = max(c["open"], c["close"])
            ax.add_patch(Rectangle((i - w / 2, lo), w, max(hi - lo, 1e-6),
                                   facecolor=col, edgecolor=col, zorder=3))
        xr = n - 0.5
        ax.set_xlim(-0.8, n * 1.12)      # room for right-edge labels
    else:
        xr = 1
        ax.set_xlim(0, 1)
        ax.text(0.5, 0.5, "no intraday candles yet", color=DIM,
                ha="center", va="center", transform=ax.transAxes)

    piv = lv.get("pivots") or {}
    _hline(ax, lv.get("vwap"), VWAP_C, "VWAP", xr, lw=1.3)
    _hline(ax, lv.get("pdh"), DIM, "PDH", xr, ls="--")
    _hline(ax, lv.get("pdl"), DIM, "PDL", xr, ls="--")
    _hline(ax, piv.get("P"), ACCENT, "P", xr, alpha=0.7)
    _hline(ax, piv.get("TC"), ACCENT, "TC", xr, ls=":", alpha=0.5)
    _hline(ax, piv.get("BC"), ACCENT, "BC", xr, ls=":", alpha=0.5)
    orb = lv.get("orb") or {}
    _hline(ax, orb.get("high"), "#9db8e4", "ORBh", xr, ls="-.", alpha=0.6)
    _hline(ax, orb.get("low"), "#9db8e4", "ORBl", xr, ls="-.", alpha=0.6)
    if highlight is not None:
        ax.axhline(highlight, color="#ffffff", lw=1.4, alpha=0.9, zorder=5)

    ltp = lv.get("ltp"); cp = lv.get("chg_pct")
    t = f"{lv['symbol']}  {ltp:,.2f}" if ltp is not None else lv["symbol"]
    if cp is not None:
        t += f"   {'+' if cp >= 0 else ''}{cp:.2f}%"
    ax.set_title(t, color=FG, fontsize=11, fontweight="bold", loc="left")
    if subtitle:
        ax.set_title(subtitle, color=DIM, fontsize=8, loc="right")
    ax.set_xticks([])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
