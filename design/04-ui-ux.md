# 04 — The screener UI and its HTTP surface

Design spec. Nothing in this document has been implemented. It touches
`static/index.html` and `screener_routes.py` only; the rubric itself
(`grading.py`), the measurement layer and the play assigner are sibling specs
and this one depends on their seams rather than defining them.

---

## 0. What this component is for, and what it refuses to do

The board answers one question and it is not "should I take this". It is
**"how good is the environment on this name right now, and why"**. The GPA is
the answer; the transcript is the *why*; the two are never separated, because
a 0-4 number with no visible parts is the same failure the weighted score
had — an opinion with a decimal point — only now with fewer decimal points to
hide behind.

Three things this UI deliberately does not do:

- **It does not grade execution.** No fill price, no P/L, no "was this a good
  trade". The journal already does that and does it better, at the only time
  it can be done honestly, which is afterwards.
- **It does not fold the journal edge into the grade.** The edge sits *beside*
  the GPA as a sanity check. A 3.0 environment that historically traded at
  -$40 over 23 trades is the single most useful thing this screen can show a
  person, and averaging the two into a 2.4 destroys it.
- **It does not get wider because the grading got better.** Five slots. The
  GPA will make it tempting to show more, because now there is a defensible
  number next to row six. The binding constraint is still attention.

---

## 1. Inventory: everything the weighted score owns, and what happens to it

### 1.1 `static/index.html` — markup

| Line(s) | What it is | Fate |
|---|---|---|
| 106–110 | CSS `.wRow` — the weight-slider row rhythm | **Gone.** The transcript has its own row class (`.txRow`, §3.1); reusing `.wRow` would leave a name that lies. |
| 111 | CSS `.wBox` — the whole-percent number input | **Gone.** Nothing in the new UI takes a numeric weight. |
| 622 | `<button onclick="scrWeightsOpen()">Weights</button>` | **Replaced** by `<button onclick="scrRubOpen()">Rubric</button>`. Same slot, same size. The owner still needs a place to read what the grader is doing; they no longer need a place to argue with it by typing. |
| 684–720 | The weights modal `#scrWModal` in full: header, the 100-total explainer, `#scrWeights`, `#scrWTotal`, `#scrWHint`, Save / Even split, `#scrWMsg`, and the "Calibrate from journal" footer `#scrCal` | **Replaced** by `#scrRubModal` (§3.4) — read-only, same geometry (`min(560px,94vw)`), same backdrop idiom. |

### 1.2 `static/index.html` — JavaScript

| Line(s) | Symbol | Fate |
|---|---|---|
| 3016 | `const bar = scrScoreBar(x);` and its conditional render at 3044 (`${x.best_play ? '' : bar}`) | **Replaced** by `scrCreditMeter(x)`, which always renders — the GPA's denominator is not a fallback for tickers without a play, it is the primary read. |
| 3025–3027 | The score fallback `(x.score*100).toFixed(0)` inside `scrRow` | **Replaced** by the GPA chip (§2.2). |
| 3065–3068 | `SCR_COMP_LABEL` | **Gone from JS.** Condition labels now arrive from `GET /api/screen/rubric`. Same argument as `/api/plays/vocabulary`: a condition added in `grading.py` must appear on screen without a second edit in a 246 KB HTML file. |
| 3069–3074 | `SCR_COMP_COLOR` | **Gone.** Seven component colours were needed for a stacked bar. The transcript is grouped by section, and five sections do not need five hues — pass/fail/partial/n-a carry all the colour the panel needs. |
| 3076–3086 | `scrScoreBar()` | **Gone**, superseded by `scrCreditMeter()`. |
| 3088–3121 | `SCR_COMP_HELP` — seven prose blocks explaining each component | **Moves server-side**, one `why` string per rubric row (§6.1). This is the largest single deletion and the one most likely to be mourned; the prose is good and must be carried across into `grading.py`, not thrown away. |
| 3123 | `let SCRW = {weights:null, defaults:null}` | **Gone.** |
| 3125–3135 | `scrWeightsOpen()` | **Replaced** by `scrRubOpen()`. |
| 3137–3159 | `scrCalLoad()` | **Replaced** by `scrRubResults()` — same slot in the modal footer, different question (§6.6). |
| 3161–3166 | `scrCalApply()` | **Gone.** There is nothing to apply. |
| 3167–3168 | `scrWClose()`, `scrWBackdrop()` | **Replaced** by `scrRubClose()`, `scrRubBackdrop()`. |
| 3173–3183 | `scrWToWhole()` | **Gone.** |
| 3185–3200 | `scrWRender()` | **Replaced** by `scrRubRender()` — same loop, renders a static credit table instead of inputs. |
| 3202–3210 | `scrWValues()` | **Gone.** |
| 3211–3224 | `scrWTotal()` | **Gone.** |
| 3226–3242 | `scrWSave()` | **Gone.** |
| 3244–3250 | `scrWEven()` | **Gone.** |
| 3252–3260 | `scrWReset()` | **Gone.** |

Net: about 190 lines of JS out, roughly 120 back in, and the 120 are mostly
rendering a payload rather than validating an input. That asymmetry is the
point — the weight UI was almost entirely input validation for a number
nobody could justify.

### 1.3 `screener_routes.py`

| Line(s) | Endpoint | Fate |
|---|---|---|
| 364–415 | `GET,POST /api/screen/weights` | **Removed outright**, not soft-deprecated. The app is served from `static/index.html` by the same process; there is no cached client to keep alive. Fifty lines of the sixty here are the whole-numbers-total-100 rule, which exists only because a slider existed. |
| 417–423 | `GET /api/screen/calibrate` | **Orphaned — say it out loud.** `screen_history.calibrate()` fits *weights* from component-vs-P/L correlations. With no weights there is nothing to fit, and the endpoint would return a suggestion that cannot be applied. See §1.5 for what replaces it and what must be kept. |

### 1.4 `screener_service.py` and `screener.py`

Not this component's files to change, but the UI's contract depends on it, so
naming it here so the rubric agent and I do not both assume the other did it:

- `screener_service.py:104–109` `_load_weights()`, `:117` `_settings['weights']`,
  `:121–137` `set_weights()`, `:203` the `w = dict(...)` read in `scan_once` —
  all dead once `/api/screen/weights` is gone. `settings()` keeps `slots`,
  `min_state`, `enabled`, `alert_on`; the UI reads exactly those four and will
  break if `weights` is still present but stale.
- `config.json` retains a `screener_weights` key nobody reads. Harmless, but
  it will confuse the next person who greps for it. One line in the load path
  to `pop` it is cheaper than the confusion.
- `screener.py:81–91` `DEFAULT_WEIGHTS` / `COMPONENTS`, `:94–107`
  `normalize_weights()`, `:404–405` `score`/`contrib`, `:434` `'weights': W`.
  The UI stops reading `score`, `components`, `contribution` and `weights` on
  every row.

### 1.5 `screen_history.py` — keep the recorder, kill the fitter

