# 05 — The timeframe matrix: `engine_ripster_tf.py` + `backtest_sweep.py`

Design spec and findings. Two new files, both implemented and runnable. **No
existing `.py` file is modified by this document.** The three edits it would
*like* of other modules are listed at the end under **PREREQUISITE EDITS**;
none of them is required for the matrix to run today, and none was made.

---

## THE QUESTION

Verbatim, from the owner:

> *"3min is great for entry but 6min or 10min might be more appropriate for
> exit, depending on how quickly we see price changing. For example, SPY moves
> FAST sometimes, so a quick exit on a lower timeframe could help reduce losses
> or lock in profits."*

That is not one parameter. It is two: an **entry timeframe** and an **exit
timeframe**, free to differ. Three candidate bar sizes each gives nine
combinations, and for reasons that are the substance of this document each
combination has to be run twice. Eighteen cells. The deliverable is the table.

---

## THE TRAP, WHICH IS MOST OF THE DESIGN

`signal_engine.py` names its clouds in **bars**:

```python
EMA_FAST = 5;  EMA_SLOW = 12;  EMA_REGIME_A = 34;  EMA_REGIME_B = 50
```

A bar is not a duration until you say what a bar is. Change the bar and every
cloud silently changes what it measures:

| cloud | on 10Min | on 6Min | on 3Min |
|---|---|---|---|
| EMA 5 | 50 min | 30 min | 15 min |
| EMA 12 | 120 min | 72 min | 36 min |
| EMA 34 | 5.7 h | 3.4 h | 1.7 h |
| **EMA 50** | **8.3 h** | **5.0 h** | **2.5 h** |

The 34/50 is the **hard trend gate**. Nothing goes long below it; nothing
shorts above it; no counter-trend entry, ever. It is the single rule that
`signal_engine`'s docstring says was added to close the loss profile of the old
three-vote engine. Running that gate on a 2.5-hour horizon instead of an
8.3-hour one is not "the same system, faster." It is a system that gets a
different answer to the only question it refuses to override.

So the matrix runs every timeframe in **two period modes**, and the mode is
baked into the engine's registry name so a row cannot be read without it:

- **`stock`** — periods stay `5/12/34/50`. Smaller bars ⇒ shorter horizons.
  This is *"what if the whole system were quicker?"*
- **`scaled`** — periods × `10 / tf_minutes`, preserving the wall-clock
  horizon of each cloud. This is *"the same system, sampled more often."*

| | fast | slow | regime A | regime B | warm-up bars |
|---|---|---|---|---|---|
| 10Min `stock` (**control**) | 5 | 12 | 34 | 50 | 250 |
| 6Min `scaled` | 8 | 20 | 57 | 83 | 415 |
| 3Min `scaled` | 17 | 40 | 113 | 167 | 835 |

Wall-clock spans after scaling: 3Min `scaled` = 51 / 120 / 339 / **501** min
against the control's 50 / 120 / 340 / **500**. Within one percent, which is
the point.

### Rounding rule

```
period' = floor(period * 10 / tf_minutes + 0.5),  floored at 2
```

Plain multiplication of the **span**, rounded half-away-from-zero. Half-away
rather than Python's `round()`, which is half-to-even and would send a 12.5
period to 12 and a 13.5 period to 14 — a rule whose answer depends on the
parity of its input is not a rule you want in a spec.

**Judgement call, and the alternative was rejected on purpose.** An EMA of span
*N* has its centre of mass at `(N-1)/2` bars, so the horizon-preserving
identity is really `N' = 1 + (N-1) * 10/tf`, which gives **14 / 38 / 111 / 164**
at 3Min rather than 17 / 40 / 113 / 167. The two rules agree to within ~2 % on
the 34 and the 50 — the clouds that matter most, because they are the gate —
and disagree by ~20 % on the **5**. The span rule is implemented because it is
the one specified for this experiment and the one whose arithmetic a reader can
check in their head. But the disagreement lands squarely on the fast cloud,
which is the *entry trigger*, so: **any conclusion this matrix produces about
entries at 3Min is sensitive to a rounding convention.** Conclusions about the
gate are not. That asymmetry should survive into whatever gets decided.

Not done: a third mode using the centre-of-mass rule. It would have made the
matrix 27 cells to answer a question nobody asked yet.

### Warm-up rule

```
warmup_bars = 5 * slowest EMA period on that stream
history     = round(1.2 * warmup_bars)
```

