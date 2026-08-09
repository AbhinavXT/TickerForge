# Reading the Numbers — a plain-language primer

Every metric here is one TickerForge actually computes, so you can see it on a
real stock while you read. Where a number appears in the app, I've said so.

Educational material, not investment advice — the point is to help you form
your own judgement rather than hand you a rule to follow.

**Contents**

1. [The two questions](#1-the-two-questions)
2. [Is it a good business? — return metrics](#2-is-it-a-good-business--return-metrics)
3. [Is it growing, and profitably? — margins and growth](#3-is-it-growing-and-profitably--margins-and-growth)
4. [Will it survive? — safety metrics](#4-will-it-survive--safety-metrics)
5. [Is the price sensible? — multiples](#5-is-the-price-sensible--multiples)
6. [What is it worth? — valuation models](#6-what-is-it-worth--valuation-models)
7. [The discount rate, which is where the magic hides](#7-the-discount-rate-which-is-where-the-magic-hides)
8. [Reverse DCF — the honest version](#8-reverse-dcf--the-honest-version)
9. [How your app combines them](#9-how-your-app-combines-them)
10. [Where these numbers lie](#10-where-these-numbers-lie)
11. [Cheat sheet](#11-cheat-sheet)

---

## 1. The two questions

All of fundamental analysis collapses into two questions asked in order:

**Is this a good business?** Does it turn ₹100 of capital into a lot of profit,
year after year, without needing constant new money or heavy debt?

**Is the price sensible?** A wonderful business at a stupid price is a bad
investment. A mediocre business at a cheap enough price can be a good one.

Return metrics (ROE, ROCE, ROIC) answer the first. Multiples (P/E, P/B) and
valuation models (DCF, Graham) answer the second. Almost every mistake comes
from answering one and forgetting the other.

One rule underneath everything: **no ratio has a "good" value in the
abstract.** A number is only high or low relative to three things — the same
company's own history, its direct competitors, and the cost of the money
funding it. Get comfortable asking "compared to what?" and half the confusion
disappears.

---

## 2. Is it a good business? — return metrics

These all answer the same question with different definitions of "capital":
**for every ₹100 put into this business, how many rupees come back per year?**

### ROE — Return on Equity

```
ROE = Net Profit ÷ Shareholders' Equity
```

Equity is what the owners put in plus all the profit ever retained
(share capital + reserves). So ROE is the return on *your* money as a
shareholder.

**Higher is better.** In India, above 15% is good, above 20% sustained is
excellent, and below 10% means the business is barely beating what you'd get
from a fixed deposit after taking equity risk.

**The trap, and it's a big one.** ROE can be inflated by debt. Two companies
both earn ₹15 on ₹100 of assets. One is funded entirely by equity: ROE = 15%.
The other borrows ₹50 of that ₹100, so equity is only ₹50, and after interest
its ROE might be 22%. The second looks better and is *more fragile*. This is
why ROE alone is never enough — the higher ROE bought with borrowed money is
the one that breaks in a downturn.

**In your app:** `/fundamentals` → computed → `ROE %`, calculated as
net profit ÷ (equity capital + reserves).

### ROCE — Return on Capital Employed

```
ROCE = EBIT ÷ (Equity + Debt)
```

EBIT is Earnings Before Interest and Tax — profit before the lenders and the
taxman take their cut. The denominator is *all* the long-term money in the
business, borrowed or owned.

This is ROE's honest cousin. Because it counts debt as capital and measures
profit before interest, **leverage can't flatter it.** The two companies above
would both show ROCE = 15%.

**Higher is better.** Above 15% is solid, above 20% suggests real competitive
advantage. The number that actually matters: **ROCE must exceed the cost of
that capital** (roughly 12% for Indian equity — see [section 7](#7-the-discount-rate-which-is-where-the-magic-hides)). A
company earning 9% on capital that costs 12% is destroying value with every
rupee it reinvests, however much its profit is growing.

**In your app:** `/fundamentals` shows two versions —
`ROCE % (ours: EBIT / eq+debt)` computed from the raw statements, and
`ROCE % (screener)` as reported. They can differ; screener's definition of
capital employed varies by company. Two independent readings disagreeing is
useful information, not a bug.

### ROIC — Return on Invested Capital

```
ROIC = NOPAT ÷ (Equity + Debt)      where NOPAT = EBIT × (1 − tax rate)
```

ROCE after tax. Since you can't spend pre-tax profit, this is the most
economically honest of the three, and the one most professionals default to.

**In your app:** `/valuation` → quality → `roic_pct`, alongside
`roic_vs_re_pp` — ROIC minus your cost of equity, in percentage points.
**That gap is the single most useful number in the whole quality panel.**
Positive means growth creates value. Negative means growth destroys it, and
you should want such a company to shrink and pay you dividends rather than
reinvest.

### Which to use

Start with **ROCE or ROIC** to judge the business, then look at ROE to see how
much of the shareholder return is coming from leverage. If ROE is far above
ROCE, debt is doing the work.

---

## 3. Is it growing, and profitably? — margins and growth

### OPM — Operating Profit Margin

```
OPM = Operating Profit ÷ Sales
```

Of every ₹100 of revenue, how much survives after the costs of running the
business (before interest and tax). **Higher is better**, but the level is
almost entirely a function of industry — a supermarket at 5% may be excellent,
a software firm at 25% may be mediocre.

What matters far more than the level is the **trend**. Rising margins mean
pricing power or improving efficiency. Falling margins mean competition,
input-cost pressure, or a company buying revenue with discounts. A company
growing sales 20% while margins slide is often just buying growth.

**In your app:** `OPM % (latest FY)`, and the full year-by-year series under
the profit-loss statement on `/fundamentals`.

### Sales and Profit CAGR

CAGR is Compound Annual Growth Rate — the smooth annual rate that would take
you from the first value to the last, ignoring the bumps along the way.

**Higher is better, with two conditions.** First, compare sales growth and
profit growth: profit growing *faster* than sales means margins are expanding
(good); slower means the growth is being bought (worrying). Second, check the
growth is worth having — growth at a ROIC below the cost of capital makes the
company bigger and the shareholder poorer.

**In your app:** `Sales CAGR 3y %`, `Sales CAGR 5y %`, `Profit CAGR 3y %`,
`Profit CAGR 5y %`. Both windows are shown deliberately — a 5-year number
starting from a COVID-crushed base will look spectacular for reasons that have
nothing to do with the business. When the 3-year and 5-year figures disagree
sharply, look at the raw yearly series before believing either.

---

## 4. Will it survive? — safety metrics

Growth and returns don't matter if the company can't make it through a bad
year. These are the "does it go to zero" checks.

### Debt / Equity

```
D/E = Total Borrowings ÷ Shareholders' Equity
```

**Lower is better.** Below 0.5 is comfortable, above 1 means more borrowed than
owned and deserves scrutiny, above 2 is dangerous for most industries.

Context matters enormously. Utilities and infrastructure carry high debt
against predictable cash flows and that's normal. A cyclical business with the
same ratio is a different proposition entirely, because the debt is fixed and
the cash flow isn't.

**Banks and NBFCs break this metric completely** — borrowing *is* their
business, and D/E of 8–10 is routine. Don't apply this ratio to lenders.

### Interest Coverage

```
Interest Coverage = EBIT ÷ Interest Expense
```

How many times over could the company pay its interest bill from operating
profit? **Higher is better.** Above 5 is comfortable, 2–3 is tight, and below
1.5 means one bad year away from missing a payment.

This is often more informative than D/E, because it measures the debt burden
against the company's actual ability to service it rather than against a
balance-sheet number.

### Altman Z-Score

A 1968 formula combining five ratios into one bankruptcy-risk number. Your app
computes the original version, weighting profitability, leverage, liquidity and
market value against total assets.

**Higher is better:**

| Z | Zone | Meaning |
|---|---|---|
| Above 2.99 | safe | Bankruptcy unlikely |
| 1.81 – 2.99 | grey | Watch it |
| Below 1.81 | distress | Elevated bankruptcy risk |

**In your app:** `altman_z` and `altman_zone`. It needs several balance-sheet
items, so it only reports when at least three of the five terms are available.

### Piotroski F-Score

Nine yes/no tests of whether the business got *better* this year than last:
positive profit, positive operating cash flow, rising profit, cash flow
exceeding profit, falling debt, improving asset turnover, improving margins,
and so on.

**Higher is better.** Scored out of 9: 8–9 is strong, 0–2 is weak.

The clever bit is the fourth test — **operating cash flow greater than net
profit**. Profit is an accounting opinion; cash is a fact. A company reporting
rising profits while cash flow lags is the classic signature of aggressive
revenue recognition or a receivables problem. This one test catches a lot.

**In your app:** `f_score` alongside `f_checked` — how many of the nine could
actually be evaluated from the data held. A 5/6 is not a 5/9. The app degrades
the score honestly rather than assuming a missing test passed.

---

## 5. Is the price sensible? — multiples

Everything above described the *business*. Nothing so far has mentioned the
share price. These do.

### P/E — Price to Earnings

```
P/E = Share Price ÷ Earnings Per Share
```

The most quoted number in investing, and the most misunderstood. Read it as:
**how many years of current profit you're paying for one share.** A P/E of 25
means you pay ₹25 for ₹1 of annual earnings.

**Neither high nor low is "good."** This is the part people get wrong.

- **Low P/E** can mean cheap — or that the market expects earnings to fall.
  A cyclical company at peak profits looks cheapest right before its earnings
  collapse. This is the classic **value trap**.
- **High P/E** can mean expensive — or that the market expects rapid growth,
  and is often right. Consistently excellent businesses rarely get cheap.

P/E is a *question*, not an answer. A low P/E asks "why is this cheap?" and you
have to go and find out.

Useful comparisons, in order: the company's own P/E history, its direct
competitors, then the market. Comparing across industries is nearly meaningless.

**In your app:** `/valuation` shows current P/E, and `history_bands` places it
against the stock's own multi-year P/E range, computed year-by-year from annual
EPS and the candle store. "Trading at the top of its own 5-year range" is a far
more useful statement than "P/E is 30."

### P/B — Price to Book

```
P/B = Share Price ÷ Book Value Per Share
```

Book value is the accounting net worth per share: assets minus liabilities.
P/B of 3 means you're paying three times the accounting value of the company's
net assets.

**Lower looks cheaper, but it depends entirely on ROE.** A company earning 25%
on equity *deserves* a high P/B — its book value compounds fast. A company
earning 6% deserves a low one. This relationship is formalised in the
[justified P/B model](#justified-pb) below.

P/B works best for banks and asset-heavy businesses where book value means
something. It's nearly useless for software or consumer brands, whose real
assets — code, brand, people — barely appear on a balance sheet.

### Dividend Yield

```
Dividend Yield = Annual Dividend per Share ÷ Share Price
```

The cash return you get just for holding, before any price movement. **Higher
is better for income, with a caveat**: a very high yield (say above 6-7% in
India) is often a warning rather than a gift. Either the price has collapsed
for a reason, or the dividend is about to be cut.

Check whether the dividend is covered by earnings and cash flow. A dividend
paid out of borrowing isn't income, it's the company returning your own capital
with extra steps.

---

## 6. What is it worth? — valuation models

Multiples compare a stock to other stocks. Valuation models try to answer the
harder question directly: **what is this business actually worth, in rupees?**

The core idea behind all of them: a business is worth the cash it will produce
for its owners over its life, with future cash discounted because a rupee in
ten years is worth less than a rupee today.

Your app runs five models and takes the median. Here's what each one believes.

### Earnings-power DCF

**Discounted Cash Flow** — the workhorse. Your app runs a two-stage version on
EPS:

1. Project earnings forward for 10 years, growing at a rate that **glides
   linearly** from the company's historical growth down to a terminal rate
   (default 5%). No company grows at 20% forever; the glide encodes that.
2. Discount each year's earnings back to today at the cost of equity.
3. Add a terminal value — everything beyond year 10, valued as a perpetuity:
   `final earnings × (1 + g_term) ÷ (re − g_term)`.

**What to know about DCF:** it is simultaneously the most theoretically correct
method and the easiest to manipulate. Small changes in growth or discount rate
produce enormous changes in value, and the terminal value is often 60–70% of
the total — meaning most of your "precise" answer comes from a guess about year
11 onward.

This is exactly why your app ships a **sensitivity grid** (fair value across a
5×5 range of growth and discount-rate assumptions) and a **scenario** view.
A single DCF number is false precision. A grid showing "₹380–₹640 depending on
assumptions" is honest, and far more useful — if the price is ₹250, you don't
need to resolve the argument.

**Guard rails in your app:** historical growth is floored at 0 and capped at
18% (`g_cap`), so a company coming off a bad base can't project 60% growth
forever. The model refuses to run if `re ≤ g_term`, which would imply infinite
value.

### Graham Number

```
Graham Number = √(22.5 × EPS × Book Value per Share)
```

Benjamin Graham's 1949 rule of thumb for defensive investors. The 22.5 comes
from his twin limits: P/E no more than 15, P/B no more than 1.5 (15 × 1.5 =
22.5).

Crude, ignores growth entirely, and blind to business quality. Its virtue is
that it's almost impossible to fool — no assumptions to tune. Treat it as a
**deep-value floor**: if a stock trades below its Graham Number it's cheap on
an asset-and-earnings basis, whatever else is true. Most decent businesses
trade well above it, and that's fine.

### Graham revised

```
V = EPS × (8.5 + 2g) × 4.4 ÷ Y
```

Graham's later formula, which does account for growth. `8.5` is his P/E for a
zero-growth company, `g` is the expected growth rate in percent, `4.4` was the
AAA corporate bond yield in 1962, and `Y` is the current bond yield — the
`4.4 ÷ Y` term adjusts for the fact that money is cheaper or dearer now than it
was then.

Be careful with this one: the `2g` term makes it extremely sensitive to the
growth input, and it produces silly numbers for high-growth companies. Your app
feeds it the same capped growth rate as the DCF, which keeps it sane.

### Justified P/B

```
Justified P/B = (ROE − g) ÷ (re − g)      then × book value per share
```

This is the formal statement of the idea from [section 5](#pb--price-to-book): what P/B *should* a company
command, given its return on equity?

Read the numerator and denominator: if ROE equals the cost of equity, justified
P/B is exactly 1 — the company is worth its book value and no more, because it
earns precisely what shareholders require. **ROE above cost of equity → worth a
premium to book. Below → worth a discount.**

This is my favourite of the five, because it makes the link between quality and
price explicit. It's also the most useful for banks, where book value is real.

### Dividend Discount Model (DDM)

```
Value = D₀ × (1 + g) ÷ (re − g)
```

The Gordon Growth Model. A share is worth the perpetuity of its dividends,
growing forever at `g`, discounted at `re`.

Only meaningful for companies that pay steady, meaningful dividends. Returns
nothing for a non-payer, which is correct behaviour rather than a gap. Very
sensitive to `g` approaching `re` — as the gap narrows, value explodes toward
infinity, which is why your app clamps `g` to just below `re`.

---

## 7. The discount rate, which is where the magic hides

Every model above needs a discount rate — `re`, the **cost of equity**. It's
the return you require for taking the risk of owning this stock rather than
holding a government bond.

It matters more than almost any other input. Move it from 12% to 14% and a
DCF fair value can drop 25%. If someone shows you a valuation without telling
you their discount rate, they haven't shown you a valuation.

**How your app builds it:**

```
re = risk-free rate + equity risk premium + size premium
   = 6.9%           + 5.5%                + 0% to 3%
```

The size premium reflects that smaller companies are riskier, scaled by market
cap in ₹ crore:

| Market cap | Size premium | Resulting `re` |
|---|---|---|
| Above ₹20,000 Cr | 0% | 12.4% |
| ₹5,000–20,000 Cr | +1% | 13.4% |
| ₹500–5,000 Cr | +2% | 14.4% |
| Below ₹500 Cr | +3% | 15.4% |

This approximates Damodaran's `rf + β × ERP` without needing beta, which
screener doesn't provide. It's a reasonable simplification — beta is noisy and
unstable anyway — but it does mean a genuinely low-risk small cap and a wild
one get the same treatment.

**One thing to keep an eye on:** `RISK_FREE = 6.9%` is hardcoded as a default
for the Indian 10-year. If yields move materially, every valuation in the app
drifts. You can override `re` per request on the valuation API and page.

### Terminal growth

`g_term`, default 5%. The rate you assume the company grows at *forever* after
the projection window.

The discipline here: **terminal growth cannot exceed the long-run growth rate
of the economy.** A company growing faster than the economy forever eventually
becomes the economy. For India, 4–6% nominal is defensible. Anything above 7%
is you putting your thumb on the scale, and because terminal value dominates
the DCF, it's the single easiest place to manufacture whatever answer you
wanted.

---

## 8. Reverse DCF — the honest version

This is the technique I'd point you at hardest, because it inverts the whole
problem.

A normal DCF asks: *given my growth assumption, what's the fair price?* The
weakness is obvious — you pick the assumption, so you pick the answer.

A reverse DCF asks: **given the market price, what growth rate is the market
assuming?** Then you judge that single number.

You don't have to forecast anything. You just have to answer: *is this
plausible?* If the market is pricing in 22% annual earnings growth for a decade
from a company that has managed 11% historically, in an industry growing 8%,
you have learned something concrete — and you didn't need a spreadsheet full of
guesses to learn it.

**In your app:** `/valuation` with rich mode → `reverse_dcf`. Compare its
implied growth against `g_hist_pct` (what the company has actually delivered).
The gap between those two numbers is the market's optimism, quantified.

---

## 9. How your app combines them

For each stock the app runs all five models, discards any that can't compute
(no dividend → no DDM; negative EPS → no Graham), and takes the **median** of
what remains.

Median rather than average, deliberately: one model producing a wild number
can't drag the answer with it. The Graham revised formula in particular can
produce absurd values for high-growth companies, and a median simply routes
around it.

Then:

```
Margin of Safety = (Composite − Price) ÷ Composite × 100
```

| Margin of safety | Verdict |
|---|---|
| ≥ 30% | undervalued |
| 0 – 30% | fair |
| −25 – 0% | rich |
| < −25% | overvalued |

**Margin of safety is Graham's central idea and worth internalising.** You don't
buy at fair value, because your estimate is uncertain and the future is
uncertain. You buy well below it, so that being somewhat wrong still leaves you
whole. The 30% threshold is a convention, not a law.

**Treat the verdict as a starting point, not a conclusion.** It's the output of
five simplified models fed by scraped data. Its real job is to tell you which
stocks deserve an hour of your attention.

---

## 10. Where these numbers lie

**Banks and NBFCs break most of this.** Debt is their raw material, so D/E and
ROCE are meaningless, and EBIT barely applies. For lenders, use ROE, ROA, P/B,
net interest margin, and asset quality (gross and net NPA). Justified P/B is
the one model above that transfers cleanly.

**Cyclicals invert the P/E signal.** Commodity, auto and steel companies look
cheapest at peak earnings and dearest at the trough. For these, a low P/E at
the top of a cycle is a sell signal, not a buy one. Look at P/B or average
earnings across a full cycle instead.

**One year is noise.** A single year's ROE can be flattered by an asset sale, a
tax writeback, or a one-off. Always look at the 5-year series, which
`/fundamentals` shows in full. Consistency beats any single excellent year.

**Growth from a low base is meaningless.** Profit rising from ₹2 Cr to ₹8 Cr is
300% growth and tells you almost nothing. Check the absolute numbers, not just
the percentage.

**Cash flow over profit, always.** If operating cash flow persistently lags net
profit, something is wrong regardless of how good the ratios look. That's the
Piotroski test worth watching on its own.

**Screener data can be stale or wrong.** `/fundamentals` shows a `fetched_at`
timestamp and a `stale` flag for a reason. Before acting on anything, sanity
check against the company's own filing.

**Everything is relative to sector.** A 12% ROCE is poor for FMCG and decent
for a utility. Compare a company to its competitors and to its own history
before comparing it to a rule of thumb from a book.

---

## 11. Cheat sheet

### Business quality

| Metric | Formula | Direction | Rough marker |
|---|---|---|---|
| ROE | Net Profit ÷ Equity | High | >15% good, >20% excellent |
| ROCE | EBIT ÷ (Equity + Debt) | High | >15%, must beat cost of capital |
| ROIC | NOPAT ÷ (Equity + Debt) | High | Compare to `re` (~12–15%) |
| OPM | Operating Profit ÷ Sales | High + rising | Sector-dependent |
| Sales CAGR | Compound growth | High | Compare 3y vs 5y |
| Profit CAGR | Compound growth | High | Should outpace sales |

### Safety

| Metric | Formula | Direction | Rough marker |
|---|---|---|---|
| Debt/Equity | Borrowings ÷ Equity | Low | <0.5 comfortable, >2 risky |
| Interest coverage | EBIT ÷ Interest | High | >5 comfortable, <1.5 alarming |
| Altman Z | Composite | High | >2.99 safe, <1.81 distress |
| Piotroski F | 9 tests | High | 8–9 strong, 0–2 weak |

### Price

| Metric | Formula | Direction | Note |
|---|---|---|---|
| P/E | Price ÷ EPS | **Depends** | Low can mean cheap *or* dying |
| P/B | Price ÷ Book Value | **Depends** | Should track ROE |
| Dividend yield | Dividend ÷ Price | High, warily | Very high = often a warning |
| Margin of safety | (Value − Price) ÷ Value | High | ≥30% is the classic threshold |

### A reading order that works

1. **ROCE / ROIC** — is this a good business at all? If it can't beat its cost
   of capital, stop here.
2. **Debt/Equity and interest coverage** — can it survive a bad year?
3. **5-year sales and profit trend** — is it going somewhere, and is profit
   keeping pace with sales?
4. **F-score, especially cash flow vs profit** — are the earnings real?
5. **P/E against its own history** — is the market unusually excited or
   unusually bored, by this stock's own standards?
6. **Reverse DCF** — what does the price require the company to achieve? Is
   that plausible?
7. **Composite and margin of safety** — the summary, read last, treated as a
   prompt to look harder rather than a verdict.

Steps 1–4 are about the business and change slowly. Steps 5–7 are about the
price and change daily. The first four decide whether a company belongs on your
watchlist at all; the last three decide when.
