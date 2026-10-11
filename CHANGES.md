# TickerForge — delivery, insiders, pre-open gaps, a checklist, regimes

Five additions, four of them fed from NSE rather than Kite.

## Delivery volume

`app/delivery.py` reads the evening bhavcopy (`sec_bhavdata_full_*.csv`, EQ
series only) into a `delivery` table. Query fields `deliv_pct`,
`avg_deliv_pct` and `deliv_ratio`; scan condition `delivery_spike`; a
`Deliv N×` chip on the Board. On the Board it's context only and doesn't
feed the score, because it's the last completed session's number on an
intraday screen. **The spike ratio is measured on quantity**, against the
prior 20 sessions excluding today, so a quiet day at 80% delivery of almost
nothing doesn't count as accumulation. The scanner refuses to match when
the delivery file is for a different session than the last candle.

## Insider trades

`app/insider.py` stores SEBI PIT disclosures (promoter, director and KMP
buys, sells, pledges, releases and invocations). Every disclosure on a name
you follow pushes. Market-wide, only open-market buys ≥ `INSIDER_MIN_VALUE`
and pledge invocations push, batched. Shown on Corporate Actions, in the
brief, and via `/insider`. Field names were checked against a live
response. The market-wide query returned no rows when this was written, so
when it's empty the poller asks per symbol for the names you follow, at
most hourly.

## Pre-open gaps

`app/preopen.py` reads the 09:08 auction prices and pushes once per weekday:
open trades opening through their stop (max priority), and followed names
gapping ≥ `PREOPEN_GAP_ATR` daily ATRs. Silent when nothing qualifies.

## Pre-trade checklist

`app/checklist.py` runs on `/log`, the Journal form (live, before submit),
`journal open`, and semi-auto proposals. It checks for results due soon, low
R:R, trading against the regime, a high-vol tape, correlation with an open
position, and insider selling. **It warns and never blocks.** Warnings are
stored as JSON in `trades.checks`, where NULL means "never checked" and `[]`
means "checked and clean". The difference matters to `journal checks`,
which compares each warning's expectancy against clean entries and leaves
pre-checklist trades out of both sides.

## Regime-split backtests

`app/regime.py` labels each Nifty day high-vol, trend-up, trend-down or
choppy, using the same alignment as `regime_gate`, so there's no
lookahead. Single runs and batches (pooled across symbols) report trade
stats per regime, plus a verdict when the edge flips sign between them.
Percentile ranks use mid-rank for ties. With `<=`, a perfectly steady
volatility ranked at 100% of its own history and read as extreme; a test
caught it.

---

# TickerForge — query engine: industry P/E, and comparing names

    pe < industry_pe * 0.8 and roe > 15
    industry contains "bank" and pb < 1.5 and roe > 14

## Computed, not scraped

Screener publishes an industry P/E. This computes one, from the P/Es already
stored for 657 companies — it refreshes whenever the fundamentals do, and there
is no second number to reconcile with the first.

**Median, not mean.** Measured on the real store: 628 P/Es, median 35.7, mean
66.7. The mean is 1.9x the median because a handful of barely-profitable names
sit at several hundred times earnings, and one of those should not decide what
its industry is worth. Non-positive P/Es are dropped entirely — a company with
no earnings has no P/E, and a negative one is not a cheap one.

**The population is everything stored**, never what a query happens to scan. An
industry P/E that moved with your universe would be a different number under the
same name in two screens, and `pe < industry_pe` would quietly mean something
different each time. Guarded structurally rather than by comment:
`group_pe()` takes no symbol list, so there is nothing for a universe to narrow.
There's a test asserting the signature stays empty.

## The classification

Sector and industry come from the peers block — a line of prose, not a table,
so it gets its own reader rather than going through the generic section parser.
They land in the `snapshot` dict, which means they store, cache and expire
through exactly the same path as "Stock P/E": a new kind of fact with no schema
change.

Absence is preserved deliberately. A company with no classification stays
unclassified rather than being filed under `""`, where it would join every
other unclassified name in an industry that doesn't exist and produce a
real-looking median of nothing. A match longer than 80 characters is also
rejected — if the markup changes and the regex swallows a paragraph, that's a
parse failure wearing the shape of a sector name.

Same caveat as shareholding: **the scrape is unverified against a live page**,
and the failure mode is absence rather than wrong answers.

## Names as values

`sector` and `industry` are the engine's first non-numeric fields. They compare
with `==`, `!=` and `contains`, against a quoted value, and with nothing else —
`sector > 5` is refused rather than coerced into an ordering that doesn't
exist. Matching ignores case.

`contains` is the one that gets used: screener's industries read like *Consumer
Durables - Domestic Appliances*, and exact matching on those is a typing
exercise.

An unclassified company matches nothing, **including under `!=`**. That one is
easy to get wrong: `sector != "Banks"` sweeping in every company whose sector
is simply unknown would silently widen a screen. Unknown reads as no, here as
everywhere else in this engine.

## Re-fetch required

Nothing is classified yet — `group_pe()` reports 0 of 657 symbols with an
industry. `tf fundamentals fetch --all` picks up both this and shareholding in
the same pass.

## Tests

`tests/test_query_industry.py`, 20 tests. One existing test needed adjusting:
it probes every field with `> -99999999`, which is the wrong probe for a field
that has no ordering, so name-valued fields are now exercised with `contains`.

Suite: 925 passed, 83 skipped. pyflakes clean.

# TickerForge — query engine: ranges, and who owns the company

## Chained comparisons

    20 < rsi(14) < 30 and close > sma(200)
    sma(50) * 0.95 < close < sma(50) * 1.05

Flagged in the first audit of this engine and left undone until now. A band is
written as a band everywhere else; here it was a parse error.

It expands to the conjunction it means, so `--why` reports which end you
missed rather than one opaque clause:

    20 < rsi(14) < 30   ->   20 < rsi(14) and rsi(14) < 30

The middle term appears in both halves as the *same node object*, and `_num`
is memoised per node, so the indicator runs once. There's a test counting the
calls, because that's the whole reason the expansion is free rather than a
doubling.

Two refusals rather than guesses:

- `20 < x > 30` looks like a band and is satisfied by anything above 30.
  Nobody types it deliberately, and picking which half was meant would be
  picking. Both operators must face the same way.
- Only `<` `<=` `>` `>=` chain. `a == b == c` is a different idea and
  `a crosses_above b < c` isn't one at all; both fail rather than quietly
  meaning something.

## Shareholding

    q("promoters") < q("promoters", 2) and fii_holding > 10
    promoter_holding > 50 and debt_equity < 0.3

`promoter_holding`, `fii_holding`, `dii_holding`, `public_holding`,
`govt_holding`, `shareholders` — all quarterly, all reachable through `q()` as
well.

Screener's "change in promoter holding" needs no field of its own: it falls out
of the period index, which is what the index is for.

**The section was simply never scraped.** `"shareholding"` wasn't in
`SECTIONS`, so 657 companies were fetched without it. `QUARTERLY` becomes a
tuple — `("quarters", "shareholding")` — so `q()` searches both, since both are
reported per quarter and live in separate tables only because screener renders
them in separate sections.

### The honest caveat

The shareholding *parse* is unverified against a live page. The existing parser
is generic — `section#<id>`, first table, header row as periods, body rows as
series — and five sections already go through it unchanged. A synthetic fixture
shaped like screener's markup parses correctly, including the trailing `+` on
row labels, `%` suffixes, and Indian digit grouping (`1,20,000` -> 120000).

But a fixture is not screener. The failure mode if the real markup differs is
benign — nothing stored, no shareholding rows in `--statements`, and
`diagnose` naming the missing section — so this is absence, not wrong answers.
Verify with one command:

    tf fundamentals fetch INFY && tf query --statements

### Re-fetch required

Nothing is stored for shareholding yet. `tf fundamentals fetch --all` is about
33 minutes at the current 3s delay. Until then the fields read blank rather
than inventing a holding.

`diagnose` now distinguishes a section that is missing because it was *added
since that symbol was stored* from one missing because the parse broke — the
first is a stale cache and the second is a layout change, and an hour spent
looking for the wrong one is an hour.

## Tests

`tests/test_query_chained_and_holding.py`, 21 tests. Suite: 905 passed, 83
skipped. pyflakes clean on query.py and fundamentals.py.

# TickerForge — query engine: the technical catalogue

Fifteen fields, closing the last gap from the original audit.

| | |
|---|---|
| `stoch(n)` `stoch_d(n)` `willr(n)` | range-position oscillators |
| `cci(n)` `mfi(n)` `cmf(n)` | Commodity Channel Index, Money Flow Index, Chaikin Money Flow |
| `obv` `vwap(n)` | on-balance volume, rolling VWAP |
| `stddev(n)` `keltner_upper/lower(n, m)` | dispersion and the ATR band |
| `prev_close` `gap_pct` | yesterday's close, today's open against it |
| `dist_from_high(n)` `dist_from_low(n)` | drawdown from the N-bar high, recovery from the low |

The indicators live in `indicators.py`, so the charts can use them too. Each
field inherits the memoisation, argument validation, data containment and
staging built in the earlier passes, which is why fifteen of them cost about
as much as one would have before.

## Window convention, stated because it's an asymmetry

`highest(n)` and `lowest(n)` **exclude** the current bar, deliberately — so
"broke above the 252-day high" isn't trivially true of every bar that makes
one. The oscillators and `dist_from_high` **include** it, because a stock at a
new high is 0% below its high and reporting yesterday's would say otherwise on
precisely the day you care.

Both conventions are now load-bearing in the same module, so there's a test
guarding each. If `highest()` ever picks up today's bar, every breakout query
in the codebase becomes true on every bar, and the test says so by name.

## Three places a plausible wrong number was available

**`stddev` is population, not sample.** `bollinger()` divides by `period`;
matching it means `sma(20) + 2 * stddev(20)` equals `bb_upper(20, 2)` exactly.
Two conventions in one module would have put them a few percent apart, for no
reason anyone could reconstruct later. There's a test asserting they agree.

**`vwap(n)` with no volume in the window reads blank**, rather than falling
back to a plain average. The fallback returns a number that looks like a VWAP,
is an SMA, and is wrong in exactly the situations where the distinction
matters.

**`stoch` on a flat window is absent, not 50.** There is no position within a
range of zero, and 50 looks identical to a genuine mid-range reading.

## A bug caught by writing the tests

`stoch` and `williams_r` read candle fields with `.get`, so candles missing
`high`/`low` entirely produced `None` for every bar — a field that silently
reads blank for ever, which is the failure mode this engine keeps closing off.
Every other field errors there. They now ask `Context.column()` for the
columns first, which tolerates gaps and refuses absence, matching the rest.

Also worth recording: the first `stoch_d` test passed for the wrong reason. On
a monotonically rising series where each high is its own close, %K is pinned at
100 on every bar, so %D matches it and "the smoothing works" is unfalsifiable.
The test now uses a spike followed by a slide, where %K genuinely varies.

## Verified on live candles

    RELIANCE  stoch 60.4  mfi 48.8  cmf -0.21  vwap20 1296.34  down_from_52wh 18.9%
    HDFCBANK  stoch 23.3  mfi 23.7  cmf -0.17  vwap20  789.88  down_from_52wh 26.2%

## Tests

`tests/test_query_technicals.py`, 31 tests. Suite: 884 passed, 83 skipped.
pyflakes clean on query.py and indicators.py.

# TickerForge — query engine: the financial statements as fields

    growth("sales", 3) > 15 and fcf > 0 and debtor_days < 60
    q("net profit") > q("net profit", 4)
    ttm("sales") > fin("sales") * 1.1

## What was already there

`fund_series` held **317,708 numbers** — 657 symbols × 57 row labels across
five statements × ~13 periods. The query engine could reach about ten of them,
through hand-mapped derived ratios. Sales, EPS, Operating Profit, Reserves,
Borrowings, Free Cash Flow, Cash from Operating Activity, Debtor Days,
Inventory Days, Working Capital Days, Cash Conversion Cycle, Tax %, Dividend
Payout, Gross NPA — all stored, none queryable.

This is the substrate underneath the derived ratios rather than a replacement
for them; `roe`, `roce`, `opm` and the growth scalars are untouched.

## Accessors and shorthands

Five accessors (`fin`, `ttm`, `q`, `growth`, `yoy`) taking a row label as a
string, because the labels are screener's, they drift, and a row that appears
next quarter should be queryable without a code change here.

About thirty named shorthands on top — `sales`, `eps`, `fcf`, `cfo`,
`reserves`, `borrowings`, `debtor_days`, `net_profit`, `pbt` — defined *as*
accessor lookups rather than beside them, so there's one path to get wrong and
one set of tests. They join `SCALAR_FIELDS`, which is what makes `index(fcf)`
and `percentile(fcf)` refuse for the reasons `index(roe)` already did.

`tf query --statements` lists every stored row per statement, with its
shorthand where one exists. The accessors take a label, so the labels have to
be discoverable.

## Two decisions that would otherwise be silent

**Period 0 is the latest.** Storage ascends — `period_idx` 0 is Mar 2015. If
that ordering had leaked into the language, every unindexed query would read
eleven-year-old figures and return a screen that looked entirely reasonable. A
negative period is refused outright: storage ascends and the language descends,
so a stray sign would read forward.

**`fin()` excludes TTM.** The annual tables carry a trailing TTM column.
Counted as a year it makes `fin("sales", 1)` a TTM-versus-fiscal-year
comparison and makes three-year growth span something other than three years.
`ttm()` reaches it deliberately.

## Label resolution