At 10Min with stock periods that is **250** and **300** — exactly
`signal_engine.WARMUP_BARS` and `engine_ripster.history['primary']`. That
coincidence is why this rule was picked over "preserve the wall-clock warm-up":
it reproduces the live numbers with no special case, and warm-up exists so an
EMA can converge, which is a property of bars, not of the clock.

**The consequence is not small and is not hidden.** In `stock` mode a 3Min cell
warms up in 250 three-minute bars. On this feed — which delivers extended-hours
bars, ~330 of them a session at 3Min (see *What the data actually is*, below) —
that is under one session. `signal_engine._volume_ok` estimates ADV from the
**prior sessions present in the window**; at 10Min/250 it has about two, and at
3Min/250 it may have one or none. When it has none it fails closed, which
blocks OPTIONS, which in `backtester.run_symbol` blocks **short entries only**.
So a fast `stock` cell can show a suppressed short count for a reason that has
nothing to do with timeframes. Check the long/short split before believing any
fast/stock row.

---

## WHAT THE DATA ACTUALLY IS

Verified, not assumed, because everything above depends on it.

**`3Min` and `6Min` are both fetchable.** `6Min` is enumerated in
`alpaca_manager._timeframe`; `3Min` is not, but that method falls through to a
parsed `TimeFrame`, and the pull works. `halflife.TF_MINUTES` is self-extending
(`__missing__` parses), so `TF_MINUTES['3Min']` returns 3 even though `'3Min'
in TF_MINUTES` is deliberately `False` — which matters, because
`halflife.auto_windows` tests **membership** and would raise on `'3Min'`. The
engine therefore calls `halflife.tf_minutes()` (parses, never raises) and never
`auto_windows`. `bar_cache.BarCache` does the same and is fine at 3Min.

Measured on SPY:

| stream | bars, 2026-02-02 → 2026-09-25 | first bar of a session |
|---|---|---|
| 3Min | 52,065 | 04:00 ET |
| 6Min | 26,0xx | 04:00 ET |
| 10Min | 15,7xx | 04:00 ET |

**The feed is extended hours.** ~990 minutes a session, not 390. Three things
follow. The first is a backtest fidelity bug that had to be fixed before a
single number was produced, and it gets its own section below.

1. **Entries outside market hours.** See *The extended-hours contamination*.
2. A "bar" of EMA history spans pre-market and after-hours, so the 5/12 cloud
   is being shaped partly by thin overnight tape. Halving the bar size
   multiplies the number of thin bars in the window, so **whipsaw in the
   illiquid hours is a cost the 3Min cells pay and the 10Min control pays
   less of.** The RTH gate below does not remove this and the matrix does not
   separate it from genuine speed. It is a real cost of trading a 3-minute
   chart on this feed, not an artifact — but it is not isolated either.
3. `backtest_run._sessions_per_bar` assumes a 390-minute session, so its
   warm-up padding over-fetches by ~2.5×. That is the safe direction and the
   padding was reused unchanged rather than "fixed": a pad that is silently
   *short* produces a table of zeros rather than an error, which is the exact
   failure this experiment could not afford.

---

## THE EXTENDED-HOURS CONTAMINATION

`signal_engine.ENTRY_START` is enforced as a **lower bound only**:

```python
if now_et.time() < ENTRY_START:   # 09:30
    return {'passed': False, ...}
```

There is no upper bound anywhere in the system. Live, the market closing *is*
the upper bound and no code is needed for it. In a replay there is no market,
so the engine enters at 18:40 on a 900-share after-hours bar and
`backtester.run_symbol` fills it at the next bar's open.

**Measured on the control cell** — which `--selftest` proves is bit-identical
to the live `ripster_ema_cloud` — SPY from 2026-08-01:

```
entries by ET hour: {4:2, 9:7, 10:8, 11:2, 12:4, 13:6, 14:4, 15:6,
                     16:6, 17:9, 18:6, 19:8}
68 trades; 31 of them (45.6%) outside 09:30-16:00 ET
```

A sibling audit found the same defect on AAPL at 31 % (22 of 72). This is a
property of the **backtester's environment**, not of any engine, and it
affects every historical backtest number this system has produced.

**Why it is fatal specifically to this matrix.** Extended hours are 600 of the
session's 990 minutes, so the *count* of extended-hours bars scales inversely
with bar size. A 3Min cell sees ~3.3× as many of them as a 10Min cell and
would therefore book proportionally more trades the live book could never
take. The bias runs **in favour of exactly the timeframes under test**. Left
alone, the headline result of this document would have been an artifact of
after-hours SPY tape.

### The fix, and where it lives

`backtest_sweep.RTHGate` — a wrapper around any `StrategyEngine`, applied
identically to **all eighteen cells including the control**:

