# 09 — Trade type: the variable that makes the GPA conditional

*Ruled by the owner 2026-09-30. Built, in part, the same day.*

---

## 0. THE STATEMENT BEING IMPLEMENTED

> *"My personal trading strategy (stupid trading choices aside) is supposed to be
> the Ripster system, but I have found that I like to scalp on, for example,
> $SPY. With this, we will also need to update the journal/grading entries to
> specify what type of trade was made; Ripster, scalp, swing. I think this will
> fundamentally change how grading goes, because it's a new variable."*

He is right that it is fundamental, and the reason is sharper than "more
metadata." Until now the rubric has had exactly one implied trade type, so every
credit in `design/01` was written for a Ripster trade without anyone having to
say so. The moment a scalp exists, several conditions on that sheet stop being
questions about the *environment* and start being questions about the *wrong*
environment — a 1-day cloud does not govern a nine-minute hold, and a
low-momentum tape is not a defect in a trade whose entire thesis is to be out
before momentum would have mattered.

So the trade type is not a filter applied after grading. It selects which rubric
is applied.

---

## 1. THE VOCABULARY — THREE, CLOSED

```python
TRADE_TYPES = ('ripster', 'scalp', 'swing')     # journal.py
```

| type | what it means | what governs it |
|---|---|---|
| `ripster` | The system proper: 34/50 hard trend gate, 5/12 cross entry, structural exit. What the engine trades. | The working timeframe |
| `scalp` | A deliberate quick in-and-out, typically an index name. His words: he trades Ripster but likes to scalp SPY. | The next few bars, and nothing else |
| `swing` | A hold measured in days. | The 1H and 1D clouds |

**The vocabulary is closed and normalised at the door**
(`journal.normalize_trade_type`), unlike every other condition key, which is
deliberately open. A stray `Scalp` or `scalping` would silently split a heatmap
column in two, and both halves would be wrong without looking wrong. Blank is
legal and means *not yet typed*; a bad value raises rather than being dropped,
because a swallowed typo reads as an honest omission and never gets fixed.

## 2. WHERE IT LIVES, AND WHY THAT WAS THE WHOLE TRICK

`trade_type` is a member of `journal.CONDITION_KEYS`, first in the list because
it governs how the rest are read. It is **not** a top-level record field.

`heatmap.py` maps every condition key to an `entry_<key>` / `exit_<key>` layer
with no per-key code, and the journaling UI renders `condition_keys` generically
into its editor. Putting the type anywhere else would have meant hand-writing a
layer to answer the first question anyone asks of it — *how do my scalps grade
against my Ripster trades?* — and hand-writing a form control to enter it. In
the condition map both are free. It is the same win `exit_kind` got from the
ladder work.

Consequence, verified against the live book:
`heatmap.compare('entry_trade_type')` splits the whole grid by type, and
`pairing.collapse_to_entries` carries the entry's type onto the decision row, so
the split is by decision rather than by leg.

### The blanket backfill

```python
TRADE_TYPE_BLANKET_BEFORE = '2026-09-28'
journal.backfill_trade_type(value='ripster', before=TRADE_TYPE_BLANKET_BEFORE)
```

His ruling: everything before 2026-09-28 is `ripster` by blanket assignment;
everything on or after it he types and grades by hand. Half-open, so a fill
**on** the 28th is his.

Run against the live journal on 2026-09-30: **414 records, 386 stamped, 28 left
untyped.** That split is exactly the graded / ungraded boundary — every record
before the 28th was already graded and every record on or after it was not —
which is why the rule could be stated as a date instead of a list of ids.
Verified id by id against a pre-write backup: no field other than
`conditions.trade_type` changed on any record, and re-running the backfill
stamps nothing.

Note for whoever reads the file next: `_rewrite` emits in `read_all` order, so
the backfill also re-sorted the lines. Any grade edit has always done this and
`pairing` sorts by `filled_at` regardless, so nothing downstream depends on file
order — but a byte-for-byte diff against an old copy will look larger than the
change was.

---

## 3. HOW THE RUBRIC CHANGES

### 3.1 The mechanism: one base table, per-type overrides

`design/01 §1.9`'s `CREDIT` is the **ripster** table and stays the base. A type
supplies an override map; anything it does not mention it inherits.

```python
TYPE_CREDIT = {
    'ripster': {},                                  # the base table, unchanged
    'scalp':   {'momentum': None, 'chop': None},    # None == N/A, see below
    'swing':   {'curl_512': 1, 'mtf_1h': 2,         # ruled 2026-09-30, §3.3
                'mtf_1d': 3, 'room_ahead': 3},
}
```

Attempted credits by type: **ripster 29, scalp 22, swing 31.**