This is the one place where "the weighted score is gone" is read too broadly.

**Keep `record()`.** It gets *more* valuable, not less. It currently logs
`components` (seven normalized floats). It should log the transcript's row
verdicts instead — `{row_key: verdict}` plus `gpa` and `credit_earned`. That
is a far better join key than seven floats, because a verdict is a fact about
the chart and a float was a fact about a coefficient.

**Delete `calibrate()` (lines 155–199).** Its whole output shape is
`{'suggested': weights}`.

**Keep `match_trades()` unchanged.** It is the reusable half, and it is what
the replacement needs.

**Replacement question.** Not "what weights should these components have" but
**"when this condition passed, did the trades go better than when it
failed?"** — per rubric row, `n` passed / `n` failed / avg P/L each side /
the same Welch-`t` confidence label `conditions.py` already produces. Served
at `GET /api/screen/rubric/results` (§6.6), rendered in the Rubric modal
footer where `#scrCal` used to sit. This is a *rubric audit*, and unlike
calibration it never writes anything: the rubric's credit is fixed in code,
and the audit's job is to tell the owner when a condition earning 2 credits
has been earning them for no reason.

If the rubric agent has not built the audit by the time this UI lands, the
endpoint returns `{ok: true, ready: false, matched: n, min_trades: 30}` and
the footer shows the progress line — exactly the shape `scrCalLoad()` already
handles for the not-ready case, so the copy carries over.

---

## 2. The ranked board

### 2.1 Structure stays cards, not a table

The existing board is `.scrCard` divs and it must stay that way. A table row
cannot hold the play block, and the play block is the thing that makes the
board actionable. "Columns" below means *fixed zones within the card*, so the
eye lands in the same place on every row — which is the only thing a table was
buying.

```
zone A (left, fixed)   ticker · direction pill · gate pill · catalyst pill
zone B (right, fixed)  GPA · state · price            <- always same x-offset
zone C (full width)    distance to anchor
zone D (full width)    credit meter + "what's missing"
zone E (full width)    journal-edge strip
zone F (full width)    play block (existing scrPlayBlock, unchanged)
zone G (full width)    the sheet note, verbatim
```

### 2.2 How a 0-4 GPA reads instantly

The owner thinks in GPA. Lean all the way into the transcript metaphor and
stop apologising for it.

```
  NBIS  long  34/50 up                          3.0  at level  $233.02
```

The GPA is set at 21px, 700 weight, `var(--navy)` — the same visual weight the
existing card gives `x.best_play.grade`, so a play grade and an environment
GPA look like the same kind of number, because they are: both are 0-4, both
mean "how much of what I wrote down is currently true". Consistency with
`plays.py`'s docstring is deliberate and load-bearing.

One decoration, and only one: a **trend caret** when the GPA moved this
session.

```
  NBIS  long  34/50 up                     3.0 ▲  at level  $233.02
                                           ^^^^^^ was 2.0 at 10:36
```

No letter grades. No colour-coding the number itself — a red 1.0 and a green
4.0 teaches the eye to read colour and stop reading the number, and there are
already five pill colours competing on this card.

### 2.3 The credit meter — and the reason it is not optional

**This is the part of the design most likely to be got wrong, so it is stated
first.** A GPA in 1.0 increments has exactly five possible values. Across a
12–40 name watchlist that is not a ranking, it is a five-bucket histogram. On
any normal morning three or four names will read 3.0 and the board's actual
order will be decided entirely by the tiebreak — which means the number the
owner is looking at is not the number doing the sorting. That is a bad UI, and
it is bad because of the rubric's coarseness, not because of anything the UI
did.

The fix is not to make the GPA continuous. The coarseness is correct: 1.0
increments are honest about how much resolution largely-boolean conditions can
carry. The fix is to **show the fraction underneath it**, so the sub-grade
ordering is visible rather than secret:

```
  ████████████████████░░░░░░░░   9.5 of 13 credits · 2 not offered
```

Filled portion `var(--navy)`, remainder `var(--card-2)`, 6px tall, the same
hairline idiom as the existing `.prog-text` bar. The "2 not offered" suffix is
dim and always present when non-zero — it is how the board admits, at a
glance, that this name is being graded on a shorter syllabus than its
neighbour.

Under the meter, **the two rows that cost the most credit**, and nothing else:

```
  missing: 5/12 not curling with the break (-1) · ATR not expanding (-1)
```

Two, not all fourteen. Fourteen conditions times five cards is a wall, and a
wall is read as decoration. The full transcript is one click away and that is
where it belongs.

### 2.4 Tie-breaking, stated exhaustively

`rank()` in `screener.py` already has a key tuple; this replaces its tail. In
strict order:

1. **A live play outranks everything.** Unchanged from today, and
   `plays.py:grade_ticker`'s argument holds: a 4.0 whose trigger has not fired
   describes something that is not happening.
2. Among live plays, higher **play grade** first.
3. `gpa` descending.
4. `gpa_exact` descending — the pre-rounding fraction. **This is why §6.1
   requires it.** Without it steps 5–7 do all the work.
5. State rank: `triggered` < `at_level` < `approaching` < `idle`.
6. `closest.distance_atr` ascending. Nearer is more decided.
7. **Ticker, A–Z.** Not cosmetic. Two rows identical through step 6 must land
   in the same order on every single scan or the board shuffles under polling
   for no reason the user can see. A deterministic final key is a
   flicker-prevention mechanism, not a nicety.

### 2.5 Communicating the 5-slot cap

The status line becomes explicit about displacement, because "5 of 5" tells
you a limit exists and nothing about what it cost you:

```
12 watched · 5 of 5 slots · 10:42:06
next in line: HOOD 3.0 (9.0 of 13) — needs to beat NBIS 3.0 (9.5 of 13)
```

That second line renders only when the top bench row's `gpa` equals or exceeds
the bottom board row's. When the bench is genuinely quieter, it stays absent
rather than saying "nothing close", which is noise.

The slots input at line 612 keeps its `min="1"` and drops to `max="8"`. The
GPA will produce mornings with six 4.0s and the reflex will be to widen. The
docstring's argument does not weaken because the grading improved; if
anything a defensible number next to row nine makes over-showing *more*
dangerous, because now you trust the list while not reading it. Eight is the
concession; twenty was never defensible.

### 2.6 Not flickering under once-a-minute polling

Three separate problems, three separate mechanisms.

**(a) `scrRender` currently destroys the DOM.** Lines 3050–3056 do
`$('scrBoard').innerHTML = res.board.map(...).join('')`. Every poll that is a
full teardown: scroll position lost, hover lost, any text selection lost, and
a CSS transition can never run because the node it would animate no longer
exists. Replace with keyed patching:

```js
SCR.nodes = {};            // entry_id -> element
function scrPatch(rows) {
  const seen = new Set();
  rows.forEach((x, i) => {
    const k = x.entry_id || x.ticker;
    seen.add(k);
    let el = SCR.nodes[k];
    if (!el) { el = document.createElement('div'); SCR.nodes[k] = el;
               $('scrBoard').appendChild(el); }
    const sig = scrSig(x);                  // order key + every rendered value
    if (el.dataset.sig !== sig) { el.innerHTML = scrRowInner(x);
                                  el.dataset.sig = sig; }
    el.className = 'scrCard ' + x.state;
    if (el !== $('scrBoard').children[i])   // reorder only on real change
      $('scrBoard').insertBefore(el, $('scrBoard').children[i] || null);
  });
  Object.keys(SCR.nodes).forEach(k => {
    if (!seen.has(k)) { SCR.nodes[k].remove(); delete SCR.nodes[k]; }
  });
}
```

`scrSig(x)` is a cheap join of everything the card renders. An unchanged row
costs one string compare per poll, which at 40 rows is free.

**(b) A GPA that crosses a band boundary must not reorder the board on the
first scan that sees it.** A name sitting on a condition edge would otherwise
swap places every minute — exactly the failure `AlertGate` exists to prevent
one layer down, and the same remedy applies. Client-side:

```js
const SCR_CONFIRM_SCANS = 2;   // a new grade moves a row only when it repeats
```

Scan 1 sees a changed `gpa`: the row **flags** — the caret appears, the
background flashes `var(--card-2)` for 1.5s via a CSS transition (which now
works, per (a)) — and **holds its position**, sorted on its previous grade.
Scan 2 agrees: the row sorts on the new grade and moves. Scan 2 disagrees: the
flag clears silently and nothing ever moved.

Held rows are not hidden. A row sorted on a stale grade shows the new GPA with
the caret; the caret *is* the disclosure that position and grade briefly
disagree. Hovering it gives `3.0 since 10:42 · sorted on 2.0 until confirmed`.

`gpa_exact` drift never triggers any of this. Only the rounded `gpa`, and only
in the direction of change.

**(c) An open transcript must survive a poll.** If `#scrDetailModal` is open
for ticker T, the poll refreshes its *contents in place* — same keyed-patch
approach on the transcript rows — and never closes it, never scrolls it, never
moves the row underneath it. A modal that closes itself while you are reading
it costs the user a move, which the constraints forbid.

### 2.7 The bench

Unchanged in purpose ("Watching, nothing near"), capped at 20 as today,
dimmed to 60%. Bench rows show ticker, GPA, credit fraction, state, price —
**no credit meter, no missing-rows line, no transcript payload** (§6.4). The
bench is a reassurance that nothing was dropped, not a second board.

---

## 3. The transcript panel

### 3.1 Where it lives

Not a new modal. The detail modal already exists (`#scrChartModal`, lines
723–746) and already opens on a card click via `scrDetail(ticker)`. It becomes
two-tabbed using the `.srcTab` class the journal scope switcher already uses,
and **Transcript is the default tab**. The chart is the second tab.

That ordering is the whole redesign in one decision. The owner asked for
"continuity and better understanding of system and results". A chart shows you
the market. A transcript shows you *the system's reading of* the market, which
is the thing that has never been visible and the thing that has to become
visible for any of this to compound.

Rename `#scrChartModal` → `#scrDetailModal`, `#scrChartBody` →
`#scrDetailBody`; the chart controls (timeframe select, MTF and Levels
checkboxes) move inside the Chart tab's own strip so they are not present while
they do nothing.

### 3.2 The four verdicts, and why N/A cannot look like failure

A closed vocabulary of four, each with its own glyph, colour and *row
treatment*:

| Verdict | Glyph | Colour | Row treatment | Counts toward |
|---|---|---|---|---|
| `pass` | `✓` | `var(--green)` | normal | earned **and** possible |
| `fail` | `✗` | `var(--red)` | normal | possible only |
| `partial` | `◐` | `#E08B00` | normal | fractional earned, full possible |
| `na` | `n/a` | `var(--text-dim)` | 55% opacity, points and credit cells rendered as `—` rather than `0`, right-aligned italic `na_reason` | **neither** |

Three independent signals separate `na` from `fail`, because one is not
enough: a different glyph (word, not a mark), a different opacity, and
**empty cells where a failure would show a zero**. A user scanning the points
column sees `2 / 2`, `0 / 1`, `— / —` — and `— / —` cannot be misread as bad
news the way a red `0 / 1` can.

The denominator carries it too. The section subtotal reads `2.0 / 2.0` and the
section header carries `1 credit not offered`. The GPA line reads `9.5 of 13
credits · 2 not offered`. A user who never opens the transcript still learns
that this name was graded on a shorter syllabus.

**Hard requirement on `grading.py`:** an unmeasurable condition is `na` and
its credit is absent from `credit_possible`. It is never `fail`, and it is
never `pass` either — "we could not check, so assume fine" is how a 4.0 gets
handed to a name with nineteen daily bars. `plays.py` already got this right
("Unknown never counts against: missing data is not evidence") and the GPA must
match it exactly, including the symmetry: missing data is not evidence *for*,
either.