| `--rth` | behaviour |
|---|---|
| `entries` *(default)* | `ENTER_*` suppressed unless the deciding bar closes in `[09:30, 16:00)` ET. `EXIT_X_THEN_ENTER_Y` degrades to `EXIT_X`, so a position is never trapped by the gate. |
| `all` | No action at all outside the window. Arguably more faithful — an 18:40 exit is as impossible as an 18:40 entry — at the cost of holding to the next session's open, which is genuine overnight gap risk the live book also carries. |
| `off` | Unfiltered. Exists so the size of the contamination can be **measured** rather than asserted. |

A wrapper rather than an engine edit, for two reasons. It keeps the fix inside
`backtest_sweep.py`, which is in scope. And it leaves
`engine_ripster_tf`'s control cell bit-identical to the live engine, so the
gate is demonstrably an artifact of the **replay**, applied uniformly, rather
than a change to the strategy.

The interval is half-open at 16:00 on purpose: the backtester fills at the
**next** bar's open, so a bar closing exactly at 16:00 would fill after the
bell. A bar closing at 09:30 is the 09:20–09:30 pre-market bar and fills at
the 09:30 open, which is the opening drive `ENTRY_START` was moved to 09:30 to
catch.

**The bars themselves are not filtered.** Extended-hours bars stay in the EMA
windows, because the live cache contains them and the live clouds are shaped
by them. Dropping them would be a different strategy, not a cleaner backtest.

Every table in this document prints its RTH mode and an audit count of entries
that landed outside the window, which must be zero unless the mode is `off`.

---

## HOW TWO TIMEFRAMES FIT THROUGH ONE BACKTESTER

`engine_contract` says `primary` is *"the stream whose bar close drives
evaluation"*, and `backtester.run_symbol` loops over `bars_primary` and knows
exactly one other role, `macro`. Two streams, one clock. That is enough, given
one assignment:

```
primary = the FASTER of (entry_tf, exit_tf)
macro   = the slower one, declared only when they differ
```

Evaluation ticks on the fast clock. It has to: a fast exit that is only checked
at a slow bar close is not a fast exit. The slow stream is consulted as
**state** — whatever its most recently closed bar says.

### Can `run_symbol` drive exits off a different stream than entries?

**Yes, as written, with no change — but only because the `macro` role is
free.** Stated plainly, since it was asked:

- `run_symbol` advances its `macro_ptr` only while
  `bar_start + width <= bar_close`, so the engine is handed **closed** macro
  bars only. The no-forming-bar property survives at both bar sizes. The
  engine re-checks this against `now_et` anyway, because a live fetch is not
  as careful as the replay.
- Fills always happen at the **next primary bar's open**. When the exit stream
  is the slow one, the exit is detected at the first *fast* bar close after the
  slow bar closes and filled at the next *fast* open. That is more responsive
  than filling at the next slow open, and it is the honest reading of
  "evaluation is driven by the primary close."
- `run_symbol` supports exactly **two** roles. `engine_ripster` declares
  `macro = 1Hour` for `REQUIRE_1H`, which is `False`, so that stream is fetched
  and ignored. This variant **spends** that role on the second bar size. If
  `REQUIRE_1H` is ever turned on, `RipsterTFEngine.__init__` **raises** rather
  than silently dropping a rule — see PREREQUISITE EDITS.

### Two rules the asymmetry forces

Both are **provably no-ops when `entry_tf == exit_tf`**, which is what keeps
the control row clean.

**1. Edge-triggered entry.** `fresh_long` computed off a slow stream stays
`True` for *every* fast bar until the slow bar rolls. Left alone, a fast exit
would be followed by an instant re-entry on the next fast bar, then again, and
again — a churn machine, and one that looks *profitable* in a simulation
charging 2 bps. So a slow-stream entry may fire only on the first primary bar
at which a **new** entry-stream bar has closed:

```python
just_closed = (now_et - (last_entry_bar_start + entry_tf)) < primary_tf
```

Derived from timestamps, not remembered. The engine stays stateless, which is
what lets one instance serve a whole basket and what
`engine_contract.StrategyEngine` requires. No-op when the entry stream *is* the
primary stream.

**2. No entry into an exit.** If the exit stream already says "leave this
direction", the entry is refused rather than taken and immediately handed back.
The proof that this is a no-op in the symmetric case, on one stream:
`fresh_long` ⇒ `close > top(5/12)`; the trend gate ⇒ `close > top(34/50)`;
`long_exit` ⇒ `close < bottom(5/12)` **or** `close < bottom(34/50)`. Both
cannot hold on one bar. Mirror for shorts. So the control row is untouched by a
rule that exists only for the asymmetric cells.

