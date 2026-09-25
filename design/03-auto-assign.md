# 03 — Automatic play assignment

**Component:** `play_assign.py` (new), plus one additive field on the screener row and one
one-word change to an existing guard. Design spec only.
**Status:** the staged lifecycle is the build. The arm path in §7 is a later decision and
is not part of it.

---

## WHAT THIS IS

You paste a morning list. By the first closed bar that can decide anything, each ticker
either holds a **staged play** — a direction, the level it is working off, a trigger, an
invalidation, a target with the clear air behind it — or holds a **written reason why
not**. Nothing reaches the broker. The record is built and waiting for you to press a
button that exists nowhere else in the app.

The assignment is derived, not invented. Direction comes from Ripster's 34/50 hard gate.
The trigger comes from Ripster's 5/12 cross. The anchor comes from the level set
`levels.py` already computes, ordered by a rule you can read top to bottom. The GPA
decides *whether* there is a play, never *which way* it points.

## WHAT THIS DOES NOT DO

- **It does not grade execution.** It reads the environment GPA and stops there.
- **It does not invent a play grammar.** Every assignment it emits is one of two shapes
  already in `plays.BUILTIN_PRESETS` (`pmh_break_curl` is the break shape;
  `curl_vs_pml` / `pdl_hold` are the hold shape). If the assigner ever emits something
  those presets could not express, the assigner is wrong, not the presets.
- **It does not re-weight anything.** `screener.LEVEL_QUALITY` stays the single source of
  what a level kind is worth. No second copy, no assigner-specific knob — two tables that
  mean the same thing will disagree by Tuesday.
- **It does not place orders, compute stops, or watch premiums.** The target field is a
  measurement of room, not an order. See the flag in §3.
- **It does not score relevance.** Relevance is a lexicographic sort, not a weighted sum.
  A weighted relevance score would be exactly what the GPA redesign is removing: one
  universal theory with hidden coefficients and no traceable answer to "why this level?".
- **It does not abstain silently.** A ticker with no play still gets a record, with the
  reason and the transcript. On a bad morning "why nothing" is the whole output.

---

## 0. WHERE IT LIVES, AND WHY NOT IN `assign.py`

`assign.py` already owns the word. It does something different:

| | `assign.py` | `play_assign.py` |
|---|---|---|
| input | a measured half-life in sessions | a graded environment + a level set |
| output | which **engine** trades a name | which **play** a name is working today |
| writes to | `cfg['watchlist'][i]['engine']` | `plays.jsonl` + an assignment sidecar |
| lifetime | durable routing, survives sessions | dated, disposable, dies at the close |
| decides by | arithmetic on one number | a filter and a sort over many |

Two different nouns with two different lifetimes — the same argument `watchlist.py` makes
for not being a basket. Extending `assign.py` would put a per-day disposable state machine
inside a module whose entire docstring is about freezing a durable decision *before* you
screen. **New module, `play_assign.py`** — reads as "assign a play", sits alphabetically
beside `plays.py`, and its docstring opens with "This is not `assign.py`."

Two facts it must respect from `assign.py`:

1. `assign.py` can set `w['mode'] = 'pause'` on a name that lost its clock. A paused ticker
   is one the *engine* may not enter. **`play_assign` still assigns it** — a staged play is
   a human decision and pause blocks automatic entry, which staging is not. But the
   assignment records `engine_mode` in provenance, and §7 refuses anything not `'run'`.
2. If `assign.py` routed a ticker to `ou_reversion_*`, its `execution` is `shares_only`.
   The assigned play's `instrument` follows the resolver, never the assigner (§3).

### Integration point

`play_assign.run(result)` is called inside `screener_service.scan_once()` immediately after
`SC.scan(...)`, wrapped in its own `try/except` and logged to forensics on failure — the
exact precedent `screen_history.record(result)` sets on the line above it.

It does **not** hook into `bar_loop`. `screener_service.py`'s docstring already says why:
the trading loop places orders, and a screener bug must not be able to stall it. The
assigner is on the read-only side of that line and stays there.

It needs no new bar fetches. Everything `propose()` consumes is already on a screener row,
with one exception:

> **Interface requirement on the screener (for the sibling specs).** The row must carry
> the full level set, not just `closest` + `near_levels[:6]`. `score_ticker` already has
> `ls` in hand; it must expose `'level_set': ls` on the returned row. Targets and
> `room_ahead` need levels *beyond* the anchor, which the truncated list does not contain.
> This is the only change to `screener.py` the assigner requires.

> **Interface requirement on `grading.py` (for the sibling spec).** `propose()` consumes
> exactly `{'gpa': int in 0..4, 'transcript': list[row]}`, each row carrying at minimum
> `{'what': str, 'result': True|False|None, 'group': str}`. The assigner reads `gpa` for
> its gates and `transcript` for provenance and for one refusal in §7 (any `result is None`
> on a level or cloud row blocks auto-send). It reads nothing else and never recomputes the
> GPA.

---

## 1. MORNING INTAKE

### Parsing and dedupe: reuse, do not rebuild

`watchlist.parse_rows()` already accepts one ticker per line with optional support,
resistance and a note, split on tabs, pipes or whitespace; already rejects a row whose
first field is not a ticker, with a per-line problem report; already strips thousands
separators so `1,870/1,857` does not become four levels. `watchlist.add_many()` already
**replaces** a same-ticker same-date entry rather than stacking it, because re-typing a row
means correcting it.

So the morning intake is the existing `POST /api/watchlist/rows` box, unchanged, plus one
checkbox — **Auto-assign (on)** — which fires a preview once the import returns. Paste the
same list twice and the watchlist layer has already made it a no-op. The assigner adds no
second parser and no second dedupe here; its own uniqueness is the `(for_date, ticker)`
slot in §5.

### When assignment runs

