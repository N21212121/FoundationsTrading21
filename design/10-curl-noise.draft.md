# 10 — The curl noise guard, rebuilt on candle bodies (DRAFT)

**Status:** draft, nothing implemented. `environment.py` does not exist yet, so
neither does the guard this replaces. No `.py` file and no `static/index.html`
is modified by this document. It changes **four constants and one paragraph** of
`design/02 §1`, which is not edited here — a settled spec is the owner's call
and this file carries `.draft` until he makes it.

What is built today: nothing in `design/02`. What exists to build on:
`signal_engine.trend_context` (the 5/12 geometry and the fresh-cross event),
`signal_engine.bars_to_df`, `levels.atr`, `halflife.tf_minutes`,
`alpaca_manager.get_bars_multi`, `bar_cache.BarCache`. The bars this document
measures came out of the last two.

---

## 0. WHY THIS EXISTS

Board row `d-curl-noise`, ruled by the owner 2026-09-30:

> *"Let's instead do it by candle open and close size across a moving range of,
> I don't know, 11 candles to start. The actual Ripster strategy is supposed to
> be something like 3 candles of increasing volume, but I don't want a specific
> volume monitor to be hooked into this particular part of the system. Maybe
> have the researcher bot find out a good lookback period on this."*

Three instructions, all honoured below: **candle body, not ATR**; **no volume
in this measure**; **find the lookback from data**. He offered 11 as a guess
and said so, which is the only reason §4 is allowed to argue with it.

### What it replaces, exactly

`design/02 §1` specifies `CURL_NOISE_ATR = 0.10` under the heading *"The noise
guard — what separates a curl from a flat tape"*:

```
gap_travel = |gap[-1] - gap[-1 - CURL_LOOKBACK]|
if atr_i is None or gap_travel < CURL_NOISE_ATR * atr_i:
    state = 'flat'
```

and it says of the constant, in its own **Thresholds and where they come from**
table: *"Judgement, and the number I am least sure of… Until then, 0.10 is a
guess with a rationale and nothing more."*

That caution was warranted, and it understates the problem. The measurement in
§1 below says the guard as specified **does not work at all** — not "is
miscalibrated", but *has the wrong sign on the engine's own timeframe*. So this
draft is not a cosmetic substitution of one normalizer for another. The owner's
instinct to move off ATR was right, and the reason it was right is bigger than
the reason he gave.

### What this draft is: a display, not a decision

Settled first, per `design/00 §2.2`. This guard is part of the **measurement
layer** that feeds the GPA. The GPA grades environment; it measures rather than
decides, and no order is placed or withheld because of it. That is what exempts
it from the configurable rule. §10 answers the rule anyway, because the answer
is favourable and worth having on the record before someone proposes wiring it
into `engine_ripster`.

### The data this document is built on

| source | symbols | timeframes | window | bars |
|---|---|---|---|---|
| `alpaca_manager.get_bars_multi`, fetched for this study | 52 (all 48 journal tickers + `KO`, `XLU`, `AAPL`, `NVDA`) | `10Min`, `6Min` | 2026-06-15 → 2026-09-30 | 289,051 / 306,620 raw |
| `%LOCALAPPDATA%\FoundationsTrading\tf_sweep_cache` | `SPY`, `QQQ`, `AAPL`, `NVDA` | `3Min`, `6Min`, `10Min` | 2026-03-11 → 2026-09-19 | as cached |
| `%LOCALAPPDATA%\FoundationsTrading\journal.jsonl` | 48 | — | 2026-07-14 → 2026-09-25 | 386 graded `ripster` fills |

The character set for the headline tests is fifteen names chosen to differ:
`SPY` and `QQQ` (index), `AAPL` `IBM` `INTC` `UBER` (megacap, ordinary),
`NVDA` `TSLA` `MU` (high-vol large), `HOOD` `SOFI` `RKLB` `MRNA` (retail-fast),
`KO` and `XLU` (low-vol — the two names most likely to break a body-size measure,
because their bodies are 3 to 7 cents).

---

## 1. THE FIRST FINDING IS ABOUT THE INCUMBENT, AND IT IS BAD

Set up exactly what `design/02 §1` specifies — `ema(close,5)`, `ema(close,12)`,
`gap`, `slope` over `CURL_LOOKBACK = 3`, `bars_to_cross = -gap/slope`, fire when
convergent and `0 <= bars_to_cross <= 5`. On RTH `10Min` bars across the fifteen
names that detector fires on **48.1% of all bars** (n = 8,311 firings). Score
each firing by whether price covered one random-walk displacement
(`atr_i * sqrt(H)`, the `design/02 §2` null) **in the direction the curl
favours** within `H` bars. Base rate at `H = 6`: **7.82%**.

Now apply `gap_travel / atr_i` as a filter at a range of thresholds, and record
what the surviving firings hit:

| kill firings below | threshold | keeps | hit(kept) | hit(killed) | lift |
|---|---|---|---|---|---|
| p20 | 0.072 | 0.96 | 7.81% | 8.04% | −0.0001 |
| p30 | 0.109 | 0.90 | 7.65% | 9.40% | −0.0017 |
| p40 | 0.151 | 0.82 | 7.44% | 9.59% | −0.0038 |
| p50 | 0.196 | 0.73 | 7.20% | 9.47% | −0.0062 |
| p60 | 0.246 | 0.61 | 7.09% | 8.98% | −0.0073 |

**Every row is negative.** The bars the incumbent guard throws away hit *more
often* than the bars it keeps. `CURL_NOISE_ATR = 0.10` sits at roughly p30, so
the specified constant is on the wrong side of a filter that is itself inverted.
On `6Min` the same table is flat rather than inverted (lift between −0.0004 and
+0.0017, i.e. nothing).

The mechanism is not subtle once seen. `gap_travel` is the distance the 5/12
separation moved, and the pair's separation moves *most* when the two EMAs are
far apart and fanning — which is when a cross is far away and a *curl* is least
likely to complete. The guard is correlated with the wrong half of its own
numerator. `design/02` says the guard is *"on the NUMERATOR OF THE MOVEMENT, not
on the ratio"*; that is precisely the error. The numerator is a property of the
indicator; the thing being guarded against — a dead tape — is a property of the
tape.

**This is the load-bearing argument for the owner's ruling.** He asked to move
the guard onto candle size. Candle size is a property of the tape. The category
was what was wrong.

---

## 2. THREE WAYS TO READ THE RULING, AND WHICH ONE THE DATA PICKS

"Do it by candle open and close size across a moving range of 11 candles" admits
three implementations. All three were measured. Let `body[t] = |close[t] −
open[t]|` throughout, and let `B_N` be the mean body over the last `N` closed
bars.

**(i) Substitution.** Swap the normalizer, keep the structure:
`flat if gap_travel < k * B_N`. The minimal edit to `design/02`.

**(ii) Expansion.** `E_N = mean(body of last 3) / B_N`, `flat if E_N < k`. The
reading that takes his second sentence seriously: *"3 candles of increasing
volume"* with body substituted for volume, compared against the rolling window
he named. Recent candle size against the window's candle size — "across a moving
range of 11 candles" as a comparison, not as a smoother.

**(iii) Conviction.** `B_N / mean(range over N)`: what fraction of the bar's
travel ends up between open and close. Wicky bars score low. The only one of the
three that uses body size *because it is not the range*, so the purest test of
whether "open and close" carries information the high and low do not.

Separation test, pooled over the fifteen names, RTH bars only. Positive class:
the next 6 bars contain a move of ≥ 1.0 random-walk displacements. Negative
class: < 0.4 (the `design/02 §2` `none` band). AUC is P(statistic higher on a
positive than on a negative); 0.500 is a coin.

**`10Min`, n = 25,428 bars (4,805 positive / 9,326 negative):**

