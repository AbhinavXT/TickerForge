# TickerForge UI revamp — plan

Status: **done** on branch `ui-revamp`. Section 8 records what shipped, the deviations from this plan, and follow-ups.

## 1. What's there today

### Frontend shape

- 16 static pages in `app/web/`, each served by its own explicit FastAPI route
  (`FileResponse`). There is no `StaticFiles` mount, so every new asset
  (`ui.js`, `board.html`) needs its own route.
- Shared: `app/web/app.css` (151 lines, GitHub-dark tokens `--bg --panel
  --panel2 --border --fg --dim --up --down --accent --warn`), `sw.js` (PWA shell
  cache `tickerforge-shell-v1`, caches `/desk` + `/app.css`), `manifest.webmanifest`.
- There's no shared JS. The top nav (`<div id="nav">`, 17 links) is copy-pasted
  into 15 pages. The chart page has its own toolbar and no `#nav`.
- Charting: **lightweight-charts 4.2.0**, loaded from unpkg on `index.html`
  and `backtest.html` only. Desk shows a server-rendered PNG (`/api/intraday/chart`).
- Every page carries its own `<style>` block, and most redefine colours inline.

### Page inventory and API calls

| Page | File | Lines | Inline CSS | API calls |
|---|---|---|---|---|
| Chart | `index.html` | 4607 | 790 | `/api/candles` `/api/search` `/api/session` `/api/watchlist` (+`/{sym}`, `/sparks`) `/api/alerts` (+`/{id}`) `/api/drawings` (+`/{id}`) `/api/layouts` (+`/{name}`) `/api/backtest/pinned` |
| Desk | `desk.html` | 775 | 283 | `/api/intraday` `/api/intraday/chart` `/api/confluence` `/api/alerts` (+`/{id}`) `/api/search` `/api/watchlist` |
| Results | `results.html` | 647 | 45 | `/api/results` `/api/results/card` `/api/results/latest` `/api/fundamentals/refresh` |
| Valuation | `valuation.html` | 573 | 168 | `/api/valuation` `/api/valuation/screen` |
| Backtest | `backtest.html` | 2088 | 442 | `/api/backtest` (+`/batch /csv /heatmap /metrics /montecarlo /pin /strategies /walkforward`) `/api/search` `/api/signals` |
| Fundamentals | `fundamentals.html` | 309 | 0 | `/api/fundamentals/full` `/api/fundamentals/refresh` `/api/search` `/api/session` |
| Metrics | `metrics.html` | 731 | 115 | none (static reference) |
| News | `news.html` | 127 | 0 | `/api/news` |
| Corporate Actions | `actions.html` | 276 | 0 | `/api/events` `/api/events/poll` `/api/calendar` `/api/calendar.xlsx` `/api/watchlist` |
| Portfolio | `portfolio.html` | 389 | 73 | `/api/portfolio` `/api/portfolio/sync-watchlist` |
| Scanner | `scanner.html` | 611 | 113 | `/api/scanner` (+`/conditions`) `/api/scans` (+`/{name}`) `/api/universe` (+`/{name}`) `/api/watchlist` |
| Screen | `screen.html` | 318 | 96 | `/api/screen` `/api/universe` |
| Query | `query.html` | 585 | 207 | `/api/query` (+`/check /fields`) `/api/universe` |
| Journal | `journal.html` | 478 | 106 | `/api/journal` (+`/{id}`, `/{id}/close`, `/compare`) |
| Paper | `paper.html` | 289 | 60 | `/api/paper` (+`/fill /order/ /signals`) |
| Settings | `settings.html` | 356 | 126 | `/api/settings` (+`/{key}`) |

**Your brief listed 9 pages, but there are 16.** I plan to include Portfolio, Scanner, Screen,
Query, Journal, Paper and Settings, giving them the same generic treatment as Fundamentals/News
(shell, tokens, cards, tables, segmented controls). They'll get no bespoke redesign.

