# 02 — The measurement layer: `environment.py`

Design spec. Nothing here is implemented yet. No existing `.py` file is
modified by this document; the two edits it *requires* of other modules are
listed at the end under **PREREQUISITE EDITS**, and both are producer-side.

---

## WHY THIS MODULE EXISTS

The GPA grades ENVIRONMENT. The codebase currently measures LEVELS
(`levels.py`), CLOUD GEOMETRY (`signal_engine.trend_context`) and JOURNAL
OUTCOMES (`conditions.py`). It measures no environment at all. Every one of
the owner's four most common criticisms is a statement about environment, and
not one of them is computable today:

| journal tag | n / 382 | avg grade | measured today? |
|---|---|---|---|
| `Low Momentum Environment` | 138 | 1.46 | no |
| `Choppy Environment` | 125 | 1.42 | no |
| `5/12 Not Followed` | 90 | 0.90 | partially (fresh cross only) |
| `5/12 Curl` | 78 | 1.65 | no |
| `No Environment` | 48 | **0.60** | no |
| book | 382 | 1.36 | — |

(Counted directly out of `%LOCALAPPDATA%\FoundationsTrading\journal.jsonl`,
flat schema: `tags_good` / `tags_bad` / `conditions` / `grade`. Grades are
already 0–4 integers, distribution `{0:99, 1:117, 2:105, 3:50, 4:11}` — the
GPA scale the sibling spec is building is the scale the owner has been using
by hand all along.)

Six measurements. In each case the formula is the easy part; the threshold
and the failure mode are the design.

## THE ONE ARCHITECTURAL RULE

**Thresholds live here. The rubric layer reads labels and booleans, never
raw scalars.**

`grading.py` must never contain a number like `0.35`. If it did, the same
threshold would exist in two files and drift. So every read below returns a
STRING STATE or a BOOL as its primary output, and carries the scalars it was
derived from alongside — for the UI, for `screen_history.py`, and because a
grade with no visible parts is an opinion with a letter on it.

Corollary: retuning a threshold is a one-line change in this file and
changes the GPA without touching the rubric.

## CONVENTIONS THAT APPLY TO ALL SIX

- **Input shape.** Every public function takes a `pandas.DataFrame` in the
  exact shape `signal_engine.bars_to_df()` produces: `time` tz-aware
  `America/New_York`, plus `open`/`high`/`low`/`close`/`volume`. Daily,
  hourly and intraday all arrive that way. The caller converts; this module
  contains no bar-dialect logic, because `screener._normalize_bars` already
  owns that problem and owning it twice is how the two copies disagree.
- **Closed bars only.** Every read keys off `.iloc[-1]`, which the caller has
  already guaranteed is closed (`screener_service.drop_forming`,
  `app._drop_forming`). This module never looks at the clock to decide what
  is closed. It looks at the clock only for the volume proration and the
  "has today's session started" test.
- **Pure.** No Flask, no I/O, no module-level mutable state, with exactly one
  documented exception: the `VolumeProfile` cache in §5, which is a class the
  caller instantiates and injects — the same pattern as `screener.scan`'s
  injected `bar_getter`.
