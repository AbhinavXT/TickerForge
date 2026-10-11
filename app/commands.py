"""Two-way phone control over ntfy.

Push notifications are one-way (home -> phone). This adds the return path
WITHOUT exposing the home machine to the internet: your phone *publishes* a
command to a private ntfy topic; this listener *subscribes* to that topic, runs
the query against the same live modules the rest of TickerForge uses, and pushes
the answer back to your normal notification topic. The ntfy server is the relay,
so there is no inbound port, no tunnel, and no public server.

Setup:
  * Pick a second, unguessable topic name and set NTFY_CMD_TOPIC in .env
    (separate from NTFY_TOPIC so replies never loop back as commands).
  * Subscribe to NTFY_CMD_TOPIC in the ntfy app too, so you can send to it
    (the app's "send message"/publish box targets that topic).
  * Run:  python -m app.cli remote

Commands (case-insensitive):
  /valuation SYM   intrinsic value + margin of safety
  /fundamentals SYM   key ratios snapshot          (alias /fund)
  /news SYM        latest headlines for a stock
  /results [SYM]   recent results filings (a stock, or market-wide)
  /dividends [SYM] recent dividend filings (ditto)   (alias /div)
  /price SYM       last candle O/H/L/C + day change
  /help            list commands

Security: the topic name is the whole credential. Anyone who learns it can
both read your alerts and publish commands, and ntfy.sh topics are public by
default — there is no account behind them.

The channel is no longer read-only. `/alert` and `/unalert` change what the
watcher will fire, so a leaked topic doesn't just leak stock queries any more:
it lets someone disarm the stop-loss alert you're relying on. Three things
follow from that:

  * Set NTFY_TOKEN (an ntfy access token) if your ntfy server supports auth —
    self-hosted, or ntfy.sh with reserved topics. That is the real fix, and
    it's server-side config rather than code.
  * Failing that, set NTFY_CMD_PIN. Write commands then need `pin=XXXX` on the
    line. This is deliberately weak — anyone subscribed to the topic can read
    the PIN out of a command you send within ntfy's retention window — but it
    does stop someone who learned the topic name from a screenshot or a URL
    from arming and disarming things immediately.
  * Whatever you do, keep this channel away from anything that moves money.
    A pushed Approve button that places an order (roadmap Phase 6.2) needs
    real authentication first, not an unguessable string.
"""
from __future__ import annotations

import json
import time
import traceback

import requests

from . import (config, notify, instruments, valuation, fundamentals, news,
               events, candles, kite_auth, results, intraday, alerts, charts,
               indicators, risk, journal, brief, portfolio, batch, heatmap, scanner, universe, walkforward)

HELP = (
    "TickerForge — commands\n"
    "\nIntraday (live):\n"
    "/brief SYM     one-look cockpit (price, levels, alerts, result, value)\n"
    "/levels SYM    VWAP, PDH/PDL, CPR, pivots, ORB, position\n"
    "/chart SYM     intraday chart image with levels drawn\n"
    "/ind SYM [tf]  RSI, Supertrend, EMAs, MACD, ADX right now\n"
    "/conf SYM      multi-timeframe confluence — do 5m/15m/1h/day agree?\n"
    "/exit SYM      where your trailing stop sits, and R so far\n"
    "/price SYM     last price + day change\n"
    "\nAlerts (fired to your phone):\n"
    "/alert SYM COND   arm one · /alerts [SYM] list · /unalert ID|SYM|all\n"
    "  NOTE: /alert and /unalert change state. If NTFY_CMD_PIN is set,\n"
    "        add pin=XXXX to those two commands.\n"
    "  price:      > 410 | < 395 | vwap | pdh | pdl | orb | 2%\n"
    "  supertrend: supertrend | supertrend up | supertrend down\n"
    "  RSI:        rsi > 70 | rsi < 30 | rsi 50 | rsi 45-55\n"
    "  EMA:        ema20 | above ema20 | below ema50 | ema20x50 | zone\n"
    "  other:      macd | bb | adx 25 | vol 3x | avwap | avwap above major\n"
    "  filter:     conf > 60 | conf < 50 | aligned\n"
    "  exit:       trail long entry=405 sl=398 | trail short 2.5x\n"
    "  add a timeframe (1m 5m 15m 1h day, default 5m) and/or 'repeat'\n"
    "  e.g. /alert BEL rsi > 70 15m repeat\n"
    "\nRisk & journal (size first, then log what you actually did):\n"
    "/premarket     the morning brief on demand — positions, tape, alerts\n"
    "  (bare /brief does the same; /brief SYM is the per-stock cockpit)\n"
    "/size SYM entry=405 sl=398   → quantity from your risk %\n"
    "  sl omitted → ATR stop suggested · add risk=1.5 to override\n"
    "/log buy SYM 405 sl=398 tag=st_adx   record the trade\n"
    "/close ID 419 reason=target   ·  /amend ID sl=411   trail the stop\n"
    "/journal [SYM]  open positions + today's R · /journal stats  by tag\n"
    "/portfolio     Kite holdings + valuation, signals, stop distance\n"
    "/batch STRATEGY  run it across the watchlist — breadth, not just the best\n"
    "/heatmap SYM STRATEGY fast=5:30:5 slow=30:80:10   peak or plateau?\n"
    "/scan 52w_high volume_surge   EOD scan across a universe\n"
    "/wf SYM STRATEGY fast=5:25:5   walk-forward — does it survive fresh data?\n"
    "/mc SYM STRATEGY               how bad could the drawdown have been?\n"
    "/query rsi(14) > 70 and adx(14) > 25   find stocks by any expression\n"
    "  ...sort by rs top 10 show pe   rank, trim and add columns\n"
    "/compare TAG             paper trades vs the same strategy backtested\n"
    "/sips                    SIP debits, soonest first\n"
    "/mf overlap A | B        how much two funds are the same fund\n"
    "/paper                   paper account: open, pending, expectancy\n"
    "/paper fill              fill last night's queue at today's open\n"
    "/paper queue sl=4        queue today's flips (after 15:35)\n"
    "  NOTE: /log, /close and /amend change state — add pin=XXXX if\n"
    "        NTFY_CMD_PIN is set. /size only calculates.\n"
    "\nStrategy signals (rules from the backtester, run on closed daily bars):\n"
    "/signals [SYM]  fresh flips + current long/flat stance per strategy\n"
    "\nResearch (any stock — fetched on demand, watchlist or not):\n"
    "/results [SYM|all|latest]  quarter numbers, the awaited date, or market feed\n"
    "/deliv [SYM]     delivery % vs its average; bare = spikes on your names\n"
    "/insider [SYM]   promoter/director trades & pledges (bare = your names, 7d)\n"
    "/valuation SYM   ·  /fundamentals SYM  ·  /news SYM  ·  /dividends SYM\n"
    "/candles SYM [day|5minute|minute] [count]   price history for the\n"
    "                NewsForge chart (sent as data, not a picture)\n"
    "\nSystem:\n"
    "/health         are the four services actually running?\n"
    "/approve TOKEN pin=XXXX   place a proposed order (single use)\n"
    "/reject TOKEN             discard it"
)


def _fmt_money(v, dp=2):
    return "—" if v is None else f"{v:,.{dp}f}"


def _ensure_fund(sym: str, ack: bool = True) -> tuple[str | None, str | None]:
    """Make sure we hold screener fundamentals for `sym`, fetching on demand
    for any stock (watchlist or not). Returns (resolved_symbol, error).

    error is a phone-ready message when the symbol is unknown or the scrape
    failed; otherwise None and the caller proceeds to read stored data.
    """
    inst = instruments.lookup(sym)
    if not inst:
        return None, f"Unknown symbol {sym}."
    S = inst["tradingsymbol"]
    if fundamentals.summary(S):            # already stored -> use it
        return S, None
    if ack:
        try:
            notify.send(f"Fetching {S} from screener… one moment.",
                        title="TickerForge", tags="hourglass_flowing_sand")
        except (Exception, SystemExit):
            pass
    try:
        fundamentals.fetch_and_store(S)
    except (Exception, SystemExit) as e:
        return S, (f"Couldn't fetch {S} from screener: {e}\n"
                   f"(screener may be rate-limiting — try again in a minute.)")
    return S, None


