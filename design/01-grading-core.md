# 01 — grading.py, the rubric core

**Status:** design spec. No code written, no existing `.py` touched.
**Replaces:** `screener.DEFAULT_WEIGHTS` and the 0–1 weighted `score`.
**Siblings:** measurement module (curl / momentum / chop / ATR structure / volume / true MTF), UI.

---

## 0. WHY THIS EXISTS, AND WHAT DIED

The seven-component weighted score in `screener.py` was honest about being
judgement — its own docstring says the weights "are not derived from outcome
data, and the UI exposes them precisely because they should not be trusted as
given." That honesty was the problem. A 0.734 that the user can re-weight with
sliders is a number nobody can defend and nobody can argue with. Two traders
looking at the same 0.734 cannot disagree usefully, because there is no claim
in it.

The GPA replaces it with a **transcript**: a fixed list of largely true/false
conditions, each carrying credit fixed in code, averaged into 0.0–4.0. You can
disagree with a transcript line by line. That is the entire point of the
change, and it is why **the credit is not user-adjustable**. A slider on
credit would reintroduce the thing being deleted.

### What it grades, and what it refuses to grade

The GPA grades the **environment only**. Owner: *"we're grading on environment
only. No grade on execution, but notes and actual P/L will speak for itself."*

A 4.0 means **the conditions were aligned for a move**. It does not mean a
trade was taken, taken well, sized well, or exited well. It is a statement
about the chart at a moment, not about the human. This has a consequence the
owner should expect and not treat as a bug: **you will see 4.0 environments
that you lost money in, and 1.0 environments where you made money.** Those
rows are the useful ones. If the GPA correlated perfectly with your P/L it
would have smuggled execution back in and we would have rebuilt the old grade
with a new scale.

### What was deliberately NOT done

- **No fit to the 382 existing grades.** Those grades are execution-flavoured
  composites. `Quick Exit` is the highest-grading tag in the whole book (mean
  grade **2.37**, n=52) and it is pure hindsight — you cannot know at 09:41
  that the exit you have not made yet will be quick. Fitting the new scale to
  the old one would be fitting to hindsight. §8 says what we test instead.
- **No `conditions.edge_for()` in the grade.** The journal edge is displayed
  *next to* the GPA as a sanity check and never feeds it. Folding realized P/L
  into an environment grade makes the grade self-confirming: high GPA states
  would be high-GPA because they made money, and the ordering test in §8 would
  become circular and always pass.
- **No letter-grade curve.** A fixed rubric, not a distribution. If a month
  produces forty 1.0s, that is information about the month.
- **No bullish tilt**, although the journal has a large one (§9.4).

---

## 1. THE CONDITION LIST

Sixteen conditions in five groups. The group is the credit tier, and the tier
is timeframe relevance: **what governs the next few bars outranks what governs
the next few days.**

Grade points per condition are on the same 0–4 scale as the composite. In
practice they take three values: **4 = true, 2 = the middle state, 0 = false.**
A few conditions have no legitimate middle and are 4/0 only — those are called
out, because "largely true/false" is the owner's phrase and the exceptions
should be visible.

Notation: `value` is the raw measurement stored on the transcript row;
`measured` flags whether the condition is arithmetic or judgement encoded as
arithmetic.

---

### Group A — TRIGGER (3 credits each). Governs the next few bars.

Decided on the 10-minute chart. If A is wrong, you know inside two candles.
This is the tier the owner was describing: *"5/12 curl is stronger in very
short timeframe than a distant magnet."*

#### A1 `curl_512` — "5/12 Curl" · credit **3** · measured
The 5/12 pair *turning*, which is not the same thing as a fresh cross.
`signal_engine.trend_context()` already gives `fresh_long`/`fresh_short`, and
those are **close-crossing-cloud events** — a one-bar flag that is false 95% of
the time. The owner's `5/12 Curl` tag (78 uses) describes a *state*: the fast
pair rolling over or rolling up, which can be true for several bars and can be
true before the cross.

- reads `measure.curl_512(df10) -> {'state': 'up'|'down'|'flat', 'value': float, 'bars': int}`
- **4** — `state` agrees with `direction` (`up` for long, `down` for short)
- **2** — `state == 'flat'`
- **0** — `state` opposes `direction`
- N/A when the sibling returns `None` (insufficient bars)

Why 3 credits — the top tier — is the one credit decision with direct outcome
evidence behind it. From `conditions.rank()` on the 205 P/L-bearing legs
(book average **−$14.94**):

| factor | shrunk edge vs book | n | confidence |
|---|---|---|---|
| `5/12 Curl` marked good | **+$21.67** | 53 | strong |
| `5/12 Not Followed` marked bad | **−$30.59** | 40 | strong |

A ~$52 spread, the widest of any single tag. And the owner's specific claim —
that the curl outranks a magnet that is arguing against it — holds up on the
raw legs:

| | avg P/L | n |
|---|---|---|
| curl, in a chop/low-momentum environment | **+$14.21** | 29 |
| curl, in a clean environment | +$6.73 | 24 |
| no curl, chop/low-momentum | −$20.49 | 85 |
| no curl, clean | −$28.28 | 67 |

The curl survives a bad environment. It is the only condition in this rubric
for which that is demonstrably true, and it is the reason the tier ladder is
steep rather than flat.

#### A2 `pos_512` — "EMA 5/12 10m" · credit **3** · measured
Price against the 5/12 cloud, direction-relative. The journal's own condition.

- reads `ctx['price_vs_5_12']` from `signal_engine.trend_context()`, then `align()` (§2)
- **4** — `with`
- **0** — `against`
- **0** — `at` — **this is a deliberate departure, see below**
- N/A only when `ctx` is `None` (fewer than 51 closed bars)

**`at` scores zero, not two.** The default for a middle state on this rubric is
2, and the journal vocabulary calls this value `at` (101 records), which reads
like a neutral. It is not one:

| `EMA 5/12 10m` | shrunk edge | n |
|---|---|---|
| with trade | +$15.76 (strong) | 109 |
| **at cloud** | **−$13.66 (some)** | 56 |
| against trade | −$19.47 (some) | 40 |

Inside the 5/12 cloud sits nearer to *against* than to neutral. That is not
surprising — it is price with no fast-timeframe direction, which is the
definition of the thing the owner tags as chop. Scoring it 2 would hand a free
half-credit to the most common bad state in the book. This exception is listed
here rather than buried because it is the one place the rubric contradicts its
own "middle state = 2" rule on outcome grounds.

---

### Group B — STRUCTURE (3 / 2 / 2 / 1). Where price is, not where an average is.

Every other group in this rubric grades a moving average. This group grades
**price against levels**, and it is the group the owner's notes are actually
written in ("34/50 curl long VS PML, bullish bias over PDC"). It also carries
the largest empirical spread in the entire journal:

| `S/R/P Followed` | shrunk edge | n | raw avg P/L |
|---|---|---|---|
| Correctly | +$18.17 (strong) | 29 | +$9.50 |
| Mixed | +$19.94 (strong) | 71 | +$7.81 |
| N/A | +$3.47 (noise) | 28 | −$10.23 |
| **Incorrectly** | **−$28.22 (strong)** | 77 | **−$46.82** |

A ~$46 spread. **But `srp` is not usable as a GPA input**, and this is the
biggest honest weakness in the whole design: "S/R/P Followed: Correctly" is
knowable only *after* price respects the level. It is a post-hoc verdict, the
same class of thing as `Quick Exit`. Group B is a **forward-looking substitute**
for the journal's single most predictive factor, assembled from things
`levels.py` computes before the fact — is there a level, whose level is it, is
there room past it, is it stacked. That substitution is untested. It is the
first thing §8's forward test should be pointed at.

#### B1 `level_in_reach` — "Level In Play" · credit **3** · measured
Is there a reference level close enough that the next few bars are *about* it?

- reads `levels.nearest(level_set, within_atr=screener.APPROACH_ATR)`, filtered
  to levels on the side `direction` is heading toward
- **4** — nearest such level within `screener.NEAR_ATR` (0.20 ATR)
- **2** — within `screener.APPROACH_ATR` (0.50 ATR)
- **0** — nothing within 0.50 ATR
- **never N/A.** No level in reach is a real, gradeable state — it is the
  owner's `no setup`, the single worst-performing tag in the book
  (**−$36.10**, strong, n=30; mean owner grade **0.75**). It also triggers a
  cap (§5).

Credit 3 alongside the 5/12 conditions because proximity is what makes the next
few bars *decidable*. A ticker 1.4 ATR from anything has no next few bars; it
has a drift.

#### B2 `level_quality` — "Level Quality" · credit **2** · judgement
Whose level is it. `screener.LEVEL_QUALITY` already encodes this judgement well
and it is preserved, not reinvented — but it is a 0–1 multiplier and this
rubric needs 0/2/4, so `grading.py` carries its own table. **`screener`'s table
is not mutated** (the old score is being deleted, not re-tuned, and other
callers read that dict).

```python
LEVEL_CREDIT = {
    'manual':      4,   # you typed it off the sheet this morning
    'premarket':   4,   # PMH/PML — owner named these explicitly; 0.95 in screener
    'prior_day':   3,
    'extreme':     3,   # ATH
    'floor_pivot': 2,   # ...but 3 for R1/S1 specifically, see below
    'prior_week':  2,
    'prior_month': 2,
    'psych':       2,   # 0.40 in screener; promoted, with a limit — §9.2
}
NAMED_PIVOTS = ('R1', 'S1')   # owner named these; P/R2/S2 stay at 2
```

- **4 / 3 / 2** from the table, using the `kind` of the B1 level; `R1`/`S1` by
  `name` override to 3
- **0** never awarded — a level that exists has some quality
- **N/A** when B1 scored 0 (there is no level whose quality could be graded)

Two changes from `screener.LEVEL_QUALITY`, both because the owner named the
levels: `premarket` moves level with `manual` (it was already 0.95, so this is
a rounding decision, not a reversal), and `R1`/`S1` are lifted above the other
floor pivots. `psych` is promoted from last place but only to 2 — §9.2 explains
why promoting it further breaks B1.

#### B3 `room_ahead` — "Room To Run" · credit **2** · measured
Clear air past the level. `screener.room_ahead()` already computes exactly this
and is reused verbatim.

- reads `screener.room_ahead(level_set, level, direction)` against `ROOM_FULL_ATR` (1.0)
- **4** — `>= 1.0` ATR, **or `None`** (nothing lies beyond: open road — the
  same reading `screener` already gives it)
- **2** — `0.5` to `1.0` ATR
- **0** — `< 0.5` ATR
- **N/A** when B1 scored 0, or when `level_set['atr']` is `None`

#### B4 `level_confluence` — "Levels Stacked" · credit **1** · measured
A shelf. `levels.confluence(band_atr=0.15)` already has the chaining guard that
stops a run of evenly spaced levels reporting as one wide shelf.

- reads `levels.confluence(level_set)`, taking the group nearest the B1 level
- **4** — `count >= 3`
- **2** — `count == 2`
- **0** — no group within `APPROACH_ATR`
- **N/A** when `atr` is `None`

Only 1 credit despite the owner's `Cloud Confluence Support/Magnet` tag scoring
well (**+$16.79**, strong, n=56). That tag conflates two different things — a
stack of *levels* and the *cloud* acting as a magnet — and this rubric splits
them (C2 is the other half). Neither half should inherit the whole tag's
credit.

---

### Group C — REGIME (2 / 2 / 2 / 1). Governs the session.

#### C1 `gate_3450` — "EMA 34/50 10m" · credit **2** · measured
Ripster's hard gate. Not a vote — `signal_engine`'s docstring is explicit:
"Nothing goes long below the 34/50; nothing shorts above it."

- reads `ctx['price_vs_34_50']`, then `align()`
- **4** — `with`
- **0** — `against`
- **0** — `at` (inside the cloud = `trend == 'chop'`)
- N/A only when `ctx` is `None`

Both 0s, and both trigger caps (§5). **Flagging a conflict honestly:** the
journal says `EMA 34/50 10m: at cloud` was **+$11.50** (strong label, but
n=18). Taken at face value that says trading from inside the 34/50 cloud was
*fine*. It is kept at 0 anyway, because n=18 is not enough to overturn the
central rule of the strategy the whole codebase implements, and because
`signal_engine` will decline the entry regardless — a rubric that grades an
environment the engine refuses to trade is grading fiction. Revisit at n≥40.

Credit 2, not 3, even though it is a hard gate: the 34/50 on a 10-minute chart
governs the session, not the next two bars, and its spread is narrower than the
5/12's (+$10.25 / −$17.52, ≈$28 vs the 5/12's ≈$35). The gate's force is
expressed as a **cap**, which is the right instrument for a hard rule — credit
is for *how much a condition contributes*, caps are for *what a condition
forbids*. Putting a hard gate in the credit weighting instead of in the cap
list is the mistake this split avoids.

#### C2 `cloud_support` — "Cloud As Support/Magnet" · credit **2** · **judgement**
The other half of `Cloud Confluence Support/Magnet`, and the owner's
`Cloud Rider (Slow trend)` (60 uses, +$4.50 noise): is the 34/50 cloud sitting
*behind* price, close enough that a pullback lands on it rather than through it?