**A preview on paste. The real assignment on the first scan after 09:30 ET where the
ticker can decide something.**

The owner's wording — "input a bunch of tickers from the start of the day and have moves
automatically set up" — reads as *assign at paste*. It cannot work, and it is worth being
blunt about why:

- Direction comes from the 34/50 gate. `screener._trend_context` needs **51 closed bars**
  of the working timeframe and returns `None` with a reason below that. At 07:00 there are
  none from today.
- PMH and PML are not final until 09:30. `levels.premarket_extremes` reads the
  04:00–09:30 window and returns `None` when it is empty. `premarket` is the
  second-highest kind in `LEVEL_QUALITY` (0.95) and the owner named it first. An assignment
  built at 07:00 anchors on a PMH that has not happened yet.
- So a 07:00 assignment is wrong by 09:35 and every one of them must be superseded at
  once. Churn on the first morning, which is how a tool like this gets abandoned by the
  second Tuesday.

**What you get instead, so the paste still feels like the list is set up:**
`POST /api/assign/preview` runs the full proposal pipeline against whatever data exists
right now, marks every record `state: 'preview'`, and **stores nothing**. At paste time you
see the direction each ticker is currently pointing, the anchor it would use, and for every
ticker that cannot be assigned yet, the named reason. It is the same output the real pass
produces, so there is no surprise at 09:31 — just a record you can act on.

**Assignment gate** — all must hold, per ticker, on a closed bar:

| gate | source | on failure |
|---|---|---|
| `now_et >= 09:30` | wall clock, ET | `not_ready: pre_open` |
| bars exist | `get_bars_multi` returned rows | `not_ready: no_bars` |
| ≥ 51 closed working-TF bars | `screener._trend_context` reason | `not_ready: thin_history` |
| ≥ 15 daily bars | ATR(14) needs 15 | `not_ready: thin_history` |
| `atr is not None` | `level_set['atr']` | **`refused: no_atr`** |
| `trend != 'chop'` | `trend_context` | `abstained: chop` |
| `gpa >= ASSIGN_MIN_GPA` | `grading.py` | `abstained: gpa_below_floor` |

Three deserve a word.

**`no_atr` is a refusal, not a degradation.** Every distance in this system is in ATR —
`levels.py` says so in its own docstring, and it is the only reason SNDK and RGTI are
comparable. Without ATR there is no "near", no `room_ahead`, no relevance ordering and no
target. A dollar-based fallback would be a different system wearing this one's field names.

**No premarket data is not a refusal.** `premarket_extremes` legitimately returns `None`
for a name with no overnight prints, or a feed without extended hours. Assign from the
level set that does exist — prior day, pivots, manual zones — and record
`premarket: 'absent'` in provenance so the transcript shows that the two levels the owner
cares most about were unavailable. **One exception:** if the only anchor surviving §2's
filters is a `psych` level *and* premarket is absent, abstain. A round number on a name
with no overnight auction and no nearby structure is, in `levels.py`'s own words, "a place
the price merely passes through".

**`thin_history` is re-checked every closed bar, not terminal.** A ticker that warms up at
10:40 is assigned at 10:40.

### The attention cap

`screener.py` caps its board at five because "the binding constraint is attention, not
detection". That applies harder to something that builds order plans. But assignment and
staging are different acts:

- **Assignment is measurement and is uncapped.** Every watchlist ticker gets a record.
  Records are cheap; the abstention reasons are the point.
- **Staging is attention and is capped.** `MAX_STAGED = 5`, matching
  `screener.DEFAULT_SLOTS`. The staging queue orders by GPA, then relevance rank, and
  refuses a sixth until one clears. A sixth candidate has to displace one of the five.

> **Flag.** If a 30-ticker morning list produces 30 assignments, the rubric is broken, not
> generous. `assign.py`'s line applies verbatim: *abstention is a first-class answer*. A
> healthy morning is 3–8 assignments out of 30. Build the acceptance test that way: a run
> assigning more than half the list fails.

---

## 2. ASSIGNMENT LOGIC

### 2.1 Direction: the 34/50 gate, and nothing else

```
trend == 'up'    ->  direction = 'long'
trend == 'down'  ->  direction = 'short'
trend == 'chop'  ->  no assignment   (abstained: chop)
```

That is the whole rule. Not the GPA, not the level's side, not the higher timeframes, not a
vote.

`signal_engine.py` calls the 34/50 "a HARD GATE, not a vote", and the git history records
that the previous 3-vote engine's loss profile *was* the gate being outvotable. An assigner
that picked direction any other way would contradict the engine on the same bar, and
`screener.py`'s docstring exists to make that impossible ("the screen and the engine can
never disagree about what the chart says").

> **Flag: the GPA must never pick direction.** A GPA is a scalar quality measure of the
> environment. Using a magnitude to choose a sign is a category error — a 4 means
> "everything decidable is holding", and it means that identically whether the hold is
> bullish or bearish. The GPA's only three jobs here are the assign floor (§1), the hold
> floor (§4), and the auto-send cutoff (§7).

Higher timeframes do not flip direction either. `signal_engine.REQUIRE_1H` is `False` by
design, "to measure the core strategy cleanly". A disagreeing 1H 34/50 therefore enters the
assignment as a **condition on the branch**, where `plays.grade_branch` counts it as
violated and lowers the play's own grade on every scan. Existing machinery, no new veto.

### 2.2 Which cloud governs the trigger: there is no choice to make

The owner asked which cloud is "most relevant". The honest answer is that Ripster already
fixed it and the assigner does not get a vote:

- the **34/50 on the working timeframe** decides direction (hard gate),
- the **5/12 on the working timeframe** is the trigger,
- the **1H 34/50, daily 20/21, daily 50/55** are conditions. They never trigger.