Exact, then prefix, then substring — so `fin("sales")` finds `Sales` rather
than losing to a longer row. `sales` and `revenue` resolve to each other:
screener labels the same line one way for a manufacturer and the other for a
bank, and without the synonym a query would quietly exclude every financial in
the universe. Verified against the real data — a screen resolves both
`('profit-loss', 'Sales')` and `('profit-loss', 'Revenue')`.

A label matching nothing raises **before the scan**, with suggestions ranked by
word overlap. Per symbol it would be indistinguishable from a company that
never filed, which is to say from an empty screen with no explanation.

## Loading

`fundamentals.series_for()` fetches every needed row for every survivor in one
pass, chunked at 400 symbols for SQLite's parameter limit. The previous path
called `fundamentals.summary()` once per symbol — two thousand round trips on
a universe scan. Only the rows a query actually names are fetched.

These are a local SQLite read rather than a website fetch, but they still sit
in the second stage: no point loading statements for a symbol the price filter
already rejected. And they're refused in `backtest()`, for the same reason as
`roe`.

## Verified against the live database

    growth("sales", 3) > 15 and fcf > 0 and debtor_days < 60
    → 48 of 400 matched
      BAJAJ-AUTO   sales CAGR 19.9%   FCF 1885   debtor days 20
      ANANDRATHI   sales CAGR 30.9%   FCF  268   debtor days 12

## Tests

`tests/test_query_statements.py`, 27 tests. The period ones carry most of the
weight — the statements fixture is made of round numbers precisely so a
misread period is visible at a glance rather than merely wrong. One test walks
every alias and asserts the row it names actually exists in the stored
catalogue, since a mapping entry pointing at a row screener doesn't publish is
a field that silently returns nothing for every company.

Two bugs caught during the build: statement values weren't broadcast like
scalars, so `fin("sales") * 1.1` raised on None; and `_at` treats a negative
index as "before the first bar", which my own test helper had assumed meant
"last".

Suite: 853 passed, 83 skipped. pyflakes clean on query.py and fundamentals.py.

# TickerForge — query engine: cross-sectional ranking

    percentile(change_pct(63)) > 80 and close > sma(200)
    rank(close / sma(200)) <= 25
    percentile(change_pct(20) - index(change_pct(20))) > 70

The last one is relative strength defined *in the language* rather than
pre-computed — beat the index over a month, ranked against everyone else who
did. `rs` remains as a field; this is the general case it was a special case of.

`percentile` is 0–100, higher is larger, ties at the midpoint. It calls
`rs.percentile_ranks` rather than reimplementing it, so RS 85 on the ranking
page and `percentile(...) > 85` in a query are the same number. `rank` counts
from 1 for the largest; ties share the better position and consume the places
behind them (1, 1, 3), which is how ranks have always worked and what stops two
identical stocks being told one of them is second. Unrankable stays None in
both, never last place — no history is not the same fact as worst in the field.

## The population is everything scanned

Not everything that survived the other clauses. If it narrowed as filters were
added, `percentile(x) > 80` would mean "top fifth of the universe" in one query
and "top fifth of the stocks already above their 200-DMA" in another: a much
weaker screen, identical syntax, nothing in the output to tell them apart. It
would also make the answer depend on clause composition, which is the sort of
invisible coupling this engine keeps having to design out.

So `_measure_field()` runs before any filtering, over the full symbol list. A
symbol whose expression can't be computed is left out of the population rather
than scored zero — including it would drag everyone else's percentile up.

Every run reports it: *"Percentile and rank measured across the 500 symbols
with enough history."* The population is the whole meaning of a percentile and
the one thing about it not visible in the query text.

## The cost, and where it isn't paid

The field has to be complete before any symbol can be judged, so a
cross-sectional query reads the candles twice. Holding two thousand symbols'
bars in memory to avoid the second read trades a bounded cost for an unbounded
one. Stored candles are a local DuckDB read; the stage-splitting argument
elsewhere in this module is about requests to somebody else's website and
doesn't apply here.

Queries with no cross-sectional operator make one pass, as before. There's a
test asserting the exact read counts for both.

500 symbols, both passes, percentile + rank + sort + columns: 0.23s.

## Refusals

- **Backtesting.** Measuring against today's universe and applying that to a
  2023 bar assumes you knew this year's cross-section at the time. Same sin as
  `roe`, and it would flatter every momentum system ever tested.
- **One symbol on its own.** There's no honest answer — the operator's entire
  input is the other stocks. `Context.cross` is None rather than empty when
  there's no field, so "no universe here" and "no value for this symbol" stay
  distinguishable.
- **`percentile(roe)`.** A cost refusal, not a semantic one: the population is
  the whole universe, so ranking on a fundamental means a statements fetch per
  company across all of it — the exact bill the two-stage design exists to
  avoid. The message says to filter on it instead.
- **Nesting.** `percentile(rank(x))` ranks a ranking; the inner one has already
  flattened the field to 1..N.

## Two complexity fixes on the way through

`rs.percentile_ranks` counted `below` and `equal` by scanning the sorted list
per symbol — O(n²). Fine for the watchlist it was written for; four million
comparisons per expression on a 2,000-name universe. Now bisected. Identical
numbers and identical tie rule: `usable` was already sorted, so the two counts
are exactly the two bisect points.

`_ranks` had the same shape in its first draft — it built the negated key list
*inside* the loop. Caught before it shipped, and there's now a test that ranks
20,000 scores inside two seconds, loose on purpose because it checks a
complexity class rather than a benchmark.

## Tests

`tests/test_query_cross.py`, 20 tests. The population test is the load-bearing
one: it runs the same ranked expression with and without an unrelated filter
and asserts the percentile doesn't move.

One existing test needed cross values supplied, for the same reason it needed a
benchmark last time — the examples now include `percentile(...)`, and a missing
field is correctly an error rather than a silent False.

Suite: 827 passed, 82 skipped. pyflakes clean.

# TickerForge — query engine: index references

    close / index(close) > ago(close / index(close), 60)
    close > sma(50) and index(close) > index(sma(200))

`index(EXPR)` evaluates EXPR on the benchmark's own bars and projects the
result onto this symbol's dates. A wrapper, like `weekly()`, not a field:
`index(sma(200))` is the *index's* 200-day average, computed from index bars.

Defaults to `NIFTY 50` — the same constant `rs.py` ranks against, with a test
asserting the two can't drift apart. Another benchmark goes in quotes:
`index("NIFTY BANK", close)`. That required string literals, which the
tokenizer had none of; they're a `Str` node that is refused everywhere except
as `index()`'s first argument, so a stray `"NIFTY BANK" > 1` says something
readable instead of being looked up in the field catalogue.

## Alignment is the whole risk

Aligning two daily series by **position** is the tempting shortcut and the real
trap. It produces a plausible number for every bar of every symbol, and it's
wrong the moment either side is missing a day — a suspension, a late listing, a
holiday one of them observed. Which is to say: often, silently, and in the
flattering direction, because a stock missing a bar starts reading the index
from the future.

`bench_map()` maps each stock bar to the last index bar at or before its
timestamp, never a later one. Same discipline as `resample_map`'s "last
completed period", for the same reason. A worked case, index trading on even
days only:

    stock day :  0  1  2  3  4  5  6  7  8  9 10
    index seen:  0  0  2  2  4  4  6  6  8  8 10

Day 9 sees day 8, not day 10. There's a test asserting exactly that, because
it's the difference between a regime filter and a time machine.

A stock bar earlier than the benchmark's first bar reads `None` rather than
borrowing the earliest value — back-filling there invents a market that wasn't
quoted yet.

## Backtestable, unlike fundamentals

`backtest()` refuses `roe` and `mos_pct` because today's balance sheet applied
to a 2023 bar asserts you knew it at the time. An index close on a 2023 bar
simply *is* what it was on that bar — point-in-time, no assertion required.
Lumping the two together would have meant the regime filter the roadmap wants
(3.5, "optional Nifty-regime filter as a strategy gate") could never be tested,
so `backtest()` now loads benchmarks and refuses only when the candles are
actually missing.

## Refusals

- `index(roe)` — an index has no balance sheet. Scalars are per-company and
  ride on the context; left alone this would have answered with the *stock's*
  ROE under a name saying otherwise. The benchmark context is built without
  scalars at all, so it's impossible rather than merely forbidden.
- `index(index(x))` — asks what the index's index was doing.
- `weekly(index(close))` — would align daily index bars onto week-start stamps.
  The error points at the order that does work: `index(weekly(close))`.
- A missing benchmark is a `QueryDataError` naming the backfill, not an empty
  result. Inside a scan it lands in `errors` per symbol; in a backtest it stops
  the run.

## Cost

Benchmarks load once per run, not once per symbol — a 200-name universe would
otherwise read the same index candles 200 times. A query naming no benchmark
loads none. Tests for both.

---

## Also: a shadowed function, and an audit for the next one

`commands.py` defined `cmd_walkforward` **twice**. The copies were identical
apart from line wrapping — a patch applied to a file that had already had it
applied. Python doesn't complain: the second silently replaced the first, and
the first sat there looking maintained. You could read it in a review, satisfy
yourself the logic was right, and none of it ran.

Nothing was broken this time, which is exactly the problem — there was no event
to notice. The dangerous version of the same shape is a fix applied to the copy
the dispatcher doesn't reach.

Removed, along with a duplicated `import json` and two unused imports of the
same origin. `commands.py` is now pyflakes clean.

`tests/test_no_shadowed_definitions.py` makes it permanent: no module may
define a top-level name twice, and every phone command in `HANDLERS` and
`MULTI_ARG` must still resolve to the function of that name currently in the
module. It mirrors the duplicate-element-ID test on the frontend, for the same
reason — a duplicate that changes nothing today is a trap for whoever edits the
wrong copy tomorrow. There's also a test that the detector itself fires on a
known duplicate, because a test that can only pass isn't one.

## Tests

`tests/test_query_index.py`, 21 tests, most of them about dates. One existing
test needed a benchmark supplied, since the examples now include `index(...)`
and a missing benchmark is correctly an error rather than a silent False.

Suite: 807 passed, 82 skipped. pyflakes clean on query.py and commands.py.

# TickerForge — query engine: ranking, trimming and columns

A screen that returns 200 names in whatever order the universe file happened
to list them isn't a screen, it's a haystack. Three clauses, after the
condition:

    close > sma(200) and rs > 60 sort by change_pct(20) top 20 show rsi(14)

| | |
|---|---|
| `sort by EXPR [asc\|desc]` | rank the results, descending unless told otherwise |
| `top N` | keep the best N, applied **after** the sort |
| `show EXPR, EXPR` | extra columns, labelled by the expression itself |

`order by`, `limit` and `select` parse as the same three.

## Why they're in the language and not on the endpoint

A saved query and a phone command are each *one string*. Put `sort` in the
HTTP layer and `/query` from the phone can never rank anything, and a saved
screen can never remember how it wanted to be ordered — which is most of the
value of saving one.

## New: Plan, alongside the AST

`plan(text) -> Plan(condition, sort, descending, top, show)`. `parse()` is
unchanged and still returns a bare condition, because that's what
`to_positions`, `backtest` and `explain` all want and none of them can honour
a ranking.

`parse()` now **refuses** a clause rather than choking on an unexpected token:
`'top' only means something when screening — a backtest or an alert has
nothing to rank`. Silently dropping it would run a query the user believes is
ranked and trimmed. `save()` and `run()` use `plan()`; `backtest()` keeps
`parse()` and so rejects a ranked query outright.

## Ordering decisions worth arguing with

**`top` applies after the sort.** Truncating first gives you twenty names
indistinguishable from the right twenty that aren't them. This is the whole
reason the change exists.

**Missing values sort last in both directions.** A company with no RS must not
top `sort by rs desc`, and must not top `sort by rs asc` either — absent is
not zero and it isn't infinity. The sort key is `(is_missing, value)` with a
constant in the second slot when the value is missing, because two unranked
rows would otherwise end up comparing None with None and raising.

**Descending is the default, and the note says so.** Right for `sort by rs`,
wrong for `sort by pe`. Rather than pick a per-field convention nobody can
remember, every sorted result states `Sorted by pe, highest first.`

**An unsorted result that gets truncated says so**: *"These are the first rows
in scan order, not the best — add `sort by …` to choose."* Where the old
behaviour survives it's now visible instead of silent.

**Ties keep scan order.** Python's sort is stable, so a re-run doesn't shuffle.

## Staging

`sort by roe` on a price-only condition would have skipped the fundamentals
load entirely under the old split, and sorted every row by None — a ranking
that silently became a tie. Clause expressions now feed the same
`_needs_scalars` decision as the condition, so they load fundamentals for
whatever passed the price filter. A price-only sort still costs nothing:
`close > 1 sort by rsi(14) show atr(14)` does not touch valuation.

## Elsewhere

- `run()` returns `columns`, `sorted_by` and `descending`.
- `/api/query/check` now uses `plan()` + `render_plan()`, so the live editor
  check accepts clauses as you type them.
- The results table on `/query` is driven by `columns` rather than the
  hardcoded `["rs","mos_pct","pe","roe"]`, falling back to that guess when
  there's no `show`. The sorted column gets an arrow.
- `summary()` (CLI and phone) prints `show` columns labelled, and stops
  double-printing RS/MoS when they were asked for by name.
- Four ranking examples added to the catalogue; `fields()` advertises the
  clauses under `kind: "clause"`.

## Tests

`tests/test_query_ranking.py`, 29 tests. Two existing tests needed adjusting
because they assumed an EXAMPLE always survives `parse()` — both now use
`plan()`, which is the honest version of what they were checking.

Suite: 783 passed, 82 skipped. pyflakes clean on query.py. (`commands.py`
still has four pre-existing pyflakes warnings — a duplicated `json` import, a
duplicated `cmd_walkforward` definition at line 977 shadowing the one at 943,
and two unused imports. Untouched here, but the duplicate function definition
is worth a look on its own.)

