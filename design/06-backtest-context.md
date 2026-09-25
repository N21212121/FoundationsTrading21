# 06 — Backtest results by market context: `backtest_context.py`

Design spec **and** implementation. `backtest_context.py` is written, tested
against synthetic trades, and RUN AGAINST REAL BACKTEST OUTPUT — the numbers
in §9 are real and the caveats on them are in §9.1, which is longer than the
results, for good reason.

No existing `.py` file is modified by this document or by the module it
describes. It imports `conditions`, `heatmap` and `levels` and calls nothing
that writes. It requires no prerequisite edits anywhere.

---

## §0 — WHY THIS MODULE EXISTS

`compute_metrics()` returns `total_pnl`, `sharpe`, `profit_factor`,
`breakeven_slippage_bps`. Every one of them is one number over one sample,
and every one of them is an average across conditions that never co-occurred.

The owner's words: *"using shorter timeframes on backtests, along with
relative volumes and ATRs, could provide greater detail to the system as a
whole."* The detail he is after is not more precision on the total. It is
that the total is a blend.

A fast timeframe can be excellent on high-relative-volume expansion days and
awful on quiet contracting ones. The mean of those two describes no day that
ever traded. Tuning a configuration against that mean tunes it against a
fiction, and the sweep that a sibling module is building will happily find
the configuration that maximises the fiction.

So: **same trades, no re-run, split by the environment each entry was taken
in.** The question stops being "does this configuration work" and becomes
"when did this configuration work", which is the only version of the
question a finite sample can address at all.

## §1 — THE ARCHITECTURAL RULE

**A consumer, and nothing but a consumer.**

`backtester.py` produces trades. This module reads them. It does not replay,
re-run, re-fit, or optimise, and it never asks for a second backtest with
different parameters. That is a hard boundary and it is the reason the
module cannot be the thing that overfits: it has no knob to turn.

```python
bucket(trades, bars_by_symbol, daily_by_symbol=None, ...) -> dict
```

Pure. Bars and trades in, buckets out. No Flask, no I/O, no module-level
mutable state, no network. The `__main__` block reads a CSV because the
owner has CSVs; the library path never touches a file.

### The contract with `backtester.py`

`run_symbol()` appends exactly this, and `run_basket()` concatenates them:

```python
{'symbol', 'direction', 'entry_time', 'entry_price', 'exit_time',
 'exit_price', 'qty', 'pnl_dollars', 'notional', 'ret_pct',
 'exit_reason', 'bars_held', 'exit_date'}
```

Five of those are used: `symbol`, `entry_time`, `entry_price`,
`pnl_dollars`, `ret_pct`. Two more (`exit_time`) feed the hold-time median.
Nothing else is read, so a field added to the trade record upstream cannot
break this module, and `__main__` accepts any CSV carrying those columns.

`bars_by_symbol` is the **same dict handed to `run_basket()`** — the module
asks for no new data. That is a bigger deal than it sounds and it is §3.1.

## §2 — MEASURE AT THE ENTRY. NEVER AT THE EXIT.

A trade's context is the context at its entry bar.

This is not fussiness, it is the same hindsight quarantine `conditions.py`
puts around exit tags. Exit-time context is partly a *consequence* of the
trade: a winner ran because the day expanded, so an "expanding" band
measured at the exit would be selecting winners and calling it a regime.

Concretely, in `backtest_context.py`:

- The volume baseline uses sessions **strictly before** the entry date, and
  today's bars only up to the entry timestamp, counted by bar CLOSE.
  `backtester._fill` fills at the next bar's open, so "bars closed at or
  before the entry timestamp" is precisely the set the engine could have
  seen and no others.
- ATR uses daily bars **strictly before** the entry date. Excluding the
  entry session matters: including it would fold the day's own realized
  range into the regime the trade was supposedly taken in, and days with big
  ranges are days with big trade outcomes. The bucketing would then be
  partly sorting trades by their own results.

**This is verified, not asserted.** The check in §9.2 recomputes every
context after truncating the bar history to the entry timestamp and
confirms the bands are identical. Zero mismatches over 72 real trades × 4
dimensions.

## §3 — THE THREE CONTEXT DIMENSIONS

### The thresholds are not new, on purpose