### 3.3 ASCII mockup — one ticker's full transcript

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  NBIS                      [ Transcript ] [  Chart  ]                 [Close] │
├───────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│   ENVIRONMENT GPA                                    long · anchor PMH 232.85 │
│                                                                               │
│      3.0  ▲                 ███████████████████████░░░░░░░░                   │
│      was 2.0 at 10:36       9.5 of 13 credits · 2 not offered                 │
│                             graded 10:42:06 on the 10:36 close                │
│                                                                               │
│   ── LEVELS ──────────────────────────────────────────────────── 3.5 / 5.0 ── │
│   ✓  At the anchor level        0.11 ATR from PMH 232.85           2.0 / 2.0  │
│   ✓  Room beyond the anchor     1.34 ATR clear, next is 236.00     1.0 / 1.0  │
│   ◐  Anchor quality             pre-market extreme (0.95)          0.5 / 1.0  │
│   ✗  Clear of an opposing shelf PDH 233.40 sits 0.05 ATR above     0.0 / 1.0  │
│                                                                               │
│   ── EMA CLOUDS ───────────────────────────────────────────────── 3.0 / 4.0 ── │
│   ✓  34/50 gate agrees          price above, trend up              2.0 / 2.0  │
│   ✓  5/12 on the break side     price above 5/12                   1.0 / 1.0  │
│   ✗  5/12 curling with it       slope -0.004/bar over last 3       0.0 / 1.0  │
│                                                                               │
│   ── HIGHER TIMEFRAMES ──────────────────── 2.0 / 2.0 · 1 not offered ──────── │
│   ✓  1H price above 34/50       above by 0.42 ATR                  2.0 / 2.0  │
│   n/a Daily price vs 20/21      19 daily bars, needs 22               —  /  — │
│                                                                               │
│   ── VOLUME ──────────────────────────────────────────────────── 1.0 / 1.0 ── │
│   ✓  Participation at the level 1.8x the 20-bar average            1.0 / 1.0  │
│                                                                               │
│   ── VOLATILITY ──────────────────────────── 0.0 / 1.0 · 1 not offered ─────── │
│   ✗  ATR expanding              ATR(14) 2.41, down from 2.68       0.0 / 1.0  │
│   n/a Chop filter               needs a full session of 6-min bars    —  /  — │
│                                                                               │
│   ─────────────────────────────────────────────────────────────────────────── │
│   TOTAL                                                           9.5 / 13.0  │
│   0.73 of the offered credit  →  band 3 (0.70–0.84)  →  GPA 3.0               │
│                                                                               │
│   ╭─ THE JOURNAL, ON THIS STATE ─ not a grade input ────────────────────────╮ │
│   │  -$40 per trade vs your book, over 23 trades          Strong           │ │
│   │  34/50 with trade + 1H with trade   -$61   n=14       Some             │ │
│   │  5/12 with trade                    -$19   n=31       Noise            │ │
│   │                                                                        │ │
│   │  A 3.0 environment your book has lost money in. The grade describes    │ │
│   │  the chart; this describes you. They are allowed to disagree.          │ │
│   ╰────────────────────────────────────────────────────────────────────────╯ │
│                                                                               │
│   YOUR NOTE   Trade vs 1h MTF, long over or short under                       │
│                                                                               │
│   [ Assign a play ]                              [ Open in Chart tab → ]      │
└───────────────────────────────────────────────────────────────────────────────┘
```

Notes on the mockup, in the order the eye hits them:

- **The band arithmetic is shown.** `0.73 → band 3 (0.70–0.84) → GPA 3.0`. The
  owner can reconstruct the rounding without reading code. This one line is
  most of what "understanding of system" means in practice.
- **`graded 10:42:06 on the 10:36 close`.** Two clocks, both stated, because
  `screener_service`'s docstring is right that they are different on purpose
  and a user who does not know that will think the panel is stale.
- **Section subtotals read `earned / possible`,** never a percentage. Credits
  are a count; a percentage invites the arithmetic the transcript exists to
  show.
- **The measured number is always on the same line as the verdict.** Never in a
  tooltip. A tooltip is where a measurement goes to be ignored.
- **`why` is on hover** (`.tipWrap`/`.tip`, already in the stylesheet at
  lines 81–96), per condition label, carrying the prose relocated from
  `SCR_COMP_HELP`. Hover is right for *rationale* and wrong for *measurement*.
- **The journal block is fenced and labelled `not a grade input`**, dashed
  border, and sits *below* the total so it can never be read as part of the
  arithmetic. The closing sentence is hardcoded UI copy, not generated — it is
  the editorial position of the whole feature and it should read identically
  every time.
- **The journal block is absent, not empty**, when `edge_matched` is `[]`. An
  empty panel labelled "the journal, on this state" reads as "the journal has
  nothing good to say", which is a different claim from "no factor reached
  eight trades".

### 3.4 The Rubric modal — the same transcript with no ticker in it

`#scrRubModal`, opened from the Rubric button at line 622, same geometry as the
dead weights modal. It renders `GET /api/screen/rubric` with the identical row
template and identical section grouping, minus verdicts and measurements:
every condition, its credit, the band table, and the full `why` prose inline
rather than on hover. Footer holds the rubric audit (§6.6) where
`#scrCal` sat.

Building it from the same template as §3.3 is deliberate: the reference and the
instance must be visibly the same document, so "what is possible" and "what
this name got" are read against each other without a translation step.

---

## 4. Morning intake

### 4.1 It is the existing paste box, extended

`#scrPaste` at line 579 already is the morning intake, `scrAddRows()` already
reports per-line problems, and `watchlist.parse_rows` already accepts the
sheet's exact format. Building a second paste box for "the morning list" would
be two boxes for one act.

What changes: one checkbox and a results table.

```
┌─ WATCHLIST ─────────────────────────────── 2026-09-25 ─┐
│  [ Carry forward ]  [ Clear day ]                      │
│  ┌──────────────────────────────────────────────────┐  │
│  │ NBIS  230/228  232.85/236  Trade vs 1h MTF       │  │
│  │ SNDK  1,870/1,857  1,886/1,900                   │  │
│  │ HOOD                                             │  │
│  │ CRWV                                             │  │
│  │ ZZZZ                                             │  │
│  └──────────────────────────────────────────────────┘  │
│  [x] Assign plays after adding                         │
│  [           Add to watchlist           ]              │
│                                                        │
│  5 parsed · 4 added · 1 skipped                        │
│  assigning plays…  ███████░░░  3 of 4                  │
│                                                        │
│  NBIS  added     long over PMH 232.85, else short      │
│  SNDK  added     long over PMH 1,886.00                │
│  HOOD  added     no play matched — 34/50 chop, no      │
│                  level within 0.5 ATR                  │
│  CRWV  replaced  assigning…                            │
│  ZZZZ  skipped   'ZZZZ' does not look like a ticker    │
└────────────────────────────────────────────────────────┘
```

### 4.2 Per-ticker statuses

A closed vocabulary. The UI renders a colour and never invents a status.

| Status | Colour | Copy shown | Where it comes from |
|---|---|---|---|
| `added` | `var(--green)` | the assigned play's sentence, or `assigning…` | `watchlist.add_many` |
| `replaced` | `var(--navy-3)` | `replaced this morning's earlier row` | `add_many` collapses a same-ticker same-date row (watchlist.py:118–127). **This is not a duplicate and must not be reported as one** — re-typing a row means correcting it. |
| `skipped` | `var(--red)` | the verbatim `issue` from `parse_rows` | watchlist.py:264–267 |
| `no_data` | `var(--red)` | `no bars from the data API` | assignment pass |
| `thin_history` | `#E08B00` | `19 daily bars, needs 22 — will grade on a short syllabus` | assignment pass |
| `no_play` | `var(--text-dim)` | the assigner's reason, verbatim | assignment pass |

`no_play` is dim, not red, and this is a real design position: **a ticker with
no play assigned is a correct outcome, not an error.** The owner's stated wish
is that every pasted ticker "comes back with a play already set up". Taken
literally that forces the assigner to manufacture a play for a name sitting
mid-cloud with no level within half an ATR, and a manufactured play is worse
than none — it is a staged order with an arbitrary trigger, sitting in an
approval queue, looking exactly like the four good ones. I would rather the
board say *"HOOD: nothing on your sheet fits this state"* in dim grey. That
sentence is information. A fabricated play is a liability with a button next
to it.

This requires the assignment agent to return `no_play` with a reason as a
first-class result. Flagging it here so it is not discovered late.

### 4.3 Why intake is two-phase

Assignment needs bars for every pasted name. At a 20-name paste that is three
batched `get_bars_multi` calls plus grading, which is seconds, not
milliseconds — and the constraints forbid blocking the once-a-minute scan or
costing the user a move.

So: **`POST /api/screen/intake` returns synchronously** with the parse result
(pure string work, instant) and a `job_id`. The rows land on the watchlist
immediately; the board picks them up on the next scan regardless of whether
assignment ever finishes. The UI then polls `GET /api/screen/intake/<job_id>`
at 1s and fills in the play column per ticker as results arrive.