- reads `ctx['clouds']['regime']` (already `[bot, top]`) and `level_set['atr']`
- for a long: `gap = (price - regime_top) / atr`; for a short: `(regime_bot - price) / atr`
- **4** — `0 < gap <= 0.5`
- **2** — `0.5 < gap <= 1.0`
- **0** — `gap > 1.0` (cloud too far to catch anything) or `gap <= 0` (price is
  not on the near side — C1 already scored that)
- N/A when `ctx` is `None` or `atr` is `None`

This is the one condition in the rubric that is **my construction, not the
owner's measurement and not the journal's vocabulary**. It is a plausible
reading of "magnet" and it is `measured: False` on the transcript so the UI can
mark it. If the forward test in §8 finds it contributes nothing, it should be
cut rather than re-tuned.

#### C3 `momentum` — "Momentum Present" · credit **2** · measured (sibling)
- reads `measure.momentum(df10) -> {'state': 'strong'|'moderate'|'low', 'value': float}`
- **4** — `strong` · **2** — `moderate` · **0** — `low`
- N/A when the sibling returns `None`

#### C4 `chop` — "Not Choppy" · credit **1** · measured (sibling)
- reads `measure.chop(df10) -> {'state': 'clean'|'mixed'|'choppy', 'value': float}`
- **4** — `clean` · **2** — `mixed` · **0** — `choppy`
- N/A when the sibling returns `None`

**C3 and C4 carry low credit on purpose, and this is the recommendation the
owner is most likely to dislike.** `Low Momentum Environment` (138 uses) and
`Choppy Environment` (125 uses) are the two most-written criticisms in the
journal. They are also, in the P/L record, **nothing**:

| tag | avg P/L with | avg P/L without | shrunk edge | n |
|---|---|---|---|---|
| Low Momentum Environment | −$10.82 | −$17.74 | +$3.68 (noise) | 83 |
| Choppy Environment | −$15.14 | −$14.83 | −$0.18 (noise) | 71 |

Trades tagged *low momentum* made **more** than trades not so tagged. Trades
tagged *choppy* were indistinguishable from the book. Mean owner grade tells
the same story: 1.46 and 1.42 against a book mean of **1.36** — the two
criticisms mark trades that graded *slightly above average*.

The reading I believe: these tags describe why a trade felt bad, not why it
lost. A chop environment produces small losses *and* small wins, so it moves
the variance and not the mean. Meanwhile A1 shows the curl was *better* inside
chop than outside it.

So: 3 credits of 25 (12%) go to momentum-and-chop combined, they are graded
because the owner wants them visible and named in his own words, and **chop
alone is not a disqualifier**. Only the *conjunction* of low momentum and chop
caps anything (§5), and that conjunction is the owner's `No Environment`, which
his own grading punishes hard (mean grade **0.60**). Giving these tags credit
proportional to how often he writes them would be giving 20%+ of the scale to a
factor with a measured edge of zero. That is the single most likely way for this
GPA to end up worse than the score it replaces.

---

### Group D — HIGHER TIMEFRAME (1 credit each). Governs the next few days.

Bottom tier, and the data agrees with the timeframe reasoning:

| factor | with | against | spread |
|---|---|---|---|
| `MTF 1HR 34/50` | +$12.88 (strong) | −$10.35 (strong) | ≈$23 |
| `MTF 1D 20/21` | +$5.43 (**noise**) | −$4.15 (some) | ≈$9.6 |

The daily is the weakest discriminator in the journal. The owner's stated
principle and the outcome record land in the same place independently, which is
the strongest thing that can be said for a credit scheme built mostly from
reasoning.

#### D1 `mtf_1h` — "MTF 1HR 34/50" · credit **1** · measured (sibling)
#### D2 `mtf_1d` — "MTF 1D 20/21" · credit **1** · measured (sibling)

- read `state['mtf']['1h_34_50']['pos']` and `state['mtf']['1d_20_21']['pos']`,
  each `'above'|'below'|'inside'`, then `align()`
- **4** — `with` · **2** — `at` (i.e. `inside`) · **0** — `against`
- N/A when the sibling returns `None` (not enough hourly/daily history)

`at` scores 2 here, unlike A2, because the journal supports neutrality on these
(`MTF 1HR 34/50: at cloud` = +$1.76, noise, n=27) — inside a higher-timeframe
cloud genuinely is "no information", where inside the 5/12 is "no fast
direction".

**Interface demand on the sibling, stated loudly:** these must be **true price
versus cloud**. `screener.mtf_state()` returns the EMA pair's *direction* and
`screener.journal_conditions()` then maps `up → above`, which its own docstring
admits is a proxy. A stock can be well below a rising 1H cloud. That proxy
produced 210 of the 382 journal records and is one of the reasons `mtf_1d`
looks like noise — some fraction of those `above`/`below` labels are simply
wrong. `grading.py` will **not** accept the direction proxy: if the sibling
cannot supply a real `pos`, D1/D2 return N/A, and an honest N/A is better than a
confident mislabel. This is the single most important item to reconcile.

---

### Group E — TRADABILITY (1 credit each). Preconditions, not signals.

Neither of these predicts direction. They say whether the instrument is capable
of the move at all, which is why they are graded but cheap. The owner asked for
volume and ATR as first-class parts of the system; first-class here means *on
the transcript every time, with a visible credit*, not *heavily weighted*.

#### E1 `atr_structure` — "Range Big Enough" · credit **1** · measured (sibling)
- reads `measure.atr_structure(daily_bars, price) -> {'state': 'ample'|'thin'|'dead', 'value': float}`
- **4** — `ample` · **2** — `thin` · **0** — `dead` · N/A on `None`

#### E2 `volume` — "Volume Participating" · credit **1** · measured (sibling)
- reads `measure.volume_read(df10, adv) -> {'state': 'heavy'|'normal'|'light', 'value': float}`
- **4** — `heavy` · **2** — `normal` · **0** — `light` · N/A on `None`
- The existing `signal_engine` opening-30-minute pace gate is a *gate*, not a
  read, and `screener.rel_volume()` is one bar against twenty. Neither is a
  continuous read. Sibling supplies one; until it does, E2 is N/A, which costs
  1 credit of 25 and changes nothing.

---

### 1.9 The credit table

```python
CREDIT = {
    # A — next few bars
    'curl_512':         3,
    'pos_512':          3,
    # B — structure
    'level_in_reach':   3,
    'level_quality':    2,
    'room_ahead':       2,
    'level_confluence': 1,
    # C — the session
    'gate_3450':        2,
    'cloud_support':    2,
    'momentum':         2,
    'chop':             1,
    # D — the next few days
    'mtf_1h':           1,
    'mtf_1d':           1,
    # E — preconditions
    'atr_structure':    1,
    'volume':           1,
}
TOTAL_CREDITS = 25        # asserted at import
```

Group sums: A 6, B 8, C 7, D 2, E 2 → **25**.