# TickerForge — query engine: hardening pass

Five defects, found by stress-testing rather than by review. The language was
correct on the questions it was asked; these are the ones it wasn't.

## Fixed — evaluation was O(n³)

`ctx.series()` cached raw fields, so `ema(20)` was computed once. Anything
*built* on a field wasn't cached at all. Because evaluation threads a bar
index, reading an expression at every bar rebuilt every sub-series once per
bar:

    close > sma(20)              0.002s
    close > sma(20) * 1.05       0.058s     ← 29× for one multiply
    max(close * 1.0, 20) > 1    27.682s     ← 500 bars, ONE symbol

The last one nests three O(n) passes. On a 200-symbol universe that isn't a
slow screen, it's a hung one — and every operator added later would have
inherited it.

`_num` is now memoised per node on the Context. Keyed by `id(node)` rather
than by the node itself: hashing a frozen dataclass AST is O(size) per lookup
where an id is O(1), and the cache entry holds the node alongside its value so
the id can't be recycled onto a different node while the context lives. Same
numbers now:

    close > sma(20)              0.0008s
    close > sma(20) * 1.05       0.0011s
    max(close * 1.0, 20) > 1     0.0027s   ← 10,000×

The cache lives on the Context, so it dies with the symbol and can't leak one
company's series into another's. There's a test for exactly that, because the
failure would hand you plausible numbers under the wrong name.

## Fixed — one malformed candle aborted the whole scan

`run()` caught only `QueryError` around evaluation. A bar with a null volume
raised `TypeError`; candles missing a `high` raised `KeyError`. Either killed
a 2,000-symbol screen outright and returned a 500 from `/api/query` — 1,999
good answers traded for one bad row.

New `QueryDataError(QueryError)` draws the line the scan needs: a misspelt
field is wrong for every symbol and still stops the run loudly, while broken
stored data costs exactly the symbol it belongs to and lands in `errors`.

`Context.column()` now separates a *gap* from an *absence*. A single null
volume is a feed hiccup — it becomes None and the window functions skip it. A
column missing from every bar means these candles aren't what the query thinks
they are, so it errors rather than answering False, which would be a wrong
answer dressed as a real one.

## Fixed — nonsense windows failed silently

`_check_window` only ever guarded the history operators. So `sma(-5)`,
`ema(0)` and `rsi(1000000)` produced a series of None, every comparison read
false, and the screen returned nothing — for ever, and indistinguishably from
a screen that genuinely had no matches. That's the same failure the
`highest()` comment already warns about; this closes the other half of it.

Periods and multipliers are now checked at **parse** time, per the module's
own rule that a typo must fail before the fortieth symbol. `ARG_KINDS` says
which argument is a lookback and which is a band multiplier, so the message
names the field: `sma()'s period has to be at least 1 bar (at column 9)`.

`_scalar` is replaced by `_fold`, which folds constant arithmetic properly —
`sma(10 * 2)` and `max(close, 5 + 5)` are reasonable things to type. Anything
referring to a series folds to None, which is what makes `ema(close)` an error
instead of a mystery.

## Fixed — `explain()` silently dropped clauses

`walk()` handled and/or/not/comparison and fell off the end for everything
else, so history operators never appeared:

    within(5, rsi(14) > 70) and close > 100
      → explained: ['close > 100']

`--why` is reached for precisely when the interesting clause is the one
failing. It now covers history operators, and anything it can't decompose gets
an honest row rather than vanishing.

Negation is reported by *contribution to the result*, not by the clause's own
truth. Under a `not`, a clause that holds is the reason the query matched
nothing; printing it as passing sends you to the wrong line.

## Fixed — deep input was a RecursionError, not a refusal

The parser is reachable from an HTTP endpoint and a phone message. 400 nested
brackets blew the stack; so did a flat `1 + 1 + 1 + …`, which never recurses in
the parser but builds a tree one level deep per term that `validate()`,
`render()` and evaluation all walk recursively.

Three limits, and two guards because one can't see both cases: `MAX_DEPTH`
counts real recursion (only `parse_or`, `parse_not` and `parse_unary` can
recurse without consuming a bracket), while `MAX_TOKENS` and `MAX_TEXT` catch
the flat chain the depth counter is blind to. Tests assert each fires on its
own — 400 brackets trips both, which would have let a broken depth guard hide
behind the token cap.

## Tests

`tests/test_query_hardening.py`, 30 tests, written first — all 30 failed
against the old module. The cost tests count work (`_history` invocations,
`_arith` invocations) rather than seconds, because a timing assertion fails on
a loaded machine and teaches you nothing; the one wall-clock test is
deliberately loose at 2s, since the unfixed engine misses it by three orders
of magnitude and it's checking a complexity class, not a benchmark.

Suite: 754 passed, 82 skipped. pyflakes clean. The existing
`test_a_query_cannot_execute_anything` still holds — it forbids `getattr` in
this module, which caught a first draft of the argument checker.

# TickerForge — chart: Supertrend fix, event markers, saved layouts

## Fixed — Supertrend drew two lines instead of one
`I_super` did `data: s.up.filter(x => x.value != null)` on both the up and down
series. Filtering the nulls out leaves each series DENSE, so lightweight-charts
connects the surviving points straight across the gaps — the up-line drew a
segment leaping over the entire downtrend and the down-line did the mirror
image. Two lines spanning the chart, where Kite shows one line changing colour.

The nulls were never noise; they were the instruction to break the line.
Whitespace (a point with `time` but no `value`) is how lightweight-charts is
told to lift the pen.

Now:
- `supertrend()` emits proper whitespace for the inactive side;
- at each flip it back-fills the previous bar into the NEW side, so the two
  colours meet and you get the vertical jump Kite draws instead of a one-bar
  hole;
- it also returns `flips[]`, which the new event markers reuse rather than
  recomputing.

Verified by geometry, not by eye: on a 180-bar up/down/up series there are 59
whitespace breaks in the up-line, 2 flips, no bar where neither line is drawn,
and both lines drawn only at the 2 bridge bars.

## New — event markers (● Events)
Dots on the price series for: Supertrend flip, RSI crossing 70/30, MACD signal
cross, golden/death cross, Bollinger band break, volume ≥ 2× average.

**Events, not states.** A marker appears on the bar a condition BECAME true.
Marking the state would paint half the chart and tell you nothing — `bb_break`
fires once per excursion, not on all 40 bars price spends outside the band.
Tested: on a 400-bar series it produces 1 marker, and a flat tape produces 0
events across all six types.

Computed from the same closed bars the alert engine and backtester use, so a
marker and an alert agree about what happened.

## New — saved layouts (▤ Layout)
Name + Enter saves the working setup: every indicator with its parameters AND
per-instance colour overrides, which event markers are on, and the interval and
range. Shift+Enter also marks it default, applied at startup before the first
symbol loads so the chart comes up configured rather than visibly rearranging.

Stored in SQLite, not localStorage — the same reasoning the roadmap gives for
drawing tools: a layout you spent time building shouldn't vanish because you
opened a different browser. New `chart_layouts` table, 3 API routes.

`applyLayout` clears existing indicators before rebuilding; reusing instances
would leave orphaned panes for any indicator the layout drops.

## New — tests/test_chart_js.py + tests/js/
The Supertrend bug was invisible to every Python test because it lived entirely
in the browser. These extract the relevant functions from index.html by brace
matching (regexes over 3000 lines break whenever the file is touched) and run
them under node:

- **supertrend.body.js** (9 checks) — both trends drawn, the up-line HAS breaks,
  arrays time-aligned, never both lines except at flips, never a bar with
  neither, flips both directions, empty shape on a short series.
- **chart_events.body.js** (11 checks) — every event type fires in both
  directions where applicable, transitions not states, a flat tape stays quiet,
  no throw on a short series, all markers carry the fields `setMarkers` needs.
- Plus a `node --check` over the whole page (a syntax error anywhere blanks the
  chart with nothing but a console message), and a direct guard asserting
  `filter` hasn't reappeared next to the Supertrend series.

Skips cleanly where node isn't installed. 481 tests total.

---

# TickerForge — Phase 3.3: walk-forward validation

Optimise on one window, test on the next one the optimiser never saw, roll
forward. New `app/walkforward.py`, `/api/backtest/walkforward`, a panel on
`/backtest`, a `walkforward` CLI command and `/wf` on the phone. 465 tests.

The roadmap called this "worth more than any new strategy" and that's right:
every other tool here optimises and scores on the same bars, so none of them
can say what a strategy would actually have earned. This can, because it never
does — the stitched out-of-sample curve is produced entirely by decisions made
without hindsight.

## The two numbers that matter
- **Efficiency** = out-of-sample ÷ in-sample. Near 1 means the optimiser found
  something that persisted; near 0 means it fitted noise and the impressive
  single backtest was an artefact of choosing parameters with the answers
  visible.
- **Parameter stability.** If the optimiser picks fast=5, then fast=30, then
  fast=12 across folds, there is no optimum to find — the good folds are luck
  rather than a setting you could have chosen in advance. Reported per
  parameter as a coefficient of variation, so a fast MA moving 5→30 and a slow
  MA moving 100→125 are comparable.

Efficiency is deliberately **None** when in-sample was negative. Dividing by a
negative produces a number that looks like a ratio and isn't one.

## Warm-up is what makes it honest
A 200-day SMA scored on a 60-bar out-of-sample slice is undefined for every
bar. Each fold therefore computes indicators over in-sample PLUS out-of-sample
and measures only the out-of-sample segment. The optimiser still never sees
out-of-sample outcomes — it only ranks parameters on in-sample bars — but the
indicators driving it are warm, exactly as in live trading.

`test_optimiser_never_sees_out_of_sample_bars` spies on every evaluation call
and asserts no optimisation ran on a slice extending past the boundary. That
single property is what everything else rests on.

## Controls
Two tests are outcome controls rather than unit tests:
- a **random walk must not hold up** — if a coin flip passes, the method is
  broken;
- a **persistent trend must hold up** — if a real edge fails, it's too strict.

Both pass, and the demo shows the gap plainly: the same strategy and grid gives
15% efficiency (CURVE-FIT) on a random walk and 112% (HOLDS UP) on a drifting
one.

## Modes
`rolling` keeps the in-sample window fixed so old regimes drop out; `anchored`
grows it from the start so every fold uses all history to date. Neither is
right in general, which is why both are there. A test asserts out-of-sample
windows never overlap, or the stitched curve would double-count bars.

## Tests — tests/test_walkforward.py (31)
Fold geometry for both modes including non-overlap, every input guard, the
optimiser-blindness spy, warm indicators across the boundary, chosen params
applied unchanged, both outcome controls, no-grid plain OOS testing, efficiency
sign handling, stitched-curve coverage, empty-aggregate shape, stability
scoring at both extremes, verdict notes, text ordering, and the API including a
401 without a session.

---

# TickerForge — Phase 2.2: EOD scanner

Scan a configurable universe on closed daily bars. New `app/scanner.py`,
`app/universe.py`, a `/scanner` page, 12 API routes, `universe` and `scan` CLI
commands, `/scan` on the phone, and saved-scan hits appended to the EOD push.
434 tests.

## Universe ≠ watchlist
Deliberately separate tables. The watchlist is what you follow; a universe is
what you look through. Scanning your watchlist would be scanning the answer you
already have — the entire value is surfacing names you aren't watching.

## The hard part was candles, as the roadmap said
200 symbols × 400 daily bars is 200 rate-limited calls. `universe.backfill()`:

- **skips symbols already covered**, which makes a re-run cheap and an
  interrupted run safe to restart. A backfill that loses three hours to one
  timeout at 2am is a backfill you stop running.
- **paces itself** at 0.4s between fetches, comfortably under Kite's limit
  rather than discovering the limit by being throttled.
- **continues past a failure** and reports which symbols failed, instead of
  aborting the other 199.
- `coverage()` tells you how much is missing and roughly how long it'll take
  *before* you start.

## Conditions
`52w_high` · `52w_low` · `volume_surge` · `golden_cross` · `death_cross` ·
`donchian_break` · `bb_squeeze` · `above_sma` · `rsi_range` · `strategy_long`.

Each is a pure function of candles, so the engine is fully testable offline.
They ANDed rather than ORed: `52w_high` alone might be forty names, which is
not a scan result, it's a list.

`strategy_long` runs any `backtest.STRATEGIES` rule, so a scan hit is the same
rule the backtester trades — directly testable rather than a lookalike that
drifts from it.

`bb_squeeze` requires the squeeze AND the break. A squeeze alone is a coiled
spring with no direction; the break is what makes it a signal.

`golden_cross` fires on the *event*, not the state — a test asserts it fires at
most twice across a 150-bar rising series. Firing on the state would return the
same forty names every night forever.

## Closed bars only
Reuses `signals.drop_forming_bar`. A scanner including today's partial candle
shows a 52-week high at 10:15 that isn't one at 15:30, and you act on it.

## Scans read, backfill fetches
`scan()` never fetches. A scan that quietly pulled 200 symbols would take
minutes and hit the rate limit mid-page. Symbols without enough stored history
are reported as **skipped**, so an empty result means "nothing matched" rather
than "nothing was looked at" — those are very different and look identical
without the count.

## Fixed while building it
An unknown condition name only raised when a symbol was actually evaluated. If
every symbol was skipped for thin history, the typo never surfaced at all — you
got a confident "0 hits" for a condition that doesn't exist. Names are now
validated before any work.

## EOD push
Saved scans with `"eod": true` have their top hits appended to the evening
push. Opt-in per scan: a scanner that pushes everything nightly is one you
mute. Isolated from the signal scan, because a scanner failure must not cost
you the flips, which are the part you actually trade.