### Which cloud belongs to which stream

| rule | stream |
|---|---|
| 1 — 34/50 hard trend gate | **entry** stream (it gates rule 2) |
| 2 — 5/12 fresh cross | **entry** stream |
| 3a — close back through 5/12 (`ride_end`) | **exit** stream |
| 3b — close through 34/50 (`structural`) | **exit** stream |
| 4 — opening pause + volume pace | see below |

They are the same cloud only when the timeframes are the same. **An asymmetric
cell can hold a position whose entry-side gate has already flipped.** That is
not a bug; it is precisely what "exit on a different timeframe" means, and it
is the behaviour under test.

### Rule 4 at other bar sizes

`signal_engine._volume_ok` is a **pace** test: volume closed so far inside
09:30–10:00 versus `OPEN_VOL_MIN_FRAC * ADV * elapsed/30`. The question was
whether it stays correct when the bars inside a fixed wall-clock window change
size. **It does**, and the reason is worth writing down:

- Numerator: whole bars with `09:30 ≤ t < 10:00`, summed. Ten 3-min bars or
  three 10-min bars cover the same thirty minutes and the same shares.
- Denominator: `_dayvol` sums every bar of a prior date — also bar-size
  invariant.
- Proration: at a bar close, `now_et = bar_start + tf`, so the closed volume
  covers exactly `elapsed` minutes, at **every** bar size. The ratio is
  therefore comparable across the matrix.

Two caveats that *are* real:

- **It is evaluated on the PRIMARY (fastest) stream in every cell**, entry
  stream or not. Volume is a property of the tape, not of a cloud. Feeding the
  test a slow stream while `now_et` ticks on a fast one would compare an
  up-to-date denominator against a numerator up to one slow bar stale, and read
  low by construction — which is *exactly* the bug the pace test was written to
  fix when `ENTRY_START` moved to 09:30.
- The **ADV thinness** problem described under the warm-up rule. Same window,
  fewer sessions.

`ENTRY_START`, `OPEN_VOL_MIN_FRAC`, `OPEN_WINDOW_END`, `REQUIRE_VOLUME` and
`REQUIRE_1H` are all read **live from `signal_engine`** rather than copied, so
this variant tracks any edit to the real spec instead of forking it.

---

## `engine_ripster_tf.py`

```python
eng = engine_ripster_tf.build('3Min', '10Min', 'scaled')
eng.name          # 'ripster_tf_e3m_x10m_scaled'
eng.timeframes    # {'primary': '3Min', 'macro': '10Min'}
eng.history       # {'primary': 1002, 'macro': 300}
eng.execution     # 'options_combo'  (unchanged)
```

Full matrix, as the module prints it with `py -3 engine_ripster_tf.py`:

| engine | primary | macro | entry periods | exit periods | warm-up |
|---|---|---|---|---|---|
| `ripster_tf_e10m_x10m_stock` | 10Min | – | 5/12/34/50 | 5/12/34/50 | 250 | **← CONTROL** |
| `ripster_tf_e3m_x3m_stock` | 3Min | – | 5/12/34/50 | 5/12/34/50 | 250 |
| `ripster_tf_e3m_x6m_stock` | 3Min | 6Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e3m_x10m_stock` | 3Min | 10Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e6m_x3m_stock` | 3Min | 6Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e6m_x6m_stock` | 6Min | – | 5/12/34/50 | 5/12/34/50 | 250 |
| `ripster_tf_e6m_x10m_stock` | 6Min | 10Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e10m_x3m_stock` | 3Min | 10Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e10m_x6m_stock` | 6Min | 10Min | 5/12/34/50 | 5/12/34/50 | 250 / 250 |
| `ripster_tf_e3m_x3m_scaled` | 3Min | – | 17/40/113/167 | 17/40/113/167 | 835 |
| `ripster_tf_e3m_x6m_scaled` | 3Min | 6Min | 17/40/113/167 | 8/20/57/83 | 835 / 415 |
| `ripster_tf_e3m_x10m_scaled` | 3Min | 10Min | 17/40/113/167 | 5/12/34/50 | 835 / 250 |
| `ripster_tf_e6m_x3m_scaled` | 3Min | 6Min | 8/20/57/83 | 17/40/113/167 | 835 / 415 |
| `ripster_tf_e6m_x6m_scaled` | 6Min | – | 8/20/57/83 | 8/20/57/83 | 415 |
| `ripster_tf_e6m_x10m_scaled` | 6Min | 10Min | 8/20/57/83 | 5/12/34/50 | 415 / 250 |
| `ripster_tf_e10m_x3m_scaled` | 3Min | 10Min | 5/12/34/50 | 17/40/113/167 | 835 / 250 |
| `ripster_tf_e10m_x6m_scaled` | 6Min | 10Min | 5/12/34/50 | 8/20/57/83 | 415 / 250 |
| `ripster_tf_e10m_x10m_scaled` | 10Min | – | 5/12/34/50 | 5/12/34/50 | 250 |

Note the last row: scaling by `10/10` is the identity, so
`ripster_tf_e10m_x10m_scaled` **must** produce numbers identical to the
control. The sweep asserts it at the bottom of every run. If those two rows
ever disagree, nothing else in the table is worth reading.

### Why `context()` is a copy of `trend_context()`

`signal_engine.trend_context` reads its periods from module constants. The one
thing this file must never do is reach in and rebind them: the live engine's
behaviour would then depend on whether an experiment happened to be imported.
So `engine_ripster_tf.context(df, periods)` is the same arithmetic with the
four periods as an argument, and the drift risk is handled by **testing** it
rather than by hoping (next section).

### One performance deviation, and it is a diagnostic one

Eighteen cells × four symbols × ~40,000 three-minute bars is a few million
evaluations. Two things dominated one evaluation: rebuilding a DataFrame with
parsed timestamps, and the ADV scan inside `signal_engine._volume_ok`. Neither
is needed on a bar where nothing happens. So:

- the four rules run off a bare `Series` of **closes**, which is all any of
  them reads; timestamps are parsed only where something needs them;
- the **volume gate is deferred** until an entry candidate has survived every
  other test.

`_volume_ok` is a pure function of the bars and the clock, so calling it later
in the same evaluation cannot change its answer. `backtester.run_symbol` reads
`decision['volume_ok']` only when the action is an entry. **The action is
identical either way; the `volume_ok` *diagnostic* differs on non-entry bars**,
where it is emitted as `True` — the contract's documented default for engines
that do not compute it. That is the whole of the deviation, and it is stated
because if this variant were ever mounted live, `app.py`'s signal log would
show an uninformative `volume_ok` on quiet bars.

Measured: 11.0 s → 1.3 s on the control cell, 98.6 s → 5.8 s on
`e3m_x10m_scaled`, **with byte-identical trade counts, P/L and breakeven
slippage before and after.** That equality is the evidence the deferral is
behaviour-neutral, not the argument above.

### Registration

`import engine_ripster_tf` registers all 18 variants into
`engines_bootstrap.registry`, skipping any name already present and **never**
touching `ripster_ema_cloud`, which stays the resolver's default. That is
enough for anything running in-process. It is **not** enough for
`backtester.run_basket_parallel`, whose workers re-import `engines_bootstrap`
in a fresh process and would not see them — which is why `backtest_sweep.py`
parallelises across **cells** itself and calls the serial `run_symbol`.

---

## `backtest_sweep.py`

```
py -3 backtest_sweep.py --selftest --symbols SPY --start 2026-08-01
py -3 backtest_sweep.py --symbols SPY --start 2026-04-01 --end 2026-09-19
```

### The one thing that would have made the table a lie

The cells have different warm-ups: 835 three-minute bars for
`e3m_x3m_scaled`, 250 ten-minute ones for the control. Hand each cell a
warm-up-padded fetch and let `run_basket` replay the lot, and **each cell goes
live on a different calendar day** — the fast/scaled cells last, after the
others have already banked or lost a week of trades. Every difference in the
table would then be part strategy and part calendar, with no way to separate
them.

So each symbol is replayed through `backtester.run_symbol` **directly**, with
`min_window` set to the index of the first bar at or after `--start`.
Evaluation begins on the same calendar bar in every cell, with each cell's own
full history behind it. `run_symbol`'s docstring warns that `min_window` must
never exceed the engine's own warm-up *"or the replay diverges from live"* —
that is exactly what is being done, deliberately. The skipped evals are the
ones inside the warm-up pad, which is borrowed data, not the measured window.
**Inside** the measured window the replay is bar-for-bar the live loop.

The pooled aggregation (trades, summed daily marks, one equity curve, one
`compute_metrics`) is `run_basket`'s, line for line. It is repeated only
because `run_basket` does not pass `min_window` through. See PREREQUISITE
EDITS.

### Metrics

All from `backtester.compute_metrics` — `n_trades`, `win_rate_pct`,
`expectancy` (avg P/L), `total_pnl`, `max_drawdown_pct`, `profit_factor`,
`sharpe`, `gross_pnl`, `slippage_cost`, `breakeven_slippage_bps`,
`turnover_annual`, `avg_bars_held`.

Two derived, because `compute_metrics` does not carry them:

- **`time_in_market_pct`** = primary bars held ÷ primary bars evaluated.
  **Bar-time, not wall-clock**, on purpose: Ripster holds overnight, and a
  wall-clock denominator would charge a 10Min cell for the 17 hours a night it
  was never asked about.
- **`avg_hold_min`** = `avg_bars_held × primary_tf_minutes`. "Twelve bars"
  means three different things across this matrix; minutes mean one.

### Sizing and cost assumptions — all judgement calls

- **`--alloc-dollars 10000` flat per position** (default). A flat dollar budget
  keeps per-trade P/L comparable once trade counts diverge by 5×, which
  percent-of-capital would not.
- **`--slippage-bps 2.0`**, the same default `backtest_run.py` uses. On a
  ~$600 SPY share that is about 12 cents a round trip: generous for SPY
  shares, and **meaningless for SPY options**, which is what actually trades.
- Therefore the column to read is **`BE slip`** — `breakeven_slippage_bps` out
  of `compute_metrics`, the slippage at which a cell's gross P/L reaches zero.
  If it is below the round trip you actually pay, the cell does not exist.
- `backtester`'s own standing caveat applies and is worse here than usual:
  this simulates the **underlying**. Live longs are calls + shares and live
  shorts are puts. The options round trip costs vastly more than the share
  spread and is paid **once per trade**, so halving the bar size does not just
  halve the edge per trade — it doubles the number of times you pay the wide
  spread. **The single most likely wrong conclusion from this matrix is a 3Min
  cell that wins gross and loses net.**

### Bar cache

3Min SPY for seven months is ~52,000 bars and took 83 seconds to pull; the
matrix wants the same three streams eighteen times. `backtest_sweep` caches
each `(symbol, timeframe, start, end)` stream as JSON under
`%LOCALAPPDATA%\FoundationsTrading\tf_sweep_cache\` and re-reads it. The cache
is also the transport to the worker processes — re-reading a file beats
pickling 50,000 dicts per cell into a pool.

### `--selftest`

Not optional. **A matrix whose control row is not the live system compares
everything to nothing.** Two assertions, both on real bars:

1. `engine_ripster_tf.context(df, BASE_PERIODS)` equals
   `signal_engine.trend_context(df)` on every decision-relevant key, across
   hundreds of rolling windows. This is what catches the copy drifting.
2. The control cell's trades are **identical** to `RipsterEMACloudEngine`'s
   over the same bars with the same `min_window` — same entries, same exits,
   same prices, same quantities. Only `exit_reason` may differ, by the
   `|10Min` `exit_tag` this variant adds for reporting.

**Result on SPY, `--start 2026-08-01` (2026-09-25):**

```
  SPY: context == trend_context on 404 windows (ok)
  SPY: control cell == ripster_ema_cloud on all 74 trades (ok)