| statistic | N=5 | N=8 | N=11 | N=15 | N=21 | N=34 |
|---|---|---|---|---|---|---|
| (i) substitution `gap_travel / B_N` | 0.478 | 0.507 | 0.531 | 0.550 | 0.556 | 0.499 |
| (ii) expansion `E_N` | 0.581 | 0.625 | **0.655** | **0.676** | **0.673** | 0.572 |
| (iii) conviction `B_N / range_N` | 0.510 | 0.505 | 0.496 | 0.487 | 0.484 | 0.495 |
| incumbent `gap_travel / atr_i` | 0.521, no N | | | | | |

**`6Min`, n = 26,962:**

| statistic | N=5 | N=8 | N=11 | N=15 | N=21 | N=34 |
|---|---|---|---|---|---|---|
| (i) substitution | 0.498 | 0.523 | 0.539 | 0.555 | 0.571 | 0.576 |
| (ii) expansion `E_N` | 0.569 | 0.602 | 0.623 | 0.641 | **0.659** | 0.658 |
| (iii) conviction | 0.481 | 0.476 | 0.471 | 0.470 | 0.472 | 0.475 |
| incumbent | 0.553, no N | | | | | |

Three conclusions.

- **Conviction is dead.** 0.47–0.51 everywhere, on both timeframes, at every
  lookback. The body-to-range ratio of a window carries no information about
  whether the next two hours contain a move. Considered, measured, rejected —
  and it is the one I expected to work, because "small bodies, long wicks" is
  what chop *looks* like.
- **Substitution barely moves.** It inherits §1's defect: same numerator,
  different denominator, and the denominators are the same thing to within a
  scale factor. Measured: median `B_15 / atr_i = 0.459` on `10Min`, `0.487` on
  `6Min`. So `CURL_NOISE_ATR = 0.10 ATR` is **0.218 mean bodies** — the two
  constants are near-translations of each other, and there was no reason to
  expect a different result. There was not one.
- **Expansion works**, and it is the reading closest to his own second sentence.

Repeat the §1 precision test with `E_15` on `10Min`:

| kill firings below | threshold | keeps | hit(kept) | hit(killed) | lift |
|---|---|---|---|---|---|
| p20 | 0.469 | 0.84 | 8.32% | 5.12% | **+0.0050** |
| p30 | 0.567 | 0.75 | 8.80% | 4.85% | **+0.0098** |
| p40 | 0.657 | 0.66 | 9.26% | 5.04% | **+0.0144** |
| p50 | 0.750 | 0.56 | 9.84% | 5.24% | **+0.0202** |
| p60 | 0.844 | 0.45 | 10.65% | 5.47% | **+0.0283** |

Positive at every threshold, on both timeframes, with the killed bucket at half
the rate of the kept bucket. That is a guard.

### Why it works, stated honestly

Not because bodies detect curls. Because **volatility is autocorrelated**. Three
recent candles larger than the window's average is a nowcast that the current
regime is the active one, and an active regime is more likely to still be active
in an hour. This is the ARCH effect — Engle (1982), *Autoregressive Conditional
Heteroskedasticity with Estimates of the Variance of United Kingdom Inflation*,
Econometrica 50(4) — applied at a horizon of minutes with the crudest available
estimator. It is not new physics and the draft should not pretend it is. What
the owner has found is that the right thing to condition a noise guard on is the
tape's **current activity level relative to its own recent history**, which is a
different quantity from either the indicator's movement (§1) or the tape's
absolute volatility (`atr_i`, a 14-bar Wilder average, which almost by
construction cannot tell you that the last three bars are unusual).

`design/02` says of the module: *"It does not predict."* That still holds. `E_N`
is computed entirely from closed bars at `t` and describes the tape as it stands.
Forward outcomes appear in this document **only to choose a constant**, which is
exactly the role `design/02 §2` gives to tag frequency when it proposes setting
`MOM_WEAK` from the distribution of readings at entry.

---

## 3. THE LOOKBACK IS A DURATION, NOT A BAR COUNT

This answers the brief's second hard constraint, and it is the finding with the
widest consequences.

Sweep `N` from 4 to 40 (to 80 on `3Min`) on the cached `SPY`/`QQQ`/`AAPL`/`NVDA`
series, one AUC per `N`, and read the argmax in **minutes**:

| timeframe | n bars | best N | best N in minutes | AUC | AUC at N=11 |
|---|---|---|---|---|---|
| `3Min` | 69,000 | 34 | **102 min** | 0.606 | 0.573 (33 min) |
| `6Min` | 34,420 | 24 | **144 min** | 0.632 | 0.594 (66 min) |
| `10Min` | 20,588 | 15 | **150 min** | 0.640 | 0.628 (110 min) |

Three timeframes, three different bar counts, **one duration**. The optimum is
100 to 150 minutes of wall clock on all three. The bar count that achieves it
differs by a factor of 2.3 between the screener's feed and the engine's.

Confirm a second way, with the threshold held fixed at 0.70 rather than at a
percentile, scoring an 18-bar horizon:

| N | `10Min` minutes | lift | N | `6Min` minutes | lift |
|---|---|---|---|---|---|
| 8 | 80 | +0.0104 | 8 | 48 | +0.0090 |
| 11 | 110 | +0.0141 | 11 | 66 | +0.0118 |
| 13 | 130 | **+0.0183** | 15 | 90 | +0.0177 |
| 15 | 150 | **+0.0178** | 21 | 126 | **+0.0217** |
| 21 | 210 | +0.0044 | 25 | 150 | **+0.0205** |
| 34 | 340 | −0.0314 | 34 | 204 | +0.0191 |

Same answer: 130–150 minutes on `10Min`, 126–150 on `6Min`. The bar counts that
reach it are 13–15 and 21–25.

**Recommendation: the constant is `CURL_BODY_MINUTES = 150`, and the bar count
is derived per timeframe via `halflife.tf_minutes`.** 150 sits inside the
plateau on all three timeframes tested: it *is* the `10Min` argmax; it is within
0.0001 AUC of the `6Min` argmax; it is within 0.003 of the `3Min` argmax. The
plateau is broad enough — within 0.004 AUC over 130–190 minutes on `10Min` —
that nobody should defend 150 against 140.

```python
def _body_bars(bar_minutes):
    """The body window, in bars, for whatever timeframe the caller is on."""
    n = round(CURL_BODY_MINUTES / bar_minutes)
    return max(CURL_BODY_MIN_BARS, int(n))
```

`10Min → 15`. `6Min → 25`. `3Min → 50`. `1Hour → 3`, clamped to
`CURL_BODY_MIN_BARS = 8`, and see §12.3 for why that clamp is a confession
rather than a feature.

### This contradicts `design/02`'s own open decision, and the contradiction is the point

`design/02` **OPEN DECISIONS #1** says: *"If `6Min` must stay, then every
bar-count constant here needs a second column and the module needs `bar_minutes`
to select it — worse, and worse for the same reason."*

A reasonable prior, and the measurement contradicts it:

- It is not a second column. It is one division by a helper that already exists,
  is already used by `bar_cache._tf_minutes`, and whose docstring already makes
  this argument — *"TF_MINUTES is a menu, not a contract. Every module that
  keyed a private dict off it broke the day a new timeframe was added."* A
  constant in minutes is the version of that constant that cannot be wrong on a
  timeframe nobody tested.
- Holding the bar count fixed does not make the two feeds agree; it makes them
  measure different things while *appearing* to agree. `CURL_BODY = 11` means
  110 minutes to the engine and 66 to the screener, and the lift table prices
  that: 66 minutes on `6Min` delivers 54% of the achievable lift.
- §6 produces the sharper version of the same result on a different constant,
  where a fixed bar count does not merely underperform — it **inverts the sign**
  of a `design/02 §3` classification between the two feeds.

The timeframe question itself is settled and this draft does not reopen it: the
engine stays on `10Min`, the screener stays on `6Min` until someone rules
otherwise. This draft's job is to be correct on both, and a duration is how.

---

## 4. IS 11 THE RIGHT STARTING POINT?

He asked. The honest answer has two halves.