# ---------------------------------------------------------------- handlers
def cmd_valuation(sym: str) -> str:
    if not sym:
        return "Usage: /valuation SYM  (add 'full' for scenarios/quality/history)"
    parts = sym.split()
    sym = parts[0]
    full = any(p.lower() in ("full", "rich", "deep") for p in parts[1:])
    # optional overrides: re=13 g=10 years=8  (percent for re/g)
    over: dict = {}
    for p in parts[1:]:
        if "=" in p:
            k, _, val = p.partition("=")
            try:
                fv = float(val)
            except ValueError:
                continue
            if k in ("re", "g", "gterm"):
                over[{"g": "g_cap", "gterm": "g_term"}.get(k, k)] = fv / 100
            elif k == "years":
                over["years"] = fv
    S, err = _ensure_fund(sym)
    if err:
        return err
    r = valuation.value(S, over or None, rich=full)
    if not r:
        return f"Couldn't value {S} — screener data may be incomplete."
    comp = r.get("composite"); price = r.get("price"); mos = r.get("mos_pct")
    verdict = r.get("verdict", "")
    lines = [f"{r.get('symbol', sym)} — {verdict.upper()}"]
    lines.append(f"Price {_fmt_money(price)}  ·  Fair {_fmt_money(comp)}")
    if mos is not None:
        lines.append(f"Margin of safety: {mos:+.1f}%")
    asm = r.get("assumptions", {})
    lines.append(f"(re {asm.get('re', 0)*100:.1f}% · g {r.get('g_used_pct')}%"
                 f"{' · smart' if asm.get('re_is_smart') else ''})")

    if not full:
        models = r.get("models") or {}
        for k, v in models.items():
            if isinstance(v, (int, float)):
                lines.append(f"  {k}: {_fmt_money(v)}")
        lines.append("\nAdd 'full' for scenarios, quality & history bands.")
        return "\n".join(lines)

    rd = r.get("reverse_dcf") or {}
    if rd.get("implied_growth_pct") is not None:
        gap = rd.get("gap_pp")
        tag = ""
        if gap is not None:
            tag = ("  (demanding)" if gap > 3 else "  (conservative)"
                   if gap < -3 else "")
        lines.append(f"\nReverse DCF: price implies {rd['implied_growth_pct']}% "
                     f"growth vs {rd.get('hist_growth_pct')}% hist{tag}")
    elif rd.get("note"):
        lines.append(f"\nReverse DCF: {rd['note']}")

    sc = r.get("scenarios") or {}
    if sc:
        def fs(x): return _fmt_money(x.get("fair"))
        lines.append("Scenarios (fair): "
                     f"bear {fs(sc.get('bear', {}))} · "
                     f"base {fs(sc.get('base', {}))} · "
                     f"bull {fs(sc.get('bull', {}))}")

    q = r.get("quality") or {}
    qbits = []
    if q.get("roic_pct") is not None:
        vs = q.get("roic_vs_re_pp")
        qbits.append(f"ROIC {q['roic_pct']}%"
                     + (f" ({vs:+.1f} vs re)" if vs is not None else ""))
    if q.get("f_score") is not None:
        qbits.append(f"F {q['f_score']}/{q.get('f_checked', 9)}")
    if q.get("altman_z") is not None:
        qbits.append(f"Z {q['altman_z']} ({q.get('altman_zone')})")
    if qbits:
        lines.append("Quality: " + " · ".join(qbits))

    h = r.get("history") or {}
    pe = h.get("pe") if h else None
    if pe:
        lines.append(f"PE {pe['current']} vs {pe['median']} median "
                     f"[{pe['min']}–{pe['max']}, {pe['n']}y] — {pe['read']}")
    return "\n".join(lines)


def cmd_fundamentals(sym: str) -> str:
    """/fundamentals SYM — screener's headline numbers plus our own ratios.

    `summary()` returns two dicts, not a flat one: `snapshot` holds screener's
    display fields under their own labels ("Stock P/E", "ROCE"), already
    formatted with ₹ and %; `computed` holds the ratios we derive from the raw
    statements, which cover things screener doesn't show on the header —
    debt/equity, interest coverage, sales and profit CAGR.

    Both are label-keyed, so this reads them by label rather than by guessing
    at short names.
    """
    if not sym:
        return "Usage: /fundamentals SYM"
    S, err = _ensure_fund(sym)
    if err:
        return err
    s = fundamentals.summary(S)
    if not s:
        return (f"Nothing stored for {S}. It may have failed to fetch — "
                f"run `cli fund diagnose {S}` to see which stage broke.")

    snap = s.get("snapshot") or {}
    comp = s.get("computed") or {}
    out = [f"{s.get('symbol', S)} — fundamentals"
           + (" ⚠ stale" if s.get("stale") else "")]

    # screener's header, in the order that reads best on a phone
    for label in ("Current Price", "Market Cap", "Stock P/E", "Book Value",
                  "ROE", "ROCE", "Dividend Yield", "High / Low"):
        if snap.get(label):
            out.append(f"{label}: {snap[label]}")

    # anything screener showed that we didn't name above — better to surface an
    # unexpected field than to silently drop it when their layout changes
    for k, v in snap.items():
        if k not in ("Current Price", "Market Cap", "Stock P/E", "Book Value",
                     "ROE", "ROCE", "Dividend Yield", "High / Low",
                     "Face Value") and v:
            out.append(f"{k}: {v}")

    if comp:
        out.append("")
        out.append("Computed from the statements:")
        for k, v in comp.items():
            out.append(f"  {k}: {v}")

    if len(out) == 1:
        return (f"{S}: page is stored but no fields parsed from it — "
                f"run `cli fund diagnose {S}`.")
    out.append("")
    out.append(f"({s.get('variant', '?')}, fetched {s.get('fetched_at', '?')})")
    return "\n".join(out)


def cmd_news(sym: str) -> str:
    if not sym:
        return "Usage: /news SYM"
    inst = instruments.lookup(sym)
    name = inst["name"] if inst else sym
    try:
        items = news._fetch_all_sources(name, sym.upper(), hours=48)
    except (Exception, SystemExit) as e:
        return f"News fetch failed: {e}"
    if not items:
        return f"No recent news for {sym}."
    seen, out = set(), [f"{sym.upper()} — latest news"]
    for it in items:
        t = it["title"].strip()
        k = t.lower()[:60]
        if k in seen:
            continue
        seen.add(k)
        src = it.get("source", "")
        out.append(f"• {t}" + (f"  ({src})" if src else ""))
        if len(out) >= 7:
            break
    return "\n".join(out)


def _recent_events(category: str, sym: str, label: str) -> str:
    if sym:
        # pull this symbol's filings from NSE on demand, so it works for any
        # stock, not just watchlist ones already captured by the poller
        inst = instruments.lookup(sym)
        if not inst:
            return f"Unknown symbol {sym}."
        sym = inst["tradingsymbol"]
        rows = events.recent(limit=12, category=category, symbol=sym)
        if not rows:
            try:
                events.fetch_for_symbol(sym)
                rows = events.recent(limit=12, category=category, symbol=sym)
            except Exception as e:
                return f"Couldn't fetch {label} for {sym}: {e}"
    else:
        rows = events.recent(limit=12, category=category, symbol=None)
    if not rows:
        where = sym if sym else "market-wide"
        return f"No recent {label} ({where})."
    head = f"{sym} — {label}" if sym else f"Recent {label} (market-wide)"
    out = [head]
    for ev in rows[:10]:
        when = f"  · {ev['when']}" if ev.get("when") else ""
        out.append(f"• {ev['symbol']} — {ev['headline'][:60]}{when}")
    return "\n".join(out)


def cmd_results(sym: str) -> str:
    """/results SYM    -> the quarter's numbers if released, else the date.
    /results          -> watchlist overview (who's reported, who's due).
    /results latest   -> market-wide latest results feed (compact).
    /results all      -> recent market-wide RESULTS filings (raw NSE feed).
    """
    if not sym:
        return results.overview_text()
    low = sym.lower()
    if low in ("latest", "mkt", "market"):
        return _latest_feed("latest", "all")
    if low in ("np", "sales") or low.startswith("latest "):
        # e.g. "/results np" or "/results latest np"
        s = low.split()[-1]
        return _latest_feed(s if s in ("np", "sales") else "latest", "all")
    if low in ("mine", "latest mine", "watch"):
        return _latest_feed("latest", "watch")
    if low == "all":
        return _recent_events("RESULTS", "", "results")
    S, err = _ensure_fund(sym)
    if err:
        return err
    c = results.card(S)
    if c is None:
        return (f"Fetched {S}, but screener has no quarterly numbers for it "
                f"yet.")
    return results.as_text(c)


def _latest_feed(sort: str, scope: str) -> str:
    from . import latest_results as L
    try:
        if L.is_stale():
            L.refresh()
    except Exception:
        pass                          # serve cache if the scrape fails
    data = L.rows(scope=scope, sort=sort)
    total = len(L.rows(scope=scope))
    return L.as_text(data, sort=sort, scope=scope, total=total)


def cmd_dividends(sym: str) -> str:
    return _recent_events("DIVIDEND", sym, "dividends")


def cmd_price(sym: str) -> str:
    if not sym:
        return "Usage: /price SYM"
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    try:
        kite = kite_auth.get_kite()
        candles.ensure(kite, inst["instrument_token"], "day", 5)
        c = candles.get(inst["instrument_token"], "day", 5)
        if not c:
            return f"No candles for {sym}."
        last = c[-1]; prev = c[-2] if len(c) > 1 else last
        chg = last["close"] - prev["close"]
        pct = chg / prev["close"] * 100 if prev["close"] else 0
        return (f"{inst['tradingsymbol']} — {_fmt_money(last['close'])}  "
                f"({chg:+.2f}, {pct:+.2f}%)\n"
                f"O {_fmt_money(last['open'])}  H {_fmt_money(last['high'])}  "
                f"L {_fmt_money(last['low'])}  V {last['volume']:,}")
    except (Exception, SystemExit) as e:
        return f"Price lookup failed: {e}"