## Tests — tests/test_scanner.py (49)
Every condition individually including short-history guards, AND semantics,
parameter pass-through, ranking, skipped-vs-no-match, truncation, explicit
symbol lists, universe membership with unknown-symbol reporting, backfill
skip/continue/no-session/empty, coverage estimates, saved scans, the API
including a 401 without a session, page wiring, and the EOD opt-in.

A test-harness bug worth noting: `seed_universe` reused instrument tokens 1..N
on every call, so a later `INSERT OR IGNORE` silently kept the earlier symbol
and tests scanned one symbol's candles under another's name. Tokens now come
from a running counter.

---

# TickerForge — Phase 3.2: parameter heatmap

Sweep two parameters, read the shape. New `app/heatmap.py`, a colour grid on
`/backtest`, `/api/backtest/heatmap`, a `heatmap` CLI command and `/heatmap`
on the phone. 385 tests.

## The shape is the answer, not the maximum
A single backtest tells you what one parameter pair did. It can't tell you
whether that was skill or luck. A grid can, because:

  * a **plateau** — a broad region where nearby settings all work — means the
    strategy captures something real. You could have picked any of them, which
    matters because you are going to be wrong about the exact number;
  * a **sharp peak** — one bright cell among dull ones — means the result
    depends on hitting that value. That's curve-fitting, and it won't survive
    out of sample, because next year's peak won't be in the same place.

`analyse()` quantifies it rather than leaving it to the eye: the best cell is
compared to its immediate neighbours (`neighbour_ratio`) and to the grid as a
whole. `verdict()` returns plateau / ridge / sharp peak / nothing works, and
flags a best cell that sits on an edge (too few neighbours to judge) or one
that topped the grid on very few trades.

## Refactor — backtest.evaluate_on
Factored the evaluation out of `backtest.run` so a sweep can fetch candles once
and evaluate the whole grid against them: a 30-cell sweep is **one** API call,
not thirty. `run` goes through the same function, which is the point — a cell
and a click-through single run must agree, or the grid lies about itself. A
test asserts they produce identical numbers. Behaviour-neutral; all pre-existing
backtest tests pass unchanged.

## Fixed while building it — an ambiguity that silently ate input
`_values` accepted `[5, 10, 15]` as either three literal values or
(start, stop, step), resolving any three-number list as a range. So asking for
exactly 5, 10 and 15 produced a **one-cell grid** — and the grid still rendered,
so nothing looked wrong. The forms are now distinguishable all the way down:
a list is always literal, a range is the string `"5:30:5"`. Server, CLI and
phone parsers all pass ranges through as strings rather than pre-expanding them
into an ambiguous list.

## UI
Diverging red/green for metrics with a meaningful zero (Sharpe, expectancy,
return); sequential blue for those without one (profit factor, win rate). Thin
cells — fewer than 10 trades — are dimmed rather than hidden, because "this
setting barely traded" is information. Clicking a cell loads that pair into the
single run below. In the ASCII rendering, a blank was the lowest ramp character
and read as "missing"; it's now `·` for no-result and the ramp starts at `.`.

## Tests — tests/test_heatmap.py (30)
Value forms (literal vs range, floats, zero step, backwards range, malformed),
unknown strategy/metric/parameter, cell limit, candles fetched exactly once,
cell-equals-single-run, plateau / sharp peak / ridge / all-negative recognised
from hand-built grids, edge-peak and thin-cell notes, empty-analysis shape,
1-D sweeps, per-cell failure isolation, SystemExit caught, text ordering, the
API including a 401 without a session, and the page calling the new handlers.

---

# TickerForge — Phase 3.1: batch backtest

One strategy across many symbols. New `app/batch.py`, `/api/backtest/batch`,
an "Across watchlist" button on `/backtest`, a `batch` CLI command and a
`/batch` phone command. 355 tests.

## The ranking is the least trustworthy part
A sorted table is a selection-bias machine. Run one strategy over sixteen
symbols, sort by expectancy, and the top row looks excellent whether or not the
strategy works — best-of-sixteen is impressive even from coin flips. Trading
that symbol because it topped the table is exactly how curve-fitting happens,
and a plain ranked table does nothing to stop it.

So the ranking is there, but **breadth leads**:

- **median expectancy**, not the mean — one runaway symbol can't move it;
- **share of symbols profitable at all**;
- **how far the top row sits above the median**, because a large gap means it's
  an outlier rather than a preview.

`verdict()` puts it in words: broad / narrow / coin-flip / negative. A strategy
profitable on 3 of 16 names hasn't found an edge, it's found three symbols that
trended during the window you chose, and the verdict says so.

The starkest case gets its own flag: when the best symbol is profitable while
the median isn't, `lone_winner` fires and the note reads "the top row is the
exception, and trading it because it topped the table is the whole trap". A
ratio can't express that — dividing by a negative median is meaningless — which
is why it's a separate check.

None of this replaces walk-forward (3.3). It just refuses to pretend a ranking
is a substitute.

## Behaviour
- Defaults to the watchlist; accepts an explicit symbol list.
- One symbol failing never stops the batch. Failures are listed, not dropped —
  a table showing 9 of 16 without saying so overstates its own breadth.
- SystemExit from a missing Kite session is caught per symbol.
- Symbols with no completed trades sort last regardless of direction; they
  haven't earned either end of the table.
- Full risk overlay (sl/tp/trail/max_bars) passes through, so you can batch a
  strategy with its stops attached.

## UI
Clicking any row on `/backtest` loads that symbol's full single run, so the
table is a way into the detail rather than a destination.

## Tests — tests/test_batch.py (30)
Unknown strategy and empty symbol list rejected, watchlist default, per-symbol
failure isolation, SystemExit caught, sort order including unscored-last,
median-not-mean (a sample with one 50% outlier still reports a 0.1% median),
profitable share, empty-aggregate shape (callers index into it; a missing key
is a 500), all four verdicts, lone-winner and outlier notes, small-sample and
thin-trade warnings, text ordering (verdict before the first symbol row), and
the page actually calls the new handlers.

---

# TickerForge — Phase 4.1: alerts from the chart

Right-click the chart to arm a price alert. Armed alerts render as horizontal
lines you can drag to re-arm at a new level. No typing, and it connects the two
systems you use most. 325 tests.

## New — app/alerts.py
- `set_level(id, price)` — move a price alert. The id survives, so the alert
  keeps its place in `/alerts` and any held reference stays valid.
- `retarget(id, price, last_price)` — move it AND flip the direction to match
  where price now is.

Two things happen beyond the number, and both matter:

  * **Cross state is cleared.** Alerts remember which side of the level price
    was on so a cross fires once rather than on every tick. Move the level
    without clearing that and the alert compares against where price stood
    relative to the OLD level — it can fire instantly, or stay silent through
    a genuine cross.
  * **The direction follows the drag.** A "> 410" line pulled below spot
    becomes a break DOWN. Left as a greater-than it would fire the moment you
    released the mouse.

Indicator alerts (RSI, Supertrend, MACD…) are refused with an explanation
rather than silently corrupted — they have no single price to move.

## New — PATCH /api/alerts/{id}?price=&last_price=

## Chart page
- **Right-click** anywhere on the chart → a menu offering "Alert when price
  crosses above/below <level>", with the direction inferred from where you
  clicked relative to spot. Right-click ON a line offers to remove it.
- **Drag** a line to move it; release commits. The chart's own pan/zoom is
  suspended during a drag so the canvas doesn't slide out from under you.
- Cursor turns to `ns-resize` within 7px of a line, so you can tell what's
  grabbable before you press.
- Lines are coloured by side (above spot green, below red) and recoloured live
  as you drag through spot.

lightweight-charts 4.x price lines aren't draggable, so this is a thin overlay:
watch mousedown near a line's y-coordinate, move the line while the pointer is
down, commit on release. The line and its axis label stay native, so they track
zoom and pan for free.

Only price thresholds are drawn. Indicator alerts have no level and stay in the
alerts panel.

## Tests — tests/test_alerts_move.py (18)
Level moves, id survives, cross state cleared, triggered alerts re-armed, stale
cached labels dropped, indicator alerts refused, direction flips both ways and
is left alone without a `last_price`, plus the PATCH route (200/400/404), DELETE
with a numeric id, and a check that the chart page actually *calls* the new
hooks — a defined-but-uncalled function is the classic way UI wiring ships dead.

---

# TickerForge — Phase 1.3: Kite portfolio (read-only)

Holdings and positions from the broker, next to what TickerForge already knows
about each name. New `app/portfolio.py`, a `/portfolio` page, 2 API routes, a
`/portfolio` phone command, 2 CLI commands. 307 tests.

The point isn't the P&L — Kite shows you that. It's that the P&L sits beside
the reasons to act on it. A holding down 12% reads differently depending on
whether your own valuation now says undervalued, whether the trend signals have
flipped flat, and whether it reports on Thursday.

## Per-holding annotations
- **Valuation verdict** from your own models, with margin of safety.
- **Signal stance** — how many configured strategies are long, read from stored
  state rather than recomputed (the EOD scan owns that; re-running six
  strategies per holding on a page load would be slow and pointless).
- **3×ATR chandelier stop** and how far price sits above it. Negative means
  price is already through where the stop would be — the most actionable number
  on the row.
- **Next results date**, armed alert count, open journal trades.

## Flags
Concentration, price below the stop, results imminent, and holdings that are up
while your models now read overvalued. Each is a prompt to look, not advice.

## Read-only, permanently
The module imports nothing that places, modifies or cancels an order.
`test_module_cannot_place_orders` greps for actual call syntax and fails if
that changes, and the FakeKite in the tests has no `place_order` at all — so a
module that tried to trade would fail with AttributeError. Order placement is
Phase 6, gated behind a paper broker and an approval step; it belongs in its
own module with its own guard rails, not bolted onto the thing that renders a
table. The page carries a "read-only" badge saying so.

## Details worth knowing
- **`t1_quantity` is counted.** Shares bought yesterday sit in T1 until
  settlement; omitting them understates what you actually own.
- **Positions use `net`, not `day`.** `day` double-counts anything carried
  over, so the same trade appears twice and the P&L is wrong.
- **Broker calls cached 60s** — Kite rate-limits, and holdings change on
  settlement rather than tick by tick. `?refresh=true` forces a re-read.
- **The chandelier uses the rolling stop, not the from-entry ratchet**, because
  a holding's true entry date isn't in the Kite payload — `average_price` is
  blended across every purchase. The rolling stop answers "where would I get
  out if I were managing this actively", which is what the column is for.
- **`sync-watchlist` is additive only.** Something you own should be something
  you're watching, since signals, events and news all iterate the watchlist.
  It never removes, because an entry you added on purpose shouldn't vanish when
  you sell.

## Fixed while building it
`sync_watchlist` crashed with **SystemExit** when a holding wasn't in the local
instrument index — `watchlist.add` raises it, and `except Exception` doesn't
catch it. That happens with a stale index, a BSE-only holding or a recent
listing, and it aborted the sync for everything else you own. Third instance of
this class; the audit doesn't catch it because the call is one level down.

## Tests — tests/test_portfolio.py (28)
Read-only guarantee, only-read-methods-called, totals arithmetic, T1 counted,
zero rows dropped, closed positions dropped, short side and P&L sign, weights
sum to 100, net-not-day, signal stance from stored state, annotations never
raise on missing data, chandelier computed and absent, all three flag types,
missing session becomes PortfolioError rather than a process exit, broker error
handled, caching, watchlist sync additive/idempotent/survives-unknown-symbol,
phone rendering, and the command is not in WRITE_CMDS.

---

# TickerForge — hardening: the SystemExit class, and a command smoke test

Two classes of bug closed rather than two instances. Three real crashes in one
week came from these — /fundamentals broken since it shipped, the daily guard
crashing every trade close, the compare route 500ing before login — and every
one was found by accident. 279 tests, up from 213.

## Class 1 — SystemExit through `except Exception`
`config.require()` raises **SystemExit**, which inherits from BaseException,
not Exception. It sits behind `kite_auth.get_kite()` and `notify.send()` —
the two calls nearly every long-running path touches.

The failure mode is inverted, which is why it went unnoticed: these blocks were
written defensively *specifically so they wouldn't crash*, and they crash only
in the exact conditions they were written to survive — an expired token in the
morning, an unconfigured ntfy topic.

**22 unsafe sites found and fixed:**
- `watcher.py` ×5 — the process whose whole job is running unattended when the
  token dies. Its main loop would have exited, leaving every armed alert dead.
- `commands.py` ×7 — including the remote listener's reconnect loop, so a
  missing topic silently killed two-way phone control.
- `watchlist.py` ×3, `signals.py` ×2, `cli.py`, `fundamentals.py`
- `server.py` ×2 — converted to **HTTP 401** rather than swallowed, so the UI
  can prompt a login instead of showing a 502.

Also `brief.compose()`: its per-section isolation caught Exception only, so one
section hitting a missing session took the whole brief down. Found by a
behavioural test, not the static sweep — the sweep can't see indirect calls.

## Class 2 — commands never exercised
`/fundamentals` read a dict shape that had never existed. `summary()` and
`compute()` both had tests and both were fine; the bug lived in the gap
between them, and no test had ever invoked the command against stored data.

## New — tests/_systemexit_audit.py
Reusable AST sweep. Flags `try` blocks that call something SystemExit-capable
and catch only `Exception`. Understands that a preceding `except SystemExit`
makes a block safe, so it doesn't flag the server routes that already convert
to 401. Runnable standalone: `python tests/_systemexit_audit.py`.

## New — tests/test_systemexit_safety.py (6)
The static sweep as a test (fails with file:line for any new site), plus a test
asserting `config.require` still raises SystemExit — if that ever changes, the
audit should be retired rather than quietly guarding nothing. Plus behavioural
checks: a trade close, a watchlist auto-fetch and the brief all survive a
missing session and a missing topic.