### localStorage keys (all on the chart page)

`tf_paneh` (pane heights), `tf_side_w` (sidebar width), `tf_events` (event
overlays). New key: `tf_theme`. No other page uses storage today.

### Chart-page keyboard shortcuts (must survive)

`/` (search), `i` (indicators), `r`, Space, Arrow keys, Enter, Escape,
Backspace/Delete (drawings), plus keydown handlers on `#cmpIn` and `#layname`.

## 2. Test baseline: it's not green yet

`.venv` created per the README (`pip install -r requirements-dev.txt`).
`pytest -q`: **68 failed, 1132 passed.** None of these failures come from the UI work. They fall into these groups:

| Cause | Count | Files |
|---|---|---|
| **Host guard returns 421.** `TestClient(app)` sends `Host: testserver`, which the loopback guard (`server.py:86`) refuses. `test_csrf`/`test_chart_extras` already pass `base_url="http://127.0.0.1:5000"`; the others don't. | ~59 | journal_api, layouts, scanner, alerts_move, batch, heatmap, walkforward, entrypoints… |
| `desk.html` doesn't match `test_pwa`: no `max-width:700px` alerts-first layout, 44px targets, 16px inputs or `offline` listener | 4 | test_pwa |
| `query.py` calls indicators that `indicators.py` doesn't have (cci, stoch, mfi, obv, vwap, keltner, willr, cmf, stddev) | 2 | test_query |
| Your uncommitted `/bars` and `/candles` commands aren't in COMMANDS.md | 1 | test_commands |
| `ACCOUNT_SIZE` leaks from the local `.env` into a test | 1 | test_commands |
| `watcher.py` doesn't call `maybe_evening_queue` | 1 | test_paper_autopilot |

The 421 group matters for this work. It includes the **page-wiring tests**: the scanner
tab on every page, the chart page wiring the layout UI, and backtest wiring
walk-forward. Those tests guard exactly what a nav/shell rewrite could break, and today they
can't run. **Proposal:** add a test-only `tests/conftest.py` change (or per-file
`base_url`) so every TestClient talks to `127.0.0.1`. The server stays untouched. I'll
fold the 4 desk/PWA tests into the Desk redesign, since the brief asks for that
anyway (44px targets, phone layout). I'd leave the query, commands, env-leak and autopilot
failures out of scope and list them as known pre-existing failures.

**There is no jsdom smoke test in the repo.** The JS tests (`tests/test_chart_js.py`)
extract functions from `index.html` and run them in bare `node`. There's no
`package.json` and jsdom isn't installed. **Proposal:** add
`tests/js/package.json` with jsdom as a **dev/test-only** dependency (never
served), plus `tests/test_shell_smoke.py`, which skips when node or jsdom is missing like the
existing JS tests do. For every page it loads the HTML into jsdom with a
stubbed `fetch` and stubbed `LightweightCharts`, runs `ui.js` and asserts:
- the shell mounts
- the active rail item matches
- the theme toggle flips `data-theme` and persists `tf_theme`
- the bottom tab bar has 5 slots, and More lists the rest
- no uncaught errors

## 3. Design system

### Tokens (`app.css`)

These are your tokens, with one correction:

- `--text-3: #6B7686` measures **4.20:1** on `--bg` and 3.80:1 on `--surface-3`, which fails 4.5:1.
  It becomes **`#7A8596`**: 5.18 on `--bg`, 4.91 on `--surface-2`, 4.68 on `--surface-3`.
- `--neutral #4A5463` is 2.5:1, which is fine for fills and bars but must never be used for text.
- Light theme: a matching set under `[data-theme="light"]`, with bull/bear darkened
  to pass 4.5:1 on white. The `prefers-color-scheme` media query picks the default, and a
  `tf_theme` value overrides it. `color-scheme` follows the theme. The existing test
  asserts the literal `color-scheme: dark`, and that stays on `:root`.