SELFTEST PASSED
```

---

## RESULTS

**Run:** SPY, `--start 2026-08-01` through 2026-09-25 (~8 weeks), `--rth entries`,
`--workers 2`, capital 100k, 10k per position, default slippage. Selftest passed
immediately before. Produced 2026-09-25.

### Period mode: STOCK — periods stay 5/12/34/50, so smaller bars mean shorter horizons

```
  entry    exit  gateH  trades       L/S   win%    avgP/L      totP/L  maxDD%  BE slip   vs control
  10Min   10Min    8.3      48      46/2   25.0     -0.31      -14.94    0.19     1.84      CONTROL
   3Min    3Min    2.5     119     84/35  15.97     -3.18     -378.78   0.573     0.35     -363.84
   3Min    6Min    2.5      95     61/34  18.95     -2.70     -256.37   0.484     0.61     -241.43
   3Min   10Min    2.5      74     46/28  21.62     -3.14     -232.46   0.497     0.38     -217.52
   6Min    3Min    5.0     104     69/35  20.19     -3.20     -332.95   0.443     0.35     -318.01
   6Min    6Min    5.0      79     67/12  24.05     -3.15     -248.57   0.377     0.38     -233.63
   6Min   10Min    5.0      48      41/7  18.75     -4.87     -233.84   0.347    -0.51     -218.90
  10Min    3Min    8.3      75     52/23   20.0     -2.68     -201.09   0.319     0.61     -186.15
  10Min    6Min    8.3      62      54/8  27.42     -2.86     -177.52   0.287     0.52     -162.58