An assigner that chose a cloud per ticker would be selecting at the level where it has the
least data — the disease `assign.py` spends four paragraphs refusing when it declines to
search eleven timeframes. Eleven chances at α=0.05 is a 43% false pass per name; "pick the
most relevant cloud for this ticker" is the same bet in different clothes.

**What the assigner does choose** is whether the 5/12 event is a *cross through a level* or
a *bounce off one*. That is the shape.

### 2.3 Shape: break or hold

Given a direction and an anchor, the anchor's side of price picks the shape. Not a new
idea — it is the difference between two presets that already exist.

**Break shape** — anchor is in front of price (above for a long, below for a short). The
break is the event, so the level is the trigger. This is `pmh_break_curl`.

```
trigger      level   <anchor>    over        (long)  /  under        (short)
conditions   cloud   34/50       above       (long)  /  below        (short)
             cloud   5/12        curls up    (long)  /  curls down   (short)
             mtf     1h 34/50    above       (long)  /  below        (short)
             volume  session     above average        [only if rel_volume decidable]
```

**Hold shape** — anchor is behind price (below for a long, above for a short). Price has
already taken it and is working off it; the bounce is the event, so the 5/12 curl is the
trigger. This is `curl_vs_pml` / `pdl_hold`.

```
trigger      cloud   5/12        curls up    (long)  /  curls down   (short)
conditions   level   <anchor>    over        (long)  /  under        (short)
             cloud   34/50       above       (long)  /  below        (short)
             mtf     1h 34/50    above       (long)  /  below        (short)
             volume  session     above average        [only if rel_volume decidable]
```

Both are built with `plays.make_condition` and `plays.make_branch` from the existing
vocabulary, so they render through `plays.describe_play` and grade through
`plays.grade_branch` with no new code. An anchor whose name is not in `plays.LEVEL_REFS`
(the only case is `psych`) is emitted as `ref='custom'` with the price as `value`, which
`make_condition` already requires and `describe` already renders.

The volume condition is **omitted entirely** when `rel_volume` is `None`, rather than
written and left unknown. `plays.py` treats unknown as no evidence, which is correct, but a
condition that can never resolve is noise on the card.

**One branch, not two.** The presets write both arms because a human writing one at 07:00
does not know which side the day takes. The assigner runs *after* the gate is decidable, so
the opposite arm would be a branch it already knows is dead. And if the gate later flips,
the right answer is a fresh assignment built on the level set as it is *then* — not a
dormant arm built on the level set as it was at 09:31. See §4.

### 2.4 "Most relevant" level: filter, then sort. Never score.

This is the vaguest word in the request, so it gets the most specific answer.

**Timeframe relevance is the organising principle, and it is already a measured quantity.**
Distance in ATR *is* time-to-decision: a level 0.1 ATR away decides in the next few bars,
one at 0.4 ATR decides today, one at 1.2 ATR decides this week. So "what governs the next
few bars outranks what governs the next few days" needs no new metric — it is
`distance_atr`, bucketed, as the first sort key. Higher-timeframe context never *selects*
an anchor; it appears only as a branch condition and, in §4, as a reason to stop.

**Stage 1 — eligibility (hard filters).** Every rejection is recorded with its reason;
nothing is dropped silently.

1. `distance_atr is not None`.
2. `distance_atr <= screener.APPROACH_ATR` (0.50). Beyond half a daily range the level
   does not govern the next few bars, which is the entire criterion.
3. Assert the level is consistent with the gate: for a long, a level below price is a hold
   anchor only while `trend == 'up'`. The gate has already guaranteed this, so the filter
   is an assertion — and it is the line that catches a future change that breaks the
   invariant.
4. `kind != 'psych'` **unless** the psych level is a member of a confluence group of at
   least two distinct kinds (`levels.confluence`, `band_atr=0.15`).
5. `room_ahead(level_set, level, direction) != 0`. A level with nothing between it and the
   next obstacle is not a trade. (`None` means nothing lies beyond at all — open road —
   and passes.)

**Stage 2 — ordering (lexicographic; the first key that differs wins).**

| # | key | direction | why it outranks what follows |
|---|---|---|---|
| 1 | `distance_atr <= screener.NEAR_ATR` (0.20) | True first | Timeframe relevance. The nearer level decides the next few bars. The owner's principle, made the top key. |
| 2 | member of a confluence group of ≥ 2 distinct kinds | True first | `levels.py`: "three levels inside a fifth of an ATR is a shelf, and a shelf is where these plays tend to resolve." A shelf is more places watched than one level. |
| 3 | `screener.LEVEL_QUALITY[kind]` | descending | Reused table, not a new one. Where people are actually watching. |
| 4 | `room_ahead` | descending; `None` = open road = max | A break into a shelf 0.2 ATR later is a different trade from one with nothing in front of it. Nothing else here measures that. |
| 5 | level `name`, lexical | ascending | Determinism. Required by §5 — a random tie-break makes the idempotency key unstable and the assigner churns forever. |

**Anchor price.** When the winner sits in a confluence group, the anchor price is the group
edge nearest price (`low` for a long break from below, `high` for a short break from
above), not the group mid — you trade the edge you touch first. The group is recorded on
the anchor so the transcript shows what the shelf was made of.

**Why a sort and not a weighted score.** A weighted relevance score is the seven-component
0–1 score again, in a new place, with new hidden coefficients, answering "why PMH?" with a
decimal. This sort answers it in a sentence: *"PMH won because it is inside 0.20 ATR and
PDH is not."* When the owner disagrees with a choice he can point at the key that decided
it. Same property the GPA transcript is buying.