# ------------------------------------------------------- candles for the phone
# Bytes kept back from the message limit for the JSON around the bars.
# The wrapper is about 120 characters; the rest is slack for a long symbol.
_PAYLOAD_OVERHEAD = 220


def _split_by_size(rows: list[str], limit: int) -> list[list[str]]:
    """Group encoded rows into messages that fit the server's limit.

    Measured rather than estimated. A row is between 41 and 53 characters
    depending on how wide the price is — three digits for most of the market,
    six for MRF — so a fixed bar count has to assume the worst and wastes a
    third of every message on anything normal. Filling to the actual byte
    budget means a self-hosted 64 KB server sends a year in one message and a
    4 KB one still never overflows.
    """
    budget = max(limit - _PAYLOAD_OVERHEAD, 512)
    out, current, size = [], [], 0
    for row in rows:
        cost = len(row) + 1                       # the ';' that joins it
        if current and size + cost > budget:
            out.append(current)
            current, size = [], 0
        current.append(row)
        size += cost
    if current:
        out.append(current)
    return out

# Bars a trading day yields, per interval, used to turn a bar COUNT into the
# day window candles.get() wants. Deliberately generous: asking for a few extra
# days costs nothing (the store is already topped up) and coming up short means
# the chart silently draws less history than was asked for.
BARS_PER_DAY = {"minute": 375, "3minute": 125, "5minute": 75,
                "10minute": 38, "15minute": 25, "30minute": 13,
                "60minute": 7, "day": 1}


def _candle_rows(bars: list[dict]) -> tuple[int, str]:
    """Encode bars as `t0` plus one positional string.

    The reader flattens JSON payloads to dotted keys and skips arrays outright,
    so the bars cannot travel as a list of objects — and at ~40 bytes a row
    that shape would blow the message limit anyway. Hence: rows separated by
    `;`, fields by `,`, offsets measured from the bar before.

    Times come from candles.series_utc(), not candles.get(): the latter carries
    IST_OFFSET so lightweight-charts renders UTC as IST, and resolves naive
    timestamps in the machine's own zone. Both are right for the local chart and
    wrong on the wire — the phone stores these as real epoch millis.
    """
    t0 = bars[0]["time"]
    out, prev = [], t0
    for b in bars:
        out.append(_encode_row(b, prev))
        prev = b["time"]
    return t0, ";".join(out)


def _encode_row(bar: dict, previous_time: int) -> str:
    """One bar: seconds since the bar before, then open/high/low/close/volume."""
    return (
        f"{bar['time'] - previous_time},{round(bar['open'], 2)},"
        f"{round(bar['high'], 2)},{round(bar['low'], 2)},"
        f"{round(bar['close'], 2)},{int(bar['volume'] or 0)}"
    )


def cmd_candles(args: list[str]):
    """/candles SYM [interval] [count] — price history for the NewsForge chart.

    Read-only: it fetches and publishes, it changes nothing, so it sits with
    /price and /levels rather than behind the PIN.

    Replies as several ordinary messages rather than one chunked transfer. The
    phone keys bars on (symbol, interval, open time), so each message is an
    independent upsert — order does not matter, a lost one is re-requested by
    the next open, and a duplicate is harmless. That same property is what lets
    a single-bar message correct the bar still forming.
    """
    if not args:
        return "Usage: /candles SYM [day|5minute|minute] [count]"
    sym = args[0]
    interval = args[1] if len(args) > 1 else "day"
    if interval not in candles.INTERVAL_MAX_DAYS:
        return (f"Unknown interval {interval}. "
                f"One of: {', '.join(sorted(candles.INTERVAL_MAX_DAYS))}")
    try:
        count = int(args[2]) if len(args) > 2 else 300
    except ValueError:
        return f"Bad count {args[2]!r} — expected a number."
    count = max(1, min(count, 2000))

    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."

    token, tradingsymbol = inst["instrument_token"], inst["tradingsymbol"]
    # Calendar days, not trading days: ~250 sessions a year, so pad by half
    # again and let the slice below take exactly what was asked for.
    per_day = BARS_PER_DAY.get(interval, 1)
    days = max(2, int(count / per_day * 1.5) + 2)
    try:
        candles.ensure(kite, token, interval, days)
        bars = candles.series_utc(token, interval, days)
    except (Exception, SystemExit) as e:
        return f"Candles failed for {tradingsymbol}: {e}"
    if not bars:
        return f"No {interval} candles for {tradingsymbol}."
    bars = bars[-count:]

    # Encode once, then group by size. The offsets are cumulative, so each group
    # is re-encoded from its own first bar to keep every message self-contained.
    # Widths only — each group is re-encoded from its own first bar below, so
    # these are measured against the preceding bar purely to size the split.
    widths = [
        _encode_row(bar, previous["time"])
        for bar, previous in zip(bars, [bars[0]] + bars[:-1])
    ]
    groups = _split_by_size(widths, config.NTFY_MESSAGE_LIMIT)

    sent = 0
    index = 0
    for group in groups:
        chunk = bars[index:index + len(group)]
        index += len(group)
        t0, rows = _candle_rows(chunk)
        payload = json.dumps({
            "nf": 1,
            "kind": "candles",
            "symbol": tradingsymbol,
            "interval": interval,
            "t0": t0,
            "bars": rows,
        }, separators=(",", ":"))
        # priority=min: these are data for the reader, not something to buzz
        # about. The phone polls the topic either way.
        # Stop at the first refusal instead of pushing the rest at a closed
        # door. The phone keys bars on (symbol, interval, open time), so a
        # partial history is simply less history — the next time the screen is
        # opened it asks again and the gap fills in.
        if not notify.send(payload, title=f"{tradingsymbol} {interval} candles",
                           priority="min", tags="chart_with_upwards_trend"):
            print(f"  candles: stopped after {sent} of {len(groups)} messages")
            break
        sent += 1
    print(f"  candles: {tradingsymbol} {interval} "
          f"{len(bars)} bars in {sent} messages")
    return None                        # payloads already pushed; no extra buzz


def _kite_or_none():
    try:
        return kite_auth.get_kite()
    except SystemExit:
        return None


def _fmt1(v):
    return "—" if v is None else f"{v:,.1f}"


# ---------------------------------------------------------------- intraday
def cmd_levels(sym: str) -> str:
    if not sym:
        return "Usage: /levels SYM"
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."
    lv = intraday.levels(kite, inst["tradingsymbol"], inst["instrument_token"])
    if lv.get("ltp") is None:
        return (f"No live quote for {inst['tradingsymbol']} "
                f"(market closed or no data).")
    return intraday.as_text(lv)


# ---------------------------------------------------------------- the Board
def cmd_board(arg: str = ""):
    """Top 5 each side of the F&O order-block board.

    `/board nf` is the same board for the NewsForge app instead: one
    structured message (see board.as_payload), pushed at priority=min like
    /candles, and no text reply to buzz about.
    """
    from . import board
    kite = _kite_or_none()
    try:
        b = board.get(lambda: kite or kite_auth.get_kite())
    except SystemExit:
        return "No Kite session — log in first."
    if (arg or "").strip().lower() == "nf":
        payload = board.as_payload(b, config.NTFY_MESSAGE_LIMIT)
        if not notify.send(payload, title="Board", priority="min", tags="bar_chart"):
            return "Board payload could not be sent."
        return None
    return board.as_text(b, n=5)


def cmd_delivery(sym: str) -> str:
    """/deliv SYM — last session's delivery %; bare /deliv lists the spikes
    among the names you follow."""
    from . import delivery
    sym = (sym or "").strip()
    if sym:
        inst = instruments.lookup(sym)
        return delivery.as_text(inst["tradingsymbol"] if inst else sym)
    from . import portfolio
    rows = delivery.spikes(list(portfolio.followed()), up_only=False)
    if not rows:
        d = delivery.last_day()
        return ("No delivery spikes among your names" + (f" on {d}." if d else
                " — no delivery data yet. Run `python -m app.cli delivery sync`."))
    out = [f"Delivery spikes · {rows[0]['day']} (≥{delivery.SPIKE_RATIO:g}× avg)"]
    for r in rows[:12]:
        chg = "" if r["change_pct"] is None else f" {r['change_pct']:+.1f}%"
        out.append(f"  {r['symbol']:<12} {r['deliv_ratio']:.1f}× · "
                   f"{r['deliv_pct']:.0f}% delivered{chg}")
    return "\n".join(out)