```

### Period mode: SCALED — periods x 10/tf_min, so every cloud spans the same wall clock

```
  entry    exit  gateH  trades       L/S   win%    avgP/L      totP/L  maxDD%  BE slip   vs control
  10Min   10Min    8.3      48      46/2   25.0     -0.31      -14.94    0.19     1.84      CONTROL
   3Min    3Min    8.3      76      69/7  17.11     -4.04     -307.01   0.412    -0.09     -292.07
   3Min    6Min    8.3      58      53/5  22.41     -3.02     -175.34   0.254     0.43     -160.40
   3Min   10Min    8.3      44      40/4  22.73     -5.21     -229.03   0.254     -0.7     -214.09
   6Min    3Min    8.3      66      62/4  18.18     -3.60     -237.67   0.356     0.14     -222.73
   6Min    6Min    8.3      61      56/5  22.95     -3.66     -223.00   0.309      0.1     -208.06
   6Min   10Min    8.3      40      38/2   20.0     -6.22     -248.72   0.299    -1.24     -233.78
  10Min    3Min    8.3      56      54/2  21.43     -1.46      -81.54   0.228     1.24      -66.60
  10Min    6Min    8.3      53      50/3  24.53     -1.67      -88.71   0.208     1.13      -73.77
  10Min   10Min    8.3      48      46/2   25.0     -0.31      -14.94    0.19     1.84       +0.00
```

`10Min/10Min/scaled` reproduces the control exactly, as it must: scaling by
`10/10` is the identity. It is left in as a running check on the harness.

---

### What the matrix says

**Nothing beats the control. Not one of the seventeen cells.** The control is
the worst-performing configuration only in the sense that it loses least: it
is -$14.94 and every alternative ranges from -$81.54 to -$378.78. The spread
across the matrix is an order of magnitude wider than the control's own
result, which is itself the cleanest evidence that bar size is not a free
parameter here.

**The owner's hypothesis is inverted by this sample.** The proposal was fast
entry, slower exit -- 3-min to get in, 6 or 10-min to manage out. Every
3-min-entry cell is among the worst in its mode. The two best non-control
cells are the opposite arrangement: `10Min` entry with a faster exit
(`10Min/3Min/scaled` at -$81.54, `10Min/6Min/scaled` at -$88.71). So the
*exit* half of the intuition survives contact with the data and the *entry*
half does not. Both still lose to doing nothing different.

**The mechanism is visible in the L/S column, and it is the trend gate.** The
control takes 46 longs and 2 shorts. `3Min/3Min/stock` takes 84 longs and 35
shorts -- seventeen times the short count. This is exactly the predicted
consequence of holding the periods constant while shrinking the bar: the
34/50 hard gate spans 2.5 hours instead of 8.3, flips regime far more often,
and each flip opens the short side.

That matters more than a generic whipsaw argument, because of what the
journal says about which side this account can trade. Over 206 paired legs:

```
  bull  n=115  avg P/L    +0.57   total    +65.86
  bear  n= 91  avg P/L   -34.11   total  -3104.07