- **Compatibility aliases.** `--panel --panel2 --fg --dim --up --down --accent --warn`
  are kept as aliases of the new tokens until the last page migrates. They're then kept
  permanently for `--accent`, because `test_web_controls` asserts
  `accent-color:var(--accent)` and the literal control-rule formatting. The aliases are also why each
  per-page commit leaves every other page still working.

### `ui.js` (new, ~300 lines, vanilla, no dependencies)

- One `NAV` definition renders the left rail and top bar on desktop, and the
  bottom tab bar (Board, Chart, Desk, Results, More) plus the More sheet below 760px.
  This replaces the 15 copy-pasted `#nav` blocks.
- Pages keep a `<div id="nav" data-page="…">` mount. The existing
  `test_scanner_tab_on_every_page` checks for `href="/scanner"` in the served HTML,
  so I'll keep a static `<noscript>`/fallback link list in the markup that `ui.js` upgrades.
- Theme toggle and `tf_theme`, plus a `tf:theme` event that pages (the chart) listen to.
- Market status: LIVE/CLOSED dot, IST clock and NIFTY last price with % change, polled every 30s.
- Helpers: `toast()`, `sheet()/modal()` with focus trap and Esc, `skeleton()`,
  `errorBox(retry)`, `fmt` (mono numbers, ▲/▼ signs), `ago()` for "updated Ns ago",
  and `cssVar()` for theming charts.
- Icons are an inline SVG sprite at 1.6px stroke, with no emoji.

### Components (CSS classes)

`.card`, `.stat` (label/value/caption), `.seg` (segmented control), `.chip`/`.tag`,
`.scorebar`, `.level`, `.tbl` (sticky header, numerics right-aligned, `.tbl-wrap`
overflow box), `.sheet`, `.modal`, `.toast`, `.empty`, `.skel`, `.err`,
and `.collapsible` (`<details>`). The existing class names (`.card .k/.v`, `table.stmt`,
`.block`, `.badge.*`, `.tabs`, `.chip-toggle`, `.range52`) keep working and are restyled
through tokens, so untouched markup still looks right.

### Fonts and dependencies

- Fonts: IBM Plex Sans and Plex Mono from Google Fonts, with system fallbacks.
- Runtime dependencies: **none added.** Everything is vanilla. jsdom is test-only, as described above.

## 4. Board (new)

### Gaps found in the data layer, which need your call

1. **The instrument table has no F&O.** `config.EXCHANGES = ("NSE", "BSE")`,
   so `instruments.sync` never stores NFO rows, and the table has no `expiry` column.
   Proposal: add `"NFO"` to the sync, and add a nullable `expiry` column (migrated with an
   `ALTER TABLE … ADD COLUMN` if missing).
   - It's a backend change outside `/api/board`, but a benign one:
     `search()` defaults to `equities_only=True` and `lookup()` matches exact
     symbols, so no existing route changes its output. I checked both.
   - The cost is a bigger sync (about 80k more rows) and needing one re-sync before the Board works.
2. **OI change %.** `kite.quote` gives current OI, not the previous session's OI.
   Proposal: fetch each current-month future's previous-day OI once per day using
   `historical_data(..., oi=True)` and cache it in SQLite.
3. **Rate limits.** Fresh 5m candles for ~190 underlyings every 60s doesn't fit
   Kite's ~3 historical calls/s (a full sweep takes about 65s). Proposal:
   - LTP, OI and volume come from **one batched `kite.quote`** every 60s.
     That covers ~380 keys, under the 500-key limit.
   - Order-block, swing and liquidity detection runs on 5m candles that a background sweeper
     refreshes on a **rolling ~5-minute cycle**. It uses the existing candle store, so
     symbols already being recorded cost nothing.
   - Score and OB proximity are recomputed every 60s from the fresh LTP.
   - `/api/board/{sym}` fetches that one symbol's candles on demand.