**On the engine's `10Min` feed, 11 is fine and not optimal.** 11 bars = 110
minutes, AUC 0.628 against a peak of 0.640 at 15 bars; fixed-threshold lift
+0.0141 against +0.0183 at 13. That is **77% of the achievable benefit**, from a
number he produced off the top of his head with an "I don't know" attached. The
intuition was good.

**On the screener's `6Min` feed, 11 is short by half.** 11 bars = 66 minutes,
AUC 0.594 against 0.632 at 24 bars; lift +0.0118 against +0.0217 at 21. **54% of
the achievable benefit.** Not broken — still the right sign, still far better
than the ATR guard it replaces — but leaving half the effect on the table for no
reason other than that the constant was expressed in the wrong unit.

So: **not 11 bars. 150 minutes**, which is 15 bars where he was looking when he
said it, and 25 bars where the GPA will actually run. If he prefers a bar count
because a bar count is what a chart shows, then the number is **15 on `10Min`**,
and the draft states plainly that the same 15 on `6Min` costs about a quarter of
the effect.

### The numerator: his "3 candles" is right

Sweeping the numerator window with the denominator fixed at 150 minutes, AUC on
`10Min`: 1 bar 0.567, **2 bars 0.582**, **3 bars 0.581**, 4 bars 0.576, 5 bars
0.566, 6 bars 0.553. On `6Min`: 0.550 / **0.560** / **0.559** / 0.555 / 0.550 /
0.544. Two and three are tied at the peak on both feeds, separated by 0.001, and
3 is what the Ripster material says. Keep 3, and keep it as a **bar count rather
than a duration** — the numerator's job is "is it working right now", and it is
the one place where the freshest available candles are the correct unit.
`CURL_BODY_RECENT = 3`.

---

## 5. THE THRESHOLD, AND THE NULL IT IS ANCHORED ON

`E_N` is a ratio of two means of the same quantity, so its null is *not* 1.0 —
the median of a 3-sample mean over an `N`-sample mean of a right-skewed variable
sits below 1. Simulate it rather than guess. 400,000 draws per cell, bodies
i.i.d.:

| body distribution | N | median | p40 | p30 | p20 | P(E < 0.70) |
|---|---|---|---|---|---|---|
| half-normal `abs(N(0,1))` (normal returns) | 15 | 0.973 | 0.873 | 0.771 | 0.656 | 0.236 |
| lognormal(0, 0.8) (fat-tailed) | 15 | 0.926 | 0.824 | 0.726 | 0.624 | 0.273 |
| half-normal | 25 | 0.965 | 0.861 | 0.756 | 0.641 | 0.249 |
| lognormal(0, 0.8) | 25 | 0.906 | 0.803 | 0.705 | 0.604 | 0.295 |

Observed, on real `10Min` RTH bars at N=15: median **0.877**, p20 0.547,
p30 0.657, p40 0.765, p60 1.008, p90 1.784. Observed `P(E < 0.70) = 0.34`.

Two things fall out, and both matter.

1. **The real median (0.877) is below the i.i.d. null (0.926–0.973).** Real
   bodies are more heavy-tailed than lognormal(0, 0.8) — worth one line on its
   own, because any threshold reasoned from a Gaussian intuition will fire too
   often.
2. **The real tape sits below 0.70 on 34% of bars against an i.i.d. expectation
   of 24–27%.** That 7-to-10 point excess *is* the volatility clustering the
   guard exists to catch. It is the honest size of the effect being harvested.

**Recommendation: `CURL_BODY_QUIET = 0.70`.**

| constant | value | basis |
|---|---|---|
| `CURL_BODY_QUIET` | 0.70 | **Anchored, then judged.** 0.70 is ~p22 of the simulated i.i.d. null at N=15 (half-normal p20 = 0.656, p30 = 0.771). On a tape with no clustering at all the guard would fire about a quarter of the time by chance; on the real tape it fires on 34% of bars and on 30% of curl firings after the §6 carve-out. Judgement enters in choosing the null's 20th percentile rather than its 30th. |

The argument for not going higher is that the hit-rate table in §2 is
**monotone** in the threshold — precision always improves as you fire less, all
the way to firing on nothing. A guard optimised on precision alone converges on
a guard that suppresses everything. Two brakes:

- The guard's remit is to suppress an **artefact**, not to be a second momentum
  measure. `design/02 §2` already owns magnitude (`momentum`, `low_momentum`,
  the `dead` predicate), and it is explicit that duplicating one measurement
  into two GPA line items is the failure mode of the retired 3-vote engine. A
  guard that fires on more than half the tape has stopped guarding and started
  grading.
- The empirical median is 0.877. A threshold at or above it calls the typical
  bar quiet, which is a contradiction in terms.

0.70 fires on about a third of bars and keeps 70% of curl firings: a guard.
1.00 fires on 59% and keeps 48%: a momentum measure wearing a guard's name.

### The hygiene floor is a separate job, and `design/02` conflated the two

The old guard had a second function the expansion ratio does not perform:
stopping `bars_to_cross = -gap/slope` from returning `0.3 bars` when `gap` and
`slope` are both float noise. `design/02 §1` describes exactly this — *"a ratio
of two numbers that are both noise… reports a screaming curl on every second bar
of the dullest chart on the board"* — and then uses one constant for both jobs.
Different jobs, different numbers.

Measured size of the arithmetic job: bars where `|gap| < 0.02·atr_i` **and**
`|slope| < 0.01·atr_i` are **0.30%** of all bars, and the naive detector fires
on **80.4%** of them. Small, real, and 100% false positives.

So keep a floor, in the new units, set purely for hygiene:
`CURL_BODY_TRAVEL = 0.10`, i.e. `gap_travel >= 0.10 * B_N`. It kills **1.7%** of
firings on `10Min` and 2.3% on `6Min`. For scale, the exact translation of
`CURL_NOISE_ATR = 0.10` into body units is 0.218, which would kill 8.2% of
firings and — per §2 — buy nothing. Half the equivalent, doing only the job that
needs doing.

---

## 6. THE FAILURE MODE: A COIL IS NOT NOISE

This is the way the design goes wrong. It is quantified, and the obvious fix
needs one non-obvious piece to work.

`design/02 §3` is emphatic: *"a coil is low displacement by COMPRESSION — the
range is narrowing, the tape has not failed at anything, and the break is in
front of you"*, and *"the only number here that separates them is the count of
discrete direction changes."* A coil's bodies are small **by definition**. A
body-size guard will call the best setup on the sheet noise.

Measured, `10Min`, 38,001 RTH bars classified by `design/02 §3` as written
(`ER_TREND` 0.60, `CHOP_CROSSES` 3, `ER_LOW` 0.35, `COIL_COMPRESSION` 0.70,
`CHOP_LOOKBACK` 12):

| §3 state | share | median `E_15` | share killed at 0.70 | 6-bar directional hit | median 12-bar break |
|---|---|---|---|---|---|
| `trend` | 12.6% | 0.981 | 35.0% | 8.77% | 0.569 |
| **`coil`** | 13.6% | **0.769** | **49.7%** | **10.46%** | **0.822** |
| `chop` | 14.2% | 0.964 | 33.6% | 10.28% | 0.605 |
| `mixed` | 59.5% | 0.872 | 40.4% | 8.79% | 0.529 |

The failure mode is confirmed, in the worst possible shape. Coils have the
**lowest** median expansion of the four states, so the guard kills **half** of
them — a higher kill rate than it applies to `chop`, the state it is supposed to
be catching. And coils are the most valuable state in the table: the highest
6-bar directional hit rate, and a 12-bar break 36% larger than `trend`'s.

### The carve-out, and the measurement that decides its shape

Restrict to curl firings and split the bars the guard would kill:

| bucket, `10Min` | n | 18-bar directional hit |
|---|---|---|
| passed (`E_15 >= 0.70`) | 5,079 | 17.13% |
| killed, **not** a §3 coil | 2,451 | **8.45%** |
| killed, **is** a §3 coil | 729 | **26.47%** |