def cmd_insider(sym: str) -> str:
    """/insider SYM — promoter/director trades and pledges; bare /insider
    lists the last week across the names you follow."""
    from . import insider
    sym = (sym or "").strip()
    if sym:
        inst = instruments.lookup(sym)
        return insider.as_text(inst["tradingsymbol"] if inst else sym)
    from . import portfolio
    rows = insider.recent(days=7, symbols=list(portfolio.followed()))
    if not rows:
        return "No promoter/director trades or pledges on your names this week."
    return "Insiders · last 7 days\n" + "\n".join(
        "  " + insider.line(r) for r in rows[:15])


def cmd_ob(sym: str) -> str:
    """One stock's order-block zone, levels and score breakdown."""
    if not sym:
        return "Usage: /ob SYM"
    from . import board
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."
    d = board.detail(kite, sym)
    if d is None:
        return f"Unknown symbol {sym}."
    return board.detail_text(d)


def cmd_chart(sym: str):
    if not sym:
        return "Usage: /chart SYM"
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."
    S, tok = inst["tradingsymbol"], inst["instrument_token"]
    lv = intraday.levels(kite, S, tok)
    try:
        candles_today = intraday.today_minutes(kite, tok, "5minute")
        png = charts.intraday_png(lv, candles_today,
                                  subtitle=time.strftime("%H:%M"))
        notify.send_image(png, caption=intraday.one_line(lv),
                          title=f"{S} intraday",
                          tags="chart_with_upwards_trend")
        return None                    # image already pushed; no extra buzz
    except (Exception, SystemExit) as e:
        return f"Chart failed: {e}\n\n" + intraday.as_text(lv)


def cmd_indicators(arg: str) -> str:
    """/ind SYM [tf] — where the indicators stand right now."""
    if not arg:
        return "Usage: /ind SYM [1m|5m|15m|1h|day]"
    parts = arg.split()
    inst = instruments.lookup(parts[0])
    if not inst:
        return f"Unknown symbol {parts[0]}."
    tf = alerts.DEFAULT_TF
    for p in parts[1:]:
        if p.lower() in alerts.TIMEFRAMES:
            tf = alerts.TIMEFRAMES[p.lower()]
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."
    from . import watcher
    S, tok = inst["tradingsymbol"], inst["instrument_token"]
    lv = intraday.levels(kite, S, tok)
    ind = watcher._IndCache().bundle(kite, tok, tf, lv.get("ltp"))
    if not ind or not ind.get("now"):
        return f"No candles for {S} on {alerts._tf_label(tf)} yet."
    n = ind["now"]
    out = [f"{S} — indicators [{alerts._tf_label(tf)}]",
           intraday.one_line(lv)]
    if n.get("rsi") is not None:
        z = "overbought" if n["rsi"] >= 70 else "oversold" if n["rsi"] <= 30 else ""
        out.append(f"RSI(14) {n['rsi']:.1f}{('  ' + z) if z else ''}")
    if n.get("st_dir") is not None:
        out.append(f"Supertrend {'UP' if n['st_dir'] == 1 else 'DOWN'}"
                   + (f" @ {n['st_value']:,.1f}" if n.get("st_value") else ""))
    e = n.get("ema") or {}
    ema_bits = [f"{p}: {v:,.1f}" for p, v in sorted(e.items()) if v is not None]
    if ema_bits:
        out.append("EMA " + " · ".join(ema_bits))
    c = n.get("close")
    if c and e.get(20) and e.get(50):
        out.append("Zone: " + (alerts._zone_of(c, e[20], e[50], 20, 50) or "—"))
    if n.get("macd") is not None and n.get("macd_signal") is not None:
        side = "above" if n["macd"] >= n["macd_signal"] else "below"
        out.append(f"MACD {n['macd']:+.2f} ({side} signal)")
    if n.get("adx") is not None:
        strength = "trending" if n["adx"] >= 25 else "choppy"
        out.append(f"ADX {n['adx']:.0f} — {strength}")
    if n.get("bb_upper") and n.get("bb_lower"):
        out.append(f"Bollinger {n['bb_lower']:,.1f} – {n['bb_upper']:,.1f}")
    if n.get("avwap"):
        side = "above" if c and c > n["avwap"] else "below"
        out.append(f"Anchored VWAP {n['avwap']:,.1f} — price {side}")
    if n.get("ce_long"):
        out.append(f"Chandelier stop (long) {n['ce_long']:,.1f}"
                   + (f" · short {n['ce_short']:,.1f}" if n.get("ce_short") else ""))
    return "\n".join(out)


def cmd_confluence(arg: str) -> str:
    """/conf SYM — do the timeframes agree?"""
    if not arg:
        return "Usage: /conf SYM"
    inst = instruments.lookup(arg.split()[0])
    if not inst:
        return f"Unknown symbol {arg.split()[0]}."
    kite = _kite_or_none()
    if not kite:
        return "No Kite session — log in first."
    from . import confluence as C, watcher
    S, tok = inst["tradingsymbol"], inst["instrument_token"]
    lv = intraday.levels(kite, S, tok)
    conf = watcher.confluence_for(kite, watcher._IndCache(), tok, lv.get("ltp"))
    if not conf:
        return f"{S}: not enough candle history yet."
    out = [intraday.one_line(lv), ""]
    out.append(C.as_text(S, conf, tf_label=alerts._tf_label))
    return "\n".join(out)


def cmd_exit(arg: str) -> str:
    """/exit SYM — where the trailing stop sits on armed exit alerts."""
    if not arg:
        return "Usage: /exit SYM   (arm one with /alert SYM trail long entry=405)"
    inst = instruments.lookup(arg.split()[0])
    if not inst:
        return f"Unknown symbol {arg.split()[0]}."
    S = inst["tradingsymbol"]
    rows = [r for r in alerts.active(S) if r["kind"] == "trail_stop"]
    if not rows:
        return (f"No trailing stop armed on {S}.\n"
                f"Arm one: /alert {S} trail long entry=405")
    kite = _kite_or_none()
    lv = intraday.levels(kite, S, inst["instrument_token"]) if kite else {}
    price = lv.get("ltp")
    out = [f"{S} — trailing stops" + (f"  (now {price:,.2f})" if price else "")]
    for r in rows:
        try:
            p = json.loads(r["params"] or "{}")
            st = json.loads(r["state"] or "{}")
        except (TypeError, ValueError):
            continue
        stop = st.get("stop")
        side = p.get("side", "long")
        line = f"#{r['id']} {side} {p.get('mult', 3):g}×ATR"
        if stop:
            line += f" · stop {stop:,.2f}"
            if price:
                dist = (price - stop) if side == "long" else (stop - price)
                line += f" ({dist:+,.2f} away)"
        if p.get("entry") and price:
            rr = indicators.r_multiple(p["entry"],
                                       p.get("init") or st.get("init_stop"),
                                       price, side)
            if rr is not None:
                line += f" · {rr:+.2f}R"
        out.append(line)
    return "\n".join(out)


def cmd_alert(tokens: list[str]) -> str:
    usage = ("Usage: /alert SYM COND\n"
             "COND: > 410 | < 395 | vwap | pdh | pdl | orb | 2%")
    if len(tokens) < 2:
        return usage
    sym = tokens[0]
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    try:
        kind, params, lbl = alerts.parse(tokens[1:])
    except ValueError as e:
        return f"Couldn't read that: {e}\n{usage}"
    params = {**params, "label": lbl}          # keep the readable form
    aid = alerts.add(inst["tradingsymbol"], inst["instrument_token"],
                     kind, params)
    return (f"Armed #{aid}: {inst['tradingsymbol']} {lbl}\n"
            f"You'll get a chart the moment it triggers.")


def cmd_alerts(sym: str) -> str:
    rows = alerts.active(sym or None)
    if not rows:
        return (f"No armed alerts for {sym.upper()}." if sym
                else "No armed alerts. Set one with /alert SYM COND.")
    out = ["Armed alerts:"]
    for r in rows:
        out.append(f"#{r['id']}  {alerts.label(r)}")
    out.append("Remove: /unalert ID | SYM | all")
    return "\n".join(out)


def cmd_unalert(target: str) -> str:
    if not target:
        return "Usage: /unalert ID | SYM | all"
    n = alerts.remove(target)
    return f"Removed {n} alert(s)." if n else "Nothing matched."