This is the `pollAssign` / `pollBacktest` / `pollScreen` pattern already in
`showTab` at line 2024. Same idiom, no new machinery.

If assignment fails wholesale — data API down mid-job — every pending row flips
to `no_data` with a `[ Retry assignment ]` button, and the watchlist rows stay.
Intake never rolls back what it added; the list is the durable part and the
plays are the convenience.

---

## 5. The assigned-play board

### 5.1 Naming, before anything else

**`assign.py` is not about plays.** It assigns OU *engines* from a measured
half-life (`assign_one`, `assign_from_bars`, `apply_assignments`), it has UI in
the Baskets tab (`runAssign`, `pollAssign`, `loadAssignBands`), and it is a
completely different noun. Calling the new thing "assignment" in code or on
screen creates a collision in a codebase that already has `/api/assign`-shaped
UI a tab away.

On screen the word is **staged**. In code the endpoints are `/api/staged/*`.
The verb the morning list performs is still "assign a play", because that is
what the owner says, but the resulting object is a *staged play* and its
lifecycle field is `stage`.

### 5.2 Placement

A collapsible band across the top of the right-hand card, above Board. At the
start of the day it is open and it is the whole screen; by 10:00 it collapses
to one line and the Board owns the space again.

```
┌─ STAGED PLAYS ─────────────────────── 4 staged · 1 triggered · 0 sent ──[▾]─┐
```

Collapsed state persists in `localStorage`, and it **auto-opens** when any
staged play transitions to `triggered`. That is the one moment the band earns
the screen back, and it is the same escalation logic `AlertGate` uses: open on
escalation, never on a downgrade.

Not a third column. At trading-desk width, 400px of watchlist plus two flexible
columns leaves every column too narrow to hold a play's five fields on one
line, and a wrapped trigger is a misread trigger.

### 5.3 One staged play, rendered

```
┌─ STAGED PLAYS ─────────────────────── 4 staged · 1 triggered · 0 sent ──[▾]─┐
│                                                                             │
│  ○─────●─────○─────○      NBIS   LONG          env 3.0     journal -$40/23  │
│  assigned triggered sent filled                                             │
│                                                                             │
│  anchor       PMH 232.85  (pre-market high)                                 │
│  trigger      6-min close over 232.85 with 5/12 above     ✓ 10:42           │
│  invalidation 6-min close back under 231.90               watching          │
│  target       236.00  (prior day high)     1.34 ATR · 1.3 R                 │
│                                                                             │
│  IF price over PMH go long (1h MTF above, volume above average)              │
│                                                                             │
│  [ Dismiss ]  [ Edit play ]                          [ APPROVE  →  SEND ]   │
│                                                            ^ 232.85 · long  │
│                                                              ~$2,000 · combo│
└─────────────────────────────────────────────────────────────────────────────┘
```

Fields, all five, always in the same four rows, always in this order —
**anchor, trigger, invalidation, target** — because a fixed reading order is
what lets the owner check a play in under a second at 9:31. A play missing an
invalidation shows `invalidation  — not set` in red, because a staged order with
no exit condition is the single most dangerous row this UI can display.

`env 3.0` is the environment GPA, clickable straight through to §3.3's
transcript. `journal -$40/23` is the same sanity check, same fence, same
colour. The staged board is where the tension between the two matters most,
because this is the row with a send button on it.

### 5.4 Making an accidental order structurally impossible

Five mechanisms, three of them in the UI and two on the server. The UI ones are
convenience; **the server ones are the guarantee.**

**(1) The send affordance does not exist until the trigger has fired.** Not
disabled — *absent*, with a fixed-size dim placeholder holding the space:

```
  [ Dismiss ]  [ Edit play ]                      ( waiting for trigger )
```

Identical geometry, so nothing shifts when it arms. A button that *appears*
where the pointer already was is the primary accident vector in this whole
design, and a reserved slot is the only fix.

**(2) It is the furthest thing from every other control.** Approve sits hard
right; Dismiss and Edit sit hard left; minimum 120px of dead space between.
Nothing is ever placed between them, and no keyboard shortcut is bound to
approval — not Enter, not Space. The card is not focusable as a whole.

**(3) Two deliberate actions, via `appConfirm`.** Never a bare `confirm()`, and
never a bespoke dialog. `appConfirm` at line 1343 is the app's standard and the
engine-arm flow at 1380 is the precedent to copy:

```js
const ok = await appConfirm('Send NBIS long?',
  'Market order, <b>combo</b> sleeve, about <b>$2,000</b>.<br>' +
  'Trigger fired 10:42 at 232.85. Invalidation 231.90.<br>' +
  'Environment GPA 3.0. Your journal on this state: <b>-$40 over 23 trades</b>.',
  'SEND NBIS LONG');
```

The confirm label is the ticker and direction, never "OK" and never "Confirm".
Muscle memory clicks "Confirm"; it does not click "SEND NBIS LONG" on the wrong
card. The journal edge is restated in the dialog — the last screen before money
moves is exactly where an inconvenient number belongs.

**(4) Server-side: a versioned confirm token.** `GET /api/staged` issues each
row a `confirm_token` derived from its `id` + `stage` + `updated_at`.
`POST /api/staged/approve` requires it and refuses a mismatch with `409`. So:
a stale tab cannot send; a double-fire cannot send twice (the first approve
bumps `updated_at` and invalidates the token); a play that flipped to
`invalidated` between render and click cannot send at all. **The UI's two-step
is ergonomics. This is the actual safety property**, and it holds even against
a replayed request.

**(5) Server-side: stage gating.** Approve is rejected unless `stage ==
'triggered'`. The trigger condition is re-evaluated server-side at approve
time against the latest closed bar, not trusted from the row. A trigger that
was true at render and false now is a `409`, and the UI surfaces it inline:
`trigger no longer holds — re-check`.

Approval then calls the existing `/api/manual/buy` path. This component adds no
new route to the broker, which is itself a safety property worth stating: there
is exactly one way an order leaves this app and the staged board is a caller of
it, not a peer.

### 5.5 Lifecycle states on screen

A four-step rail plus a pill. Terminal states collapse the rail.

| `stage` | Rail | Left border | Pill | Send affordance |
|---|---|---|---|---|
| `assigned` | `●─○─○─○` | `var(--border)` | `staged` | placeholder |
| `triggered` | `○─●─○─○` | `var(--scarlet)` | `triggered 10:42` | **APPROVE → SEND** |
| `sent` | `○─○─●─○` | `var(--navy)` | `sent 10:42:18` | gone; `[ Cancel order ]` |
| `filled` | `○─○─○─●` | `var(--green)` | `filled 232.91` | gone; links to the journal row |
| `invalidated` | rail replaced | `var(--red)`, 60% opacity | `invalidated 11:04 — closed under 231.90` | gone |
| `expired` | rail replaced | `var(--border)`, 45% opacity | `expired at the close` | gone |

