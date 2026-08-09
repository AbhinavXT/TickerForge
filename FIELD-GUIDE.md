# TickerForge — Field Guide

**What this document is.** `GUIDE.md` is the reference: every command, every flag, every config key. This is the other half — what each screen *shows*, where those numbers come from, what they actually mean, and what to do with them. When the two disagree, `GUIDE.md` wins on syntax and this wins on interpretation.

**Who it's for.** You in six months, having forgotten why the scanner ranks the way it does and whether a 0.4R expectancy is good.

---

## Table of contents

- [0. How the system is shaped](#0-how-the-system-is-shaped)
- [1. Chart — the main page](#1-chart--the-main-page)
- [2. Desk — the intraday page](#2-desk--the-intraday-page)
- [3. Scanner](#3-scanner)
- [4. Backtester](#4-backtester)
- [5. Journal](#5-journal)
- [6. Portfolio](#6-portfolio)
- [7. Valuation](#7-valuation)
- [8. Fundamentals](#8-fundamentals)
- [9. Results](#9-results)
- [10. Actions](#10-actions)
- [11. News](#11-news)
- [12. Metrics](#12-metrics)
- [13. The phone channel](#13-the-phone-channel)
- [14. Risk sizing](#14-risk-sizing)
- [15. Routines that tie it together](#15-routines-that-tie-it-together)
- [16. What to distrust](#16-what-to-distrust)

---

## 0. How the system is shaped

Four things run, and they are deliberately separate processes:

| Process | Job | If it dies |
|---|---|---|
| `serve` | The web UI and its API | Pages stop loading. Obvious. |
| `watch` | Intraday alert evaluation, ~5s poll | **Silent.** Alerts simply never fire. |
| `events` | Corporate actions, news, scheduled pushes | Silent. No brief, no filings. |
| `remote` | Listens for phone commands | Commands are ignored. |

The dangerous ones are the silent failures. Nothing tells you an alert didn't fire; you just don't get a message, which is indistinguishable from the condition not being met. This is why the roadmap's supervisor item exists and why the token-expiry heartbeat was built early.

**Two stores, on purpose.**

- **SQLite** (`data/tickerforge.db`) — watchlist, alerts, journal, fundamentals, events, saved scans, layouts, meta settings. Anything small, relational, and written often. Runs in WAL mode so the four processes can share it.
- **DuckDB** (`data/candles.duckdb`) — OHLCV candles only. Columnar, fast over long histories, and the thing a backtest reads a hundred thousand rows from.

**Where data comes from.** Kite Connect for candles, quotes, holdings and positions. NSE's public feeds for corporate events. screener.in for fundamentals and the market-wide results screen. Google News RSS for headlines. Everything is cached locally; the app is offline-tolerant except for live quotes.

**One concept underpins nearly every screen: the closed bar.** An indicator computed on a bar that is still forming will change before the day ends, so a signal derived from it isn't real yet. Signals, the scanner and the backtester all work on *closed* bars only. The chart shows the forming bar because you want to see the price, but it is excluded from anything that generates a decision. Anywhere you see a number that seems one bar behind, this is why, and it's correct.

---

## 1. Chart — the main page

Route `/`. This is the daily driver: candles, indicators, event markers, and the place alerts get armed.

### Timeframes

The timeframe menu offers all eight intervals Kite serves — 1, 3, 5, 10, 15 and 30 minutes, 1 hour, 1 day — plus **1 week and 1 month**, which are built from daily bars in the browser rather than requested. Kite has no weekly endpoint, so the alternative was not offering them.

There is nothing below a minute because Kite has no sub-minute historical data. An option that produced an empty chart would be worse than its absence.

Candles are fetched once, stored in DuckDB, and topped up incrementally — so revisiting a symbol is instant and only the new bars cost an API call. Weekly and monthly cost nothing extra at all, since they're a transform of data you already have.

A resampled bar takes the **first** open, the highest high, the lowest low, the **last** close, and the summed volume — and every high and low is a price that actually traded, never a computed one.

### Chart types

Ten, in three groups.

**Candles · Hollow candles · Heikin Ashi.** Hollow draws up-bars as outlines and down-bars solid, which makes runs of one direction easier to see at a glance.

**Heikin Ashi is worth a warning.** Each bar averages itself with the one before, which strips out a lot of single-bar noise and makes trends much easier to read. The cost is that the OHLC no longer refers to prices anything traded at — a Heikin Ashi close is not a close. Never read a level, set a stop, or place an alert off one.

**Bars · HLC bars.** Traditional OHLC bars; HLC drops the opening tick.

**Line · Line with markers · Step line · Area · Baseline.** All close-only. Baseline anchors its shading at the first visible close, so the colour reads as "up or down since the start of what you're looking at".

Renko, Kagi and point & figure aren't offered. They discard time entirely — a Renko brick forms on movement, not on the clock — so every other tool on the page, from drawings to event markers to the alert engine, would be plotting against a different x-axis.

### Kite intervals

The bar for the session in progress is provisional. It's drawn, but it's dropped from any calculation that feeds a decision, and it's re-read from Kite rather than trusted from cache.

### Indicators

Eighteen, split into price overlays (drawn on the candles) and oscillators (drawn in their own pane below). Every one is configurable via the gear icon: **Inputs** for periods and multipliers, **Style** for colours and line width. Settings persist per instance, so you can have EMA 21 and EMA 200 on the same chart in different colours.

#### Overlays

| Indicator | Default | What it computes | How to read it |
|---|---|---|---|
| **Simple MA** | 20 | Arithmetic mean of the last N closes | The plainest trend line. Price above = uptrend, crudely. Slow to react, which is the point. |
| **Exponential MA** | 21 | Weighted mean, recent bars weighted more | Reacts faster than SMA to a turn, at the cost of more whipsaws. The 21 is a common swing-trading default. |
| **Weighted MA** | 20 | Linearly weighted mean | Between SMA and EMA in responsiveness. Rarely worth the third line unless you specifically want it. |
| **Bollinger Bands** | 20, 2σ | SMA ± 2 standard deviations of close | Bands measure *volatility*, not direction. Narrow bands (a "squeeze") mean a quiet market, which historically precedes a move — direction unknown. Price touching a band is not a signal; in a strong trend price rides the band for weeks. |
| **Donchian Channels** | 20 | Highest high and lowest low of the last N bars | The original turtle breakout system. A close above the upper channel is a 20-bar high. Clean, mechanical, no parameters to fudge. |
| **VWAP** | — | Volume-weighted average price for the session | Intraday only. The institutional reference price: above it, buyers are in control of the day. Resets each session. |
| **Supertrend** | 10, 3× | ATR bands that ratchet; the line flips sides when price closes through it | The single line switches colour — green below price (uptrend), red above (downtrend). The vertical jump is the flip. A trailing stop and a trend filter in one. Excellent in trends, brutal in ranges. |
| **Parabolic SAR** | 0.02, 0.2 | Dots that accelerate toward price | Same job as Supertrend, tighter and noisier. The acceleration factor means it catches up faster the longer a trend runs. |
| **52-week High/Low** | — | Horizontal lines at the yearly extremes | Context, not signal. Distance from the 52-week high is one of the more durable momentum factors. |

#### Oscillators

| Indicator | Default | What it computes | How to read it |
|---|---|---|---|
| **RSI** | 14 | Ratio of average gains to average losses, Wilder-smoothed, scaled 0–100 | >70 "overbought", <30 "oversold" — but in a strong trend RSI parks above 70 for weeks. More useful: RSI failing to make a new high while price does (divergence), and the 50 line as a trend proxy. |
| **MACD** | 12/26/9 | Fast EMA − slow EMA, plus a signal EMA and a histogram of the gap | Two events matter: the MACD crossing its signal line (momentum shift) and crossing zero (trend change). The histogram shrinking is the earliest warning. |
| **Stochastic** | 14/3/3 | Where the close sits within the recent high-low range | Faster and noisier than RSI. Best in ranges. The %K/%D cross in the extreme zones is the classic setup. |
| **ATR** | 14 | Average true range, Wilder-smoothed | Not directional at all — it's the volatility unit. This is what position sizing and chandelier stops are denominated in. A 2× ATR stop on a stock with ATR 8 is a ₹16 stop, whatever the price. |
| **ADX / DMI** | 14 | Trend strength (ADX) plus directional components (+DI, −DI) | ADX says *how much* trend, never which way. Below 20 = no trend, don't run breakout systems. Above 25 = trending. +DI over −DI = up. |
| **CCI** | 20 | Deviation from a moving average, scaled by mean deviation | ±100 are the conventional bands. Used here mostly as a mean-reversion trigger. |
| **Williams %R** | 14 | Inverted stochastic, −100 to 0 | Same information as Stochastic, different scale. Pick one. |
| **Money Flow Index** | 14 | RSI weighted by volume | "RSI that knows about volume." Divergences carry more weight than plain RSI's because they imply money actually leaving. |
| **OBV** | — | Running total of volume, signed by the day's direction | The *level* is meaningless; the *slope* is the point. OBV making new highs while price consolidates suggests accumulation. |

### Event markers

Dots on the price series where something *happened*. This is a deliberate distinction: markers show events, not states. A "Supertrend flip" marker appears on the bar the flip occurred, not on every bar the trend continues. Marking states would paint half the chart and tell you nothing.

Available: Supertrend flip · RSI crossing 70/30 · MACD signal cross · golden/death cross (50/200) · Bollinger band break · volume ≥ 2× average.

All are computed from closed bars already on the chart, so they agree exactly with what the alert engine and the backtester see. If a marker is on the chart, the alert would have fired.

### Alerts from the chart

Click the price axis (or right-click) → "Alert at ₹X". Armed alerts render as horizontal lines you can drag; dragging re-arms at the new level. This connects the two things you use most without typing a command.

### Drawing tools

Toolbar beside the indicator chips: trendline, ray, horizontal line, rectangle, Fibonacci retracement, and a clear button.

Draw by picking a tool and dragging. Click a drawing with the cursor tool to select it; drag its body to move it, drag a handle to reshape it, `Delete` to remove it, `Esc` to drop back to the cursor.

**Everything is anchored in data, never pixels.** A drawing is stored as timestamps and prices, and converted to screen coordinates at paint time. Stored in pixels it would be correct at exactly one zoom level and quietly wrong everywhere else — pointing at a different bar the moment you scrolled. This is also why a trendline survives you resizing the window, switching to a laptop, or reopening the chart a month later.

Drawings are **per symbol and per interval**. A trendline across daily bars connects levels that don't exist on a 5-minute chart, so it isn't shown there. They live in SQLite rather than browser storage, so they survive a different browser or a cleared cache.

The Fib ladder is anchored 0 at your first click and 1 at your second, which means dragging high→low and low→high give mirrored ladders. That's deliberate: the direction you drew in encodes which way you think the move went. The levels are the conventional seven, including 0.5 — not a Fibonacci ratio at all, but in every charting package because price respects halves.

One interaction detail worth knowing: the overlay is transparent to the mouse unless a tool is armed. With the cursor tool selected, panning, zooming and the crosshair behave exactly as they did before.

### Compare mode

Type a symbol into the compare box and both series are **indexed to 100** at the first visible bar. This is the only way the overlay means anything: raw prices can't be compared, because a ₹3,000 stock and a ₹40 stock on one axis tell you which is expensive per share and nothing else. Indexed, two lines with identical shape land exactly on top of each other whatever they cost.

The usual comparison is against Nifty — it turns "this went up 8%" into "this went up 8% while the market went up 11%", which is a different and more useful sentence.

### Bar replay

Press `r`, or the ⏯ button. The chart rewinds to about 40% of the loaded history and steps forward one bar at a time — space to play/pause, arrows to step, or drag the slider.

The point is to see a setup unfold **without knowing what came next**, which is the only honest way to judge whether you'd actually have taken it. Reading a chart backwards, every breakout looks obvious; the ones that failed have already been edited out of your attention.

Replay operates on data already loaded, so it costs nothing and works offline. Leaving the symbol exits replay automatically.

### Backtest trades on the real chart

A backtest result gets a **Show on chart** button. It pins that trade list server-side and opens the main chart with every entry and exit marked, on your usual indicators and drawings.

Worth doing because the backtest's own mini-chart shows you *that* the trades happened; this shows you *what they looked like* — whether the entries sat at the top of extended moves, whether the stops were inside the noise. Trade statistics can't tell you that.

### Keyboard

| Key | Does |
|---|---|
| `/` | Focus the search box |
| `↑` `↓` | Previous / next watchlist symbol (wraps) |
| `i` | Indicator menu |
| `r` | Toggle bar replay |
| `space` | Play / pause replay |
| `←` `→` | Step replay back / forward |
| `Esc` | Leave the search box, or drop back to the cursor tool |
| `Delete` | Remove the selected drawing |

Watchlist rows also carry a 30-day sparkline and the day's change. The sparkline is scaled to its own range rather than a shared one, so it shows shape rather than level — which is what you're scanning for when your eye runs down the list.

### Layouts

The set of indicators, their parameters and styles can be saved as a named layout and recalled per symbol. Stored in SQLite rather than browser storage, so they survive a different browser or a cleared cache.

---

## 2. Desk — the intraday page

Route `/desk`. Built for the phone in a pocket during market hours: one symbol, the levels that matter, and the alert panel first.

### The levels block

| Level | Calculation | Why it matters |
|---|---|---|
| **LTP / change** | Live quote | — |
| **VWAP** | Session volume-weighted average | The day's fair value. Above it the day belongs to buyers. |
| **PDH / PDL / PDC** | Previous day's high / low / close | Yesterday's extremes are the most-watched levels on any chart. Breaks of them are where stops sit. |
| **Gap %** | `(open − prev close) / prev close` | A gap changes the character of a day. Big gaps often fill; gaps on news often don't. |
| **ORB** | High and low of the first N minutes (default 15) | The opening range. Breaking it is the most common intraday setup in existence. |
| **CPR** | Pivot `P = (H+L+C)/3`, `BC = (H+L)/2`, `TC = 2P − BC` | The Central Pivot Range. A **narrow** CPR implies the market hasn't found value — expect a trending day. A **wide** CPR implies balance — expect chop. This is the single most useful pre-open read on the page. |
| **Pivots** | R1/R2/R3, S1/S2/S3 from the classic floor formulas | Reference levels where reactions cluster. Not magic; self-fulfilling enough to respect. |

### Installing it on your phone

Desk is a PWA. Open `/desk` in the phone's browser and use **Add to Home Screen** — it then launches full-screen with no browser chrome, its own icon, and straight into Desk rather than the chart.

For this to work from the office the server has to be reachable, which means Tailscale (see `PUBLIC_BASE_URL` in `.env`). ntfy covers push; this covers pull.

**What gets cached, and what deliberately doesn't.** The service worker caches the app *shell* — HTML, CSS, icons — so the icon opens instantly instead of showing a white rectangle on a slow connection. It **never** caches anything under `/api`. That rule is the whole design: a cached quote looks exactly like a live one, same layout, same styling, no hint that the number is an hour old — and you'd size a position against it. A page that fails to load tells you something is wrong; a page that loads with a stale price does not.

So when the connection drops you get a red banner saying so, and every reading carries an "updated 3m ago" stamp. Offline means the page says offline.

**On a phone the layout reorders**: the alert panel comes first, because that's what you're actually doing one-handed in a corridor — arming something, or checking what fired. Levels are reference and the chart is a glance, so they move below. The quote header stays stuck to the top while you scroll, since it's the number everything else is measured against. Tap targets go to 44px, and inputs to 16px — under that, iOS zooms the whole page every time a field takes focus.

### The position readout

A one-line human summary: *"above VWAP, inside CPR, above PDH"*. It's assembled by comparing LTP against each level in turn. Read it as a checklist of what's already been decided today — if price is above VWAP, above the CPR and above yesterday's high, the bulls have won every argument so far.

### Confluence

The multi-timeframe agreement score, expressed as a percentage from −100 to +100.

**How it's built.** Each timeframe (default 5m, 15m, 1h, day) gets scored from its indicator bundle. Six components, each voting +1, 0 or −1:

- price vs EMA20
- EMA20 vs EMA50
- Supertrend direction
- RSI (≥55 bullish, ≤45 bearish, neutral between)
- MACD vs its signal
- price vs anchored VWAP

The votes are averaged. Then ADX adjusts *conviction*, not direction: below 20 the score is multiplied by 0.6, above 25 by 1.2. A bullish read in a directionless tape is discounted; the same read in a trending tape is amplified.

Timeframes are then weighted, slower ones more heavily — daily 2.5, hourly 2.0, 15m 1.5, 5m 1.0 — because the slower chart carries more information about where price is actually going. The weighted average is the headline percentage.

**Aligned** means every scored timeframe points the same way (and none is neutral). That's the flag that matters more than the number.

**Verdicts:** strong bullish (aligned and ≥60) · bullish (≥35) · mixed · bearish (≤−35) · strong bearish (aligned and ≤−60).

**How to use it.** As a veto, not a trigger. The module's own docstring puts it well: it should stop trades more often than it starts them. A 5-minute long signal with confluence at −40 is a trade against every larger timeframe. Take the signal when the tape agrees; skip it when it doesn't.

---

## 3. Scanner

Route `/scanner`. Finds candidates you *aren't* already watching.

### Universe vs watchlist

These are different on purpose. The **watchlist** is what you follow. A **universe** is what you look through. Scanning your watchlist would be scanning the answer you already have — the entire value of a scanner is surfacing names that aren't on it.

Universes need candles, and that's the expensive part: 200 symbols × 400 daily bars is 200 rate-limited API calls. The backfill is a resumable overnight job — interrupting it loses nothing, and re-running it is cheap because it only fetches what's missing.

### Conditions

Ten, combinable. A symbol must satisfy *all* selected conditions to appear.

| Condition | Fires when | What it's actually finding |
|---|---|---|
| **New 52-week high** | Close is the highest in 252 bars | The strongest names in the market. Counterintuitively, new highs beget new highs — this is the momentum anomaly in its purest form. |
| **New 52-week low** | Close is the lowest in 252 bars | Either falling knives or the beginning of a base. Rarely actionable alone. |
| **Volume ≥ N× average** | Today's volume vs its own N-bar average | Something happened. Volume is the only unfakeable input; a 3× day means real participation, not drift. |
| **Golden cross** | 50-SMA crosses above 200-SMA | A slow, famous, much-derided trend confirmation. Late by construction. Its value is as a regime filter, not a timing tool. |
| **Death cross** | 50-SMA crosses below 200-SMA | The inverse. Mostly useful for *removing* names from consideration. |
| **Donchian breakout** | Close above the N-bar high | A clean, parameterless breakout. The turtle entry. |
| **Bollinger squeeze firing** | Bandwidth was in its lowest percentile, and price has now broken out | Two-stage: compression, then expansion. The squeeze identifies *when*, the break identifies *which way*. |
| **Trading above its SMA** | Close > N-SMA | A filter, not a signal. Use it to constrain other conditions. |
| **RSI in a band** | RSI between two levels | Finds a specific state — e.g. RSI 40–50 in an uptrend is a pullback, not weakness. |
| **Strategy flipped long** | Any of the 17 backtester strategies just turned long | Runs the actual strategy logic across the universe. The bridge between the backtester and discovery. |

### Reading the results

Columns: **Symbol · Close · Day% · Volume · Why**.

The **Why** column is the important one — it names which conditions fired and with what values, so a hit is auditable rather than a black box. Rows are ranked by the strength of the triggering condition (how far past the threshold), not alphabetically.

Saved scans persist, and top hits get appended to the EOD push.

### Relative strength

RS strips the market out of a stock's return. A name up 18% in a year the Nifty rose 22% is a laggard wearing a gain, and absolute return can't tell you that.

Measured over **3, 6 and 12 months** and blended, weighted toward the recent. One window alone is a coin flip on your choice of window — a stock can top a 3-month list on one earnings pop and a 12-month list on something that finished six months ago. Agreement across horizons is the actual signal.

**The 12-month figure deliberately skips the most recent month.** Short-horizon reversal is one of the most reliable effects in equities, and the classic momentum factor is defined as 12-minus-1 precisely because the last month contaminates a year of trend with a few weeks of mean reversion. The 3-month window keeps its recent bars, so recency is still represented — it just isn't allowed to poison the long horizon too.

The blended number is meaningless alone; what you use is the **percentile** across the universe. RS 85 means stronger than 85% of the Nifty 200. That's the form the classic pairing needs: a breakout scan filtered to RS > 70.

Names with no history rank as blank, not zero — no history isn't the same fact as terrible performance, and zero would sort a new listing below every genuine laggard.

### How to use it

Pair conditions. A single condition is a list; two are a thesis. The classic combinations:

- Donchian breakout **+** volume ≥ 2× — a breakout somebody actually participated in.
- 52-week high **+** above 200-SMA — momentum in an established uptrend.
- Bollinger squeeze **+** ADX rising — compression resolving into a real trend.

And check the regime first. Long breakout scans in a risk-off tape are exactly where breakout systems bleed.

---

## 3b. Query — conditions you write yourself

Route `/query`, or `tf query "…"`, or `/query` from the phone.

The scanner answers ten fixed questions well. This answers the rest:

```
rsi(14) > 70 and adx(14) > 25
ema(20) crosses_above ema(50)
close > highest(252) and volume > 2 * avg_volume(20)
rs > 70 and mos_pct > 20
close > sma(200) and rsi(2) < 10
```

### What the words mean

Comparisons read the **last closed bar**. `crosses_above` and `crosses_below` read the last two — so they mean "crossed on the most recent bar", not "has ever crossed". The second reading would be useless: every pair of series has crossed at some point.

`highest(n)` and `lowest(n)` **exclude the current bar**, matching the Donchian convention in the backtester. Include it and `close > highest(252)` becomes unsatisfiable — a close can't exceed a window it's part of — and the query would return nothing for ever without ever erroring.

A symbol without enough history to evaluate a clause counts as **not matching**, never as probably matching. A screen that guesses hands you a list you can't trust.

### Explain

The most useful button on the page is "explain misses". An empty result has many causes — the threshold is wrong, the data is missing, you meant `>` and typed `<` — and they're indistinguishable from a blank table. Explain shows each clause with the values it actually saw:

```
✗ rsi(14) > 70 — 42.47 vs 70.00
✓ close > sma(200) — 114.89 vs 108.60
```

That turns "no results" into a specific number you can argue with.

### Timeframes

Wrap any expression in `weekly(...)` or `monthly(...)`:

```
weekly(close) > weekly(sma(20))
weekly(rsi(14)) > 55 and rsi(14) < 40
```

That second one is the pattern worth knowing: **weekly trend intact, daily pulled back.** It's most of what multi-timeframe analysis is for, and it can't be written at all without this.

Each side of a comparison reads its own most recent bar, so `weekly(close) > sma(200)` compares the latest weekly close against the latest daily 200-DMA — what you'd get reading two charts side by side. Crosses inside a wrapper use that timeframe's bars, so `weekly(ema(5)) crosses_above weekly(ema(20))` means a weekly cross, which is a much rarer and slower event than the daily one.

Resampling matches the chart's exactly, including that the final week is a partial one. So `weekly(sma(20))` moves during the week — as it does in every charting package, and as you'd see if you looked.

### Fundamentals

Twenty per-company fields, from statements you've already fetched:

| Group | Fields |
|---|---|
| Profitability | `roe` `roce` `roce_screener` `opm` |
| Balance sheet | `debt_equity` `interest_cover` |
| Growth | `sales_growth3` `sales_growth5` `profit_growth3` `profit_growth5` |
| Multiples | `pe` `pb` `book_value` `div_yield` `earnings_yield` `market_cap` |
| Yours | `rs` `mos_pct` `fair_value` `n_models` |

```
roe > 18 and debt_equity < 0.5
sales_growth3 > 15 and profit_growth3 > 15
roe > 20 and pe < 25 and rs > 60
```

`roce_screener` sits beside your own `roce` on purpose — everyone defines "capital employed" differently, and the gap between the two is informative rather than an error.

**A company whose figure isn't stored never matches.** `debt_equity < 0.5` won't surface a business whose debt you don't know. Treating absent as zero would make the screen systematically favour the companies with the worst disclosure, which is the opposite of what you want.

These are only loaded when a query mentions one, so a pure price query pays nothing for them.

### History

These look backwards from the bar being evaluated, which is only possible because evaluation carries a bar index rather than assuming the last one:

| | |
|---|---|
| `within(n, cond)` | held on any of the last n bars |
| `bars_since(cond)` | how long since it last held |
| `streak(cond)` | consecutive bars it has held |
| `ago(expr, n)` | the value n bars back |
| `max/min/avg/sum(expr, n)` | over the last n bars |

```
within(5, rsi(14) crosses_above 70)
bars_since(close > sma(200)) > 60
streak(close > open) > 4
volume > max(volume, 60)
```

`bars_since` on something that never happened is **blank, not a large number** — `bars_since(x) > 100` must not be satisfied by a stock where x has never once been true.

### Backtesting a query

Any query can be run as a strategy: long while it holds, flat while it doesn't.

```bash
tf query "close > sma(50) and rsi(14) > 45" --backtest BEL
```

It goes through **the same simulator** as the built-in strategies — same costs, same intrabar fill ordering, same metrics — so the numbers sit beside a strategy's without a footnote. That turns "here are six stocks" into "here is what this idea did over three years", which is a different order of usefulness.

**Fundamentals are refused here.** `roe` and `mos_pct` are today's figures; applying them to a bar from two years ago asserts you knew this year's balance sheet then. That's the most flattering lookahead available, so the query is rejected rather than silently backdated.

A 200-bar warm-up is skipped by default, because indicators are still blank there and a blank indicator reads as "flat" rather than "unknown".

### Watching a query

Saved queries can push when a symbol **newly** enters the result set.

```bash
tf query "close > highest(252) and rs > 70" --save breakouts --alert
tf query --check-alerts
```

The diff is the whole feature. A query that pushed its entire result every evening would be ignored within a week; three names that just qualified is a message worth opening. Departures are recorded but not pushed — a stock dropping out of a screen is rarely something to act on, and every extra notification costs a little of the attention the useful ones need.

Nothing is pushed on the first run, when `before` is empty and everything would look new.

### Screening the whole market

The watchlist is what you follow; a universe is what you search. To hunt across everything:

```bash
tf universe from-market market
tf universe backfill market --days 500     # overnight
tf universe prune market
```

`from-market` excludes rights issues, the T2T and SME series, ETFs and index funds — real instruments, but not companies, and each one costs a rate-limited request.

`prune` runs **after** the backfill, because liquidity is a fact about prices and the instrument index has none. It drops anything under ₹20, under 50,000 average daily shares, or with less than 200 bars. Skip it and the screen surfaces micro-caps trading four thousand shares a day: technically hits, and unactionable — the spread eats the edge and your own order moves the price.

**Fundamental screening is two-stage, and it has to be.** Loading statements for two thousand companies means two thousand requests to somebody else's website. So a query like `close > sma(200) and roe > 18` runs the price half over everything from stored candles, then fetches fundamentals only for the eighty that survived. `and` is commutative, so the answer is identical — the cost isn't.

That only works when the fundamentals sit in a top-level `and`. Put one under an `or` and every symbol needs the figure, because the other branch might be false. The engine detects this and says whether it staged.

The result reports where the funnel narrowed: `500 scanned, 84 passed the price filter, 12 then passed on fundamentals`. A price filter leaving three candidates is a different problem from a valuation rejecting nine hundred, and a single hit count can't tell you which you have.

### It doesn't run your text

Queries arrive from an HTTP endpoint and from phone messages, so the engine parses to a syntax tree and walks it. There's no `eval` anywhere — the worst a hostile query can do is fail to parse. Field names and argument counts are checked at parse time too, so a typo is caught as you type rather than on the fortieth symbol, and it suggests the nearest real field.

## 3a. Screen — value with momentum

Route `/screen`. Three questions usually asked by three different tools, answered at once:

| Axis | Source | Why it fails alone |
|---|---|---|
| **Is it cheap?** | your Damodaran composite | Cheap alone is a value trap — the market is often right that a business is worth less |
| **Is it winning?** | RS percentile vs Nifty | Momentum alone buys whatever went up, including the part that went up because it was overvalued |
| **Is it working?** | your strategies' current stance | A signal alone is a timing tool with nothing to say about what it's timing |

The intersection is small on purpose. **A screen returning forty names hasn't screened.** Three is a good result.

Two things the page does that a plain filter wouldn't. It shows **near misses** — names failing exactly one axis — because a stock that misses by a whisker deserves more attention than the twentieth pass, and an invisible rejection teaches you nothing. And it distinguishes *"nothing qualified"* from *"an input is missing"*: an empty table from an un-backfilled universe reads identically to a genuinely empty market unless the tool says which, and those demand opposite responses.

Nothing here is newly computed. It's a join over work the other pages already did, which is why it stays cheap to keep current.

## 4. Backtester

Route `/backtest`. Seventeen strategies, an optional risk overlay, and — more importantly — three tools for asking whether a result is real.

### The strategies

Every one produces a position series of 1 (long) or 0 (flat). Long-only.

**Trend following**

| Strategy | Defaults | Logic |
|---|---|---|
| `sma_cross` | 20/50 | Long while fast SMA > slow SMA |
| `ema_cross` | 12/26 | Same, exponential |
| `price_sma` | 50 | Long while price > SMA |
| `momentum` | 126 | Long while the 126-bar rate of change is positive (~6 months) |
| `supertrend` | 10, 3× | Long while Supertrend direction is up |
| `st_adx` | 10, 3×, ADX>20 | Supertrend, but only when ADX confirms a trend exists |
| `macd_zero` | 12/26/9 | Long while MACD > 0 |

**Momentum / crossover**

| Strategy | Defaults | Logic |
|---|---|---|
| `macd_cross` | 12/26/9 | Long while MACD > its signal line |

**Mean reversion**

| Strategy | Defaults | Logic |
|---|---|---|
| `rsi_revert` | 14, 30/55 | Buy below 30, exit above 55 |
| `stoch_revert` | 14/3, 20/80 | Buy below 20, exit above 80 |
| `cci_revert` | 20, ±100 | Buy below −100, exit above +100 |
| `bollinger_revert` | 20, 2σ | Buy the lower band, exit the middle |
| `pullback_trend` | 200 trend, RSI(2) 10/60 | Buy a 2-period RSI dip *inside* a 200-SMA uptrend — mean reversion with a trend filter, the most robust of this family |

**Breakout**

| Strategy | Defaults | Logic |
|---|---|---|
| `donchian_breakout` | 20 in / 10 out | Buy N-bar highs, exit on the shorter low |
| `keltner_break` | 20, 2× ATR | Buy the upper Keltner band |
| `bb_squeeze` | 20, 2σ, 100 lookback | Buy the break out of a volatility squeeze |
| `vol_breakout` | 20/10, 1.5× vol | Donchian break, but only with volume confirmation |

### The risk overlay

Optional, and it changes results dramatically — a strategy that's marginal without stops is often profitable with them, and vice versa.

| Parameter | Effect |
|---|---|
| `sl_pct` | Fixed stop below entry |
| `tp_pct` | Fixed target above entry |
| `trail_mult` | ATR chandelier trailing stop |
| `trail_period` | ATR period for the trail (default 22) |
| `max_bars` | Time stop — exit after N bars regardless |

**How intrabar fills are decided.** This matters more than it sounds, because it's where most backtesters quietly lie. The rule here: a bar that *opens* through a level fills at the open, never at a price the market skipped. A gap up through the target is a target fill even if that bar later trades down through the stop — the open came first chronologically, and that's not a choice, it's the order the prices happened in. Only when neither gap applies is the bar's interior examined, and there the stop beats the target, because true intrabar ordering is unknowable and the pessimistic reading is the honest one.

After a risk exit, the strategy must go flat once before re-entering — otherwise a still-long signal would re-enter on the very next bar and the stop would have accomplished nothing.

### Position sizing inside the sim

Off by default, and the default is worth understanding: a plain backtest puts **the entire account into every trade**. Its returns are what you'd get betting everything on each signal, which is almost certainly not what you do.

Turning sizing on makes position size a function of the stop:

```
qty  = (equity × risk%) / (entry − stop)
```

So 1% risk behind a 4% stop deploys 25% of the account; behind a 2% stop, 50%. **Tight stops concentrate capital** — the same warning the `/size` command gives, now visible in the equity curve. A cap (default 100% = no leverage) stops a very tight stop demanding 200% of the account.

Two modes. **Fixed fractional** sizes off your configured stop and refuses to run without one — silently falling back to all-in would produce a curve labelled "1% risk" that risks everything. **ATR risk** derives its own stop from volatility, so the same risk buys a large position in a quiet stock and a small one in a violent one.

The effect is usually a much shallower drawdown and a lower return. That's not the model being pessimistic; it's what your account would actually have done.

Sized runs report **R-multiples per trade**, using the same definition as the journal. That's the point: the backtest starts speaking the journal's unit.

### Regime filter

Optional gate: hold only while Nifty is above its own 200-DMA.

It's a **veto, not a signal** — it can remove a position but never create one, so a gated run is always a subset of the same strategy ungated and the comparison means something. The result reports what percentage of the window the gate allowed; if a strategy looks transformed by the filter while that reads 95%, it wasn't the filter.

Two details that matter for honesty. Each bar is matched to the most recent index bar that had already *closed* — matching same-day would let the strategy dodge a crash using the close of the session it happened in. And during the 200-bar warm-up the gate is **off**, not on: "I don't know the regime yet" means stand aside, because assuming risk-on would quietly ungate the first year of every backtest.

### Exporting trades

**Export CSV** writes the trade list in the journal's exact columns, in the journal's order, tagged with the strategy and stamped `source=backtest`.

That's what makes the live-vs-backtest comparison genuinely like-for-like instead of approximately so. Quantity and R are blank when the run wasn't sized — writing 1 would make the R column arithmetic nonsense — and charges are blank because costs are already inside the net figures, so naming them again would double-count against a journal row carrying real ones.

### The metrics

Costs first: `cost_bps` is charged on **every** entry and **every** exit, so 10 bps ≈ 0.2% per round trip. Test at 15–20 to be safe. **Every trade statistic is net of costs.** A trade that gained less than it cost to put on counts as the loss it was.

| Metric | Calculation | How to read it |
|---|---|---|
| **Trades** | Completed round trips | Under ~30, no statistic on this page means anything. Under ~100, treat everything as a hint. |
| **Expectancy** | Mean net return per trade | The only number that matters if you had to pick one. Positive and stable beats large and erratic. |
| **Win %** | Winners / total | Nearly useless alone. A 30% win rate with 4:1 winners is a fine system; an 80% win rate with 1:6 losers is a disaster waiting for its one bad week. |
| **Profit factor** | Gross profit / gross loss | >1.5 is respectable, >2 is strong, >3 on a small sample usually means curve-fitting. |
| **Return** | Total equity change | Depends entirely on the window. Always read next to vs B&H. |
| **vs B&H** | Strategy return − buy-and-hold return | The honest benchmark. A strategy that returned 40% while the stock returned 60% *lost you money* for the effort and risk. Most trend systems underperform buy-and-hold on a single trending stock; their value is elsewhere (drawdown, breadth). |
| **Max DD** | Largest peak-to-trough equity decline | The number that decides whether you can actually run the system. A 40% drawdown is arithmetically survivable and psychologically rarely so. |
| **Longest DD** | Bars spent below a previous peak | Often more punishing than depth. Two years underwater ends more systems than a fast 30% loss. |
| **Sharpe** | Annualised mean return / annualised stdev | Comparable across systems. Above 1 is good for a retail system; above 2 on daily bars invites suspicion. |
| **Sortino** | Same, but only downside deviation in the denominator | Fairer to systems whose volatility is mostly upside. |
| **Calmar** | CAGR / max drawdown | Return per unit of worst pain. |
| **Recovery factor** | Total return / max drawdown | How many drawdowns' worth of profit the system produced. |
| **Volatility** | Annualised stdev of bar returns | Context for Sharpe. |
| **Exposure** | Fraction of bars in a position | A system in the market 20% of the time earning the same as buy-and-hold is doing something genuinely valuable. Low exposure also means capital is free for other things. |

An open position at the end of the window has no exit, so it shows in the trade log marked *open* and in a separate count, but it doesn't vote in any statistic.

### The trade log

Every round trip: entry date and price, exit date and price, **reason** (signal / stop / target / trail / time), bars held, gross return, net return. The reason column is where you learn what the risk overlay is actually doing — if 80% of exits are "stop", your stop is inside the noise.

### Batch backtest

One strategy across many symbols at once.

**And this is where the page argues with you, deliberately.** A ranked table is a selection-bias machine. Run one strategy over sixteen symbols, sort by expectancy, and the top row looks excellent whether or not the strategy works — even coin flips produce a best-of-sixteen. Trading that top symbol *because* it topped the table is precisely how curve-fitting happens.

So the ranking is shown, but the headline is **breadth**:

- **Median symbol's result** — not the best one. Half did better, half worse, and no single lucky name can move it.
- **Share of symbols profitable at all** — an edge that's real shows up broadly.
- **Gap between best and median** — a large gap means the top row is an outlier, not a representative.

If a strategy is profitable on three names out of sixteen, you haven't found an edge; you've found three symbols that happened to trend during your window. The `verdict` line says so in words.

### Parameter heatmap

Sweep two parameters, colour the grid by Sharpe or profit factor. **The shape is the answer, not the peak.**

- A **plateau** — a broad region where nearby settings all work — means the strategy captures something real. You could have picked any of those settings and done roughly as well, which is exactly what you want, because you *will* be wrong about the exact number.
- A **single sharp peak** surrounded by mediocrity means you found a lucky cell. It will not survive contact with next year's data.

The analysis line quantifies this: neighbour ratio (how the best cell compares to its surroundings), plateau size, share of positive cells, median, spread. Candles are cached once per sweep, so a 100-cell grid costs one symbol's data.

### Monte Carlo

A backtest gives you *one* max drawdown. That number is a single draw from a distribution — the trades happened in one order, and a different order produces a different worst moment. This reshuffles the trade list 1,000× and reports the spread.

**Two modes, two questions.**

- **Shuffle** — permute the actual trades. *Was my drawdown just lucky sequencing?* Note it cannot tell you anything about return: compounding is commutative, so every ordering of the same trades ends at the identical number. The page says so rather than showing a zero-width distribution and letting you read meaning into it.
- **Bootstrap** — resample with replacement. *What if the next hundred trades resemble these but aren't these?* Return varies here, and this mode is both more honest as a forecast and more pessimistic, because it can draw the same bad trade twice.

**The headline is the floor:** "95% of orderings stayed above −18%." That's a number you can size against. "Max drawdown was −11%" isn't, because you don't know whether −11% was typical or the kindest arrangement available.

**The second number is the luck check** — where your actual backtest sits in the distribution. At the 90th percentile it got a gentle ordering, and the figure you've been anchoring on is optimistic; plan around the floor instead.

**What it assumes.** Both modes treat trades as independent. If a strategy genuinely streaks — trend systems win together and lose together — resampling breaks up those clusters and *understates* real drawdown. Read the result as a floor on how bad it gets, not a ceiling.

### Walk-forward

The honest answer to selection bias, and worth more than any new strategy.

**How it works.** Optimise on window A (in-sample), test the winning parameters — untouched — on window B (out-of-sample), roll forward, repeat. Indicators are computed over the full IS+OOS stretch so the OOS segment isn't warming up from zero, but only OOS bars are scored.

**The output table.** Per fold: bars, the parameters chosen on in-sample, the in-sample metric, the out-of-sample metric, and whether the fold was **kept**.

**How to read it.** Only the OOS column is real. The IS column exists to show you the size of the gap. In-sample 2.4 Sharpe collapsing to 0.1 out-of-sample is the normal, expected result for an over-fitted strategy, and seeing it repeatedly is the point of the tool. A strategy whose OOS numbers are merely *worse* rather than *absent* is doing well. Also watch parameter stability across folds: if the optimiser picks 10/40 in one fold and 45/90 in the next, there is no stable setting to find and the "optimisation" is fitting noise.

---

## 5. Journal

Route `/journal`. Where discipline gets measured. This is the counterweight to a toolchain that generates signals faster than judgment can absorb them.

### The R model

Everything is denominated in **R** — one R is the money you'd lose if the initial stop were hit.

```
risk per share = entry − initial stop
R multiple     = (exit − entry) / (entry − initial stop)     [long]
```

Buy at 405 with a stop at 398: 1R = ₹7/share. Exit at 419 and that's +2R, whether you traded 71 shares or 710. This is what makes trades comparable across position sizes and price levels, and it's why the daily loss guard is expressed in R.

**R is net of charges.** The backtester charges `cost_bps` on both legs, so the journal does the equivalent: real charges if you supply them from the contract note, otherwise an estimate on the same basis. Without this, live results would flatter themselves against backtested ones and the comparison would be meaningless.

A trade logged **without a stop** has no R. It's stored, but it's excluded from every statistic and counted separately as *unscored*. That's not a nag; it's arithmetic. You can't measure risk-adjusted performance on a trade that had no defined risk.

### Open positions table

**Symbol · Side · Qty · Entry · Stop · Last · Live R · Tag**

Live R is marked to the last price. It's the honest running answer to "how am I doing", stated in the same unit as the target.

### Closed trades table

**Symbol · Side · Qty · Entry · Exit · Charges · Why · Tag · Closed**

The **Why** field is free text and it's the most valuable column on the page — not because the app uses it, but because writing "chased it after the gap" thirty times is what changes behaviour.

**Tag** is the setup name (`st_adx`, `donchian`, whatever). It's what makes the comparison below possible, so tag consistently or the feature is dead.

### The statistics

Computed over closed, scored trades. Deliberately mirrors the backtester's definitions exactly — same exclusions, same net basis — so the two can be printed side by side without a footnote explaining why they aren't comparable.

| Statistic | Meaning |
|---|---|
| **Trades / unscored** | Sample size, and how many were logged without a stop |
| **Win rate %** | Winners as a share of the total |
| **Expectancy (R)** | Mean R per trade. **The headline number.** Positive means the process makes money over repetition |
| **Profit factor** | Total R won / total R lost |
| **Total R** | Cumulative — your realised performance in risk units |
| **Avg win R / avg loss R** | The shape of the distribution. Avg loss materially worse than −1R means stops are being widened or ignored |
| **Best / worst R** | Extremes. A worst of −3R when you trade 1R risk is a discipline problem, not bad luck |
| **Max consecutive wins / losses** | What the streaks actually looked like. Knowing you've historically had six losers in a row is what lets you survive the seventh |
| **Max drawdown (R)** | Largest peak-to-trough decline along the realised R curve |

### Live vs backtest

The point of the whole module. For each strategy tag: **live trades, live win%, live expectancy** beside **backtested trades, win%, expectancy** for the same tag.

**How to read the gap.** It will be negative. Backtests don't model slippage on your actual fills, the trades you talked yourself out of, or the ones you took that the system never signalled. A live expectancy of 60–70% of backtested is a *good* outcome. Live near zero while backtested is strongly positive means one of three things, and the trade log usually says which: the edge was over-fitted, you're not taking the trades the system gives, or you're taking extra ones it doesn't.

---

## 5a. Paper

Route `/paper`. The first thing in the project that makes a claim about a future it hasn't seen: signals fire, orders queue, the next session fills them, and after a month the journal says whether the edge survived an actual sequence of days.

**Orders fill at the next open — never the close that produced the signal.** This is the whole discipline. A flip computed from tonight's close could not have been traded at tonight's close; you didn't know until it had already happened. Filling at the signal bar is the single most common way to manufacture an edge that evaporates live, and the overnight gap is precisely where a real strategy's profit goes. So the gap is kept, not hidden: a fill worse than the signal price is the system working correctly.

Everything else follows from wanting the comparison to mean something. Costs are charged on both legs on the same basis the backtester uses. Positions are sized by the same risk model as `/size`, because a paper run that bets the account on every signal tests a strategy nobody would trade. Pending orders live in SQLite, because the window between signal and fill is overnight — exactly when the watcher gets restarted — and an in-memory queue would silently drop the trades the experiment exists to measure.

**What it doesn't model:** slippage beyond the open, partial fills, market impact. On liquid names in personal-account sizes those are small but not zero, so read the results as a mild over-estimate rather than a floor.

Paper rows sit in the journal marked `source=paper`, and every statistic has to opt in or out explicitly — there's no default. Blending them would be the most misleading number the app could produce: a month of flawless simulated trading quietly lifting the figure you judge real results by.

### Reading the three columns

Once a month of paper trades exists you have backtest, paper and live expectancy for the same tags. The gaps are diagnostic:

- **Backtest high, paper much lower** — the edge was over-fitted. The strategy worked on data it had already seen.
- **Backtest and paper close, live much lower** — the edge is real and the gap is you: hesitation, early exits, trades taken that the system never signalled. That's the most useful result the tool can give you, because it's the one you can actually fix.
- **All three close** — proceed to 6.2 with the smallest size you can trade.

## 5b. Semi-automatic execution

Signal fires → your phone asks → the box places the order with a stop attached. The human stays in the loop; only the typing goes away.

**Both `LIVE_TRADING` and `SEMIAUTO` must be set.** Two switches rather than one, because a single switch is something you flip while debugging and forget. Nothing places an order unless both are on, and the refusal names which is missing.

### The safeguards, and why each exists

**Single-use tokens.** Approval arrives over a public ntfy topic, and a message on a topic can be observed and replayed by anyone who has ever seen one. For `/quote` that's harmless; for an order it isn't. The first approval consumes the token, so a replay finds nothing to claim. The claim is a conditional `UPDATE`, so two taps racing can't both win.

**Expiry, 15 minutes.** An approval tapped the next morning would execute against a setup that stopped existing overnight — price moved, stop meaningless, and the trade you agreed to is not the trade you'd get.

**The guardrails run twice — at proposal AND at approval.** This is the most important line in the module. Minutes pass between the push and your tap, and the world moves: you may have opened another position, hit the daily loss limit, or the session may have closed. Checking only when the push was sent would let a stale approval walk through limits breached afterwards.

**No stop, no order.** An entry without a stop has no defined risk, can't be sized honestly, and can't be scored afterwards. It's refused outright rather than defaulted.

**The stop goes on immediately**, as a GTT with a limit price just below the trigger — a market sell on a gap can fill far below anything sensible. If the GTT fails, the push is `priority=max` and says *you are unprotected, set it manually now*. This module would rather wake you at 2am than let you believe you're covered.

**Every failure is reported, not just the first.** Blocked for three reasons and blocked for one look identical if you only see one, and the difference decides whether you override.

**A failed order never writes a journal row.** A trade record for an order that didn't exist would corrupt every statistic downstream of it.

### Approving

Two paths. `/approve TOKEN pin=XXXX` from the phone — this is the primary one and needs no inbound network access. Or, if `PUBLIC_BASE_URL` is set, the push carries an ntfy **Approve** button that hits the local API directly, behind the same Origin and Host guards as everything else.

The PIN is required either way: `approve` is a write command, and the only one that spends money.

## 6. Portfolio

Route `/portfolio`. Read-only mirror of your Kite holdings and positions, annotated with everything TickerForge already knows.

**Holdings:** Symbol · Qty · Avg · Last · Value · P&L · Weight · **Valuation · Signals · 3×ATR stop · Results**

The last four columns are what make this more than a broker screen:

- **Valuation** — the composite verdict from the valuation engine (undervalued / fair / rich / overvalued). Something you bought at "undervalued" now reading "rich" is a thesis that has played out.
- **Signals** — the current stance of your configured strategies. Holding a stock every strategy has turned bearish on is a decision, and it should be a conscious one.
- **3×ATR stop** — where a chandelier stop would sit, and how far price is from it. Turns a vague "should I still hold this" into a number.
- **Results** — the next results date, from the events calendar. Holding through earnings is a position on volatility whether you meant it or not.

**Weight** is the allocation check. Concentration is the fastest route to a bad month, and it accumulates silently as winners run.

Holdings auto-join the watchlist, and a trailing-stop alert can be armed on any holding in one tap.

---

## 7. Valuation

Route `/valuation`. Damodaran-style intrinsic valuation, run on the fundamentals already stored.

### The discount rate

Cost of equity, built rather than assumed:

```
re = risk-free (6.9%) + equity risk premium (5.5%) + size premium
```

Size premium: +3% under ₹500 Cr, +2% under ₹5,000 Cr, +1% under ₹20,000 Cr, nothing above. So a large cap discounts at ~12.4%, a small cap at ~15.4%. Smaller companies are riskier, so their future cash flows are worth less today — that's the entire logic, and it's why the same growth assumption produces a lower fair value for a small cap.

### The models

Five, run in parallel where the inputs exist:

| Model | Needs | What it assumes |
|---|---|---|
| **Earnings-power DCF** | EPS, growth, `re` | EPS grows at `g` for N years (default 10), then at the terminal rate (5%), discounted at `re`. The workhorse. |
| **Graham Number** | EPS, book value | `√(22.5 × EPS × BVPS)`. A crude 1930s ceiling for a defensive investor. Deliberately conservative. |
| **Graham revised formula** | EPS, growth, bond yield | Value as a function of growth and prevailing bond yields. Sensitive to the bond input. |
| **Justified P/B** | BVPS, ROE, growth, `re` | `(ROE − g) / (re − g) × BVPS`. Sound in theory — a company earning above its cost of equity deserves a premium to book. |
| **Dividend discount** | Dividend yield, growth, `re` | Only meaningful for genuine dividend payers. |

The **composite** is the median of whichever models produced a positive number, and the model count is shown beside it. Median rather than mean, so one wild model can't drag the answer.

### Margin of safety and the verdict

```
MoS % = (composite fair value − price) / composite fair value × 100
```

| MoS | Verdict |
|---|---|
| ≥ 30% | undervalued |
| 0 to 30% | fair |
| −25% to 0 | rich |
| < −25% | overvalued |

The 30% threshold is the point: you require a discount not because you expect the market to be wrong by exactly that much, but because *your inputs* are wrong by roughly that much. The margin is for your own error.

### Scenarios

Bear / base / bull, each a different assumption set rather than a different formula:

- **Base** — historical growth, capped at 18%; the computed `re`
- **Bear** — half the growth, `re` +2%
- **Bull** — 1.5× growth +2pp, `re` −1%

Read the *spread*, not the base case. If bear and bull are ₹400 and ₹2,000, you don't have a valuation, you have a range wide enough to justify anything.

### Sensitivity grid

Fair value across growth (rows) × discount rate (columns), coloured by upside. The same lesson as the backtest heatmap: a valuation that's only attractive in one corner of the grid isn't attractive, it's assumption-dependent. If most of the grid shows upside, the conclusion is robust to being wrong about the inputs.

### Reading valuation honestly

Three warnings, in order of how much money they can cost you:

1. **Two models decline to answer when growth approaches the discount rate.** Justified P/B and the dividend discount model both divide by `(re − g)`, and as that denominator vanishes the fair value goes to infinity — an optimistic growth assumption produces an arithmetically absurd answer rather than merely a wrong one. Below a 2-point spread they return nothing instead of a number. A blank cell here is the model being honest, not missing data; it means the growth assumption is too close to the discount rate for that method to say anything.
2. **The number of models varies by company, and it's shown.** A dividend-paying, profitable company gets five; a loss-maker might get two. A composite from two models is not the same kind of number as one from five — check the model count before comparing two stocks' margins of safety.
3. **Growth is capped at 18% for a reason.** No company grows at 30% for ten years. When the DCF disagrees violently with the Graham Number, the DCF is usually the one extrapolating too confidently.

---

## 8. Fundamentals

Route `/fundamentals`. Screener.in data, ingested and stored raw.

The philosophy is worth understanding: **store the raw statement tables, compute the ratios yourself.** Quarterly results, annual P&L, balance sheet, cash flow, and screener's own ratio history all get stored as fetched. Ratios are then computed from the primary numbers.

Screener's headline ROCE is *also* kept, deliberately, so you can compare it against yours. Everyone defines "capital employed" differently — some exclude cash, some use net debt — and seeing the definitional gap is the point. When the two disagree, neither is wrong; they're answering slightly different questions.

Fetching is polite: one page per request, cached with a timestamp, refreshed on demand or when older than seven days. Consolidated figures preferred over standalone.

---

## 9. Results

Two different things share this name.

**Per-symbol** (`/results` for a watchlist stock) answers one question: *are this quarter's results out yet?* If released, you get a Sales / Operating Profit / Net Profit table with YoY and QoQ growth against the three comparable quarters. If not, you get the expected date from the NSE corporate-events feed, or "not announced".

**Market-wide** is the "Latest Quarterly Results" feed — every company that just reported, scraped from a public screener.in screen:

**Company · CMP · P/E · M Cap ₹Cr · NP Qtr ₹Cr · NP YoY · Sales Qtr ₹Cr · Sales YoY · ROCE**

Read the two growth columns together. Net profit growing much faster than sales means margin expansion — good, but check whether it's operational or a one-off tax or other-income item. Sales growing while profit doesn't means costs are winning. Both negative on a stock you hold is the sort of thing you'd rather learn from this page than from the price.

---

## 10. Actions

Route `/actions`. Corporate events for watchlist stocks: **Date · Symbol · Company · Type · Details**.

Dividends, splits, bonuses, buybacks, board meetings, results dates. Pulled from NSE's feeds.

Two design details worth knowing. **Dedupe:** every event gets a stable hash of its salient fields stored in SQLite, so restarting the poller never re-notifies you. **First-run seeding:** the first poll of a feed marks the existing backlog as seen silently, so you don't get two weeks of history blasted at your phone.

The practical use is avoiding surprises. An ex-dividend gap is not a breakdown. A stock that "crashed 40%" on a 1:2 split did nothing at all.

---

## 11. News

Route `/news`. One Google News query per watchlist stock, windowed, deduped across digests, near-duplicates collapsed, then curated into one push.

Three curation backends: `none` (grouped plain list, zero dependencies), `ollama` (local LLM, free and private), `claude` (best quality, needs an API key).

Treat it as awareness, not signal. By the time a headline is on Google News the price has moved. Its real value is explanation: when the scanner shows a 3× volume day, this tells you whether it was an order win or a fraud allegation.

---

## 12. Metrics

Route `/metrics`. A searchable glossary of ~35 fundamental metrics — the reference layer behind the Fundamentals and Valuation pages.

Each entry has: **formula · what it means · why it matters · what drives it · a rule of thumb · the caveat.**

The caveats are the reason the page exists. "ROE >20% is excellent" is worth much less than "leverage inflates ROE — a highly indebted firm can show a great ROE that's really just borrowed risk." Grouped by Profitability & returns, Valuation multiples, Growth, Balance sheet, Cash flow, and Quality.

If a number on any other page needs explaining, look here first — this guide deliberately doesn't duplicate it.

---

## 13. The phone channel

Two-way, over ntfy. Push comes out, commands go in.

### Alerts

Armed per symbol and timeframe (`1m`, `5m`, `15m`, `1h`, `day`; default `5m`), evaluated by the watcher on a ~5-second poll. `repeat` re-arms after firing; without it an alert fires once and disarms.

Twenty-eight conditions across six families:

- **Price** — crossing a level, VWAP, previous day's high/low, opening-range break, a % move on the day
- **Indicators** — Supertrend flips, RSI levels and bands, price vs an EMA, EMA crossovers, EMA zones, MACD crosses, Bollinger breaks, ADX thresholds, volume multiples
- **Anchored VWAP** — VWAP anchored at a recent low, high, session start, major swing, or an arbitrary bar count back
- **Confluence** — the multi-timeframe score crossing a level, or all timeframes aligning
- **Trailing stop** — a chandelier stop being hit, with `/exit SYM` reporting where the stop sits and how much R you're up

**Anchored VWAP deserves a note.** Ordinary VWAP resets daily. Anchored VWAP starts from a bar *you* choose — the low of a selloff, the day of an earnings gap — and answers "what's the average price everyone who bought since that event paid?" Price reclaiming an anchored VWAP means the buyers from that event are collectively back to breakeven, which is where supply tends to appear.

### Commands

Read commands (quotes, levels, signals, confluence, valuation, position sizing) work without authentication. **Write commands** — arming and removing alerts, logging and closing trades — require a PIN when `NTFY_CMD_PIN` is set: `pin=1234` anywhere in the message.

Be clear-eyed about the security model: the ntfy topic is the credential. Anyone who knows the topic name can read your pushes. The PIN gates state changes, not visibility, and there's no replay protection — someone who observes a PIN'd command within the retention window could repeat it. Use a long random topic name, and treat the PIN as a guard against accidents more than adversaries.

### The pre-market brief (08:45)

One composed push, ordered by what changes a decision soonest:

1. **Where you stand** — open positions and live R. What you already own outranks anything you might buy.
2. **Regime** — the market-wide read (below).
3. **Gaps to watch** — watchlist previous closes against overnight direction.
4. **Results due today** — from the stored calendar.
5. **Armed alerts** — what's already watching.
6. **Yesterday's signal flips.**
7. **Overnight headlines.**

Almost none of this is new data. The value is that things you'd otherwise open five pages to check arrive in one message while there's still time to act.

### Market regime

Nifty 50 versus its own 200-DMA, plus the share of your watchlist trading above their 200-DMAs. Reduced to one verdict: **risk-on / neutral / risk-off**.

Use it as a position-size dial, not an on/off switch. Long breakout signals in a risk-off tape are exactly where breakout systems bleed — the regime read makes the tape visible *before* the trade rather than after.

---

## 14. Risk sizing

The `/size` command and the Desk widget. Given entry, stop and account size, it returns the quantity that puts a fixed percentage of capital at risk.

```
risk ₹     = account × risk %
qty        = risk ₹ / (entry − stop)
```

Example — ₹5,00,000 account, 1% risk, entry 405, stop 398:

```
LONG 714 @ 405  (SL 398)
risk ₹4,998 · 1R = ₹7/sh
value ₹289,170 · 57.83% of capital
targets  1R 412 · 2R 419 · 3R 426
⚠ 58% of capital in one name.
```

**The warning is the most useful part.** The maths is correct — a 1.7% stop with 1% risk *requires* a big position — but "correct" and "wise" diverge here. A tight stop concentrates capital, and a gap through it loses far more than 1R. The R model assumes your stop fills at your stop; gaps don't care.

When no stop is given, an ATR-based one is suggested. This is usually better than a round number: a stop should sit outside the noise, and ATR is what noise is measured in.

### The daily loss guard

Configure `DAILY_LOSS_R` (e.g. 2R). The watcher counts realised R from journal entries and pushes a "stand down" notice when the limit is breached. It counts open positions' risk against the limit too, so being down 1R with two open trades already puts you at the edge.

This is the single highest-leverage feature in the app, and it only works if you log trades.

---

## 15. Routines that tie it together

**Pre-open (5 minutes).** Read the brief. Check the regime line first — it sets how aggressive everything else should be. Note results due today on anything you hold.

**During the session.** Desk for the symbol in play. Alerts do the watching; the phone does the notifying. The point of the alert engine is that you *don't* watch charts.

**Post-close (10 minutes).** EOD signals push. Scanner if you're hunting. Log every trade you took, with the Why field filled in, while you still remember the reason.

**Weekly (30 minutes).** Open the journal. Look at expectancy by tag, and at live-vs-backtest. Ask one question: which tag is actually making money? Most people trade five setups and make all their money on one.

**Before adopting a new strategy.** Single backtest to see if it's plausible → batch across the watchlist to check breadth → heatmap to check the parameters are a plateau, not a peak → walk-forward to check it survives out-of-sample → Monte Carlo to see what the bad orderings look like before you meet one. If it fails at any stage, stop. This ordering exists because each step is cheaper than the next and rejects most candidates.

---

## 16. What to distrust

Every tool has an edge where it stops being reliable. Knowing where is what separates using a tool from being used by one.

**Small samples.** Any statistic on fewer than 30 trades is noise wearing a decimal point. This applies to the journal as much as the backtester, and the journal is where you'll be most tempted to forget it.

**The ranked table.** In batch results, the top row is selected *because* it's extreme. Read the median.

**In-sample results.** A backtest over data you chose after seeing it is not evidence. The walk-forward OOS column is the only number on the backtest page that has earned belief.

**Valuation precision.** A fair value of ₹2,547.83 implies a precision that does not exist. Round it to the nearest hundred mentally, and check the scenario spread — and the model count — before believing any of it.

**Live quotes on the Desk during volatility.** A ~5-second poll is fine for swing decisions and not fine for anything that needs to be exact at the tick.

**Scraped fundamentals.** Screener.in can change its page structure, and a scraper that half-works is worse than one that fails loudly. If a number looks wrong, check the source page before acting.

**Your own tagging.** Live-vs-backtest comparisons are only as good as your tag discipline. Inconsistent tags produce a table that looks authoritative and means nothing.

**The tooling itself.** It generates signals faster than judgment can absorb them — that was flagged at the very beginning of the project, and it's why the journal and the risk guard exist. More alerts is not more edge. The counterweight only works if you use it.