## New — tests/test_commands_smoke.py (60)
Every one of the 41 dispatchable phone commands against a populated fixture DB,
with candles, Kite, ntfy and news stubbed. Deliberately shallow — it asserts
each command returns something plausible rather than pinning wording; deep
assertions stay in the per-feature modules. Also: bare commands explain
themselves, unknown symbols are handled, the log→amend→close sequence works,
and nothing escapes `handle()` (the listener has one try/except, so a raise
there drops the reply silently).

**Verified against the real bug.** Reinstating the original `cmd_fundamentals`
verbatim makes it fail on `/fundamentals BEL` and `/fund BEL`; restoring the
fix makes it pass. A regression test that doesn't catch its own bug is worthless
— the first attempt at this didn't, and was rewritten.

---

# TickerForge — Phase 1.4: pre-market brief

One composed push before the open. New `app/brief.py`, a `brief` CLI command,
a `/premarket` phone command, and an 08:45 hook in the events scheduler.
213 tests.

## What's in it, in order
1. **Positions** — open trades with live R. What you already own outranks
   anything you might buy, so it leads.
2. **Risk** — yesterday's realised R, today's budget, R already live, and what
   1R is worth in rupees.
3. **Tape** — Nifty vs its 200-DMA plus the share of your watchlist above
   theirs, resolved to risk-on / neutral / risk-off. This is 2.1's core folded
   in early: it costs nothing here and is read at the right moment, before the
   first trade rather than after it. Risk-off adds "long breakouts bleed in
   this tape".
4. **Results due today** — the one scheduled event that can gap a held name
   through its stop.
5. **Alerts near their trigger** — armed alerts within 2% of the last close,
   ranked by proximity.
6. **Signal stance changes** from recent closes.
7. **Overnight headlines**.

Each section is isolated. A dead source costs you that section and is listed
under "(unavailable: …)" — a brief that fails to arrive is worse than one
missing its news line.

## Not included, deliberately
The roadmap wanted watchlist gaps from GIFT Nifty direction. GIFT Nifty trades
on NSE IX and isn't in the Kite instrument dump — verified, no such instrument.
Rather than invent a number, section 5 lists the alerts sitting closest to
firing, which is the actionable half of what a gap forecast would have said.

## Scheduling
`events run` sends it once per weekday at `--brief-at` (default 08:45). That
process is chosen because it's the one already awake before the open; the
intraday watcher idles until 09:15. Date-guarded in the DB so a restart can't
double-send, and the day is marked BEFORE composing — composing hits several
network sources, and a retry storm at 08:45 is worse than a missed brief. The
failure path pushes, so a miss isn't silent.

`maybe_send` matches a 45-minute window rather than an exact minute: the
caller's loop ticks every 20s and a badly timed restart shouldn't lose the day.

## Commands
```
python -m app.cli brief [--dry] [--skip news,signals]
/premarket    (also bare /brief; /brief SYM is still the per-stock cockpit)
```

## Fixed while building it
**`/api/journal/compare` would 500 without a Kite session.** `backtest.run`
calls `config.require`, which raises SystemExit — a BaseException, so the
route's per-symbol `except Exception` didn't catch it. No session is the normal
state every morning before login. Now caught; the backtest column empties with
a note instead. Same class as the guard bug in the previous entry, found
because a test-ordering change exposed it.

## Tests
+ tests/test_brief.py (24): composition never raises on an empty DB, a failing
section doesn't take the brief down, ordering, live R, regime reads both
directions and degrades without history, alert proximity ranking, stale signal
state ignored, weekday-only, fires once, window not instant, marks before
composing, survives a dead ntfy, and `/brief SYM` still routes to the stock
cockpit rather than the morning brief.

Also: test modules each set `config.DB_PATH` at import and `db.connect()` reads
it at call time, so import order decided which DB a test saw. test_brief now
re-pins its path in `setup_function`. Worth doing to the other modules if this
bites again.

---

# TickerForge — the daily loss guard now pushes

Completes roadmap 1.1, which specified "the watcher counts realised R from
journal entries and pushes a 'stand down' notice when breached". The guard
existed but was pull-only: it fired only when you ran /size or /journal —
backwards for a feature whose whole purpose is to reach you when you're least
likely to go looking for it.

## New — app/journal.py
- `notify_if_breached()` — pushes a stand-down notice the first time the day's
  limit is breached. Deduped per day via a `meta` flag, so it survives a
  watcher restart; without persistence a crash-restart loop would re-push the
  same notice all afternoon. Idempotent and safe to call from anywhere.
- `stood_down_today()` — lets other surfaces annotate rather than re-push.
- Only the LOSS limit pushes. Hitting max-positions is a visible state you can
  see coming, not news worth a buzz.
- A failed push deliberately does NOT mark the day as notified, so a transient
  network blip can't silently eat the day's only warning.

## Wired in three places
- **`close_trade`** — the only moment realised R changes, so the earliest the
  limit can be newly breached and the moment the notice is most useful.
- **Both watcher loops** — trades close from the phone, CLI and web, none of
  which run in the watcher, so the limit can be breached without that process
  seeing it. Re-checked each cycle; the dedupe makes it free.
- **`_fire`** — alerts arriving after the limit carry "⛔ daily loss limit
  already reached today". An alert is a nudge toward a trade; when you're
  deciding whether to make it back is exactly when the reminder must be on the
  alert's face rather than a page you'd have to check.

## Fixed while building it
`notify.send` calls `config.require`, which raises **SystemExit** — and
SystemExit inherits from BaseException, not Exception. The first version of the
guard hook caught only `Exception`, so on a machine without NTFY_TOPIC set,
every single trade close would have crashed. Caught by two pre-existing journal
tests going red. All three call sites now catch `(Exception, SystemExit)`.

## Nothing is blocked
Open positions are untouched, and you can still log trades. The guard's job is
to make ignoring the limit a visible choice, not to take the decision away.
The notice says so explicitly rather than scolding.

## Tests
+ 10 in tests/test_journal.py: clean day silent, notice on breach, auto-push on
close, once per day, dedupe survives restart, resets next day, open risk can
trip it, position count alone doesn't, failed push doesn't mark as notified and
doesn't break the close. 189 tests total.

---

# TickerForge — /fundamentals returned "no ratio fields stored" for everything

Not a data problem. `cmd_fundamentals` read a dict shape that has never
existed, so the command failed identically for every symbol whether or not
fundamentals were stored.

## The bug — app/commands.py
```python
r = s.get("ratios") or s          # summary() has no "ratios" key -> r = s
v = r.get(k)                       # "pe","roe","roce","de","pb","div_yield"…
```
`summary()` returns `{symbol, variant, fetched_at, stale, snapshot, computed,
quarters}`. `snapshot` holds screener's header fields under their own display
labels ("Stock P/E", "ROCE", already formatted with ₹ and %); `computed` holds
our derived ratios ("ROE %", "Debt / Equity", "Sales CAGR 3y %"). None of the
short keys the command looked for exist anywhere, so `out` never grew past its
header and every call fell through to the fallback string.

Verified against a live DB: BEL had 9 snapshot fields and 10 computed ratios
and still reported "no ratio fields stored".

## Fixed
- Reads `snapshot` and `computed` by label. Screener's header first in a
  phone-readable order, then our computed ratios, then variant + fetch time.
- Unrecognised snapshot fields are surfaced rather than dropped, so a screener
  layout change adds a line instead of silently losing one.
- Flags stale data (the `stale` flag was already computed and never shown).
- Empty case now points at `cli fund diagnose SYM` instead of a dead end.

## Tests
+ 5 in tests/test_fundamentals_diag.py. The key one pins the `summary()` shape
and asserts `"ratios" not in s` — the mismatch was a silent contract break, so
the contract is now the thing under test. Confirmed they fail when the original
bug is reintroduced (2 failed) and pass when it isn't. 179 tests total.

## Worth knowing
- `/valuation`, `/brief` and the web pages were never affected — they read
  `summary()` correctly. Only the phone's `/fundamentals` was broken.
- Banks will show a nonsensical "ROCE % (ours: EBIT / eq+debt)" — HDFCBANK
  reads 50.1%. That's the metric not applying to lenders, not a fault.

---

# TickerForge — fundamentals: silent fetch failures

A stock could land on the watchlist with no fundamentals and nothing would say
so. Three changes: the failure now reaches your phone, a diagnostic names which
stage broke, and a slug override fixes the most common cause.

## The bug
`watchlist.ensure_fundamentals` caught every fetch exception and reported it
with `print()`. On the web add path that runs on a background thread, so the
message went to a server terminal — the stock appeared on the watchlist looking
healthy and the fundamentals silently never arrived. Evidence in a live DB:
one watchlist symbol with 0 rows in `fund_snapshot` and `fund_series` while
every other had 564.

## Fixed — app/watchlist.py
- A failed background fetch now pushes to ntfy with the error and the diagnose
  command, instead of only printing.

## New — app/fundamentals.py
- `slug_for()` / `set_slug()`: screener.in's URL code is usually the NSE ticker
  but not always — some companies are keyed differently, BSE-only names by
  numeric scrip id. When they differ every fetch 404s. Overrides live in `meta`
  so a bad mapping is a one-line fix, not a code change.
- `fetch_html` now builds URLs from the slug.
- `diagnose()`: walks URL → HTTP → page shape → section parse → ratio inputs →
  computed output, and names the first stage that broke with the fix for it.
  Five different failures previously surfaced identically as "nothing stored".

## New — CLI
```
python -m app.cli fund diagnose GRSE
python -m app.cli fund slug GRSE 533250     # then: fund fetch GRSE
python -m app.cli fund slug GRSE            # show current mapping
```

## Tests
+ tests/test_fundamentals_diag.py (9): slug default, override round-trip,
override actually used in the URL, diagnose naming each of the three failure
stages, healthy path, compute from a full parse, and a failed auto-fetch
reaching the phone. 174 tests total.

## Worth knowing
- Banks and NBFCs legitimately produce fewer ratios — ROCE and Debt/Equity
  don't apply to lenders, so a short list there is correct, not a fault.
- `diagnose` makes a live request to screener. Don't loop it.

---

# TickerForge — Phase 1.1 + 1.2: risk sizing and the trade journal

Position sizing, a trade journal priced in R, a daily loss guard, and the
live-vs-backtest comparison the journal exists for. New: `app/risk.py`,
`app/journal.py`, `app/web/journal.html`, a `trades` table, 5 phone commands,
2 CLI commands, 7 API routes. 165 tests, all offline.

Built as one unit rather than sequentially because 1.1's daily loss guard
counts realised R from 1.2's journal entries — the guard can't be finished
without the journal existing.

## New — app/risk.py
Pure functions, no DB or config reads, so the same code serves `/size`, the
Desk widget, sizing inside the simulator (3.5) and the bot's hard limits (6.3).

- `size()` — quantity from entry, stop, account and risk %. Rounds DOWN always;
  rounding up would risk more than you chose.
- `atr_stop()` — volatility-based stop when there's no structural level.
- `targets()` — price at 1R/2R/3R, so you can see before entering whether a
  plausible target is even 1R away.
- `r_multiple()` — the unit the journal and the backtester's expectancy share.
- `check_limits()` — the daily guard, counting open risk as well as realised.

**A design error the tests caught.** The first version capped every position at
25% of capital and silently shrank an ordinary trade: entry 405, stop 398, 1%
of ₹5L wants 714 shares worth 58% of the account. That's not a mistake —
position value = risk% ÷ stop-distance%, so tight stops legitimately need big
positions. The cap was quietly reducing real risk below what was asked for,
which defeats the point of choosing a risk number. Split into:

  * **hard cap** (`MAX_POSITION_PCT`, default 100) — capital you don't have. A
    0.25% stop demands 400% of the account; that order can't be placed. Reduces
    size and reports how much less you're now risking.
  * **concentration warning** (default 30%) — affordable but large. Flags,
    never shrinks. You're told "58% of capital in one name" and you decide.

## New — app/journal.py + `trades` table
- `open_trade` / `close_trade` / `update_trade`, open positions with live R,
  closed history, `stats()`, `by_tag()`, `realised_r_today()`, `guard()`.
- `import_backtest()` loads a simulated trade list for side-by-side review,
  marked `source='import'` so it can't contaminate live expectancy.
- Schema mirrors the backtester's trade dict (entry/exit/entry_time/exit_time/
  reason) so 3.5's CSV export is a mapping, not a migration.

**A second bug the tests caught, and the more dangerous one.** `update_trade`
overwrote the stop, so a trade entered at 400 with a 390 stop, trailed to 395
and exited at 420 scored **+4R instead of +2R**. Every trailed winner would
have been inflated — and trailing is exactly what the `trail` alerts encourage,
so that would have been most of them. Fixed by splitting the column:

  * `stop` — moves as you trail it, stays visible.
  * `initial_stop` — written once, and what every R is measured against:
    closing R, live R, all of it.

Migration backfills `initial_stop` from `stop` for any existing journal.

**R is net of charges.** Costs become a per-share price drag before R is
computed, so a winner shrinks and a loser deepens. Pass real contract-note
charges if you have them; otherwise it estimates at 10 bps/side — the
backtester's default, so both sides of the comparison start from the same
assumption. A journal recording gross R against a backtest recording net would
make live look systematically better than backtested.

**Trades without a stop are recorded but unscored** — they can't produce an R,
so they're excluded from every statistic and counted separately. Recording an
undisciplined trade beats refusing to record it; it just shouldn't quietly
pollute expectancy.