```

Essentially the entire realized loss in the book is on the short side. A
faster trend gate manufactures short signals, and short signals are where
this account bleeds. The stock-mode cells lose on a mechanism the owner's own
history already documents.

Note that scaled mode largely removes this -- `3Min/3Min/scaled` takes 69/7
rather than 84/35, because its gate still spans 8.3 hours -- and scaled mode
is correspondingly less bad in aggregate. The two rows are measuring two
different failures, which is why they were never allowed to share a table.

**The cost column kills the fast cells independently.** Breakeven slippage on
the control is 1.84 bps. Four cells are *negative* (`6Min/10Min/stock` -0.51,
`3Min/3Min/scaled` -0.09, `3Min/10Min/scaled` -0.70, `6Min/10Min/scaled`
-1.24), meaning they lose before any transaction cost is applied at all.
Several more sit under 0.5 bps. Turnover roughly doubles from the control's
29.73/yr to 74.26/yr at `3Min/3Min`. And this simulation trades the
UNDERLYING; the live book trades options, where the round trip costs far more
than the share spread. The real bar is higher than the printed one for every
row.

### What this does not say

One symbol. One date range of about eight weeks. One market regime. Cells
carry 40 to 119 trades. Seventeen numbers from one sample are seventeen
observations, and the honest summary is **"no configuration tested here earns
a change to the live engine"**, not "10-min is optimal."

The control lost money too. This matrix ranks configurations of a system that
was unprofitable over this window; it does not establish that any of them
works. A fair reading is that bar size is not the variable standing between
this system and profitability, and that looking there further has a poor
expected return compared with the environment-grading work in `design/01`-`04`.

The one result worth a follow-up is the asymmetric pair -- `10Min` entry with
a `3Min` or `6Min` exit -- which is the only family that stays within
$90 of the control while showing a breakeven slippage above 1 bps. If any
timeframe work continues, it should start and probably end there, on more
symbols and a longer window.


---

## PREREQUISITE EDITS

Four, none made. Listed so they are decisions rather than discoveries.

0. **`signal_engine.py` — an upper bound next to `ENTRY_START`.** The
   producer-side fix for the extended-hours contamination:
   ```python
   ENTRY_END = dtime(15, 50)    # or 16:00, a judgement call in itself
   ...
   if now_et.time() >= ENTRY_END:
       return {'passed': False, 'volume_ok': False,
               'reason': f'closing pause: no entries after {ENTRY_END}'}
   ```
   `backtest_sweep.RTHGate` is a **replay-side** patch. It makes this matrix
   honest and does nothing for `backtest_run.py`, the screener, or anything
   else that replays bars. **Adopting the producer-side fix would change every
   historical backtest number in the system**, in the direction of fewer
   trades and, on the SPY control cell, roughly 46 % fewer entries. It is a
   real decision about the strategy's specification, not a bug-fix to wave
   through, and it is not made here. Note also that it is live-path-neutral by
   construction: live, no such entry can fill anyway.

   *This is a live file and was not touched.*

1. **`backtester.run_basket` — pass `min_window` through.**
   ```python
   def run_basket(engine, bars_by_symbol, ..., min_window=None):
       ...
       r = run_symbol(engine, sym, bars, macro_by_symbol.get(sym),
                      ..., min_window=min_window)
   ```
   `run_symbol` already takes it; `run_basket` simply does not forward it.
   Without this, a sweep that needs every cell to start evaluating on the same
   calendar bar has to re-implement `run_basket`'s aggregation, which
   `backtest_sweep._aggregate` does today. The edit would delete that function.
   *This is a live file and was not touched.*

2. **`engines_bootstrap.py` — `import engine_ripster_tf`.**
   One line, after the existing registrations. It makes the 18 variants
   visible to `backtest_run.py --engine ...`, to `run_basket_parallel`'s worker
   processes, and to anything else that goes through the shared registry. It
   does **not** change the resolver's default, which stays
   `ripster_ema_cloud`. Without it the variants are visible only to code that
   imports the module itself, which today means `backtest_sweep.py`.
   *Not a live file by the letter of the constraint, but it is the file that
   decides what the live loop can mount, so it is listed here rather than
   edited.*

3. **A third stream role, if `signal_engine.REQUIRE_1H` is ever turned on.**
   `run_symbol` understands `primary` and `macro` and nothing else. This
   variant spends `macro` on the second bar size, so a 1-hour confirmation has
   nowhere to live. `RipsterTFEngine.__init__` **raises** when `REQUIRE_1H` is
   `True` rather than dropping rule 4's optional confirmation quietly.
   Restoring it needs a `macro2` role in `run_symbol`'s window loop plus the
   matching fetch in the runner — a real change, not a one-liner, and it should
   not be made speculatively.

---

## WHAT THIS DOES NOT DO, ON PURPOSE

- **No live path change.** `signal_engine.py`, `engine_ripster.py`,
  `backtester.py`, `app.py`, `trade_router.py` and `alpaca_manager.py` are
  byte-identical. The resolver default is still `ripster_ema_cloud`.
- **No new rules.** No stops, no targets, no trailing exits, no per-timeframe
  tuning of `OPEN_VOL_MIN_FRAC` or `ENTRY_START`. Every constant that is not
  the bar size or the EMA periods is read live from `signal_engine`. The
  matrix is a measurement of one change, not a new strategy.
- **No optimiser, no ranking loop, no "mount the winner".** The same rule
  `engine_contract.characterize` states: *measures, never selects*. Eighteen
  cells over one sample is precisely the amount of data from which an
  automatic selector would manufacture a confident overfit.
- **No options model.** Inherited from `backtester.py`, and the reason the
  `BE slip` column carries the weight here.
- **No third period mode** (centre-of-mass scaling), **no timeframes beyond
  3/6/10**, and **no intraday flat-by-close rule**, though the last is the
  most obvious next question: `inMkt%` and overnight gap risk are entangled in
  every number in the table.