`None` means **N/A**, not zero — and this is the load-bearing detail.
`design/01 §4` already says an N/A condition is a course not taken: it leaves
the numerator *and* the denominator, and the survivors' shares rise
automatically in exact proportion to their own credit. No redistribution table,
no renormalising, no bespoke scalp arithmetic. The machinery this needed was
built before the requirement existed.

An untyped record grades as `ripster` and is marked in the transcript as having
done so, so the 28 rows he has not typed yet still produce a number.

### 3.2 Scalp: why momentum and chop go N/A

His words, on the same day he weighted them heavily:

> *"If it's a quick exit with low momentum, it's not really counted against the
> trade."*

That is a conditional exemption keyed to holding time, which is execution, and
the GPA is environment-only. Trade type is what makes it expressible without
breaching that rule: the *intent* to be out quickly is known at entry, before
any execution has happened. So it is not "do not penalise him because he got out
fast," it is "a scalp's environment does not include the next two hours."

Arithmetic, with the new weights from §3.3 of this file's sibling ruling
(`momentum` 4, `chop` 3, `TOTAL_CREDITS` 29): a scalp attempts 22 credits
instead of 29, and a scalp that fails nothing else still reads 4.0 where a
Ripster trade in the same tape would read 3.03.

### 3.3 Swing — ruled 2026-09-30

He took the proposal as offered: *"Choice A, your recommendation for now."*
Four rows change, ten are inherited.

| condition | ripster | swing | why |
|---|---|---|---|
| `curl_512` | 3 | **1** | a 6-minute curl does not govern a three-day hold |
| `mtf_1h` | 1 | **2** | the hour starts to matter |
| `mtf_1d` | 1 | **3** | the daily 20/21 cloud is what actually governs it |
| `room_ahead` | 2 | **3** | a multi-day target needs more clear air than an intraday one |
| `momentum` | 4 | 4 | unchanged — a swing genuinely does bleed in a range |
| `chop` | 3 | 3 | unchanged, same reason |

Total 31. **Momentum and chop staying put is the one choice here worth
defending**, because it is the exact opposite of the scalp exemption: a scalp is
out before a flat tape can cost it anything, and a swing sits in that tape for
days. The same two conditions are N/A for one type and heavily weighted for
another, which is the clearest evidence that the rubric needed a strategy axis
at all rather than one table with apologies in it.

**Group weights, per type.** Swing moves 4 credits from A (trigger) into D
(higher timeframe) and B (structure). A falls from 6/29 to 4/31 and D rises from
2/29 to 5/31 — so the group that *governs the next few bars* shrinks by more than
half in relative terms and the group that *governs the next few days* more than
doubles. That is the intended reading of the ruling and it is worth stating
numerically, because the four row edits look small and the shift is not.

**The "for now" in his ruling is load-bearing.** He accepted this table while
saying the rubric will also need to flex by **market condition**, which is a
second axis and a larger design. This table is not the finished answer; it is
the one that unblocks `grading.py` today. Board row `t-cond-vocab` carries the
vocabulary half of that, and the market-condition axis has no row yet because it
has no design.

---

## 4. WHAT THIS COSTS

**Validation gets thinner per type.** `design/01 §8` validates against the hand
graded book; after the backfill those records are all one type. Every claim the
retro-GPA makes is therefore a claim about Ripster trades. The first scalp
graded by hand is n=1, and §8.7's forward test is the only honest validation the
scalp table can have.

**The heatmap's counts fragment.** Slicing 218 decision rows three ways will put
most cells under `sparse_below=3` in the smaller types. Correct behaviour, not a
bug — but the grid will look emptier than expected the first time he splits it.

---

## 5. STATE — 2026-09-30

Built and verified:
- `journal.TRADE_TYPES`, `normalize_trade_type`, `TRADE_TYPE_BLANKET_BEFORE`
- `trade_type` first in `CONDITION_KEYS`; `make_record(trade_type=...)`
- `set_conditions` validates it; `condition_values` offers the closed vocabulary
  whether or not a value has been observed, so the first scalp is not typed
  blind against a datalist that only knows `ripster`
- `journal.backfill_trade_type`, run live: 386 stamped, 28 left for him
- `pairing.py --selftest` and `exit_ladder.py` still pass
- **No frontend change was needed.** The editor renders `condition_keys`.

Ruled 2026-09-30, not yet built because `grading.py` does not exist at all:
- `TYPE_CREDIT` and the per-type override resolution (§3.1)
- The swing table (§3.3) — **now specified**, so nothing in this file is
  waiting on him. `t-grading` has no remaining blocker.

---

## 6. DEPENDENCIES

`design/01 §1.9` (base credit table), `§3` (the arithmetic), `§4` (N/A
neutrality — the mechanism §3.1 rides on), `§8` (validation, now per-type).
Board rows `d-swing-credits`, `t-grading`.