`design/02-measurements.md` already specifies relative volume and ATR
regime for the screener's environment GPA, with every threshold argued at
length. **The bands here are that document's, to the number.** A backtest
bucket labelled `contracting` and a live GPA read labelled `contracting`
have to mean the same thing or the backtest is not describing the system the
owner is about to trade.

The constants are therefore duplicated in `backtest_context.py` with a
comment saying so, because `environment.py` does not exist yet. **When it
lands, delete them and import.** Two copies of a threshold is how two copies
of a threshold disagree.

### §3.1 Relative volume — `rvol`

```
bucket width = the modal gap between consecutive bars, MEASURED from the bars
elapsed      = minutes from 09:30 ET to the entry timestamp
today_cum    = sum of today's RTH bar volumes for bars CLOSING at or before
               the entry timestamp
base_cum     = MEDIAN, over the prior RVOL_SESSIONS sessions, of that
               session's cumulative RTH volume at the same elapsed minute
rvol         = today_cum / base_cum
```

| band | condition |
|---|---|
| `hot` | `>= 1.50` |
| `normal` | `1.00 – 1.50` |
| `light` | `0.60 – 1.00` |
| `dead` | `< 0.60` |

**Lookback: `RVOL_SESSIONS = 10`, floor `RVOL_MIN_SESSIONS = 3`.**
Judgement, inherited from design/02 §5: long enough for a median to be
stable, short enough to reflect the name's current level of interest rather
than its level three months ago. Below 3 sessions the baseline is `None`, not
a guess.

**Cumulative-vs-time-of-day, not per-bar-vs-trailing.** This matters and
`screener.rel_volume` gets it wrong today: comparing the latest bar to a
20-bar trailing mean at 09:40 compares it to a window that is mostly
overnight bars, so it reads 5x on every name every morning. Cumulative
against the same clock time on prior sessions is what "relative volume"
means on a chart.

**Median, not mean.** One earnings gap triples a mean for a month.

**Why the bar width is measured rather than passed in.** The caller handing
over `bars_by_symbol` does not necessarily know which timeframe the engine
ran on — and the whole point of the sibling sweep module is that the
timeframe varies. Getting the width wrong shifts every bucket boundary, so
it is taken from the modal inter-bar gap in the data itself.

#### Why this needs no `VolumeProfile` and no new I/O

design/02 §5 spends a page establishing that the LIVE path cannot build a
10-session time-of-day baseline: `BarCache(limit=300)` at `10Min` on a SIP
feed holds about three sessions, `clear()` wipes it on reconnect, and a
separate once-per-session fetch has to be designed.

**None of that applies here.** `backtest_run._fetch` pulls the entire
date range in one call with `limit=BIG_LIMIT` and hands `run_basket` every
session at once. The backtest already holds in memory exactly the history
the live path has to go and fetch. The baseline is a slice of the input.

This is the one place the backtest path is strictly better equipped than the
live path, and it is worth naming because the reflex would have been to copy
the `VolumeProfile` design across.

#### The structural blind spot at 09:30

A fill at exactly 09:30 has **no closed RTH bar behind it** — the 09:30 bar
is the one being filled at. `today_cum` is therefore `None` and `rvol` is
`unknown`.