4. **Sectors.** The only sector source is stored Screener fundamentals
   (`fundamentals.py:379`). Stocks without fundamentals go into "Unclassified".
5. **NIFTY in the top bar.** No existing route returns it. I'll add a
   light `GET /api/board/market` (status, IST time, NIFTY last/%) so the shell
   doesn't trigger a full board build on every page load.

### `app/board.py`

- Tunables are constants at the top: `K_ATR`, `EQ_TOL`, `ADX_CHOPPY = 18`, and `WEIGHTS`.
- Pure functions, Kite-free:
  - `find_displacements`
  - `order_block_for`
  - `is_mitigated`
  - `liquidity_levels` (equal highs/lows plus PDH/PDL)
  - `oi_read` (price direction × OI direction gives the 4 classes)
  - `score` (ADX < 18 scores 0)
  - `rank`
  - `breadth`
  - `sector_flow`
- These reuse `indicators.atr`, `indicators.adx` and `indicators.swing_points`.
- The I/O shell uses the quote/candle layer, with the 60s cache during 09:15–15:30 IST
  and the last-session snapshot outside those hours (persisted so a restart serves it).
- `tests/test_board.py` covers synthetic candles for each pure function, including the
  choppy → 0 case.
- Phone commands: `/board` (top 5 per side) and `/ob SYM` in `commands.py`, plus
  COMMANDS.md entries, since `test_every_phone_command_is_documented` enforces that.

### Board page

- Header: breadth bar and NIFTY.
- A Bullish/Bearish toggle over a top-20 list.
- The zone view is a drawer on desktop and a full sheet on a phone. It has:
  - a lightweight-charts 5m chart with the OB band (a custom series primitive) and right-edge price lines
  - level cards
  - the "Why it ranks" breakdown
  - "Alert on OB retest", which POSTs to `/api/alerts`
- Market X-Ray is an inline SVG scatter plus diverging sector bars.

## 5. Migration order, one commit each

Work happens on a `ui-revamp` branch, not `main`.

0. Test harness: loopback `base_url` for TestClient; jsdom smoke test scaffold.
1. Foundations: tokens, aliases, components in `app.css`, `ui.js` and its route, and an `sw.js`
   cache bump with `/ui.js` added to the shell. No page changes yet.
2. News: the smallest page, used to prove the shell.
3. Corporate Actions
4. Fundamentals
5. Results
6. Metrics
7. Valuation
8. Desk (also fixes the 4 PWA tests)
9. Backtest
10. Portfolio, Scanner, Screen, Query, Journal, Paper and Settings, one commit each
11. **Chart (`index.html`) goes last of the existing pages**, because it's the riskiest and the shell is proven by then.
12. `board.py` and `test_board.py`, with the NFO sync and expiry column
13. Board routes and `board.html`
14. Phone commands `/board` and `/ob`
15. Cleanup: hex grep, delete the aliases that are no longer used, update this doc.

## 6. Risks

### Chart page

- **The chart page is a full-viewport layout** (`height:100vh; overflow:hidden`). The rail
  and top bar take width and height away from it, so every size calculation that assumes the
  window (pane heights, `resize` → `syncScaleWidths`) has to use the
  container instead. I'll add a `ResizeObserver` on the chart host that triggers
  the existing `syncScaleWidths()`. Its logic stays untouched.
- **`padToCandles` and `syncScaleWidths`** stay byte-for-byte the same. The restyle only touches the
  `layout`/`grid`/`crosshair` options passed to `createChart`, through one
  `chartTheme()` function that reads CSS variables, applied live with `applyOptions` on
  `tf:theme`. Series colours that encode bull/bear come from tokens. Indicator colours
  (`--ma20` and so on) stay distinct series colours, re-tuned for both themes.
- **Pane drag-resize and `tf_paneh`.** These are restyled by CSS only. The pointer handlers and the stored
  format are unchanged, and a jsdom test asserts the key round-trips.