> **Flag: R1/S1 and psych rank low, and the owner says they matter.** He named premarket
> high/low, R1/S1 and psych as the important levels. `LEVEL_QUALITY` puts `floor_pivot` at
> 0.60 and `psych` at 0.40, both below `prior_day` (0.85). I would **not** re-rank the
> table. Note instead that both earn their lift at **key 2, before quality is ever
> consulted**: a psych number sitting on R1 sitting on PDH is a shelf, and the shelf beats
> a lone PDH outright. That answers the owner's intuition with a measurement he can see
> rather than a coefficient he has to trust. If, after a month of transcripts, R1 keeps
> winning on merit and losing on quality, change `LEVEL_QUALITY` once, in `screener.py`,
> where the board and the assigner both read it.

---

## 3. THE ASSIGNED-PLAY RECORD

### Two stores, and why

The **play** goes into `plays.jsonl` through the public `plays.add(...)`, in exactly the
shape `plays.make_play` already produces. Nothing about `plays.py` changes, so
`screener_service`'s `PL.by_ticker(for_date=today)` picks assigned plays up for free and
`screener.score_ticker` grades them on every scan with no new code anywhere.

The **assignment envelope** goes into a sidecar, `DATA_DIR/assigned_plays.jsonl`, keyed by
`play_id`. Three reasons, in order of weight:

1. **The envelope changes state many times a day; the play does not.** `plays.py` mutates
   through `_rewrite()`, a whole-file rewrite. Putting a state machine in that store means
   rewriting every play on the disk on every transition — the one operation that can lose
   data, performed dozens of times a session.
2. **`plays.py` deliberately contains no prices, no instrument and no size.** A play is a
   bias, a trigger and conditions. Adding a target, a strike sleeve and a dollar budget
   would change what a play *means* in a module whose docstring is about the opposite.
3. **`plays.update()` only patches `EDITABLE` keys and `plays.carry_forward()` rebuilds
   records through `make_play`.** Extra keys stuffed onto a play record survive a read and
   are silently dropped by either of those. A sidecar cannot be lost that way.

The envelope makes exactly **one** write into `plays.jsonl`, through the public API:
`plays.update(play_id, active=False)` when the assignment reaches a terminal state.
`active` is already in `EDITABLE`, and `by_ticker`'s default `active_only=True` then stops
returning dead plays. No `plays.py` change; see §6.

### Schema

```python
{
  # identity ------------------------------------------------------------------
  'id':            str,        # 'ap_<12 hex>'
  'key':           str,        # 16-hex content identity; see §5
  'play_id':       str | None, # the plays.jsonl record; None when abstained
  'ticker':        str,        # upper
  'for_date':      str,        # 'YYYY-MM-DD', the ET SESSION date; see §6
  'state':         str,        # see §4
  'order':         int,        # mirrors the play's order; supersessions increment

  # the read ------------------------------------------------------------------
  'direction':     str,        # 'long' | 'short'   -- from the 34/50 gate only
  'shape':         str,        # 'break' | 'hold'
  'anchor': {
      'name':          str,    # 'PMH', 'R1', 'psych', 'manual_support', ...
      'kind':          str,    # levels.py kind: premarket | prior_day | floor_pivot | ...
      'price':         float,
      'side':          str,    # 'above' | 'below' | 'at', relative to price
      'distance_atr':  float,
      'quality':       float,  # screener.LEVEL_QUALITY[kind], copied for the transcript
      'shelf':         dict | None,   # levels.confluence group, verbatim
      'rank_keys':     list,   # the five §2.4 sort keys, as evaluated. The audit trail.
      'rejected':      list,   # [{'name','kind','why'}] every level that lost, and why
  },

  # the play, in plays.py vocabulary ------------------------------------------
  'trigger':       dict,       # a plays.make_condition dict
  'trigger_text':  str,        # plays.describe(trigger)
  'conditions':    list[dict], # plays.make_condition dicts
  'invalidation': {
      'conditions': list[dict],  # ANY true -> invalidated
      'text':       str,
      'basis':      str,         # 'structural_34_50' | 'structural_34_50+anchor_lost'
      'price':      float | None,# anchor price for a hold; None when purely cloud-based
  },
  'targets': [                 # ordered, at most two. NOT orders. See the flag below.
      {'price':      float,
       'basis':      str,      # 'next_level' | 'open_road'
       'level_name': str | None,
       'room_atr':   float | None,   # screener.room_ahead(level_set, anchor, direction)
       'r_multiple': float | None},  # room_atr / risk_atr; arithmetic on two measurements
  ],
  'risk_atr':      float | None,  # |anchor - invalidation.price| / atr, hold shape only

  # how it would be expressed --------------------------------------------------
  'instrument': {
      'execution':       str,   # 'options_combo' | 'shares_only'  <- from resolver, not us
      'options_allowed': bool,  # signal_engine.launch_gate(...)['volume_ok']
      'sleeve':          str,   # 'calls+shares' | 'puts_only' | 'shares' | 'blocked'
      'why':             str,   # the volume gate's own reason string, verbatim
  },
  'size_basis': {
      'method':      'set_budget',
      'source':      'app._budget_for(ticker, cfg) at SEND time',
      'preview_dollars': float | None,   # what it would be right now, for display only
      'frozen':      False,              # always. See the flag below.
  },

  # the grade at assignment time ------------------------------------------------
  'gpa':            int,       # 0..4, integer increments
  'gpa_transcript': list,      # grading.py's transcript, verbatim, frozen at assignment
  'gpa_at':         str,       # iso seconds
  'gpa_now':        int,       # updated every pass; may drift from 'gpa'
  'gpa_transcript_now': list,  # replaced every pass
  'drift':          list,      # [{'at','was','now','changed':[...]}]; see §4

  # the measured state at assignment time ---------------------------------------
  'price_at_assign': float,
  'atr':             float,
  'trend':           str,      # 'up' | 'down'
  'mtf_1h':          str | None,
  'mtf_1d_2021':     str | None,
  'mtf_1d_5055':     str | None,
  'rel_volume':      float | None,
  'journal_conditions': dict,  # screener.journal_conditions(...), carried to the fill

  # the human's words, untouched ------------------------------------------------
  'note':            str,      # the watchlist note, VERBATIM. Never parsed.
  'label':           str,      # 'Auto: long over PMH'
  'why':             str,      # one sentence: the winning sort key, in English

  # provenance -------------------------------------------------------------------
  'provenance': {
      'assigner_version': str,   # this module's constant, bumped by hand on rule changes
      'rubric_version':   str,   # grading.py's constant
      'scan_at':          str,   # iso seconds, ET naive
      'bar_time':         str,   # the CLOSED bar this was decided on
      'intra_tf':         str,   # '6Min'
      'n_bars':           {'intra': int, 'daily': int, 'hourly': int},
      'watchlist_entry_id': str,
      'engine':           str | None,  # resolver.resolve(ticker).name
      'engine_mode':      str,         # 'run' | 'pause' from the config watchlist
      'premarket':        str,         # 'present' | 'absent'
      'inputs_hash':      str,         # see §5
  },

  # lifecycle -----------------------------------------------------------------
  'history':        list,      # [{'at','from','to','why'}] every transition, append-only
  'supersedes':     str | None,
  'superseded_by':  str | None,
  'staged':         dict | None,  # {'at', 'plan': trade_router.plan_entry output,
                                  #  'plan_hash': str, 'budget_dollars': float}
  'sent':           dict | None,  # {'at','order_ids':[...],'by':'human'|'armed'}
  'fills':          list,      # journal record ids
  'reason':         str,       # populated on abstained / refused / not_ready
  'last_seen_at':   str,       # touched every pass the proposal is unchanged
}
```

