# TickerForge — Full Reference

Every command, flag, alert condition, page and setting.

TickerForge runs entirely on your own machine. It talks to Zerodha Kite for
prices and candles, screener.in for fundamentals, NSE for corporate events, and
ntfy for pushing to (and taking commands from) your phone. Nothing is hosted;
the web UI binds to `127.0.0.1` only.

**Contents**

1. [Setup](#1-setup)
2. [The daily routine](#2-the-daily-routine)
3. [What runs where](#3-what-runs-where-processes)
4. [CLI reference](#4-cli-reference)
5. [Phone commands](#5-phone-commands)
6. [Alert conditions](#6-alert-conditions)
7. [Strategies](#7-strategies)
8. [The backtester](#8-the-backtester)
9. [Web UI](#9-web-ui)
10. [HTTP API](#10-http-api)
11. [Configuration](#11-configuration)
12. [Data & files](#12-data--files)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit it
```

**Kite app** — create one at <https://developers.kite.trade>. Set the redirect
URL to `http://127.0.0.1:5000/`; the web server catches the redirect and
completes login in one click. Historical candles and the live tick stream need
the paid Connect data subscription.

**ntfy** — install the app, subscribe to two long random topic names. One
(`NTFY_TOPIC`) receives alerts; the other (`NTFY_CMD_TOPIC`) is where you send
commands. They must be different, or replies loop back in as commands.

Then:

```bash
python -m app.cli login
python -m app.cli sync-instruments     # ~90k rows; re-run nightly
python -m app.cli status               # confirm session + topic
python -m app.cli notify-test          # confirm the phone buzzes
```

---

## 2. The daily routine

Kite access tokens die every morning around 07:30 IST. This is the one manual
step:

```bash
python -m app.cli login
```

It prints a URL. Log in, and if `serve` is running the redirect completes the
login automatically — otherwise paste the `request_token` back at the prompt.
Everything else reuses the cached token for the rest of the day.

The intraday watcher re-reads the token every cycle, so logging in revives a
running watcher in place. You don't need to restart it.

---

## 3. What runs where (processes)

Four long-running processes, each in its own terminal. None depends on the
others being up; each degrades on its own.

| Process | Command | Purpose |
|---|---|---|
| Web server | `python -m app.cli serve` | The UI on <http://127.0.0.1:5000> |
| Events watcher | `python -m app.cli events run` | NSE corporate actions + scheduled digests |
| Intraday watcher | `python -m app.cli intraday run --ticks` | Fires armed alerts; runs the EOD signal scan |
| Remote listener | `python -m app.cli remote` | Two-way phone control |

A crash is silent by nature — the intraday watcher pushes to your phone when
its session dies or its tick stream goes quiet, but nothing supervises the
processes themselves. Roadmap Phase 5 addresses this.

---

## 4. CLI reference

All commands are `python -m app.cli <command>`.

### Session & setup

| Command | What it does |
|---|---|
| `login` | Daily interactive Kite login |
| `sync-instruments` | Download the instrument dump into SQLite (needed for search and every symbol lookup) |
| `status` | Session validity, instrument count, watchlist size, whether ntfy is configured |
| `notify-test ["message"]` | Send a test push |
| `serve` | Web server on `127.0.0.1:5000` |
| `remote` | Listen for phone commands over ntfy |

### Search & watchlist

```bash
python -m app.cli search tata              # equities only
python -m app.cli search NIFTY --all       # include F&O, indices, etc.

python -m app.cli watch add RELIANCE
python -m app.cli watch add TCS --exchange BSE
python -m app.cli watch remove RELIANCE
python -m app.cli watch list
python -m app.cli watch fetch-missing      # prime fundamentals+results for the whole list
```

Adding a stock auto-fetches its fundamentals and results unless
`AUTO_FETCH_ON_ADD=0`.

### Corporate events (NSE)

```bash
python -m app.cli events poll                    # one-shot
python -m app.cli events digest                  # curated digest now
python -m app.cli events digest --dry            # print only, don't send or mark seen
python -m app.cli events run                     # continuous
```

`events run` flags:

| Flag | Default | Notes |
|---|---|---|
| `--every N` | `30` | Seconds between polls **during market hours only** |
| `--always` | off | Poll every N seconds around the clock, ignoring the schedule |
| `--digest-times` | `08:45,21:15` | Comma-separated `HH:MM`; empty string disables |
| `--brief-at` | `08:45` | Pre-market brief time, weekdays only |

Outside market hours `--every` has no effect. The schedule polls continuously
on weekdays 09:00–16:00, then fires single sweeps at 16:30, 18:00, 19:30,
21:00 and 22:30 (results and board-meeting outcomes land after close), plus
11:00 and 18:00 at weekends. The loop wakes every 20s regardless, so `--every`
values that aren't multiples of 20 round up.

### News

```bash
python -m app.cli news digest                    # collect + curate + push
python -m app.cli news digest --hours 48 --dry
python -m app.cli news run --times 08:30,18:30   # daily at fixed times
```

Curation depends on `NEWS_CURATOR` — see [Configuration](#11-configuration).

### Fundamentals

```bash
python -m app.cli fund fetch RELIANCE
python -m app.cli fund fetch --all               # whole watchlist
python -m app.cli fund show RELIANCE             # stored snapshot + computed ratios
python -m app.cli fund diagnose GRSE            # why did this get no ratios?
python -m app.cli fund slug GRSE 533250         # override the screener.in slug
```

`fund diagnose` walks the pipeline — URL, HTTP status, page shape, which
sections parsed, which ratio inputs are present — and names the first stage
that broke. A fetch can fail in five places and they all otherwise surface
identically as "nothing stored".

### Market-wide results

```bash
python -m app.cli results fetch-latest                    # scrape screener's feed
python -m app.cli results fetch-latest --sort np
python -m app.cli results latest                          # show the cache
python -m app.cli results latest --scope watch --limit 40
```

`--sort`: `latest` · `np` · `sales` · `mcap` · `pe`
`--scope`: `all` · `watch`

### Strategy signals

```bash
python -m app.cli signals                        # whole watchlist, no push
python -m app.cli signals BEL TATAMOTORS
python -m app.cli signals --push                 # push flips to the phone
```

Runs on **closed daily bars** — today's forming candle is dropped during
market hours, so a signal never flickers intraday and then vanishes.

Flips are detected against the last stance that was *reported to you*, not
against the previous bar. A flip that happened while nothing was running is
still found later, and comes tagged with how many bars ago it fired plus the
price at the time. Without `--push` the scan is read-only and won't consume a
flip that the evening's EOD push should deliver.

### Scanner (EOD, across a universe)

A universe is what you *search*; the watchlist is what you follow. They're
separate because the point of scanning is surfacing names you aren't already
watching.

```bash
# 1. define what to search
python -m app.cli universe set nifty200 RELIANCE TCS HDFCBANK ...
python -m app.cli universe from-watchlist          # to try it out
python -m app.cli universe coverage nifty200       # how many have candles

# 2. fetch candles — the overnight job. Resumable; re-runs are cheap.
python -m app.cli universe backfill nifty200

# 3. scan
python -m app.cli scan 52w_high volume_surge --universe nifty200
python -m app.cli scan 52w_high --save momentum
python -m app.cli scan momentum --saved
```

Also on `/scanner`, and from the phone as `/scan 52w_high volume_surge`.

**Conditions are ANDed** — the point is a list short enough to read. `52w_high`
alone might be forty names; `52w_high volume_surge` is five.

| Condition | Fires when |
|---|---|
| `52w_high` / `52w_low` | Close makes a new 52-week extreme |
| `volume_surge` | Volume ≥ N× its own 20-day average (default 2) |
| `golden_cross` / `death_cross` | Fast SMA crosses slow — the *event*, not the state |
| `donchian_break` | Close breaks the prior N-bar high |
| `bb_squeeze` | Bandwidth was in its lowest decile and price has just broken out |
| `above_sma` | Trading above its N-day SMA |
| `rsi_range` | RSI inside a band |
| `strategy_long` | Any backtester strategy has just flipped long |

`strategy_long` is the bridge worth knowing about: it runs the same rule the
backtester trades, so a hit is directly testable rather than a lookalike.

**Scans use stored candles only** and never fetch — that's what `backfill` is
for. Symbols without enough history are reported as *skipped*, so an empty
result means "nothing matched" rather than "nothing was looked at".

Saved scans with `"eod": true` in their spec have their top hits appended to
the evening push. Opt-in per scan, because a scanner that pushes everything
nightly is one you mute.

### Walk-forward validation

```bash
python -m app.cli walkforward BEL ema_cross fast=5:25:5 slow=30:70:20
python -m app.cli walkforward BEL supertrend --mode anchored --is-bars 400
python -m app.cli walkforward BEL st_adx          # no grid = plain OOS test
```

Also on `/backtest` under "Walk-forward", and from the phone as
`/wf SYM STRATEGY fast=5:25:5`.

**This is the only measurement here that doesn't use hindsight.** Every other
tool optimises and scores on the same bars. Walk-forward optimises on an
in-sample window, then applies the chosen parameters *unchanged* to the next
window the optimiser never saw, and rolls forward. Stitch those out-of-sample
segments together and the resulting curve was produced entirely by decisions
made without knowing the answers.

Two numbers carry the weight:

- **Efficiency** = out-of-sample ÷ in-sample. Near 1 means the optimiser found
  something that persisted; near 0 means it fitted noise.
- **Parameter stability.** If the optimiser picks `fast=5`, then `fast=30`,
  then `fast=12`, there is no optimum to find — the good folds are luck rather
  than a setting you could have chosen in advance.

`rolling` keeps the in-sample window a fixed length so old regimes drop out;
`anchored` grows it from the start so every fold uses all history to date.
Neither is right in general, which is why both are available.

Expect out-of-sample to be much worse than the optimised backtest. That gap is
the finding.

### Parameter heatmap

```bash
python -m app.cli heatmap BEL ema_cross fast=5:30:5 slow=30:80:10
python -m app.cli heatmap BEL rsi_revert length=5,7,9,11,14 --metric profit_factor
```

Also on `/backtest` under "Parameter sweep", and from the phone as
`/heatmap SYM STRATEGY fast=5:30:5 slow=30:80:10`.

Two value forms, and they mean different things:

| Form | Meaning |
|---|---|
| `5,10,15` | exactly those three values |
| `5:30:5` | an inclusive range — 5, 10, 15, 20, 25, 30 |

**Read the shape, not the maximum.** A broad warm region (a *plateau*) means
nearby settings all work, so the exact number isn't load-bearing — which
matters because you will be wrong about it. One bright cell surrounded by dull
ones (a *sharp peak*) means the result depends on hitting that value, which is
what curve-fitting looks like. The verdict says which you're looking at, based
on how the best cell compares to its immediate neighbours.

Metrics: `sharpe` (default) · `profit_factor` · `expectancy_pct` ·
`total_return_pct` · `cagr_pct` · `max_drawdown_pct` · `win_rate_pct`.

Candles are fetched once for the whole grid, so a 30-cell sweep costs one
rate-limited API call, not thirty. Clicking a cell loads that parameter pair
into the single run below.

### Chart events and layouts

**Event markers** — the ● Events menu toggles dots on the price series:
Supertrend flip, RSI crossing 70/30, MACD signal cross, golden/death cross,
Bollinger band break, volume ≥ 2× average.

These mark the bar a condition *became* true, not every bar it stays true.
Marking the state would paint half the chart. They're computed from the same
closed bars the alert engine and backtester use, so a marker and an alert agree.

**Saved layouts** — the ▤ Layout menu. Type a name and press Enter to save the
current setup: every indicator with its parameters *and* per-instance colour
overrides, which event markers are on, and the interval and range. Shift+Enter
also marks it the default, which is applied automatically at startup.

Layouts live in SQLite rather than the browser, so they survive switching
browsers or clearing site data.

### Batch backtest

```bash
python -m app.cli batch ema_cross                      # across the watchlist
python -m app.cli batch st_adx BEL HAL TCS --days 1460
python -m app.cli batch supertrend --sort profit_factor --limit 20
```

Also on `/backtest` — the "Across watchlist" button beside Run — and from the
phone as `/batch STRATEGY [SYM ...]`.

Read the **breadth** figures before the ranking. A sorted table's top row looks
good whether or not the strategy works: best-of-N is impressive even from pure
noise. The median expectancy and the profitable share can't be moved by one
lucky symbol, and the verdict says in words whether the edge is broad, narrow,
a coin-flip, or negative. When a single name is profitable while the median
isn't, it says that too — that's the exact trap.

Sorts: `expectancy_pct` (default) · `total_return_pct` · `profit_factor` ·
`sharpe` · `win_rate_pct` · `max_drawdown_pct` · `trades` · `symbol`.

### Portfolio (read-only)

```bash
python -m app.cli portfolio                    # holdings + annotations
python -m app.cli portfolio sync-watchlist     # add holdings to the watchlist
```

Pulls holdings and positions from Kite and annotates each with what
TickerForge already knows: your own valuation verdict, the strategies' current
stance, where a 3×ATR chandelier stop would sit, and the next results date.
Flags concentration, price below the stop, imminent results, and holdings your
models now read as overvalued.

**Read-only and permanently so.** The module imports nothing that places,
modifies or cancels an order, and a test asserts it. Order placement is Phase
6, gated behind a paper broker and an approval step.

Broker calls are cached for 60s, so refreshing the page doesn't spend a
rate-limited API call. `?refresh=true` forces a re-read.

### Pre-market brief

```bash
python -m app.cli brief                 # compose and push now
python -m app.cli brief --dry           # print it, don't send
python -m app.cli brief --skip news,signals
```

Sent automatically once per weekday by `events run`, at `--brief-at` (default
08:45). That process is chosen because it's already awake before the open —
the intraday watcher idles until 09:15.

Sections, in the order they appear: open positions with live R, today's risk
budget, the tape (Nifty vs its 200-DMA plus watchlist breadth), watchlist
results due today, armed alerts within 2% of their trigger, recent signal
stance changes, overnight headlines. Each is isolated — a dead news source
costs you that line, not the brief.

Not included: gap prediction. GIFT Nifty trades on NSE IX and isn't in the Kite
instrument dump, so there's no source for overnight direction. The "alerts
close to firing" section covers the actionable half of what that would tell you.

### Risk sizing and the journal

```bash
python -m app.cli size --entry 405 --sl 398
python -m app.cli size --entry 405 --sl 398 --risk 1.5 --account 500000
python -m app.cli size BEL --entry 405            # stop suggested from ATR

python -m app.cli journal open BEL --entry 405 --sl 398 --tag st_adx
python -m app.cli journal list [SYMBOL]
python -m app.cli journal amend 3 --sl 411        # trail the stop
python -m app.cli journal close 3 --exit 419 --reason target
python -m app.cli journal stats
```

`journal open` sizes itself from `ACCOUNT_SIZE` and `RISK_PCT` when `--qty` is
omitted and a `--sl` is given. `--tag` should be a strategy key from
[section 7](#7-strategies) if you want the trade to appear in the live-vs-backtest comparison.

### Intraday

```bash
python -m app.cli intraday run --ticks           # websocket, lowest latency
python -m app.cli intraday run                   # polling fallback
python -m app.cli intraday run --every 10
python -m app.cli intraday run --ticks --always  # ignore market hours

python -m app.cli intraday levels BEL            # print levels once
python -m app.cli intraday alerts                # list armed alerts
```

| Flag | Default | Notes |
|---|---|---|
| `--ticks` | off | Subscribe to the tick stream; alerts fire the moment price moves |
| `--every N` | `5` | Poll interval in seconds (polling mode only) |
| `--always` | off | Run outside market hours too |

`--ticks` is the mode to use. Both modes run the EOD signal scan at 15:35 and
push a warning if the Kite session dies; tick mode additionally warns if the
stream goes silent for 5 minutes while the market is open with symbols
subscribed.

---

## 5. Phone commands

Send these as ntfy messages to `NTFY_CMD_TOPIC`. Replies come back on
`NTFY_TOPIC`. `python -m app.cli remote` must be running.

Send `/help` at any time for the built-in list.

### Live / intraday

| Command | Reply |
|---|---|
| `/brief SYM` | One-look cockpit: price, levels, armed alerts, next result, valuation verdict |
| `/levels SYM` | VWAP, PDH/PDL, CPR, pivots, opening range, where price sits |
| `/chart SYM` | Intraday chart image with levels drawn on |
| `/ind SYM [tf]` | RSI, Supertrend, EMAs, MACD, ADX right now |
| `/conf SYM` | Multi-timeframe confluence across 5m/15m/1h/day |
| `/exit SYM` | Where the trailing stop sits and R so far |
| `/price SYM` | Last price and day change |

Aliases: `/b` = `/brief`, `/lvl` = `/levels`, `/ta` and `/indicators` = `/ind`,
`/mtf` and `/confluence` = `/conf`, `/trail` and `/stop` = `/exit`,
`/quote` = `/price`.

### Alerts

| Command | Effect |
|---|---|
| `/alert SYM COND [tf] [repeat]` | Arm an alert |
| `/alerts [SYM]` | List armed alerts |
| `/unalert ID` · `/unalert SYM` · `/unalert all` | Disarm |

`/alert` and `/unalert` change state. If `NTFY_CMD_PIN` is set, they need
`pin=XXXX` anywhere on the line:

```
/alert BEL rsi > 70 15m repeat pin=4821
```

Everything else is read-only and never needs a PIN.

### Risk & journal

Size the trade first, then log what you actually did.

| Command | Effect |
|---|---|
| `/size SYM entry=405 sl=398` | Quantity, 1R, position %, targets at 1R/2R/3R |
| `/log buy SYM 405 sl=398 tag=st_adx` | Record a trade |
| `/close ID 419 reason=target` | Close it; reports R and today's remaining budget |
| `/amend ID sl=411` | Trail the stop |
| `/premarket` | The morning brief on demand (also bare `/brief`) |
| `/batch STRATEGY [SYM…]` | One strategy across the watchlist, with breadth stats |
| `/heatmap SYM STRATEGY p=5:30:5` | Sweep parameters — plateau or curve-fit? |
| `/scan 52w_high volume_surge` | EOD scan across a universe (or a saved scan name) |
| `/wf SYM STRATEGY fast=5:25:5` | Walk-forward — does it survive unseen data? |
| `/portfolio` | Kite holdings with valuation, signal stance and stop distance |
| `/journal [SYM]` | Open positions with live R, recent closes, today's R |
| `/journal stats` | Expectancy, win rate and drawdown per tag |

Arguments parse in any order, so `/log buy BEL sl=398 405 tag=st_adx` works.
Omit `sl` on `/size` and a stop is suggested from 2×ATR. Omit `qty` on `/log`
and it's sized from your risk settings.

`/log`, `/close` and `/amend` change state, so they need `pin=XXXX` when
`NTFY_CMD_PIN` is set. **`/size` does not** — it calculates and writes nothing,
so it stays available even on a PIN-protected channel.

The daily guard is both pushed and pulled. The moment a close takes you past
`DAILY_LOSS_R`, a stand-down notice goes to your phone — once per day, deduped
in the database so a watcher restart can't re-send it. After that, alerts carry
a "⛔ daily loss limit already reached today" line so a signal doesn't arrive
looking like an invitation, and `/size` prepends the refusal as you're about to
size the next trade.

Nothing is blocked. Open positions are untouched and you can still log trades —
the guard makes ignoring the limit a visible choice rather than an accident.

### Signals & research

| Command | Reply |
|---|---|
| `/signals [SYM]` | Unreported flips plus the current long/flat stance per strategy |
| `/valuation SYM` | Damodaran-style valuation, composite verdict |
| `/fundamentals SYM` | Stored snapshot and computed ratios |
| `/news SYM` | Recent headlines |
| `/results SYM` | Quarter numbers, or the awaited date |
| `/results all` · `/results latest` | Market-wide feed |
| `/dividends SYM` | Dividend history |

Research commands work for **any** stock, not just the watchlist — data is
fetched on demand.

Aliases: `/val` = `/valuation`, `/fund` = `/fundamentals`, `/sig` = `/signals`,
`/result` = `/results`, `/div` and `/dividend` = `/dividends`,
`/delalert` = `/unalert`, `/j` and `/trades` = `/journal`,
`/morning` = `/premarket`, `/pf` and `/holdings` = `/portfolio`,
`/sweep` = `/heatmap`, `/scanner` = `/scan`,
`/walkforward` = `/wf`. Note `/brief SYM` is the per-stock
cockpit while bare `/brief` is the morning brief.

---

## 6. Alert conditions

Grammar: `/alert SYMBOL <condition> [timeframe] [repeat]`

Timeframes: `1m` `5m` `15m` `1h` `day` — default `5m`.
`repeat` re-arms after firing; without it an alert fires once and disarms.

### Price

| Condition | Fires when |
|---|---|
| `> 410` · `above 410` · `>410` | Price crosses above 410 |
| `< 395` · `below 395` | Price crosses below 395 |
| `vwap` | Price crosses session VWAP |
| `pdh` / `pdl` | Breaks previous day's high / low |
| `orb` | Breaks the opening range |
| `2%` | Moves ±2% on the day |

### Indicators

| Condition | Fires when |
|---|---|
| `supertrend` | Supertrend flips either way |
| `supertrend up` / `supertrend down` | Flips to that side |
| `supertrend 14` | With a custom period |
| `rsi > 70` / `rsi < 30` | RSI crosses a level |
| `rsi 50` | RSI crosses 50 in either direction |
| `rsi 45-55` | RSI enters the band |
| `ema20` | Price crosses EMA20 either way |
| `above ema20` / `below ema50` | Price crosses in that direction |
| `ema20x50` | EMA20 crosses EMA50 |
| `zone` · `zone 20 50` | Price moves between EMA zones (above both / between / below both) |
| `macd` · `macd up` · `macd down` | MACD crosses its signal line |
| `bb` · `bb upper` · `bb lower` | Bollinger band break |
| `adx 25` | ADX rises above 25 (default 25 if no number) |
| `vol 3x` | Volume ≥ 3× average (default 2×) |

### Anchored VWAP

```
/alert BEL avwap                    # cross the AVWAP anchored at the recent low
/alert BEL avwap above major        # cross above, anchored at the major swing
/alert BEL avwap high               # anchored at the recent high
/alert BEL avwap 20                 # anchored 20 bars back
```

Anchors: `low` (default) · `high` · `session` · `today` · `day` · `start` ·
`lowest` · `highest` · `major` · any bar count.

### Confluence filter

| Condition | Fires when |
|---|---|
| `conf > 60` | Multi-timeframe confluence reaches +60% |
| `conf < 50` | Falls to −50% |
| `aligned` | All timeframes agree |

### Trailing-stop exit

```
/alert BEL trail long entry=405 sl=398
/alert BEL trail short 2.5x
/alert BEL trail long entry=405 atr=14 3x
```

Parameters: `long`/`short` (default long), `Nx` ATR multiple (default 3),
`entry=`, `sl=` initial stop, `atr=` period (default 22).

Fires when a chandelier stop is hit, and `/exit SYM` reports where the stop
currently sits and how much R you're up.

---

## 7. Strategies

Seventeen, shared by the backtester and the signal scanner — so a rule you
backtest is literally the rule that alerts you.

| Key | Name | Default params |
|---|---|---|
| `sma_cross` | SMA crossover | fast 20, slow 50 |
| `ema_cross` | EMA crossover | fast 12, slow 26 |
| `price_sma` | Price vs SMA | length 50 |
| `momentum` | Momentum (rate of change) | length 126 |
| `rsi_revert` | RSI mean-reversion | length 14, low 30, high 55 |
| `macd_cross` | MACD signal cross | 12 / 26 / 9 |
| `macd_zero` | MACD zero line | 12 / 26 / 9 |
| `supertrend` | Supertrend | length 10, mult 3 |
| `st_adx` | Supertrend + ADX filter | length 10, mult 3, adx_len 14, adx 20 |
| `pullback_trend` | RSI pullback in uptrend | trend 200, length 2, entry 10, exit 60 |
| `bollinger_revert` | Bollinger reversion | length 20, mult 2 |
| `stoch_revert` | Stochastic reversion | k 14, d 3, low 20, high 80 |
| `cci_revert` | CCI reversion | length 20, low −100, high 100 |
| `donchian_breakout` | Donchian breakout | entry 20, exit 10 |
| `keltner_break` | Keltner breakout | length 20, mult 2, atr_len 10 |
| `bb_squeeze` | Bollinger squeeze breakout | length 20, mult 2, lookback 100 |
| `vol_breakout` | Volume-confirmed breakout | entry 20, exit 10, vol_mult 1.5, vol_len 20 |

The signal scanner runs six by default: `supertrend`, `ema_cross`,
`donchian_breakout`, `momentum`, `pullback_trend`, `macd_zero`. Change the set
via the `signals_config` meta key or the signals API.

---

## 8. The backtester

Long/flat on closed bars. Entries and exits fill at the **next** bar's open,
never the signal bar's close — that gap is where most published backtests
quietly cheat.

### Risk overlay (optional)

| Parameter | Effect |
|---|---|
| `sl_pct` | Fixed stop-loss % below entry |
| `tp_pct` | Fixed take-profit % above entry |
| `trail_mult` | ATR chandelier trailing stop multiple |
| `trail_period` | ATR period for the trail (default 22) |
| `max_bars` | Time stop — exit after N bars regardless |

Risk exits are evaluated intrabar in the order the market reveals prices. A bar
that *opens* through a level fills at the open, never at a price the market
skipped. A gap up through the target is a target fill even if that bar later
trades down through the stop. Only when neither gap applies is the bar's
interior checked, where stop beats target (the conservative reading — intrabar
ordering is unknowable).

After a risk exit the strategy must go flat once before re-entering, otherwise
a still-long signal would re-enter on the very next bar.

### Costs and statistics

`cost_bps` is charged on **every** entry and every exit, so 10 bps ≈ 0.2% per
round trip. Test at 15–20 to be safe.

Every trade statistic is measured **after costs**: win rate, profit factor,
average win/loss, best and worst trade. A trade that gained less than it cost
to put on counts as the loss it was. The trade log still shows gross and net
side by side.

Completed trades only. A position still open at the end of the window has no
exit price, so it appears in the log marked *open* and in a separate count, but
it doesn't vote in any statistic.

---

## 9. Web UI

`python -m app.cli serve` → <http://127.0.0.1:5000>

| Page | What's there |
|---|---|
| `/` | Kite-parity charting: candles, indicators, watchlist. Right-click to arm a price alert; drag its line to re-arm. **Event markers and saved layouts** |
| `/desk` | The intraday cockpit — levels, alerts panel, live prices |
| `/backtest` | Run any strategy on any symbol, with the risk overlay and a full field guide. **"Across watchlist" runs it on every symbol; "Parameter sweep" renders a heatmap; "Walk-forward" tests on unseen data** |
| `/portfolio` | Kite holdings annotated with valuation, signals, stop distance and results dates |
| `/scanner` | Build universes, pick conditions, save scans, run the backfill |
| `/journal` | Trade journal: open positions with live R, closed history, expectancy by tag, and live-vs-backtest per strategy |
| `/valuation` | Damodaran valuation: DCF, Graham, justified P/B, DDM, reverse DCF, scenarios, sensitivity grid |
| `/fundamentals` | Stored screener data and computed ratios |
| `/metrics` | Quality scoring |
| `/results` | Market-wide latest quarterly results |
| `/news` | Watchlist news |
| `/actions` | Corporate events and the results calendar |

The server binds to `127.0.0.1` only. If you want to reach it from another
machine, Tailscale beats exposing the port — no code changes, and the API has
no authentication of its own.

---

## 10. HTTP API

All under `http://127.0.0.1:5000`. Useful for scripting.

**Session** — `/api/session` (token validity, for the UI's login banner)

**Market data** — `/api/search?q=` · `/api/candles?symbol=&interval=&days=` ·
`/api/intraday?symbol=` · `/api/intraday/chart?symbol=` ·
`/api/confluence?symbol=`

**Watchlist** — `GET /api/watchlist` · `POST /api/watchlist/{symbol}`

**Fundamentals & valuation** — `/api/fundamentals?symbol=` ·
`POST /api/fundamentals/refresh?symbol=` · `/api/fundamentals/full?symbol=` ·
`/api/valuation?symbol=&re=&g_term=` · `/api/valuation/screen?symbols=`

**Results & events** — `/api/results?scope=&sort=` · `/api/results/card?symbol=` ·
`/api/results/latest?scope=&bucket=` · `POST /api/results/latest/refresh` ·
`/api/events?limit=&source=` · `POST /api/events/poll` · `/api/calendar` ·
`/api/calendar.xlsx` · `/api/news?hours=`

**Alerts** — `GET /api/alerts?symbol=&all=` ·
`POST /api/alerts?symbol=&cond=` (same grammar as the phone) ·
`PATCH /api/alerts/{alert_id}?price=&last_price=` (move a level) ·
`DELETE /api/alerts/{target}`

**Signals & backtest** — `/api/signals?symbols=&push=` ·
`/api/backtest/strategies` ·
`/api/backtest/batch?strategy=&symbols=&days=&sort=` ·
`/api/backtest/heatmap?symbol=&strategy=&x_param=&x_values=&y_param=&y_values=&metric=` ·
`/api/backtest/metrics` ·
`/api/backtest/walkforward?symbol=&strategy=&grid=&is_bars=&oos_bars=&mode=` ·

**Scanner** — `/api/scanner?conditions=&universe_name=&days=&limit=` ·
`/api/scanner/conditions` · `/api/universe` · `/api/universe/{name}` ·
`/api/universe/{name}/coverage` · `POST /api/universe/{name}/backfill` ·
`/api/scans` · `POST /api/scans/{name}` · `POST /api/scans/{name}/run` ·
`/api/backtest?symbol=&strategy=&interval=&days=&cost_bps=&capital=&params=`
plus `sl_pct`, `tp_pct`, `trail_mult`, `trail_period`, `max_bars`.
`params` is a JSON object, e.g. `{"fast":20,"slow":50}`.

**Chart layouts** — `GET /api/layouts` · `POST /api/layouts/{name}?spec=&is_default=` ·
`DELETE /api/layouts/{name}`

**Portfolio** — `GET /api/portfolio?annotate=&refresh=` ·
`POST /api/portfolio/sync-watchlist`

**Risk & journal** — `/api/risk/size?entry=&stop=&account=&risk_pct=&lot_size=` ·
`/api/risk/atr-stop?symbol=&side=&entry=&period=&mult=` ·
`GET /api/journal?symbol=&tag=&limit=` ·
`POST /api/journal?symbol=&entry=&qty=&stop=&side=&target=&tag=` ·
`POST /api/journal/{trade_id}/close?exit_price=&reason=&charges=` ·
`PATCH /api/journal/{trade_id}?stop=&target=&tag=` ·
`DELETE /api/journal/{trade_id}` ·
`GET /api/journal/compare?tag=&symbol=&days=&cost_bps=`

**Live** — `WS /ws/ticks?token=` streams ticks for one instrument token.

`/api/signals?push=false` (the default) is read-only and won't consume a
pending flip.

---

## 11. Configuration

### `.env`

| Key | Default | Purpose |
|---|---|---|
| `KITE_API_KEY` | — | Required |
| `KITE_API_SECRET` | — | Required |
| `NTFY_TOPIC` | — | Where alerts and replies go |
| `NTFY_CMD_TOPIC` | — | Where you send commands (must differ from above) |
| `NTFY_SERVER` | `https://ntfy.sh` | Self-hosted ntfy goes here |
| `NTFY_TOKEN` | — | ntfy access token, sent as a Bearer header |
| `NTFY_CMD_PIN` | — | Require `pin=XXXX` on write commands |
| `ACCOUNT_SIZE` | — | Trading capital that position sizing divides. Without it, `/size` can't work |
| `RISK_PCT` | `1` | % of capital risked per trade |
| `DAILY_LOSS_R` | `2` | Realised R after which the guard says stand down |
| `MAX_POSITIONS` | `5` | Concurrent open positions before the guard blocks |
| `MAX_POSITION_PCT` | `100` | Hard ceiling on one position as % of capital. 100 = cash account |
| `AUTO_FETCH_ON_ADD` | `1` | Fetch fundamentals+results when a stock is added |
| `AUTO_FETCH_NOTIFY` | `0` | Push when that background fetch finishes |
| `NEWS_CURATOR` | `none` | `none` · `ollama` · `claude` |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local LLM for news curation |
| `OLLAMA_MODEL` | `llama3.2` | |
| `ANTHROPIC_API_KEY` | — | If `NEWS_CURATOR=claude` |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | |

### Security of the command channel

The topic name is the whole credential, and ntfy.sh topics are public by
default. Since `/alert` and `/unalert` are writes, a leaked topic doesn't just
leak stock queries — it lets someone disarm the stop-loss alert you're relying
on. Three levels, in order of preference:

1. **`NTFY_TOKEN`** — real authentication. Needs a server that supports it
   (self-hosted, or ntfy.sh with a reserved topic). This is the actual fix.
2. **`NTFY_CMD_PIN`** — write commands need `pin=XXXX`. Deliberately weak:
   anyone already subscribed can read the PIN out of a command you send inside
   ntfy's retention window. It stops the screenshot case, not a listener.
3. **Neither** — the topic name alone.

The listener prints which level is in force at startup, and logs write commands
distinctly. An unexplained `WRITE` in that log means the topic has leaked.

### Meta settings (in SQLite, not `.env`)

- `signals_config` — which strategies the scanner runs, as JSON
- `signals_last_eod` — date guard so the EOD scan fires once per day
- `instruments_synced_at`, `latest_results_fetched_at` — cache timestamps

---

## 12. Data & files

```
data/
  tickerforge.db        SQLite: watchlist, alerts, events, fundamentals, signal state
  tickerforge.db-wal    WAL journal — back these up together
  tickerforge.db-shm
  candles.duckdb        OHLCV store
  kite_token.json       Today's access token
.env                    Secrets — never commit or zip this
```

SQLite runs in WAL mode with a 5-second busy timeout, so the four processes
don't lock each other out. **Back up with `VACUUM INTO` or `.backup`, not
`cp`** — a plain copy taken mid-write will be missing the WAL contents.

The candle store fills both forwards and backwards: asking for five years when
it holds one will fetch the missing history rather than silently returning what
it has. It records how far back it has already asked, so a window the exchange
has no data for (pre-listing) is requested once, not on every chart load.

---

## 13. Troubleshooting

**"No valid Kite session for today"** — run `login`. Tokens expire daily.

**Search returns nothing** — run `sync-instruments`. It's needed before any
symbol lookup works, and should be re-run after each login.

**Alerts stopped firing** — check the intraday watcher's terminal. It pushes to
your phone when the session dies or the tick stream goes quiet, but if the
process itself died there's nothing to push. `intraday alerts` confirms what's
still armed.

**A burst of old flips arrived** — expected after upgrading. Anything that
flipped while state was recorded but never reported gets found on the next
scan, each tagged with how many bars ago it fired. It drains once.

**Indicator alerts on one symbol went quiet** — if candle fetches keep failing
for it, the watcher stops evaluating rather than running indicators on frozen
bars, and pushes a notice. It resumes automatically once fetches work.

**Backtest numbers changed after an upgrade** — trade statistics are now
measured after costs and exclude the still-open position, and gap-through-target
exits are labelled correctly. Win rates will read lower than they used to. The
old numbers were wrong.

**`/size` says a trade needs 58% of my capital** — that's correct, not a bug.
Position value = risk% ÷ stop-distance%, so risking 1% with a 1.7% stop needs a
position worth 58% of the account. Tight stops need big positions. It's flagged
as concentrated but not shrunk, because shrinking it would silently reduce your
risk below what you chose. If the size is uncomfortable, use a wider stop or a
smaller risk %.

**A trade shows "unscored" in the journal** — it was logged without a stop, so
there's no 1R to measure against. It's kept in the history but excluded from
expectancy. Add one with `/amend ID sl=<price>` before closing.

**A stock has no fundamentals / no computed ratios** — the fetch failed and,
if the stock was added through the web UI, the failure went to a server
terminal rather than to you. Run `fund diagnose SYMBOL`. The most common cause
is a screener.in slug that differs from the NSE ticker; fix it with
`fund slug SYMBOL <actual-slug>` then re-fetch. Banks and NBFCs genuinely
produce fewer ratios — ROCE and Debt/Equity don't apply to lenders.

**Live-vs-backtest shows nothing** — the comparison only covers trades tagged
with a strategy key from `STRATEGIES`. Untagged trades still count in overall
stats, they just have nothing to be compared against.

**Supertrend used to draw two lines across the whole chart** — fixed. The
null points were being filtered out, which left dense series that
lightweight-charts connected straight over the gaps. It now renders as one line
that changes colour at each flip.

**A chart alert line won't drag** — only price-threshold alerts have a level to
move. RSI, Supertrend and the other indicator conditions have no single price,
so they don't render on the chart and live in the alerts panel instead.

**Alerts stopped firing after the token expired** — fixed. The watcher's main
loops used to exit on SystemExit (which `except Exception` doesn't catch), so
an expired token could kill the process rather than being retried. If you see
it on an older build, restart the watcher and update.

**`database is locked`** — shouldn't happen now, but if it does, some process is
holding a write transaction far longer than 5 seconds. Check for a stuck
`events run` or `serve`.