The left-border colours deliberately reuse `.scrCard.triggered` /
`.at_level` / `.approaching` from lines 65–68, so scarlet means "something is
happening now" on both boards.

`invalidated` and `expired` keep their cards for the rest of the session rather
than vanishing, sorted to the bottom and dimmed. A play that invalidated at
11:04 is the most instructive row on the screen at 11:30, and silently removing
it is how a tool stops teaching. A `[ Clear settled ]` button in the band
header removes them when the owner chooses to.

---

## 6. The HTTP surface

### 6.1 The seam: exactly what I need from `grading.py`

This is the interface most likely to be got wrong, so it is stated as a
requirement rather than an assumption. **One function, one shape:**

```python
grading.transcript(row) -> dict
```

where `row` is whatever the measurement layer produces for one ticker. The
returned dict, in full:

```json
{
  "ticker": "NBIS",
  "gpa": 3.0,
  "gpa_exact": 2.92,
  "credit_earned": 9.5,
  "credit_possible": 13.0,
  "credit_not_offered": 2.0,
  "band": {"index": 3, "lo": 0.70, "hi": 0.84, "fraction": 0.7308},
  "direction": "long",
  "anchor": {"name": "PMH", "price": 232.85, "kind": "premarket",
             "label": "pre-market high"},
  "graded_at": "2026-09-25T10:42:06",
  "bar_close_at": "2026-09-25T10:36:00",
  "sections": [
    {
      "key": "levels",
      "label": "Levels",
      "credit_earned": 3.5,
      "credit_possible": 5.0,
      "credit_not_offered": 0.0,
      "rows": [
        {
          "key": "at_anchor",
          "label": "At the anchor level",
          "verdict": "pass",
          "offered": true,
          "points": 1.0,
          "credit": 2.0,
          "earned": 2.0,
          "measured": "0.11 ATR from PMH 232.85",
          "measured_value": 0.11,
          "measured_unit": "atr",
          "why": "Under a fifth of a daily range is where a break or a rejection becomes imminent rather than hypothetical.",
          "na_reason": null
        }
      ]
    }
  ]
}
```

**Non-negotiable properties, and what breaks in this UI without each:**

1. **`verdict` is exactly one of `"pass" | "fail" | "partial" | "na"`.** Four
   strings, no booleans, no `null`. A `null` verdict forces the UI to guess
   between "failed" and "could not measure", and it will guess wrong in the
   direction that looks like failure.
2. **`na` implies `offered: false`, and its `credit` is excluded from
   `credit_possible`.** This is the mechanism that makes §3.2 true. If `na`
   credit stays in the denominator, a thin-history name is silently punished for
   the data it does not have, and nothing in the UI can undo that.
3. **`gpa_exact` is required.** Five discrete GPA values across a 40-name list
   is a five-way tie. Without the pre-rounding fraction, §2.4's tiebreak falls
   through to distance-in-ATR and the board is ordered by something the owner
   is not looking at. This is the single most important field in the payload
   after `verdict`.
4. **`credit` is an absolute count, not a fraction of the total.** The UI says
   "2 of 13 credits" and must not re-derive it. `earned == points * credit`,
   and the UI displays both rather than recomputing.
5. **`measured` is a pre-formatted display string, authored where the
   measurement is made.** The UI never formats a measurement. It does not know
   that proximity is in ATR and volume is a ratio, and teaching it invites the
   screener and the screen to disagree about units — the same class of bug
   `screener.py`'s docstring describes for cloud state.
6. **`measured_value` + `measured_unit` accompany it** for the caret, the
   colour and the audit join. Display string for humans, number for machines,
   both present, never one standing in for the other.
7. **`sections` and `rows` are in display order.** The UI renders the arrays as
   given and never sorts. Display order is a rubric decision; it belongs where
   the rubric is.
8. **`row.key` is stable across releases.** It is what `screen_history` logs
   and what the rubric audit (§6.6) joins on. A renamed key silently empties
   an audit row rather than erroring, which is the worst available failure.
9. **`why` lives in the payload.** The `SCR_COMP_HELP` prose was good and is
   being deleted from JS; it must land here or it is lost.
10. **`band` reports its own arithmetic** — `fraction`, and the `lo`/`hi` of the
    band it landed in. §3.3 shows that line verbatim. The UI must not have the
    band table hardcoded in two places.

Also required, on the module rather than per-ticker:

```python
grading.rubric() -> {"sections": [...same shape, no verdicts...],
                     "bands": [{"index": 0, "lo": 0.00, "hi": 0.29}, ...],
                     "credit_total": 15.0,
                     "version": "2026-09-25"}
```

`version` is a plain string, bumped whenever credit or row keys change, logged
with every `screen_history` reading. Without it a transcript from three weeks
ago cannot be read and the audit silently mixes two rubrics.

### 6.2 Removed

| Endpoint | Note |
|---|---|
| `GET /api/screen/weights` | gone |
| `POST /api/screen/weights` | gone |
| `GET /api/screen/calibrate` | gone — orphaned by the weighted score's removal (§1.5) |

### 6.3 `GET /api/screen/rubric`

The rubric with no ticker in it. Fetched once per session; the Rubric modal and
the transcript's section labels both read it.

```json
{"ok": true, "rubric": { /* grading.rubric() verbatim */ }}
```

### 6.4 `GET /api/screen` — same path, changed rows

Each row in `result.board` **gains** `gpa`, `gpa_exact`, `credit_earned`,
`credit_possible`, `credit_not_offered`, `band`, and a full inline
`transcript`. Each row **loses** `score`, `components`, `contribution`,
`weights`.

Five board rows with ~14 transcript rows each is about 8 KB — fine inline, and
worth it: the board's missing-rows line (§2.3) needs the per-row credit, and a
second round trip per card on every poll is not acceptable at 60s cadence.

**Bench rows carry the scalars only — `gpa`, `gpa_exact`, `credit_earned`,
`credit_possible` — and no `transcript`.** Twenty bench rows with full
transcripts is 30 KB of payload per poll for panels nobody has opened. The
detail modal fetches §6.5 on click.

`result` also gains:

```json
{"rubric_version": "2026-09-25",
 "next_in_line": {"ticker": "HOOD", "gpa": 3.0,
                  "credit_earned": 9.0, "credit_possible": 13.0}}
```

`next_in_line` is the top bench row, or `null`. It exists so §2.5's
displacement line does not need the UI to reach into the bench array and
re-derive the comparison.

### 6.5 `GET /api/screen/transcript/<ticker>`

Live transcript for one ticker, computed on request. What the detail modal and
the bench rows use.

```json
{"ok": true, "ticker": "NBIS",
 "price": 233.02, "atr": 2.41,
 "transcript": { /* §6.1 */ },
 "edge": {"edge": -40.0, "matched": [
    {"id": "combo:entry:ema_34_50=with&entry:mtf_1h=with",
     "label": "34/50 + 1H", "value": "with trade + with trade",
     "edge": -61.0, "n": 14, "confidence": "some"}]},
 "note": "Trade vs 1h MTF, long over or short under",
 "plays": [ /* plays.grade_ticker output, unchanged */ ],
 "staged": [ /* §6.8 rows for this ticker */ ]}
```