### Three deliberate choices in that schema

> **Flag: `targets` must never become an order. This is the most dangerous field here.**
> `trade_router.py`'s docstring is unambiguous: *"There are NO price stops, trails,
> ratchets, premium monitors, or MACD-collapse exits in this file anymore."* The Ripster
> rebuild deleted the price brain on purpose, and the commit log says those gaps *were the
> loss profile*. A `target.price` sitting in a trade record is one small refactor away from
> a bracket order or a take-profit, and that refactor would quietly reinstate the thing that
> was removed. So: **the target is the room measurement that justified the play**, present
> so the owner can judge whether the trade is worth taking, and the exit remains the
> engine's 5/12 close and 34/50 break. The field name should carry the warning — I would
> name it `room_target` in the UI and put the sentence "not an order; exits are
> engine-driven" beside it on the card.

> **Flag: size is never frozen at assignment time.** `_budget_for` is pinned to a day-start
> cash snapshot (commit 662657b) and is the same base the dashboard and the Set cap use.
> Copying dollars into the assignment at 09:31 means a play staged at 14:00 sizes off a
> number that three other things have since disagreed with. `preview_dollars` is for
> display; the real budget is read at send time, from the one place that owns it.

> **Instrument follows the resolver and the volume gate, never the assigner.**
> `trade_router.plan_entry` already encodes the rules: `shares_only` for OU engines because
> "a reversion engine filled with calls is a reversion engine that loses"; long =
> calls + shares; **short = puts only, and a short with no qualifying put is no trade**.
> And `signal_engine`'s volume gate restricts OPTIONS only — low volume means shares for
> longs and **shorts blocked outright**, since shares are long-only. So a short assigned
> into a low-volume tape records `sleeve: 'blocked'` with the gate's own reason string and
> **cannot be staged**. That is not the assigner adding a rule; it is the assigner refusing
> to build a plan the router would reject anyway, early enough that the owner sees why.

---

## 4. LIFECYCLE

### States

| state | terminal | what it means |
|---|---|---|
| `preview` | — | never stored. The dry-run output at paste time. |
| `not_ready` | no | data is missing. Re-checked every closed bar. |
| `refused` | yes for the day | no ATR. Cannot be assigned in this system. |
| `abstained` | no | chop, or GPA below floor. Re-checked every closed bar. |
| `assigned` | no | **the resting state.** Play written, waiting on its trigger. |
| `triggered` | no | the trigger resolved True on a closed bar. Still nothing sent. |
| `invalidated` | yes | an invalidation condition resolved True. |
| `expired` | yes | the session ended, or the trigger fired past the cutoff. |
| `superseded` | yes | replaced. `superseded_by` names the successor. |
| `cancelled` | yes | the owner dropped it. |
| `staged_for_send` | no | **human action.** Order plan built and shown. Not sent. |
| `sent` | no | legs handed to `trade_router.execute_plan`. |
| `rejected` | no | every leg failed at the broker. |
| `filled` | no | at least one leg confirmed by `trade_router.confirm_fill`. |
| `journaled` | yes | journal records written, conditions pre-filled. |

### Legal transitions — the whole table

```
not_ready   -> assigned | abstained | refused | not_ready | expired
abstained   -> assigned | abstained | expired
refused     -> expired
assigned    -> triggered | invalidated | superseded | expired | cancelled
               | staged_for_send                       [HUMAN]
triggered   -> invalidated | expired | cancelled
               | staged_for_send                       [HUMAN]
staged_for_send -> sent                                [HUMAN, + plan_hash match]
               | assigned | triggered                  [HUMAN: un-stage, back to whence]
               | invalidated | expired | cancelled
sent        -> filled | rejected
rejected    -> staged_for_send                         [HUMAN]  | cancelled | expired
filled      -> journaled
journaled   -> (terminal)
invalidated | expired | superseded | cancelled -> (terminal)
```

Everything else is illegal and `advance()` raises rather than coercing. In particular:

- **There is no edge from `triggered` to `sent`.** The only path to the broker runs through
  `staged_for_send`, and the only way into `staged_for_send` is a human POST. This is the
  safety default, expressed as a graph property rather than as a flag somebody can set.
