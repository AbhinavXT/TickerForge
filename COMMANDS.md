# TickerForge — Command Reference

Every command, every flag, and when you'd actually reach for it.

Generated against the running argparse tree and command registry, so it matches
the code rather than my memory of it. `FIELD-GUIDE.md` explains what the
*output* means; this is about getting the output.

**Two surfaces.** The CLI (`python -m app.cli …`) is the full set. The phone
channel (slash-commands over ntfy) is a curated subset for when you're away from
the machine.

---

## Contents

- [Conventions](#conventions)
- [1. Daily ritual](#1-daily-ritual)
- [2. Running the system](#2-running-the-system)
- [3. Watchlist and data](#3-watchlist-and-data)
- [4. Research](#4-research)
- [5. Backtesting](#5-backtesting)
- [6. Trading](#6-trading)
- [7. Configuration](#7-configuration)
- [8. Phone commands](#8-phone-commands)
- [Reference tables](#reference-tables)

---

## Conventions

```
python -m app.cli <command> [subcommand] [args] [--flags]
```

A shell alias saves a lot of typing:

```bash
alias tf='python -m app.cli'
```

Everything below writes `tf` for brevity.

**Flags that recur** across the research and backtest commands:

| Flag | Default | Means |
|---|---|---|
| `--days N` | varies | How much history to load |
| `--interval X` | `day` | Candle size — see [intervals](#intervals) |
| `--cost-bps N` | `10` | Assumed cost **per leg**, so 10 ≈ 0.2% round trip |
| `--dry` | off | Report what would happen, change nothing |

Anything that takes `--symbols` accepts a space-separated list. Symbols are
case-insensitive.

---

## 1. Daily ritual

Kite tokens die every morning. This is the whole of it:

```bash
tf login                    # opens the browser, completes in one click
tf status                   # session, index freshness, watchlist size
```

| Command | Args | Notes |
|---|---|---|
| `login` | — | Interactive. Needed once a day, every day. |
| `status` | — | The first thing to run when something looks wrong. |
| `sync-instruments` | — | ~90k rows. Re-run nightly; `maintain` does it for you. |
| `notify-test` | `[message]` | Confirms the phone actually buzzes. |

---

## 2. Running the system

### Everything at once

```bash
tf up                       # all four services under one supervisor
tf health                   # are they actually alive?
```

`up` runs the web UI, the intraday alert engine, the events poller and the
phone-command listener under one parent. It restarts what dies with
exponential backoff, gives up after five failures, and pushes to your phone
when either happens. Output from all four is tagged into one terminal.

**Only one supervisor may run at a time.** A second one refuses to start and
tells you the pid of the first. This matters more than it sounds: two
supervisors means two watchers double-firing every alert and two remote
listeners each answering the same phone command. If you loaded the launchd
agent, `tf up` in a terminal is that second one — unload the agent first:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.tickerforge.up.plist
```

### Individually

| Command | Flags | Runs |
|---|---|---|
| `serve` | — | Web UI on `http://127.0.0.1:5000` |
| `intraday run` | `--every N` `--ticks` `--always` | Alert engine. `--ticks` uses the websocket instead of polling; `--always` ignores market hours. |
| `events run` | `--every N` `--always` `--digest-times` `--brief-at` | Corporate actions, scheduled digests, the 08:45 brief |
| `news run` | `--times HH:MM,…` | News digests at fixed times |
| `remote` | — | Listens for phone commands |

Only `serve` fails loudly. If the alert engine dies unsupervised, alerts
silently stop firing — which is indistinguishable from nothing triggering.
That asymmetry is why `up` exists.

### At boot, and nightly

```bash
tf install-service          # writes launchd agents, prints the launchctl lines
tf install-service --hour 4 # nightly job at 04:00 instead of 03:00
tf maintain --dry           # see what the nightly job would do
tf maintain                 # back up, top up candles, prune old backups
```

| `maintain` flag | Effect |
|---|---|
| `--dry` | Report only |
| `--keep N` | Days of dated backups to keep |
| `--no-candles` | Skip the top-up — no Kite session needed |
| `--quiet` | Don't push the report |

`install-service` deliberately doesn't register anything itself; it hands you
the `launchctl load` commands to run. `--dir` writes the plists elsewhere.

---

## 3. Watchlist and data

```bash
tf search reliance                    # find the exact tradingsymbol
tf watch add BEL
tf watch list
tf watch remove BEL
tf watch fetch-missing                # fundamentals for anything lacking them
```

`search` takes `--all` to include every exchange rather than NSE equity only.
`watch add` takes `--exchange`.

### Screening the whole market

```bash
tf universe from-market market          # every ordinary NSE equity
tf universe backfill market --days 500  # overnight — one request per symbol
tf universe prune market                # drop what you couldn't trade anyway
tf query "roe > 18 and debt_equity < 0.5 and close > sma(200)" \
     --universe market --fetch-missing
```

**`from-market`** takes every `EQ` instrument minus the things that aren't
companies: rights issues, the trade-to-trade and SME series, ETFs and index
funds. Each of those would otherwise cost a rate-limited request at backfill
and can never be the answer to "find me an undervalued stock".

**`backfill`** is the slow part — roughly one request per symbol, so a couple
of thousand names is around fifteen minutes. Resumable: interrupting loses
nothing.

**`prune`** runs *after* the backfill, because liquidity is a fact about
prices and the instrument index has none. Defaults drop anything under ₹20,
under 50,000 average daily shares, or with fewer than 200 bars of history.
Without it a whole-market screen surfaces micro-caps trading four thousand
shares a day — technically hits, and completely unactionable.

| `prune` flag | Default |
|---|---|
| `--min-price` | 20 |
| `--min-volume` | 50,000 |
| `--min-bars` | 200 |
| `--dry` | report only |

**Fundamentals are fetched in two stages.** A query like `close > sma(200) and
roe > 18` runs the price half over every symbol from stored candles, then
loads fundamentals only for the survivors. That's the difference between two
thousand requests to screener.in and eighty. `--fetch-missing` pulls
statements for survivors that have none.

The result says where the funnel narrowed — a price filter leaving three
candidates is a different problem from a valuation rejecting nine hundred.

### Universes — what the scanner looks *through*

The watchlist is what you follow; a universe is what you search. Scanning your
watchlist is scanning the answer you already have.

```bash
tf universe from-watchlist mylist     # seed one from the watchlist
tf universe set nifty200 RELIANCE TCS INFY …
tf universe set nifty200 NEWNAME --add     # append instead of replace
tf universe list                      # names and sizes
tf universe show nifty200
tf universe coverage nifty200         # how many members have candles yet
tf universe backfill nifty200 --days 400   # the overnight job
tf universe backfill --all
```

Backfill is one rate-limited request per symbol, so 200 names is a while.
It's resumable — interrupting loses nothing and re-running only fetches what's
missing.

### Fundamentals and results

```bash
tf fund fetch BEL
tf fund fetch BEL --all               # every statement, not just the summary
tf fund show BEL
tf fund diagnose BEL                  # why did this symbol get no ratios?
tf fund slug BEL some-screener-slug   # override the screener.in URL

tf results fetch-latest               # scrape the market-wide results feed
tf results latest --limit 40 --sort np_yoy --scope large
```

`fund diagnose` is the one to reach for when a valuation comes back empty —
usually a slug mismatch.

---

## 4. Research

```bash
tf signals                            # strategy stances across the watchlist
tf signals BEL TCS                    # just these
tf signals --push                     # and send it to the phone
```

### Relative strength

```bash
tf rs                                 # rank the watchlist against Nifty
tf rs --universe nifty200 --top 25
tf rs --symbols BEL TCS INFY
```

Reads stored candles only — backfill first, then it's instant.

### Value with momentum

```bash
tf screen
tf screen --universe nifty200 --min-rs 80
tf screen --verdicts undervalued --any-signal
```

| Flag | Default | Means |
|---|---|---|
| `--min-rs N` | `70` | Minimum RS percentile |
| `--verdicts a,b` | `undervalued,fair` | Which valuation verdicts qualify |
| `--any-signal` | off | Drop the requirement that a strategy be long |

### Query — arbitrary conditions

The scanner answers fixed questions. This answers the ones nobody thought of
in advance, which in practice is most of them.

```bash
tf query "rsi(14) > 70 and adx(14) > 25"
tf query "ema(20) crosses_above ema(50)" --universe nifty200
tf query "close > highest(252) and volume > 2 * avg_volume(20)"
tf query "rs > 70 and mos_pct > 20" --top 40
tf query "close > sma(200) and rsi(2) < 10" --why 5
tf query --fields                     # every field, with its defaults
tf query --examples
```

| Flag | Means |
|---|---|
| `--universe X` | Search a universe instead of the watchlist |
| `--days N` | History to load (default 420) |
| `--top N` | How many hits to print |
| `--why N` | Show why N *non-matching* symbols failed, clause by clause |
| `--backtest SYM` | Run the query as a strategy on one symbol |
| `--save NAME` | Store it. Add `--alert` to push on new entries |
| `--saved` · `--forget NAME` | List / delete |
| `--check-alerts` | Run every watched query, push what just entered |

`--why` is the one to remember. An empty result is a dead end; `--why` turns it
into a specific number to argue with.

**History operators** — these look backwards from the bar being evaluated:

| | |
|---|---|
| `within(n, cond)` | true on any of the last n bars |
| `bars_since(cond)` | how long since it last held |
| `streak(cond)` | consecutive bars it has held |
| `ago(expr, n)` | the value n bars back |
| `max/min/avg/sum(expr, n)` | over the last n bars |

```bash
tf query "within(5, rsi(14) crosses_above 70)"
tf query "bars_since(close > sma(200)) > 60"
tf query "volume > max(volume, 60)"
```

**Against its own industry.**

| | |
|---|---|
| `industry_pe` `sector_pe` | median P/E of this company's industry / sector |
| `sector` `industry` | the names themselves — compare with `==`, `!=`, `contains` |

```bash
tf query "pe < industry_pe * 0.8 and roe > 15"
tf query 'industry contains "bank" and pb < 1.5 and roe > 14'
```

The P/E is **computed here**, as the median of the P/Es already stored, rather
than scraped as a number. It refreshes whenever the fundamentals do and there's
no second figure to reconcile with the first.

Median, not mean: across the 628 P/Es currently stored the mean is 1.9× the
median, because a handful of barely-profitable companies sit at several hundred
times earnings. One of those should not decide what its industry is worth.
Non-positive P/Es are dropped — a company with no earnings has no P/E, and a
negative one is not a cheap one.

**The population is everything stored**, never what a query happens to scan.
An industry P/E that shifted with your universe would be a different number
under the same name in two screens.

`sector` and `industry` are the first name-valued fields, so they compare only
with `==`, `!=` and `contains`, and only against a quoted value — `sector > 5`
is refused rather than coerced. Matching ignores case, and `contains` is
usually what you want, since screener's industries read like *Consumer
Durables - Domestic Appliances*. An unclassified company matches nothing,
including under `!=`: unknown reads as no, as everywhere else here.

**These need a re-fetch**, like shareholding — the classification comes from
the peers block, which wasn't previously read. Until then `industry_pe` reads
blank rather than grouping everything under one nameless industry.

**Ranges.** Comparisons chain, so a band reads as a band:

```bash
tf query "20 < rsi(14) < 30 and close > sma(200)"
tf query "sma(50) * 0.95 < close < sma(50) * 1.05"
```

It expands to the conjunction it means — `20 < rsi(14) and rsi(14) < 30` — so
`--why` tells you which end you missed. The middle term is evaluated once.
Both operators have to face the same way: `20 < x > 30` looks like a band, is
satisfied by anything above 30, and is refused rather than guessed at. Only
`<` `<=` `>` `>=` chain.

**Shareholding.** Who owns the company, per quarter:

| | |
|---|---|
| `promoter_holding` `fii_holding` `dii_holding` | promoter, foreign and domestic institutional % |
| `public_holding` `govt_holding` `shareholders` | public %, government %, number of shareholders |

```bash
tf query 'q("promoters") < q("promoters", 2) and fii_holding > 10'
tf query "promoter_holding > 50 and debt_equity < 0.3"
```

Screener's "change in promoter holding" falls out of the period index rather
than needing a field of its own — that's what the index is for.

**These need a re-fetch.** The shareholding section wasn't in the list this
codebase scraped, so nothing is stored for it yet. Run `tf fundamentals fetch
--all` (about 33 minutes at the current delay) before the fields return
anything. Until then they read blank rather than inventing a holding, and
`tf fundamentals diagnose SYM` will say the section is missing because it was
added since that symbol was stored.

**Oscillators, volume and bar-relative figures.**

| | |
|---|---|
| `stoch(n)` `stoch_d(n)` | stochastic %K and its smoothed %D, 0–100 |
| `willr(n)` | Williams %R, −100 to 0 |
| `cci(n)` `mfi(n)` `cmf(n)` | Commodity Channel Index, Money Flow Index, Chaikin Money Flow |
| `obv` | on-balance volume, cumulative |
| `vwap(n)` | volume-weighted average price over N bars |
| `stddev(n)` | standard deviation of closes |
| `keltner_upper(n, m)` `keltner_lower(n, m)` | EMA ± m × ATR |
| `prev_close` `gap_pct` | yesterday's close; today's open against it |
| `dist_from_high(n)` `dist_from_low(n)` | % below the N-bar high / above the low |

```bash
tf query "stoch(14) < 20 and close > sma(200) and cmf(20) > 0"
tf query "dist_from_high(252) < 5 and volume > 2 * avg_volume(20)"
tf query "close > vwap(20) and obv > ago(obv, 20)"
```

**Window convention.** `highest(n)` and `lowest(n)` **exclude** today, so
"broke above the 252-day high" isn't trivially true of every bar that makes
one. The oscillators and `dist_from_high` **include** it: a stock at a new high
is 0% below its high, and reporting yesterday's would say otherwise on exactly
the day you care.

`stddev` is population, matching `bb_upper`/`bb_lower` — so
`sma(20) + 2 * stddev(20)` equals `bb_upper(20, 2)` rather than being a few
percent off for no findable reason. `vwap(n)` on a window with no volume reads
blank rather than falling back to an average, since that would return a number
that looks like a VWAP and is an SMA.

All of these are price fields, so they backtest.

**From the statements.** Every row screener publishes is queryable, at any
period.

| | |
|---|---|
| `fin("row")` / `fin("row", n)` | annual. 0 is the last completed year, TTM excluded |
| `ttm("row")` | the trailing-twelve-month column |
| `q("row")` / `q("row", n)` | quarterly, 0 is the latest |
| `growth("row", years)` | annual CAGR % |
| `yoy("row")` | latest quarter against four back, % |

```bash
tf query --statements                 # every row you can name, with shorthands
tf query 'growth("sales", 3) > 15 and fcf > 0 and debtor_days < 60'
tf query 'q("net profit") > q("net profit", 4)'
tf query 'ttm("sales") > fin("sales") * 1.1'
```

**Period 0 is the latest.** Storage ascends — index 0 in the database is Mar
2015 — but the language counts backwards, because an unindexed query reading
the oldest column would return a perfectly plausible screen built on
eleven-year-old figures.

**`fin()` excludes TTM.** The annual tables carry a trailing TTM column.
Counting it as a year makes `fin("sales", 1)` a TTM-versus-fiscal-year
comparison and makes three-year growth span something other than three years.
Use `ttm()` when you want it.

About thirty rows have shorthands — `sales`, `eps`, `fcf`, `cfo`, `reserves`,
`borrowings`, `debtor_days`, `inventory_days`, `working_capital_days`,
`net_profit`, `operating_profit`, `pbt`, `tax_pct`, `dividend_payout` and the
rest. `tf query --statements` lists them beside the row they map to. They're
resolved through the same lookup as the accessors, so there's one set of
behaviours rather than two.

Labels are matched exactly first, then by prefix, then by substring — so
`fin("sales")` finds `Sales` rather than being beaten to it by a longer row.
`sales` and `revenue` are treated as the same line, because screener labels it
one way for a manufacturer and the other for a bank, and without that a query
would quietly exclude every financial in the universe.

A label matching nothing fails **before** the scan with a suggestion, rather
than per symbol — per symbol it looks identical to a company that never filed.

These count as fundamentals: second-stage loading, and **not backtestable**,
for the same reason as `roe`.

**Against everyone else.** `percentile(EXPR)` and `rank(EXPR)` compare this
stock with the rest of the field.

```bash
tf query "percentile(change_pct(63)) > 80 and close > sma(200)"
tf query "rank(close / sma(200)) <= 25"
tf query "percentile(change_pct(20) - index(change_pct(20))) > 70"
```

`percentile` is 0–100, higher is larger, ties at the midpoint — the same
convention and the same code `tf rs` uses, so RS 85 on the ranking page and
`percentile(...) > 85` here mean the same thing. `rank` counts from 1 for the
largest; ties share the better position and consume the places behind them
(1, 1, 3).

**The population is everything scanned**, not what survived the other clauses.
Otherwise `percentile(x) > 80` would mean "top fifth of the universe" in one
query and "top fifth of the stocks already above their 200-DMA" in another —
a much weaker screen, identical syntax, nothing in the output to tell them
apart. The note reports the population size on every run. A symbol without
enough history is left out of it rather than counted as zero.

The cost is a second read of the stored candles: the field has to be complete
before any symbol can be judged. That's a local DuckDB read, and only queries
that actually use these operators pay it.

Not available: `percentile(roe)` and friends — the population is the whole
universe, so ranking on a fundamental means a statements fetch per company
across all of it. Filter on `roe` instead of ranking by it. These also can't
nest, and **can't be backtested**: measuring against today's universe and
applying that to a 2023 bar assumes you knew this year's cross-section at the
time, which would flatter every momentum system ever tested.

**Against the index.** `index(EXPR)` evaluates EXPR on the benchmark's own
bars and aligns the result to this symbol's dates.

```bash
tf query "index(close) > index(sma(200))"          # is the tape risk-on?
tf query "close / index(close) > ago(close / index(close), 60)"
tf query "change_pct(63) > index(change_pct(63)) and close > sma(50)"
tf query 'index("NIFTY BANK", close) > index("NIFTY BANK", sma(50))'
```

Defaults to `NIFTY 50` — the same benchmark `tf rs` ranks against. Name another
in quotes as the first argument.

`index(sma(200))` is the *index's* 200-day average, computed from index bars.
It's a wrapper, like `weekly()`, not a field.

Alignment is by **date**, and always to the last index bar at or before the
stock's bar — never a later one. Two daily series are not the same series:
suspensions, late listings and holidays one of them observed all mean bar 200
of a stock isn't bar 200 of the index. Aligning by position produces a
plausible number for every bar and is wrong the moment either side misses a
day, in the direction that flatters the result. A stock bar earlier than the
benchmark's first bar reads blank rather than borrowing the earliest value.

Unlike `roe` and `mos_pct`, **`index()` can be backtested.** Today's balance
sheet applied to a 2023 bar asserts you knew it at the time; an index close on
a 2023 bar simply *is* what it was on that bar. So the regime-gated test works:

```bash
tf query "close > sma(50) and index(close) > index(sma(200))" --backtest INFY
```

Not available: `index(roe)` and friends — an index has no balance sheet.
`index()` can't nest, and a timeframe goes *inside* it: `index(weekly(close))`,
not `weekly(index(close))`.

**Ranking, trimming and columns** — three clauses that go after the condition:

| | |
|---|---|
| `sort by EXPR [asc\|desc]` | rank the results. Descending unless you say `asc` |
| `top N` | keep the best N — applied **after** the sort |
| `show EXPR, EXPR` | extra columns in the table |

`order by`, `limit` and `select` are accepted as spellings of the same three.

```bash
tf query "close > sma(200) sort by change_pct(20) top 20"
tf query "rs > 60 sort by pe asc top 15 show roe, debt_equity"
tf query "close > highest(252) show atr_pct(14), rs"
```

They live in the language rather than as flags because a saved query and a
phone message are each *one string* — `/query rs > 60 sort by pe asc top 10`
works from the phone, and a saved screen remembers how it wanted to be ranked.

`top` is applied after the sort, never before: truncating first hands you
twenty names that look exactly like the right twenty and aren't. A result that
gets trimmed *without* a sort says so in its note, because ordering by
whatever the universe file happened to list first is not a ranking.

Sort direction defaults to descending, which is right for `rs` and wrong for
`pe` — so the note always states which way it went rather than leaving you to
infer it from the rows. Symbols with no value for the sort key go last in
both directions: absent isn't zero and it isn't infinity.

`sort by` and `show` are allowed to name fundamentals even when the condition
is price-only. Doing so loads them for everything that passed the price
filter, exactly as a fundamental condition would.

**Backtest a query.** Long while it holds, flat while it doesn't, through the
same simulator the built-in strategies use — same costs, same fill rules, same
metrics:

```bash
tf query "close > sma(50) and rsi(14) > 45" --backtest BEL
```

Fundamentals are refused here: `roe` is today's figure, and applying it to a
bar from two years ago assumes you knew it at the time.

**Watch a query.** Saved queries can push when a symbol *newly* qualifies:

```bash
tf query "close > highest(252) and rs > 70" --save breakouts --alert
tf query --check-alerts        # nightly; the events service can run this
```

Only entries are pushed, never the whole list — and never on the first run,
when everything would look new.

**Operators:** `> < >= <= == !=`, `and` `or` `not`, brackets, `+ - * /`, and
`crosses_above` / `crosses_below`. Comparisons read the **last closed bar**;
crosses read the last two, so they mean "crossed on the most recent bar"
rather than "has ever crossed".

A symbol without enough history to evaluate a clause counts as **not
matching**, never as probably matching.

Also at `/query` in the browser, with live syntax checking and a clickable
field list.

### EOD scans

```bash
tf scan 52w_high volume_surge --universe nifty200
tf scan donchian_break --universe nifty200 --limit 40
tf scan bb_squeeze --save squeeze-watch      # remember this scan
tf scan --saved                              # list saved scans
```

Conditions combine with AND — a symbol must satisfy all of them. See
[conditions](#scan-conditions).

---

## 5. Backtesting

The honest order is single run → breadth → parameter shape → out-of-sample →
drawdown distribution. Each step is cheaper than the next and rejects most
candidates.

```bash
# breadth: does the edge exist beyond one lucky symbol?
tf batch st_adx --days 730 --sort expectancy_pct --limit 30
tf batch st_adx BEL TCS INFY

# parameter shape: plateau or spike?
tf heatmap BEL sma_cross fast slow --metric sharpe

# out-of-sample: does it survive data the optimiser never saw?
tf walkforward BEL sma_cross fast=5:25:5 slow=30:90:10 \
    --is-bars 250 --oos-bars 60 --mode rolling --metric sharpe

# drawdown distribution: how bad could that have been?
tf mc BEL st_adx --sims 2000 --mode bootstrap --seed 1
```

| Command | Positional | Key flags |
|---|---|---|
| `batch` | `<strategy> [symbols…]` | `--sort` `--limit` `--days` `--interval` `--cost-bps` |
| `heatmap` | `<symbol> <strategy> <x> [y]` | `--metric` `--days` `--interval` `--cost-bps` |
| `walkforward` | `<symbol> <strategy> [grid…]` | `--is-bars` `--oos-bars` `--mode` `--metric` `--days` |
| `montecarlo` (`mc`) | `<symbol> <strategy>` | `--sims` `--mode` `--seed` `--days` `--interval` |

**Grid syntax** for `walkforward` and `heatmap`: `fast=5:25:5` is
start:stop:step, or `slow=30,50,70` for an explicit list.

**Monte Carlo modes:** `shuffle` reorders your actual trades and answers *was
my drawdown lucky sequencing?* `bootstrap` resamples with replacement and
answers *what if the next hundred trades resemble these but aren't these?*
Bootstrap is the more honest forecast and the more pessimistic one.

Position sizing, the regime filter and CSV export live on the **Backtest
page** in the browser rather than the CLI.

---

## 6. Trading

### Position sizing

```bash
tf size --entry 405 --sl 398
tf size BEL --entry 405 --sl 398 --risk 1 --account 500000 --lot 1
```

Falls back to `ACCOUNT_SIZE` and `RISK_PCT` from config. Warns when a tight
stop concentrates too much capital — the maths can be right and the position
still unwise.

### Journal

```bash
tf journal open BEL --entry 405 --sl 398 --qty 71 --tag st_adx --note "breakout"
tf journal list                       # open positions + recent closed
tf journal list BEL
tf journal close 12 --exit 419 --reason target --charges 84
tf journal amend 12 --sl 405          # trail a stop
tf journal amend 12 --tag donchian    # fix a label
tf journal stats
tf journal stats --tag st_adx
```

Omit `--qty` on `open` and it's sized from your risk model. **Tag
consistently** — every later comparison joins on the tag.

### Portfolio

```bash
tf portfolio show                     # holdings + valuation, signals, stop distance
tf portfolio sync-watchlist           # add holdings to the watchlist
```

**Mutual funds** are included, on `/portfolio` and in `/pf` from the phone —
units, average cost, latest NAV with the date it was struck, value and P&L.

They are deliberately **kept out of the equity totals**. Folding fund value
into `total_value` would move every holding's `weight_pct` and quietly defuse
the concentration flag: a 30% position stops looking like one the moment an
index fund joins the denominator. Funds get their own total, and
`totals.net_worth` gives the combined figure when you want it.

They also carry **none of the annotations** — no chandelier stop, no signal
stance, no results date. There's no instrument token, so there are no candles,
and a stop level on a mutual fund would be a number with nothing behind it. For
the same reason `sync-watchlist` never adds them; an ISIN on the watchlist
breaks every scan that iterates it.

**If nothing shows up:** Kite Connect's mutual-fund API is a separate
subscription from the equity one, and without it the call returns a permission
error. The page says so in one line and renders everything else normally —
that's a billing fact, not a broken setup. Other errors are reported as
themselves, so a network timeout doesn't send you to the Kite console.

### Mutual funds

```bash
tf mf sips                            # instructed SIPs, soonest debit first
tf mf schemes                         # imported fund portfolios
tf mf import july.csv --scheme "Flexi Cap" --as-of 2026-07
tf mf overlap "Flexi Cap" "Index Fund"
tf mf lookthrough                     # funds + direct holdings, per company
```

SIPs come from Kite. **Composition does not** — and this is worth knowing
before you go looking for a setting. Kite Connect's mutual-fund API is
`mf_holdings`, `mf_orders`, `mf_sips` and `mf_instruments`; the last is scheme
*metadata* (name, ISIN, NAV, minimum purchase), not a portfolio. No endpoint
anywhere returns the stocks a fund holds.

What does exist is SEBI's monthly portfolio disclosure: every AMC publishes a
spreadsheet of its schemes' holdings on its own site, in its own layout. There
is no single feed. So `mf import` reads a local file, and the workflow is one
download per fund per month rather than an integration that would quietly rot.

The parser is deliberately tolerant — the header is rarely on line 1, column
names differ at every fund house, and subtotals, cash and section headings are
dropped rather than imported as very large holdings.

**Overlap** is the sum of the *smaller* of each shared company's two weights:
the portion of each fund you're buying twice. Funds are joined on **ISIN**,
because "HDFC Bank Limited" and "HDFC Bank Ltd" are one company and joining on
name reports zero overlap between two funds that hold the same book. A fund
with no imported portfolio reads as **missing**, never as 0% — those look
identical in a table and mean opposite things.

**Look-through** answers the question a fund investor otherwise can't: a 4%
direct holding that is also 8% of a fund worth as much again is a 12%
position, and nothing else on this platform would have flagged it. Funds
without an imported portfolio are reported as blind spots with their value, so
you can see how much of the total it can't see.

Symbol matching refuses to guess: an ambiguous company name resolves to
nothing rather than to a plausible wrong symbol. Unmatched rows still count
for overlap (that joins on ISIN) but are skipped by look-through.

**SIP dates** also appear in the pre-market brief when a debit is due within
five days — the one mutual-fund fact with a date attached, and a debit you'd
forgotten is one that lands the morning you were going to deploy the same cash.

### Live vs backtested

```bash
tf journal compare rsi_revert
tf journal compare rsi_revert --stop-pct 4 --days 1095
/compare rsi_revert                     # from the phone
```

The paper-tracking loop the journal was written for, and the reason to run a
paper account at all: not whether it made money, but whether the edge the
backtest promised survived contact with a real sequence of days.

**Both columns are computed by the same function.** The backtester reports
percentages per trade and the journal reports R multiples, which is why these
two tables were never actually comparable. The simulator's own trade list is
fed back through `journal.stats()`, so there is one definition of expectancy
rather than two that agree today.

That requires the backtest to run with a stop model — R is only defined against
one — so it uses `PAPER_STOP_PCT` unless you pass `--stop-pct`.

**Same symbols, both sides.** Backtesting on fifty names and comparing that to
eight weeks of paper on eight would measure universe selection, not the edge.

```
rsi_revert — live vs backtested
  1 symbol(s), 5% stop

                        live    backtest
  trades                  12          16
  win rate %              42          50
  expectancy R         +0.23       +0.66
  profit factor         1.40        2.31
  total R               +2.8       +10.5

  12 trades is too few to tell a real gap from noise — the error bar is ±1.71R
```

That last line is the point. A 0.43R gap looks alarming and is nothing at this
sample size. Below 30 trades it always says so; above it, it reports whether
the backtested figure sits within two standard errors of the live one, and
names the direction when it doesn't. Live running *above* the backtest gets
flagged too — that also needs explaining before you trust it.

### Paper broker

```bash
tf paper queue                        # after the close: today's flips → orders
tf paper queue --stop-pct 4
tf paper fill                         # next morning: fill at the open
tf paper status
tf paper orders                       # the order log
tf paper reset --confirm              # wipe paper history, spare real trades
```

Orders fill at the **next open**, never the close that produced the signal.
Run `queue` after 15:35 and `fill` the following morning once candles land.

#### From the phone

```
/paper                 open, pending, expectancy so far
/paper fill            fill last night's queue at today's open
/paper queue sl=4      queue today's flips with a 4% stop
/paper orders          the last 15 orders
```

`/paper fill` tops the candles up before filling. `fill_pending` reads the
**stored** bars and never fetches, so without that step every order reports as
pending — which reads exactly like a morning with nothing to do.

`fill` and `queue` are writes, so `NTFY_CMD_PIN` guards them. Bare `/paper` is
a status check and needs no PIN.

#### Self-executing

Set `PAPER_AUTO=1` (Settings → Paper) and the account trades on its own:

| | |
|---|---|
| **15:40** | queues tomorrow's orders from today's flips |
| **09:20** | fills them at the open |

| setting | default |
|---|---|
| `PAPER_AUTO` | `0` — off |
| `PAPER_STRATEGIES` | `signals` — follow the signal scan. Or name keys: `rsi_revert,sma_cross` |
| `PAPER_STOP_PCT` | `5` — initial stop below entry |
| `PAPER_MAX_POSITIONS` | `8` |
| `PAPER_TRAIL_MULT` | `0` — off. Trail at highest-high minus N x ATR |
| `PAPER_TRAIL_PERIOD` | `22` — matches the backtester |
| `PAPER_REGIME_GATE` | `0` — skip entries when the index is below its 200-day |

**Trailing stops.** With `PAPER_TRAIL_MULT` at 0 a winner gives back every
rupee of open profit waiting for the strategy to flip, so trend systems look
far worse on paper than they are — one-directionally, which is how you'd
conclude an edge wasn't there when it was. Set it to whatever multiple your
backtests use; the ATR period defaults to the backtester's 22 for the same
reason.

The trail is a ratchet: highest-high-since-entry minus N x ATR, never below
the entry stop and never giving ground. **The stored stop is not rewritten** —
R is measured from the stop the position was sized against, and moving it as
the trail rises would rescale every trade's R as it ran, making expectancy
depend on how long you held.

**The daily loss guard now blocks new entries.** It existed and pushed a
stand-down notice; the autopilot ignored it and kept queueing, which made the
guard advice rather than a limit. Exits are never gated by either guard — a
limit that stops you closing a losing position isn't risk control, it's a trap.

**The regime gate** skips entries when the index sits below its own 200-day.
A benchmark that can't be read reads as *unknown*, not as a bear market: a
missing index must not silently stop the account trading for a week.

`rsi_revert` and `sma_cross` are the two you asked about; `tf backtest --list`
has the other fifteen.

**Stops are now enforced.** They weren't. The stop was recorded when a trade
opened, used to size it, and then ignored for ever — exits arrived only when a
strategy flipped, so a position that fell 40% stayed open and every expectancy
figure the journal produced was flattered by losers that would have been cut in
reality. For an account whose whole purpose is evidence about going live, that
was the wrong answer rather than a rounding error.

A position that trades through its stop closes at the stop. One that **gaps
through** closes at the open, because if the day opened at 88 you did not get
95 — filling at the stop regardless is the commonest way a simulator invents
money, and the backtester already refuses to. Only settled bars are used;
today's forming bar would stop you out or not depending on what minute the
watcher happened to run.

**Guardrails apply to play money.** Position limit, one position per symbol,
and exits processed before entries — an exit frees a slot, and handling entries
first turns a rotation into a rejection, so the account slowly fills with
whatever signalled earliest. These numbers are the evidence for going live, and
evidence gathered without limits says nothing about a system that will have
them.

**Leaving `PAPER_STRATEGIES` as `signals` is worth defending.** `from_signals`
reads the same scan the EOD push reads, so the trades taken and the alerts you
receive can't disagree about what fired. Naming your own list breaks that on
purpose — reasonable if you want the paper account testing mean-reversion while
your alerts stay on trend rules — and `/paper` says so rather than leaving you
to notice.

It stays off until you turn it on. A paper account that starts trading because
you updated the code is a surprise, even with play money.

#### It also runs itself

The watcher fills at **09:20** on trading days, once, and pushes what filled.
The two halves of paper trading land at the two times you're least likely to be
at the machine — the queue is built after the close, the fill needs a bar that
only exists after the open — and a paper experiment you have to be home for is
one that quietly stops after a fortnight.

Why 09:20 and not 09:15: `_next_open` reads stored candles, and a bar asked for
at 09:15:00 is a coin flip on whether the first one exists yet. Only the
`open` field is read, which is final from the first trade, so a still-forming
daily bar is safe here — it's the one field on it that can't change.

Attempted once per day whether or not it succeeds, so a dead session at 09:20
doesn't become a retry storm. Anything left pending is picked up by the next
run: orders persist and `fill_pending` is safe to call repeatedly.

### Semi-automatic execution

Requires `LIVE_TRADING=1` **and** `SEMIAUTO=1` in `.env`. Off by default.

```bash
tf semiauto status                    # armed? what's awaiting approval?
tf semiauto propose BEL --qty 71 --price 405 --stop 398 --tag st_adx
tf semiauto approve <token>
tf semiauto reject <token>
```

Tokens are single-use and expire in 15 minutes. The guardrails re-run at
approval, not just at proposal.

### Pre-market brief

```bash
tf brief                              # compose and push
tf brief --dry                        # print without sending
tf brief --skip news,gaps             # omit sections
```

---

## 7. Configuration

```bash
tf settings                           # everything, with its source
tf settings RISK_PCT                  # one value + where it came from
tf settings RISK_PCT 0.5              # set an override
tf settings RISK_PCT clear            # revert to .env
```

A `*` marks values overridden here, a blank marks `.env`, a `·` marks the
built-in default. Same data as the **Settings page** in the browser.

---

### Quieting the notifications

Two settings, on the Settings page or in `.env`:

| | |
|---|---|
| `EVENT_PUSH_CATEGORIES` | which events buzz. Default `RESULTS,CORP ACTION,DIVIDEND`. Also available: `CONCALL`. `all` for everything |
| `EVENT_PUSH_HORIZON_DAYS` | how far ahead a dated event may be and still buzz. Default `0` — today only |

**Where the volume came from.** Market-wide events were already curated down
to four categories. Watchlist events were not — they bypassed the classifier
entirely and pushed at high priority, so holding a stock subscribed you to its
whole paperwork trail: ESOP allotments, trading-window notices, newspaper
publications, duplicate share certificates. Both tiers now use the same gate.

Measured against the 2,000 events currently stored: **2,000 pushes → 94.**

**Nothing is lost.** A board meeting three weeks out no longer buzzes, but it
is still stored, and the morning brief has a *Due now* section listing results
and corporate actions dated today or tomorrow. One notification a day carrying
the calendar, rather than one per filing carrying paperwork.

**Dividends gate on the record date, not the filing.** A dividend arrives on
the announcements feed as *"…has informed the Exchange that Record date for the
purpose of Dividend is 18-Aug-2026"*, filed on 31 July. Gating on the filing
timestamp buzzes you eighteen days early and then says nothing on the day you'd
act. The ex-date or record date is read out of the prose and used instead —
43 of the 83 stored dividend announcements move, by up to 19 days.

Under T+1 the ex-date and record date fall on the same day, so a **record date
tomorrow means today is the last day to buy**. The brief says exactly that, and
says "too late to buy for this one" when the date is today — "tomorrow" on its
own reads like there's still time.

The extractor only fires on ex-date / record-date / book-closure phrasing, so
"quarter ended 30-Jun-2026" in a results filing isn't mistaken for an event
date. Across the 1,336 stored results filings it re-dates none of them, and
there's a test asserting that against the real store.

A dividend *declaration* with no record date stated ("Board declared an interim
dividend of Rs 5") keeps its filing date, because the declaration is the event
and it happened today. The record date arrives later as its own filing.

**Undated events still push** if their category is allowed. Announcements are
filed as they happen, so an undated one is almost always today's — and dropping
it would lose a results filing whose date field came through malformed, which
is the most important message this system sends. Undated is not a way round
the *category* filter, only the date one.

To widen it again, set `EVENT_PUSH_HORIZON_DAYS=3` for a few days' warning, or
`EVENT_PUSH_CATEGORIES=all` to go back to everything.

## 8. Phone commands

Sent to `NTFY_CMD_TOPIC`. The `remote` service must be running.

### Read commands — no PIN

| Command | Aliases | Does |
|---|---|---|
| `/price SYM` | `/quote` | Last price and day change |
| `/levels SYM` | `/lvl` | VWAP, PDH/PDL, CPR, pivots, ORB |
| `/board` | — | F&O Board: breadth, leading/lagging sector, top 5 bullish and bearish order-block setups, flagged `REV` (reversal build-up) and `trap ▲/▼` (liquidity sweep). `/board nf` pushes the same board as one structured message for the NewsForge app instead of text |
| `/ob SYM` | — | One stock's order block, R/S and liquidity, any liquidity trap or reversal build-up, why it ranks, and the retest `/alert` to send |
| `/ind SYM` | `/indicators` `/ta` | Indicator bundle |
| `/conf SYM` | `/confluence` `/mtf` | Multi-timeframe agreement score |
| `/signals` | `/sig` | Strategy stances |
| `/chart SYM` | — | Chart image |
| `/candles SYM [day\|5minute\|minute] [count]` | `/bars` | Price history as compact JSON messages for the phone chart (default `day`, 300 bars, max 2000); stops at the first rate-limit refusal |
| `/valuation SYM` | `/val` | Fair value, MoS, verdict |
| `/fundamentals SYM` | `/fund` | Stored ratios |
| `/results SYM` | `/result` | Latest quarter |
| `/dividends SYM` | `/div` `/dividend` | Upcoming corporate actions |
| `/news SYM` | — | Recent headlines |
| `/portfolio` | `/pf` `/holdings` | Holdings with annotations |
| `/sips` | `/sip` | SIP debits, soonest first, with the monthly total |
| `/compare TAG` | — | Paper trades vs the same strategy backtested |
| `/mf schemes` | — | Which fund portfolios you've imported |
| `/mf overlap A \| B` | — | How much two funds are the same fund |
| `/mf lookthrough` | — | True exposure: funds plus direct holdings |
| `/journal` | `/j` `/trades` | Open positions and live R |
| `/alerts` | — | What's armed |
| `/exit SYM` | `/trail` `/stop` | Where the trailing stop sits, and R so far |
| `/brief` | `/b` | The pre-market brief on demand |
| `/premarket` | `/morning` | Same |
| `/size SYM entry=405 sl=398` | — | Position size. Computes, writes nothing. |
| `/health` | `/status` `/up` | Are the four services running? |
| `/mc SYM STRATEGY` | `/montecarlo` | Drawdown distribution |
| `/query EXPRESSION` | `/q` `/find` | Find stocks by any condition |
| `/scan CONDITIONS` | `/scanner` | Run a scan |
| `/batch STRATEGY` | — | Breadth across symbols |
| `/heatmap SYM STRATEGY x y` | `/sweep` | Parameter grid |
| `/wf SYM STRATEGY grid` | `/walkforward` | Walk-forward |

### Write commands — PIN required

Add `pin=XXXX` anywhere in the message when `NTFY_CMD_PIN` is set.

| Command | Does |
|---|---|
| `/alert SYM CONDITION [tf=5m] [repeat]` | Arm an alert |
| `/unalert SYM` · `/delalert ID` | Disarm |
| `/log buy SYM 405 sl=398 tag=st_adx` | Journal a trade |
| `/close ID exit=419 reason=target` | Close one |
| `/amend ID sl=405` | Trail a stop |
| `/approve TOKEN pin=XXXX` | **Place a real order.** Single-use token. |
| `/reject TOKEN` | Discard a proposal |

Be clear-eyed about the security model: the topic name is effectively the
credential, and the PIN can be read out of a command you send by anyone
already subscribed. It stops the over-the-shoulder case, not a subscribed
attacker. Set `NTFY_TOKEN` if your server supports real auth.

---

## Reference tables

### Intervals

`minute` `3minute` `5minute` `10minute` `15minute` `30minute` `60minute` `day`

All eight are Kite-native. The chart page also offers **week** and **month**,
built from daily bars in the browser — there's no CLI equivalent because
nothing server-side needs them.

### Strategies

```
bb_squeeze          bollinger_revert    cci_revert          donchian_breakout
ema_cross           keltner_break       macd_cross          macd_zero
momentum            price_sma           pullback_trend      rsi_revert
sma_cross           st_adx              stoch_revert        supertrend
vol_breakout
```

### Query fields

Price: `close` `open` `high` `low` `volume` `price`
Averages: `sma(n)` `ema(n)`
Oscillators: `rsi(n)` `adx(n)` `atr(n)` `atr_pct(n)` `macd` `macd_signal` `macd_hist`
Bands: `bb_upper(n,k)` `bb_lower(n,k)` `bb_width(n,k)` `highest(n)` `lowest(n)`
Volume: `avg_volume(n)`
Derived: `change_pct(n)` `supertrend(n,k)` `supertrend_dir(n,k)`

**Timeframes:** wrap any expression in `weekly(...)` or `monthly(...)`.

```bash
tf query "weekly(close) > weekly(sma(20))"
tf query "weekly(rsi(14)) > 55 and rsi(14) < 40"   # weekly trend, daily dip
```

Each side of a comparison is read at its own most recent bar, so
`weekly(close) > sma(200)` compares the latest weekly close against the latest
daily 200-DMA — what you'd read off two charts side by side. They can't be
nested.

**Per symbol** (one value per company, from fundamentals and valuation):

| Group | Fields |
|---|---|
| Profitability | `roe` `roce` `roce_screener` `opm` |
| Balance sheet | `debt_equity` `interest_cover` |
| Growth | `sales_growth3` `sales_growth5` `profit_growth3` `profit_growth5` |
| Multiples | `pe` `pb` `book_value` `div_yield` `earnings_yield` `market_cap` |
| Yours | `rs` `mos_pct` `fair_value` `n_models` |

A company whose figure isn't stored **never matches** — `debt_equity < 0.5`
won't surface a business whose debt you don't know, which would otherwise mean
the screen favours the worst disclosure.

Arguments are optional and fall back to conventional defaults, so `rsi` means
`rsi(14)`. `highest(n)` and `lowest(n)` **exclude the current bar**, matching
the backtester's Donchian convention — otherwise `close > highest(252)` could
never be true.

`tf query --fields` prints the live list.

### Scan conditions

```
52w_high      52w_low       above_sma     bb_squeeze    death_cross
donchian_break  golden_cross  rsi_range   strategy_long volume_surge
```

Combine two or more. A single condition is a list; two are a thesis.

### Getting help from the tool itself

```bash
tf --help                   # every command
tf paper --help             # a group's subcommands
tf walkforward --help       # one command's flags
```

Send `/help` from the phone for the command list there — `/commands` and
`/?` work too.

---

## A worked week

```bash
# Monday morning
tf login

# any evening, once
tf universe from-watchlist mine
tf universe backfill mine --days 400        # leave it running

# after the close, daily
tf signals
tf paper queue

# next morning
tf paper fill

# weekly
tf journal stats                             # which tag actually makes money?
tf rs --universe mine --top 20
tf screen --universe mine

# before trusting a new strategy
tf batch st_adx
tf heatmap BEL st_adx fast slow
tf walkforward BEL st_adx fast=5:25:5
tf mc BEL st_adx --mode bootstrap
```