Structure (B) is the heaviest group. That is deliberate and it is the one place
the rubric departs from pure timeframe reasoning: levels are timeframe-agnostic
— a level matters whenever price reaches it — but they are the only *price*
condition on the sheet, everything else being a transform of price, and they
carry the widest measured spread in the book (§Group B). If a single group has
to be wrong, this is the one to test first.

The owner's arithmetic claim is preserved literally: `curl_512` is **3** credits
against `cloud_support` **2** and `level_confluence` **1** — *"the curl was
worth 1 arbitrary point more."*

---

## 2. DIRECTION RELATIVITY

Raw `above`/`below` means opposite things depending on which way the move is
being considered, and `conditions.py` already solved this for the journal. Its
solution is adopted unchanged in vocabulary — `with` / `against` / `at` — so the
screener and the journal analysis can never describe the same chart in two
languages.

### One difference, and it matters

`conditions.bias(leg)` derives direction from the **instrument**: a long put is
bearish, so `below` is `with`. The screener has no instrument in hand. It is
grading a **directional move**, so:

```python
def direction_for(level_set: dict, ctx: dict | None) -> tuple[str | None, str]:
    """('long'|'short'|None, why). Same logic as screener._alignment(): the
    side of the in-play level decides, and with no level in play the 34/50
    trend decides."""
```

This is intentionally the same rule `screener._alignment()` already uses, so
the GPA's direction and the old board's direction can never disagree. It is
lifted rather than imported because `_alignment()` also returns an `aligned`
boolean that C1 now supersedes.

```python
def align(raw: str | None, direction: str) -> str | None:
    """'with' | 'against' | 'at' | None. raw is journal/engine vocabulary:
    'above', 'below', 'at', 'inside'. None means unreadable — a blank, or a
    mixed value such as the one 'at, above' record in the journal."""
```

**Pinned invariant, to be a unit test:**

```
align(raw, 'long')  == conditions._align(raw, 'bull')
align(raw, 'short') == conditions._align(raw, 'bear')
```

for every raw in `('above', 'below', 'at', 'inside', '', None, 'at, above')`.
The test exists so that a change to either file breaks loudly instead of
quietly producing two different definitions of "with the trade". `grading.py`
does **not** import `conditions._align` — importing another module's private is
how a refactor over there silently regrades everything over here. The test is
the contract; the duplication is deliberate and is the cheaper of the two
couplings.

Conditions with no direction (B1–B4 as distances, C2–C4, E1, E2) are evaluated
with `direction` only where it picks a side — B1 filters levels to the side
price is heading, C2 measures the gap on the correct side. Nothing is scored
from a raw `above`/`below` anywhere in this module.

---

## 3. THE CREDIT SCHEME AND THE ARITHMETIC

It is a **credit-hour GPA**, run exactly the way a registrar runs one. Each
condition is a course. It has credit hours (fixed in code) and grade points
earned (0–4).

```
        Σ (points_i × credit_i)     over conditions where na_i is False
GPA  =  ──────────────────────
            Σ credit_i             over the same conditions
```

With every condition attempted: denominator 25, maximum numerator 100, so the
scale is 0.0–4.0 by construction and needs no normalisation constant, no
clamping, and no rescaling when a condition is added. **Adding a 17th condition
later changes the denominator and nothing else.** That property is why this
structure was chosen over "weights that sum to 1.0 times 4" — the thing that
made `DEFAULT_WEIGHTS` brittle was that every change required re-normalising
all seven.

### Worked example — a good long, nothing missing

| condition | credit | points | weighted |
|---|---|---|---|
| curl_512 (up, with) | 3 | 4 | 12 |
| pos_512 (with) | 3 | 4 | 12 |
| level_in_reach (0.11 ATR) | 3 | 4 | 12 |
| level_quality (PML, premarket) | 2 | 4 | 8 |
| room_ahead (1.2 ATR) | 2 | 4 | 8 |
| level_confluence (2 levels) | 1 | 2 | 2 |
| gate_3450 (with) | 2 | 4 | 8 |
| cloud_support (0.4 ATR behind) | 2 | 4 | 8 |
| momentum (moderate) | 2 | 2 | 4 |
| chop (mixed) | 1 | 2 | 2 |
| mtf_1h (with) | 1 | 4 | 4 |
| mtf_1d (against) | 1 | 0 | 0 |
| atr_structure (ample) | 1 | 4 | 4 |
| volume (normal) | 1 | 2 | 2 |
| **total** | **25** | | **86** |

`gpa_raw = 86 / 25 = 3.44` → **displayed 3**.

That is the right answer and it demonstrates the scale is not generous: price
at a premarket level with a curl, the gate onside, room ahead, the hour
agreeing — and it is a 3, because momentum is only moderate, the day disagrees
and volume is ordinary. **4.0 should be rare.** The owner's own journal gave
4 to 11 of 382 records (2.9%); a rubric that mints 4.0s weekly has redefined
the word.

### Rounding, and what gets stored

```python
def round_gpa(raw: float) -> int:
    """0-4 in 1.0 increments. Half-up below the top, with a raised bar for a 4."""
    if raw >= 3.75:
        return 4
    return min(3, int(math.floor(raw + 0.5)))
```

Half-up everywhere except the top of the scale, where 3.75 is required rather
than 3.50. Plain half-up would turn every 3.5 into a 4.0, and on the 25-credit
table a 3.5 is reachable while failing three or four conditions outright. The
3.75 bar keeps 4.0 meaning what the owner's 11 records meant.

**The stored value keeps the unrounded composite. Recommended, and it is not a
compromise on the owner's "1.0 increments" — the increments are a display and
comparison rule, not a storage rule.** Three reasons:

1. **Ranking.** `screener.rank()` sorts 40 tickers into 5 slots. On a 5-bucket
   scale a normal morning puts a dozen tickers on 2 and the tie-break falls
   through to `state`, which is far coarser than the GPA it is supposed to be
   subordinate to. `gpa_raw` orders the board; the UI never shows the decimal.
2. **The validation in §8 needs a continuous variable.** A rank correlation
   over five buckets on 205 legs is close to uninformative. Throwing away the
   decimal would throw away the only means of checking whether the unequal
   credit does anything that equal credit would not.
3. **Drift is invisible without it.** A ticker sliding 3.4 → 2.6 across an hour
   is a real deterioration that prints as "3, 3, 3, 3, 3" and then abruptly "2".