- **`staged_for_send -> sent` additionally requires the `plan_hash` the owner was shown.**
  A minute after staging, the strike, the mid and the budget may all have moved. Sending a
  plan the owner did not see is the failure mode a confirmation dialog is supposed to
  prevent and usually does not. Mismatch re-plans and re-shows; it never sends.
- `invalidated`, `expired`, `superseded` and `cancelled` all call
  `plays.update(play_id, active=False)` on the way in (§6).

### Trigger and invalidation evaluation

Both are `plays.make_condition` dicts, so both are evaluated by
`plays.evaluate_condition(cond, state)` against the state dict `screener.build_state()`
already builds every scan. No second evaluator, and therefore no way for the assignment and
the play card to disagree about whether the trigger fired.

`advance()` acts on `True` only. `None` (unknown) never advances anything and never
invalidates — `plays.py`'s rule that missing data is not evidence holds here too.

Evaluation happens **once per closed working-TF bar per ticker**, tracked exactly as
`app._last_bar_seen` does it. The scan runs every 60s but decides on closed 6-minute
candles; without the bar guard a "fresh cross" can appear and vanish inside one candle and
the state machine would ratchet on noise.

`LATE_TRIGGER_CUTOFF = 15:30 ET`. An `assigned` play whose trigger has not fired by then
goes `expired`: with `DTE_MIN = 4`, an entry after 15:30 is an overnight decision, not the
day trade the play describes. A play already `triggered` or `staged_for_send` stays alive
until the close.

### When the GPA changes after assignment — the recommendation

**Leave the play. Flag drift. Supersede only when the assignment's own structure breaks.**
Three rules, in order:

**1. GPA moves, structure intact → record drift, change nothing.**
Direction, anchor and trigger unchanged: append a drift event, update `gpa_now` and
`gpa_transcript_now`, leave the play alone.

Why: a play is a statement about what this stock is supposed to do today. `plays.py` already
re-grades it against live conditions on every scan — the environment getting worse *is*
what the play's own conditions measure, and `grade_play` will show it as a 2 instead of a 4
without anyone rewriting anything. Rewriting the play on a GPA tick destroys the single
most valuable property it has: that it was written *before* the move, and you can check it
against what happened. A record that edits itself to stay right is not evidence.

**2. The environment that produced the direction is gone → `invalidated`, do not reassign.**
Any of: `gpa_now < HOLD_MIN_GPA` (1), or `trend` flips, or `trend` becomes `chop`. Ripster's
own rule is that inside the 34/50 there are no entries; a gate flip means the hard gate that
chose the direction now says the opposite. The correct response is to stop, not to rewrite
the play into the other direction — a long that becomes a short is not a revision, it is a
different trade, and calling it a revision hides that the first read was wrong.

**3. The anchor stops being the relevant level → `superseded`.**
Either: price closes beyond the anchor by more than `ANCHOR_STALE_ATR` (1.0 ATR) — the
level is taken and behind you, the play is about something that already happened — or a
different level wins §2.4's sort **on two consecutive closed bars**. The two-bar
requirement is the churn guard: a single bar's winner can be a level price is oscillating
across. A supersession writes a new assignment (new play, `order` incremented), marks the
old `superseded`, and links both ways so the day's history reads as a chain.

**Cap: `MAX_SUPERSESSIONS_PER_DAY = 3` per ticker.** On the fourth, stop assigning that
ticker for the session and say so in the record: `abstained: unstable`. A ticker that needs
a fourth plan is a ticker whose environment is not gradeable, and a tool that rewrites the
plan five times does not have a plan. This is the same judgement `screener.py` makes when
it caps the board: a system that always produces an answer is not answering.

---

## 5. RE-ASSIGNMENT CADENCE AND IDEMPOTENCY

The scan is every 60 seconds. Three mechanisms, each doing one job.

### 5.1 The slot: one live assignment per ticker per day

**Hard invariant: at most one non-terminal assignment per `(for_date, ticker)`.** Enforced
on write by reading the sidecar for that date and ticker first. A second one is a bug, not
a feature; two live plans on one ticker is how you end up holding both sides.

Terminal records accumulate freely — the day's chain of supersessions is history and is
kept.

### 5.2 The content key: did the proposal actually change?

```python
key = sha1('|'.join([
    for_date,
    ticker,
    direction,                       # 'long' | 'short'
    shape,                           # 'break' | 'hold'
    anchor_name,                     # 'PMH'
    f'{anchor_price:.4f}',           # rounded before hashing
    f"{trigger['kind']}:{trigger['ref']}:{trigger['op']}",
])).hexdigest()[:16]
```

Every pass, `propose()` builds a proposal and computes its key.

- **key matches the live assignment** → touch `last_seen_at`, refresh `gpa_now` /
  `gpa_transcript_now`, append a drift event if the GPA moved. **No new record. No write to
  `plays.jsonl`.** This is the overwhelmingly common case, and it must cost one line in a
  sidecar, not a play.
- **key differs** → §4 rule 3 decides. Two consecutive closed bars agreeing on the new key
  before anything is superseded.
- **no live assignment for the slot** → write, if §1's gate passes.

**What is deliberately not in the key:**

- **The GPA.** It moves on almost every bar. Keying on it guarantees a new play per pass,
  which is precisely the churn this section exists to prevent.
- **Price, `distance_atr`, `room_ahead`, `rel_volume`.** All continuous, all moving. They
  are recorded on the assignment and they feed the sort, but they do not define identity.
- **The condition list.** Conditions are derived deterministically from direction + shape,
  so they cannot differ when the key components match — except for the volume condition,
  which appears or vanishes with `rel_volume` availability. That must not count as a new
  play, so it is out of the key by construction.

Anchor price is rounded to 4dp before hashing because `levels.build` already rounds to 4dp
and floating noise below that would churn the key with no change on the chart.