def cmd_brief(sym: str) -> str:
    """One clean, scannable cockpit for a stock: live + levels + alerts +
    result + valuation + a headline + next event. Each line is optional."""
    if not sym:
        return "Usage: /brief SYM"
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."
    S = inst["tradingsymbol"]
    lines: list[str] = []

    kite = _kite_or_none()
    lv = intraday.levels(kite, S, inst["instrument_token"]) if kite else None
    if lv and lv.get("ltp") is not None:
        lines.append(intraday.one_line(lv))
        piv = lv.get("pivots") or {}
        seg = (f"VWAP {_fmt1(lv.get('vwap'))} · PDH {_fmt1(lv.get('pdh'))} "
               f"PDL {_fmt1(lv.get('pdl'))}")
        if piv:
            seg += f" · P {_fmt1(piv.get('P'))}"
        lines.append(seg)
    else:
        lines.append(f"{S} — no live quote (market closed?)")

    ar = alerts.active(S)
    if ar:
        lines.append("Alerts: " + ", ".join(
            alerts.label(r).replace(S + " ", "") for r in ar))

    try:
        c = results.card(S)
        if c and not c.get("missing"):
            if c["released"]:
                np = next((r["yoy_growth"] for r in c["rows"]
                           if r["label"] == "Net Profit"), None)
                lines.append(f"Result {c['quarter']}: ✓ NP {np:+.1f}% YoY"
                             if np is not None
                             else f"Result {c['quarter']}: ✓ out")
            else:
                d = c.get("date")
                lines.append(f"Result {c['quarter']}: due {d}" if d
                             else f"Result {c['quarter']}: not out yet")
    except Exception:
        pass

    try:
        v = valuation.value(S)
        if v and v.get("verdict"):
            mos = v.get("mos_pct")
            m = f" · MoS {mos:+.0f}%" if mos is not None else ""
            lines.append(f"Value: {v['verdict'].upper()}{m}")
    except Exception:
        pass

    try:
        items = news._fetch_all_sources(inst.get("name") or S, S, hours=48)
        if items:
            lines.append("News: " + items[0]["title"][:72])
    except (Exception, SystemExit):
        pass

    try:
        evs = events.recent(limit=5, symbol=S)
        if evs:
            e = evs[0]
            when = f" · {e['when']}" if e.get("when") else ""
            lines.append(f"Next: {e['headline'][:48]}{when}")
    except Exception:
        pass

    return "\n".join(lines)


def cmd_signals(sym: str) -> str:
    """/signals [SYM] — run the configured strategy scan (closed daily bars)."""
    from . import signals
    return signals.summary_text(sym or None)


def _account() -> tuple[float, float]:
    """Account size and risk %, from config. Zero account = not set up yet."""
    return (float(config.get("ACCOUNT_SIZE", 0) or 0),
            float(config.get("RISK_PCT", 1) or 1))


def _kv(tokens: list[str]) -> tuple[dict, list[str]]:
    """Split `k=v` tokens from bare ones. Lets arguments arrive in any order,
    which matters when you're typing on a phone at 09:20."""
    kv, rest = {}, []
    for t in tokens:
        if "=" in t and not t.startswith("="):
            k, _, v = t.partition("=")
            kv[k.lower()] = v
        else:
            rest.append(t)
    return kv, rest


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cmd_size(tokens: list[str]) -> str:
    """/size SYM entry=405 sl=398 — how many shares to buy.

    With no stop given, suggests one from ATR so the answer is still usable;
    with no entry, uses the last close. Both are conveniences, not defaults
    you should rely on — a stop at a level you chose beats a stop from a
    formula.
    """
    usage = ("Usage: /size SYM entry=405 sl=398\n"
             "  sl omitted → ATR stop suggested · entry omitted → last close\n"
             "  optional: risk=1.5 (% of capital) · account=500000")
    if not tokens:
        return usage

    account, risk_pct = _account()
    kv, rest = _kv(tokens)
    if kv.get("account"):
        account = _num(kv["account"]) or account
    if kv.get("risk"):
        risk_pct = _num(kv["risk"]) or risk_pct
    if account <= 0:
        return ("No ACCOUNT_SIZE set. Add it to .env (e.g. ACCOUNT_SIZE=500000, "
                "RISK_PCT=1) or pass account=500000 on the command.")

    sym = rest[0] if rest else ""
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}.\n{usage}"

    entry = _num(kv.get("entry") or kv.get("e"))
    stop = _num(kv.get("sl") or kv.get("stop"))
    # bare numbers, in order: entry then stop
    bare = [n for n in (_num(t) for t in rest[1:]) if n is not None]
    if entry is None and bare:
        entry = bare.pop(0)
    if stop is None and bare:
        stop = bare.pop(0)

    note = ""
    if entry is None or stop is None:
        try:
            kite = kite_auth.get_kite()
            candles.ensure(kite, inst["instrument_token"], "day", 60)
            c = candles.get(inst["instrument_token"], "day", 60)
        except (Exception, SystemExit) as e:
            return f"Need entry and sl — couldn't fetch candles to infer them: {e}"
        if not c:
            return "Need entry= and sl= (no candles available to infer them)."
        if entry is None:
            entry = c[-1]["close"]
            note = f"entry assumed = last close {entry:g}"
        if stop is None:
            side = "short" if kv.get("side", "").startswith("s") else "long"
            sug = risk.atr_stop(c, entry=entry, side=side, period=14, mult=2)
            if not sug:
                return "Need sl= (not enough history for an ATR stop)."
            stop = sug["stop"]
            note = ((note + " · " if note else "") +
                    f"stop from 2×ATR({sug['atr']:g}) = {stop:g}, "
                    f"{sug['distance_pct']:g}% away")

    try:
        s = risk.size(entry, stop, account, risk_pct,
                      max_position_pct=float(config.get("MAX_POSITION_PCT", 100)))
    except risk.RiskError as e:
        return f"Can't size that: {e}"

    out = [f"{inst['tradingsymbol']} — {risk.format_size(s)}"]
    if note:
        out.append(f"({note})")

    g = journal.guard(daily_loss_r=float(config.get("DAILY_LOSS_R", 2)),
                      max_positions=int(config.get("MAX_POSITIONS", 5)))
    if not g["ok"]:
        out.append("⛔ " + " · ".join(g["reasons"]))
    return "\n".join(out)


def cmd_log(tokens: list[str]) -> str:
    """/log buy SYM 405 sl=398 tag=st_adx — record a trade you've taken."""
    usage = ("Usage: /log buy SYM 405 sl=398 [qty=100] [tag=st_adx]\n"
             "  buy|sell · price positional or entry= · qty omitted → sized "
             "from your risk settings")
    if len(tokens) < 2:
        return usage

    kv, rest = _kv(tokens)
    side = "long"
    if rest and rest[0].lower() in ("buy", "long", "b"):
        rest.pop(0)
    elif rest and rest[0].lower() in ("sell", "short", "s"):
        side = "short"
        rest.pop(0)

    if not rest:
        return usage
    sym = rest.pop(0)
    inst = instruments.lookup(sym)
    if not inst:
        return f"Unknown symbol {sym}."

    bare = [n for n in (_num(t) for t in rest) if n is not None]
    entry = _num(kv.get("entry") or kv.get("e")) or (bare.pop(0) if bare else None)
    stop = _num(kv.get("sl") or kv.get("stop")) or (bare.pop(0) if bare else None)
    if entry is None:
        return f"Need an entry price.\n{usage}"

    qty = kv.get("qty") or kv.get("q")
    qty = int(_num(qty)) if qty else None
    sized_note = ""
    if qty is None:
        account, risk_pct = _account()
        if account > 0 and stop is not None:
            try:
                s = risk.size(entry, stop, account, risk_pct,
                              max_position_pct=float(config.get("MAX_POSITION_PCT", 100)))
                qty = s["qty"]
                sized_note = f"qty {qty} from {risk_pct:g}% risk"
            except risk.RiskError as e:
                return f"Can't size that: {e}"
        if not qty:
            return f"Need qty= (no ACCOUNT_SIZE set, or no stop given).\n{usage}"

    try:
        tid = journal.open_trade(
            inst["tradingsymbol"], entry=entry, qty=qty, stop=stop, side=side,
            target=_num(kv.get("target") or kv.get("tp")),
            tag=kv.get("tag"), notes=kv.get("note") or kv.get("notes"))
    except journal.JournalError as e:
        return f"Not logged: {e}"

    t = journal.get(tid)
    out = [f"Logged {journal.format_trade(t)}"]
    if sized_note:
        out.append(sized_note)
    if stop is None:
        out.append("⚠ no stop — this trade can't be scored in R and won't "
                   "count toward your stats. Add one: /amend "
                   f"{tid} sl=<price>")
    else:
        tgt = risk.targets(entry, stop)
        out.append("targets  " + " · ".join(f"{k} {v:g}" for k, v in tgt.items()))
    return "\n".join(out)


def cmd_close(tokens: list[str]) -> str:
    """/close ID 419 [reason=target] — close a logged trade."""
    usage = "Usage: /close ID 419 [reason=stop|target|signal|manual]"
    if len(tokens) < 2:
        return usage
    kv, rest = _kv(tokens)
    ids = [n for n in (_num(t) for t in rest) if n is not None]
    if len(ids) < 2:
        return usage
    tid, price = int(ids[0]), ids[1]
    try:
        t = journal.close_trade(
            tid, exit_price=price,
            reason=kv.get("reason") or kv.get("r") or "manual",
            charges=_num(kv.get("charges")),
            notes=kv.get("note") or kv.get("notes"))
    except journal.JournalError as e:
        return f"Not closed: {e}"

    out = [f"Closed {journal.format_trade(t)}"]
    g = journal.guard(daily_loss_r=float(config.get("DAILY_LOSS_R", 2)),
                      max_positions=int(config.get("MAX_POSITIONS", 5)))
    out.append(f"today {g['realised_r']:+.2f}R · {g['remaining_r']:.2f}R left "
               f"before the limit")
    if not g["ok"]:
        out.append("⛔ " + " · ".join(g["reasons"]) + "\nStand down for today.")
    return "\n".join(out)