So: `gpa` (int, for humans and for the journal's `grade` field), `gpa_raw`
(float, 2dp, for sorting, logging into `screen_history`, and testing). If they
ever disagree in a way that confuses someone, `gpa` is the one that is true —
`gpa_raw` is machinery.

### Letters

`{4:'A', 3:'B', 2:'C', 1:'D', 0:'F'}`. Included because the GPA metaphor
invites it and a letter is faster to scan than a digit on a dense board. It
carries no information the integer does not.

---

## 4. N/A: MISSING IS NOT FAILING

This is the requirement that drives the whole structure, and the credit-hour
form answers it without any bespoke redistribution logic:

**An N/A condition is a course not taken. It leaves the numerator AND the
denominator.**

No redistribution table, no renormalising, no "spread the missing credit
across the survivors" — the survivors' *shares* rise automatically because the
denominator shrank, exactly in proportion to their own credit. That is the
correct behaviour and it is free.

### Worked example — pre-open, sibling module not yet landed

`curl_512`, `momentum`, `chop`, `atr_structure`, `volume` all N/A (8 credits
gone). Of the remaining 17 credits, suppose `room_ahead` scores 2 and
`level_confluence` 2, everything else 4:

`(12 + 12 + 8 + 4 + 8 + 8 + 2 + 4 + 4) = 62`, `62 / 17 = 3.65` → **displayed 3**.

Not penalised for the eight missing credits — it lands where an equally good
full transcript would land. That is the test in §8.5.

### Which conditions may be N/A

| condition | N/A when |
|---|---|
| `curl_512`, `momentum`, `chop`, `atr_structure`, `volume` | sibling returns `None` |
| `pos_512`, `gate_3450`, `cloud_support` | `ctx is None` (<51 closed 10m bars) |
| `mtf_1h`, `mtf_1d` | no hourly/daily history, **or only the direction proxy available** (§Group D) |
| `level_quality`, `room_ahead` | `level_in_reach` scored 0, or `atr is None` |
| `level_confluence` | `atr is None` |
| **`level_in_reach`** | **never** |

`level_in_reach` is never N/A by design. "No level anywhere near" is a fact
about the chart, not a gap in the data, and it is the best-evidenced bad state
in the journal (`no setup`: −$36.10, strong). Letting it go N/A would let the
worst environment in the book score by omission.

Note what does *not* appear: PMH/PML missing. A name with no premarket prints
is common and legitimate (`levels.premarket_extremes()` returns `None` and says
so in its docstring). It does not make anything N/A — it just means the B1
level will be some other kind and `level_quality` will grade it accordingly.
Absent premarket data lowers `level_quality` only if a worse level is the
nearest one, which is the truth.

### The floor

```python
MIN_CREDITS_FOR_GPA = 13
```

Below 13 of 25 attempted credits, `gpa` and `gpa_raw` are **`None`** and
`incomplete` is `True`. Never `0` — a 0.0 is a verdict and a `None` is an
absence, and conflating them is how "no data" becomes "bad setup" on a board.

13 is not arbitrary: it is exactly what the **existing** code can measure with
no sibling work at all — `level_in_reach` 3 + `level_quality` 2 + `room_ahead` 2
+ `level_confluence` 1 + `pos_512` 3 + `gate_3450` 2 = 13. So `grading.py` is
computable the day it lands and every sibling delivery raises the transcript's
completeness without changing the scale.

### The one interaction that needs stating

If `level_in_reach` scores 0, three dependent conditions go N/A and attempted
credits can fall to 8 — below the floor. Without a rule, the worst state in the
book would return `None`. So: **a cap always produces a GPA.** If any
disqualifier in §5 fires, `incomplete` does not suppress the result; the GPA is
`min(earned_or_0.0, cap)` and the transcript says which cap and why.

---

## 5. DISQUALIFIERS (CAPS)

Caps are for hard rules. Credit is for contribution. Mixing the two is how a
hard gate ends up as "a heavy weight" that enough other components can outvote
— which is the exact failure `signal_engine`'s docstring describes in the old
3-vote engine: "let the 34/50 be outvoted (allowing counter-trend entries)…
Those three gaps were the loss profile."

A cap is `min()` on the composite, applied to `gpa_raw` before rounding, and
every cap hit is recorded on the transcript with its reason. Multiple caps take
the lowest.

| id | fires when | cap | evidence |
|---|---|---|---|
| `no_setup` | `level_in_reach == 0` | **1.0** | `no setup` −$36.10 strong n=30; owner grade 0.75 |
| `against_gate` | `gate_3450` state is `against` | **1.0** | `EMA 34/50 against` −$17.52 strong n=78; and the engine will refuse the entry |
| `no_environment` | `momentum == 0` **and** `chop == 0` | **1.0** | owner grade for `No Environment` **0.60** vs book 1.36 |
| `inside_gate` | `gate_3450` state is `at` (price inside 34/50) | **2.0** | engine declines rather than contradicts; journal `at cloud` +$11.50 n=18 argues against a harder cap |
| `curl_against` | `curl_512` state opposes `direction` | **2.0** | `5/12 Not Followed` −$30.59 strong n=40 |

### Notes on each

**`no_environment` is honoured, not evidenced.** The P/L edge on the
`No Environment` tag is −$2.12 (noise, n=36). The owner's own grading, however,
gives it a mean of 0.60 against a book mean of 1.36 — the harshest of any tag
except `Hold over night/ weekend` (0.07). This cap encodes the owner's
judgement that a dead tape is not gradeable, and it is labelled as judgement on
the transcript. Note carefully that it requires **both** momentum and chop to
fail — either one alone caps nothing, for the reasons in §C4.

**`curl_against` caps at 2.0, not 1.0.** The curl carries the widest spread in
the book, so a curl running against the move being considered is serious. But
it is a *trigger*-timeframe fact and a curl can flip in two bars, where a level
either exists or does not. 2.0 says "this is not gradeable as aligned right
now" without claiming the environment is worthless.

**No cap on `chop` alone, and no cap on low momentum alone.** See §C4: the
curl-in-chop bucket was the best-performing bucket in the entire journal
(+$14.21, n=29). A chop cap would have suppressed it.

**No cap for a bearish setup**, despite `Bias: bearish` scoring −$17.27
(strong, n=91) against `Bias: bullish` +$14.07 (strong, n=114). See §9.4.

---

## 6. THE TRANSCRIPT SHAPE

The contract with the UI sibling. Everything needed to render every row — its
points, its credit, its share of the denominator, its raw measured value, a
sentence of why, and whether it is measured or judged.

```python
{
  'version': 'grading/1',
  'ticker': 'NBIS',
  'at': '2026-09-25T10:32:00',          # naive ET, same convention as screen_history

  'direction': 'long',                   # 'long' | 'short' | None
  'direction_why': 'nearest level PML is below price',

  'gpa': 3,                              # int 0-4, or None when incomplete
  'gpa_raw': 3.44,                       # float 2dp after caps, or None — SORT ON THIS
  'gpa_earned': 3.44,                    # before caps, always present when gradeable
  'letter': 'B',                         # or None

  'credits_attempted': 25,
  'credits_possible': 25,                # == TOTAL_CREDITS, so the UI can show 25/25
  'points_earned': 86,                   # Σ points*credit
  'points_possible': 100,                # 4 * credits_attempted
  'incomplete': False,
  'incomplete_why': None,                # e.g. 'only 9 of 25 credits measurable'

  'caps': [                              # every cap that fired, lowest first; [] is normal
    {'id': 'inside_gate', 'label': 'Price inside the 34/50 cloud',
     'cap': 2.0, 'why': 'Ripster hard gate: no entries from inside the regime cloud',
     'measured': True}
  ],
  'cap_applied': None,                   # the binding cap value, or None

  'conditions': [                        # ALWAYS all 16, in rubric order, N/A included
    {
      'id': 'curl_512',
      'label': '5/12 Curl',              # owner-facing, his vocabulary
      'group': 'trigger',
      'group_label': 'Next few bars',
      'credit': 3,
      'points': 4,                        # 0 | 2 | 4, or None when na
      'weighted': 12,                     # credit * points, or 0 when na
      'share': 0.12,                      # credit / credits_attempted — the UI's bar width
      'na': False,
      'state': 'with',                    # closed vocabulary, see below
      'value': 0.62,                      # the raw number, whatever it is
      'value_label': '+0.62 ATR/bar, turning up',
      'why': '5/12 slope turned up 2 bars ago; price closed above the cloud',
      'source': 'measure.curl_512',       # which function produced it
      'measured': True                    # False for judgement (cloud_support, level_quality)
    },
    ...
  ],

  'by_group': [                          # for a group header row / small multiples
    {'id': 'trigger',   'label': 'Next few bars',   'credit': 6, 'attempted': 6,
     'points_earned': 24, 'points_possible': 24, 'gpa': 4.0},
    {'id': 'structure', 'label': 'Where price is',  ...},
    {'id': 'regime',    'label': 'The session',     ...},
    {'id': 'mtf',       'label': 'The next few days', ...},
    {'id': 'tradable',  'label': 'Can it move',     ...}
  ],

  'edge': {                              # conditions.edge_for() VERBATIM — sanity check,
    'edge': -4.2, 'sub': 0.46,           # NOT an input to any number above.
    'matched': [...]
  },
  'edge_note': 'journal P/L for this cloud state — not part of the grade',
}
```

### Closed vocabularies the UI can switch on

- `state` — `'with' | 'against' | 'at' | 'na'` for the aligned conditions
  (A2, C1, D1, D2); for the rest it is the sibling's own label (`'up'`,
  `'flat'`, `'strong'`, `'low'`, `'clean'`, `'choppy'`, `'ample'`, `'dead'`,
  `'heavy'`, `'light'`) or a distance bucket (`'near'`, `'approaching'`,
  `'far'`, `'open_road'`, `'shelf'`).