### 5.3 The bar guard: assignment is a closed-bar act

`propose()` runs on every scan (it is pure and cheap), but a **write** happens at most once
per closed working-TF bar per ticker, tracked exactly as `app._last_bar_seen` does it. This
is the same two-clocks design `screener_service.py` already documents: poll at 60s so price
and proximity stay current, decide on candles that have actually closed.

`inputs_hash` in provenance is a hash of everything `propose()` read — the level set, the
cloud state, the MTF reads, the GPA, the rubric and assigner versions. It exists so that
"why did this change?" is answerable after the fact, and so a replay can prove the assigner
is deterministic. `propose()` is a **pure function** of `(watchlist_entry, screener_row,
gpa)` with no clock and no I/O, testable without Alpaca — the same reason
`screener.scan()` takes an injected `bar_getter`.

---

## 6. END OF DAY, AND THE NEXT MORNING

### Unfilled assignments expire. Lazily.

At the close, every non-terminal assignment becomes `expired` — including
`staged_for_send`, whose order plan is discarded rather than carried, because the strike and
the mid it was built from are stale by morning.

**Expiry is lazy, not timed.** Any read of an assignment whose `for_date` is earlier than
today's ET session date and whose state is non-terminal reports it as `expired` and writes
that transition once, on first touch. A 16:00 timer would miss every evening the app is
closed, and a missed sweep would leave yesterday's plays `assigned` — which
`by_ticker(for_date=...)` would not surface, but the sidecar would, and a stale
`staged_for_send` sitting in a queue is exactly the kind of thing that eventually gets
clicked.

### `by_ticker(for_date=...)` stays correct because of three things

1. **`for_date` is the ET session date, not `date.today()`.**

   > **Flag: a real bug in the surrounding code, which this component must not inherit.**
   > `watchlist.make_entry` and `plays.make_play` both default `for_date` to
   > `date.today().isoformat()` — the *machine's* local date. Every other dated thing in
   > this system is Eastern: `journal.py` normalizes every `filled_at` to naive ET and says
   > why ("one UTC timestamp among Eastern ones puts a sell four hours adrift");
   > `screen_history.py` stores naive ET; `levels.py` converts to ET before reading the
   > premarket window. On a machine set to UTC, `date.today()` rolls over at 20:00 ET and
   > every play written in the last four hours of a session lands on *tomorrow's* date,
   > where `by_ticker(for_date=today)` will never find it. `play_assign.session_date()`
   > must compute `datetime.now(ET).date()` and pass `for_date` **explicitly** to
   > `plays.add()`. I would not change `plays.py`'s default in this pass — but it should be
   > on the list, and every assigner call site must pass the date rather than rely on it.

2. **Terminal assignments deactivate their play.** `plays.update(play_id, active=False)`,
   through the public API, on `invalidated` / `expired` / `superseded` / `cancelled`.
   `by_ticker`'s default `active_only=True` then stops returning it, the screener stops
   grading it, and `rank()` stops letting a dead play outrank a live read. Without this,
   a superseded play keeps competing with its own replacement for the board slot.

3. **`order` increments across a supersession.** `plays.grade_ticker` sorts live plays by
   grade then `order`, and `grade_play` reads branches as if/else in the order written. The
   successor taking the next order number keeps that ordering meaningful instead of
   accidental.

### Nothing carries forward automatically

`plays.carry_forward()` exists and stays a human action. Auto-carrying an assigned play
means trading yesterday's level set — yesterday's PMH, yesterday's pivots, a 34/50 gate
read at yesterday's close. Every anchor in the record would be stale in a way the record
does not advertise.

**The unit that should carry is the watchlist, and it already does.**
`watchlist.carry_forward()` copies the tickers, the typed zones and the notes — the parts
that are genuinely durable, and the parts whose docstring already says re-typing forty
tickers every morning is "the main reason a tool like this stops getting used". Carry the
list; re-assign from today's bars. The morning ritual is: carry the watchlist, and the
09:30 pass builds today's plays.

The daily ledger of what happened to each assignment — how many assigned, how many
triggered, how many invalidated and how — is the material that eventually says whether the
rubric is any good. Keep the terminal records; never clean them up automatically.

---

## 7. THE ARM PATH — FENCED OFF

**This is not part of the first build.** It is written down here so that the staged
lifecycle is built in a shape that can accept it later, and so the decision, when it is
taken, is taken against a list rather than in a moment of enthusiasm.

One thing is worth saying plainly before the list. The staged path's value is not
convenience, it is that a human looks at each play once before money moves. Arming removes
the only review step in the system. It should be turned on, if ever, after a month of
staged records show that the owner pressed Send on nearly everything the assigner staged at
GPA 4 — that is, after the data says the review step was not doing any work. Turning it on
first and checking afterwards inverts the only evidence that matters.

### What it reuses (all of it already exists; invent nothing)

| guard | where | what it does for us |
|---|---|---|
| `_engine_enabled` master switch | `app.py:154`, OFF at every start | no auto-send while OBSERVE ONLY |
| type `LIVE` to go live | Setup tab / `setup_routes` | a stray click cannot reach a live account |
| `_force_engine_off()` on any credential or account change | `app.py:902` | an armed assignment cannot inherit an account it was not armed against |
| per-ticker `mode == 'run'` | config watchlist, checked in `_evaluate_ticker` | a paused ticker never auto-enters |
| pyramiding guard | `_do_entry`, `source == 'engine' and ticker in _positions()` | never adds to a held ticker |
| Set-budget ceiling | `_do_entry`, `_budget_for` | belt-and-suspenders on position growth |
| no-rebuy flags | `trade_router.mark_stop_out` / `rebuy_blocked` | a structural stop blocks the same-day rebuy |
| `plan_entry` sleeve rules | `trade_router` | shorts are puts-only; no put, no trade |
| explicit `buy_to_open` / `sell_to_close` intent | `alpaca_manager` (commit 1268104) | no naked short options, ever |
| `execute_plan` + `confirm_fill` | `trade_router` | broker truth, not planning estimates |