def cmd_amend(tokens: list[str]) -> str:
    """/amend ID sl=402 tag=st_adx — trail a stop or fix a label."""
    usage = "Usage: /amend ID sl=402 | target=430 | tag=st_adx | note=…"
    if len(tokens) < 2:
        return usage
    kv, rest = _kv(tokens)
    ids = [n for n in (_num(t) for t in rest) if n is not None]
    if not ids:
        return usage
    fields = {}
    if "sl" in kv or "stop" in kv:
        fields["stop"] = _num(kv.get("sl") or kv.get("stop"))
    if "target" in kv or "tp" in kv:
        fields["target"] = _num(kv.get("target") or kv.get("tp"))
    if "tag" in kv:
        fields["tag"] = kv["tag"]
    if "note" in kv or "notes" in kv:
        fields["notes"] = kv.get("note") or kv.get("notes")
    if "qty" in kv:
        fields["qty"] = int(_num(kv["qty"]))
    if not fields:
        return usage
    try:
        t = journal.update_trade(int(ids[0]), **fields)
    except journal.JournalError as e:
        return f"Not amended: {e}"
    out = [f"Updated {journal.format_trade(t)}"]
    if "stop" in fields and t.get("initial_stop") is not None:
        out.append(f"(R still measured against the entry stop "
                   f"{t['initial_stop']:g})")
    return "\n".join(out)


def cmd_batch(tokens: list[str]) -> str:
    """/batch STRATEGY [SYM ...] — one strategy across the watchlist."""
    from . import backtest as bt
    if not tokens:
        return ("Usage: /batch STRATEGY [SYM ...]\n"
                "Strategies: " + ", ".join(sorted(bt.STRATEGIES)))
    kv, rest = _kv(tokens)
    strat = rest[0].lower() if rest else ""
    if strat not in bt.STRATEGIES:
        return (f"Unknown strategy '{strat}'.\n"
                "Try: " + ", ".join(sorted(bt.STRATEGIES)[:8]) + " …")
    try:
        res = batch.run(strat, symbols=[s.upper() for s in rest[1:]] or None,
                        days=int(_num(kv.get("days")) or 730),
                        cost_bps=_num(kv.get("cost")) or 10.0)
    except (ValueError, SystemExit) as e:
        return f"Batch failed: {e}"
    return batch.as_text(res, limit=8)


def cmd_walkforward(tokens: list[str]) -> str:
    """/wf SYM STRATEGY fast=5:25:5 — does it survive data it never saw?"""
    from . import backtest as bt
    usage = ("Usage: /wf SYM STRATEGY [param=5:25:5 ...]\n"
             "  optional: is=250 oos=60 mode=rolling|anchored metric=sharpe")
    if len(tokens) < 2:
        return usage
    kv, rest = _kv(tokens)
    if len(rest) < 2:
        return usage
    sym, strat = rest[0].upper(), rest[1].lower()
    if strat not in bt.STRATEGIES:
        return f"Unknown strategy '{strat}'."

    reserved = {"is", "oos", "mode", "metric", "days", "cost"}
    grid = {}
    for k, v in kv.items():
        if k in reserved:
            continue
        grid[k] = (v if ":" in v else
                   [int(float(x)) if float(x).is_integer() else float(x)
                    for x in v.split(",") if x.strip()])
    try:
        res = walkforward.run(
            sym, strat, grid or None,
            is_bars=int(_num(kv.get("is")) or 250),
            oos_bars=int(_num(kv.get("oos")) or 60),
            mode=kv.get("mode", "rolling"),
            metric=kv.get("metric", "sharpe"),
            days=int(_num(kv.get("days")) or 1460),
            cost_bps=_num(kv.get("cost")) or 10.0)
    except (ValueError, SystemExit) as e:
        return f"Walk-forward failed: {e}"
    return walkforward.as_text(res)


def cmd_scan(tokens: list[str]) -> str:
    """/scan 52w_high volume_surge — run an EOD scan across a universe."""
    if not tokens:
        saved = universe.saved_scans()
        return ("Usage: /scan CONDITION [CONDITION ...] [universe=nifty200]\n"
                "Conditions: " + ", ".join(scanner.CONDITIONS) +
                (("\nSaved: " + ", ".join(s["name"] for s in saved))
                 if saved else ""))
    kv, rest = _kv(tokens)

    # a single unknown word that matches a saved scan is a saved-scan run
    if len(rest) == 1 and rest[0] not in scanner.CONDITIONS:
        if universe.get_scan(rest[0]):
            try:
                return scanner.as_text(scanner.run_saved(rest[0]))
            except ValueError as e:
                return str(e)

    bad = [c for c in rest if c not in scanner.CONDITIONS]
    if bad:
        return (f"Unknown condition(s): {', '.join(bad)}\n"
                "Available: " + ", ".join(scanner.CONDITIONS))
    try:
        res = scanner.scan(list(rest),
                           name=kv.get("universe", universe.DEFAULT_UNIVERSE),
                           days=int(_num(kv.get("days")) or 400),
                           limit=int(_num(kv.get("limit")) or 15))
    except (ValueError, SystemExit) as e:
        return f"Scan failed: {e}"
    return scanner.as_text(res, limit=12)


def cmd_heatmap(tokens: list[str]) -> str:
    """/heatmap SYM STRATEGY fast=5:30:5 slow=30:80:10 — peak or plateau?"""
    from . import backtest as bt
    usage = ("Usage: /heatmap SYM STRATEGY p1=5,10,15 [p2=30:80:10]\n"
             "  optional: metric=sharpe|profit_factor|expectancy_pct · days=730")
    if len(tokens) < 3:
        return usage
    kv, rest = _kv(tokens)
    if len(rest) < 2:
        return usage
    sym, strat = rest[0].upper(), rest[1].lower()
    if strat not in bt.STRATEGIES:
        return f"Unknown strategy '{strat}'."

    sweeps = [(k, v) for k, v in kv.items()
              if k not in ("metric", "days", "cost")]
    if not sweeps:
        return usage

    def vals(spec):
        # ranges stay strings; comma lists become explicit values
        if ":" in spec:
            return spec
        return [int(float(x)) if float(x).is_integer() else float(x)
                for x in spec.split(",") if x.strip()]

    try:
        xp, xv = sweeps[0][0], vals(sweeps[0][1])
        yp, yv = (sweeps[1][0], vals(sweeps[1][1])) if len(sweeps) > 1 else (None, None)
        res = heatmap.sweep(sym, strat, xp, xv, yp, yv,
                            metric=kv.get("metric", "sharpe"),
                            days=int(_num(kv.get("days")) or 730),
                            cost_bps=_num(kv.get("cost")) or 10.0)
    except (ValueError, SystemExit) as e:
        return f"Sweep failed: {e}"
    return heatmap.as_text(res)


def cmd_portfolio(arg: str) -> str:
    """/portfolio — holdings with valuation, signal stance and stop distance."""
    try:
        snap = portfolio.snapshot()
    except portfolio.PortfolioError as e:
        return f"Portfolio unavailable: {e}"
    if not snap["holdings"] and not snap["positions"]:
        return "No holdings or open positions at Kite."
    return portfolio.as_text(snap)


def cmd_premarket(arg: str) -> str:
    """/premarket — the pre-market brief on demand, any time of day.

    Note this shadows nothing: /brief SYM is the per-stock cockpit, /brief with
    no symbol is the market-wide morning brief. Two different questions that
    happen to share a word.
    """
    skip = tuple(x.strip() for x in (arg or "").split(",") if x.strip())
    return brief.compose(skip=skip)


def cmd_journal(arg: str) -> str:
    """/journal [SYM|tag|stats] — open positions, today's R, and expectancy."""
    arg = (arg or "").strip()
    lines = []

    if arg.lower() in ("stats", "tags", "tag"):
        groups = journal.by_tag()
        if not groups:
            return "No closed trades yet."
        for tag, s in groups.items():
            lines.append(journal.format_stats(s, label=f"{tag}: "))
        return "\n\n".join(lines)

    sym = arg.upper() if arg else None
    opens = journal.open_positions(sym)
    if opens:
        lines.append(f"Open ({len(opens)}):")
        for t in opens:
            last = None
            try:
                inst = instruments.lookup(t["symbol"])
                if inst:
                    c = candles.get(inst["instrument_token"], "day", 5)
                    last = c[-1]["close"] if c else None
            except Exception:
                pass
            lines.append("  " + journal.format_trade(t, last_price=last))
    else:
        lines.append("No open positions.")

    recent = journal.closed_trades(symbol=sym, limit=5)
    if recent:
        lines.append("")
        lines.append("Recent:")
        lines += ["  " + journal.format_trade(t) for t in recent]

    lines.append("")
    lines.append(journal.format_stats(journal.stats(), label="All: "))

    g = journal.guard(daily_loss_r=float(config.get("DAILY_LOSS_R", 2)),
                      max_positions=int(config.get("MAX_POSITIONS", 5)))
    lines.append(f"Today {g['realised_r']:+.2f}R realised · "
                 f"{g['open_risk_r']:.2f}R live · "
                 f"{g['remaining_r']:.2f}R to the limit")
    if not g["ok"]:
        lines.append("⛔ " + " · ".join(g["reasons"]))
    return "\n".join(lines)


