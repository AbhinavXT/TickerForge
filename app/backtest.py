"""Backtesting engine v2 — long/flat, end-of-bar execution, no look-ahead.

Design (still deliberately simple and honest):
  * A strategy maps OHLCV -> a target position series in {0,1} (long or flat).
    The position at bar t is decided from information available *at t's close*,
    and earns bar t+1's return. That ordering is what keeps it look-ahead-free.
  * Optional risk overlay, applied on top of any strategy: fixed stop-loss %,
    take-profit %, ATR trailing (chandelier) stop, and a time stop. Risk exits
    are evaluated INTRABAR on the following bars, in the order the market
    actually reveals prices. The open comes first: a bar that *opens* through
    the stop or the target fills at the open — never at a price the market
    skipped over — so a gap up through the target is a target fill even if the
    bar later trades down through the stop. Only when neither gap applies do we
    look inside the bar, where a low piercing the stop fills at the stop price,
    and stop beats target when both sit within one bar (the conservative
    reading; intrabar ordering is unknowable).
    After a risk exit the strategy must go flat once before re-entering,
    otherwise a still-long signal would re-enter on the very next bar.
  * Costs: `cost_bps` charged on every entry and every exit (round trip = 2x).
  * Trade stats count COMPLETED trades only and split wins/losses on the
    post-cost return, so win rate and profit factor can't disagree with
    expectancy about whether a trade made money.
  * Metrics vs a buy-&-hold benchmark on the same window.

The simulator (`simulate`) is a pure function of candles + a position series,
so the whole engine is unit-testable without Kite or the network.

Not modelled (kept for the future algo bot): shorting, leverage, position
sizing, slippage models beyond the flat bps cost, multi-asset portfolios.
"""
from __future__ import annotations

import math

from . import candles, instruments, kite_auth
from . import indicators as ind

# bars per year, for annualising returns/vol/Sharpe
_BPY = {"minute": 252*375, "3minute": 252*125, "5minute": 252*75,
        "10minute": 252*37, "15minute": 252*25, "30minute": 252*13,
        "60minute": 252*7, "day": 252}


# ------------------------------------------------- extra indicator helpers
# (sma/ema/rsi/macd/atr/bollinger/supertrend/adx come from app.indicators —
#  the same tested maths the alert engine uses, so backtests and live alerts
#  can never disagree about what an indicator says.)

def _stoch(c, k_period: int, d_period: int):
    """Stochastic oscillator -> (%K, %D) series."""
    n = len(c)
    k = [None]*n
    for i in range(k_period-1, n):
        win = c[i-k_period+1:i+1]
        hh = max(x["high"] for x in win)
        ll = min(x["low"] for x in win)
        k[i] = 50.0 if hh == ll else (c[i]["close"]-ll)/(hh-ll)*100.0
    valid = [v for v in k if v is not None]
    d_tail = ind.sma(valid, d_period)
    d = [None]*n
    off = n - len(d_tail)
    for i, v in enumerate(d_tail):
        d[off+i] = v
    return k, d


def _cci(c, period: int):
    """Commodity Channel Index on typical price."""
    n = len(c)
    tp = [(x["high"]+x["low"]+x["close"])/3 for x in c]
    m = ind.sma(tp, period)
    out = [None]*n
    for i in range(period-1, n):
        if m[i] is None:
            continue
        win = tp[i-period+1:i+1]
        md = sum(abs(v-m[i]) for v in win)/period
        out[i] = 0.0 if md == 0 else (tp[i]-m[i])/(0.015*md)
    return out


# --------------------------------------------------------------- strategies
# each returns a target-position list in {0,1}, aligned to candles.

def s_sma_cross(c, p):
    cl = [x["close"] for x in c]
    fa, sl = ind.sma(cl, p["fast"]), ind.sma(cl, p["slow"])
    return [1 if fa[i] is not None and sl[i] is not None and fa[i] > sl[i] else 0
            for i in range(len(c))]

def s_ema_cross(c, p):
    cl = [x["close"] for x in c]
    fa, sl = ind.ema(cl, p["fast"]), ind.ema(cl, p["slow"])
    return [1 if fa[i] is not None and sl[i] is not None and fa[i] > sl[i] else 0
            for i in range(len(c))]

def s_price_sma(c, p):
    cl = [x["close"] for x in c]
    ma = ind.sma(cl, p["length"])
    return [1 if ma[i] is not None and cl[i] > ma[i] else 0 for i in range(len(c))]

def s_momentum(c, p):
    L = p["length"]
    return [1 if i >= L and c[i]["close"] > c[i-L]["close"] else 0
            for i in range(len(c))]