- **The indicator settings modal** gets the `.modal` styling but keeps its DOM ids
  and handlers. That's the most selector-coupled code in the file, so I'll only change classes.
- **Keyboard shortcuts.** The new shell must not steal `/`, `i`, `r` or the arrow keys. `ui.js` binds
  no global keys except Esc inside its own sheets.
- 134 hex literals live in `index.html`. Most are chart option colours, which move to
  `chartTheme()`. Drawing-tool palette colours are user data and stay as data.

### Other pages

- **Backtest's synced panes** use the same pattern, so the same `chartTheme()` treatment applies.
- **Test assertions on literal strings.** `test_web_controls`, `test_pwa`,
  `test_layouts` and `test_scanner` assert exact substrings in HTML and CSS. I'll run them after
  every commit, and won't reword a test unless it asserts styling I'm deliberately
  replacing. I'll flag every such case.
- **The service worker** caches the old `app.css`. Bumping `CACHE` in step 1 means
  installed PWAs pick up the new shell.
- **Google Fonts is an external request.** If it fails, the system font stacks take over, so nothing breaks.

### Your uncommitted work

`app/commands.py`, `candles.py`, `notify.py` and `config.py` have uncommitted changes.
The Board touches `commands.py` and `config.py`. **Please commit or stash those
first**, so my per-step commits don't sweep in your work in progress. The background code
review found 10 issues in that diff, which I'll report separately.

## 7. Verification I can and can't do

- **Can:**
  - pytest, the jsdom smoke tests, and `node --check` on every page
  - loading each page from a local `uvicorn` in a headless browser at 360, 768 and
    1440px, if a browser tool is available here, to check for horizontal scroll and both themes
- **Can't:** anything that needs live Kite data or market hours:
  - real Board rankings
  - OI classes
  - NIFTY in the top bar
  - Desk auto-refresh against real quotes


## 8. Outcome

### What changed, per page

| Page | Change |
|---|---|
| Shell (`ui.js`, `app.css`) | Tokens (dark + light, OS default, `tf_theme`), IBM Plex Sans/Mono, components: card, stat, seg, chip, delta, score bar, level card, table, drawer/modal/sheet, toast, skeleton, error, empty, collapsible. Rail + top bar (LIVE/CLOSED, IST clock, NIFTY, theme toggle); 5-slot phone tab bar + More sheet; `tf:theme` event; `TF.chartTheme()` |
| Board | New: ranked list, zone view, Market X-Ray (`board.html`, `app/board.py`) |
| Chart | Compact shell (icon rail, status + toggle in the chart header); every chart colour from tokens via `PAL`, re-themed live; SVG draw tools; phone search/watchlist sheet; **pane alignment fixed** (see deviations) |
| Desk | Cockpit grid: quote + position chips, grouped level cards with distance-from-LTP, chart image with fallback, per-TF diverging confluence bars, alerts with collapsible cheatsheet, ticking "updated Ns ago"; phone puts alerts first |
| Results | Metric-tile cards with ▲/▼ YoY/QoQ; market table; filters reachable on phones (were hidden) |
| Valuation | Sortable screener; drawer with model bars vs price, reverse-DCF callout, scenario track, single-hue sensitivity grid, quality chips, P/E history band |
| Backtest | Left control panel / phone sheet, stat grid, themed synced panes, ▲/▼ trade log, collapsible explainers and sweeps |
| Fundamentals | Stat tiles, tabbed statements, skeleton/error states |
| Metrics | Sticky TOC with scroll-spy, grouped select on phones, match count |
| News | Grouped by stock, source + time chips, jump chips |
| Corporate Actions | Filterable filings table, collapsible calendar preview/export |
| Portfolio, Scanner, Screen, Query, Journal, Paper, Settings | Shell + tokens; Query editor stacks on phones; Paper's literal `\u2019` text fixed |

