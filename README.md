# TickerForge

A personal market platform on Zerodha Kite Connect. Charting with Kite-parity
indicators and drawing tools, a 17-strategy backtester with walk-forward and
Monte Carlo validation, an EOD scanner, relative-strength ranking, Damodaran
valuation, a risk-sized trade journal, a paper broker, and tick-latency alerts
pushed to your phone.

Runs entirely on your own machine. No account, no cloud, no third party holding
your positions.

---

## Documentation

| Where | What's in it |
|---|---|
| **[FIELD-GUIDE.md](FIELD-GUIDE.md)** | What each screen shows, how every number is calculated, and how to read it. **Start here** when a figure needs explaining. |
| **[COMMANDS.md](COMMANDS.md)** | Every CLI and phone command, with flags and worked examples. Grouped by task. |
| **[GUIDE.md](GUIDE.md)** | Deeper reference: all 28 alert conditions, config keys, the HTTP API, troubleshooting. |
| **[VALUATION-PRIMER.md](VALUATION-PRIMER.md)** | The valuation models and the thinking behind them. |
| **[Roadmap](Roadmap)** | What's built and what's next. |
| `/metrics` in the app | Searchable glossary of ~35 fundamental ratios — formula, meaning, rule of thumb, caveat. |
| `/settings` in the app | Risk model, costs, news curation and backup retention. Shows whether each value comes from the page, `.env`, or a built-in default. |

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the four REQUIRED entries
```

**Kite app** — create one at <https://developers.kite.trade>. Set the redirect
URL to `http://127.0.0.1:5000/` and login completes in one click. Historical
candles and the live tick stream need the paid Connect data subscription.

**ntfy** — install the app and subscribe to *two* long random topic names. One
(`NTFY_TOPIC`) receives alerts, the other (`NTFY_CMD_TOPIC`) is where you send
commands. They must differ, or replies loop back in as commands.

```bash
python -m app.cli login              # once a day; Kite tokens expire each morning
python -m app.cli sync-instruments   # ~90k rows; re-run nightly
python -m app.cli status             # confirm session + topic
python -m app.cli notify-test        # confirm the phone buzzes
```

---

## Running it

```bash
python -m app.cli up          # all services under one supervisor
python -m app.cli health      # or send /health from your phone
```

`up` runs the web UI, the intraday alert engine, the events poller and the
phone-command listener under one parent, restarting anything that dies with
exponential backoff and pushing to your phone when it does. Output from all
four is tagged into one terminal.

They can still be run separately if you prefer separate windows:

```bash
python -m app.cli serve         # web UI on http://127.0.0.1:5000
python -m app.cli intraday run  # intraday alert engine
python -m app.cli events run    # corporate events, news, scheduled pushes
python -m app.cli remote        # listens for phone commands
```

Only `serve` fails loudly — you notice when pages stop loading. If the alert
engine dies unsupervised, alerts silently stop firing, which looks exactly like
nothing having triggered. That asymmetry is what `up` exists to fix.

**At boot, and nightly:**

```bash
python -m app.cli install-service    # writes launchd agents, loads nothing
python -m app.cli maintain --dry     # see what the nightly job would do
```

`install-service` writes two agents and prints the `launchctl load` lines for
you to run — it deliberately doesn't register anything itself. One keeps the
supervisor alive across reboots; the other runs `maintain` at 03:00, which
backs up both databases, tops up candles and prunes old backups. On Linux it
prints the systemd and cron equivalents instead.

launchd starts the services; it can't log you in. The daily expiry push carries
a one-tap login link.

---

## The pages

| Page | For |
|---|---|
| `/` **Chart** | Candles, 18 indicators, drawing tools, event markers, alerts from the chart, compare mode, bar replay |
| `/desk` **Desk** | Intraday cockpit — VWAP, CPR, ORB, pivots, confluence, alerts. Installs as a phone app |
| `/portfolio` | Kite holdings annotated with valuation, signal stance, chandelier stop and next results date |
| `/scanner` | EOD scans across a universe: breakouts, 52-week highs, volume, squeezes |
| `/screen` | Value **and** momentum **and** signal — the intersection |
| `/backtest` | 17 strategies, risk overlay, position sizing, batch, heatmap, walk-forward, Monte Carlo |
| `/journal` | Trades in R, expectancy by tag, live vs backtest |
| `/paper` | Signals traded forward with no money at stake |
| `/valuation` | Damodaran models, scenarios, sensitivity grid |
| `/fundamentals` `/results` `/news` `/actions` | Statements, quarterly results, headlines, corporate events |
| `/metrics` `/settings` | Ratio glossary, and configuration |

---

## Where to start

The tool generates signals faster than discipline can absorb them. That was
true from the first milestone, and most of what has been built since is the
counterweight. A reasonable order:

1. **Add a few names** to the watchlist and look at charts. Get a feel for the
   indicators and the Desk.
2. **Log every trade you take**, with a consistent tag. The journal is the only
   thing here that measures *you*, and every later comparison joins on those
   tags.
3. **Set the risk model** in `/settings` — account size, risk %, daily loss
   limit — and use `/size SYM entry=… sl=…` before entering.
4. **Backtest before believing**: single run → batch across the watchlist for
   breadth → heatmap for a plateau not a spike → walk-forward for out-of-sample
   → Monte Carlo for what the bad orderings look like.
5. **Start the paper broker** and leave it alone for ~30 closed trades.
6. **Then** read the three-way comparison, and only then consider live orders.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Some tests shell out to `node` to exercise the chart's JavaScript — indicator
maths, drawing geometry, sparkline paths — directly. They skip cleanly where
node isn't installed.

---

## Data and safety

Everything lives in `data/`: `tickerforge.db` (SQLite — watchlist, alerts,
journal, drawings, fundamentals, events) and `candles.duckdb` (OHLCV). Both are
local files worth backing up; the nightly job does it for you. Nothing in this
repo transmits them anywhere.

The web server binds to loopback, refuses non-loopback `Host` headers, and
rejects cross-site writes — but it has **no login**. Anyone with access to your
desktop session has access to the app.

The ntfy command channel is protected by a PIN for write commands, but the
topic name is effectively the credential: anyone who knows it can read your
pushes. Use a long random one, and set `NTFY_TOKEN` if your server supports it.

### Live orders

Semi-automatic execution is **off** unless both `LIVE_TRADING` and `SEMIAUTO`
are set in `.env`. Neither is editable from the browser — a page with no login
is not where the trading switch belongs.

Even armed, nothing places itself. A signal *proposes* and your phone
*approves*. Approval requires the PIN, every proposal carries a single-use
token that expires in 15 minutes, and the guardrails re-run at the moment you
approve rather than only when the push was sent. A stop is attached with the
entry, and if that fails you get a `priority=max` push telling you so.

Leave all of it off until the paper broker has given you a real answer. There
is nothing to learn from enabling it early that paper won't tell you for free.

---

## A note on what this is

It reads your broker account and, if you let it, places orders. It is a
personal tool written for one user, tested but not audited, and every number it
shows rests on assumptions documented in the field guide. Read §16 of that
document — "What to distrust" — before acting on anything here.