- **Degradation.** Short history, a fresh session, a missing timeframe: the
  measured field is `None` and `reason` says why. No zeros, no defaults.
  `plays.py` already draws this distinction ("plays treat a missing input as
  unknown and a defaulted one as fact") and the GPA must inherit it: a `None`
  read must cost nothing and credit nothing, not silently grade as a fail.
- **ATR normalization.** Two different ATRs are in play and conflating them
  is the easiest bug available here. `atr_daily` is the daily range from
  `levels.atr(daily_bars, 14)` — the units `levels.py` and the screener board
  already speak. `atr_i` is the SAME FORMULA applied to the intraday bars,
  i.e. the average true range of one 10-minute bar. A slope per 10-min bar
  normalized by a daily range is wrong by a factor of roughly √39. §1–§3 use
  `atr_i`. §4 uses `atr_daily`. §6 uses the ATR of whatever timeframe it was
  handed.
- **`atr_daily` is passed in, not recomputed.** `score_ticker` already has it
  as `ls['atr']`. Taking it as an argument guarantees the number in the GPA
  is the same number on the board.

### Module name

`environment.py`. Not `structure.py` — `levels.py` already owns structure,
and a reader looking for price structure would open the wrong file. This
module's output *is* the environment vector the GPA grades, and the name
should say so.

### Module constants (the spec — edit + commit to change)

```python
# ── EVERY WINDOW BELOW IS MINUTES (ruled 2026-09-30). Convert with
#    bars_for(minutes, tf). The 10Min column of every conversion reproduces the
#    bar count this spec was originally reasoned against, exactly.
#    EMA periods are NOT here: they are bar counts by doctrine.

# --- 5/12 curl (§1) ---
CURL_LOOKBACK = 3          # BARS. slope of the 5/12 gap; a candle count, not a duration
CURL_LEAD_MINUTES = 50     # a convergent curl counts when the cross is <= this far out
CURL_SPREAD_MINUTES = 150  # the expansion window. Measured: argmax is ~150 min on
                           # every timeframe, where the BAR COUNT differs by 2.3x
CURL_SPREAD_RECENT = 3     # BARS. Nate's "3 candles", and the measured optimum
CURL_SPREAD_FIELD = 'range'  # 'range' (high-low) or 'body' (|close-open|). RULED
                           # 'range' 2026-09-30: same idea, AUC 0.707 vs 0.638
CURL_SPREAD_QUIET = None   # <- SET IN §1. The one constant the ruling left open:
                           # 0.70 was derived against a BODY null and does not
                           # transfer to range, which is a tighter estimator
CURL_SPREAD_TRAVEL = 0.10  # hygiene only: the degenerate -gap/slope case, 0.30% of bars
CURL_SPREAD_MIN_BARS = 8   # below this the window is None, not a guess
CURL_SPREAD_RTH_ONLY = True  # the spread window only. See §1 -- 59% of a 10Min
                             # stream is extended hours and its candles are 27-48%
                             # the size, which would pass everything until 11:30
# CURL_NOISE_ATR -- DELETED 2026-09-30. Not uncalibrated: actively harmful.
#   Negative lift at every threshold over 8,311 firings; the bars it discarded
#   hit MORE often than the ones it kept; phi = +0.005 against Nate's own hand
#   labels. See design/10 and OPEN DECISIONS #2.

# --- momentum (§2) ---
MOM_MINUTES = 60           # displacement window. 6 bars at 10Min, 10 at 6Min
MOM_STRONG = 2.0           # >= 2 random-walk displacements
MOM_MODERATE = 1.0         # the random-walk null itself
MOM_WEAK = 0.4             # below this = 'none' (Low Momentum Environment)

# --- chop vs coil (§3) ---
CHOP_MINUTES = 120         # one full slow-EMA span at 10Min
COIL_RECENT_MINUTES = 60   # compression numerator: one CHOP window
COIL_PRIOR_MINUTES = 240   # compression denominator: the two before it
CHOP_CROSSES = 3           # >= this many 5/12 side changes in the window IS chop
COIL_CROSSES = 1           # <= this many, with low ER, is a coil
ER_LOW = 0.35              # efficiency ratio at or below this = going nowhere
ER_TREND = 0.60            # at or above this = clean directional tape
COIL_COMPRESSION = 0.70    # recent range / prior range; a coil NARROWS

# --- ATR structure (§4) ---
ATR_PCT_WIDE = 0.035
ATR_PCT_OK = 0.020
ATR_PCT_THIN = 0.012
ATR_USED_ROOM = 0.60
ATR_USED_EXTENDED = 1.00
ATR_TREND_EXPANDING = 1.15
ATR_TREND_CONTRACTING = 0.85

# --- relative volume (§5) ---
RVOL_SESSIONS = 10         # prior sessions in the time-of-day baseline
RVOL_MIN_SESSIONS = 3      # below this the baseline is None, not a guess
RVOL_HOT = 1.50
RVOL_NORMAL = 1.00
RVOL_LIGHT = 0.60

# --- price vs cloud (§6) ---
AT_TOL_ATR = 0.05          # inside-the-cloud tolerance, in that timeframe's ATRs
EMA_WARMUP_SPANS = 3       # bars needed = EMA_WARMUP_SPANS * slow (+ lookback)
```

---

## §1 — THE 5/12 CURL

### What a curl is, numerically

`trend_context` reports a *fresh cross*: the close changed sides of the 5/12
cloud on this bar. That is the event. The curl is the approach to the event,
and the owner's claim — "5/12 curl is stronger in very short timeframe than a
distant magnet" — is a claim that the approach carries more information than
the arrival, because by the time the close has crossed, the move has started
without you.

So the measurement must be in units of LEAD, not in units of slope. Take the
signed gap between the pair:

```
e5  = ema(close, 5)        # the series, not the last value
e12 = ema(close, 12)
gap[t]   = e5[t] - e12[t]                      # signed; + = fast above slow
slope    = (gap[-1] - gap[-1 - CURL_LOOKBACK]) / CURL_LOOKBACK   # per bar
```

Then, when `gap` and `slope` have opposite signs (the pair is converging):

```
bars_to_cross = -gap[-1] / slope
```

That is a linear extrapolation of the current convergence to zero, in BARS.
It needs no ATR, no percentage and no per-ticker tuning, because its units
are the same units the strategy already thinks in. A curl "leads the cross"
is not a metaphor in this formulation; it is the number.

Two flavours, both of which the owner calls a curl ("rolling toward/away"):

- **Convergent curl** — `sign(slope) != sign(gap)`, the pair is closing.
  `bars_to_cross <= bars_for(CURL_LEAD_MINUTES, tf)` → state `curl_up` /
  `curl_down`, favouring
  `sign(slope)`. This is the pre-cross curl and the valuable one.
- **Divergent curl** — `sign(slope) == sign(gap)`, the pair is opening in the
  direction it already has. State `expand_up` / `expand_down`, favouring
  `sign(gap)`. This is the cloud fanning after the cross: less lead, but it is
  what "5/12 Curl" looks like on the trades where the cross already fired, and
  omitting it would under-report the tag by roughly half.

### The noise guard — REPLACED 2026-09-30

The problem it solves is unchanged and still the part that matters. In a flat
tape both EMAs sit on top of each other, `gap ≈ 0` and `slope ≈ 0`, and
`bars_to_cross = -gap/slope` is a ratio of two numbers that are both noise. It
returns `0.3 bars` on one bar and `800 bars` on the next, and a naive
implementation reports a screaming curl on every second bar of the dullest
chart on the board. (Measured: that degenerate case is 0.30% of bars and the
naive detector fires on 80% of them.)

**What changed is the answer.** The old guard tested the movement of the EMA
GAP against intraday ATR. It did not work — see OPEN DECISIONS #2: negative
lift at every threshold across 8,311 firings, and φ = +0.005 against the
owner's own hand labels. The mechanism of the failure generalises and is worth
stating: **`gap_travel` is a property of the INDICATOR, and the thing being
guarded against is a property of the TAPE.** Something derived from price cannot
tell you whether price is doing anything.

The new guard asks the tape directly, and it is the owner's own formulation
(2026-09-30) — *"candle open and close size across a moving range"* — with his
*"3 candles of increasing volume"* structure and candle spread standing in for
volume:

```
n      = bars_for(CURL_SPREAD_MINUTES, tf)     # 15 at 10Min, 25 at 6Min
window = rth_only(closed_bars)[-n:]            # SEE BELOW -- not optional
spread = high - low   for each bar             # CURL_SPREAD_FIELD
if len(window) < CURL_SPREAD_MIN_BARS:
    return None                                # unknown, never 'flat'

E = mean(spread[-CURL_SPREAD_RECENT:]) / mean(spread)

if E <= CURL_SPREAD_QUIET:
    state = 'flat'          # ... unless section 3 says coil. SEE THE CARVE-OUT.
```

`E` is **expansion**: is the tape doing more in the last three candles than it
has been doing all window. AUC 0.676 against forward movement, against the old
guard's *negative* lift.

**Say plainly what this is.** The mechanism is volatility autocorrelation —
quiet tapes stay quiet, busy tapes stay busy — not curl detection. It earns its
place by predicting **whether** a move happens, and that is all it is claimed to
do. See THE FLAG below.

**A straight substitution would have been worthless.** Testing
`gap_travel < k × mean_spread` — the same numerator with a new denominator —
measured lift +0.000 to +0.007, because `mean(body) ≈ 0.46 × atr_i` makes the
two constants near-translations of each other. The owner's ruling works because
of the RATIO, not because of the units.

### The field is the RANGE, and the threshold had to be re-derived

`CURL_SPREAD_FIELD = 'range'`, ruled 2026-09-30 (board `d-body-vs-range`).
`high - low` beats `|close - open|` at the identical construction: AUC 0.707
against 0.638, separating kept from killed bars 4.01:1 against 1.84:1. They
correlate 0.83, so it is the same idea measured better. Parkinson (1980) gives
the reason — a range-based volatility estimator is several times more efficient
than a close-based one. Still no volume in it: high and low are price.

**The trap that created, and why the constant is not 0.70.** `design/10` derived
`QUIET = 0.70` against a **body** null. Range is the tighter estimator, so its
null distribution is far narrower — simulated null standard deviation **0.171
against body's 0.392**, which is the Parkinson efficiency showing up directly.
Carrying 0.70 across would have fired the guard on **2.6% of a dead tape instead
of 23.6%.** It would have looked installed and done essentially nothing.

Re-derived at the same null percentile (200,000 draws, 48 intra-bar substeps,
driftless walk, unit bar volatility):

| N | p20 of the range null | equivalent of body's 0.70 |
|---|---|---|
| 15 (`10Min`) | 0.854 | **0.872** |
| 25 (`6Min`) | 0.846 | **0.872** |
| 50 (`3Min`) | 0.841 | **0.871** |

`CURL_SPREAD_QUIET = 0.87`. Stable to three decimals across every timeframe, so
it needs no per-feed value — the same conclusion the lookback ruling reached
from the other direction.

### The window must be regular hours only

`CURL_SPREAD_RTH_ONLY = True`, and this is not a preference. The SIP feed runs
04:00–20:00, so a `10Min` stream carries 96 bars per calendar day of which 39
are regular hours — **59% extended**. Extended-hours candles measure 27–48% of
regular-hours ones and it varies by name (XLU 0.27, KO 0.31, SPY 0.48).
`screener_service.drop_forming` removes only the in-progress bar; nothing
anywhere filters the stream to RTH.

Without this, a 15-bar window at 09:40 is fourteen pre-market bars, `E` reads
enormous, and **the guard passes everything through the first two hours of every
session while appearing to work.**

Filter the SPREAD WINDOW only. The EMAs stay on the continuous stream, because
changing what the 5/12 pair is computed over is a trading change, not a
measurement one. Bar budgets already cover it: `6Min` yields ~106 RTH bars from
`INTRA_BARS = 260`, `10Min` ~122 from the cache.

### The coil carve-out, which is not optional either

A coil is by definition the state where spread has collapsed, so this guard
kills **49.7% of coils** — a higher rate than it applies to chop. Those are the
owner's best setups: among curl firings, killed-coil bars went on to move
**26.47%** of the time against killed-non-coil **8.45%** (Welch t = +10.56).

So `'flat'` is suppressed when section 3 classifies the window as `coil`, and a
new state `'coiled'` is emitted instead, with **`side = None`**. It is a bucket,
not a debit — the compression is real information and its direction is not.
Adding the carve-out keeps MORE firings (70.2% against 61.5%) *and* sharpens
both sides (kept 18.30% against 17.13%, killed 8.37% against 12.49%). A strict
win, which is rare enough to be worth treating as suspicious; it was checked
twice.

### THE FLAG: this measure does not carry direction

After the guard passes a firing, the chance of a move **with** the curl is
17.13% and **against** it is 17.03%. **Ratio 1.01.** The guard separates whether
a move happens (26% coiled against 8% flat, 3.2×) and says nothing whatever
about which way.

`design/01`'s A1 gives `curl_512` three credits — joint-heaviest on the sheet —
and scores 4 when the curl agrees with the trade direction against 0 when it
opposes. **That 4-versus-0 is not supported by this measurement.**

It is also not refuted by it, which is why A1 stands unchanged. This measure
scores **AUC 0.530 against the owner's own `5/12 Curl` tag** — statistically
unrelated to what he means by a curl. A1's credit was earned from his graded
book (+$21.67 on n=53 against −$30.59 on n=40, the widest spread of any tag he
writes), and a detector that does not correlate with his tag cannot overturn
evidence about his tag. **Board `d-curl-direction` — raised, and he deferred it
2026-09-30.** The flag lives here, against the sibling that computes the
mechanical curl, which is the honest place for it.

### Thresholds and where they come from

| constant | value | basis |
|---|---|---|
| `CURL_LOOKBACK` | 3 bars | **Judgement, reasoned.** The 5-EMA's centre of mass sits ~2 bars back, the 12-EMA's ~5.5. At `k=1` the slope is one bar of noise. At `k=6` the cross has usually already happened, so the measure stops being a lead and becomes a lagging confirmation of the thing `trend_context` already reports. 3 is the widest window that still sits inside the fast EMA's memory. |
| `CURL_LEAD_MINUTES` | 50 min | **Judgement.** Half the slow EMA's span at `10Min`. A cross projected 20 bars out is not a curl, it is a hope. Was `CURL_LEAD_BARS = 5`; a duration now like every other window. **Unmeasured** — nothing in `design/10` tested it. |
| `CURL_SPREAD_MINUTES` | 150 min | **MEASURED.** Argmax of AUC, and the same duration on every timeframe (102 min at `3Min`, 144 at `6Min`, 150 at `10Min`) while the bar count differs by 2.3×. This is the constant that forced OPEN DECISIONS #1. |
| `CURL_SPREAD_RECENT` | 3 bars | **MEASURED, and the owner's own number.** He offered "3 candles" as a guess; at `6Min` 2 and 3 tie at AUC 0.582/0.581. Stays a candle COUNT, not a duration. |
| `CURL_SPREAD_FIELD` | `'range'` | **RULED 2026-09-30**, on AUC 0.707 against body's 0.638, plus Parkinson (1980). |
| `CURL_SPREAD_QUIET` | 0.87 | **DERIVED**, not judged — body's null percentile re-solved for range. 0.70 would have fired on 2.6% of a dead tape instead of 23.6%. |
| `CURL_SPREAD_TRAVEL` | 0.10 | **Hygiene only.** The one thing the old `CURL_NOISE_ATR` was good for: catching the degenerate `-gap/slope` case, 0.30% of bars. Kept at its old value because nothing about that case changed. |
| `CURL_SPREAD_MIN_BARS` | 8 | **Judgement.** Below this the window returns `None`. Unknown is a bucket, never a silent `'flat'`. |
| ~~`CURL_NOISE_ATR`~~ | ~~0.10~~ | **DELETED.** Not a calibration debt — a wrong measure. See OPEN DECISIONS #2. |

### Evidence that this deserves heavy credit in the GPA

Splitting the journal on the curl tag, within trades that carry NO
environment criticism (no `Low Momentum`, no `Choppy`, no `No Environment`):

- curl present: **n=32, avg grade 1.84**
- curl absent: **n=117, avg grade 1.29**

And the curl holds up even where the environment was criticised (n=41, avg
1.61 vs a book average of 1.36). That is the largest clean split in the tag
set and it is consistent with the owner's stated conviction. Caveat stated
plainly: these are the owner's own grades, so this is evidence about what the
owner values, not evidence about P/L. It justifies high credit in a GPA that
is explicitly a model of the owner's judgement. It does not justify sizing.

### Signature and return

```python
def curl_512(df, atr_i=None, fast=5, slow=12, lookback=CURL_LOOKBACK):
    """The 5/12 pair's turn, measured in bars-to-cross.

    df: closed intraday bars, bars_to_df shape.
    atr_i: intraday ATR; computed from df if None.
    Returns the dict below. Every numeric field is None on short history.
    """
```

```python
{
  'state': 'curl_up' | 'curl_down' | 'expand_up' | 'expand_down'
           | 'flat' | 'none' | None,
  'side': 'long' | 'short' | None,     # which direction the curl favours
  'curling': bool,                     # state in (curl_up, curl_down) -- the GPA's flag
  'expanding': bool,                   # state in (expand_up, expand_down)
  'bars_to_cross': float | None,       # convergent only; None when diverging
  'gap_atr': float | None,             # gap[-1] / atr_i, signed
  'slope_atr': float | None,           # slope / atr_i, signed, per bar
  'gap_travel_atr': float | None,      # the noise-guard numerator
  'reason': str,
}
```

- **Minimum data:** `EMA_WARMUP_SPANS * slow + lookback + 1` = **40 closed
  intraday bars**, and 15 for `atr_i`. So 40. This follows the rule
  `signal_engine.step3_macro` already uses (`EMA_REGIME_B * 3`); it is not a
  new convention. The screener's existing 51-bar floor in `_trend_context`
  makes 40 non-binding on that path.
- **Failure mode:** fewer than 40 bars → all numerics `None`, `state=None`,
  `reason='need 40 closed bars, have N'`. `atr_i` is `None` or zero →
  `state='flat'`, `reason='no intraday ATR'` — never a `ZeroDivisionError`,
  never an `inf` `bars_to_cross`. `slope == 0` exactly → `state='flat'`.

---

## §2 — MOMENTUM, AND ITS ABSENCE

### The choice, and why the other three lose

**Recommendation: ATR-normalized directional travel over N bars.** One
scalar, reported as a band.

- **EMA separation / expansion** loses because §1 already measures it. `gap`
  and `slope` ARE EMA separation and its expansion. Using them again as
  "momentum" would double-count the same arithmetic into two GPA line items,
  which is exactly the failure mode `signal_engine`'s docstring describes in
  the old 3-vote engine — several votes that were really one vote.
- **Consecutive-bar directional agreement** loses because it is blind to
  magnitude and nearly blind in resolution. Six consecutive doji up-bars read
  as maximum momentum; it has only N+1 possible values; and it is dominated by
  the tick that happened to close each bar.
- **Efficiency ratio** loses *as a momentum measure* and wins in §3, and the
  reason is worth stating because it is the whole argument for keeping the two
  measurements separate: ER is a QUALITY ratio, not a MAGNITUDE. A tape
  drifting 0.2 ATR in a perfectly straight line has ER ≈ 1.0, identical to a
  tape driving 4 ATR in a straight line. ER cannot tell you whether anything
  happened, only whether what happened was tidy. Momentum is a question about
  size. Chop is a question about tidiness. One measure cannot answer both.

### Formula

```
n      = bars_for(MOM_MINUTES, tf)          # 6 at 10Min, 10 at 6Min, 20 at 3Min
travel = close[-1] - close[-1 - n]
mom    = travel / (atr_i * sqrt(n))
```

The `sqrt(N)` is the only piece of this spec that is not judgement. A random
walk's expected absolute displacement over N bars of typical range `atr_i` is
`atr_i * sqrt(N)`. Dividing by it makes `|mom| ≈ 1.0` **the null** — the
distance a directionless tape covers by accident — and makes the thresholds
independent of N. Without it, every band edge would have to be retuned the
moment anyone changed the window — and someone did: N became a function of bar
width on 2026-09-30 and not one band moved.

### Bands

| band | condition | basis |
|---|---|---|
| `strong` | `|mom| >= 2.0` | Two random-walk displacements. Measured anchor. |
| `moderate` | `1.0 <= |mom| < 2.0` | The null itself. Measured anchor: the name has gone further than drift. |
| `weak` | `0.4 <= |mom| < 1.0` | **Judgement.** Below the null: less ground covered than chance. |
| `none` | `|mom| < 0.4` | **Judgement.** Under half the null — visibly going nowhere. |

`MOM_MINUTES = 60` is **judgement**: an hour, because the owner's holds run
tens of minutes to a couple of hours and the window should span about one hold.

**It is a duration, not a bar count** (ruled 2026-09-30 — see *Lookbacks are
durations* under OPEN DECISIONS). It was `MOM_BARS = 6`, which meant an hour on
the engine's `10Min` feed and 36 minutes on the screener's `6Min` feed; that
defect is now closed, and the `10Min` behaviour is unchanged because
`bars_for(60, '10Min') == 6`.

**The `sqrt(N)` is what makes this conversion free.** Because the bands are
expressed against the random-walk null, they do not move when N does — which
was the stated reason for the normalisation before anyone knew the lookback
would have to become a duration. It is the only constant in this spec that
needed no re-derivation to survive the ruling.

### Calibration plan for the band edges

`Low Momentum Environment` appears on **138 of 382 trades = 36%**. That is a
calibration target the owner has already supplied. Log `mom` to
`screen_history.py` alongside the existing components, and after a month set
`MOM_WEAK` so that `low_momentum` fires on roughly a third of readings taken
at the moments trades were actually entered. Until that data exists, 0.4 is a
guess and should be labelled as one in the UI.

### What the GPA receives

**The band, plus one boolean.** Not the scalar.

```python
def momentum(df, atr_i=None, tf='6Min', bars=None):
    # bars=None -> bars_for(MOM_MINUTES, tf). Passing bars overrides, for tests.
    """ATR-normalized directional travel. Magnitude, not tidiness."""
```

```python
{
  'band': 'strong' | 'moderate' | 'weak' | 'none' | None,
  'mom': float | None,                 # signed, in random-walk displacements
  'direction': 'up' | 'down' | None,   # sign(travel); None when band == 'none'
  'low_momentum': bool,                # band in ('weak', 'none') -- the owner's tag
  'travel_atr': float | None,          # raw travel / atr_i, for the UI
  'reason': str,
}
```

`direction` is deliberately `None` in the `none` band. A direction on a tape
that has not moved is a fabricated number, and the GPA would happily grade
alignment against it.

- **Minimum data:** `bars_for(MOM_MINUTES, tf) + 1` closes, plus 15 for
  `atr_i` → **16 closed bars at `10Min`, 26 at `6Min`.** The requirement rises
  on a faster feed because the window is a fixed duration; that is the point,
  and `screener_service.INTRA_BARS = 260` covers it comfortably.
- **Failure mode:** short history or `atr_i` in `(None, 0)` → everything
  `None` except `low_momentum=False` and a reason. `low_momentum` must be
  `False`, not `True`, on missing data: an unmeasured environment is unknown,
  and debiting the GPA for a data gap would make the grade a function of the
  feed's health.

### `No Environment` (48 trades, avg grade 0.60 — the worst tag in the book)

This is not a third momentum band. It is the conjunction of a dead tape and
nothing to trade:

```
dead = (momentum.band == 'none') and (atr_structure.quality == 'poor')
```

Exposed as `read()['dead']` so the rubric can spend its largest debit on it
without recomputing the predicate. It is the strongest signal in the tag set
(0.60 vs a book 1.36) and it is trivially computable, which makes it the
cheapest win in this whole document.

---

## §3 — CHOP, AND WHY IT IS NOT A COIL

**This is the most valuable section in the spec. Everything else here is
arithmetic; this is the distinction that makes the GPA worth building.**

### The distinction

Chop and a coil produce the SAME low-displacement reading. Both have small
net travel, both have low efficiency, both look "quiet". They are opposite
trades:

- a **coil** is low displacement by COMPRESSION — the range is narrowing, the
  tape has not failed at anything, and the break is in front of you;
- **chop** is low displacement by REVERSAL — the tape has committed to a
  direction, failed, committed to the other, and failed again. The break is
  behind you, several times.

No magnitude measure separates them, and no ratio measure separates them,
because both are low-magnitude and low-ratio. **The only number here that
separates them is the count of discrete direction changes.** That is the
sentence this section exists to deliver.

### Machinery

Three numbers over `bars_for(CHOP_MINUTES, tf)` closed bars — 12 at `10Min`,
20 at `6Min`, 40 at `3Min`:

```
# 1. cross count -- the separator
#    per-bar side of the 5/12 cloud, using EXACTLY signal_engine's geometry
#    (f_top = max(e5, e12), f_bot = min(e5, e12), side by close) so the
#    screener and the engine can never disagree about what a cross is
side[t] = 'above' if close[t] > f_top[t] else ('below' if close[t] < f_bot[t] else 'in')
crosses = count of t in the window where side[t] != side[t-1] and both are in
          ('above', 'below')        # 'in' -> 'above' is not a new commitment

# 2. efficiency ratio -- confirms low displacement
er = |close[-1] - close[-1-k]| / sum(|close[i] - close[i-1]| for the k steps)

# 3. compression -- identifies the coil
#    windows are DURATIONS: one CHOP window against the two before it
r = bars_for(COIL_RECENT_MINUTES, tf)     # 6 at 10Min, 10 at 6Min
q = bars_for(COIL_PRIOR_MINUTES, tf)      # 24 at 10Min, 40 at 6Min
compression = atr_i(last r bars) / atr_i(the q bars before those)
```

### Classification

| state | condition | reading |
|---|---|---|
| `chop` | `crosses >= 3` **and** `er <= 0.35` | repeated failed direction |
| `coil` | `crosses <= 1` **and** `er <= 0.35` **and** `compression <= 0.70` | narrowing, untested, tradeable |
| `trend` | `er >= 0.60` | clean directional tape, whatever the cross count |
| `mixed` | anything else | no opinion; the GPA credits and debits nothing |

`trend` is tested first, so a strong directional move that happens to clip
the cloud three times on the way up is not labelled chop.

### Thresholds

| constant | value | basis |
|---|---|---|
| `CHOP_MINUTES` | 120 min | **Judgement, reasoned.** Two hours — one full slow-EMA span on `10Min`, the window over which the 5/12 pair has completely refreshed its memory, and long enough that three crosses cannot come from a single indecisive bar pair. Was `CHOP_LOOKBACK = 12` bars; `bars_for(120, '10Min') == 12`, so `10Min` is unchanged. |
| `CHOP_CROSSES` | 3 | **Judgement, and the number I would defend hardest.** Not a ratio — a count of discrete failures. One cross is a commitment. Two is a commitment and a reversal. Three is the tape changing its mind twice, which is the owner's own `5/12 Not Followed` written as a measurement (90 trades, avg grade 0.90). |
| `ER_LOW` | 0.35 | **Judgement.** Net move under a third of the distance travelled: two-thirds of the effort retraced. |
| `ER_TREND` | 0.60 | **Judgement.** |
| `COIL_COMPRESSION` | 0.70 | **Judgement.** The recent window's range at 70% or less of the prior stretch's. |
| `COIL_RECENT_MINUTES` / `COIL_PRIOR_MINUTES` | 60 / 240 | One CHOP window against the two before it, expressed as durations. `10Min` reproduces the original 6/24 exactly. |

All of them are judgement. The STRUCTURE — cross count separates, ER confirms,
compression identifies the coil — is the claim; the numbers are first guesses
and the UI should say so.

### The coil is load-bearing, and its bar counts were the bug

`design/10`'s measurement makes this section more important than it looked. A
curl guard built on spread expansion **kills 49.7% of coils** — a higher rate
than it applies to chop — because a coil is by definition the state where
spread has collapsed. And the coils it kills are the best setups on the sheet:
among curl firings, killed-coil bars went on to move **26.47%** of the time over
18 bars against killed-non-coil **8.45%** (Welch t = +10.56). So §3's `coil`
label is what stops §1's guard throwing away the owner's best environment, and
the carve-out is a **strict win** — it keeps more firings (70.2% against 61.5%)
*and* sharpens both sides.

**That result inverted on `6Min` using this section's bar counts as literally
written** — killed-coil 6.02% against killed-non-coil 10.25%, t = −2.89 — and
reproduced cleanly once the windows became durations. This section's own
constants carried the defect that OPEN DECISIONS #1 now closes, and the coil
carve-out is where it would have shown up as a wrong answer rather than a
warning.

### Why not range overlap between consecutive bars

Because on intraday bars it is nearly collinear with ER, and it would add a
sixth arbitrary threshold in exchange for no information ER does not already
carry. Considered and rejected.

### Evidence that chop and low momentum are separate axes

Of the 382 graded trades: **66 carry both** `Low Momentum Environment` and
`Choppy Environment` — but **72 carry Low Momentum without Choppy** and
**59 carry Choppy without Low Momentum**. The owner already treats them as
two different criticisms roughly half the time. Collapsing them into one GPA
line would throw away 131 trades' worth of distinction.

Honest counter-evidence, stated because this codebase is scrupulous about it:
`Low Momentum`-only trades average grade 1.58 and `Choppy`-only 1.53, both
*above* the 1.36 book average, while trades carrying BOTH average 1.32. So
these tags do not predict the owner's own grade on their own — only their
conjunction does, and weakly. The tags record WHAT THE OWNER NOTICES. That is
precisely what a GPA modelling the owner's judgement should measure, and it is
not the same thing as an edge. Do not let the rubric imply otherwise.

### Signature and return

```python
def chop(df, atr_i=None, fast=5, slow=12, tf='6Min', lookback=None):
    # lookback=None -> bars_for(CHOP_MINUTES, tf). fast/slow stay BAR COUNTS:
    # they are EMA periods, and EMA periods are bar counts by doctrine.
    """Chop vs coil vs trend. The cross count is the separator."""
```

```python
{
  'state': 'chop' | 'coil' | 'trend' | 'mixed' | None,
  'choppy': bool,                 # state == 'chop' -- the GPA's debit flag
  'coiled': bool,                 # state == 'coil' -- eligible for CREDIT with a curl
  'crosses': int | None,
  'er': float | None,
  'compression': float | None,
  'reason': str,
}
```

**Recommendation to the rubric layer:** `coiled` and `curl_512.curling`
together should be worth credit, not nothing — a coil with the pair turning is
the setup the owner's `Cloud Confluence Support/Magnet` tag (80 trades) sits
on top of. `choppy` should be the single largest debit available. They must
never be the same line item.

- **Minimum data:** `EMA_WARMUP_SPANS * slow + lookback + 1` = **49 closed
  intraday bars** (and 30 for the compression split, so 49 binds). The
  screener's existing 51-bar floor covers it exactly — which is a coincidence,
  not a design, and the constant should not lean on it.
- **Failure mode:** short history → all `None`, both bools `False`. `er`
  denominator zero (a completely flat 12 bars, possible on a halted or
  untraded name) → `er = None`, `state = None`,
  `reason = 'no realized movement in window'`. That case must NOT read as a
  coil; a halted tape is not a setup.

---

## §4 — ATR STRUCTURE QUALITY

### The question

"Is this name's range big enough, and stable enough, for a move to work."
Three sub-questions, three numbers, one verdict.

```
atr_pct   = atr_daily / price                       # is there anything to trade at all
used      = today_rth_range / atr_daily             # spent, or still to come
atr_trend = levels.atr(daily, 5) / levels.atr(daily, 20)   # expanding or contracting
```

`today_rth_range = max(high) - min(low)` over intraday bars whose ET date is
today **and** whose ET time is `>= 09:30` (`levels.PREMARKET_END`).

That RTH restriction is a correctness requirement, not a preference. Alpaca's
daily bars are regular-hours, so `atr_daily` is an RTH range. Alpaca's
intraday SIP bars include 04:00–20:00. Computing `used` over all intraday bars
would compare an extended-hours range against a regular-hours ATR, and a name
with a wild pre-market would report `used > 1.0` at 09:31 — the read would fire
its most severe verdict at the exact moment it is most wrong.

### Bands

| read | band | condition | basis |
|---|---|---|---|
| `atr_pct` | `wide` | `>= 0.035` | |
| | `ok` | `0.020 – 0.035` | |
| | `thin` | `0.012 – 0.020` | |
| | `dead` | `< 0.012` | |
| `used` | `room` | `<= 0.60` | |
| | `most_spent` | `0.60 – 1.00` | |
| | `extended` | `> 1.00` | today has already traded a full average day |
| `atr_trend` | `expanding` | `>= 1.15` | |
| | `steady` | `0.85 – 1.15` | |
| | `contracting` | `<= 0.85` | |

`ATR_PCT_OK = 0.020` is **judgement, anchored**: at 2% of price, a 0.5-ATR
move is 1% of price, which on the near-dated options this book actually
trades (279 of 382 legs are options) is a move that pays. Below ~1.2% there is
nothing there — the owner's own example, a $0.30 ATR on a $400 name, is
`atr_pct = 0.00075`, sixteen times below the `dead` line.

The reason this read has to be normalized by price rather than expressed in
dollars is visible in the journal's own universe: 48 tickers with average
fill prices from **VIVS at $1.00 to MU at $930**. A dollar threshold would be
meaningless across that range, which is the same argument `levels.py` makes
for expressing distance in ATRs.

`ATR_USED_ROOM = 0.60`, `ATR_TREND_*` at ±15%: **judgement.** The ±15% band
was chosen because ATR(5) against ATR(20) wobbles by roughly 10% on a quiet
name, so a narrower band would label noise as a regime change.

### Composite verdict

```
poor  if atr_pct_band in ('dead', 'thin') or used > 1.25
good  if atr_pct_band in ('wide', 'ok') and used <= 0.60
         and atr_trend_band != 'contracting'
ok    otherwise
```

`used` is allowed past 1.00 before `poor` (1.25) because a genuine trend day
routinely exceeds its average range, and calling that "poor structure" would
debit the GPA on the best days of the month.

### Signature and return

```python
def atr_structure(daily_df, intraday_df, price=None, atr_daily=None,
                  now_et=None):
    """Is there enough range here, is it still available, is it growing."""
```

```python
{
  'quality': 'good' | 'ok' | 'poor' | None,
  'atr_daily': float | None,
  'atr_pct': float | None,
  'atr_pct_band': 'wide' | 'ok' | 'thin' | 'dead' | None,
  'used': float | None,
  'used_band': 'room' | 'most_spent' | 'extended' | None,
  'today_range': float | None,
  'atr_trend': float | None,
  'atr_trend_band': 'expanding' | 'steady' | 'contracting' | None,
  'reason': str,
}
```

- **Minimum data:** 15 daily bars for `atr_daily`; 21 for `atr_trend`;
  at least one intraday bar at or after 09:30 ET today for `used`.
- **Failure mode:** fewer than 15 dailies → everything `None`. 15–20 dailies →
  `atr_trend = None`, `atr_trend_band = None`, and the composite falls back to
  `atr_pct_band` and `used` alone (a `None` trend cannot block `good`, per the
  degradation rule). Before 09:30 ET, or on a session with no RTH bars yet →
  `used = None`, `reason = 'RTH session has not started'`. **Not `0.0`.**
  Zero is arithmetically defensible and semantically false: "today has used
  none of its range" is a claim about a day that does not exist yet, and the
  GPA would credit `room` for it.

---

## §5 — CONTINUOUS VOLUME

### First: what `signal_engine._volume_ok` is, and why it stays

Read in full. It is a PACE test, not a volume read: volume accumulated in the
09:30–10:00 ET window, compared to `OPEN_VOL_MIN_FRAC (0.20) * ADV *
(elapsed / 30 min)`, where ADV is approximated from whatever prior sessions
happen to be in the 10-min history already in hand. It gates OPTIONS only —
`launch_gate` returns `volume_ok` and the caller restricts an entry to shares
when it is false. It is dead after 10:00 (the proration saturates), it says
nothing at 14:00, and it is binary.

**It stays exactly as it is.** It answers a specific question with a
documented correction for a specific bug, its threshold is wired into the
options path, and folding a continuous RVOL into it would change execution
behaviour as a side effect of adding a measurement. The new read is ADDITIVE
and is consumed only by the GPA and the board.

### The read

Cumulative-from-open relative volume against a time-of-day baseline.

```
bucket(bar)  = minutes from 09:30 ET to bar.time, floored to bar width
today_cum[k] = sum of today's RTH bar volumes through bucket k
base_cum[k]  = MEDIAN over the prior RVOL_SESSIONS sessions of that session's
               cumulative RTH volume through bucket k
rvol_cum     = today_cum[k] / base_cum[k]
rvol_bar     = today_bar_vol[k] / median(prior sessions' bar volume at bucket k)
```

Cumulative, not per-bar, is the headline. Per-bar volume on a 6- or 10-minute
bar is extremely noisy and the existing `screener.rel_volume` (latest bar vs a
20-bar trailing mean) is measuring bar-to-bar noise against a *trailing*
window rather than against the same time of day — at 09:40 its 20-bar window
is mostly overnight bars, so it reads 5x on every single name every morning.
Cumulative-vs-time-of-day is what "relative volume" means on a chart, and it
is the shape that is actually useful.

Median, not mean, for the baseline: one earnings gap triples a mean for a
month.

`RVOL_SESSIONS = 10` is **judgement**: long enough for a median to be stable,
short enough to reflect the name's current level of interest rather than its
level three months ago. Bands `1.5 / 1.0 / 0.6` — `RVOL_HOT = 1.5` is
deliberately inherited from the screener's existing `volume` component, which
already treats 1.5 as full marks (`_clamp((rv or 0) / 1.5)`), so the GPA and
the board at least agree rather than each carrying its own guess.

### How to get the baselines given what `bar_cache.py` provides

**You cannot get them from what `bar_cache` holds. This is the one place the
measurement layer needs new I/O, and it needs to be designed rather than
discovered at runtime.**

`BarCache(timeframe, limit)` keeps a rolling window of `limit` bars per
symbol, merged by timestamp, and `clear()`s on reconnect. Do the arithmetic:

- Alpaca's SIP feed returns extended-hours bars, so a full 04:00–20:00 session
  is up to **96** `10Min` bars. `engine_ripster.history['primary'] = 300`
  therefore spans roughly **3 sessions**, not 10.
- The screener's own path is worse: `screener_service.INTRA_BARS = 260` of
  `INTRA_TF = '6Min'` is **under 3 sessions**, and as little as 1.6 on a name
  with a fully-printed pre-market.
- `_volume_ok`'s ADV is built from exactly this — "the prior days present in
  the 10-min history already in hand" — which is why its docstring calls it an
  approximation. A 10-session time-of-day baseline cannot be extracted from 3
  sessions of bars by any amount of cleverness.
- And `clear()` on reconnect means even a cache sized for 10 sessions would
  lose the baseline on the first disconnect of the day.

So: a separate, once-per-session fetch, cached. Design a `VolumeProfile`
class — **the one documented piece of state in this module**:

```python
class VolumeProfile:
    """Time-of-day cumulative-volume baselines, built once per session.

    ONE batched get_bars_multi per timeframe per session. At MULTI_CHUNK=200
    and a ~40-name watchlist that is one extra request per day, against the
    180 requests/hour the minute scan already makes. It is not on the scan's
    critical path: ensure() is called at the start of the first scan of the
    session and is a no-op for the rest of it.

    NOT thread-safe. screener_service owns one instance on its own thread.
    If the bar loop ever wants one too, give it its own rather than sharing.
    """
    def __init__(self, timeframe, sessions=RVOL_SESSIONS): ...
    def ensure(self, alpaca, tickers, today=None) -> int:
        """Build/load the profile for `today`. Returns symbols covered.
        Fetches start = today - 16 calendar days (to clear 10 trading
        sessions plus holidays). Idempotent within a session."""
    def baseline(self, ticker, bucket) -> tuple[float, int] | None:
        """(median cumulative volume, n_sessions) or None."""
```

Persist to `cm.DATA_DIR/volume_profile/<timeframe>-<YYYY-MM-DD>.json` so that
a mid-session restart does not refetch, and so the baseline is auditable
after the fact — the same reasoning `screen_history.py` gives for logging
readings at all. On a cache miss and a failed fetch, `baseline()` returns
`None` and every RVOL read that day is `None`. That is the correct outcome: a
missing baseline must cost the GPA nothing.

The measurement function itself stays pure and takes the profile as an
argument, the way `screener.scan` takes an injected `bar_getter`:

```python
def rel_volume_tod(df, profile, ticker, now_et=None):
    """Cumulative relative volume vs the same time of day on prior sessions."""
```

```python
{
  'band': 'hot' | 'normal' | 'light' | 'dead' | None,
  'rvol_cum': float | None,
  'rvol_bar': float | None,
  'bucket': int | None,               # minutes since 09:30
  'today_cum': float | None,
  'base_cum': float | None,
  'n_sessions': int | None,           # how many sessions the median rests on
  'reason': str,
}
```

- **Minimum data:** `RVOL_MIN_SESSIONS = 3` prior sessions present at that
  bucket, and at least one RTH bar today. `n_sessions` is returned so the
  rubric can discount a thin baseline the way `conditions.py` discounts a thin
  factor — a 3-session median is not a 10-session median and the GPA should be
  able to tell.
- **Failure mode:** before 09:30 ET → `None`, `reason='pre-market: no
  time-of-day baseline'` (extended-hours volume deliberately gets no read;
  baselining pre-market volume against pre-market volume is a different
  measurement and a worse-conditioned one). No profile for this symbol →
  `None`. `base_cum == 0` → `None`, never a divide.
- **Half days.** An early close at 13:00 contributes no bars to buckets after
  210, so late buckets silently rest on fewer sessions. `n_sessions` is
  per-bucket, not per-symbol, precisely so this shows up in the output instead
  of quietly halving the denominator.

---

## §6 — TRUE PRICE-VS-CLOUD ON 1H AND 1D

### The bug, exactly

Three call sites:

- `screener.py :: mtf_state(bars, fast, slow)` — returns `'up'`/`'down'`/`None`
  from `ema(fast).iloc[-1] > ema(slow).iloc[-1]`. **Price never enters the
  function.** It is the EMA pair's direction and nothing else.
- `screener.py :: score_ticker()` — `m1h = mtf_state(hourly_bars, 34, 50)`,
  `m1d = mtf_state(daily_bars, 20, 21)`, `m1d_5055 = mtf_state(daily_bars, 50, 55)`.
- `screener.py :: journal_conditions(ctx, m1h, m1d)` — maps `'up' -> 'above'`,
  `'down' -> 'below'`, and its own docstring concedes it: *"the 1H and 1D
  reads here are the EMA pair's direction rather than price against it — the
  closest the screener measures — so 'up' is taken as 'above'."*

Meanwhile `signal_engine.step3_macro(df1h, direction)` DOES compute genuine
price-vs-cloud on the 1H — `state = 'above' if px > hi else 'below' if px < lo
else 'chop'` — but it is only reached when `REQUIRE_1H`, which is `False`, and
it spells the inside state `'chop'` rather than the journal's `'at'`.

So the fix is producer-side only. **`conditions._align` already handles `'at'`
and `'inside'`** (`if v in ('at', 'inside'): return 'at'`). The consumer has
been waiting for a producer this whole time. No consumer changes are required.

### Why it matters more than "correctness"

The state the proxy cannot express is the most informative one in the journal.
Splitting the 210 graded trades that carry a `mtf_1h` reading:

| `mtf_1h` | n | avg grade | % also tagged `Choppy Environment` |
|---|---|---|---|
| `above` | 94 | 1.30 | 29% |
| `below` | 85 | 1.13 | 38% |
| **`at`** | **30** | **0.97** | **60%** |

And on `ema_34_50`, where the producer is already correct: `at` (n=40) is 50%
choppy against 26% / 35% for above / below.

`at` is where chop lives and where the owner's grades are worst. The current
proxy assigns all thirty of those 1H readings to `above` or `below` — it does
not merely lose information, it puts the worst-conditioned readings in with
the best.

### The function

```python
def cloud_state(df, fast, slow, atr=None, tol_atr=AT_TOL_ATR):
    """Genuine price-vs-cloud on any timeframe.

    Returns BOTH `state` (price's position: the journal's vocabulary) and
    `direction` (the pair's slope: what timeframe_votes wants). They answer
    different questions and two existing callers need different answers.
    """
```

```python
e_f, e_s = ema(close, fast).iloc[-1], ema(close, slow).iloc[-1]
lo, hi   = min(e_f, e_s), max(e_f, e_s)
px       = float(close.iloc[-1])          # last CLOSED bar's close, as trend_context does
tol      = tol_atr * atr if atr else 0.0
state    = 'above' if px > hi + tol else ('below' if px < lo - tol else 'at')
direction = 'up' if e_f > e_s else 'down'
```

```python
{
  'state': 'above' | 'below' | 'at' | None,   # -> journal_conditions
  'direction': 'up' | 'down' | None,          # -> timeframe_votes
  'price': float | None,
  'cloud': [lo, hi] | None,
  'width': float | None,          # hi - lo, absolute
  'width_atr': float | None,      # hi - lo, in that timeframe's ATRs
  'dist_atr': float | None,       # signed distance to the near edge, in ATRs
  'reason': str,
}
```

Both fields are returned because the two existing consumers need different
ones, and collapsing them is how this bug happened in the first place:

- `journal_conditions` must switch to `state`. That is the fix.
- `timeframe_votes` must keep using `direction`. It is a vote on the pair's
  slope, and it already abstains on `None` and `'chop'`; `state == 'at'`
  should abstain there too.

### The 1D cloud is 20/21, and that has a consequence

Two EMAs one period apart are nearly the same line. The daily 20/21 cloud is
often pennies thick, so price is essentially never strictly inside it. The
journal confirms this is not a theoretical worry: **`mtf_1d` has 3 `at`
readings out of 210, against 30 of 210 for the 34/50-based `mtf_1h`.** The
owner's hand-logged data already shows the 20/21 cloud has almost no
thickness.

`AT_TOL_ATR = 0.05` exists solely so that a cloud three cents thick does not
flip `above`/`below` on every bar. It is expressed in that timeframe's own ATR
— `levels.atr` applied to the bars that were passed in — so one constant works
for the hour and the day without a per-timeframe table. **Judgement**, and
deliberately small: one twentieth of that timeframe's range. On the 1H 34/50,
where the cloud has real thickness, it almost never binds. If it turns out to
bind often on the daily, the honest conclusion is that a 20/21 "cloud" is a
crossover line and the `at` state should be dropped for it entirely rather
than manufactured with a wider tolerance.

Also worth noting: `m1d_5055` (daily 50/55) is computed in `score_ticker` and
appears in `build_state`, but `journal_conditions` never logs it — there is no
`mtf_1d_5055` key in the journal. Producing it correctly costs nothing, so
`read()` returns it, and if the journal ever gains that field the producer is
already there.

### Minimum data, and a live bug found

`EMA_WARMUP_SPANS * slow + 1`, following the rule `step3_macro` already uses
(`EMA_REGIME_B * 3` = 150 for the 1H 34/50):

| read | bars needed | screener fetches | status |
|---|---|---|---|
| 1H 34/50 | 151 | `HOURLY_BARS = 200` | ok |
| 1D 20/21 | 64 | `DAILY_BARS = 120` | ok |
| 1D 50/55 | 166 | `DAILY_BARS = 120` | **short** |

`mtf_state` only requires `len(rows) > slow + 2`, i.e. 57 rows for the 50/55
pair, so today's `m1d_5055` passes its own guard on 120 daily bars while its
EMA(55) has had barely two spans to warm. It is a number, it is on the board,
and it is not the number it claims to be. Either raise `DAILY_BARS` to 180 or
let the 50/55 read return `None` — but do not keep reporting it off 120 bars
once the warmup rule is written down. I recommend raising `DAILY_BARS` to 180:
one batched daily request per scan either way, and `levels.py` benefits from
the deeper prior-month history too.

**Failure mode:** short history → `state=None`, `direction=None`, reason names
the shortfall (`'need 151 bars for 34/50, have 96'`). `None` ATR → `tol = 0.0`
and a strict test, which is the correct degradation: slightly knife-edged, but
never a fabricated `at`.

---

## THE ENTRY POINT

One call, mirroring `score_ticker`'s own signature so the call site is a
single line:

```python
def read(daily_df, intra_df, price=None, hourly_df=None,
         atr_daily=None, bar_minutes=10, vol_profile=None,
         ticker=None, now_et=None):
    """Every environment measurement for one ticker, in one pass.

    Pure. Fetches nothing. `vol_profile` is the only stateful input and it is
    injected; None means the volume read is None, which costs the GPA nothing.
    """
```

```python
{
  'curl':     {...},   # §1
  'momentum': {...},   # §2
  'chop':     {...},   # §3
  'atr':      {...},   # §4
  'volume':   {...},   # §5
  'cloud': {           # §6
      '1h_34_50':  {...},
      '1d_20_21':  {...},
      '1d_50_55':  {...},
  },
  'dead': bool,        # §2: the `No Environment` predicate
  'atr_i': float | None,
  'bars': int,
  'reasons': [str],    # every non-None reason, for the UI and the log
}
```

### Cost

All six are O(bars) list/pandas arithmetic over at most 300 bars. Forty
tickers a minute is microseconds of CPU. **The only new I/O in this entire
document is `VolumeProfile.ensure()`: one batched `get_bars_multi` per
timeframe per session.** Nothing else touches the API, nothing else caches,
and the once-a-minute scan's request count is unchanged.

One minor redundancy, accepted deliberately: the 5/12 EMA pair is now
computed twice per ticker per scan — once inside `signal_engine.trend_context`
and once inside `curl_512`/`chop`. `trend_context` returns only the last
values, not the series, and the series is what §1 and §3 need. Widening
`trend_context`'s return to pass EMA series around would change a contract
that `app.py` logs and `engine_contract` validates, for a saving measured in
microseconds. Not worth it. If it ever becomes worth it, the fix is for
`read()` to accept an optional precomputed series, not for `signal_engine` to
change shape.

### Minimum data, whole read

`intra_df >= 49`, `daily_df >= 15`, `hourly_df >= 151`. Below any of those the
corresponding sub-reads are `None` and the rest still work. There is no
all-or-nothing gate: a name with 20 daily bars should still get a curl read.

---

## PREREQUISITE EDITS TO EXISTING FILES

Listed, not made. Both are producer-side and neither changes a consumer.

1. **`screener.py`** — `journal_conditions()` takes the `cloud_state()` dicts
   instead of `mtf_state()`'s direction strings and writes
   `out['mtf_1h'] = st['state']`, `out['mtf_1d'] = st['state']`. Drop the
   `'up' -> 'above'` mapping and the docstring paragraph that apologises for
   it. `timeframe_votes()` keeps taking `direction`, with `state == 'at'`
   abstaining. `mtf_state()` itself can stay as a thin wrapper returning
   `cloud_state(...)['direction']`, so any other caller is unaffected.
2. **`screener_service.py`** — `DAILY_BARS` from 120 to 180, so the daily
   50/55 read is warm (§6).

Optional but recommended: move `screener._normalize_bars` into
`signal_engine.py` next to `bars_to_df`, where it belongs, and have
`screener.py` re-export it. `environment.py` needs the same conversion and
importing `screener` from it would be circular. Until that move, the caller
converts and `environment.py` takes DataFrames only.

## OPEN DECISIONS FOR THE OWNER

**BOTH RULED 2026-09-30. Neither went the way this section recommended.**

1. ~~**`6Min` or `10Min`?**~~ **RULED, and my recommendation was wrong twice
   over.** The owner ruled the screener stays `6Min` as a universal and the
   engine moves toward it (board `d-screener-tf`), so the project runs two bar
   widths permanently and standardising is off the table. I then argued that a
   `bar_minutes` selector would be "worse"; **measurement says it is required**
   (board `d-bar-minutes`, ruled on the recommendation). The optimal lookback
   is the same DURATION on every timeframe — 102 min at `3Min`, 144 at `6Min`,
   150 at `10Min` — while the bar COUNT that achieves it differs by 2.3x. A
   constant in bars is therefore wrong on every feed but the one it was tuned
   on.

   It is also not "a second column." It is **one division**, in one helper, and
   `halflife.TF_MINUTES` already holds the widths. See *Lookbacks are durations*
   below.

   **The evidence this is not theoretical:** §3's coil carve-out works on
   `10Min` and **inverts** on `6Min` when §3's bar counts are used as literally
   written — killed-coil 6.02% against killed-non-coil 10.25%, t = −2.89. Re-run
   with constant-MINUTE windows it reproduces `10Min` cleanly: 34.22% against
   12.26%, t = +5.08. Same code, same data; only the window units changed and
   the sign of the finding flipped.

2. ~~**`CURL_NOISE_ATR` and the momentum band edges are the two calibration
   debts.**~~ **Half resolved, and worse than a debt.** `CURL_NOISE_ATR` was not
   uncalibrated — it was **actively harmful**: across 8,311 curl firings it
   measured *negative* lift at every threshold, discarding bars that hit MORE
   often than the ones it kept, with φ = +0.005 against the owner's own hand
   labels. It is **deleted**, not calibrated (§1, and `design/10`). The momentum
   band edges remain a genuine debt with the plan below intact: log `mom` to
   `screen_history.py` and set `MOM_WEAK` against the 36% `Low Momentum` tag
   frequency the owner has already supplied.

### Lookbacks are durations, not bar counts

```python
import halflife as hlf

def bars_for(minutes, tf, minimum=2):
    """A duration, in bars of `tf`. The ONE place bar width enters a lookback.

    Ruled 2026-09-30. Every window constant in this spec is a number of
    MINUTES; this converts it at read time. EMA periods are NOT converted --
    they are bar counts by definition and by doctrine (design/00 §2), and
    changing what the 5/12 pair is computed over is a trading change, not a
    measurement one.
    """
    width = hlf.TF_MINUTES.get(tf)
    if not width:
        raise ValueError(f'unknown timeframe {tf!r}')
    return max(minimum, int(round(minutes / width)))
```

| duration | `3Min` | `6Min` | `10Min` |
|---|---|---|---|
| `CURL_SPREAD_MINUTES` 150 | 50 | 25 | 15 |
| `MOM_MINUTES` 60 | 20 | 10 | 6 |
| `CHOP_MINUTES` 120 | 40 | 20 | 12 |
| `COIL_RECENT_MINUTES` 60 | 20 | 10 | 6 |
| `COIL_PRIOR_MINUTES` 240 | 80 | 40 | 24 |

**Every `10Min` column is the old bar count**, by construction: the durations
were chosen so the constant this spec was written against is preserved exactly
on the feed it was reasoned on. Nothing about the `10Min` behaviour changes.
What changes is that `6Min` stops being silently wrong.

**Two constants stay in bars, deliberately.** `CURL_SPREAD_RECENT = 3` is the
owner's "3 candles" and the measured optimum on both feeds independently (at
`6Min`, 2 and 3 tie at AUC 0.582/0.581) — it is a count of candles, not a
duration. `EMA_WARMUP_SPANS` multiplies EMA periods, which are bar counts.

## WHAT THIS SPEC DELIBERATELY DOES NOT DO

- **It does not touch `_volume_ok`.** That gate has a documented pace
  correction and it controls whether options may be traded. Improving a
  measurement should not change execution as a side effect.
- **It does not measure execution.** No hold time, no entry timing, no
  fill quality. `Late Entry/Exit` (46), `Double Down` (37), `Early Exit` (24)
  and both `Quick Exit` variants are all execution criticisms and none of them
  belongs in an environment GPA. The owner's rule that the GPA grades
  environment only is the whole reason this module can be pure functions over
  bars.
- **It does not predict.** Every number here describes the tape as it stands
  at the last closed bar. `conditions.py` says it of the journal and it is
  true here too: none of this is predictive, it describes what is in front of
  you.
- **It does not add a scoring function.** No weights, no 0–1 normalization, no
  composite. The 7-component weighted score is being retired; reimplementing
  its shape one module lower would be the same mistake with a new name. This
  module emits labels. `grading.py` assigns credit.