def cmd_montecarlo(arg: str) -> str:
    """`/mc SYM STRATEGY [sims=2000] [mode=bootstrap]`

    The question a single backtest can't answer: was that drawdown typical, or
    was it the ordering you happened to get?
    """
    from . import backtest as B, montecarlo as MC

    parts = arg.split()
    if len(parts) < 2:
        return ("Usage: /mc SYM STRATEGY [sims=2000] [mode=bootstrap]\n"
                f"Strategies: {', '.join(sorted(B.STRATEGIES))}")
    symbol, strategy = parts[0].upper(), parts[1]
    if strategy not in B.STRATEGIES:
        return f"Unknown strategy '{strategy}'. Try: {', '.join(sorted(B.STRATEGIES))}"

    kw = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
    mode = kw.get("mode", "shuffle")
    try:
        sims = int(kw.get("sims", 1000))
    except ValueError:
        return "sims must be a number"

    try:
        res = B.run(symbol, strategy, days=int(kw.get("days", 730)))
    except Exception as e:                        # noqa: BLE001
        return f"Backtest failed: {e}"
    try:
        out = MC.run(res["trades"], sims=sims, mode=mode)
    except MC.MonteCarloError as e:
        return f"Can't simulate: {e}"
    return f"{symbol} · {strategy}\n\n" + MC.format_report(out)


def cmd_approve(arg: str) -> str:
    """`/approve TOKEN pin=XXXX` — place a proposed order.

    A WRITE command, and the only one that spends money, so the PIN check in
    `dispatch` is doing real work here. The token itself is single-use, which
    is what stops this message being replayed off the topic later.
    """
    from . import semiauto

    token = (arg or "").split()[0] if arg.strip() else ""
    if not token:
        rows = semiauto.pending()
        if not rows:
            return "Nothing awaiting approval."
        return "Awaiting approval:\n" + "\n".join(
            f"  {r['symbol']} {r['qty']} @ {r['ref_price']:.2f} "
            f"stop {r['stop']:.2f}\n    /approve {r['token']} pin=XXXX"
            for r in rows)
    try:
        out = semiauto.approve(token)
    except semiauto.Blocked as e:
        return f"Refused at execution:\n{e}"
    except semiauto.SemiAutoError as e:
        return f"Not placed: {e}"
    if not out["stop_attached"]:
        return (f"⚠ Order {out['order_id']} placed but THE STOP DID NOT GO ON."
                f"\n{out['warning']}\nSet it manually now.")
    return (f"Placed {out['order_id']} · stop attached (GTT {out['gtt_id']}) "
            f"· journal #{out['trade_id']}")


def cmd_reject(arg: str) -> str:
    from . import semiauto

    token = (arg or "").split()[0] if arg.strip() else ""
    if not token:
        return "Usage: /reject TOKEN"
    try:
        semiauto.reject(token)
    except semiauto.SemiAutoError as e:
        return str(e)
    return "Rejected."


def cmd_query(parts: list[str]) -> str:
    """`/query rsi(14) > 70 and adx(14) > 25`

    Takes the whole line, because a query has spaces in it and splitting on
    the first one would truncate every useful expression.
    """
    from . import query as Q, watchlist as W

    text = " ".join(parts).strip()
    if not text:
        lines = ["Usage: /query EXPRESSION", ""]
        for q, means in Q.EXAMPLES[:5]:
            lines.append(f"  {q}\n    {means}")
        return "\n".join(lines)

    syms = [r["tradingsymbol"] for r in W.items()]
    if not syms:
        return "Watchlist is empty."
    try:
        res = Q.run(text, syms, explain_misses=2)
    except Q.QueryError as e:
        return f"Bad query: {e}"
    return Q.summary(res, top=15)


def cmd_health(arg: str) -> str:
    """Are the services actually running?

    The one question no other command can answer, because every other command
    is answered BY one of the services — if the remote listener is dead you
    get silence, and silence is what a working system with nothing to say
    looks like too.
    """
    from . import supervisor
    return supervisor.format_health(supervisor.read_state())


def cmd_paper(tokens: list[str]) -> str:
    """/paper [fill|queue|orders] — the paper broker, from the office.

    `fill` is the one that matters on a weekday morning: paper orders fill at
    the NEXT open, so the queue sitting there from last night's close needs a
    bar that only exists after 09:15. That is exactly when you're not at the
    machine.

    It reads the stored candles rather than fetching, so it tops up first —
    otherwise the bar it needs isn't there yet and every order politely stays
    pending, which looks identical to nothing having happened.
    """
    from . import paper as P

    sub = (tokens[0].lower() if tokens else "status")
    kv, _rest = _kv(tokens[1:] if tokens else [])

    if sub in ("status", "st"):
        return P.summary()

    if sub in ("orders", "log"):
        rows = P.orders(15)
        if not rows:
            return "No paper orders yet."
        out = []
        for o in rows:
            line = (f"#{o['id']} {o['status']} {o['kind']} {o['symbol']} "
                    f"sig {o['signal_time'][:16]}")
            if o["fill_price"]:
                line += f" @ {o['fill_price']}"
            out.append(line)
        return "\n".join(out)

    if sub == "queue":
        P.start()
        stop_pct = float(kv["sl"]) if kv.get("sl") else None
        out = P.from_signals(stop_pct=stop_pct)
        lines = [f"{len(out['queued'])} queued from {out['flips']} flip(s)"]
        lines += [f"  {q['kind']} {q['symbol']} {q['tag']}"
                  for q in out["queued"]]
        lines += [f"  \u2014 {sk}" for sk in out["skipped"][:5]]
        lines.append("Fills at the NEXT open \u2014 send /paper fill "
                     "after 09:20.")
        return "\n".join(lines)

    if sub == "fill":
        topped, why = _topup_today()
        out = P.fill_pending()
        lines = []
        for f in out["filled"]:
            r = f.get("r_multiple")
            lines.append(f"{f['kind']} {f['symbol']} {f['qty']} @ {f['price']}"
                         + (f"  {r:+.2f}R" if r is not None else ""))
        if out["still_pending"]:
            # Naming the reason matters: "waiting" before the open is normal,
            # and "waiting" at 10:00 means the top-up didn't land.
            lines.append(f"{out['still_pending']} still waiting for a next "
                         f"open" + ("" if topped else f" ({why})"))
        for prob in out["problems"][:5]:
            lines.append(f"! {prob}")
        if not lines:
            return "Nothing pending."
        return "\n".join(lines)

    return ("Usage: /paper [status|fill|queue|orders]\n"
            "  /paper queue sl=4   \u2014 queue tonight's flips, 4% stop\n"
            "  /paper fill         \u2014 fill at this morning's open")


def _topup_today() -> tuple[bool, str]:
    """Pull today's bars in before filling. -> (worked, why not).

    paper.fill_pending reads the stored candles and never fetches, so without
    this the morning fill finds no bar and reports everything as pending —
    which reads exactly like a quiet morning with no signals.
    """
    try:
        from . import maintenance, watchlist
        kite = _kite_or_none()
        if not kite:
            return False, "no Kite session \u2014 log in first"
        syms = [w["tradingsymbol"] for w in watchlist.items()]
        maintenance.topup_candles(kite, syms)
        return True, ""
    except Exception as e:                           # noqa: BLE001
        return False, f"top-up failed: {type(e).__name__}"


def cmd_sips(arg: str) -> str:
    """/sips — instructed SIPs, soonest debit first."""
    from . import mf
    return mf.sips_text(mf.sips())


def cmd_mf(tokens: list[str]) -> str:
    """/mf [overlap A | B] [schemes] [lookthrough] — what the funds hold."""
    from . import mf

    sub = (tokens[0].lower() if tokens else "schemes")
    rest = " ".join(tokens[1:])

    if sub in ("schemes", "imported", "list"):
        rows = mf.schemes()
        if not rows:
            return ("No fund portfolios imported. Kite has no composition "
                    "endpoint, so these come from the AMC's monthly "
                    "disclosure: `tf mf import FILE --scheme NAME`.")
        return "\n".join(f"{r['scheme']} ({r['as_of']}) — {r['n']} holdings, "
                          f"{r['matched']} matched to NSE" for r in rows)

    if sub == "overlap":
        if "|" not in rest:
            return "Usage: /mf overlap Fund A | Fund B"
        a, b = (x.strip() for x in rest.split("|", 1))
        return mf.overlap_text(mf.overlap(a, b))

    if sub in ("lookthrough", "look", "exposure"):
        out = mf.look_through()
        if not out["exposure"]:
            return "Nothing to look through yet."
        lines = [f"True exposure \u2014 \u20b9{out['total']:,.0f}"]
        for e in out["exposure"][:12]:
            via = f" (+{e['via_funds']:,.0f} via funds)" if e["via_funds"] else ""
            lines.append(f"  {e['symbol']:<12} {e['pct']:>5.1f}%  "
                         f"\u20b9{e['total']:,.0f}{via}")
        if out.get("note"):
            lines.append(f"  ({out['note']})")
        return "\n".join(lines)

    return "Usage: /mf [schemes|overlap A | B|lookthrough]"