Welch t between the two killed buckets = **+10.56**. The bars the guard kills
because they are coiled hit at three times the rate of the bars it kills because
the tape is dead, and half again the rate of the bars it passes. Without a
carve-out this guard throws away the best population it can see.

**Recommendation.** `flat` requires a quiet tape **and** evidence the tape has
already failed:

```
quiet = (E_N < CURL_BODY_QUIET)
if quiet and not coiled:   state = 'flat'
elif quiet and coiled:     state = 'coiled'     # a third state, not a debit
```

Priced at 0.70 on `10Min`:

| | keeps of firings | hit(kept) | hit(killed) |
|---|---|---|---|
| no carve-out | 61.5% | 17.13% | 12.49% |
| **with carve-out** | **70.2%** | **18.30%** | **8.37%** |

The carve-out keeps *more* firings **and** sharpens the filter on both sides. It
is not a concession to protect coils; it is a strict improvement. On `6Min` with
constant-minute windows: 62.4% / 18.80% / 12.09% with, against 57.6% / 17.50% /
14.62% without.

### The non-obvious piece: the carve-out inverts if the windows are bar counts

Run the identical carve-out on `6Min` using `design/02 §3`'s **bar counts**
unchanged (`CHOP_LOOKBACK` 12 bars = 72 min, compression 6/24 bars):

| bucket, `6Min`, §3 as written | n | 18-bar directional hit |
|---|---|---|
| passed | 4,873 | 13.87% |
| killed, not a coil | 2,536 | **10.25%** |
| killed, is a §3 coil | 1,046 | **6.02%** |

Welch t = **−2.89**. The sign flips. On the screener's feed, `design/02 §3`'s
coil predicate as literally specified identifies the *worst* bucket, and a
carve-out built on it would protect exactly the wrong bars.

Now hold the wall clock constant instead — `CHOP_LOOKBACK` 20 bars = 120 min,
compression 10/40 bars, horizon 30 bars = 180 min:

| bucket, `6Min`, constant-minute windows | n | 30-bar directional hit |
|---|---|---|
| passed | 4,852 | 17.56% |
| killed, not a coil | 3,123 | 12.26% |
| killed, is a §3 coil | 412 | **34.22%** |

Welch t = **+5.08**, and the table now reads like `10Min`'s. Two feeds, one
behaviour, as soon as the windows mean the same number of minutes.

**Consequence, stated as a dependency rather than an edit:** the carve-out is
only sound if the coil predicate it calls is computed on constant-minute
windows. `design/02 §3`'s `CHOP_LOOKBACK = 12` and its 6/24 compression split
are bar counts. This draft does not edit them; it records that they carry the
same latent defect, with the sign flip above as the evidence, and flags it as a
blocker for the carve-out on the `6Min` path.

### Where the carve-out lives, and why it is not a circular call

`curl_512` must not call `chop()`; §3's `chop()` reads the same EMAs, and a
mutual import is the sort of thing that later becomes a partially-initialised
module. The coil predicate needs `close`, `e5`, `e12` and `atr_i` — all of which
`curl_512` already has in hand. So: a **private helper in the same module**,
`_coiled(df, e5, e12, atr_i, bar_minutes)`, called by both `curl_512` and
`chop()`, with the thresholds living once. That is `design/00 §2.7` applied
inside a single file: *"two tables that mean the same thing will disagree by
Tuesday."*

### The state must be direction-agnostic, and here is why

| bucket, `10Min` | n | P(move ≥ 1 rw **with** the curl) | P(≥ 1 rw **against**) | ratio |
|---|---|---|---|---|
| passed, `E_15 >= 0.70` | 5,108 | 17.13% | 17.03% | **1.01** |
| `flat` | 2,474 | 8.37% | 9.05% | 0.92 |
| `coiled` | 729 | 26.47% | 24.42% | 1.08 |

**The curl's direction carries no information in these bars.** 1.01. What the
guard buys is knowledge of whether the next three hours contain a move at all —
26% against 8%, a factor of 3.2 — and nothing whatever about which way.

Two consequences, the second uncomfortable:

1. `state = 'coiled'` must carry `side = None`. A coil that breaks is
   direction-agnostic, and the worked example in §7.2 is a real coil that broke
   **against** the curl it contained. Passing a directional `side` through the
   carve-out would hand the GPA a fabricated direction, the exact mistake
   `design/02 §2` refuses when it sets `direction = None` in the `none` band:
   *"A direction on a tape that has not moved is a fabricated number, and the
   GPA would happily grade alignment against it."*
2. The symmetry applies to the **passed** bars too, not just the coiled ones.
   See §12.1.

---

## 7. WORKED EXAMPLE, REAL BARS, FULL ARITHMETIC

Three real `SPY` `10Min` RTH bars. `CURL_BODY_MINUTES = 150` → N = 15;
`CURL_BODY_RECENT = 3`; `CURL_BODY_QUIET = 0.70`; `CURL_LOOKBACK = 3` and
`CURL_LEAD_BARS = 5` inherited from `design/02 §1`.

### 7.1 The guard fires — bar closing 2026-08-21 13:00 ET

The 15 bodies `|close − open|`, oldest first:

```
0.270 0.665 0.775 0.030 0.250 0.355 1.098 0.040 0.179 0.850 0.320 0.505 0.040 0.380 0.230
```

```
B_15   = 5.987 / 15                    = 0.3992
body3  = (0.040 + 0.380 + 0.230) / 3   = 0.2167
E_15   = 0.2167 / 0.3992               = 0.543      < 0.70   -> quiet
```

The curl itself, unchanged from `design/02 §1`:

```
e5 = 766.375   e12 = 766.179
gap        = +0.1957
slope      = (gap[-1] - gap[-4]) / 3   = -0.05609   per bar
gap_travel = 0.1683
sign(slope) != sign(gap)               -> convergent
bars_to_cross = -(+0.1957) / (-0.05609) = 3.49 bars  <= 5   -> curl_down
```

`design/02`'s guard on the same bar:

```
atr_i = 0.8073
gap_travel / atr_i = 0.1683 / 0.8073 = 0.208   >= CURL_NOISE_ATR (0.10)   -> PASSES
```

Hygiene floor: `gap_travel / B_15 = 0.1683 / 0.3992 = 0.422 >= 0.10` — passes,
so the arithmetic is not degenerate. This bar is a genuine convergence on a dead
tape, which is the hard case.

Coil test: `crosses/12 = 0`, `er = 0.135`, `compression = 0.891`. Compression is
above 0.70, so **not** a coil — the range is not narrowing, it is merely small.
The carve-out does not apply.

```
design/02 §1 as written :  state = 'curl_down', side = 'short', bars_to_cross = 3.49
this draft              :  state = 'flat',      side = None
```

What happened next. Close 766.39; over the following 18 bars the closes ran
765.23 to 766.67. Maximum favourable excursion in the curl's direction: 1.16, or
`1.16 / (0.8073 × √18) = 0.34` random-walk displacements. Three hours, a third
of a drift. Nothing happened. **The ATR guard passed it and the body guard
caught it** — the whole claim of this draft, evaluated once on one real bar.

### 7.2 The carve-out fires — bar closing 2026-08-13 14:10 ET

```
bodies: 0.040 0.405 0.485 0.115 0.670 0.235 0.324 0.090 0.540 0.150 0.295 0.310 0.043 0.095 0.080
B_15   = 0.2585      body3 = (0.043 + 0.095 + 0.080)/3 = 0.0728
E_15   = 0.0728 / 0.2585 = 0.282     < 0.70   -> quiet, and very quiet
gap = +0.0714   slope = -0.03722   gap_travel = 0.1117
bars_to_cross = -(+0.0714)/(-0.03722) = 1.92 bars
gap_travel / B_15 = 0.432            >= 0.10  -> hygiene floor passes
atr_i = 0.5856 ;  gap_travel/atr_i = 0.191    >= 0.10  -> design/02 passes this too
crosses/12 = 1   er = 0.294   compression = 0.511       -> COIL
```

```
state = 'coiled', side = None, quiet = True, coiled = True
```