- `group` — `'trigger' | 'structure' | 'regime' | 'mtf' | 'tradable'`.
- `points` — `0 | 2 | 4 | None`. Nothing else is ever produced. The UI can key
  colour off this directly.

### Rules the UI can rely on

1. `conditions` always has all 16 rows in the same order, every call. N/A rows
   are present with `na: True`, `points: None`. A missing condition must be
   *visible* — the owner needs to see that the GPA was computed on 17 of 25
   credits, not silently get a number.
2. `share` sums to 1.0 across non-N/A rows. It is the width of the bar.
3. `Σ weighted == points_earned` and `4 × credits_attempted == points_possible`.
   Both are assertions in `composite()`.
4. `gpa` may be `None`; `conditions` is never empty.
5. Sort a board on `gpa_raw`, never on `gpa`.

---

## 7. FUNCTION SIGNATURES

```python
"""grading.py — Foundations Trading"""

import math
from datetime import datetime
from typing import Any, Callable, Optional

# ─── THE RUBRIC (module constants, NOT user-adjustable) ──────────────────────

CREDIT: dict[str, int]                   # §1.9
TOTAL_CREDITS: int = 25
MIN_CREDITS_FOR_GPA: int = 13
FOUR_POINT_BAR: float = 3.75
LEVEL_CREDIT: dict[str, int]
NAMED_PIVOTS: tuple[str, ...] = ('R1', 'S1')
GROUPS: tuple[tuple[str, str], ...]      # (id, label) in display order
CONDITIONS: tuple['Spec', ...]           # 16 specs, rubric order
CAPS: tuple['Cap', ...]                  # §5
LETTER: dict[int, str] = {4: 'A', 3: 'B', 2: 'C', 1: 'D', 0: 'F'}


# ─── ENTRY POINTS ────────────────────────────────────────────────────────────

def grade_ticker(state: dict[str, Any],
                 direction: Optional[str] = None,
                 *,
                 edge: Optional[dict] = None,
                 ticker: Optional[str] = None,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """One ticker's measured state -> a full transcript (§6).

    state    the measurement bundle (§7.1). Everything is optional; anything
             absent becomes an N/A row, never a zero.
    direction 'long'|'short'. None means derive it with direction_for().
    edge     conditions.edge_for() output, passed through to the transcript
             untouched. Injected rather than called so this module has no
             journal dependency and stays unit-testable with a dict.
    """

def composite(rows: list[dict], caps: list[dict] | None = None) -> dict[str, Any]:
    """Graded rows -> the GPA block: gpa, gpa_raw, gpa_earned, letter,
    credits_attempted, points_earned, incomplete, cap_applied.

    Pure arithmetic over rows. No measurement, no state, no I/O — so the
    rounding and N/A rules can be tested on hand-written rows without bars.
    """

def evaluate(spec: 'Spec', state: dict, direction: str | None) -> dict[str, Any]:
    """One condition -> one transcript row (§6). Never raises: a spec whose
    reader throws returns an N/A row carrying the exception text in `why`.
    One bad measurement must not lose the other fifteen."""

def disqualifiers(rows: dict[str, dict], direction: str | None) -> list[dict]:
    """Which caps fire, lowest cap first. rows is keyed by condition id."""

def round_gpa(raw: float) -> int:
    """0-4 in 1.0 increments; >= FOUR_POINT_BAR for a 4."""

def align(raw: str | None, direction: str) -> str | None:
    """'with' | 'against' | 'at' | None. Pinned to conditions._align (§2)."""

def direction_for(level_set: dict,
                  ctx: dict | None) -> tuple[Optional[str], str]:
    """('long'|'short'|None, why). Same rule as screener._alignment()."""

def transcript_for_journal(t: dict) -> dict[str, Any]:
    """The subset written onto a journal record / screen_history row:
    {'gpa', 'gpa_raw', 'credits_attempted', 'caps': [ids],
     'points': {id: points}}. Flat and small — a transcript is ~4KB and
     screen_history writes one per ticker per 6 minutes."""
```

### 7.1 `state` — the interface to reconcile with the sibling

An extension of `screener.build_state()`. The existing keys keep their meaning;
the five new blocks are the sibling's.

