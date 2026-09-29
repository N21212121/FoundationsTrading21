# 08 — the visual system: depth, gloss, motion

**Status:** design spec. Written 2026-09-29, before any of it is built.
**Scope:** `static/index.html` only — 6470 lines, one file, no build step.
**Constraint from the owner:** the logo stays, and the theme colours stay.
**Replaces:** nothing. This is additive. No `:root` token is renamed and no
hue moves anywhere in this document.

---

## 0. WHY THIS EXISTS

The owner, 2026-09-29: *"I'd like you to give the whole program an upgrade in
aesthetic/theme/appearance to make this look less 'homemade'... I do like the
theme colors, but something about the whole program just seems amateurish, and
that's mainly because of my lack of direction. I wish I could better describe
what I mean... Actually, Foundations currently looks a bit flat and 2D. Maybe
it's that on Fidelity, TradingView, etc that there is a lot of motion with
charts and numbers updating? I think also there is a certain 'gloss' that's
missing."*

He described the symptom accurately and then apologised for not being able to
name the cause. The cause is three measurable things, and none of them is the
palette:

1. **Depth is carried entirely by borders.** `--border` `#c9cfdc` appears 46
   times in the CSS block and 74 times in the file. There are 41+ declarations
   of `1px solid var(--border)` against **exactly one** resting elevation
   shadow in 6470 lines (`.card`, L269, 3px blur at 8% alpha). A border says
   *edge*. It cannot say *above* or *below*. So every surface in the app — a
   button you can press, a field you type into, a card that holds them, a
   table rule, a modal — draws the identical hairline and sits on the
   identical plane.
2. **No gloss, because there is not one gradient on any chrome.** The only
   `linear-gradient` in the CSS block (L226/228) fakes the heatmap's divider
   rules. The topbar, every button, the primary send button, every pill and
   chip: flat fills. There is also no `:active` rule anywhere in the file and
   no `:focus` rule anywhere in the file, so nothing in the app responds to
   being pressed or to being reached by keyboard.
3. **The chart does not move.** `loadChartBars()` is called from exactly two
   places — `drawChart` (L1990) and `setTimeframe` (L2237) — and **there is no
   timer on either.** The candles and clouds on screen are frozen at whenever
   the owner last clicked a ticker or a timeframe chip. Meanwhile the one
   second-by-second interval in the app (L2219) polls TWO endpoints
   (`/api/health` and `/api/quote/{t}`) every second in order to update one
   text node, `#chartPrice`, and never touches the canvas.

Point 3 is the one that answers his actual question. Next to TradingView this
app looks static because **it is static**. That is a bug, and it is fixed
first, before any of the paint.

---

## 1. THE RULE: DEPTH ENCODES AFFORDANCE

One rule, applied everywhere, so this does not become sprinkled shadows.

> **Readouts are carved in. Controls are lifted out.**
>
> A surface that *reports* takes an INSET shadow: it is a recess, a well, a
> machined pocket holding a number. A surface that *accepts input* takes an
> OUTER shadow: it stands above the plane and can be pressed. Pressing it
> inverts the shadow. Nothing gets a shadow for decoration.

The consequence is that depth becomes information rather than styling. A new
reader of the UI learns, without being told, that the thing with an edge-lit
top can be clicked and the thing sunk into the panel cannot. It also gives a
mechanical test for every one of the ~60 rules this touches: *does this
element report, or does it accept?* If the answer is "neither, it contains",
it is a panel and takes `--elev-1` and nothing else.

This is also why the motion layer reads as instrumentation rather than as
animation: a number that ticks inside a recessed well is a gauge. The same
number ticking on a flat white rectangle is a web page.

**Where the metaphor stops.** No bevels, no simulated glass, no blur. The
reference is a modern instrument panel — Fidelity's Active Trader Pro, DAS,
TradingView's chrome — not skeuomorphic brushed aluminium. Every gradient in
this spec darkens downward by at most 5.5% alpha, which is below the threshold
where anyone would call it a gradient, and is chosen so that text contrast on
chrome can only improve (see §3.4).

---

## 2. THE PALETTE DOES NOT CHANGE

Every existing `:root` token keeps its name and its value, with one exception
argued in §3.5. The tokens added below are all derived arithmetically from
`--navy`, `--border`, `--card-2` and `--navy-2`, with the derivation written
into the comment at each one. Nothing introduces a hue.

Two pre-existing colour problems are fixed while in the file, because both are
bugs rather than taste:

- **`--panel` and `--accent` do not exist.** `.jobctl` (L640, L642) references
  both. `background:var(--panel)` resolves to nothing, so those buttons render
  with no background at all, and `.jobctl:hover { border-color:var(--accent) }`
  is discarded by the parser, so they have no hover state. Repoint at
  `--card-2` and `--scarlet`.