> **Flag: one existing line must change, and it must change before the arm path is built,
> not after.** `_do_entry`'s pyramiding guard fires on `source == 'engine'`. `manual` is
> deliberately exempt ("manual adds are deliberate and remain allowed"). An auto-sent
> assigned play is neither: it is unattended, so it must be guarded, but it is not the
> engine. It must enter as `source='assigned'` and the guard must read
> `source in ('engine', 'assigned')`. If that word is not changed first, the day the arm
> path ships is the day auto-send can pyramid a held ticker — and commit 8593419 records
> that this guard has already been lost once.

### What it additionally requires (three things, and nothing else)

1. **A per-assignment `armed: bool`, default `False`**, settable only by a POST naming the
   assignment `id`, the GPA cutoff in force, the `plan_hash`, and a typed confirmation
   string `ARM <TICKER>`. Arming one play never arms another.
2. **A process-global `AUTO_SEND_ENABLED`, `False` at every start, never persisted to
   `config.json`.** Arming does not survive a restart. `_engine_enabled` sets exactly this
   precedent and the reason is the same: a flag that survives a crash is a flag nobody
   remembers setting.
3. **`MAX_AUTO_SENDS_PER_DAY`**, session-scoped, small (start at 2). A runaway is bounded
   by arithmetic, not by attention.

### The cutoff

`ARM_MIN_GPA = 4`, measured **on the same closed bar as the trigger**, not on a stale
reading.

Not 3. A 4 means everything decidable is holding — the population where discretion adds
least. A 3 is "mostly holding", which is exactly the population where looking at the chart
pays, so automating it buys the least and costs the most. If the cutoff has to be argued
down to 3 to make the feature useful, the feature is not useful.

### What it must refuse, unconditionally

- `trend == 'chop'`, or any gate flip since the trigger bar.
- **Any `result is None` in the GPA transcript on a level or cloud row.** Unknown is not
  evidence — and an unattended send on missing data is the worst failure this system can
  have. The staged path may proceed on unknowns because a human is looking; the armed path
  may not.
- Any drift event recorded since the trigger bar.
- Outside 09:30–15:30 ET.
- An anchor whose only support is a `psych` level.
- A short whose `instrument.sleeve` is `'blocked'` (the volume gate; `plan_entry` would
  refuse anyway, but it must refuse here first and say so).
- More than once per ticker per day.
- While `_engine_enabled` is `False`, or `mode != 'run'`, or the account or credentials
  changed since arming.
- Re-arming itself after a `rejected`. A broker rejection returns the assignment to the
  human; the arm flag clears.
- Placing its own broker call, computing its own budget, or setting its own stop or target.
  It calls `_do_entry(ticker, direction, cfg, source='assigned', allow_options=...)` and
  nothing else. If the arm path ever needs a code path the staged path does not have, that
  is the signal to stop building it.

---

## APPENDIX — CONSTANTS

All hardcoded in `play_assign.py`, edit-and-commit to change, matching
`trade_router.py`'s "HARDCODED CONSTANTS BY DESIGN". None of these is a tuning knob in the
UI; a slider here would make the assigner un-replayable.

```python
ASSIGN_MIN_GPA          = 2      # stage nothing below the midpoint of plays.py's scale
HOLD_MIN_GPA            = 1      # below this, invalidate: the environment is gone
ARM_MIN_GPA             = 4      # §7 only, and §7 is not built
ANCHOR_STALE_ATR        = 1.0    # anchor is behind you; the play is about history
SUPERSEDE_BARS          = 2      # consecutive closed bars agreeing before a supersession
MAX_SUPERSESSIONS_PER_DAY = 3    # a fourth plan is not a plan
MAX_STAGED              = 5      # == screener.DEFAULT_SLOTS; attention, not detection
LATE_TRIGGER_CUTOFF     = 15:30  # ET; DTE_MIN=4 makes a later entry an overnight call
ASSIGNER_VERSION        = 'pa-1' # bumped by hand whenever a rule in §2 changes
```

Borrowed, never copied: `screener.NEAR_ATR` (0.20), `screener.APPROACH_ATR` (0.50),
`screener.LEVEL_QUALITY`, `screener.ROOM_FULL_ATR` (1.0), `levels.confluence` band (0.15),
`plays.NEAR_ATR`. If one of those needs to be different for assignment than for the board,
the board is wrong.

## APPENDIX — MODULE SURFACE

```python
session_date()                            -> 'YYYY-MM-DD'  (ET, always)
propose(entry, row, gpa)                  -> proposal | abstention    # PURE. No I/O.
key_for(proposal)                         -> str
build_play(proposal)                      -> branches for plays.make_play  # PURE
assign(proposal, dry_run=False)           -> record            # writes both stores
advance(record, row, state_dict, now_et)   -> record            # PURE state machine
run(scan_result)                          -> {'assigned': n, 'drifted': n, ...}
read_all(for_date=None, ticker=None)      -> list
by_ticker(for_date=None)                  -> {ticker: [record]}
live_for(ticker, for_date)                -> record | None      # the slot; §5.1
expire_stale(now_et=None)                 -> n                  # lazy; §6
stage(assignment_id, alpaca, cfg)         -> record             # HUMAN entry point
send(assignment_id, plan_hash, alpaca, cfg) -> record           # HUMAN entry point
```

`propose`, `build_play` and `advance` are pure and take no clock they do not receive, so the
whole of §2 and §4 is testable with fixtures and no Alpaca. `stage` and `send` are the only
functions that touch the broker, they are the only ones a route may call on a POST, and
neither is ever called from `run()`.