What happened next. Close 776.83; over 18 bars the closes ran 776.95 to 778.52 —
it **broke upward by 1.69**, which is `1.69 / (0.5856 × √18) = 0.68` random-walk
displacements, while the curl inside it pointed **down**. Favourable excursion in
the curl's direction: 0.05 rw.

This example is kept rather than swapped for a flattering one, because it makes
§6's last argument better than an agreeing example would: the coil resolved, the
carve-out was right to refuse to call it noise, and the curl's direction was
worthless. `side = None` is not squeamishness. It is what this bar says.

### 7.3 The guard passes — bar closing 2026-08-14 09:40 ET

```
bodies: 0.043 0.095 0.080 0.130 0.050 0.410 0.340 0.010 0.605 0.210 0.115 0.075 0.190 0.050 0.520
B_15  = 0.1949    body3 = (0.190 + 0.050 + 0.520)/3 = 0.2533
E_15  = 0.2533 / 0.1949 = 1.300     >= 0.70   -> not quiet
gap = +0.1943   slope = -0.04482   bars_to_cross = 4.34   -> curl_down
state = 'curl_down', side = 'short'
```

Close 778.02; over 18 bars, closes 775.89 to 778.52. Favourable excursion 2.13 =
`2.13 / (0.5529 × √18) = 0.91` rw, in the curl's direction. Not a home run —
0.91 is just under the random-walk null — and quoting it as one would be the kind
of cherry-pick the 8,311-firing tables in §2 exist to make unnecessary.

---

## 8. ANCHORING IT TO HIS OWN BOOK

The best validation available: does the guard flag the bars he flagged by hand?
386 graded `ripster` fills; for each, take the last **closed** `10Min` RTH bar at
or before `filled_at`, dropping fills not within 3 bar-widths of a live bar
(pre/post-market and halts). **346 of 386 matched.** Call it a hit when he wrote
`Low Momentum Environment`, `Choppy Environment` or `No Environment` in
`tags_bad`.

He flags a dead environment on **62.4%** of his own graded fills, 71.8% of buys.
That base rate is worth sitting with: he criticises the environment on nearly two
thirds of the trades he took.

| guard | fires | agreement | precision | recall | φ | mean grade flagged | mean grade not |
|---|---|---|---|---|---|---|---|
| `design/02` ATR, `A < 0.10` | 15.6% | 41.6% | 0.630 | 0.157 | **+0.005** | 1.41 | 1.32 |
| `E_15 < 0.60` | 20.5% | 48.3% | 0.761 | 0.250 | +0.143 | **1.06** | 1.40 |
| `E_15 < 0.70` | 28.0% | 50.6% | 0.732 | 0.329 | +0.139 | **1.11** | 1.41 |
| `E_15 < 0.80` | 32.4% | 53.2% | 0.741 | 0.384 | +0.167 | 1.11 | 1.44 |
| `E_15 < 0.70` **and not coiled** | 25.1% | 49.4% | 0.736 | 0.296 | +0.133 | 1.13 | 1.40 |
| `E_15 < 1.488` (rate-matched to his 62.4%) | 62.4% | **66.5%** | 0.731 | 0.731 | **+0.285** | 1.31 | 1.37 |

Separation of `E_15` against his flags, treated as a ranking problem:
**AUC 0.655**, Welch t = 4.35, median `E_15` 0.999 on bars he criticised against
1.586 on bars he did not. By subgroup: **0.681** on entries only (n = 177),
**0.771** on stock fills (n = 90), **0.600** on option fills (n = 256).

And the grade split — the number that matters most for a GPA whose charter is to
model his judgement: fills where `E_15 < 0.70` average grade **1.113** (n = 97)
against **1.414** (n = 249), Welch t = **−2.62**. Three tenths of a grade point,
on a 0–4 scale whose book average is 1.36, from one number computed off open and
close.

### Reported honestly, including the parts that argue against the design

- **The incumbent's φ is +0.005.** The ATR guard is statistically independent of
  the owner's own judgement about whether the environment was alive. Not weakly
  correlated — uncorrelated. A stronger indictment than §1's inverted lift
  table, because §1 measures against the tape and this measures against *him*.
- **Agreement at the operating threshold is only 50.6%.** A 28%-firing detector
  cannot recall a 62%-base-rate label; recall is 0.329 by arithmetic.
  Rate-matching it to his base rate gets 66.5% agreement and φ = +0.285 — a real
  but modest association. **A body-size guard at any threshold does not
  reproduce his hand labels.** It is not a substitute for his eye, and anyone
  reading the GPA should be told that.
- **`E_15` has no relationship to his `5/12 Curl` tag.** Among the 64 fills he
  praised for a curl, median `E_15` is 1.219 against 1.158 for the rest; AUC
  0.530. Whatever he means by "5/12 curl", it is not body expansion. The guard
  is an **environment** measure that happens to gate a curl reading — the same
  conclusion §6's direction symmetry reached from the other side.
- **A selection effect inflates the comparison and must be named.** Median
  `E_15` at his fills is **1.173**; median across the tape is **0.877**. He
  enters on expanding candles. So the guard and his entry timing partly measure
  the same instinct, and the 0.655 AUC is against a population he already
  filtered. His uncriticised fills sit at median 1.586, which is his instinct
  working well, not the guard's.
- **A tag is a trade-level judgement; `E_15` is a bar-level reading.** "Choppy
  Environment" may describe the session he traded rather than the ten minutes
  before his fill. This test is therefore biased *against* any bar-local
  measure, and 0.655 should be read as a floor.
- 40 fills could not be matched to a closed RTH bar. They are not silently
  dropped from the denominator above; the 346 is stated everywhere.

---

## 9. THE SPEC: CONSTANTS, SESSION HANDLING, SIGNATURE, DEGRADATION

### 9.1 Constants — replacing `CURL_NOISE_ATR` in `environment.py`

Per `design/00 §2.7` and `design/02`'s one architectural rule: these live here,
`grading.py` never sees a number, and `grading.py` reads `state`, `quiet`,
`coiled` and `side` only.

```python
# --- 5/12 curl noise guard (§1) --- replaces CURL_NOISE_ATR
CURL_BODY_MINUTES = 150    # rolling body window as a DURATION, not a bar count.
                           # Argmax of the separation test on 3Min (102 min),
                           # 6Min (144) and 10Min (150); the plateau is
                           # 100-190 min and 150 is inside it on all three.
                           # 10Min -> 15 bars.  6Min -> 25.  3Min -> 50.
CURL_BODY_RECENT = 3       # bars in the numerator. Ripster's "3 candles", and
                           # the measured argmax is 2-3 (0.582 vs 0.581).
                           # A BAR count on purpose: "is it working right now".
CURL_BODY_QUIET = 0.70     # body expansion below this = the tape is not working.
                           # ~p22 of the simulated i.i.d. null (400k draws;
                           # half-normal p20 = 0.656, p30 = 0.771). Fires on 34%
                           # of real bars vs 24-27% under the null; that excess
                           # is the volatility clustering being harvested.
CURL_BODY_TRAVEL = 0.10    # HYGIENE ONLY, not the noise verdict: gap_travel must
                           # exceed this many mean bodies or -gap/slope is a
                           # ratio of two float noises. Kills 1.7% of firings.
                           # design/02's 0.10 ATR == 0.218 mean bodies; this is
                           # deliberately less than half of that.
CURL_BODY_MIN_BARS = 8     # floor on the derived bar count. Below 8 bars the
                           # denominator is 8 numbers and E is noise; see §12.3.
CURL_BODY_RTH_ONLY = True  # the body window is measured over RTH bars only.
                           # See §9.2 -- the largest implementation trap here.
```

Deleted: `CURL_NOISE_ATR`. Unchanged: `CURL_LOOKBACK = 3`, `CURL_LEAD_BARS = 5`.
`atr_i` stays in the return for the UI and for `screen_history`, but no longer
gates anything in §1.

### 9.2 The session trap, which will otherwise break this every morning