- **`.chip` is defined twice**, L184–190 and L420–423, disagreeing on
  background, colour, font-size, padding and border-radius. The second wins on
  every shared property, so L188's `background: var(--card-2)` is dead code
  and every chip in the app is actually `#fff`. Consolidate into one rule
  BEFORE adding anything to it, or the change will appear to do nothing.
  While there: L423's `color:#e8ecf4` becomes `#fff` to match the four sibling
  `.on` states at L73/182/183/189.

Also folded in: five unrelated near-blacks (`rgba(0,0,0,…)`,
`rgba(10,20,48,…)`, `rgba(11,31,59,…)`, `rgba(15,28,43,…)` across 18 sites at
9 different alphas) collapse onto one navy-derived shadow colour, and the
orphan literal `#e3e7ef` (L402) becomes `--rule`.

---

## 3. THE ELEVATION AND GLOSS LAYER

### 3.1 The shadow colour

```css
--shadow-rgb: 43,58,82;   /* --navy #2b3a52 split into channels for rgba() */
```

Every shadow and every scrim in the app becomes this one colour at different
alphas. **Nothing is black after this.** A black shadow under a navy-and-cream
UI reads as dirt; a navy shadow reads as the same light falling on the same
material.

### 3.2 The elevation scale

Each step is a **tight contact shadow** (1–4px, no spread — "this rests on the
plane below") stacked with a **broad ambient shadow carrying negative spread**,
so the ambient stays under the object instead of haloing it. Alpha roughly
doubles per step; blur roughly triples.

| Token | Reach | Assigned to |
|---|---|---|
| `--elev-0` | none | `body` / `--bg`. The page recedes. |
| `--elev-1` | ~6px | resting panels: `.card`, `.scrCard`, `.brBox`, `.calDay`, `.layerBox`, `.ordSec`, every button and chip at rest |
| `--elev-2` | ~12px | hover and raised chrome: any control under the pointer, `.hmCell:hover`, `.scrCard:hover` |
| `--elev-3` | ~24px | transient chrome that arrives: `.scrToast`, `#posActionBar`, sticky table headers |
| `--elev-4` | ~84px | modals, `.card.maxed`, `#ordBox` |

`--inset-1` is the contact shadow turned inward, for readouts and fields.
`--inset-2` is the pressed state for controls.

### 3.3 Gloss

Gloss is **one inset white hairline on the top edge only**, at two strengths
(white-on-white needs more alpha than white-on-grey). No text ever sits on a
1px line, so gloss cannot move a contrast ratio. Paired with a *lighter top
border* (`--border-lift`) on raised surfaces and a *darker top border*
(`--border-firm`) on sunken ones — the whole trick is that light falls from
above, so a raised object is lit on top and a recess is shadowed on top.

### 3.4 Sheen, and why contrast can only improve

```css
--sheen: linear-gradient(180deg, rgba(var(--shadow-rgb),0) 0%,
                                 rgba(var(--shadow-rgb),.055) 100%);
```

Applied as `background-image` over an existing `background-color`, so one
token glosses navy, yellow and grey chrome alike. It runs from 0% to 5.5%
navy, which means **it darkens only** — every pixel of a sheened surface ends
up equal to or darker than it is today, so dark text on it can only gain
contrast. The topbar specifically goes from white-on-`--navy-2` at 3.18:1 to
3.67:1 across its lower half.

**Sheen is forbidden on three things**, and this is load-bearing, not taste:

- **`.scrCard` background tints** (L97–109). The comment at L86–96 spells out
  that the fill carries DIRECTION and its intensity carries STATE, deliberately
  on a separate axis from the left border's urgency. A 5.5% darkening at the
  bottom of each tint is contrast-safe but muddies a signal the owner tuned by
  hand. Screener cards lift with shadow and border only.
- **`.scrPill` and `.eng-pill.ov`** — same argument, same colours.
- **Bare `td`/`th`/`*`** — `.hmGapV`/`.hmGapH` (L225–228) fake the heatmap's
  section dividers with a `background-image` gradient. `background-image` is a
  single property; a blanket application would **erase the dividers**. Every
  `--sheen` application is scoped to an explicit class list.

### 3.5 The one existing value that changes

`--scarlet-bright` (L15) is currently `#F9E04C` — **identical to `--scarlet`**,
which makes all four of its uses visual no-ops. It is set to what its name
promises, `#FBEB8B` (`--scarlet` mixed 35% toward white, same hue), and the
primary buttons get a real top-to-bottom gradient from a token that is already
wired into the four correct places. Navy text on a *lighter* yellow gains
contrast, so this is strictly safe.

While there: `.ordSend` (L597) — the app's send-the-order button — currently
carries `border: 1px solid var(--border)`, a grey border on a yellow button.
That single line is a large part of why the order panel looks unfinished.

---

## 4. TYPE

This is the largest single contributor to "homemade" and the owner did not
name it, because a default font is invisible until it is replaced.

Current state: `font-family: 'Segoe UI', system-ui, sans-serif` at 14px
(L34) — the Windows default, which is what every utility someone wrote for
themselves looks like.

**The measured problem is not the face, it is the absence of a scale.** There
are **259 `font-size` declarations across 15 distinct values**, and
11 / 11.5 / 12 / 12.5px — four sizes inside a 1.5px band, perceptually
indistinguishable — account for **210 of them (81%)**. Inline it is worse:
171 of 186 (92%). Those are not decisions; they are four moments of "that
looks a touch big" preserved in amber. The same semantic element gets all
three: the uppercase section label exists as `.section-label` at 11.5px
(L370, 17 uses) and as five hand-typed clones at 11px (L840, L1245, L1251)
and 10.5px (L3150, L3153) — three sizes and three greys for one idiom.

**Headings are not inconsistent, they are accidental.** There is no global
`h2` rule at all. `.card h2` is 12px uppercase with 1.5px tracking (L266) —
but the four modal titles at L857, L904, L942 and L1332 are `<h2>` *outside*
any `.card`, so they fall through to the browser default: ~21px bold navy,
no tracking. `#modalTitle` is 15px/600 (L618) and `#ordHead h3` is 18px
(L474). **Four different modal-title treatments.** Nine bare `<h3>` have no
rule either, so they render at ~16.4px while `.layerBox h3` (L246) is 11px
uppercase — `h3` means two unrelated things in the same file.

Numbers are set in the same face as prose, and `font-variant-numeric:
tabular-nums` is patched onto 12 individual selectors while being absent from
`body`, where it would inherit. So these readouts change width as their digits
change: `#acctEquity` (the equity figure in the persistent chrome),
`#chartPrice`, `.kpi b` and `.kpiStack b` (the 19px KPI numbers, the largest
in the app), `.calDay .pl` (centred in a grid, where ragged width is maximally
visible), `.hmCell .val`, `.wBox` (right-aligned numbers *without* tabular
figures, the worst combination), `.badge`, `.mode-pill`, `.eng-pill`.

Worse than the jitter is the alignment. `th` is `text-align:left` (L399) and
`td` sets no alignment at all (L402), and the positions render (L1834-1839)
emits Shares, Contracts, Set $ and Current $ as plain `<td>` with the `$`
prefixed into the string. **A trading app's price columns are left-aligned
with unaligned decimal points and unaligned currency symbols** — while
`table.ordChain td` (L521) right-aligns correctly. One of those two is already
right, so fixing it is bringing the app in line with itself.

### 4.1 Three faces, all already installed

No webfont, because there is no build step and the app must work with no
network. Verified present on this machine:

| Role | Face | Why |
|---|---|---|
| **Chrome** — wordmark, tab labels, section eyebrows, table headers | **Bahnschrift** (`C:\Windows\Fonts\bahnschrift.ttf`, variable, wght 300–700) | Windows ships DIN 1451 — the German industrial standard face used on gauges, instrument panels and signage. It is immediately not-a-default, and it is on-subject rather than merely different: this application *is* an instrument panel. |
| **Readouts** — the numbers you watch move | **Consolas** (`consola.ttf`) | Genuinely tabular, with a slashed zero. Scope is argued below; it is deliberately NOT every number. |
| **Prose** — body, labels, inputs, notes | **Segoe UI Variable** (`SegUIVar.ttf`, variable, wght 300–700) | The Windows 11 refresh of Segoe with optical sizing; cleaner than plain Segoe UI at the 11–13px sizes this app lives at. `Segoe UI` sits behind it in the stack. |

Every stack falls through to what the app uses today, so a non-Windows browser
degrades to the current appearance rather than to Times New Roman.

```css
--ff-chrome: 'Bahnschrift', 'Segoe UI Variable Text', 'Segoe UI', system-ui, sans-serif;
--ff-body:   'Segoe UI Variable Text', 'Segoe UI', system-ui, sans-serif;
--ff-num:    'Consolas', ui-monospace, monospace;
```

**How far the mono face goes, and why not further.** The first draft of this
spec put Consolas on every number in the app. That is wrong, and the reason is
worth recording: a mono face inside a sentence fights the prose voice, and most
numbers in this UI are inside sentences — counts in status strips, quantities
in log lines, dates. Blanket mono would make the app look like a terminal
emulator rather than an instrument.

The rule that replaces it is §1 again. **A readout gets the instrument face;
a number in a sentence does not.** Consolas is scoped to: the KPI figures
(`.kpi b`, `.kpiStack b`), the numeric table columns in the positions blotter,
ledger, logs and the option chain, `#acctEquity`, `#chartPrice`, `.pl-pill`,
`.calDay .pl`, `.hmCell .val` and the screener grade. Everything else stays in
the prose face.

Separately and unconditionally: **`font-variant-numeric: tabular-nums` goes on
`body`.** It inherits, Segoe UI ships the `tnum` feature, and that single line
makes every number in the app stop jittering — including all the ones Consolas
does not touch. The 12 individual patches at L122, 326, 369, 403, 482, 522,
556, 585, 3429 and 3539 then delete. **Net negative line count**, and it is
the single highest-value line in this entire document.

### 4.2 The scale

Seven steps, derived from the measured counts rather than invented.

| Token | Size | Absorbs | Declarations moved |
|---|---|---|---|
| `--fs-micro` | 10px | 9.5, 10, 10.5 | 13 |
| `--fs-label` | 11px | 11, **11.5** | **99** |
| `--fs-meta` | 12px | 12, **12.5** | **111** |
| `--fs-body` | 13px | 13, 13.5, 14 | 16 |
| `--fs-lg` | 15px | 15, 16 | 10 |
| `--fs-xl` | 18px | 17, 18 | 3 |
| `--fs-display` | 22px | 19 | 5 |

The two collapses that matter are **11.5 → 11 (47 declarations)** and
**12.5 → 12 (29)**. Both are pure find-replace with no judgement call, and
together with their partners they normalise 210 of 259 declarations. Do these
before anything else in this section.

`19px → 22px` is a bump, not a collapse: 19px sitting next to 15px is not
hierarchy, it is noise.

`body`'s declared 14px (L34) is close to a lie — almost nothing inherits it,
because `table` (L398), `input, select` (L387), `button` (L390) and `.kv`
(L368) all restate 13px. Set `body` to `--fs-body` and those four
declarations become inheritance.

Consolas has a smaller x-height than Segoe at the same nominal size, so
numeric runs in `--ff-num` take a +0.5px bump where they sit inline with
prose. Applied by class, not by eye.

### 4.3 Alignment, and getting the currency symbol out of the way

```css
.num { text-align: right; }
td.num, th.num { text-align: right; }
```

Applied to the numeric cells in the positions table (header L766-770, render
L1834-1839), `#ldgMonths` (L993), `#ldgEntries` (L1031), `#logTable` (L1357)
and `#perfDetail` (L1263). The `$` moves out of the cell string into a dimmed
span or the column header, so the digits themselves line up rather than being
pushed around by a sign.

Also: **`table-layout` appears zero times in the file.** All 14 tables are
auto-layout, so column widths reflow as data arrives and on every price tick.
Combined with the missing tabular figures, the positions blotter visibly
jitters horizontally while live. The numeric columns get explicit widths and
`table-layout: fixed` where the column set is known.

---

## 5. MOTION

### 5.1 One helper, not scattered animation

`setLiveValue(node, text, {key, num, tween, wash, colorize})` is the single
integration point. A call site that did `node.textContent = s` becomes one
call, and about a dozen call sites change in total. The helper owns: history,
direction detection, the tick animation, optional interpolation, first-paint
suppression and reduced-motion.

**The history store is keyed by a STRING, never by the node.** This is the most
important line in the design. `scrRender` replaces `#scrBoard.innerHTML`
(L3478), so on every reorder all ~50 screener cards are brand-new nodes. A
`WeakMap` keyed on the element would see "first render" for every one of them
and **flash all fifty rows at once**. Keyed by `'scr:AAPL:px'`, history
survives the rebuild and only genuinely-changed numbers move.

**A first paint is not a change.** Nothing animates when there is no history
for a key. The existing code already gets this right in one place (L3970–3973)
and documents the trap in a comment (L3968–3969): `px > null` coerces to
`px > 0`, so an unguarded comparison flashes every row green on arrival. That
comment stays; it is the reason the guard exists.

### 5.2 What ticks, what tweens, what must not

**Ticks** (hard jump + a 420ms directional flash and a 4px rise or fall):
every live number. This is what Fidelity and TradingView actually do to a
quote.

**Tweens** (count-up through intermediate values): only slow aggregates —
account equity (600ms against a 5s cadence), the journal and alert badges, the
screener grade, and the position P/L cells. Rule for future call sites:
**interpolate only when the update interval is at least 4× the animation
duration, and the intermediate values are not themselves claims about the
market.**

**Never tweened: the 1-second price.** Three reasons, in order of weight.
*Correctness* — a 400ms tween on a 1000ms cadence spends 40% of its life
displaying a price that was never quoted, and on a screener card whose whole
job is reporting where the tape sits relative to a level, a synthesised price
is a lie with a decimal point in it. The app already takes exactly this
position one layer up: the comment at L3937–3939 refuses to raise a crossing
alert because the feed cannot distinguish a close through a level from a wick
through it. *Lag compounds* — `price_feed.TICK_SECONDS = 1`, so on a fast mover
the tween is still in flight when the next tick lands, and the display sits
permanently a fraction of a move behind the tape: smooth and wrong. *It is not
what the platforms do* — they tick quotes and count up balances, and the
distinction is precisely this one.

### 5.3 The chart

**First, the bug.** Add a 10-second `loadChartBars()` refresh, gated on the
dashboard being visible, the tab being focused, and a ticker being selected.
10s and not 1s because `renderChart` (L2044–2216) is a full repaint — session
shading, grid, up to three EMA clouds through the polygon builder, candles,
volume, axes, crosshair, legend. That is fine at 0.1 Hz and wasteful at 1 Hz.

**Then, the liveness.** A second `<canvas>` overlaid on `#chartCanvas` with
`pointer-events: none`, created in JS so no markup changes, carrying the three
things that belong at 1 Hz: the live price line and its right-axis tag, the
crosshair and its labels, and the forming candle's extension from the live mid.
Moving the crosshair there also removes **a full chart repaint per mousemove**,
which L2279 and L2281 do today.

The live marker's y-position eases by exponential smoothing on a rAF loop
rather than a fixed tween, because that self-corrects when a new print arrives
mid-flight. **What must never ease: candle geometry, the EMA cloud polygons,
and the price axis range.** Tweening the y-scale would make the entire chart
drift every 10 seconds and would fight every pan and zoom handler at
L2261–2300. The scale snaps; only the marker eases.

### 5.4 Refresh states, and one thing to stop doing

A refresh is not a blank flash: the container keeps its content, goes slightly
quiet, and a 2px hairline slides across its top edge — reusing the one
`@keyframes` that already exists in the file (`progSlide`, L337) rather than
writing a second sliding bar.

`scrLoadBoard`'s catch (L3349) currently **overwrites `#scrBoard`** when a poll
fails, destroying up to 50 cards because one fetch timed out. It should keep
the last good board and mark it stale. A stale board that says so is strictly
better than an empty one.

### 5.5 The dead animation that already exists

`.prog-bar > i` has `transition: width .35s ease` (L331) and it has **never
fired once.** `progressHTML` (L5335) returns a fresh `<div class="prog-bar"><i
style="width:N%">` string which is `innerHTML`'d every 1200ms, so the `<i>` is
a new node born at its final width with nothing to transition from. Emit the
wrapper once and then patch `style.width` in place, and the existing CSS starts
working. ~20 lines, revives paid-for CSS, visible on every job in the app.

---

## 6. BUGS FOUND WHILE READING, NOT CAUSED BY THIS WORK

Recorded here because they were found by the audit and each is independently
worth fixing.

1. **The chart never refreshes** (§0.3, §5.3). Functional.
2. **`.prog-bar` transition is dead** (§5.5). Cosmetic but paid-for.
3. **`--panel` / `--accent` undefined** (§2). `.jobctl` has no background and
   no hover.
4. **`.chip` defined twice with conflicting values** (§2).
5. **The order panel opens underneath a maximized card.** `#ordBack` is
   `z-index:60` (L466) while `.card.maxed` is 200 (L280/292/313) and its
   backdrop is 150. Maximize the chart, press `order` on a screener card, and
   the panel is behind the backdrop. It also carries the heaviest shadow in the
   app (`0 18px 60px`, L472) while sitting at the lowest overlay layer, so
   depth and stacking actively contradict each other. `#ordBack` must be raised
   to ≥ 500.
6. **`/api/screen` is registered twice** — `app.py:2094` (the screen job) and
   `screener_routes.py:320` (the live board). The blueprint registers at
   `app.py:1212`, before the decorator at 2094 runs, so the blueprint wins GET;
   `pollScreen` (L6113) then reads `j.state` as `undefined`, falls through every
   branch, does nothing, and never clears its timer. Masked only because the
   Baskets tab is hidden at L656. **Do not unhide that tab before fixing this.**
7. **`.tipWrap .tip` is clipped today.** It is `position:absolute` inside a
   `.tipWrap` whose ancestor `.card` has `overflow:auto` (L268), which creates a
   clipping box that `z-index:960` cannot escape. Pre-existing. Do not invest a
   larger shadow here expecting it to show; the real fix is `position:fixed`
   with JS positioning, which is out of scope.
8. **`#scrBoard.innerHTML = …` resets `scrollTop` to 0** (L3478), so scrolling
   the screener and waiting for a reorder yanks the view back to the top. The
   FLIP wrapper restores scroll position as a side effect.
9. **No `visibilitychange` handler anywhere.** `refreshHealth` (5s) and
   `refreshPositions` (10s) run regardless of tab, and `startPriceTicker`'s
   interval is never cleared, so it polls two endpoints every second for the
   rest of the session from any tab with the browser minimized. Roughly
   3.3 req/s sustained at rest.
10. **`refreshPositions` (L1790) has no change guard** — it rebuilds every row
    every 10 seconds whether or not anything moved and whether or not the
    dashboard is visible, destroying hover, text selection and focus. It
    already preserves checkbox state correctly through `_selected`; it should
    copy `scrRender`'s signature trick (L3467–3472).
11. **`box-shadow` on a `<tr>` does not paint in a collapsed table.** Both main
    tables use `border-collapse: collapse` (L398, L510), so "rows lift on
    hover" must be `inset` shadows on `td`. The file already has the correct
    pattern at L525–527; copy it rather than inventing a `tr` shadow. Same
    class of bug as `tr.pos-row.selected { border-left: … }` at L406–407.
12. **The primary navigation cannot be reached by keyboard.** There are **28
    clickable non-buttons** — 14 `<span onclick>`, 12 `<div onclick>`, one
    `<tr>`, one `<th>` — and **zero `tabindex` and zero `aria-*` attributes in
    the entire 6470 lines.** Those 28 include all eight tab-bar items
    (L651–662), all nine `.tf-chip` timeframes (L690–698), the four
    chart-layer chips (L683–686), every maximize control and the row-remove
    `✕`. There is also **no `:focus` or `:focus-visible` rule anywhere**, so
    even the real `<button>`s and `<input>`s show only the browser's default
    ring, which matches nothing in the theme. This is a defect, not a polish
    item.
13. **The maximize button turns into a close button.** L2244, L5501 and L6307
    all do `$('chartMax').textContent = maximized ? '✕' : '⛶'`, so the
    *restore* affordance renders as a **close** glyph. A user who maximized
    the chart sees an X and reasonably expects the chart to go away. A
    correctness problem wearing an icon costume.
14. **The app looks disconnected for its first second.** L664 ships
    `<span id="connText">…</span>` as literal initial DOM, and `.dot` defaults
    to `--red` (L52). So on every first paint the topbar reads a red dot next
    to a bare ellipsis until the first poll returns. It should say
    "Connecting" with a neutral dot.
15. **`textarea` appears in no selector in the file.** The base control rule
    (L386) covers `input, select` only, so the five textareas get five
    different treatments — and three of them (L791, L1154, **L2617**) render
    as raw browser textareas. L2617 is the journal note field, the one prose
    input in the app: with no `font-family` set it inherits Chrome's UA
    default, which is **monospace**. A field whose placeholder reads *"What
    you saw, and why you took it"* renders in Courier inside a 2px inset
    border that matches nothing else on screen. Adding `textarea` to the L386
    selector fixes three of the five outright.
16. **SELL reads as less important than "Bulk Add".** `button.buy` (L392) gets
    a yellow fill, navy text and weight 600 — a real primary button.
    `button.sell` (L394) gets `background:#fff` and nothing else, so on L722–723
    the destructive-side action in a trading app is visually indistinguishable
    from a tertiary control. Relatedly, `.danger` is only defined as
    `#posActionBar button.danger` (L352–354); its one use happens to sit inside
    that bar, so it works **by luck**, and any future `class="danger"`
    elsewhere renders as a default button.
17. **Four hand-rolled modals duplicate a component that already exists.**
    `#modalBack`/`#modalBox` (L611–631) carries the comment *"ALL confirmations
    route through this"* — and then four more were built inline at L849–854,
    L896–901, L934–939 and L1324–1329, each gratuitously different: **four
    distinct backdrop colours for one scrim concept, six z-indexes across the
    six dialogs (60/500/900/940/945/946), radius 9 vs 10, and two of the four
    with no elevation shadow at all.** Two dialogs floating with no shadow is
    the clearest single "unfinished" tell on screen.
18. **39 selects show the native Windows arrow.** `appearance` appears zero
    times. Custom borders and backgrounds with a stock grey chevron reads
    *worse* than fully native, because it looks half-restyled.
19. **31 scroll containers show native Windows scrollbars.** No
    `::-webkit-scrollbar`, no `scrollbar-width`, no `scrollbar-color` anywhere.
    A square grey system scrollbar inside a rounded, shadowed card is one of
    the loudest homemade cues in the file, and it is about eight lines to fix.
20. **No button disables itself during its own async call**, except `.ordSend`
    (L600). There are 27 text-only loading states (`'Sending…'`, `'Saving…'`,
    `'Scanning…'`…) and every one of them can be double-clicked mid-flight.
    In an app that places orders, `'Sending…'` at L5983 with a still-clickable
    button is worth fixing for reasons well beyond polish.

---

## 7. RISKS THAT CONSTRAIN THE BUILD

- **`.card { overflow: auto }` (L268) clips every child's shadow.** The card's
  14px padding leaves room for `--elev-1` (≤6px) but not for `--elev-2`
  (~12px), and the last child of a scrolled-to-bottom list has zero room below
  it. Nested panels stay at `--elev-1`; nested hover uses `--inset-1` and
  `--border-firm` instead of a bigger lift.
- **`#chartCanvas` repaints its full bitmap opaque white every frame** (L2011,
  L2047) and has no `border-radius`, so any sheen or gloss placed on
  `#card-chart` is invisible inside the canvas rect and the canvas's square
  white corners will show inside a rounded card. Gloss goes on the card's
  border and top edge only. Separately, the canvas bakes `#0a1430`, `#5b6478`,
  `#eef1f6`, `#8a93a8` as literals — those are pixels, not CSS, so the chart's
  internals will drift from the new chrome unless read via `getComputedStyle`.
- **`.hmCell:hover { transform: scale(1.05) }` (L201) claims no z-index**, so
  neighbouring cells paint over the scaled one and a hover shadow would be
  sliced. Needs `position:relative; z-index:1`. `.hmTable` also has only 3px
  `border-spacing` (L191), so keep that hover shadow small.
- **A focus ring must be an `outline`, never a `box-shadow`.** `box-shadow` is
  a single property; a ring implemented that way would replace the elevation on
  every button and chip it applied to. `outline` follows `border-radius` in all
  current browsers, so there is no reason to reach for the other.
- **`#basketsView { overflow-y: auto }` (L307) forces `overflow-x: auto` too**,
  so a tool card's horizontal shadow reach must stay inside its 12px padding or
  it produces a spurious horizontal scrollbar.

---

## 8. BUILD ORDER

Each step is independently shippable and independently revertible.

1. **The frozen chart** (§5.3 first half) + the `visibilitychange` handler
   (§6.9). This is a bug fix and it is the owner's actual complaint.
2. **The token layer** (§3) + the colour bugs (§2, §6.3, §6.4) + the z-index
   correction (§6.5). No visual change yet beyond the bugs; this is the
   vocabulary everything else speaks.
3. **Type** (§4): `tabular-nums` on `body`, the two size collapses, the
   heading rules, the three faces, numeric alignment.
4. **Elevation applied**, in order of surface area: `.card`, `#topbar`,
   `button`, the primary actions, `input`/`select`, the scrims, `.scrCard`.
5. **The cheap finish items** (§9): scrollbars, select arrows, focus ring,
   `textarea` in the base selector, `.sell` and `.danger`.
6. **The motion helper** (§5.1) + the price, badge and equity call sites +
   the progress-bar fix (§5.5).
7. **FLIP on the screener reorder** (§5.4), which also fixes the scroll reset.
8. **Keyboard reachability** (§6.12) — `tabindex` and roles on the 28
   clickable non-buttons.
9. **The modal consolidation** (§6.17) and the icon set (§9.3).
10. **The chart overlay canvas** (§5.3 second half).
11. **Key `refreshPositions`** (§6.10) so P/L cells can tick.

---

## 9. THE TOKEN LAYER IS THE WHOLE FIX

The owner blamed his own lack of direction. The audit disagrees: the file has
a coherent sensibility — the palette comments at L16–29 and L64–105 document
contrast ratios and argue their own reasoning, which is more than most
production CSS does. What is missing is not taste. It is **a token layer**.

Counted:

| Thing re-typed by hand at each use site | Distinct values | Uses |
|---|---|---|
| inline `style=` attributes | 300 distinct declarations | **586 attributes / 1382 declarations** |
| `font-size` | 15 | 259 |
| padding / margin / gap | 20 | 473 |
| `border-radius` | 13 | 56 |
| `box-shadow` | 9 recipes | 12 |
| `letter-spacing` | 8 (including both `.5px` and `0.5px`) | 26 |

**542 of the 586 inline styles are fully static** — no `${}` interpolation —
so they are extractable by find-replace rather than by judgement. The single
clearest signal in the file: **`color:var(--text-dim)` appears 161 times
inline.** That is a semantic role — "this is secondary text" — typed out 161
times instead of being a class.

That is what "amateurish" means here, mechanically: near-identical things
drift apart because nothing holds them together. Six bordered boxes have six
paddings. Odd spacing values (3/5/7/9/11/13px) account for 132 uses, 28% of
all spacing, off any grid. `7px → 6px` (33 uses) and `9px → 8px` (33 uses)
are 66 changes with no perceptible difference, and they remove the
"someone nudged this by a pixel" texture that is most of the feeling.

### 9.1 The spacing scale

A strict 8px grid would force 6px — the single most common value in the file
at 83 uses — up to 8px and visibly loosen the dense screener and chain
layouts. A 2px base keeps the existing rhythm while killing the drift:

```css
--sp-1: 2px;  --sp-2: 4px;  --sp-3: 6px;  --sp-4: 8px;
--sp-5: 12px; --sp-6: 16px; --sp-7: 24px;
```

20 atoms collapse to 7. Composite box paddings standardise to exactly three:
`--sp-3` (dense: chips, pills, chain cells), `--sp-4 --sp-5` (cards,
sections), `--sp-5 --sp-6` (modal headers and bodies).

Radii collapse to four: `--r-sm: 4px` (controls), `--r-md: 8px` (cards),
`--r-lg: 12px` (modals), `--r-pill: 999px`. The seven pill classes are all
*trying* to be pills at radius 9/10/11/12px; `999px` says so and never needs
retuning again.

### 9.2 Components that are one component

- **`.rangeBtn` / `.gradeBtn` / `.srcTab`** are three names for a flex-1
  segmented button in a row, differing only in font-size (11/15/12px),
  vertical padding (3/10/5px) and radius (4/6/5px). `.rangeBtn` and
  `.gradeBtn` even share a byte-identical hover rule. → one `.seg` with
  density modifiers.
- **Fourteen bordered-box variants** — `.card`, `.scrCard`, `.brBox`,
  `.scrToast`, `.ordSec`, `#ordChainWrap`, `.layerBox`, `.del-opt`,
  `.kpiStack div`, `#posActionBar`, `.calDay`, the four inline modal boxes,
  `#modalBox`, `#ordBox` — carry **five radii and ten paddings for one
  concept**. `.brBox` and `.scrToast` are byte-identical apart from the
  shadow. → `.box` + `.box--dense` / `.box--lead` (the 4px left rail) /
  `.box--float`.
- **Seven pill classes**, four font sizes, three radii, two `min-width`s and
  one with none — so the engine pills in the positions table are ragged-width
  next to mode pills that are not. → one `.pill` base.
- **`.chip` carries three unrelated roles**: a filter toggle (L683–686), an
  icon button (the `⛶` at L681/750), and a tool-card header control. An icon
  button and a filter toggle should not share a class. → split `.icon-btn`
  out.
- **`.chip.on` and `.tf-chip.on` mean the opposite things.** In `#chartHead`
  the four layer chips turn **navy** when active; in `#tfRow` immediately
  below, the nine timeframe chips turn **yellow**. Two adjacent rows of the
  same visual atom with inverted activation semantics, and no stated rule for
  which is which. Pick one: **navy = a layer or filter is on, yellow = a mode
  is armed** (which is what `#engineBtn.armed` already means).
- **`margin-left:auto` appears 16 times**, each re-deriving "push the controls
  to the right". `#posActionBar` is the best-built component in the file — a
  real `display:none` → `.show` pattern with a `.count` and a `.spacer` — and
  it is used exactly once. → one `.bar` component, used everywhere.
- **Five inputs carry `text-transform:none`** purely to undo the over-broad
  `input[type=text] { text-transform: uppercase }` at L385, which is a
  ticker-field rule applied to all 21 text inputs. A global rule too
  aggressive and then fought inline at seven sites is the canonical
  homemade-CSS signature. → `.ticker-input`.

### 9.3 Icons

21 distinct non-ASCII glyphs do UI work, and **two different characters mean
"close"**: `×` (U+00D7) on `<button>` elements at L1542/2980/3073/3281, and
`✕` (U+2715) on `<span>` elements at L1842/5209 — same job, two characters,
two element types, two style paths.

One 16×16 stroke set, `currentColor`, `stroke-width:1.5`, as `<symbol>`s in a
single hidden `<svg>` near L648: `i-close`, `i-maximize`, `i-restore`
(which fixes §6.13), `i-sort-asc`, `i-sort-desc`, `i-warning`,
`i-arrow-right`. Seven symbols inline is about 40 lines and keeps the no-build
constraint. **No icon font, no CDN.**

Keep as text: the `·` separator (51 uses — that is typography, not
iconography), `§`, `Γ`, `Δ`, `²`, and every `×` that is genuine
multiplication (L5475 `×/yr`, L5918, L5966 `contracts × type`). Legend
swatches at L3874–3877 stop being `▬` — a glyph with no consistent baseline
across fonts — and become a 12×3 span whose colour comes from a token.

### 9.4 Empty states

The app already has empty-state copy in ~18 places and **the writing is
good**: *"No plays yet. Apply a preset or build one."*, *"No scan yet. Add
tickers, then Scan now."* The problem is six different renderings of it —
inline div at `padding:10px`, inline div at `padding:14px`, `<td colspan>`
with colour but no padding, `.prog-text`, bare `textContent`, and in one case
nothing at all (`#detailsBody` after a failed load just stays blank). None
has an icon, a title/body split or a call to action. One `.empty-state` class
across those 18 sites is a small diff with an outsized effect on perceived
finish.

---

## 10. WHAT WAS DELIBERATELY NOT DONE

- **No palette change.** He said he likes the colours and the audit agrees the
  colours are not the problem.
- **No framework, no build step, no dependency.** Still one file, still
  vanilla, still served by `app.send_static_file`.
- **No dark mode.** It would double the token surface and he did not ask.
- **No webfont.** A local app that stops looking right when the network is
  down is worse than one that uses what is installed.
- **No restructuring of `scrRow`.** A true keyed rewrite of the screener card
  is ~200 lines for an effect that FLIP achieves in ~25, because FLIP needs
  the old *geometry*, not the old *nodes*.
- **No glassmorphism, no blur, no bevels.** §1.