## New — phone commands
```
/size SYM entry=405 sl=398      quantity, 1R, position %, targets
/log buy SYM 405 sl=398 tag=st_adx    qty omitted → sized from your risk %
/close ID 419 reason=target     R, charges, and today's remaining budget
/amend ID sl=411                trail the stop
/journal [SYM] · /journal stats by tag
```
Arguments parse in any order. `/size` is deliberately NOT a write command — it
calculates and returns a number, touching nothing, so it stays usable when
NTFY_CMD_PIN is set. A test asserts it leaves the journal empty.

The guard surfaces where it's useful: after two stop-outs, `/size` prepends the
refusal as you're about to size the next trade, rather than burying it in a
page you'd have to go and check.

## New — /journal page + API
- `GET/POST /api/journal`, `POST /api/journal/{id}/close`, `PATCH`, `DELETE`
- `GET /api/risk/size`, `GET /api/risk/atr-stop`
- `GET /api/journal/compare` — **the point of the journal**: for each tag that
  matches a strategy key, runs that strategy over the symbols you actually
  traded it on and reports both sets of numbers.

The page leads with the day's risk budget as a bar, because the number that
should change behaviour is the one you see before you start looking for reasons
to take another trade. Trailed stops show "(from 398)" so the R basis is never
ambiguous.

The compare table ships with its caveats attached rather than in a footnote:
backtest expectancy is %/trade and live is R; the backtest takes every signal
and you didn't; and any tag with under 20 live trades is labelled "too few to
conclude much" on its own row. A gap is information, not automatically a
decayed edge.

## New — CLI
```
python -m app.cli size --entry 405 --sl 398 [--risk 1.5] [--account 500000]
python -m app.cli size BEL --entry 405            # stop suggested from ATR
python -m app.cli journal open BEL --entry 405 --sl 398 --tag st_adx
python -m app.cli journal list | close ID --exit 419 | amend ID --sl 411 | stats
```

## Config (.env)
`ACCOUNT_SIZE` · `RISK_PCT` (1) · `DAILY_LOSS_R` (2) · `MAX_POSITIONS` (5) ·
`MAX_POSITION_PCT` (100). Without ACCOUNT_SIZE, `/size` says so rather than
guessing; it can still be overridden per-command.

## Worth knowing
- The comparison only becomes meaningful now. Before the backtest fixes, it
  counted open positions as wins and measured win rate before costs — live
  would have been compared against a yardstick that flattered itself.
- Tag trades with a key from STRATEGIES (`st_adx`, `supertrend`, …) or they
  won't appear in the comparison. Untagged trades still count in overall stats.
- `httpx` is needed for the API tests (TestClient) and is not in
  requirements.txt; those tests skip cleanly without it.

---

# TickerForge — missed flips, SQLite concurrency, stale candles

Closes the three items left open in the previous entry, plus one found while
fixing the first. No new dependencies, no config, no schema change.
SQLite switches to WAL on first connect — see the note at the bottom.

## Fixed — app/signals.py
- **Flips that happened during downtime were lost.** `scan` detected a flip as
  `pos[-1] != pos[-2]` — a comparison of the last two bars — and used stored
  state only to dedupe. If the watcher was down Monday to Wednesday and the
  flip landed on Tuesday, Thursday's scan saw long-vs-long, reported nothing,
  and wrote the new stance to state as though it had been handled. A long
  weekend, a crashed process or a morning without a login was enough. Flips
  are now detected against the LAST RECORDED STANCE, so the gap can be any
  length. First sighting of a new symbol/strategy pair still falls back to the
  bar-to-bar test — there's no recorded stance to compare against, and
  reporting the whole history as one flip would be noise.
- **A recovered flip now says how old it is.** `evaluate` reports `since` (the
  bar the current stance began on) and `bars_ago`. Pushes carry the flip's own
  bar and close plus the current price — "flipped LONG @ 110 (3 bars ago; now
  113)" — because a three-day-old signal priced at today's close is how you
  size an entry off a level the market has already left.
- **Checking /signals suppressed that evening's EOD push.** `scan(push=False)`
  still wrote state, and state was the dedupe key, so glancing at the phone or
  the web page in the afternoon consumed the flip and the 15:35 push went out
  without it. New `record` argument, defaulting to `push`: state means "the
  last stance we told you about", so read-only paths no longer advance it.
  Both existing callers (`summary_text`, the server route) pass push=False and
  are fixed by the default.
  The /signals heading changed from "Fresh flips" to "Unreported flips",
  which is what it always meant.

## Fixed — app/db.py
- **No WAL, no busy timeout, four writing processes.** SQLite's default
  rollback journal lets a page render block the watcher recording an alert
  firing, and the default busy timeout is zero — the loser raises `database is
  locked` immediately rather than retrying. `connect()` now sets
  `journal_mode=WAL`, `busy_timeout=5000` and `synchronous=NORMAL` (durable
  against process crashes, which is the right trade for a rebuildable cache of
  market data). Pragma failures are caught and logged rather than fatal, so an
  old SQLite or a filesystem that won't do WAL degrades instead of crashing.

## Fixed — app/watcher.py
- **The indicator cache served stale candles forever.** `_IndCache.candles`
  caught every fetch exception with a bare `pass` and kept the previous bars.
  Right for a blip; wrong indefinitely — indicators computed on frozen candles
  with a live price patched onto the end are confidently wrong rather than
  merely late, and nothing said so. It now tracks the last successful fetch:
  up to 2 bars behind it still serves (one failed call shouldn't drop an
  alert), beyond that it returns nothing, which makes `bundle()` return None
  and skips the alert rather than firing it on bad data. Going stale pushes to
  the phone; recovering clears the warning.
- New module-level `_warn(key, msg)` — terminal plus phone, deduped per key,
  push failures swallowed. `run_ticks`'s nag now uses it.

## Tests
- + tests/test_db.py (4): WAL on, busy timeout set, a writer blocked by
  another connection waits and succeeds rather than raising, pragma failure
  degrades gracefully.
- + tests/test_watcher.py (6): fresh serve, brief failure keeps serving,
  persistent failure stops serving and warns, recovery clears the warning,
  warn dedupe, warn survives a dead network.
- + 4 in tests/test_signals.py: flip recovered after a 3-bar gap with the
  right price and staleness, unchanged stance after a gap stays quiet,
  read-only scan doesn't consume the flip, `evaluate` reports the stance start.
- 59 tests, all offline, stable across repeated runs and both module orders.

## Worth knowing
- **WAL creates `tickerforge.db-wal` and `-shm` next to the database.** Add
  them to any backup or sync routine (the nightly-maintenance job in Phase 5.3
  should copy via `VACUUM INTO` or `.backup`, not `cp`, or a copy taken
  mid-write will be missing the WAL contents). WAL is a property of the file
  and persists once set.
- **The first scan after this may push a burst of flips** — anything that
  flipped while state was last written but never reported will now be found.
  That's the backlog draining, not a fault. Each will be marked with how many
  bars ago it fired.
- EOD pushes may now include older flips generally. If you'd rather suppress
  anything beyond N bars, the filter belongs in `scan` on `bars_ago`.

---

# TickerForge — watcher liveness + command channel security

Closes the three silent-failure paths in the tick watcher and stops the
command channel from lying about what it can do. New optional .env keys:
`NTFY_TOKEN`, `NTFY_CMD_PIN`. Both default to empty and change nothing if
unset — existing setups keep working exactly as before.

## Fixed — app/watcher.py (run_ticks)
- **The EOD signal scan never ran here.** `signals.maybe_eod_scan()` lived only
  in the polling `run()`, so anyone using the tick watcher — the lower-latency
  mode, the one worth running — silently got no daily signal push at all. Now
  called each cycle, and deliberately *before* the market-hours gate: the scan
  fires at 15:35 and `should_watch()` goes false at 15:30.
- **A dead Kite session went unnoticed.** The module docstring promised a
  heartbeat that "notices when the Kite token has expired ... instead of going
  silently blind"; that only existed in `run()`. The tick watcher now re-reads
  the cached token every cycle and pings the phone once a day when it's gone.
- **A morning login didn't revive it.** The access token was captured once at
  startup, so a watcher left running overnight reconnected forever with
  yesterday's credentials even after you logged back in. The ticker is now
  rebuilt when the cached token changes — no restart.
- **The stream could be up and silent.** New tick heartbeat: subscribed
  symbols, market open, nothing arriving for 5 minutes → one push (repeat
  suppressed for 30 min). The clock resets at the open so an overnight
  silence doesn't read as a stall.
- `on_connect` now clears `sub_tokens` before subscribing — a fresh socket
  carries no subscriptions, and the old code could believe it was subscribed
  to symbols the new connection had never asked for.
- Added an `on_error` handler; errors were previously invisible.

## Fixed — app/signals.py
- `maybe_eod_scan` marks the day done *before* running (by design — it stops a
  retry storm), so a failure is never retried. It printed to a terminal nobody
  is watching. It now pushes the failure to the phone, with the push itself
  wrapped so a dead network can't take the watcher loop down.

## Fixed — app/commands.py
- **The security docstring was false.** It claimed the command topic was
  "READ-ONLY market data — no orders, no writes — so the worst a leaked topic
  allows is someone querying stock info." `/alert` and `/unalert` are writes:
  a leaked topic lets someone disarm the trailing stop you're relying on.
  Replaced with an accurate note and the three things that follow from it.
- `WRITE_CMDS` — the read/write split is now explicit in code rather than
  implied, so it's greppable when Phase 6.2 wants to add an order path.
- `NTFY_CMD_PIN` (optional): write commands require `pin=XXXX` anywhere on the
  line. The PIN is stripped before dispatch, and redacted from terminal logs.
  Weak by construction — anyone subscribed can read a PIN out of a command you
  send inside ntfy's retention window — and documented as such. It stops the
  screenshot/shoulder case, not a subscribed attacker.
- `NTFY_TOKEN` (optional): ntfy access token, sent as a Bearer header on
  pushes, image pushes and the command stream. This is the actual fix where
  the server supports auth; the PIN is the fallback for plain ntfy.sh.
- The listener prints which of the three protection levels is in force at
  startup, and logs write commands as `WRITE` — an unexplained WRITE in that
  log is your leak indicator.

## Tests
- + tests/test_commands.py (10): writes pass untouched with no PIN configured,
  refused without/with a wrong one, allowed with the right one regardless of
  position, reads never gated, `pin=` stripped from handler arguments,
  WRITE_CMDS ⊆ dispatch tables, redaction.
- + 2 in tests/test_signals.py: EOD failure reaches the phone; a failing push
  doesn't propagate.
- 45 tests, all offline.

## Still open from the review
- Flips missed while the watcher is down are still silent: `signals.scan`
  detects a flip as `pos[-1] != pos[-2]` and uses the stored state only for
  dedupe, so a Tuesday flip found on Thursday reports nothing. The fix is to
  compare against `prev_state`, which is already read.
- `db.connect()` still sets no WAL and no `busy_timeout` with four processes
  writing.
- `_IndCache.candles` still swallows fetch errors and serves stale candles
  indefinitely.

---

# TickerForge — backtest accuracy fixes

Four corrections to the numbers the backtester reports. No new dependencies.
DuckDB gains one small table (`coverage`), created automatically on next use;
SQLite is untouched. Existing saved backtests will report *different* stats
after this — that's the point, the old ones were wrong in the ways below.

## Fixed
- app/backtest.py   **Gap through the target was recorded as a stop-out.** Exit
                    checks ran stop-gap → stop-intrabar → target-gap, so the
                    intrabar stop test pre-empted the target gap. A bar opening
                    above the target and then selling off booked a stop loss.
                    Now both gap tests (the open is the bar's first traded
                    price) resolve before either intrabar test. Measured on a
                    trade entered at 100 with a 110 target and 95 stop, where
                    the next bar opened at 120 and fell to 90: was −5.0%
                    "stop", now +20.0% "target (gap)".
- app/backtest.py   **The still-open position counted as a completed trade.**
                    `_trade_stats` iterated every trade including the one
                    flagged `open`, so an unrealised mark-to-market voted in
                    win rate, profit factor and expectancy — free wins for any
                    strategy holding into a rising close. Completed trades only
                    now; the open one stays visible in the log and in a new
                    `open_trades` count.
- app/backtest.py   **Win rate and profit factor were gross, expectancy net.**
                    A trade could show as a win and a money-loser at once (at
                    20 bps: gross +0.2%, net −0.2%, win rate 100%, expectancy
                    −0.2%). Every trade statistic is now measured on `net_pct`
                    — win/loss split, profit factor, avg win/loss, best/worst.
                    The trade log still shows both columns.
- app/candles.py    **`ensure()` never backfilled earlier history.** It topped
                    up forward from `max(ts)`; the `days` argument only applied
                    to an empty store. A store seeded by a 200-day backtest
                    answered a 5-year request with 200 bars, silently — `run()`
                    only raises below 30. Now fills both ends: back to the
                    requested start and forward to now.
                    + `coverage` table: the earliest start already requested per
                    (token, interval), so a window the exchange has no data for
                    (pre-listing) is asked for once rather than on every chart
                    load.

## Also
- app/web/backtest.html  Field guide: profit factor described as gross ÷ gross
                        (now net ÷ net); "10 bps ≈ 0.1% round trip" was half the
                        real figure (0.2%); notes that open positions are
                        excluded from the stats. Trades card shows "+N open".
- tests/            + test_candles.py (4 tests: whole-window fetch, backward
                    backfill, forward-only top-up, pre-listing asked once),
                    + gap-ordering both directions, open-trade exclusion,
                    net-basis win classification. test_trade_stats updated —
                    it asserted the old gross profit factor. 33 tests, offline.

## Worth knowing
- Any strategy you previously judged on win rate is now measured on a stricter
  ruler. Expect win rates to drop by roughly the share of trades whose gross
  gain was smaller than the round trip, and profit factors to fall with them.