`SPY` `10Min` bars as the feed delivers them: **96 bars per calendar day, 39 of
them RTH.** 59% of the stream is extended hours. And extended-hours bodies are
not merely smaller, they are smaller by a factor that varies with the name:

| symbol | median body, RTH | median body, extended | ratio | extended share of stream |
|---|---|---|---|---|
| `SPY` | 0.3050 | 0.1450 | 0.48 | 0.59 |
| `NVDA` | 0.2750 | 0.1202 | 0.44 | 0.59 |
| `HOOD` | 0.2571 | 0.1100 | 0.43 | 0.59 |
| `KO` | 0.0650 | 0.0200 | **0.31** | 0.52 |
| `XLU` | 0.0300 | 0.0081 | **0.27** | 0.44 |

Nothing in the live path filters this. `screener_service.sweep` calls
`get_bars_multi(tickers, INTRA_TF, limit=INTRA_BARS)` and passes the result
through `drop_forming`, which removes the forming bar and nothing else. So on a
raw stream, a 15-bar body window evaluated at 09:40 ET is **fourteen pre-market
bars and one live one**: `B_N` is 30–70% too small, `E_N` reads enormous, and the
guard passes everything for the first two hours of every session — the two hours
that matter most. On `KO` and `XLU` the error is a factor of three.

**Therefore the body window filters to RTH inside the module.** A small extension
of `design/02`'s conventions, which say the module *"looks at the clock only for
the volume proration and the 'has today's session started' test"*. It needs the
owner's nod, and it is cheap:

```python
t = df['time'].dt.time
rth = (t >= dtime(9, 30)) & (t < dtime(16, 0))
w = df.loc[rth, 'body'].tail(body_bars)     # the body window only
```

Scope is deliberately narrow: **only the body window.** The EMAs, `gap`, `slope`
and `atr_i` keep running on the continuous stream the engine and
`signal_engine.trend_context` see today, because changing what the 5/12 pair is
computed over would change `engine_ripster`'s signals — a trading change dressed
as a measurement change. `backtest_sweep.RTH_START` / `RTH_END` already hold
09:30 and 16:00; borrow them rather than restate them (`design/00 §2.7`:
constants are borrowed, never copied).

Bar budget check, so this is not a prerequisite edit in disguise. `6Min`:
`INTRA_BARS = 260` raw bars × (65 RTH / 160 total) ≈ **106 RTH bars** available,
against 25 needed for the body window and `design/02 §1`'s floor of 40 closed
bars for the curl. `10Min`: `BarCache` holds ~300 raw × (39/96) ≈ **122 RTH
bars**, against 15 + 40. Both fit with better than 1.5× margin, so **no change to
`INTRA_BARS` or the cache limit is required.** Worth noting in passing that
`screener_service.py:86`'s comment — `INTRA_BARS = 260  # 6-min bars: ~4.3
sessions` — is wrong on a stream that includes extended hours: 260 bars at ~160
bars per calendar day is **1.6 calendar days**, about 1.6 sessions of RTH. It
still clears every floor above, and it is not this draft's to fix.

### 9.3 Signature and return

`design/02 §1`'s signature gains `bar_minutes` and loses nothing:

```python
def curl_512(df, atr_i=None, fast=5, slow=12, lookback=CURL_LOOKBACK,
             bar_minutes=10):
    """The 5/12 pair's turn, measured in bars-to-cross, gated on candle bodies.

    df: closed intraday bars, bars_to_df shape, continuous stream (the body
        window filters itself to RTH; the EMAs deliberately do not).
    bar_minutes: minutes per bar; the body window is CURL_BODY_MINUTES of wall
        clock, so this is what converts it. halflife.tf_minutes(tf) supplies it.
    atr_i: intraday ATR; computed from df if None. Reported, no longer a gate.
    """