def s_rsi_revert(c, p):
    cl = [x["close"] for x in c]
    r = ind.rsi(cl, p["length"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if r[i] is None:
            pos[i] = 0; continue
        if not on and r[i] < p["low"]: on = True
        elif on and r[i] > p["high"]: on = False
        pos[i] = 1 if on else 0
    return pos

def s_macd_cross(c, p):
    cl = [x["close"] for x in c]
    line, sig, _ = ind.macd(cl, p["fast"], p["slow"], p["signal"])
    return [1 if line[i] is not None and sig[i] is not None and line[i] > sig[i] else 0
            for i in range(len(c))]

def s_macd_zero(c, p):
    cl = [x["close"] for x in c]
    line, _, _ = ind.macd(cl, p["fast"], p["slow"], p["signal"])
    return [1 if line[i] is not None and line[i] > 0 else 0 for i in range(len(c))]

def s_supertrend(c, p):
    st = ind.supertrend(c, p["length"], p["mult"])
    return [1 if st[i]["dir"] == 1 else 0 for i in range(len(c))]

def s_st_adx(c, p):
    st = ind.supertrend(c, p["length"], p["mult"])
    ax = ind.adx(c, p["adx_len"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        d, a = st[i]["dir"], ax[i]
        if on:
            if d == -1: on = False           # ride until Supertrend flips
        else:
            if d == 1 and a is not None and a >= p["adx"]: on = True
        pos[i] = 1 if on else 0
    return pos

def s_pullback_trend(c, p):
    cl = [x["close"] for x in c]
    trend = ind.sma(cl, p["trend"])
    r = ind.rsi(cl, p["length"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if trend[i] is None or r[i] is None:
            pos[i] = 0; continue
        above = cl[i] > trend[i]
        if on:
            if r[i] > p["exit"] or not above: on = False
        else:
            if above and r[i] < p["entry"]: on = True
        pos[i] = 1 if on else 0
    return pos

def s_bollinger_revert(c, p):
    cl = [x["close"] for x in c]
    upper, mid, lower = ind.bollinger(cl, p["length"], p["mult"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if mid[i] is None:
            pos[i] = 0; continue
        if not on and cl[i] < lower[i]: on = True
        elif on and cl[i] > mid[i]: on = False
        pos[i] = 1 if on else 0
    return pos

def s_stoch_revert(c, p):
    k, d = _stoch(c, p["k"], p["d"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if k[i] is None or d[i] is None:
            pos[i] = 0; continue
        if not on and k[i] < p["low"] and k[i] > d[i]: on = True
        elif on and k[i] > p["high"]: on = False
        pos[i] = 1 if on else 0
    return pos

def s_cci_revert(c, p):
    cc = _cci(c, p["length"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if cc[i] is None:
            pos[i] = 0; continue
        if not on and cc[i] < p["low"]: on = True
        elif on and cc[i] > p["high"]: on = False
        pos[i] = 1 if on else 0
    return pos

def s_donchian_breakout(c, p):
    n = len(c); pos = [0]*n; on = False
    E, X = p["entry"], p["exit"]
    for i in range(n):
        if i < max(E, X):
            pos[i] = 0; continue
        hh = max(x["high"] for x in c[i-E:i])
        ll = min(x["low"] for x in c[i-X:i])
        if not on and c[i]["close"] > hh: on = True
        elif on and c[i]["close"] < ll: on = False
        pos[i] = 1 if on else 0
    return pos

def s_keltner_break(c, p):
    cl = [x["close"] for x in c]
    mid = ind.ema(cl, p["length"])
    a = ind.atr(c, p["atr_len"])
    pos = [0]*len(c); on = False
    for i in range(len(c)):
        if mid[i] is None or a[i] is None:
            pos[i] = 0; continue
        upper = mid[i] + p["mult"]*a[i]
        if not on and cl[i] > upper: on = True
        elif on and cl[i] < mid[i]: on = False
        pos[i] = 1 if on else 0
    return pos

def s_bb_squeeze(c, p):
    cl = [x["close"] for x in c]
    upper, mid, lower = ind.bollinger(cl, p["length"], p["mult"])
    n = len(c)
    bw = [None if mid[i] is None or mid[i] == 0 else (upper[i]-lower[i])/mid[i]
          for i in range(n)]
    pos = [0]*n; on = False; armed_until = -1
    L = p["lookback"]
    for i in range(n):
        if mid[i] is None:
            pos[i] = 0; continue
        # armed when today's bandwidth is in the lowest quartile of the window
        win = [b for b in bw[max(0, i-L+1):i+1] if b is not None]
        if len(win) >= L//2:
            rank = sum(1 for b in win if b <= bw[i]) / len(win)
            if rank <= 0.25:
                armed_until = i + 5           # breakout must come soon after
        if on:
            if cl[i] < mid[i]: on = False
        else:
            if i <= armed_until and cl[i] > upper[i]: on = True
        pos[i] = 1 if on else 0
    return pos


def s_vol_breakout(c, p):
    n = len(c); pos = [0]*n; on = False
    E, X = p["entry"], p["exit"]
    vol = [x["volume"] for x in c]
    va = ind.sma(vol, p["vol_len"])
    for i in range(n):
        if i < max(E, X, p["vol_len"]):
            pos[i] = 0; continue
        hh = max(x["high"] for x in c[i-E:i])
        ll = min(x["low"] for x in c[i-X:i])
        if not on and c[i]["close"] > hh and va[i] and vol[i] >= p["vol_mult"]*va[i]:
            on = True
        elif on and c[i]["close"] < ll:
            on = False
        pos[i] = 1 if on else 0
    return pos


def _abt(category, summary, entry, exit_, math_, works, fails, tip):
    return {"category": category, "summary": summary, "entry": entry,
            "exit": exit_, "math": math_, "works": works, "fails": fails,
            "tip": tip}

STRATEGIES = {
    "sma_cross": {
        "name": "SMA crossover", "fn": s_sma_cross,
        "params": {"fast": 20, "slow": 50},
        "about": _abt(
            "Trend following",
            "The classic trend filter: hold the stock whenever a fast simple "
            "moving average sits above a slow one. With fast=50/slow=200 this "
            "is the famous golden cross / death cross.",
            "Fast SMA closes above the slow SMA.",
            "Fast SMA closes back below the slow SMA.",
            "SMA(n) = mean of the last n closes. The fast average reacts to "
            "recent price, the slow one anchors the longer trend; their "
            "ordering is a smoothed statement of trend direction.",
            "Long, persistent trends — the lag costs you the first leg but "
            "keeps you in for the middle, which is where trend profits live.",
            "Sideways / choppy markets. Every oscillation around the slow "
            "average whipsaws you in and out, paying costs both ways.",
            "Widening the gap between fast and slow trades less and whipsaws "
            "less, at the price of later entries and exits."),
    },
    "ema_cross": {
        "name": "EMA crossover", "fn": s_ema_cross,
        "params": {"fast": 12, "slow": 26},
        "about": _abt(
            "Trend following",
            "Same idea as the SMA crossover but with exponential averages, "
            "which weight recent bars more and therefore turn earlier.",
            "Fast EMA closes above the slow EMA.",
            "Fast EMA closes back below the slow EMA.",
            "EMA_t = k·price + (1−k)·EMA_(t−1), k = 2/(n+1). Recent prices "
            "dominate, so the average hugs price more tightly than an SMA of "
            "the same length.",
            "Trends that start fast — you get in earlier than the SMA "
            "version.",
            "Chop, even more than SMA crossover: the tighter hug means more "
            "crossings and more false starts.",
            "12/26 are the MACD defaults; try 21/55 if the whipsaw count in "
            "the trade log looks ugly."),
    },
    "price_sma": {
        "name": "Price vs SMA", "fn": s_price_sma,
        "params": {"length": 50},
        "about": _abt(
            "Trend following",
            "The simplest regime filter there is: long when price closes "
            "above its moving average, flat below. One line, one rule.",
            "Close above the SMA.",
            "Close below the SMA.",
            "Position = 1 if close > SMA(length) else 0. That's the whole "
            "model.",
            "Strong one-way markets, and as an honesty benchmark: if a fancy "
            "strategy can't beat this, the fancy parts aren't earning "
            "anything.",
            "Ranging markets where price straddles the average — expect many "
            "tiny losing trades in the log.",
            "length=200 on daily bars is the classic \"only own stocks above "
            "the 200-day\" market filter."),
    },
    "momentum": {
        "name": "Momentum (rate of change)", "fn": s_momentum,
        "params": {"length": 126},
        "about": _abt(
            "Momentum",
            "Time-series momentum: own the stock while it is higher than it "
            "was N bars ago. One of the most robust effects in the academic "
            "literature.",
            "Close is above the close from `length` bars ago.",
            "Close falls below the close from `length` bars ago.",
            "ROC = close_t / close_(t−N) − 1; long while ROC > 0. 126 daily "
            "bars ≈ 6 months, the horizon where the momentum effect tests "
            "strongest.",
            "Persistent multi-month trends, up or down — it simply refuses to "
            "hold a falling stock for long.",
            "V-shaped reversals: it exits near lows after a crash and re-"
            "enters late after the rebound.",
            "Momentum measured over 3–12 months (63–252 daily bars) is where "
            "the research consensus sits; very short lookbacks turn it into "
            "noise-chasing."),
    },
    "rsi_revert": {
        "name": "RSI mean-reversion", "fn": s_rsi_revert,
        "params": {"length": 14, "low": 30, "high": 55},
        "about": _abt(
            "Mean reversion",
            "Buy fear, sell normality: enter when RSI says the stock is "
            "oversold, exit once it has recovered to neutral.",
            "RSI drops below the `low` threshold (oversold).",
            "RSI rises above the `high` threshold (recovered).",
            "RSI = 100 − 100/(1+RS), RS = Wilder-smoothed average gain / "
            "average loss over `length` bars. It maps recent win/loss "
            "pressure onto a 0–100 scale.",
            "Range-bound quality stocks that get sold off and snap back — "
            "dips are bought, so oversold readings resolve upward.",
            "Downtrends: \"oversold\" can stay oversold while price halves. "
            "Mean reversion with no trend filter is knife-catching.",
            "Pair with a stop-loss from the risk panel, or use the RSI "
            "pullback-in-uptrend strategy which adds the trend filter for "
            "you."),
    },
    "macd_cross": {
        "name": "MACD signal cross", "fn": s_macd_cross,
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "about": _abt(
            "Momentum",
            "Hold while the MACD line is above its own signal line — i.e. "
            "while momentum is improving faster than its recent norm.",
            "MACD line closes above the signal line.",
            "MACD line closes back below the signal line.",
            "MACD = EMA(12) − EMA(26); signal = EMA(9) of MACD. The cross "
            "detects acceleration: not just \"trending up\" but \"trending up "
            "faster than lately\".",
            "Swing moves lasting weeks — it reacts faster than a plain EMA "
            "cross because it watches the *spread* between EMAs.",
            "Quiet drifts and chop: MACD hovers near zero and crosses its "
            "signal constantly on noise.",
            "Crosses that happen far below the zero line are counter-trend "
            "bounces; compare with the MACD zero-line strategy to see the "
            "difference."),
    },
    "macd_zero": {
        "name": "MACD zero line", "fn": s_macd_zero,
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "about": _abt(
            "Momentum",
            "A slower, calmer MACD: hold only while the MACD line itself is "
            "above zero, which by construction means the 12-EMA is above the "
            "26-EMA.",
            "MACD line crosses above zero.",
            "MACD line crosses below zero.",
            "MACD > 0 ⟺ EMA(fast) > EMA(slow), so this is the EMA crossover "
            "expressed as an oscillator — same trades, but the distance from "
            "zero shows how stretched the trend is.",
            "Sustained trends; it trades far less than the signal-line cross.",
            "The same enemy as every trend rule: sideways markets.",
            "Run it and MACD signal-cross on the same window: the zero-line "
            "version is usually fewer trades, lower cost drag, later "
            "entries."),
    },
    "supertrend": {
        "name": "Supertrend", "fn": s_supertrend,
        "params": {"length": 10, "mult": 3},
        "about": _abt(
            "Trend following",
            "Ride the trend with a volatility-adjusted band: long while price "
            "holds above the Supertrend line, which trails below price at a "
            "distance of `mult` ATRs.",
            "Supertrend flips to an uptrend (price closes above the upper "
            "band).",
            "Supertrend flips to a downtrend (price closes below the trailing "
            "lower band).",
            "Bands = (H+L)/2 ± mult·ATR(length), with the active band only "
            "ever ratcheting toward price — so the line acts as a built-in "
            "trailing stop that adapts to volatility.",
            "Trending stocks with orderly pullbacks smaller than mult ATRs — "
            "the band absorbs the noise and holds the position.",
            "Violent chop wider than the band, which flips it long and flat "
            "repeatedly at the worst prices.",
            "mult=3 is standard; mult=2 exits quicker, mult=4 sits through "
            "deeper pullbacks. This is the same Supertrend your live alerts "
            "use."),
    },
    "st_adx": {
        "name": "Supertrend + ADX filter", "fn": s_st_adx,
        "params": {"length": 10, "mult": 3, "adx_len": 14, "adx": 20},
        "about": _abt(
            "Trend following (filtered)",
            "Supertrend, but only allowed to enter when ADX confirms a real "
            "trend is in progress — an attempt to fix Supertrend's one "
            "weakness, chop.",
            "Supertrend is in an uptrend AND ADX ≥ the threshold.",
            "Supertrend flips down (ADX is only an entry gate, not an exit).",
            "ADX measures trend *strength* regardless of direction, from the "
            "smoothed spread between +DI and −DI. Below ~20 is considered "
            "trendless; 25+ a trending market.",
            "The same trends as plain Supertrend, while skipping many of the "
            "whipsaw entries inside ranges.",
            "Trends that ignite from dead-quiet ranges: ADX lags, so the "
            "filter can cost you the first leg of a strong move.",
            "Compare against plain Supertrend on the same window — the filter "
            "should cut trade count and improve win rate; if it doesn't, "
            "lower the threshold."),
    },
    "pullback_trend": {
        "name": "RSI pullback in uptrend", "fn": s_pullback_trend,
        "params": {"trend": 200, "length": 2, "entry": 10, "exit": 60},
        "about": _abt(
            "Mean reversion (trend-filtered)",
            "Buy sharp dips, but only in stocks that are in a long-term "
            "uptrend — the Connors RSI-2 idea. Combines a trend filter with a "
            "fast mean-reversion trigger.",
            "Close above the `trend` SMA AND fast RSI below the `entry` "
            "level (a violent short-term dip).",
            "Fast RSI recovers above the `exit` level, or price loses the "
            "trend SMA (filter failed — get out).",
            "RSI(2) is deliberately hair-trigger: it reaches single digits "
            "only after consecutive hard down days. The SMA(200) filter "
            "ensures those down days are a dip in an uptrend, not the start "
            "of a downtrend.",
            "Bull markets with periodic scary shakeouts — historically high "
            "win rates with short holds.",
            "Regime breaks: the dip that keeps dipping right through the "
            "200-day. The trend-loss exit limits, not prevents, that damage.",
            "Wins are many but small and losses rare but large — judge it by "
            "profit factor and max drawdown, never by win rate alone."),
    },
    "bollinger_revert": {
        "name": "Bollinger reversion", "fn": s_bollinger_revert,
        "params": {"length": 20, "mult": 2},
        "about": _abt(
            "Mean reversion",
            "Buy statistical stretch: enter when price closes below the "
            "lower Bollinger band, exit once it reverts to the middle band.",
            "Close below the lower band (mid − mult·σ).",
            "Close back above the middle band (the SMA).",
            "Bands sit `mult` standard deviations around SMA(length), so "
            "they widen in volatile phases and tighten in quiet ones — "
            "\"oversold\" self-adjusts to current volatility.",
            "Rangy markets where band touches are stretches that snap back "
            "to the mean.",
            "Downtrends, where price \"walks the band\": closing below the "
            "lower band repeatedly while the mean itself falls away from "
            "you.",
            "Exiting at the mid band (not the upper) takes the reliable half "
            "of the reversion; demand the upper band and you hold twice as "
            "long for the flakier half."),
    },
    "stoch_revert": {
        "name": "Stochastic reversion", "fn": s_stoch_revert,
        "params": {"k": 14, "d": 3, "low": 20, "high": 80},
        "about": _abt(
            "Mean reversion",
            "Buy when the stochastic is oversold AND already curling back up "
            "(%K above %D) — reversion with a small confirmation, rather "
            "than catching the falling knife.",
            "%K below the `low` level while %K > %D (turning up in oversold "
            "territory).",
            "%K rises above the `high` level.",
            "%K = (close − lowest low) / (highest high − lowest low) × 100 "
            "over `k` bars: where today's close sits inside the recent "
            "range. %D = SMA(%K, d) smooths it.",
            "Range-bound stocks: close near the bottom of the recent range "
            "with upward curl tends to resolve upward.",
            "Strong trends: in an uptrend it barely triggers; in a downtrend "
            "the range keeps shifting lower beneath the entry.",
            "The %K>%D confirmation trades entry speed for quality — remove "
            "the comparison mentally and you have the RSI-reversion shape."),
    },
    "cci_revert": {
        "name": "CCI reversion", "fn": s_cci_revert,
        "params": {"length": 20, "low": -100, "high": 100},
        "about": _abt(
            "Mean reversion",
            "Buy when price is unusually far below its typical level as "
            "measured by CCI, exit when it swings to unusually high.",
            "CCI drops below the `low` level (typically −100).",
            "CCI rises above the `high` level (typically +100).",
            "CCI = (TP − SMA(TP)) / (0.015 · mean deviation), TP = (H+L+C)/3. "
            "The 0.015 constant scales it so ~70–80% of readings land inside "
            "±100 — beyond that is the unusual zone.",
            "Oscillating markets with regular overshoots in both directions.",
            "Trends — like all unfiltered reversion it fights them; sub−100 "
            "readings cluster exactly when a downtrend accelerates.",
            "Exiting at +100 (not 0) means you hold through the full swing; "
            "tighten `high` to 0 for quicker, smaller reversion trades."),
    },
    "donchian_breakout": {
        "name": "Donchian breakout", "fn": s_donchian_breakout,
        "params": {"entry": 20, "exit": 10},
        "about": _abt(
            "Breakout",
            "The Turtle Traders' rule: buy a close above the highest high of "
            "the last `entry` bars, exit on a close below the lowest low of "
            "the last `exit` bars.",
            "Close above the `entry`-bar highest high (fresh breakout).",
            "Close below the `exit`-bar lowest low.",
            "The Donchian channel is just the running max/min of highs and "
            "lows. New highs are objective — no smoothing, no parameters to "
            "curve-fit beyond the two lookbacks.",
            "Explosive moves from consolidation: it will never miss a big "
            "trend, because every big trend starts with a new high.",
            "False breakouts in ranges — most breakouts fail, so expect a "
            "win rate well under 50% carried by a few large winners.",
            "The asymmetric 20-in / 10-out (Turtle System 1 style) exits "
            "faster than it enters, protecting open profits; set both equal "
            "for the older symmetric channel."),
    },
    "keltner_break": {
        "name": "Keltner breakout", "fn": s_keltner_break,
        "params": {"length": 20, "mult": 2, "atr_len": 10},
        "about": _abt(
            "Breakout",
            "Buy strength that clears a volatility envelope: enter when "
            "price closes above the upper Keltner channel, ride it, exit "
            "when it falls back through the channel midline.",
            "Close above EMA(length) + mult·ATR(atr_len).",
            "Close below the EMA midline.",
            "Keltner bands use ATR (average true range) for width instead of "
            "standard deviation, so gaps and intraday range count — the "
            "envelope reflects how far price *actually travels*, not just "
            "close-to-close dispersion.",
            "Momentum ignitions: a close beyond 2 ATRs above trend is a "
            "genuinely unusual thrust, and thrusts tend to continue.",
            "Exhaustion spikes — sometimes the 2-ATR close IS the top. The "
            "midline exit gives back a chunk on those.",
            "Compare with Bollinger reversion: identical geometry, opposite "
            "philosophy — one buys the lower band expecting reversion, this "
            "buys above the upper band expecting continuation."),
    },
    "bb_squeeze": {
        "name": "Bollinger squeeze breakout", "fn": s_bb_squeeze,
        "params": {"length": 20, "mult": 2, "lookback": 100},
        "about": _abt(
            "Volatility breakout",
            "Volatility is cyclical: quiet periods precede loud ones. Wait "
            "for the Bollinger bands to squeeze to unusually narrow, then "
            "buy the upside break.",
            "Bandwidth is in the lowest quartile of the last `lookback` bars "
            "(the squeeze), and within a few bars price closes above the "
            "upper band (the release).",
            "Close below the middle band.",
            "Bandwidth = (upper − lower) / middle. Ranking today's bandwidth "
            "against its own recent history finds compression without any "
            "absolute threshold to tune.",
            "Consolidation-then-expansion patterns — flags, tight ranges "
            "before results, coiling charts.",
            "Squeezes that release *downward*, and head-fake breaks that "
            "close above the band once and die. Long-only, it simply misses "
            "the down-releases and eats the head-fakes.",
            "Fewer signals than any other strategy here by design; judge it "
            "on profit factor over a long window, not trade count."),
    },
    "vol_breakout": {
        "name": "Volume-confirmed breakout", "fn": s_vol_breakout,
        "params": {"entry": 20, "exit": 10, "vol_mult": 1.5, "vol_len": 20},
        "about": _abt(
            "Breakout",
            "The Donchian breakout, but the break only counts if volume backs "
            "it: a new high on quiet volume is a drift, a new high on heavy "
            "volume is a decision. The single most repeated filter in "
            "practitioner breakout playbooks.",
            "Close above the `entry`-bar highest high AND today's volume ≥ "
            "vol_mult × its `vol_len`-bar average.",
            "Close below the `exit`-bar lowest low (volume is only an entry "
            "gate).",
            "Volume ratio = volume / SMA(volume, vol_len). Breakouts carried "
            "by 1.5–2× average volume imply real participation — someone "
            "large is paying up through every prior seller.",
            "Consolidation breaks where institutions are accumulating — the "
            "volume gate skips most low-conviction pokes above the range.",
            "Gap-driven news breakouts that fade: volume is huge, the filter "
            "passes, and the move still reverses. Volume confirms interest, "
            "not direction of follow-through.",
            "Compare against plain Donchian on the same window — the filter "
            "should trade less and lose less; if it filters nothing, the "
            "stock's volume is too erratic for the average to mean much."),
    },
}


# --------------------------------------------------------------- simulator
def _apply_risk_defaults(risk: dict | None) -> dict:
    r = {"sl_pct": None, "tp_pct": None, "trail_mult": None,
         "trail_period": 22, "max_bars": None,
         # --- position sizing (3.5). None keeps the original behaviour:
         # every trade is the whole account, which is what a plain backtest
         # implicitly assumes and almost nobody actually does.
         "sizing": None,              # None | "fixed_fractional" | "atr_risk"
         "risk_pct": 1.0,             # % of CURRENT equity risked per trade
         "atr_mult": 2.0,             # stop distance for sizing="atr_risk"
         "atr_period": 14,
         "max_position_pct": 100.0}   # 100 = cash account, no leverage
    for k, v in (risk or {}).items():
        if k in r and v not in (None, "", 0):
            r[k] = v
    return r


class SizingError(ValueError):
    """Sizing was asked for but there is nothing to size against."""


def regime_gate(times: list[int], nifty: list[dict], period: int = 200,
                lag_days: int = 1) -> list[int]:
    """1 where the index was above its own long moving average, else 0.

    Pure, so the alignment can be tested without a Kite session — and the
    alignment is the whole difficulty. Two rules matter:

    **No lookahead.** Each symbol bar is matched to the most recent index bar
    that had already CLOSED, never the one forming alongside it. Matching same
    day would let a strategy avoid a crash using the close of the very session
    it happened in, which is the exact free lunch backtests love to serve.

    **Warm-up is off, not on.** Before there are `period` index bars the
    average doesn't exist, and the honest reading of "I don't know the regime"
    is to stand aside rather than assume risk-on. Assuming risk-on would
    quietly turn the first year of every backtest into an ungated one.
    """
    if not nifty:
        return [1] * len(times)          # no index data: don't silently gate
    closes = [b["close"] for b in nifty]
    ma = ind.sma(closes, period)
    stamps = [b["time"] for b in nifty]
    on = [1 if (ma[i] is not None and closes[i] > ma[i]) else 0
          for i in range(len(nifty))]

    out, j = [], -1
    lag = lag_days * 86400
    for t in times:
        # advance to the newest index bar that closed at least `lag` before
        while j + 1 < len(stamps) and stamps[j + 1] <= t - lag:
            j += 1
        out.append(on[j] if j >= 0 else 0)
    return out


def simulate(c: list[dict], pos: list[int], cost_bps: float = 10.0,
             capital: float = 100000.0, risk: dict | None = None) -> dict:
    """Run the position series through the market. Pure function — no I/O.

    `pos[i]` is the strategy's desired position decided at bar i's close.
    Returns equity curve, per-bar returns, trades (with exit reasons),
    chart markers and exposure.
    """
    n = len(c)
    risk = _apply_risk_defaults(risk)
    use_trail = risk["trail_mult"] is not None
    atr = ind.atr(c, int(risk["trail_period"])) if use_trail else None
    cost = cost_bps / 10000.0

    # --- position sizing -------------------------------------------------
    # Without it, every trade is 100% of equity: a plain backtest's returns
    # are what you'd get betting the account on each signal. Real sizing makes
    # the position a FUNCTION of the stop, which changes results in both
    # directions — a tight stop demands a large position (and a gap through it
    # hurts more than 1R), a wide stop a small one.
    #
    # Implemented as a deployed FRACTION of equity rather than a share count,
    # which is exactly equivalent for the curve and far less machinery:
    #
    #   qty   = (equity x risk%) / (entry - stop)
    #   value = qty x entry
    #   frac  = value / equity = risk% / ((entry - stop) / entry)
    #
    # so a 1% risk behind a 2% stop deploys 50% of the account, and a 1% risk
    # behind a 0.5% stop would want 200% — which is why the cap exists.
    sizing = risk["sizing"]
    if sizing not in (None, "fixed_fractional", "atr_risk"):
        raise SizingError(f"unknown sizing '{sizing}'")
    size_atr = (ind.atr(c, int(risk["atr_period"]))
                if sizing == "atr_risk" else None)
    if sizing == "fixed_fractional" and not (risk["sl_pct"] or use_trail):
        raise SizingError(
            "fixed_fractional sizing needs a stop to size against — set "
            "sl_pct or trail_mult, or use sizing='atr_risk'")
    max_frac = float(risk["max_position_pct"]) / 100.0
    skipped = 0

    eq = capital
    equity = [capital]
    curve = [{"time": c[0]["time"], "value": capital}]
    rets: list[float] = []
    trades: list[dict] = []
    markers: list[dict] = []
    exposure = 0

    in_pos = False
    blocked = False           # after a risk exit, wait for the signal to reset
    entry_price = entry_i = None
    hh = stop = target = None
    frac = 1.0                # share of equity deployed in the open position
    entry_stop = None         # the ORIGINAL stop — what 1R is measured from
    entry_eq = capital        # equity when the position was sized

    def open_pos(i):
        nonlocal in_pos, entry_price, entry_i, hh, stop, target, eq, frac
        nonlocal entry_stop, skipped, entry_eq
        price = c[i]["close"]
        s_ = price * (1 - risk["sl_pct"]/100.0) if risk["sl_pct"] else None
        h_ = c[i]["high"]
        if use_trail and atr[i] is not None:
            t = h_ - risk["trail_mult"] * atr[i]
            s_ = t if s_ is None else max(s_, t)
        if sizing == "atr_risk":
            # An ATR stop sizes off volatility rather than a fixed percentage,
            # so the same risk buys a big position in a quiet stock and a small
            # one in a violent stock — which is the point.
            if size_atr[i] is None:
                skipped += 1
                return
            a_stop = price - risk["atr_mult"] * size_atr[i]
            s_ = a_stop if s_ is None else max(s_, a_stop)

        f = 1.0
        if sizing:
            if s_ is None or s_ >= price:
                skipped += 1          # nothing to size against
                return
            f = (risk["risk_pct"] / 100.0) / ((price - s_) / price)
            f = min(f, max_frac)
            if f * eq / price < 1:    # can't afford a single share
                skipped += 1
                return

        in_pos = True
        entry_price, entry_i = price, i
        hh = h_
        stop = s_
        entry_stop = s_
        frac = f
        entry_eq = eq             # what the size was actually calculated on
        target = entry_price * (1 + risk["tp_pct"]/100.0) if risk["tp_pct"] else None
        eq *= (1 - cost * frac)
        markers.append({"time": c[i]["time"], "type": "buy", "price": price})

    def close_pos(price, t, i, reason, open_=False):
        nonlocal in_pos, entry_price, entry_i, hh, stop, target, eq
        nonlocal frac, entry_stop
        gross = price/entry_price - 1
        net = (price/entry_price) * (1 - cost)**2 - 1
        rec = {"entry_time": c[entry_i]["time"], "entry": round(entry_price, 2),
               "exit_time": t, "exit": round(price, 2),
               "bars": i - entry_i, "return_pct": round(gross*100, 2),
               "net_pct": round(net*100, 2), "reason": reason}
        if sizing:
            # Sized runs report what the journal reports, so the two tables can
            # sit beside each other without a footnote: a share count, the
            # stop 1R was measured from, and the R the trade actually made.
            qty = int(frac * entry_eq / entry_price)
            rec["qty"] = qty
            rec["stop"] = round(entry_stop, 2) if entry_stop else None
            rec["position_pct"] = round(frac * 100, 1)
            rec["equity_pct"] = round(net * frac * 100, 3)
            if entry_stop and entry_price > entry_stop:
                rec["r_multiple"] = round(
                    (price - entry_price) / (entry_price - entry_stop), 2)
        if open_:
            rec["open"] = True
        trades.append(rec)
        if not open_:
            markers.append({"time": t, "type": "sell", "price": price})
            eq *= (1 - cost * frac)
        in_pos = False
        entry_price = entry_i = hh = stop = target = None
        entry_stop = None
        frac = 1.0

    for i in range(n - 1):
        des = pos[i]
        if blocked and des == 0:
            blocked = False

        # 1) strategy exit at this bar's close
        if in_pos and des == 0:
            close_pos(c[i]["close"], c[i]["time"], i, "signal")
        # 2) strategy entry at this bar's close
        if (not in_pos) and des == 1 and not blocked:
            open_pos(i)

        # 3) live through bar i+1
        r = 0.0
        # Captured BEFORE any exit below, because close_pos resets frac and
        # this bar's move was earned at the size the position actually was.
        bar_frac = frac if in_pos else 1.0
        if in_pos:
            exposure += 1
            nb = c[i + 1]
            exit_price = reason = None
            # The open is the bar's FIRST traded price, so a gap through either
            # level is settled before anything that happens inside the bar —
            # a bar that opens above the target is a target fill even if it
            # later trades through the stop. Only once both gaps are ruled out
            # do we look intrabar, and there the stop is assumed to hit first
            # (we can't know the true ordering, so take the worse reading).
            if stop is not None and nb["open"] <= stop:
                exit_price, reason = nb["open"], "stop (gap)"
            elif target is not None and nb["open"] >= target:
                exit_price, reason = nb["open"], "target (gap)"
            elif stop is not None and nb["low"] <= stop:
                exit_price, reason = stop, "stop"
            elif target is not None and nb["high"] >= target:
                exit_price, reason = target, "target"

            if exit_price is not None:
                r = exit_price / c[i]["close"] - 1
                close_pos(exit_price, nb["time"], i + 1, reason)
                blocked = True
            else:
                r = nb["close"] / c[i]["close"] - 1
                if risk["max_bars"] and (i + 1 - entry_i) >= risk["max_bars"]:
                    close_pos(nb["close"], nb["time"], i + 1, "time")
                    blocked = True
                else:
                    hh = max(hh, nb["high"])
                    if use_trail and atr[i + 1] is not None:
                        t = hh - risk["trail_mult"] * atr[i + 1]
                        stop = t if stop is None else max(stop, t)

        # The position is `bar_frac` of the account, so a 10% price move on a
        # 30% position is a 3% move in equity. It is 1.0 whenever sizing is
        # off, which reduces this to the original line exactly.
        er = r * bar_frac
        eq *= (1 + er)
        rets.append(er)
        equity.append(eq)
        curve.append({"time": c[i + 1]["time"], "value": round(eq, 2)})

    if in_pos:  # mark-to-market an open position at the last close
        close_pos(c[-1]["close"], c[-1]["time"], n - 1, "open", open_=True)

    return {"equity": equity, "curve": curve, "rets": rets, "trades": trades,
            "markers": markers, "exposure": exposure,
            "sizing": sizing, "skipped": skipped}


# ----------------------------------------------------------------- metrics
def _metrics(equity, rets, bpy):
    if len(equity) < 2:
        return {}
    total = equity[-1]/equity[0] - 1
    yrs = max(len(rets)/bpy, 1e-9)
    cagr = (equity[-1]/equity[0])**(1/yrs) - 1
    peak = equity[0]; mdd = 0.0
    dd_run = 0; dd_longest = 0
    for v in equity:
        if v >= peak:
            peak = v; dd_run = 0
        else:
            dd_run += 1; dd_longest = max(dd_longest, dd_run)
        mdd = min(mdd, v/peak - 1)
    mean = sum(rets)/len(rets) if rets else 0
    var = sum((r-mean)**2 for r in rets)/len(rets) if rets else 0
    sd = math.sqrt(var)
    downs = [r for r in rets if r < 0]
    dd = math.sqrt(sum(r*r for r in downs)/len(rets)) if rets else 0
    sharpe = (mean/sd*math.sqrt(bpy)) if sd > 0 else None
    sortino = (mean/dd*math.sqrt(bpy)) if dd > 0 else None
    calmar = (cagr/abs(mdd)) if mdd < 0 else None
    recovery = (total/abs(mdd)) if mdd < 0 else None
    return {
        "total_return_pct": round(total*100, 2),
        "cagr_pct": round(cagr*100, 2),
        "max_drawdown_pct": round(mdd*100, 2),
        "longest_dd_bars": dd_longest,
        "volatility_pct": round(sd*math.sqrt(bpy)*100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "calmar": round(calmar, 2) if calmar is not None else None,
        "recovery_factor": round(recovery, 2) if recovery is not None else None,
    }


def _trade_stats(trades):
    """Trade statistics over COMPLETED trades, measured on NET returns.

    Two rules make these numbers mean what they say:

      * The position still open at the end of the window is excluded. It has
        no exit, so counting it books an unrealised mark-to-market as a win
        or a loss — which flatters any strategy that happens to be long into
        a rising close. It stays in the trade list (flagged `open`) so you
        can still see it; it just doesn't vote.
      * Wins and losses are split on `net_pct`, after the round-trip cost.
        A trade that gains less than it cost to put on lost you money, and
        counting it as a win is how a strategy shows a 60% win rate while
        expectancy sits below zero.
    """
    closed = [t for t in trades if not t.get("open")]
    wins = [t for t in closed if t["net_pct"] > 0]
    losses = [t for t in closed if t["net_pct"] <= 0]
    net_win = sum(t["net_pct"] for t in wins)
    net_loss = -sum(t["net_pct"] for t in losses)
    # streaks
    mw = ml = cw = cl = 0
    for t in closed:
        if t["net_pct"] > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        mw, ml = max(mw, cw), max(ml, cl)
    nets = [t["net_pct"] for t in closed]
    return {
        "trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "win_rate_pct": round(len(wins)/len(closed)*100, 1) if closed else None,
        "avg_win_pct": round(net_win/len(wins), 2) if wins else None,
        "avg_loss_pct": round(-net_loss/len(losses), 2) if losses else None,
        "profit_factor": round(net_win/net_loss, 2) if net_loss > 0 else None,
        "expectancy_pct": round(sum(nets)/len(nets), 2) if nets else None,
        "best_trade_pct": round(max(t["net_pct"] for t in closed), 2) if closed else None,
        "worst_trade_pct": round(min(t["net_pct"] for t in closed), 2) if closed else None,
        "max_consec_wins": mw if closed else None,
        "max_consec_losses": ml if closed else None,
        "avg_bars_held": round(sum(t["bars"] for t in closed)/len(closed), 1) if closed else None,
    }


# ------------------------------------------------------------------- run
def evaluate_on(c: list[dict], strategy: str, interval: str = "day",
                cost_bps: float = 10.0, capital: float = 100000.0,
                params: dict | None = None, risk: dict | None = None,
                gate: list[int] | None = None) -> dict:
    """Everything `run` computes, but on candles you already have.

    Split out so a parameter sweep can fetch candles once and evaluate a whole
    grid against them — 49 cells otherwise means 49 identical fetches against a
    rate-limited API. `run` goes through here too, which is the point: a
    heatmap cell and a single run must produce the same numbers, or clicking
    through from the grid shows you something that disagrees with the grid.

    -> {"sim", "stats", "buyhold", "bh_equity", "bh_rets", "params"}
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")
    if interval not in _BPY:
        raise ValueError(f"backtest supports native intervals only, not {interval}")
    if len(c) < 30:
        raise ValueError(f"not enough candles ({len(c)}) to backtest")

    spec = STRATEGIES[strategy]
    p = {**spec["params"], **(params or {})}
    pos = spec["fn"](c, p)                    # target position at each bar close
    if gate is not None:
        # The regime filter is a VETO, not a signal. It can only take a
        # position away, never create one — so a strategy gated by it is
        # always a subset of the same strategy ungated, and the comparison
        # between the two means something.
        pos = [p_ if g else 0 for p_, g in zip(pos, gate)]

    sim = simulate(c, pos, cost_bps=cost_bps, capital=capital, risk=risk)
    equity, rets = sim["equity"], sim["rets"]

    bh_rets = [c[i+1]["close"]/c[i]["close"] - 1 for i in range(len(c)-1)]
    bh_eq = [capital]
    for r in bh_rets:
        bh_eq.append(bh_eq[-1]*(1+r))

    bpy = _BPY[interval]
    stats = _metrics(equity, rets, bpy)
    stats.update(_trade_stats(sim["trades"]))
    stats["exposure_pct"] = round(sim["exposure"]/(len(c)-1)*100, 1) if len(c) > 1 else None
    bh_stats = _metrics(bh_eq, bh_rets, bpy)
    stats["vs_buyhold_pct"] = (round(stats["total_return_pct"] - bh_stats["total_return_pct"], 2)
                               if stats.get("total_return_pct") is not None else None)

    return {"sim": sim, "stats": stats, "buyhold": bh_stats,
            "bh_equity": bh_eq, "bh_rets": bh_rets, "params": p,
            "strategy_name": spec["name"]}


REGIME_INDEX = "NIFTY 50"


def run_positions(symbol: str, positions: list[int], label: str,
                  interval: str = "day", days: int = 730,
                  cost_bps: float = 10.0, capital: float = 100000.0,
                  risk: dict | None = None, candles_in=None) -> dict:
    """Backtest a position series someone else computed.

    Exists so the query engine can reach the simulator without pretending to
    be a strategy. Everything downstream — costs, intrabar fill ordering,
    sizing, the metrics, Monte Carlo — is the same code the built-in
    strategies run through, which is the point: a query and a strategy have to
    be measured on identical terms or comparing them means nothing.
    """
    c = candles_in
    if c is None:
        inst = instruments.lookup(symbol)
        if not inst:
            raise ValueError(f"unknown symbol {symbol}")
        kite = kite_auth.get_kite()
        candles.ensure(kite, inst["instrument_token"], interval, days)
        c = candles.get(inst["instrument_token"], interval, days)
    if len(c) < 2:
        raise ValueError("not enough candles")
    if len(positions) != len(c):
        raise ValueError(
            f"positions ({len(positions)}) and candles ({len(c)}) disagree")

    sim = simulate(c, positions, cost_bps=cost_bps, capital=capital, risk=risk)
    bpy = _BPY[interval]
    stats = {**_metrics(sim["equity"], sim["rets"], bpy),
             **_trade_stats(sim["trades"]),
             "exposure_pct": round(sim["exposure"] * 100, 1)}
    bh_curve = [{"time": c[i]["time"],
                 "value": round(c[i]["close"] / c[0]["close"] * capital, 2)}
                for i in range(len(c))]
    bh_rets = [c[i]["close"] / c[i - 1]["close"] - 1 for i in range(1, len(c))]
    bh_stats = _metrics([b["value"] for b in bh_curve], bh_rets, bpy)

    return {
        "symbol": symbol.upper(), "strategy": "query", "strategy_name": label,
        "interval": interval, "days": days, "bars": len(c),
        "cost_bps": cost_bps, "capital": capital,
        "risk": _apply_risk_defaults(risk),
        "stats": stats, "buyhold": bh_stats,
        "equity": sim["curve"], "buyhold_equity": bh_curve,
        "price": [{"time": x["time"], "value": round(x["close"], 2)} for x in c],
        "markers": sim["markers"], "trades": sim["trades"],
        "start": c[0]["time"], "end": c[-1]["time"],
        "sizing": sim.get("sizing"), "skipped_trades": sim.get("skipped", 0),
    }

# Exactly the journal's columns, in the journal's order. The point of this
# export is a side-by-side comparison, and a column that doesn't line up is a
# column you end up reconciling by hand at the moment you least want to.
JOURNAL_COLUMNS = ("symbol", "side", "qty", "entry", "stop", "target",
                   "entry_time", "exit", "exit_time", "reason", "charges",
                   "r_multiple", "tag", "notes", "source")


def trades_csv(result: dict) -> str:
    """A backtest's trade list in the journal's own schema.

    Two things this deliberately does NOT do. It doesn't invent a quantity
    when the run wasn't sized — an unsized backtest has no share count, and
    writing 1 would make the R column arithmetic nonsense. And it doesn't
    include the open trade, for the same reason every statistic excludes it:
    there is no exit, so there is nothing to score.

    `source` is stamped `backtest` so that if this ever gets imported into the
    real journal, live and simulated rows can never be silently mixed.
    """
    import csv
    import io
    from datetime import datetime

    def when(ts):
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=JOURNAL_COLUMNS, lineterminator="\n")
    w.writeheader()
    tag = result.get("strategy", "")
    symbol = result.get("symbol", "")
    for t in result.get("trades", []):
        if t.get("open"):
            continue
        w.writerow({
            "symbol": symbol,
            "side": "long",
            "qty": t.get("qty", ""),
            "entry": t["entry"],
            "stop": t.get("stop", ""),
            "target": "",
            "entry_time": when(t["entry_time"]),
            "exit": t["exit"],
            "exit_time": when(t["exit_time"]),
            "reason": t["reason"],
            # Costs are already inside net_pct; naming them here would double
            # count them against a journal row that carries real charges.
            "charges": "",
            "r_multiple": t.get("r_multiple", ""),
            "tag": tag,
            "notes": f"{t['bars']} bars, {t['net_pct']}% net",
            "source": "backtest",
        })
    return buf.getvalue()


def run(symbol: str, strategy: str, interval: str = "day", days: int = 730,
        cost_bps: float = 10.0, capital: float = 100000.0,
        params: dict | None = None, risk: dict | None = None,
        regime: bool = False) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")
    if interval not in _BPY:
        raise ValueError(f"backtest supports native intervals only, not {interval}")
    inst = instruments.lookup(symbol)
    if not inst:
        raise ValueError(f"unknown symbol {symbol}")
    kite = kite_auth.get_kite()
    candles.ensure(kite, inst["instrument_token"], interval, days)
    c = candles.get(inst["instrument_token"], interval, days)
    if len(c) < 30:
        raise ValueError(f"not enough candles ({len(c)}) to backtest")

    gate = None
    gate_pct = None
    if regime:
        idx = instruments.lookup(REGIME_INDEX)
        if not idx:
            raise ValueError(f"{REGIME_INDEX} is not in the instrument index — "
                             f"run sync-instruments")
        # Daily regardless of the symbol's interval: a 200-day average of
        # 5-minute bars is not the same idea, and not the one 2.1 means.
        candles.ensure(kite, idx["instrument_token"], "day", days + 400)
        nifty = candles.get(idx["instrument_token"], "day", days + 400)
        gate = regime_gate([b["time"] for b in c], nifty)
        gate_pct = round(sum(gate) / len(gate) * 100, 1) if gate else None

    ev = evaluate_on(c, strategy, interval=interval, cost_bps=cost_bps,
                     capital=capital, params=params, risk=risk, gate=gate)
    spec = STRATEGIES[strategy]
    p = ev["params"]
    sim = ev["sim"]
    equity = sim["equity"]
    stats = ev["stats"]
    bh_stats = ev["buyhold"]
    bh_eq = ev["bh_equity"]
    bh_curve = [{"time": c[i]["time"], "value": round(bh_eq[i], 2)}
                for i in range(len(c))]

    # drawdown series for the chart
    peak = equity[0]; dd_curve = []
    for i, v in enumerate(equity):
        peak = max(peak, v)
        dd_curve.append({"time": c[i]["time"], "value": round((v/peak - 1)*100, 2)})

    rd = _apply_risk_defaults(risk)
    if rd["trail_mult"] is None:
        rd.pop("trail_period")
    active_risk = {k: v for k, v in rd.items() if v is not None}

    return {
        "symbol": inst["tradingsymbol"], "name": inst["name"],
        "strategy": strategy, "strategy_name": spec["name"],
        "interval": interval, "days": days, "bars": len(c),
        "cost_bps": cost_bps, "capital": capital, "params": p, "risk": active_risk,
        "stats": stats, "buyhold": bh_stats,
        "equity": sim["curve"], "buyhold_equity": bh_curve, "drawdown": dd_curve,
        "price": [{"time": x["time"], "value": round(x["close"], 2)} for x in c],
        "markers": sim["markers"], "trades": sim["trades"],
        "start": c[0]["time"], "end": c[-1]["time"],
        "regime": regime,
        # How much of the window the gate allowed. A strategy that looks
        # transformed by the filter while this reads 95% wasn't transformed by
        # the filter.
        "regime_on_pct": gate_pct,
        "sizing": sim.get("sizing"),
        "skipped_trades": sim.get("skipped", 0),
    }


def catalog() -> list[dict]:
    return [{"key": k, "name": v["name"], "params": v["params"], "about": v["about"]}
            for k, v in STRATEGIES.items()]