`409` not connected, `404` no bars — matching `/api/screen/levels/<ticker>` and
`/api/plays/grade/<ticker>` exactly, so the UI's error handling is one code
path for all three.

`edge` comes straight from `conditions.edge_for()`. **`sub` is not read by this
UI and should not be sent** — it was the 0-1 normalization for the weighted
score's `edge` component and it no longer means anything. `edge` (dollars) and
`matched` (with `n` and `confidence`) are the display payload.

### 6.6 `GET /api/screen/rubric/results` — what replaces calibrate

Per rubric row, how trades taken while that row passed compare to trades taken
while it failed. Reuses `screen_history.match_trades()` unchanged and
`conditions.py`'s existing Welch-`t` confidence vocabulary.

```json
{"ok": true, "ready": true, "matched": 47,
 "min_trades": 30, "window_min": 10,
 "rubric_version": "2026-09-25",
 "book": {"n": 47, "avg_pl": 18.40},
 "rows": [
   {"key": "at_anchor", "label": "At the anchor level", "credit": 2.0,
    "pass": {"n": 31, "avg_pl": 42.10},
    "fail": {"n": 11, "avg_pl": -22.30},
    "na":   {"n": 5},
    "spread": 64.40, "t": 2.31, "confidence": "strong"}]}
```

Not ready:

```json
{"ok": true, "ready": false, "matched": 12, "min_trades": 30,
 "window_min": 10, "rubric_version": "2026-09-25"}
```

Read-only forever. There is no apply button and no suggestion, because the
rubric's credit is fixed in code by design. The audit's job is to tell the
owner when a row earning 2 credits has been earning them for nothing — and then
the fix is an argued edit to `grading.py`, in a commit, not a slider.

`confidence` uses `conditions.py`'s four labels (`thin` / `some` / `strong` /
`noise`) and the UI reuses `COND_CONF` at line 4080 verbatim. One confidence
vocabulary in the app.

### 6.7 `POST /api/screen/intake` and `GET /api/screen/intake/<job_id>`

```json
POST {"text": "NBIS 230/228 232.85/236 Trade vs 1h MTF\nHOOD\nZZZZ",
      "date": "2026-09-25", "assign": true}

200  {"ok": true, "parsed": 3, "added": 2, "job_id": "ik_9f2c1a",
      "rows": [
        {"ticker": "NBIS", "status": "added",    "reason": null},
        {"ticker": "HOOD", "status": "replaced", "reason": "replaced this morning's earlier row"},
        {"ticker": null,   "status": "skipped",  "line": 3,
         "reason": "'ZZZZ' does not look like a ticker"}]}
```

Returns as fast as `/api/watchlist/rows` does today; assignment runs on its own
thread. `assign: false` omits `job_id` and the endpoint is a straight rename of
the existing route's behaviour.

```json
GET  /api/screen/intake/ik_9f2c1a

200  {"ok": true, "done": false, "progress": {"n": 3, "of": 4},
      "rows": [
        {"ticker": "NBIS", "status": "added", "staged_id": "sp_44ab",
         "sentence": "IF price over PMH go long (1h MTF above, volume above average)",
         "gpa": 3.0, "reason": null},
        {"ticker": "HOOD", "status": "no_play", "staged_id": null,
         "reason": "34/50 chop, no level within 0.5 ATR"},
        {"ticker": "CRWV", "status": "thin_history", "staged_id": null,
         "reason": "19 daily bars, needs 22"}]}
```

Job records live in memory, keyed by `job_id`, dropped after an hour. Losing one
to a restart costs nothing: the watchlist rows are already written and the board
grades them on the next pass regardless.

### 6.8 `GET /api/staged`

```json
GET  /api/staged?date=2026-09-25

200  {"ok": true, "date": "2026-09-25",
      "counts": {"assigned": 3, "triggered": 1, "sent": 0, "filled": 0,
                 "invalidated": 1, "expired": 0},
      "rows": [
        {"id": "sp_44ab", "play_id": "p_7c1e", "ticker": "NBIS",
         "stage": "triggered", "direction": "long",
         "anchor": {"name": "PMH", "price": 232.85, "label": "pre-market high"},
         "trigger": {"text": "6-min close over 232.85 with 5/12 above",
                     "result": true, "at": "2026-09-25T10:42:00"},
         "invalidation": {"text": "6-min close back under 231.90",
                          "result": false, "at": null},
         "target": {"price": 236.00, "label": "prior day high",
                    "atr": 1.34, "r_multiple": 1.3},
         "sentence": "IF price over PMH go long (1h MTF above, volume above average)",
         "gpa": 3.0, "credit_earned": 9.5, "credit_possible": 13.0,
         "journal_edge": -40.0, "journal_n": 23,
         "sleeve": "combo", "est_dollars": 2000,
         "updated_at": "2026-09-25T10:42:00",
         "confirm_token": "sp_44ab.triggered.1758798120"}]}
```

`invalidation` may be `null`, and the UI renders that in red (§5.3). It is not
filled in with a default; a guessed stop is worse than a visible gap.

### 6.9 `POST /api/staged/approve`

```json
POST {"id": "sp_44ab", "confirm_token": "sp_44ab.triggered.1758798120"}

200  {"ok": true, "stage": "sent", "order": {"filled": 2, "failed": 0},
      "row": { /* the updated §6.8 row */ }}

409  {"ok": false, "error": "stale", "reason":
      "this play changed since the page drew it", "row": { /* current */ }}
409  {"ok": false, "error": "not_triggered", "reason":
      "trigger no longer holds on the 10:48 close"}
409  {"ok": false, "error": "not_connected", "reason": "Not connected to Alpaca."}
```

Every `409` returns the current row so the UI redraws with the truth instead of
showing a bare error over a stale card. Delegates to the existing
`/api/manual/buy` path; adds no new route to the broker.

### 6.10 `POST /api/staged/dismiss`, `POST /api/staged/reassign`

```json
POST /api/staged/dismiss   {"id": "sp_44ab"}        -> {"ok": true}
POST /api/staged/reassign  {"ticker": "HOOD"}       -> {"ok": true, "job_id": "ik_..."}
```

`dismiss` sets `stage: "expired"` rather than deleting — the card stays, dimmed,
for the rest of the session (§5.5). `reassign` re-runs assignment for one
ticker and returns a job to poll, for the `no_play` row the owner disagrees with.

### 6.11 Unchanged

`/api/watchlist*`, `/api/plays*`, `/api/screen`, `/api/screen/run`,
`/api/screen/alerts`, `/api/screen/state`, `/api/screen/settings`,
`/api/screen/levels/<ticker>`, `/api/screen/chart/<ticker>`. The chart endpoint
in particular is untouched and the Chart tab keeps working exactly as it does
today.