```python
state = {
  # --- already produced today ---
  'price':      float,
  'atr':        float | None,
  'level_set':  dict,        # levels.build() output VERBATIM (not the flattened
                             # name->price map screener.build_state() makes —
                             # grading needs kind, side and distance_atr)
  'ctx':        dict | None, # signal_engine.trend_context() output verbatim

  # --- sibling-provided (new) ---
  'curl_512':      {'state': 'up'|'down'|'flat', 'value': float, 'bars': int} | None,
  'momentum':      {'state': 'strong'|'moderate'|'low', 'value': float} | None,
  'chop':          {'state': 'clean'|'mixed'|'choppy', 'value': float} | None,
  'atr_structure': {'state': 'ample'|'thin'|'dead', 'value': float} | None,
  'volume':        {'state': 'heavy'|'normal'|'light', 'value': float} | None,
  'mtf': {
      '1h_34_50': {'pos': 'above'|'below'|'inside', 'value': float} | None,
      '1d_20_21': {'pos': 'above'|'below'|'inside', 'value': float} | None,
  },
}
```

**Three rules on that interface, stated so the sibling can object now rather
than after both modules exist:**

1. **Every measurement returns `None` or a dict — never a bare float.** A bare
   float cannot distinguish "momentum is 0.0" from "momentum is unknown", and
   that single ambiguity is what turns a missing condition into a silent
   failure. This is non-negotiable; it is requirement 4 of this spec.
2. **`state` is a closed vocabulary, listed above.** `grading.py` maps labels to
   points with a dict lookup and treats an unrecognised label as N/A with
   `why='unknown state <x>'`. It will not guess from `value`. If the sibling
   wants a fourth bucket, the rubric changes here, in code review, not at
   runtime.
3. **`value` is whatever the sibling's natural unit is** and is carried to the
   transcript for display only. `grading.py` never thresholds `value` — the
   sibling owns the thresholds, because the sibling owns the measurement. The
   one exception is the distance conditions (B1–B4, C2), which threshold ATR
   distances against constants already public in `screener` and `levels`.

**The item most likely to need negotiation:** `mtf['*']['pos']` must be *price
versus the cloud*, with a real `inside`. See §Group D. If the sibling can only
deliver the EMA-pair direction, say so and D1/D2 stay N/A — 2 credits of 25.

---

## 8. VALIDATION PLAN

### The limit, first

The 382 grades are **execution-flavoured composites on a differently-defined
scale**. Eight of the eighteen distinct tag values are exit-side judgements
(`Quick Exit`, `Early Exit`, `Late Entry/Exit`, `Hold over night/ weekend`…),
and the highest-grading tag in the book is `Quick Exit` at mean grade 2.37 —
knowable only after the fact. The new scale grades environment only.

So this is **not a fit test, and a high correlation with the old grade would be
a warning sign, not a pass.** If GPA tracks the owner's grade closely, the GPA
has re-absorbed execution and the redesign failed. What is tested is
**ordering against realized P/L**: do states the GPA calls good carry better
outcomes than states it calls bad, as measured in `conditions.py` terms.

All tests run on the **205 legs with a P/L** from `pairing.build_legs()` — not
all 382 fills, since an unpaired fill has no outcome. Book average −$14.94, win
rate 65.8%, total −$3,062.

### 8.1 The ordering ladder (the headline test)

Bucket the 205 legs by GPA, report avg P/L, win rate and n per bucket, and
require **monotone non-decreasing avg P/L across buckets, with the 0 bucket
strictly worst and the 3–4 buckets strictly positive.**

Baseline to beat — the owner's own grade on the same legs:

| owner grade | n | avg P/L |
|---|---|---|
| 0 | 62 | −$48.60 |
| 1 | 73 | −$14.33 |
| 2 | 45 | +$12.01 |
| 3 | 20 | +$10.28 |
| 4 | 5 | +$50.20 |

Note 2 and 3 invert. So the bar is **"at least as monotone as the owner's own
grade"**, reported as Spearman ρ of `gpa_raw` against leg P/L, not as perfection.

### 8.2 The equal-weight control (the acceptance bar)

The sharpest test available, because it isolates the only genuinely new claim in
this spec — that **unequal** credit is worth having. Equal-weight alignment
count across the four journal cloud conditions, on the same 205 legs:

| clouds with the trade | n | avg P/L | win rate |
|---|---|---|---|
| 0 | 50 | −$34.18 | 0.62 |
| 1 | 46 | −$27.48 | 0.59 |
| 2 | 42 | −$11.46 | 0.52 |
| 3 | 30 | **+$7.03** | 0.73 |
| 4 | 37 | +$4.90 | 0.73 |

That ladder is already monotone except at the top. **If the credit-weighted
retro-GPA does not separate these legs better than this equal-weight count,
the credit scheme is decoration and should be flattened to 1 credit per
condition.** I would rather find that out in a test than defend a 3/2/1 ladder
that buys nothing. Report both Spearman ρ and the spread between the top and
bottom buckets.

### 8.3 The retro-GPA, and why it is partial

The journal holds 5 of the 16 conditions. A retro transcript can therefore
score at most:

| condition | credit | from |
|---|---|---|
| `pos_512` | 3 | `conditions.ema_5_12` |
| `gate_3450` | 2 | `conditions.ema_34_50` |
| `mtf_1h` | 1 | `conditions.mtf_1h` (direction proxy — noisy, §Group D) |
| `mtf_1d` | 1 | `conditions.mtf_1d` (same) |
| `level_in_reach` (proxy) | 3 | `srp != 'N/A'` → 4, `'N/A'` → 0 |
| **total** | **10** | |

10 credits is **below `MIN_CREDITS_FOR_GPA` (13)**, so the retro test must
lower the floor explicitly (`MIN_CREDITS_FOR_GPA=8` as a test override) and say
so in its output. It is running on 40% of the transcript with two of five
inputs derived from a proxy the code itself flags as wrong. Treat 8.3 as
supporting evidence for 8.2 and nothing more. Do not tune credit on it.

The `srp → level_in_reach` proxy is the weakest link and must be labelled as
such: `srp` is a post-hoc verdict on whether levels *were* respected, and it is
standing in for a forward statement that a level *is in reach*. It is being
used only because it is the one Group-B-shaped thing the journal recorded.

### 8.4 Cap validation

Each cap in §5 must show its capped bucket's avg P/L below the uncapped
population, on tag proxies:

| cap | proxy | expected |
|---|---|---|
| `no_setup` | `no setup` tag | −$36.10 shrunk, strong, n=30 ✓ |
| `against_gate` | `ema_34_50` against | −$17.52 shrunk, strong, n=78 ✓ |
| `curl_against` | `5/12 Not Followed` tag | −$30.59 shrunk, strong, n=40 ✓ |
| `no_environment` | `No Environment` tag | −$2.12, **noise**, n=36 ✗ on P/L; owner grade 0.60 ✓ |
| `inside_gate` | `ema_34_50` at cloud | +$11.50, n=18 — **contradicts the cap** |