That is the correct answer and it is also an awkward one, because 09:30 is
the middle of the window the owner cares most about. On the real run in §9
it cost 6 of the 13 first-30-minute trades their volume read. It is stated
here rather than papered over. The fix would be a pre-market relative-volume
read (04:00–09:30 cumulative against prior sessions' pre-market), which IS
available at 09:30 — **deliberately not built**, because no threshold for it
is argued anywhere and inventing one here to fill a hole would be exactly
the kind of number this document is supposed to flag.

### §3.2 ATR as a share of price — `atr_pct`

```
atr_pct = levels.atr(prior_daily_bars, 14) / entry_price
```

| band | condition |
|---|---|
| `wide` | `>= 0.035` |
| `ok` | `0.020 – 0.035` |
| `thin` | `0.012 – 0.020` |
| `dead` | `< 0.012` |

**Lookback: ATR(14), design/02's and `levels.atr`'s own default. Minimum 15
prior daily bars or the read is `None`.**

**Why a percentage and not dollars.** The owner's own example: a $0.30 ATR
on a $400 name gives nothing to trade. That is `atr_pct = 0.00075`, sixteen
times below the `dead` line. A dollar threshold is meaningless across a book
whose fills run from $1 to $930, which is the same argument `levels.py`
already makes for expressing distance in ATRs.

`ATR_PCT_OK = 0.020` is **judgement, anchored** (design/02 §4): at 2% of
price a 0.5-ATR move is 1% of price, which on near-dated options is a move
that pays.

### §3.3 ATR expanding or contracting — `atr_trend`

```
atr_trend = levels.atr(prior_daily, 5) / levels.atr(prior_daily, 20)
```

| band | condition |
|---|---|
| `expanding` | `>= 1.15` |
| `steady` | `0.85 – 1.15` |
| `contracting` | `<= 0.85` |

**Lookback: ATR(5) over ATR(20), needing 21 prior daily bars. Below that the
read is `None` — not a shorter window silently substituted.**

`±15%` is **judgement** (design/02 §4): ATR(5) against ATR(20) wobbles by
roughly 10% on a quiet name, so a narrower band labels noise as a regime
change.

**This has a real cost at the start of a run and it is not hidden.**
`backtest_run.warmup_start` pads `10Min`/300 bars by about 21 calendar days
≈ 15 sessions. ATR(14) is just covered; **ATR(20) is not**, so the earliest
trades in a run legitimately have `atr_trend = None` and land in `unknown`.
On the synthetic 40-session test this was 52% of trades. The remedy is to
pass real `daily_by_symbol` — one extra `get_bars_multi` call with a longer
start date — and the real run in §9 does exactly that, reaching 100%
coverage on both ATR dimensions.

### §3.4 Where the daily bars come from

`backtest_run.py` fetches primary and macro bars and **no dailies at all**,
so `bucket()` takes an optional `daily_by_symbol` and, without it,
synthesizes daily OHLCV from the RTH intraday bars: first RTH open, last RTH
close, max high, min low, summed volume, per ET date.

This is **not identical** to Alpaca's daily bars. Alpaca's daily open and
close are the official auction prints; the first and last 10-minute bar are
close but not the same tick, and a print can land outside the intraday bar
grid. The error is small and same-signed, so ATR *ratios* are barely
affected and `atr_pct` may run a hair narrow. `daily_source` is reported in
`params` so a reader always knows which they got. **Pass real dailies when
you can** — it costs one request.

### §3.5 Time of day — `tod`

`09:30-10:00`, `10:00-12:00`, `12:00-14:00`, `14:00-16:00`, all ET, from the
owner's own claim. Plus `premarket` (< 09:30) and `after-hours` (>= 16:00),
which exist because the SIP feed prints 04:00–20:00 and a trade entered
there has to land in a bucket that can be counted rather than vanish. §9.3
shows that this was not a hypothetical.

**Timestamp handling.** `alpaca_manager` emits `b.timestamp.isoformat()`
from a UTC-aware bar and `backtester` copies it through, so every real
timestamp is offset-aware and converts cleanly. A *naive* timestamp — which
only arises if something strips the offset — is read as **UTC**, because
that is what produced it. Assuming local time instead would shift every
bucket boundary by the machine's offset, which is a different answer on a
laptop that travels.

### §3.6 What is deliberately NOT a dimension

- **`used` (today's range spent against ATR).** design/02 §4 computes it and
  it is genuinely informative, but it is a within-session *path* variable:
  by 14:00 it partly encodes what has already happened. That puts it closer
  to a signal than to a regime and it does not belong on the same axis as
  the other three.
- **Market-wide regime (SPY, VIX).** Every read here is per-symbol and
  computable from bars the backtest already held. An index feed adds a
  fetch, a cache and a failure mode for a dimension nobody has asked for.
- **`halflife.classify()`.** Checked, as instructed, to see whether it gives
  a regime cut for free. It does not fit here. It returns
  `trending / reverting / random_walk / unknown` from a variance ratio over
  a long window (`len(logc) >= 4k + 2`), which is a statement about the
  **series**, not about the **session** — it changes slowly and would
  bucket whole stretches of the backtest identically. It is also the
  selection primitive for choosing *which engine* to run, and reusing it as
  a context cut would conflate "should this engine be here at all" with
  "did this engine do well today". Worth a separate cut at basket-selection
  time; not one of these three.
- **More than three pairs.** Four dimensions is six pairs and 100+ cells.
  `DEFAULT_PAIRS` is three, chosen because each answers a question someone
  actually asked: `tod × rvol` (is the open special, or is the open just
  where the volume is?), `tod × atr_trend` (does the open hold up on
  contracting days?), `rvol × atr_trend` (do the two environment reads say
  the same thing, or are they independent?). The `pairs` argument accepts
  others; §4 is the reason not to ask for all of them.

### §3.7 Missing data is a bucket, not a default

Every read degrades to `None`, and `None` becomes the band `unknown`.

`unknown` is a **real bucket with a real count**, never a silent drop. Every
trade lands in exactly one band of every dimension, and the per-dimension
counts reconcile to the total — asserted in the test harness, not hoped for.
`coverage` reports `classified / unknown / pct_classified` per dimension, and
each context carries a `reason` string saying *why* a `None` is `None`.

`unknown` buckets are forced `interpretable: False`. A data gap is not an
environment, and "trades where we couldn't measure the volume" is not a
finding about volume.

A missing read is never filled with a default. A defaulted `normal` is a
wrong number wearing the right clothes.

---

## §4 — THE STATISTICS. THIS IS THE PART THAT MATTERS.

**Slicing a few hundred trades four ways produces cells of n=6, and cells of
n=6 show enormous spurious edges every single time.** Reporting those raw
would be worse than reporting nothing, because they look like discoveries.
This is the failure mode the module is built around, and everything below is
in service of not committing it.

### §4.1 The machinery is `conditions.py`'s, imported

Not reimplemented. `backtest_context` does `import conditions as CD` and
uses `CD.PRIOR`, `CD.THIN` and `CD._welch_t` directly, so the two files
cannot drift apart.

**Shrinkage.** Each bucket's average is pulled toward the book average by
`PRIOR = 10` pseudo-trades before it is scored:

```
shrunk = (sum + PRIOR * book_avg) / (n + PRIOR)
score  = shrunk - book_avg
```

A bucket needs real weight of evidence before its score moves off zero.
Eight trades averaging +0.4% become +0.18% after shrinkage; forty trades
averaging +0.4% stay near +0.32%. That is the whole point.

**Welch t.** The bucket against every trade outside it, unpooled variance.
It drives the confidence label only, never the ordering of the buckets.

**Labels, `THIN = 8`:**

| label | condition |
|---|---|
| `thin` | `n < THIN`, or no `t` |
| `strong` | `\|t\| >= 2` |
| `some` | `\|t\| >= 1` |
| `noise` | otherwise |

### §4.2 The minimum n, and how it is enforced

**`MIN_N = THIN = 8`. Below 8 trades a bucket is REPORTED but NOT
INTERPRETED.**

It is deliberately the *same constant*, not a second number, so there is one
place to change and no way for the two to disagree.

Why 8 and not 20 or 30: 8 is `conditions.py`'s existing floor, applied to
the same owner's same trades for the same purpose, and a second threshold
here would be a second opinion about the same question. It is **not** a
claim that 8 trades is enough to know something — §4.3 exists precisely
because it is not. It is the line below which the module refuses to even
try.

**Enforcement is structural, not advisory.** `findings` — the only key in
the whole result where the module states a conclusion — is *built* by
filtering on `interpretable` and a non-noise label:

```python
if c['interpretable'] and c['confidence'] in ('strong', 'some'):
    findings.append(...)
```

A thin bucket cannot reach it. There is no flag to override, no verbose mode
that unlocks it. Same in `first_thirty()`: `verdict` is the literal string
`'insufficient sample'` whenever either side is under `MIN_N`, produced by
the same branch that would have produced a real verdict, so there is no path
that returns a conclusion the sample does not support.

And **every** bucket reports `n` beside its result, always, including in the
grid cells (`+0.414%/n=13`) and in `format_report`'s tables. There is no
formatting path in the module that prints a result without its sample size.

The thin bucket is still shown in full. Hiding it is its own kind of lie:
you would not know the split had been tried, and "the 12:00–14:00 bucket has
4 trades" is information.

### §4.3 Multiple comparisons — the failure shrinkage does NOT catch

Shrinkage protects a bucket against its own small sample. It does nothing
about the fact that this module runs the test dozens of times.

Four dimensions of bands plus three grids is on the order of seventy
comparisons. At `|t| >= 2` a two-sided normal hands out a `strong` label
about **4.6% of the time to data with no structure in it whatsoever.**

This is not a theoretical worry, it is a measured one. On the synthetic
harness — returns drawn i.i.d. from one distribution, **no context effect
present by construction** — the module produced *two* `strong` buckets at
`|t| ≈ 2.0` on its first run, and a third appeared in a grid cell. All
noise. That is exactly what a reader would have written up as a finding.

So the result carries a `multiplicity` block, and `format_report` prints it
under the findings list:

```
-- how many times the test was run: 17 buckets reached n=8.
   'strong' found 1, pure noise owes about 0.8.
   'some' found 5, pure noise owes about 5.4.
```

`expected_strong_by_chance` is a **lower bound**, and the module says so in
its own output. It uses the normal tail; the t-distribution at n=10 has
fatter tails, and the buckets are not independent (a trade sits in one band
of every dimension and in every grid). The true rate is higher and is not
worth pretending to compute exactly.

**The reading rule: if `n_strong` is not comfortably above
`expected_strong_by_chance`, the honest summary of the findings list is
"nothing here yet."**

No correction is applied — no Bonferroni, no FDR. Applying one would replace
a visible count the reader can weigh with an adjusted p-value that looks
authoritative and hides the same uncertainty. The count is shown instead.

### §4.4 The one deliberate deviation from `conditions.py`

**The headline variable is `ret_pct`, not dollars.**

`conditions.py` ranks on `score_pl` because the journal's legs are whatever
size they were. A backtest's per-trade dollars are an artifact of the pinned
budget: with `budget_by_symbol` weights (the Sharpe-weighted portfolio
modes), a large-budget name dominates every bucket on notional alone and the
buckets end up sorting symbols rather than contexts.

So `t` and `score_pct` are computed on return percent; `t_pl` and `score_pl`
on dollars are reported alongside, and `findings` is ordered by `|t|` then
`n`. For a flat `--alloc-pct` run the two orderings agree. This is the only
place the two modules differ and it is flagged in the module docstring too.

### §4.5 Descriptive, not predictive

Every number in the output is a statement about **one historical sample**:
these trades, these symbols, these dates.

A bucket showing +0.4% per trade **did well over that stretch**. It is not a
forecast. It is not an edge. It carries no claim that the next high-RVOL day
pays. Nothing here is fit, cross-validated, or held out, and the module has
no mechanism that could do any of those.

The result dict carries `'descriptive_only': True` as a field, not only as
prose, so a UI or a downstream report cannot present these as forecasts by
omission. `format_report` prints it on line three.

The correct use of a bucket you like is as a **hypothesis to test on a
different date range**. Not as a filter to bolt onto the engine.

---

## §5 — OUTPUT SHAPE: THE JOURNAL HEATMAP'S, NOT A NEW ONE

The owner already has a grid UI over his journal with layer filters. Where
this module's output can wear that shape, it does.

**Each bucket is literally `heatmap._cell()`**, called on the trades
projected into journal-leg field names (`pnl_dollars → pl`,
`ret_pct → pl_pct`, entry/exit times → `hold_minutes`). Not a
reimplementation — the same function. So a context bucket and a journal cell
carry the identical key set and, more importantly, the identical
**conventions**: win rate excludes scratches, still-open legs are separated
from priced ones, `median_hold_min` is computed the same way. Two surfaces
reporting a win rate by two different rules is a bug waiting to be argued
about, and this removes the possibility.

Each cell is then augmented with the `conditions.py` keys — `score_pl`,
`score_pct`, `score_wr`, `t`, `t_pl`, `confidence` — plus `interpretable`,
`sparse`, `n`, and `avg_pct`.

**A pair of dimensions comes back in `heatmap.build()`'s grid shape**:
`cells` keyed `'row|col'`, plus `row_margin`, `col_margin` and `summary`.
`to_heatmap_grid()` renames the two axis keys (`rows → grades`,
`cols → exit_keys`) and adds `filters_applied`, so an existing grid renderer
takes the payload unmodified. That two-key rename is the **entire**
difference between a context grid and a grade grid as far as display is
concerned.

The semantics are not identical and the helper's docstring says so: `grades`
here holds strings like `hot` and `contracting`, and the axes are **not
ordinal**, so the diagonal means nothing. A renderer that draws a diagonal
emphasis for the entry-grade/exit-grade grid should not draw one here.

`sparse_below` is carried through with the same meaning it has in
`heatmap.build()` (a cell to gray out), and sits *alongside* `interpretable`
rather than replacing it: `sparse` is a display hint at 3, `interpretable`
is the statistical floor at 8.

`apply_filters` is **not** reused. It filters on journal condition keys that
backtest trades do not carry. Narrowing a backtest trade list is a
one-line list comprehension at the call site and wiring a journal-shaped
filter language onto it would be machinery for its own sake.

---

## §6 — THE OWNER'S CLAIM, TESTED

> *"Most of the Ripster-based plays happen within first 30min, then rest of
> day is generally chop."*

That sentence contains two claims and they need separate answers, because a
belief can be right on one and wrong on the other:

- **FREQUENCY** — do a disproportionate share of entries occur 09:30–10:00?
  Measured as the window's share of RTH entries against its share of the RTH
  clock, `30 / 390 = 7.7%`. Over-represented at `> 1.5×` that,
  under-represented at `< 0.5×`, otherwise proportional.
- **QUALITY** — are those trades *better*? First-30 return percent against
  every other RTH trade, Welch t.

Conflating them is how "that's where the plays are" survives a losing
morning book: the entries really are clustered there, so the claim feels
confirmed even when the outcomes are flat.

`first_thirty()` returns both, plus the full cell for each side, plus
`interpretable`. Pre-market and after-hours entries are excluded from both
sides — the claim is about the trading day.

**The answer on the one real sample available is in §9.3, and it is "his
belief is supported on both counts, and the sample is nowhere near able to
settle it."**

---

## §7 — WHAT THIS MODULE DOES NOT DO

- **No re-running and no optimising.** It reads one trades list. It cannot
  search a parameter space, so it cannot overfit one.
- **No picking a winning bucket and re-fitting to it.** That is how a
  backtest gets sawn into overfit slivers, and it is the natural next thing
  to want to do with this output. Don't.
- **No filtering recommendation.** The module never says "only trade
  expanding days". The `findings` list says which buckets this sample can
  distinguish, and §4.5 says what that is worth.
- **No p-value correction.** §4.3.
- **No options overlay awareness.** `backtester.py`'s own docstring is clear
  that it simulates the directional signal on the underlying and models no
  theta, IV or spread. Bucketing does not change that; a context breakdown
  of signal quality is still a breakdown of signal quality.

---

## §8 — RUNNING IT

As a library:

```python
import backtest_context as BC
res = backtest_context.bucket(result['trades'], bars_by_symbol,
                              daily_by_symbol)   # dailies optional
print(BC.format_report(res, title))
```

From a trades CSV (what `backtest_run.py` already writes):

```
python backtest_context.py <trades.csv> --bars bars.json --daily daily.json
python backtest_context.py <trades.csv>            # time of day only
python backtest_context.py <trades.csv> --json
```

Without `--bars` only the time-of-day dimension is computable; the other
three report 100% `unknown` and contribute nothing to `findings`. That is
the intended degradation, not a broken run.

`annotate()` is public and returns one context dict per trade in input
order, for anyone who wants to join an `rvol` column onto a trades CSV and
ask their own questions.

---

## §9 — RESULTS ON REAL BACKTEST OUTPUT

`%LOCALAPPDATA%\FoundationsTrading\backtest_adhoc_ripster_ema_cloud_20260709_201001.csv`
— the only completed backtest in the data directory. **72 trades, AAPL
only, 2026-05-20 → 2026-07-08, engine `ripster_ema_cloud`.** Real
`10Min` and `1Day` bars re-fetched from Alpaca for the same window, so both
ATR dimensions reach 100% coverage and the dailies are the real ones.

Book: **72 trades, 20.8% win rate, −$1,404.88 total, −$19.51 average,
−0.020% average return.**

### Time of day

| band | n | win | avg $ | avg % | shrunk % | t | label |
|---|---|---|---|---|---|---|---|
| `premarket` | 1 | 0.0% | −144.12 | −0.144 | −0.011 | — | thin (not interpreted) |
| **`09:30-10:00`** | **13** | 46.2% | +414.14 | **+0.414** | +0.245 | **1.72** | some |
| `10:00-12:00` | 11 | 36.4% | −177.66 | −0.178 | −0.083 | −1.33 | some |
| `12:00-14:00` | 12 | 16.7% | −187.04 | −0.187 | −0.091 | −1.89 | some |
| `14:00-16:00` | 13 | 7.7% | −30.88 | −0.031 | −0.006 | −0.12 | noise |
| `after-hours` | 22 | 9.1% | −92.92 | −0.093 | −0.051 | −0.98 | noise |

### Relative volume — 59.7% classified, 29 `unknown`

| band | n | win | avg % | t | label |
|---|---|---|---|---|---|
| `hot` | 10 | 20.0% | −0.072 | −0.37 | noise |
| `normal` | 15 | 26.7% | +0.051 | 0.42 | noise |
| `light` | 18 | 22.2% | −0.104 | −1.05 | some |
| `unknown` | 29 | 17.2% | +0.014 | 0.41 | not interpreted |

The 29 unknowns break down as **22 after-hours + 1 pre-market** (outside
RTH, no time-of-day baseline exists) **+ 6 at exactly 09:30** (no closed RTH
bar yet — §3.1). Nothing is missing for want of data; the reads are
undefined where they are undefined.

### ATR % of price — 100% classified

`ok` n=49 (−0.010%, t=0.19, noise) · `thin` n=23 (−0.039%, t=−0.19, noise).
One symbol, so this dimension is really "AAPL's own ATR drifting across two
bands over seven weeks." It cannot say anything and it doesn't.

### ATR regime — 100% classified

`expanding` n=40 (−0.000%, t=0.32, noise) · `steady` n=24 (+0.019%, t=0.39,
noise) · **`contracting` n=8 (−0.232%, t=−2.43, `strong`)**.

### Multiplicity

```
17 buckets reached n=8.  'strong' found 1, pure noise owes about 0.8.
                         'some'   found 5, pure noise owes about 5.4.
```

**So the one `strong` result is exactly what one would expect from noise,
and the five `some` results are slightly fewer than noise owes.** By the
module's own reading rule (§4.3) the correct summary of this run is: the
sample shows nothing that survives the number of times it was asked.

The `contracting` bucket in particular is n=8 — exactly the floor — on a
single symbol. It is the kind of cell this whole design exists to stop
someone writing up.

### §9.1 What this sample cannot support

Everything above, honestly. Enumerated because the table looks more
convincing than it is:

1. **One symbol.** AAPL. There is no cross-sectional variation at all, so
   `atr_pct` is measuring AAPL's drift over seven weeks and every "context"
   band is heavily confounded with *date*. A bucket is close to a set of
   adjacent calendar days.
2. **Seven weeks, 72 trades.** Four time-of-day buckets at n≈12 each. The
   grid cells run n=2 to n=6 and not one `tod × rvol` cell reaches the
   floor — the report says `0/24 cells reach n=8`, which is the honest
   output of this sample size and not a failure of the split.
3. **Trades are not independent.** Several per day on one name in one
   direction; one good trend day supplies a cluster of correlated winners.
   The Welch t assumes independence and therefore **overstates** every
   t-statistic here. The real degrees of freedom are closer to the number of
   *sessions* than the number of trades.
4. **The book lost money** (−$1,405, 20.8% win rate). Buckets of a losing
   configuration describe where it lost least. That is not the same question
   as where a working configuration wins, and the two should not be conflated.
5. **Multiplicity, quantified above**, says the findings list is consistent
   with no structure.

### §9.2 The no-lookahead check

Every context was recomputed after truncating the bar history to the trade's
own entry timestamp (and the daily bars to strictly-prior sessions), then
compared to the context computed with the full history in hand.

**0 mismatches over 72 trades × 4 dimensions.** No band changes when the
future is removed, which is the property §2 claims. This is a check worth
re-running whenever the baseline logic is touched.

### §9.3 The first-30-minutes claim

```
verdict: entries over-represented; outcomes better than the rest of the day
13/49 RTH entries (26.5%) in 7.7% of the session clock.
Welch t on return % = 1.75.
```

**Both halves of the owner's belief are supported on this sample.** The
window holds 26.5% of RTH entries against 7.7% of the RTH clock — 3.4× its
share — and the first-30 trades averaged **+0.414%** against a book of
−0.020%, with the three subsequent blocks all negative. His "rest of day is
generally chop" reads as true here too: 10:00–12:00, 12:00–14:00 and
14:00–16:00 are −0.178%, −0.187% and −0.031%.

**And the sample cannot settle it.** n=13 against n=36, `t = 1.75` is `some`
and not `strong`, the trades are clustered within days (§9.1.3) so the true
t is lower than 1.75, and 1 symbol over 7 weeks is one market regime. This
is a belief the data leans toward, on a sample that would lean somewhere by
chance about a third of the time.

What would settle it: the same test across a basket of 20+ names over 6+
months, with the t recomputed on session-level averages rather than
trade-level ones so the clustering stops inflating it. The module runs
unchanged on that input — it is the input that does not exist yet.

### §9.4 An incidental finding worth more than the buckets

**22 of 72 entries (31%) were taken after 16:00 ET, and 1 before 09:30.**

`signal_engine.ENTRY_START = dtime(9, 30)` gates the *lower* bound only.
There is no upper bound, and `backtest_run._fetch` pulls SIP bars covering
04:00–20:00. So the replay happily enters at 16:50 and 18:00 on
extended-hours bars, in a session the live system does not trade.

Nearly a third of this backtest's sample is trades the live book would never
have taken. Every total in every report over this run is diluted by them.

This is not a context finding — it is a **backtest fidelity finding**, and
the time-of-day bucket is simply the first thing in the codebase that could
see it. It is reported and not fixed: `signal_engine.py`, `backtester.py`
and `backtest_run.py` are all outside this document's scope. The fix
belongs on the producer side (an entry cutoff in the engine, or an RTH
filter on the fetched bars), and whoever makes it should be aware that it
will change every historical backtest number in the system.

---

## §10 — JUDGEMENT VERSUS MEASUREMENT

Stated plainly, since the house rule is to separate them.

**Measured, or inherited from a document that argues it:**

- The formulas: cumulative RVOL against a time-of-day median, `atr/price`,
  `ATR(5)/ATR(20)`. Design/02 §4–§5.
- Every band threshold. Design/02 §4–§5, unchanged, deliberately.
- `PRIOR = 10`, `THIN = 8`, the Welch t and the label ladder.
  `conditions.py`, imported.
- Bar width, taken from the modal inter-bar gap in the data.
- The no-lookahead property — verified by reconstruction (§9.2), not
  assumed.
- The multiple-comparisons rate — demonstrated on synthetic null data
  (§4.3), not asserted.

**Judgement, mine, with no evidence behind it:**

- **`MIN_N = 8`.** Chosen to equal `conditions.THIN` so the system has one
  floor rather than two. There is no analysis saying 8 is the right number
  of trades; there is an argument that a second number would be worse.
- **Which three pairs are in `DEFAULT_PAIRS`.** Six pairs exist. Three were
  picked because each maps to a question someone asked out loud. The other
  three are available and unexamined.
- **Excluding `used` and `halflife.classify()`** as dimensions (§3.6).
  Reasoned, not tested.
- **`ret_pct` as the headline variable** over dollars (§4.4). Correct under
  per-symbol budget weighting; under a flat allocation it makes no
  difference.
- **Reporting `unknown` as a visible bucket** rather than dropping those
  trades. A preference for a reconciling count over a tidier table.
- **The `1.5× / 0.5×` cut for "over-" and "under-represented"** in
  `first_thirty()`. Round numbers, picked to be obviously loose rather than
  falsely precise.

## §11 — STATUS

- `backtest_context.py` — written, imports cleanly, runs.
- Synthetic harness — 180 trades of the exact `run_symbol()` record shape
  over 40 synthetic sessions. Verified: per-dimension counts reconcile to
  the total; grid counts reconcile; no-bars degrades to 100% `unknown` on
  three dimensions with time-of-day still working; empty trades list returns
  a valid empty result; `min_n` enforcement admits no thin bucket to
  `findings` and returns `'insufficient sample'` from `first_thirty()`; the
  cell key set is a superset of `heatmap._cell`'s; the whole result is
  JSON-serialisable.
- Real data — run against the 72-trade AAPL backtest above, with real
  10-minute and daily bars. Results in §9, and §9.1 is why they are not
  conclusions.
- Not wired into `app.py` or any route. It is a library and a CLI, on
  purpose; the brief's boundary excluded the live path and nothing here
  needs to cross it.