`/api/screen/settings` keeps taking `slots`, but its server-side clamp at
`screener_routes.py:356` should come down from `min(20, ...)` to `min(8, ...)`
to match the input's new `max` (§2.5). Enforcing the cap only in the browser is
the same mistake the weights validation was built to avoid.

---

## 7. Degraded states

Each is a real morning, and in each the rule is the same: **say which
measurement is missing and what it would have graded.** "No data" is not an
answer a person can act on.

### 7.1 Pre-market, no premarket extremes yet

Before the pre-market session prints, `PMH`/`PML` do not exist, and they are the
two levels the sheet references most. Every level-section row that anchors on
them is `na`.

The board card:

```
  NBIS  —                                       2.0  pre-open  $231.40
  █████████░░░░░░░░░░░░░░░   4.0 of 8 credits · 5 not offered
  pre-open: no pre-market extremes yet — 5 credits not offered
```

The GPA is shown, not suppressed. It is computed on a genuinely shorter
syllabus and the meter says so. What must *not* happen is a 4.0 from four
offered credits reading identically to a 4.0 from thirteen — which is exactly
why `credit_not_offered` is a required field and why the suffix is always
rendered.

A band-level guard belongs in the rubric, not here, but the UI needs to know
about it: **below some floor of offered credit the GPA should be suppressed
entirely** and the row should read `ungraded — 4 of 15 credits measurable`.
`plays.py` uses the same instinct (`2.0 if not decidable`). I would rather see
`ungraded` than a 4.0 earned on a quarter of the syllabus, and I need
`grading.py` to make that call and report it as `"gpa": null` with
`"ungraded_reason": "only 4 of 15 credits measurable"` rather than leaving the
UI to invent a threshold.

### 7.2 A ticker with too little history

`_trend_context` needs 51 closed bars and already reports its own reason
(`screener.py:246`, `'need 51 closed bars, have 38'`). The whole cloud and
higher-timeframe sections go `na`, each carrying that string verbatim as
`na_reason`.

Board card, replacing today's red `no cloud state:` line:

```
  ABCD  —                                    ungraded  approaching  $18.22
  ░░░░░░░░░░░░░░░░░░░░░░░   2.0 of 4 credits measurable · 11 not offered
  too new to grade: 38 closed 6-min bars, needs 51
```

Not red. Red means something is wrong; a newly-listed name is not wrong, it is
new. Dim, and the row sorts below everything graded.

Intake flags this at paste time (`thin_history`, §4.2) so the owner learns it at
9:15 instead of discovering it at 10:40.

### 7.3 The data API is down

The scan loop already survives this — `screener_service.loop` wraps every
iteration and a bad pass leaves the last board intact. The UI's job is to stop
the last board from looking current.

```
12 watched · 5 of 5 slots · 10:42:06   ⚠ data API down since 10:47 — board is 6 min stale
```

The board dims to 70%, every GPA gets a `stale` pill, the `▲` carets clear, and
**every send affordance on the staged board disappears**, replaced by
`( data stale — cannot confirm trigger )`. That last one is the important part:
the trigger re-check at §5.4(5) cannot run without data, so the UI must not
offer an action the server would refuse.

Transcripts remain openable and are stamped `graded 10:42:06 · data since
10:47 unavailable`. A stale transcript is still the truth about 10:42.

### 7.4 A scan mid-flight

The scan is 60s and its fetches are batched, so a pass takes a second or two.
**Nothing blanks.** The existing board stays fully rendered; `#scrStatus` gains
a hairline progress treatment (the `.prog-text` idiom at line 270) and the word
`scanning…`. When the result lands, §2.6's keyed patch updates only changed
rows.

`Scan now` disables itself for the duration so a double-click cannot queue two
passes.

A scan that returns with `errors` non-empty keeps today's behaviour — the red
`n error(s)` count in the status line — and the count becomes clickable,
listing ticker and error, because an error affecting two of twelve names is
survivable and worth reading rather than worth hiding.

### 7.5 The watchlist is empty

`scan_once` already returns `note: 'watchlist is empty for today'`. The board
shows:

```
  Nothing on today's list. Paste this morning's tickers on the left,
  or [ carry forward from 2026-09-24 (11 names) ].
```

The carry button is inline and pre-filled with the most recent prior date, which
`/api/watchlist/carry` already resolves server-side when `from` is omitted. One
click to a full morning.

### 7.6 `grading.py` raised

A rubric that throws must not take the board with it. The service already wraps
each pass; `grading.transcript()` must be called per-ticker inside a `try`, and
a failure produces a row with `gpa: null`, `ungraded_reason: "grading failed:
KeyError: 'atr'"`, rendered in red as the one genuinely-wrong case:

```
  NBIS  long                                 ungraded  at level  $233.02
  grading failed: KeyError: 'atr' — the rest of the board is unaffected
```

Verbose on purpose. `screener.py:238`'s note applies exactly: swallowing the
cause here is how a schema mismatch hides as "no signal" for weeks.

---

## 8. Where I would push back on the brief

Collected so they are not spread through the document.

1. **"A GPA in 1.0 increments"** is the right call for honesty and the wrong
   call for ordering. Five values will not rank 40 names. The credit meter and
   `gpa_exact` are not optional additions; without them the board is sorted by
   something the owner cannot see. §2.3, §6.1(3).

2. **"Each ticker comes back with a play already set up"** must be allowed to
   fail out loud. A manufactured play for a name with no level within half an
   ATR is a staged order with an arbitrary trigger sitting in an approval queue
   looking exactly like the good ones. `no_play` with a reason, in dim grey, is
   the better answer. §4.2.

3. **The GPA will argue for more slots.** It should not win. Six defensible
   4.0s is a harder call than six indefensible 68s, not an easier one, and the
   cap exists because attention is the constraint. Eight is the most I would
   concede; the count that did not fit goes in the status line instead. §2.5.

4. **The journal edge will contradict the GPA, and the reflex will be to fix
   it** by folding edge into the rubric so the numbers stop disagreeing. Refuse.
   The GPA grades the chart; the edge grades the owner's history of trading that
   chart. Making them agree destroys the only place in the app where those two
   things can be compared. §0, §3.3.

5. **The transcript does not belong on the board card.** Fourteen rows times
   five cards is a wall, and a wall is skipped. Two rows of "what's missing" on
   the card, the full transcript one click away, and the click is cheap. §2.3.

6. **`/api/screen/calibrate` is dead and should be deleted, not left to rot.**
   It fits weights and there are no weights. But `screen_history.record()` and
   `match_trades()` must survive, because the rubric audit that replaces
   calibration is a better version of the same idea and runs on the same data.
   §1.5, §6.6.

7. **Do not call it "assign" in code or on screen.** `assign.py` already means
   engine assignment from measured half-life, with its own UI one tab away. The
   noun is *staged play*; the field is `stage`; the routes are `/api/staged/*`.
   §5.1.