### New files

`app/web/ui.js`, `app/web/board.html`, `app/board.py`, `tests/test_board.py`,
`tests/test_shell_smoke.py`, `tests/js/smoke.js`, `tests/js/package.json` (jsdom, test-only).

### Deviations from the plan

- **Pane alignment was already off.** The draw-tool column sits beside the main chart
  only (plus a stale 10px margin from an older toolbar), so oscillator panes were ~60px
  wider than the main plot and their bars landed at different x. Panes are now indented
  by the column width; plot boxes and price-axis widths match exactly. `padToCandles`,
  `syncScaleWidths` and the sync handlers are unchanged.
- **Bull/bear are blue/orange everywhere, including candles** (was teal/red). The
  Supertrend node test's fallback assertion now checks the theme defaults (`PAL`).
- **Board OB band** is a positioned overlay kept in step every frame, not a series
  primitive — lightweight-charts 4.2 has no shaded-band series.
- **Board candle freshness:** prices/OI refresh every 60s from one quote call; order-block
  detection uses stored 5m candles that a background sweep refreshes at ≈2.5 calls/s, so a
  full pass over ~190 stocks takes ~80s.
- **Palette validator:** the brief's bull `#7BD3EA` fails the dataviz lightness/chroma
  checks (it's pale) but passes CVD and normal-vision separation against bear and
  contrast; direction always carries ▲/▼ or a label as well, so it was kept.

### Tests

Baseline 68 failed / 1132 passed → now 4 failed / 1312 passed. The 4 are pre-existing
and untouched: `test_query` ×2 (indicators missing from `indicators.py`),
`test_commands` (`ACCOUNT_SIZE` leaks from the local `.env`), `test_paper_autopilot`
(watcher lacks `maybe_evening_queue`). `/bars` and `/candles` are now documented.

### Follow-ups

- ~~`/api/portfolio` without a Kite session kills the server process.~~ **Fixed**
  (`061526d`), along with `POST /api/watchlist/{unknown}`, which did the same. Every
  `/api` route was then called with the session removed; none lets `SystemExit` escape.
- Run `python -m app.cli sync-instruments` once so NFO futures (and expiries) land; the Board is empty
  until then.
- Market-hours check a live Board: OI change fills ~1 min after the first build of the day;
  scores and ranks need real data to sanity-check the weights in `board.py`.
- Some stored screener snapshots are empty strings (e.g. BDL) — a scraper issue, visible on
  Fundamentals.
- ~~Compatibility aliases~~ **Done:** every page now uses the real tokens, and the
  aliases are gone from `app.css` (only `--accent`, which native controls use, remains).

## 9. Order-block features after launch

Matched against order-block.com's feature list; rankings, liquidity score, X-Ray, zones
and the last-session board were already here. Added:

- **Liquidity traps** (`board.liquidity_traps`): the first bar after an EQH/EQL/PDH/PDL
  formed that runs it and closes back inside. A close beyond it first is a break, and the
  level is spent. Above a high it points ▼, below a low ▲.
- **Reversal build-up** (`board.reversal_setup`): price within `REVERSAL_NEAR_ATR` of its
  live OB, backed by OI on that side (`OI_SUPPORT ≥ REVERSAL_OI_MIN`) or a sweep the same
  way within `TRAP_RECENT` bars. Neither changes the score; both are badges on the list,
  notes in the zone drawer (traps also as chart markers) and suffixes in `/board`, `/ob`.
- **Chart page**: indicator *Order Blocks & Liquidity* (the newest live zones per side as
  top/bottom lines, the nearest resting liquidity dashed) and event *Liquidity trap*. It is
  a JS port of the same maths (`obScan`), PDH/PDL taken from the previous session in view
  on intraday timeframes; `tests/test_chart_js.py` runs both on the same candles and
  requires identical displacements, zones, levels and traps.