def cmd_compare(tag: str) -> str:
    """/compare TAG — live paper trades against the same strategy backtested.

    The one question the paper account exists to answer: not whether it made
    money, but whether the edge the backtest promised survived contact with a
    real sequence of days.
    """
    if not tag:
        return ("Usage: /compare TAG   (a strategy key, e.g. rsi_revert)\n"
                "Compares your paper trades with the same strategy "
                "backtested on the same symbols.")
    try:
        return journal.compare_text(journal.compare(tag.strip()))
    except Exception as e:                           # noqa: BLE001
        return f"Compare failed: {e}"


HANDLERS = {
    "valuation": cmd_valuation, "val": cmd_valuation,
    "fundamentals": cmd_fundamentals, "fund": cmd_fundamentals,
    "news": cmd_news,
    "results": cmd_results, "result": cmd_results,
    "dividends": cmd_dividends, "dividend": cmd_dividends, "div": cmd_dividends,
    "price": cmd_price, "quote": cmd_price,
    "levels": cmd_levels, "lvl": cmd_levels,
    "board": cmd_board, "ob": cmd_ob,
    "deliv": cmd_delivery, "delivery": cmd_delivery,
    "insider": cmd_insider, "insiders": cmd_insider,
    "chart": cmd_chart,
    "ind": cmd_indicators, "indicators": cmd_indicators, "ta": cmd_indicators,
    "conf": cmd_confluence, "confluence": cmd_confluence, "mtf": cmd_confluence,
    "exit": cmd_exit, "trail": cmd_exit, "stop": cmd_exit,
    "alerts": cmd_alerts,
    "signals": cmd_signals, "sig": cmd_signals,
    "unalert": cmd_unalert, "delalert": cmd_unalert,
    "brief": lambda a: cmd_brief(a) if a else cmd_premarket(""),
    "b": lambda a: cmd_brief(a) if a else cmd_premarket(""),
    "premarket": cmd_premarket, "morning": cmd_premarket,
    "journal": cmd_journal, "j": cmd_journal, "trades": cmd_journal,
    "portfolio": cmd_portfolio, "pf": cmd_portfolio,
    "sips": cmd_sips, "sip": cmd_sips,
    "compare": cmd_compare,
    "holdings": cmd_portfolio,
    "health": cmd_health, "status": cmd_health, "up": cmd_health,
    "montecarlo": cmd_montecarlo, "mc": cmd_montecarlo,
    "approve": cmd_approve, "reject": cmd_reject,
}

# commands that need the whole token list, not just the first arg
MULTI_ARG = {"alert": cmd_alert, "size": cmd_size, "log": cmd_log,
             "mf": cmd_mf,
             "paper": cmd_paper,
             "query": cmd_query, "q": cmd_query, "find": cmd_query,
             "close": cmd_close, "amend": cmd_amend, "batch": cmd_batch,
             "heatmap": cmd_heatmap, "sweep": cmd_heatmap,
             "scan": cmd_scan, "scanner": cmd_scan,
             "wf": cmd_walkforward, "walkforward": cmd_walkforward,
             "candles": cmd_candles, "bars": cmd_candles}

# Commands that CHANGE STATE rather than just report it. Listed explicitly so
# the read/write split is visible in code — this set is what NTFY_CMD_PIN
# guards, and what needs re-examining before anything order-placing is added.
# /size is NOT here: it computes and returns a number, it writes nothing.
WRITE_CMDS = {"alert", "unalert", "delalert", "log", "close", "amend",
              "approve", "reject"}

# Commands where only SOME subcommands write. /paper on its own reports; its
# fill and queue create orders and journal rows. Guarding the whole command
# would put a PIN in front of a status check you want to be able to glance at
# one-handed; guarding none of it would leave the writes open.
WRITE_SUBCMDS = {("paper", "fill"), ("paper", "queue")}


def _check_pin(cmd: str, parts: list[str]) -> tuple[bool, str | None, list[str]]:
    """Strip a `pin=XXXX` token and decide whether a write may proceed.

    -> (allowed, refusal_message, remaining_parts). Read commands always pass
    and keep their arguments untouched.
    """
    pin_required = config.get("NTFY_CMD_PIN", "")
    supplied = None
    rest = []
    for p in parts:
        if p.lower().startswith("pin="):
            supplied = p.split("=", 1)[1]
        else:
            rest.append(p)
    writes = cmd in WRITE_CMDS or (
        bool(rest) and (cmd, rest[0].lower()) in WRITE_SUBCMDS)
    if not writes or not pin_required:
        return True, None, rest
    if supplied != pin_required:
        return False, (f"/{cmd} needs a PIN. Send it as `pin=XXXX` anywhere on "
                       f"the line."), rest
    return True, None, rest


def handle(text: str) -> str | None:
    """Parse and dispatch one command line. Returns the reply, or None to
    ignore (non-command chatter)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if cmd in ("help", "commands", "?"):
        return HELP

    ok, refusal, rest = _check_pin(cmd, parts[1:])
    if not ok:
        print(f"  refused /{cmd}: bad or missing PIN")
        return refusal
    arg = rest[0] if rest else ""

    if cmd in MULTI_ARG:                      # needs the full token list
        try:
            return MULTI_ARG[cmd](rest)
        except Exception as e:
            traceback.print_exc()
            return f"/{cmd} failed: {e}"
    fn = HANDLERS.get(cmd)
    if not fn:
        return f"Unknown command /{cmd}. Send /help for the list."
    try:
        return fn(arg)
    except Exception as e:
        traceback.print_exc()
        return f"/{cmd} failed: {e}"


# ---------------------------------------------------------------- listener
def _cmd_topic() -> str:
    topic = config.get("NTFY_CMD_TOPIC", "") if hasattr(config, "get") \
        else __import__("os").getenv("NTFY_CMD_TOPIC", "")
    if not topic:
        raise SystemExit(
            "NTFY_CMD_TOPIC not set. Pick an unguessable topic, subscribe to it "
            "in the ntfy app, and add it to .env (separate from NTFY_TOPIC).")
    return topic


def _redact(text: str) -> str:
    """Keep the PIN out of the terminal log — it's weak enough already."""
    return " ".join("pin=***" if p.lower().startswith("pin=") else p
                    for p in text.split())


def _protection_note() -> str:
    """What is actually standing between a stranger and your armed alerts."""
    if config.get("NTFY_TOKEN", ""):
        return "ntfy auth token set — the channel is authenticated."
    if config.get("NTFY_CMD_PIN", ""):
        return ("no ntfy auth; write commands (/alert, /unalert) need a PIN. "
                "The topic name still guards everything else.")
    return ("no ntfy auth, no PIN — the topic name is the only credential, and "
            "anyone holding it can arm and disarm your alerts. Set NTFY_TOKEN, "
            "or NTFY_CMD_PIN at minimum.")


def listen():
    """Subscribe to the command topic's JSON stream and reply to each command.
    Auto-reconnects; a stale command missed during a reconnect is harmless."""
    topic = _cmd_topic()
    url = f"{config.NTFY_SERVER}/{topic}/json"
    headers = notify._auth_headers()
    print(f"Listening for phone commands on {config.NTFY_SERVER}/{topic} … "
          f"(replies go to {config.NTFY_TOPIC}). Ctrl+C to stop.")
    print(f"  security: {_protection_note()}")
    notify.send("Remote control online. Send /help.",
                title="TickerForge", priority="low", tags="satellite")
    backoff = 2
    while True:
        try:
            with requests.get(url, stream=True, timeout=(10, 65),
                              headers=headers) as r:
                r.raise_for_status()
                backoff = 2
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("event") != "message":
                        continue      # skip open/keepalive frames
                    text = obj.get("message", "")
                    reply = handle(text)
                    if reply is None:
                        continue
                    # log writes distinctly: if one shows up that you didn't
                    # send, that's the signal the topic has leaked
                    verb = text.lstrip("/").split(" ")[0].lower()
                    kind = "WRITE" if verb in WRITE_CMDS else "read"
                    print(f"  {kind} cmd: {_redact(text)!r} -> {len(reply)} chars")
                    notify.send(reply, title="TickerForge ↩",
                                priority="default", tags="robot")
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as e:
            # SystemExit too: notify.send -> config.require raises it, and a
            # missing NTFY_TOPIC must not silently kill two-way phone control.
            print(f"stream dropped ({e}); reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)