- Re-run anything long: the first `ensure()` per symbol after this will pull
  the history that was quietly missing, which is a rate-limited Kite call per
  chunk. A five-year daily window is one or two chunks per symbol.

---

# TickerForge — three new full pages

Drop these files into your repo (paths shown relative to project root).
No new dependencies. No DB migration. Existing chart page untouched except a
small nav strip in the sidebar.

## Changed (backend)
- app/server.py       + routes: /fundamentals /news /actions /app.css and
                        APIs /api/fundamentals/full, /api/news, /api/events,
                        POST /api/events/poll. (imports news, events)
- app/fundamentals.py + full(symbol): summary() plus every stored statement
                        table (quarters, P&L, balance sheet, ratios, cash flow).
- app/news.py         + aggregate(hours): live watchlist news grouped by symbol,
                        does NOT touch seen_news (won't disturb the push digest).
- app/events.py       + recent(limit,source,category,symbol): query over
                        seen_events, newest first.

## New (frontend)
- app/web/app.css          shared dark theme + top nav + components
- app/web/fundamentals.html search a stock -> snapshot cards, 52W range,
                            our computed ratios, tabbed full statement tables,
                            refresh-from-screener button. Deep-links: /fundamentals?symbol=BEL
- app/web/news.html        live watchlist news, window selector (24h/48h/72h/7d)
- app/web/actions.html     stored corporate filings, filter by category / feed /
                            symbol, watchlist tag, "Poll NSE now" (no phone push)
- app/web/index.html       + sidebar nav strip linking the four pages

## Notes
- /api/news does a live Google-News fetch per watchlist stock (~0.3s each),
  so the News page shows a loading state; it's independent of the digest.
- "Poll NSE now" calls events.poll_once(push=False): it ingests new filings
  into the DB (so they appear here) without pinging your phone.
- The corporate-actions page reads whatever the poller has already stored —
  watchlist events plus market-wide results/dividends/concalls/corp-actions.

## Update — valuation screener + metrics reference
- app/valuation.py   NEW: intrinsic-value engine (5 models: earnings-power DCF,
                      Graham number, Graham revised, justified P/B, dividend
                      discount) + screen() over watchlist. Reads stored
                      fundamentals only — no new deps.
- app/server.py      + routes /valuation /metrics, + APIs /api/valuation and
                      /api/valuation/screen (assumptions via query params).
- app/web/valuation.html  NEW: watchlist screener table (sortable, MoS + verdict),
                      adjustable assumptions bar, per-stock detail drawer showing
                      each model vs price.
- app/web/metrics.html    NEW: searchable reference of 27 metrics/concepts
                      (meaning, formula, why it matters, drivers, rule of thumb,
                      caveat) — pure static content.
- nav: added Valuation + Metrics tabs across all pages and the chart sidebar.

## Update — backtesting engine (final milestone item)
- app/backtest.py   NEW: long/flat vectorized engine, end-of-bar execution,
                    no look-ahead. 8 strategies (SMA/EMA cross, price vs SMA,
                    RSI reversion, MACD cross, Supertrend, Bollinger reversion,
                    Donchian breakout). Metrics: total return, CAGR, max DD,
                    Sharpe, Sortino, vol, win rate, profit factor, exposure,
                    avg win/loss/bars-held — all vs a buy & hold benchmark.
                    Costs charged per side (cost_bps). Formulas unit-tested.
- app/server.py     + /backtest page route, + /api/backtest and
                    /api/backtest/strategies. ALSO FIXED: /valuation and
                    /metrics page routes were missing from the prior drop —
                    added now (those pages would have 404'd on direct load).
- app/web/backtest.html  NEW: symbol + strategy + params + interval/days/cost
                    controls, stat cards vs buy&hold, price chart with entry/exit
                    markers, equity-curve chart (strategy vs hold, time-synced),
                    trade table.
- nav: Backtest tab added across all pages + chart sidebar.

NOTE: backtester uses native Kite intervals only (minute..day); resampled
timeframes (2m/week/month) aren't offered here.

## Update — all-stocks corporate calendar (Excel) + richer news
- app/calendar_export.py  NEW: market-wide corporate calendar from NSE
                          board-meeting + corp-action feeds (ALL stocks, not
                          just watchlist). build() -> rows; to_xlsx() -> workbook.
- app/server.py           + /api/calendar (JSON preview) and
                          /api/calendar.xlsx (Excel download).
- app/news.py             + Moneycontrol scrape as a 2nd news source
                          (_fetch_mc_items via MC autosuggest -> stock page ->
                          BeautifulSoup). Additive & self-contained: MC failures
                          never break the Google News path. Toggle with
                          NEWS_MONEYCONTROL=0 in .env.
- app/web/actions.html    + "Market-wide calendar" block: Preview table +
                          "Export to Excel" button.
- requirements.txt        + openpyxl>=3.1  (needed for the Excel export)

RUN: pip install openpyxl   (before using the Excel export)

NOTE: aggregate()/collapse still call _fetch_items for the live News PAGE. To
get Moneycontrol on that page too, point aggregate() at _fetch_all_sources
(needs the symbol) — left conservative for now; the push digest is unchanged.

## Update — indicator value readouts + near-real-time phone alerts
- app/web/index.html   Chart legend now shows each indicator's LIVE value, like
                       Kite: overlay chips show "EMA 21  616.53"; oscillator
                       panes show "RSI 14  52.47" (coloured per line). Values
                       track the crosshair and fall back to the last bar when
                       the cursor is off-chart. (renderIndicator stores each
                       row's time->value map; updateIndicatorValues() on
                       crosshairMove.)
- app/events.py        Market-wide results/dividends/concalls/corp-actions now
                       push INSTANTLY (batched into one grouped ntfy message per
                       poll cycle, RESULTS+DIVIDEND first, ex-dates included) —
                       no longer digest-only. Watchlist per-event push unchanged.
                       New should_poll() schedule helper (IST server clock).
- app/cli.py           `events run` is now schedule-aware: polls every 90s
                       (--every) during market hours (Mon–Fri 09:00–16:00),
                       plus evening sweeps (16:30/18:00/19:30/21:00/22:30) and
                       light weekend sweeps (Sat/Sun 11:00,18:00). --always for
                       around-the-clock. Default digests trimmed to 08:45,21:15.

RUN:  python -m app.cli events run
      (add --always to poll 24/7 at the --every cadence)

## Update — two-way phone control (query from your phone over ntfy)
- app/commands.py  NEW: subscribes to a private ntfy command topic and replies
                   with live data. Commands: /valuation SYM, /fundamentals SYM
                   (/fund), /news SYM, /results [SYM], /dividends [SYM] (/div),
                   /price SYM, /help. Uses the same modules as the app; no server
                   exposure — ntfy relays both ways.
- app/config.py    + config.get(name, default) helper.
- app/cli.py       + `remote` command → runs the listener.

SETUP:
  1. Pick a 2nd unguessable topic; set NTFY_CMD_TOPIC in .env (NOT NTFY_TOPIC).
  2. In the ntfy app, SUBSCRIBE to that topic too (so you can publish to it).
  3. Run:  python -m app.cli remote
  4. From the ntfy app, send "/valuation BEL" (etc.) to the command topic.
     Replies arrive on your normal NTFY_TOPIC.
SECURITY: read-only market data only (no orders/writes). Keep the topic name
unguessable, or put ntfy auth in front of it.

## Update — Latest Quarterly Results (screener-style card, web + phone)
- app/results.py    NEW. For any stock: is *this* quarter's result out?
                    Released -> Sales/Operating Profit/Net Profit table with
                    YoY & QoQ growth and the three comparable quarters (the
                    screenshotted layout). Not out -> just the date (board-
                    meeting/results date from the NSE events feed, or "not
                    announced"). "Current quarter" is derived from the calendar
                    (expected_quarter), so it rolls over each quarter with no
                    code change. Reads only what's already stored (screener
                    fund_series 'quarters', fund_snapshot Mcap/PE, events feed).
- app/web/results.html  NEW /results page. Watchlist ("My Companies") or all
                    stored ("All Companies"); sort by Latest / Sales / Operating
                    Profit / Net Profit YoY%; search; per-card Refresh (re-checks
                    screener so a card flips to released when the company reports).
                    Released cards mirror the screenshot; unreleased show the date.
- app/server.py     + /results page, + GET /api/results (scope, sort) and
                    GET /api/results/card?symbol=. Registered results import.
- app/commands.py   /results now returns the SAME thing on your phone:
                      /results SYM  -> the numbers if out, else the date
                      /results      -> watchlist overview (reported vs due)
                      /results all  -> the old raw market-wide filings feed
- nav               "Results" tab added across all pages + the chart sidebar.
No new dependencies, no DB migration.

## Update — auto-fetch fundamentals + results when you add to the watchlist
- app/watchlist.py  add() now fetches the stock's screener.in fundamentals by
                    default — and since the quarterly RESULTS live on that same
                    page, one fetch primes both the Fundamentals and Results
                    views. Best-effort: a screener error never breaks the add.
                    + ensure_fundamentals() (only_if_missing so re-adds don't
                    re-hit screener) and fetch_missing() to backfill stocks
                    added before this feature. Toggle: AUTO_FETCH_ON_ADD=0.
                    Optional AUTO_FETCH_NOTIFY=1 pings your phone when done.
- app/server.py     web add (POST /api/watchlist/{sym}) runs the fetch on a
                    background thread, so the add button stays instant.
- app/cli.py        `watch add` fetches inline and shows progress;
                    new `watch fetch-missing` backfills the whole watchlist.
No new dependencies, no DB migration.

## Update — Tier 1 intraday: live levels, phone alerts with chart images, Desk
The office can't reach the home box directly, so everything works over ntfy
(pull) or as a push. ntfy image attachments mean an alert arrives as a chart.

- app/intraday.py   NEW. Day-static levels (PDH/PDL/PDC, CPR TC/P/BC, classic
                    pivots R1-3/S1-3) + live snapshot (LTP, VWAP=average_price,
                    day O/H/L, %chg, gap) + opening range (ORB) + a position
                    read ("above VWAP / above CPR / below PDH"). Pure maths are
                    Kite-free and unit-tested; only fetchers hit the network.
- app/alerts.py     NEW. Arm from phone: > X | < X | vwap | pdh | pdl | orb | N%.
                    One-shot (fires once, disarms) so results season isn't a
                    buzz storm. SQLite-backed; VWAP-cross remembers its side.
- app/charts.py     NEW. matplotlib -> clean dark intraday PNG (candles + VWAP +
                    levels, triggered level highlighted) for ntfy attachments.
- app/watcher.py    NEW. Home-side loop: ONE quote() per cycle covers every
                    armed symbol; day-static levels + ORB cached. Fires alerts
                    as chart images. Session heartbeat nags once if the Kite
                    token has expired (it dies daily) instead of going blind.
- app/notify.py     + send_image() (ntfy PUT attachment; caption in Message hdr).
- app/commands.py   phone: /brief SYM (one-look cockpit), /levels SYM, /chart SYM
                    (image), /alert SYM COND, /alerts [SYM], /unalert ID|SYM|all.
                    HELP regrouped: Intraday / Alerts / Research.
- app/web/desk.html NEW /desk page — the clean cockpit: quote + position chips,
                    a levels grid, the intraday chart image, alert add/list/
                    remove, and deep-link buttons out to Fundamentals/Results/
                    Valuation/News (not duplicated). Auto-refreshes ~15s.
- app/server.py     + /desk, GET /api/intraday, GET /api/intraday/chart (PNG),
                    GET/POST/DELETE /api/alerts. (Also fixed a latent missing
                    import used by the calendar.xlsx route.)
- app/cli.py        + `intraday run` (the watcher; --every, --always),
                    `intraday levels SYM`, `intraday alerts`.
- app/db.py         + alerts table (created on next init; no manual migration).
- requirements.txt  + matplotlib.
- nav               "Desk" tab added across all pages.

RUN (at home):  python -m app.cli intraday run     # during market hours
NOTE: live/intraday quotes need the historical-data API on your Kite plan, and
the Kite token expires daily (~7:30am) — the watcher pings you to log back in.

## Update — faster, quieter polling + profit numbers in results pushes
- cli.py       `events run` poll interval default 90s -> 30s. The run loop now
               prints a cycle line only when a poll finds something new (and
               drops the "0 new" feeds from the line) — quiet unless there's
               news. Push dedup itself was already handled by seen_events.
- events.py    Watchlist results pushes now carry the numbers. New
               _result_numbers(): when a filing looks like a quarterly result
               and we hold the screener figures, the ntfy message gains a
               compact block —
                 Jun 2026 (Rs Cr):
                   Sales: 47,364  (+9.8% YoY)
                   Op Profit: 9,285  (+24.2% YoY)
                   Net Profit: 4,696  (+112.6% YoY)
                   Mcap ... · PE ...
               Best-effort: nothing appended if it isn't a result or we don't
               yet hold that quarter (numbers come from screener via
               results.card(), not the NSE feed — so keep watchlist
               fundamentals fetched/fresh for these to show).

## Update — market-wide "Latest Quarterly Results" feed (whole market)
Like screener.in's public latest-results list: every company that just
reported, with the numbers — as a sortable web list and a compact phone view.

- latest_results.py  NEW. Scrapes a public screener.in screen (default 608536,
                     set LATEST_RESULTS_SCREEN to change) — paginated table ->
                     rows (Name/CMP/PE/Mcap/Div Yld/NP Qtr + YoY%/Sales Qtr +
                     YoY%/ROCE/PAT). Cached in a new SQLite table, 30-min
                     staleness, mcap buckets, scope all|watchlist, sort
                     latest|np|sales|mcap|pe.
- server.py          + GET /api/results/latest (auto-refresh if stale) and
                     POST /api/results/latest/refresh.
- web/results.html   + a "View" toggle: My Companies (cards) vs Latest — whole
                     market (list). Market mode = sortable table with a
                     By-market-cap filter. (Screener's screen has no Operating-
                     Profit-YoY column, so that one sort is hidden there.)
- commands.py        + /results latest · /results np · /results sales ·
                     /results mine  (compact list to the phone).
- cli.py             + results fetch-latest [--sort] and
                     results latest [--sort --scope --limit].

NOTE: numbers come from screener's screen (one fetch = whole market), separate
from the per-stock cards. Run `results fetch-latest` once at home to prime the
cache (the web list auto-refreshes when stale). If that public screen ever
changes, point LATEST_RESULTS_SCREEN at another latest-results screen.

## Fix — 502 on /api/fundamentals/refresh (e.g. GRSE)
The screener fetch had no retry and treated any hiccup as fatal, so a moment of
screener rate-limiting surfaced as a blank 502.
- fundamentals.fetch_html now retries transient throttling (429/5xx) with
  backoff, handles network errors, and accepts a company page on any of several
  stable markers instead of the single "top-ratios" string (which, if screener
  renames it, would fail every fetch).
- /api/fundamentals/refresh now returns the real upstream reason and a hint
  ("screener is rate-limiting; wait a minute and retry") instead of a bare 502.
Note: GRSE's screener page is fine — the cause was almost certainly transient
throttling, made worse by back-to-back market-wide scrapes. If it recurs, space
out `results fetch-latest` from card refreshes.

## Update — every phone command works for ANY stock (not just watchlist)
Previously /results, /valuation, /fundamentals for an unstored stock replied
"no fundamentals stored — fetch first." Now the home box fetches on demand.
- commands._ensure_fund(sym): resolves the symbol, and if nothing's stored,
  pushes a quick "Fetching <SYM> from screener…" ack, scrapes it, then answers.
  Clear error only if the symbol is unknown or the scrape genuinely fails.
  Wired into /valuation, /fundamentals, /results SYM.
- /dividends and corporate actions: the events feed was watchlist-filtered at
  poll time, so off-watchlist stocks had nothing stored. Added
  events.fetch_for_symbol() — pulls the NSE feeds on demand and stores just that
  symbol's filings, so /dividends SYM works for any stock.
- /news already fetched live; unchanged.
So from your phone: /results SYM · /valuation SYM · /fundamentals SYM ·
/news SYM · /dividends SYM all work for any listed stock, watchlist or not —
the fetch runs on your Mac and the answer comes back over ntfy.

## Update — much richer valuation (reverse DCF, scenarios, quality, history)
Four enrichments, on both web and phone, with smart-default assumptions you can
override per stock.
- valuation.smart_re(mcap): cost of equity = risk-free + ERP + a size premium
  (small caps carry more), used by default; override with re=… anywhere.
- Reverse DCF: solves DCF(g)=price for the implied EPS growth, and compares it
  to the business's historical growth ("demanding" vs "conservative").
- Scenarios: bear / base / bull fair value from three assumption sets.
- Sensitivity: a grid of fair value + upside across growth × discount rate.
- Quality & risk: ROIC (vs your cost of equity), a Piotroski-style F-score
  (reports x/checked), and Altman Z (safe/grey/distress) — each computed from
  whatever line items screener gave, degrading instead of faking.
- History bands: today's P/E vs the stock's own multi-year median/min/max range
  (mean-reversion read), from annual EPS × year-end price in the candle store.
- Phone: /valuation SYM stays concise; /valuation SYM full adds all of the
  above; inline overrides like /valuation SYM full re=15 g=20 years=8.
- Web: the /valuation drawer now shows a reverse-DCF callout, scenario tracks,
  a colour-coded sensitivity grid, quality chips, and a P/E history band.
- API: /api/valuation gained rich=1.
All new math unit-tested on a seeded fixture; screener is blocked in the build
sandbox, so the live end-to-end pull is worth a quick check at home.

## Update — indicator alert triggers (Supertrend / RSI / EMA / MACD …) + low latency
New trigger vocabulary, and two delivery modes so alerts land as fast as the
data allows.

- indicators.py  NEW. Pure-maths engine (no network): SMA, EMA, Wilder RSI,
                 ATR, Supertrend, MACD, Bollinger, ADX. Returns full *series*,
                 not just the last value — that's what lets the engine detect a
                 genuine cross (compare the last two points) instead of just
                 "is above". Verified against independent calculations.
- alerts.py      New kinds + syntax:
                   supertrend | supertrend up | supertrend down
                   rsi > 70 | rsi < 30 | rsi 50 | rsi 45-55
                   ema20 | above ema20 | below ema50 | ema20x50 | zone
                   macd | bb | bb upper | adx 25 | vol 3x
                 Add a timeframe (1m 3m 5m 15m 30m 1h day — default 5m) and/or
                 `repeat` to re-arm after firing (with a one-bar cooldown so a
                 single event can't machine-gun your phone). `zone` fires on
                 moves between "above EMA20/50", "between EMA20 and EMA50" and
                 "below EMA20/50".
- watcher.py     Latency work. _IndCache caches candles per (symbol, timeframe)
                 and refetches only when a new bar opens, then patches the LIVE
                 price into the forming bar — so a cross fires the moment price
                 moves, not at the next bar close. Default poll 15s -> 5s.
                 NEW `--ticks` mode: streams via Kite's websocket and evaluates
                 on every tick (per-symbol throttle keeps CPU sane). This is the
                 lowest-latency option.
                     python -m app.cli intraday run --ticks
- commands.py    /ind SYM [tf] — RSI, Supertrend, EMAs, zone, MACD, ADX,
                 Bollinger as they stand right now. HELP lists all conditions.
- web/desk.html  Alert box accepts the new conditions, with a cheatsheet.

Examples:
  /alert BEL supertrend up 15m
  /alert BEL rsi > 70
  /alert BEL rsi 50 repeat
  /alert BEL above ema20
  /alert BEL zone            (crossing between the EMA20/50 bands)
  /alert BEL ema20x50        (golden / death cross)

## Update — timing tools: confluence filter, anchored VWAP, ATR trailing exit
Three additions aimed at *when* to act, not at generating more signals.

- confluence.py  NEW. Multi-timeframe agreement score. Each timeframe is scored
                 -1..+1 from price-vs-EMA20, EMA20-vs-EMA50, Supertrend, RSI
                 regime, MACD and price-vs-AVWAP; ADX acts as a conviction
                 multiplier (chop is discounted, strong trends amplified);
                 slower timeframes are weighted more (5m 1.0 → day 2.5). Returns
                 one score, an `aligned` flag, a verdict, and the breakdown.
                 Use it as a filter: it should stop trades more than start them.
- indicators.py  + anchored_vwap(), swing_points() (confirmed pivots),
                 anchor_index() (swing low/high, session, N bars, or the
                 absolute major low/high), chandelier(), trailing_stop() and
                 r_multiple(). The trailing stop hangs off the best price SINCE
                 ENTRY and only ever ratchets in your favour.
- alerts.py      + `avwap` — price crossing anchored VWAP. The anchor is PINNED
                   when you arm the alert; re-resolving "most recent swing low"
                   every cycle would move the line and invent fake crossings.
                 + `trail` — the exit engine. Ratchets a stop and pushes an EXIT
                   alert when hit, with the R-multiple measured against your
                   ORIGINAL stop.
                 + `conf > 60` / `conf < 50` / `aligned` — confluence triggers.
- commands.py    /conf SYM — the full multi-timeframe breakdown.
                 /exit SYM — where each trailing stop sits, distance, and R.
                 /ind SYM  — now also shows anchored VWAP and chandelier stops.
- web            /desk gains a Confluence panel (score + per-timeframe bars);
                 GET /api/confluence.

Examples:
  /alert BEL conf > 60            only when the timeframes agree
  /alert BEL aligned              when 5m/15m/1h/day all line up
  /alert BEL avwap above major    reclaim of AVWAP from the major low
  /alert BEL trail long entry=405 sl=398
  /conf BEL   ·   /exit BEL

## Update — backtest engine v2: risk exits, 8 new strategies, in-page technique explainers
The backtester grows from a signal-flipper into a proper research tool, and the
page now teaches each technique instead of just running it.

- backtest.py   Engine rewritten around a pure `simulate(candles, pos, ...)` —
                fully unit-testable without Kite. Execution model unchanged
                (position decided at bar close earns the next bar, no
                look-ahead), plus:
                * RISK OVERLAY on any strategy: fixed stop-loss %, take-profit
                  %, ATR trailing (chandelier) stop, and a max-bars time stop.
                  Checked INTRABAR on following bars — fills at the stop/target
                  price, or at the open when price gaps through it; stop is
                  assumed before target when both sit inside one bar
                  (conservative). After a risk exit the signal must go flat
                  once before re-entering, so a still-long signal can't buy
                  right back and defeat the stop.
                * Bug fix: a position opened on the very first bar is now
                  costed and marked (the old engine only noticed changes from
                  bar 1 onward).
                * Indicators now come from app.indicators — the same tested
                  maths the live alert engine uses, so a backtest and an alert
                  can never disagree about what RSI/Supertrend/ADX say.
                * Trades carry an exit reason (signal/stop/target/time/open)
                  and a cost-adjusted net return alongside the gross.
                * New metrics: Calmar, recovery factor, expectancy per trade,
                  best/worst trade, max consecutive wins/losses, longest
                  drawdown (bars), vs-buy&hold spread; plus a drawdown series
                  for the chart.
- strategies    8 new (16 total), each chosen to represent a distinct school:
                * momentum        — 6-month time-series momentum (ROC > 0)
                * macd_zero       — MACD zero-line (the slow MACD)
                * st_adx          — Supertrend gated by ADX trend strength
                * pullback_trend  — Connors RSI-2 dips above the 200-SMA
                * stoch_revert    — stochastic oversold with %K>%D curl
                * cci_revert      — CCI ±100 reversion
                * keltner_break   — close beyond EMA + 2·ATR, exit midline
                * bb_squeeze      — bandwidth-percentile squeeze, buy the break
                donchian_breakout gains Turtle-style asymmetric entry/exit
                lookbacks (entry=20/exit=10; the old single `length` is gone).
- web           /backtest gets a "How this strategy works" panel that follows
                the selector — category, plain-English summary, entry/exit
                rules, the maths, works-best-in / fails-in, and a practical
                tip for every strategy (served from the catalog, one source of
                truth in backtest.py). A collapsible "How the engine works"
                explains execution, costs, every metric, and the honesty
                caveats. New risk-exit controls, 20 stat cards, a time-synced
                drawdown pane under the equity curve, and Reason + Net columns
                in the trade log.
- server.py     /api/backtest accepts params as JSON (new strategies use names
                the old fixed args couldn't carry) + sl_pct/tp_pct/trail_mult/
                trail_period/max_bars; legacy individual params still work.
- tests/        NEW tests/test_backtest.py — 20 offline tests: look-ahead
                ordering, bar-0 entry costing, stop fills at the stop price,
                gap fills at the open, stop-before-target, time stop, trailing
                ratchet locks in profit, blocked re-entry until signal reset,
                metric maths, and every strategy validated on trending + choppy
                synthetic series. Run: python -m tests.test_backtest

## Update — signals.py: the backtest -> live bridge, +vol_breakout, field guide on /backtest
Research pass over practitioner strategy writeups (QuantifiedStrategies, NSE
swing-trading guides, screener catalogs) confirmed the library already covers
what people actually run — MA/golden crosses, 200-DMA regime, Supertrend, RSI
reversion, N-day momentum, channel breakouts — with one gap: volume
confirmation, the single most repeated breakout filter in the wild. Added.

- backtest.py   + vol_breakout (17 strategies): Donchian entry that only counts
                when volume ≥ vol_mult × its vol_len-bar average; exit at the
                exit-bar low. Volume was sitting unused in the candle store.
- signals.py    NEW. Runs the SAME strategy functions the backtester uses over
                the watchlist on CLOSED daily bars (today's forming candle is
                dropped until 15:35, so a signal can never un-happen by the
                close) and pushes flips to the phone:
                    ▲ BEL — Supertrend flipped LONG @ 412.30
                Persistent dedupe in SQLite (signal_state table): one push per
                (symbol, strategy, bar), re-scans never re-send. Scanned set is
                configurable via db meta `signals_config` JSON; default is one
                representative per school (supertrend, ema_cross,
                donchian_breakout, momentum, pullback_trend, macd_zero) — trim
                it to rules you've actually validated in the backtester.
- integration   * watcher.run() calls signals.maybe_eod_scan() each cycle: the
                  scan runs itself once per trading day after 15:35 and pushes
                  flips — no cron needed if the watcher is up.
                * /signals [SYM] phone command — fresh flips + a ●/○ stance
                  line per watchlist stock.
                * GET /api/signals (+optional symbols=, push=).
                * `python -m app.cli signals [SYM...] [--push]` for cron.
                * /backtest page gains a "Watchlist signals" panel: Scan now
                  button, flip list, and a symbol × strategy dot matrix.
- web           /backtest also gains a full FIELD GUIDE: hover tooltips on
                every control and parameter, plus a collapsible section that
                explains Symbol/Interval/Days, Cost bps (with the honest Indian
                delivery cost breakdown — STT both legs, charges, slippage —
                and why to test at 15–20), every strategy parameter by name,
                a proper ATR explainer (true range, Wilder smoothing, why ATR
                units beat fixed % stops), each risk exit, and how the stops
                interact (highest stop wins, stop-before-target, signal-reset
                rule).
- tests/        NEW tests/test_signals.py — forming-bar drop, flip detection,
                state upsert + config, scan flip→notify→dedupe cycle, EOD
                once-per-day guard. 25 offline tests total across both suites.