```

```python
{
  'state': 'curl_up' | 'curl_down' | 'expand_up' | 'expand_down'
           | 'flat' | 'coiled' | 'none' | None,
  'side': 'long' | 'short' | None,      # None for flat, coiled and none
  'curling': bool,                      # state in (curl_up, curl_down)
  'expanding': bool,
  'quiet': bool,                        # body_expansion < CURL_BODY_QUIET
  'coiled': bool,                       # quiet AND the coil predicate holds
  'bars_to_cross': float | None,
  'body_expansion': float | None,       # E_N -- the guard's scalar, for the UI
  'body_recent': float | None,          # mean body over CURL_BODY_RECENT bars
  'body_window': float | None,          # B_N, in price units
  'body_bars': int | None,              # the DERIVED bar count, so the UI can
                                        # show 15 on 10Min and 25 on 6Min
  'gap_atr': float | None,              # design/02's fields all survive
  'slope_atr': float | None,
  'gap_travel_body': float | None,      # the hygiene ratio, gap_travel / B_N
  'reason': str,
}
```

`state = 'coiled'` is a **new state**, and per `design/00 §2.9` — *"unknown is a
bucket, not a default"* — the rubric neither credits nor debits it. Not a
half-credit, not a discounted curl: a bucket. §6's 1.08 direction ratio is the
evidence that anything finer would be invented.

### 9.4 Minimum data and failure modes

- **Minimum:** `max(design/02 §1's 40 closed bars, _body_bars(bar_minutes) + CURL_BODY_RECENT)`
  **RTH** bars. `10Min` → 40 (the body window is free). `6Min` → 40. `3Min` →
  53, so on a 3-minute feed the body window, not the EMA warmup, sets the floor.
- Fewer than that → every numeric `None`, `state = None`,
  `reason = 'need N closed RTH bars, have M'`. Never a zero.
- `B_N == 0` — every body in the window exactly zero, a halted or one-tick name
  → `state = 'flat'`, `quiet = None`,
  `reason = 'no candle bodies in window'`. Never a `ZeroDivisionError`. The one
  case where `flat` is returned without a measured `E_N`, and it is correct: a
  window of fifteen doji is not a tape.
- `slope == 0` exactly, or `gap_travel < CURL_BODY_TRAVEL * B_N` →
  `state = 'flat'`, `reason = 'degenerate 5/12 arithmetic'`. Reported separately
  from the quiet verdict, so the UI can distinguish "the tape is dead" from "the
  indicator is dividing noise by noise".
- `atr_i` `None` or 0 → `gap_atr`/`slope_atr` are `None`, `state` is **still
  computed**. A behaviour change from `design/02`, which returns
  `state = 'flat'`, `reason = 'no intraday ATR'`. The guard no longer needs ATR,
  so a missing ATR must no longer suppress a curl. The GPA becoming a function of
  the feed's health is the thing `design/02 §2` already forbids.
- Missing data never sets `quiet = True`. `quiet` is `False` or `None`, never a
  debit earned by a data gap.

---

## 10. CAN THE BACKTESTER SCORE THIS?

Asked and answered in the words `design/00 §2.2` uses, because a draft that
introduces four constants has to.

**As proposed: the question does not arise, and the answer is yes anyway.**

This is a **display**, in the brief's sense — a measurement feeding the GPA,
which grades an environment and places no orders. `design/00 §2.2` scopes the
rule to entry and exit rules; a measure is exempt because it measures rather than
decides. `design/02 §3` sets the precedent: `CHOP_LOOKBACK`, `COIL_COMPRESSION`
and `ER_LOW` are all unrefereed constants in the same module for the same reason.

But the stronger answer belongs on the record, because somebody will eventually
propose gating `engine_ripster`'s entries on this:

**Yes — `backtester.py` can replay this from bars alone.** The guard needs
`open`, `close`, `high`, `low` and the bar's timestamp. Nothing else. No premium,
no spread, no open interest, no Greek, no wall-clock latency, no indicator the
backtest does not compute. It is arithmetic on the same OHLC the replay already
feeds the engine one bar at a time. `CURL_BODY_MINUTES`, `CURL_BODY_RECENT`,
`CURL_BODY_QUIET` and `CURL_BODY_TRAVEL` are therefore all **legitimately
sweepable** by `backtest_sweep.py`, and a sweep is the correct way to settle
`CURL_BODY_QUIET`, which §5 had to reason about from a simulated null instead.

Two boundaries on that, both from `design/00 §2.2`:

1. **Shares sleeve only.** *"`backtester.py` models the shares sleeve only, so a
   ladder is refereeable on shares and not at all on options."* Same here. A
   sweep would score whether the guard improves the directional signal on the
   underlying. On the options overlay the guard is unmeasured, as everything
   options-side currently is.
2. **A sweep is not offered as part of this draft.** Proposing constants and
   proposing to tune them against P/L are different acts. `design/01 §0` is
   explicit that the GPA must not be fit to outcomes — *"a high correlation with
   the old grade would be a warning sign, not a pass"* — and a constant inside
   the GPA's measurement layer, optimised on the shares P/L of the engine the
   GPA is meant to grade independently, would make the instrument a function of
   the thing it measures. If `CURL_BODY_QUIET` is ever swept, it should be swept
   as **signal quality on the shares sleeve**, reported next to the §8 journal
   agreement, and not allowed to overwrite it.

**Not a configurable, either way.** `design/00 §2.3`: `HARDCODED CONSTANTS BY
DESIGN — edit + commit to change`. No slider, no settings row, no per-ticker
override. A per-name `CURL_BODY_MINUTES` would be one overfit per name, which
`design/00 §2.4` forbids in five separate files.

---

## 11. WHAT THIS DRAFT DELIBERATELY DOES NOT DO

- **It uses no volume, anywhere.** He ruled it and this obeys it. Worth being
  precise about the kind of limit that is: the bar dicts from `get_bars_multi`
  **do** carry `volume`, and `design/02 §5` already builds a whole
  relative-volume measure on it. So the absence here is a **ruling, not a data
  limit**, and it is scoped to this guard only — nothing here touches §5 or
  `screener._volume_ok`.
- **It does not touch the curl detector.** `CURL_LOOKBACK`, `CURL_LEAD_BARS`,
  `bars_to_cross`, and the convergent/divergent split are `design/02 §1`'s and
  are inherited unchanged. This draft replaces one guard, adds one state, and
  changes one failure mode. Re-deriving the detector while I was in here would
  have made the two changes impossible to review separately, which
  `design/00 §1` is explicitly about.
- **It does not edit `design/02 §3`**, though §6 shows its coil constants invert
  a classification between `6Min` and `10Min`. That is a finding to hand over,
  not a change to make in a draft about something else. It is recorded as a
  blocker for the carve-out on the `6Min` path and nothing more.
- **It does not change what the 5/12 EMAs are computed over.** §9.2 filters the
  body window to RTH and stops there. Recomputing the EMAs on RTH-only bars
  would change `engine_ripster`'s live signals: a trading change, needing the
  backtester, and not a measurement.
- **It does not predict.** `E_N` describes the last closed bar and the fourteen
  before it. Forward windows appear only to select a constant.
- **It does not add a range-based term**, though §12.2 measures one as better.
  He ruled on open and close. The draft records the cost and leaves the choice
  with him.
- **It does not propose a UI.** `design/04` owns that. One note for whoever
  does: `body_bars` is in the return specifically so the panel can show *15 bars
  (150 min)* rather than a bare constant, because a lookback that changes with
  the timeframe will otherwise look like a bug.
- **It does not grade.** `state`, `quiet`, `coiled`, `side`. `grading.py` assigns
  credit and contains no number from this file.

---

## 12. WHERE I THINK THE RULING WILL PRODUCE A WORSE RESULT

In the shape `design/01 §9` and `design/04 §8` use: the flag, the evidence, and
what I would do instead. The first is the one I would defend hardest, and it is
not about his body-size ruling.

### 12.1 The curl's direction is not predictive, and the GPA is about to credit it

Found only because the guard had to be scored.

`design/02 §1` calls the convergent curl *"the pre-cross curl and the valuable
one"*, gives it `side`, and argues from the journal for *"heavy credit in the
GPA"*. On 8,311 real firings across fifteen names, a curl favouring long is
followed by a 1-random-walk move **up** 17.13% of the time and a 1-random-walk
move **down** 17.03% of the time. **Ratio 1.01.** After the guard passes it. On
coiled bars, 1.08. On flat bars, 0.92.

What the guard separates is **whether a move happens** — 26.5% on coiled against
8.4% on flat, a factor of 3.2 — and not **which way**. So:

- The body guard is a legitimate, well-measured **environment** gate. Good.
- The `side` field it gates is, on these bars, a coin. The GPA is being designed
  to credit cloud alignment against a direction that does not predict.

This does not contradict `design/02 §1`'s evidence, and the distinction is the
whole point. That section's split — curl present n=32 avg 1.84 against absent
n=117 avg 1.29 — is a split on **his grades**, and it says so: *"this is evidence
about what the owner values, not evidence about P/L. It does not justify
sizing."* §8 above adds the other half: his `5/12 Curl` tag has AUC 0.530
against `E_15`, so what he calls a curl is not what this measures, and he may
well be seeing something real that neither measure captures.

**What I would do:** keep the curl, keep the credit, keep `side` for cloud
alignment — and put the 1.01 on the screen next to it, or at minimum into
`screen_history` so it can be re-measured in six months. A GPA that says "3.6,
curl up" while the underlying direction is a coin is an instrument implying a
confidence its own inputs do not have. `design/02 §2` refuses exactly this when
it sets `direction = None` in the `none` band; the same scruple applied to `side`
would be consistent.

### 12.2 Open-and-close costs about 0.07 AUC against high-and-low, and the reason is a theorem

He asked for *"candle open and close size"*. The identical construction on the
bar's **range** — `mean(high−low, last 3) / mean(high−low, last N)`, same
numerator window, same denominator window, same threshold, still no volume —
beats it on every test run:

| | `10Min` N=15 | `6Min` N=21 |
|---|---|---|
| AUC, body expansion | 0.638 | 0.630 |
| AUC, **range** expansion | **0.707** | **0.713** |
| correlation between the two | 0.83 | 0.84 |

And on the precision test at p40, `10Min`:

| statistic | keeps | hit(kept) | hit(killed) | kept : killed |
|---|---|---|---|---|
| body expansion | 0.66 | 9.26% | 5.04% | 1.84 : 1 |
| **range** expansion | 0.63 | **10.87%** | **2.71%** | **4.01 : 1** |

The two are 83% the same number, and the 17% that differs favours the range on
every measurement. There is a known reason. `|close − open|` throws away the high
and the low, and the high and the low are the most informative part of a bar for
estimating volatility: Parkinson (1980), *The Extreme Value Method for Estimating
the Variance of the Rate of Return*, Journal of Business 53(1), 61–65, shows the
high-low range estimator is roughly 2.5 to 5 times more efficient than a
close-based one, and Garman & Klass (1980) extend it to open-high-low-close. Body
expansion is a deliberately less efficient volatility nowcast than range
expansion, and the measurements above are that inefficiency showing up where it
should.

Three different kinds of claim are in play here and they should not be run
together. **The standard** is Parkinson / Garman-Klass: range beats
close-to-close for volatility estimation, and that is a result, not a preference.
**The precedent** is the Ripster material, which is third-party educational
writing rather than a specification — and it happens to support the substitution
he made: secondary write-ups of the scale-out rule describe *three consecutive
candles that open and close outside the cloud*, note that *"ideally these are
large spread candles"*, and reach for volume only *"with smaller spread
candles"*. So candle size as a volume substitute is his own strategy's own idiom
and he is on solid ground. **The third thing** is what the bars say, which is
that using the whole bar rather than its body is worth 0.07 AUC.

**What I would do:** ship the body version he ruled for, as specified above,
because 0.638 is a large improvement on the incumbent's 0.521 and the ruling is
his. But make it a **one-constant switch** rather than a hardcoded field:

```python
CURL_BODY_FIELD = 'body'   # 'body' = |close-open| (ruled 2026-09-30)
                           # 'range' = high-low, measured ~0.07 AUC better;
                           #           see design/10 §12.2. Still no volume.
```

so the comparison can be re-run in one line rather than re-argued. If he ever
wants the 0.07 it costs an edit and a commit and no new design.

### 12.3 A duration-based lookback quietly breaks on slow timeframes

`CURL_BODY_MINUTES = 150` is right on `3Min`, `6Min` and `10Min` because those
are the feeds the strategy runs on. On `1Hour` it derives 2.5 bars, clamped to
`CURL_BODY_MIN_BARS = 8`, and an 8-bar denominator against a 3-bar numerator is a
ratio of two small samples — which is the noise problem the guard exists to
solve, reintroduced one level down. On `1Day` it is nonsense.

I did not measure `1Hour` or `1Day`, and the clamp is the reason: below about 8
bars this construction should be declared inapplicable rather than clamped.

**What I would do:** `_body_bars` returns `None` below `CURL_BODY_MIN_BARS`
rather than clamping, and `curl_512` returns `quiet = None`,
`reason = 'body window needs >= 8 bars, timeframe gives 3'`. The guard then
abstains on slow timeframes instead of answering badly, which is
`design/00 §2.9` — missing is not failing. `design/02 §6` already reads clouds on
`1Hour` and `1Day`, so a caller will reach this path.

### 12.4 The threshold is a calibration debt, and so was the one it replaces

`design/02` was candid that `CURL_NOISE_ATR = 0.10` was *"a guess with a
rationale and nothing more"*, and specified the fix: log the scalar to
`screen_history.py` for a month and set the constant from the distribution.
`CURL_BODY_QUIET = 0.70` is better founded — a simulated null, a 34%-against-25%
firing rate against it, a monotone precision table, and a journal grade split at
t = −2.62 — but it is still a threshold chosen by argument. The same debt,
smaller.

The plan, unchanged in shape from `design/02`'s and strictly cheaper because this
constant is backtestable (§10): log `body_expansion` and `body_bars` to
`screen_history.py` alongside the components, and after a month set
`CURL_BODY_QUIET` from the observed distribution at the moments trades were
actually taken. Two targets are already in hand — his 36% / 33% tag frequencies
from `design/02`, and the 62.4% environment-criticism rate measured in §8. Until
that data exists the UI should say the number is provisional, exactly as
`design/02 §2` requires of the momentum band edges.

---

## 13. WHAT I COULD NOT DETERMINE

- **Out-of-sample stability.** Everything here is 2026-03-11 → 2026-09-30, one
  regime, fifteen names, and the two datasets overlap. The n is large enough
  that the AUCs are not sampling noise (t between 20 and 33 on the pooled
  separation tests), but large-n is not out-of-sample. I ran no walk-forward and
  no per-month split. If the effect is regime-dependent it would not show up in
  anything above. `backtest_sweep.py` could answer this on the shares sleeve; see
  §10.
- **Per-symbol consistency.** Everything is pooled. A pooled AUC of 0.64 is
  compatible with the effect being concentrated in three names. I did not break
  it out by symbol, and that is the first thing I would run next.
- **The two thinnest names.** `KO` and `XLU` are in the pool because they have
  3-to-7-cent bodies and would break a badly-scaled measure. `E_N` is
  dimensionless so they cannot break it arithmetically, but I did not check
  whether tick granularity — a 3-cent body is 3 ticks — makes `E_N` lumpy on
  them. On a sub-$10 name it may be materially lumpy.
- **`1Hour` and `1Day`.** Not measured. §12.3.
- **Options-side anything.** `backtester.py` models the shares sleeve only, so
  every forward-outcome number in this draft is a move in the **underlying**, not
  P/L on a call. Per `design/00 §2.2` and `backtester.py`'s own docstring, read
  them as signal quality.
- **Whether `filled_at` alignment in §8 is right to within a bar.** Fills are
  Eastern wall-clock stored naive; I took the last closed bar at or before the
  fill and discarded anything more than three bar-widths from a live bar. A fill
  1 second after a bar close and a fill 9 minutes after it get the same reading.
  Tightening that would shrink n.
- **`5/12` versus `5/13`.** Secondary write-ups of the Ripster system are not
  consistent — several describe the fast cloud as 5/13, some as 5/12, and the
  project uses 5/12 throughout (`signal_engine.EMA_FAST`, `EMA_SLOW`). I did not
  revisit it, and nothing in this draft depends on which is right, but it is a
  live discrepancy in the cited material and someone should settle it against a
  primary source rather than a blog.
- **Why agreement with his labels is much better on stock fills (AUC 0.771,
  n=90) than option fills (0.600, n=256).** Plausible stories exist — options are
  where he chases, or where the thinner names are — and I have no evidence for
  any of them. Reported because it is a 0.17 AUC gap on the validation that
  matters most, and unexplained.

---

## APPENDIX — REPRODUCING THE NUMBERS

Nothing here is a stored artifact; every table is recomputable. The bars came
from `alpaca_manager.get_bars_multi(syms, timeframe, limit=6000, start=date)`
using the key in `%LOCALAPPDATA%\FoundationsTrading\config.json`, plus the
`3Min`/`6Min`/`10Min` JSON already in
`%LOCALAPPDATA%\FoundationsTrading\tf_sweep_cache`. The recipe, in the order the
sections need it:

1. `bars_to_df` shape, ET, sorted, **RTH filtered** (09:30 ≤ t < 16:00).
2. `e5 = ema(close,5)`, `e12 = ema(close,12)`, `gap = e5-e12`,
   `slope = (gap[t]-gap[t-3])/3`, `gap_travel = |gap[t]-gap[t-3]|`,
   `atr_i =` Wilder(14) on the same bars.
3. `btc = -gap/slope` where `sign(slope) != sign(gap)`; firing = `0 <= btc <= 5`.
4. `body = |close-open|`; `E_N = mean(body,3) / mean(body,N)`.
5. Labels: `fwd = max over i in 1..H of (close[t+i]-close[t])`, signed by
   `sign(slope)` for directional tests and absolute for the separation tests, all
   divided by `atr_i[t] * sqrt(H)`. Positive class ≥ 1.0, negative < 0.4, both
   from `design/02 §2`'s bands.
6. §6's state classification uses `design/02 §3` verbatim; its second table uses
   the same predicate with `CHOP_LOOKBACK` 20 and compression 10/40 on `6Min`.
7. §5's null: 400,000 draws of N i.i.d. bodies, `mean(last 3)/mean(all N)`,
   half-normal and lognormal(0, 0.8).
8. §8: `journal.jsonl`, `grade is not None` and
   `conditions.trade_type == 'ripster'` (386 of 414), joined to the last closed
   `10Min` RTH bar at or before `filled_at`, fills more than 3 bar-widths from a
   live bar dropped (346 survive).

### Cited

- Parkinson, M. (1980). *The Extreme Value Method for Estimating the Variance of
  the Rate of Return.* Journal of Business 53(1), 61–65.
  https://ideas.repec.org/a/ucp/jnlbus/v53y1980i1p61-65.html — §12.2's theorem.
- Garman, M. & Klass, M. (1980). *On the Estimation of Security Price
  Volatilities from Historical Data.* Journal of Business 53(1), 67–78. The OHLC
  extension of the same result.
- Engle, R. (1982). *Autoregressive Conditional Heteroskedasticity with Estimates
  of the Variance of United Kingdom Inflation.* Econometrica 50(4), 987–1008.
  The clustering §2 says the guard is harvesting.
- Wilder, J.W. (1978). *New Concepts in Technical Trading Systems.* The ATR the
  incumbent guard uses, and which this one reports but no longer gates on.
- Ripster EMA cloud material, e.g. https://www.ripstereducation.com/post/ema-clouds
  and the circulated rule sheets. **Design precedent, not a definition** — these
  are third-party educational write-ups, they disagree with each other on 5/12
  versus 5/13, and §12.2 cites them only for the candle-size-versus-volume idiom,
  which is the one place they speak directly to this ruling.