Two of five caps are not supported by P/L. Both are kept and both are labelled
judgement on the transcript. `inside_gate` in particular is retained on strategy
grounds, not evidence grounds, and should be re-examined at n≥40 (§C1). Writing
that in the test output rather than hiding it is the point of this row.

### 8.5 N/A neutrality (synthetic, and the most important unit test)

Build a synthetic all-4 transcript. Drop each condition to N/A one at a time
and assert `gpa == 4` and `gpa_raw == 4.0` every time. Repeat from an all-0
transcript and assert `0`. Then drop conditions in every combination that keeps
`credits_attempted >= 13` and assert the GPA never moves.

This is the test that proves *missing is not failing*. If it fails, the credit
denominator is wrong somewhere and every other test is meaningless.

### 8.6 Invariants

- `Σ CREDIT.values() == 25`; group sums 6/8/7/2/2
- `Σ row['weighted'] == points_earned` for every transcript
- `Σ row['share'] == 1.0 ± 1e-9` over non-N/A rows
- `align()` matches `conditions._align()` on the full raw-value set (§2)
- `len(transcript['conditions']) == 16` always, including when `ctx is None`
- `round_gpa`: 0.49→0, 0.50→1, 2.49→2, 2.50→3, 3.74→3, 3.75→4
- a transcript with a cap always has a non-`None` `gpa` (§4)

### 8.7 Forward test (the only honest one)

`screen_history.py` already exists for exactly this: it logs readings at
`RECORD_EVERY_MIN=6`, joins them to fills within `MATCH_WINDOW_MIN=10`, and
declines to report until `MIN_TRADES=30`. Write `transcript_for_journal()` onto
every logged reading. After 30 matched trades, run 8.1 on **live** GPAs rather
than retro-fitted ones, and report per-condition point-vs-P/L correlation so
individual credits can be argued from results. Target for the first honest
read: **~40 trades, roughly one month.** Everything before that is a sanity
check on arithmetic, not evidence about the rubric.

Two specific questions for the forward test, both flagged above as untested:
does Group B (the forward-looking substitute for `srp`) discriminate at all,
and does `cloud_support` — the one condition I invented — contribute anything?

---

## 9. FLAGS: WHERE I THINK THE STATED PREFERENCE WILL PRODUCE A BAD RESULT

### 9.1 Momentum and chop, weighted by how often they are complained about
The strongest flag in this document. 311 tag instances across the two, and
**zero measured P/L separation** (`Low Momentum` +$3.68 noise, `Choppy` −$0.18
noise); trades tagged low-momentum made *more* than untagged ones; both tags
carry a mean owner grade slightly *above* the book mean; and the single best
bucket in the journal was the curl *inside* a chop environment (+$14.21, n=29).

These are almost certainly descriptions of why a trade felt bad, not why it
lost. My recommendation is the 3-of-25 credits above and no chop-alone cap. If
the owner insists on making chop and momentum dominant, the GPA will grade down
the best-performing state in his own book, and §8.2 will show it doing worse
than counting clouds on your fingers.

### 9.2 Psych levels as "especially important"
`screener.LEVEL_QUALITY` puts psych last at 0.40 and that judgement is sound:
`levels.psych_levels()` generates *every* round number within 2 ATR, which on a
$60 stock is a $5 grid — four or five "levels" always in reach. Promote psych to
manual-pivot status and `level_in_reach` can never score 0, which kills the
`no_setup` cap, which is the best-evidenced disqualifier in the book.

`psych` therefore goes to 2 (up from last place, honouring the instruction) and
no further. What the owner actually values, I think, is psych **confluence** — a
round number that a pivot or a prior-day extreme also sits on — and
`levels.confluence(band_atr=0.15)` already finds exactly that, scored as B4. The
instruction is honoured through the mechanism that makes it true rather than by
inflating a table.

### 9.3 "1.0 increments" as the stored value
Store the raw composite, display the integer (§3). Increments are a reading
rule. Storing only the integer costs board ordering, costs §8.2 outright, and
makes intraday deterioration invisible.

### 9.4 Environment-only, and the bullish asymmetry
`Bias: bullish` +$14.07 (strong, n=114) against `Bias: bearish` −$17.27
(strong, n=91) — a ~$31 spread, one of the largest in the book. That is an
execution-and-selection fact, not an environment fact, so it is correctly
excluded from the GPA. The consequence to expect: **the GPA will keep printing
4.0 short environments that this account has historically lost money in.** My
recommendation is to display it as a standing flag beside the GPA on any short
transcript — "your bearish book is −$17/trade vs book" — sourced from
`conditions.rank()`, never folded into the grade. It is exactly the kind of
thing the owner said notes and P/L would speak to; give the display a voice so
the grade does not need one.

### 9.5 `srp` is the best factor in the book and the GPA cannot use it
Restating §Group B because it is the design's biggest exposure. The widest
measured spread in the journal (+$18/+$20 vs −$28, all strong) belongs to a
condition that can only be evaluated after the fact. Group B's four conditions
are a forward-looking guess at it. They might not work. §8.7 is pointed at this
first.

### 9.6 `grade_ticker` already exists
`plays.grade_ticker(plays, state)` is a different function with a different
signature. `grading.grade_ticker(state, direction)` is the right name for the
owner-facing verb and both are always module-qualified in this codebase
(`screener.py` already does `import plays as PL; PL.grade_ticker(...)`), so
there is no collision in practice — but an unqualified `from x import
grade_ticker` anywhere would be a live bug. Worth a line in the module
docstring.

### 9.7 What the old score had that the GPA loses
`DEFAULT_WEIGHTS` produced a continuous 0–1 that ordered forty tickers cleanly.
A 5-bucket GPA does not. `gpa_raw` recovers most of it (§3) but a transcript is
a coarser sort key than a weighted sum by design, and `screener.rank()` will
lean harder on `state` and on live plays. That is an acceptable loss — the old
ordering was precise about a quantity nobody could defend — but it is a real
loss and should not be discovered later as a surprise.

---

## 10. DEPENDENCIES

`grading.py` imports: `math`, `datetime`, `levels`, and module constants from
`screener` (`NEAR_ATR`, `APPROACH_ATR`, `ROOM_FULL_ATR`) plus
`screener.room_ahead()`.

It does **not** import `conditions`, `journal`, `pairing` or `signal_engine`.
`ctx` and `edge` arrive as plain dicts from the caller. So the module grades
without touching a file or a network, is testable from hand-written dicts, and
cannot be slowed down by the journal being re-paired. The `align()` invariant
against `conditions._align()` lives in the test file, which may import both.
