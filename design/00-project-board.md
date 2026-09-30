# 00 — the board, the doctrine, and how to work on this project

**Status:** live. This file is maintained, not archived.
**Numbered `00`** because it sits before the other eight specs: it is how to
read them.

---

## 0. WHY THIS FILE EXISTS

The owner, 2026-09-29: *"It is virtually impossible for me as is to
meaningfully guide you in Foundations' build."* Earlier the same day, after
eight commits landed in one session: *"The changes now are so many that I can't
keep up."*

Both statements were correct, and the cause was structural. The project's
entire record was 54 commits and eight prose design specs. The specs do record
open questions — but as asides, in the middle of long arguments, across nearly
seven thousand lines. The things that actually block progress were the hardest
things in the repo to find.

So there are now two records, with a clear split:

| | What it holds | Who edits it |
|---|---|---|
| **The board** (a published page) | Decisions waiting on the owner, work in flight, blocked, queued, and what is done by area. Rows live in the page's own database. | The owner, from any device. Claude reads it back at the start of a session. |
| **This file** | The doctrine, the pacing rule, the agent roster, and the true build state of the design set. | Claude, in git, versioned beside the code it describes. |

**Board URL:** https://claude.ai/artifact/T4B44vTWfwvqBToB7TLWzu
(private — only the owner can open it)

Rows are `tasks/<id>` and `decisions/<id>`, plus `meta/snapshot` (the repo's
headline figures, so the page has no hardcoded numbers to go stale) and
`meta/lastcheck` (the last whole-board review). There is deliberately **no
second task list in this file**: two lists that mean the same thing will
disagree by Tuesday, which is the same rule `design/03` applies to
level-quality tables.

A task carries `deps` — an array of other task ids — alongside the free-text
`blocked_on`. The two are not redundant: `deps` is machine-readable and is what
the graph checks below run on, while `blocked_on` records the things that are
not tasks (a ruling, a backtester that does not exist yet).

### 0.1 How the board checks itself

Asked for on 2026-09-29: *"maybe there should be an agent involved in checking
tasks — when new tasks are added — to see if there are any dependencies that
stack or paradoxes/inconsistencies between any."*

Split in two, along the line this project already draws everywhere else:
**don't ask a model what you can compute.**

**Computed, in the page, from the `deps` graph.** These are facts, and the
board states them as facts:

- a **circular dependency** — A waits on B waits on A — which is the literal
  paradox case
- a dependency on an id **not on the board**
- a task marked `blocked` whose dependencies are **all done**
- a task marked `open` (startable) that **depends on unfinished work**
- a task marked `blocked` with **nothing recorded** as blocking it
- **chain depth**, so a task three deep is labelled as such on its own row
  rather than looking like a quick win

**Reviewed, by Claude, through the page's `sample` capability.** Only the
question code cannot answer: does this row contradict another row, duplicate
one, break one of §2's invariants, or belong behind an existing dependency. It
runs on Add for the single new task, and on demand over the whole board from
the Consistency box. The result is written to the row's `check` field, so it
persists and I read it next session.

The two are labelled differently on screen — *computed* and *reviewed* — because
one is a proof and the other is a judgement, and collapsing them would make the
judgement look stronger than it is.

---

## 1. THE PACING RULE

**One visually-judgeable change per session, then stop.**

There is no Node, no Deno and no Bun on this machine, and browser tooling is
not available in-session. So nothing in `static/index.html` can be executed,
linted or seen by Claude. The only checks that run are:

- `py -3 pairing.py --selftest` — 46 cases
- `py -3 exit_ladder.py` — 36 cases
- `py -3 backtest_sweep.py --symbols SPY --start <date> --selftest` — 2
  assertions against real bars, including that the control cell is
  byte-identical to the live engine
- `py -3 -m py_compile <module>`
- a delimiter-balance count on the `<script>` block, whose only honest reading
  is *"the open and close deltas match HEAD exactly"* — it carries a
  pre-existing off-by-three from regex literals it cannot parse

That means **the owner is the only one who can see whether the frontend is
right.** Work he cannot keep up with is work he cannot validate, and a mistake
then rides along for several commits before anyone catches it. Six commits of
unverified CSS in one session is how this rule was learned.

Backend work with a passing selftest can go further in one run, because the
machine checks it rather than he does.

---

## 2. THE DOCTRINE

Invariants the code states about itself. **Any change touching one of these
needs checking against it**, and this list is what the `ft-doctrine` agent
exists to enforce — no general-purpose reviewer can know these.

### 2.1 Exits

> **EXITS — ENGINE-DRIVEN ONLY. READ THIS.** There are NO price stops, trails,
> ratchets, premium monitors, or MACD-collapse exits in this file anymore. …
> **A gap through a level exits on the NEXT bar close, not at the level. That
> is the accepted design.**
> — `trade_router.py:20-28`

Stop-outs (`exit_kind='structural'`) set a same-day no-rebuy flag; profit exits
do not.

### 2.2 What may be a configurable — the most load-bearing rule in the project

> **An exit rule may exist as configuration if and only if `backtester.py` can
> replay it from bars alone.** Anything keyed to premium, to the spread, to an
> indicator the backtest does not compute, or to wall-clock latency stays out.
> … **If the ladder ships in the live path before the backtester can score it,
> this spec has smuggled an unrefereeable knob in through the front door while
> citing the rule that forbids it.**
> — `design/07-order-placement.md:67-77`

Because *"a knob you cannot score is a knob you tune by vibe, and that is the
thing this project deliberately refuses — tune the instrument, not the
verdict."* The code side is `exit_ladder.py:12-16`: rungs key to the underlying,
**never to premium**.

**Its measured narrowing:** `backtester.py` models the shares sleeve only, so a
ladder is refereeable on shares and **not at all on options**. Until an options
backtester exists, the honest position is that the options ladder is unmeasured
— and the app says so to the user at runtime (`app.py:2831`).

### 2.3 Constants are hardcoded on purpose

`HARDCODED CONSTANTS BY DESIGN — edit + commit to change`, stated at
`trade_router.py:30`, `engine_ou.py:75` and `halflife.py:69`. Extended to the
rubric: *"the credit is not user-adjustable. A slider on credit would
reintroduce the thing being deleted"* (`design/01:22`), and to the assigner:
*"None of these is a tuning knob in the UI; a slider here would make the
assigner un-replayable"* (`design/03:844`).

### 2.4 Measures, never selects

Stated in five files — `engine_contract.py:130`, `engines_bootstrap.py:32`,
`assign.py:3`, `halflife.py:3`, `design/05:684`:

> **Never let a loop rank engines per ticker and mount the winner: that is one
> overfit per name.**

The two engines hold opposite theses and must never both be mounted on one
ticker. `assign.py` never looks at P&L, and that is the whole point.

### 2.5 The journal

- `source` is validated against `SOURCES` and raises otherwise
  (`journal.py:116`). It is what keeps the hand-graded import separable, what
  `read_all` and `purge` filter on, and what `stats()` reports. **It must
  survive**, and the arm path needs it to gain a third value, `'assigned'`.
- **ONE RECORD = ONE FILL.** A position scaled into with three buys and closed
  with one sell is four records, not one (`journal.py:26`).
- The journal is **additive**: *"if this file is deleted the trading side keeps
  working"* (`journal.py:16`), and `app.py:98` enforces it — a broken journal
  must never stop a trade being recorded or block the bar loop.
- Every `filled_at` is Eastern wall-clock stored naive, normalised at the door.

### 2.6 The backtester is the referee

> Feeds historical bars through the **REAL** engine, one bar close at a time,
> exactly the way the live loop does. No vectorized shortcut, no reimplemented
> rules. **The engine under test is the engine that trades.**
> — `backtester.py:1-43`

Seven named fidelity rules, including *the engine never trades its own signal
bar*. And the boundary on every number the project quotes: it does not model
the options overlay, so **read results as signal quality, not dollar-accurate
live P/L**.

### 2.7 One threshold, one place

> Thresholds live [in `environment.py`]. The rubric layer reads labels and
> booleans, never raw scalars. **`grading.py` must never contain a number like
> `0.35`** … because **a grade with no visible parts is an opinion with a letter
> on it.**
> — `design/02:35-48`

Same rule as tables: `screener.LEVEL_QUALITY` is the single source of what a
level kind is worth — *"two tables that mean the same thing will disagree by
Tuesday"* (`design/03:30`). Constants are **borrowed, never copied**, and *"if
one of those needs to be different for assignment than for the board, the board
is wrong"* (`design/03:859`). Entry size has one source too: `_budget_for` and
`trade_router.compute_fill_spill` (`design/07:37`).

### 2.8 Propose, do not commit

> `evaluate()` returns INTENTS. It never marks a rung fired, because **a rung is
> only fired when the broker says so.**
> — `exit_ladder.py:17-23`

ATR is **pinned at fill** and never recomputed, so a rung cannot drift when
volatility expands (`exit_ladder.py:57`). A ladder is attached **after**
`confirm_fill` returns broker truth, never before — *"a ladder attached to an
unfilled order is a ladder against a position that does not exist"*
(`design/07:352`).

### 2.9 Unknown is a bucket, not a default

`UNKNOWN = 'unknown'   # a real bucket, never a silent drop`
(`backtest_context.py:170`). **Missing is not failing** (`design/01 §4`), and
N/A must not look like failure on screen (`design/04 §3.2`). The asymmetry that
matters: *"The staged path may proceed on unknowns because a human is looking;
the armed path may not"* (`design/03:822`).

Related: **measurement at entry, never after** (`backtest_context.py:68`), and
all decisions on the most recently **closed** bar (`signal_engine.py:108`).

### 2.10 The GPA's charter

Grades **environment only**. *"You will see 4.0 environments that you lost money
in, and 1.0 environments where you made money. Those rows are the useful
ones."* No fit to the 382 existing grades. No `conditions.edge_for()` in the
grade, because folding realized P/L into an environment grade makes it
self-confirming and turns §8's ordering test circular. No letter curve, no
bullish tilt. *"A high correlation with the old grade would be a warning sign,
not a pass"* (`design/01:25-55`, `:936`).

### 2.11 Failure-domain isolation

A screener bug must never stall the loop that manages positions
(`app.py:3173`); a disk error must never cost the board
(`screener_service.py:281`); the monitor thread must never raise out
(`app.py:1494`); a data gap triggers a full refill, never a partial
(`bar_cache.py:20`).

### 2.12 Execution safety

`alpaca_manager.py` is **the only module that talks to Alpaca**, and everything
upward receives plain dicts, never SDK objects. Shorts are **puts-only — no
put, no trade**. Intent is explicit `buy_to_open` / `sell_to_close`, so there
are **no naked short options, ever** (commit `1268104`). `execute_plan` +
`confirm_fill` mean broker truth, not planning estimates. Ten such guards are
enumerated at `design/03:772-786` as the arm path's dependency list.

### 2.13 EMA periods are bar counts

5/12/34/50 are **counts of bars, not minutes**, so changing bar size silently
changes the trend gate — at 3-minute bars the 34/50 gate shortens from 8.3
hours to 2.5. This is why the timeframe question is not a constant flip. The
matrix settled it: no configuration beat the live 10-minute setup.

### 2.14 Naming

`assign.py` already means engine assignment from measured half-life. The
staged-play feature is **not** called assign: the noun is *staged play*, the
field is `stage`, the routes are `/api/staged/*`, and `play_assign.py`'s
docstring is required to open with *"This is not `assign.py`."*
(`design/04:1193`, `design/03:66`).

---

## 3. THE TRUE BUILD STATE OF THE DESIGN SET

Two status lines in the specs are wrong, in opposite directions. Trust this
table, not the status line at the top of each file.

| Spec | Its own status line | Reality |
|---|---|---|
| `01` grading core | "No code written" | **Correct.** `grading.py` does not exist. `screener.DEFAULT_WEIGHTS` is still the live ranking mechanism. |
| `02` measurements | "Nothing here is implemented yet" | **Correct.** `environment.py` does not exist; `backtest_context.py:83` carries duplicate constants awaiting it. |
| `03` auto-assign | "Design spec only" | **Correct.** `play_assign.py` does not exist; no `/api/staged/*` route. |
| `04` UI/UX | "Nothing implemented" | **Correct.** None of its HTTP surface exists. Its `index.html` line citations are stale. |
| `05` timeframe matrix | "Both implemented and runnable" | **Correct**, but **every result table is stale** — they predate `ENTRY_END` (`d9416f5`), which removed ~46% of entries. Prereqs 1–3 still unmade. |
| `06` backtest context | "Written and tested, not wired in" | **Correct.** Still a library and CLI by design. Its §9 numbers are stale for the same reason. |
| `07` order placement | **"Nothing here is built" — FALSE** | **4 of 5 steps shipped.** `exit_ladder.py`, the `plan_exit(qty)` wiring, backtester partials and the order panel all exist. Only §4.1 and the arm path remain, both deliberately fenced. |
| `08` visual system | **"before any of it is built" — FALSE** | **7 of 11 steps shipped** (`b2d3c93`…`9597d4d`). Steps 8–11 remain. Every `index.html` line number in it is offset by ~740: the file grew from 6,470 to 7,212 lines. |

**The dependency chain is linear and blocked at the front.** `grading.py`
blocks `design/02`'s consumer, `design/03`, the whole of `design/04`, and the
arm path — which has no GPA 4 to gate on until it exists. Nothing in `01`–`04`
is built. That single module is the project's critical path.

---

## 4. THE AGENT ROSTER

Settled 2026-09-29, **not yet built.** Queued deliberately behind the board so
the project manager has something to read.

| Agent | Job | Tools |
|---|---|---|
| `ft-pm` | Reads the board, git and the specs; returns a briefing — current state, the three things worth doing next, what needs a ruling. **Proposes, never dispatches.** | read-only + `ArtifactData` |
| `ft-doctrine` | Checks a change against §2 of this file. The highest-value agent, because no general reviewer can know these rules. | read-only |
| `ft-smoke` | Runs the verification that actually exists (§1) and reports real output, pass or fail. | Bash + read |
| `ft-design` | UI/UX work from `design/08`: depth encodes affordance, palette fixed, logo untouched, one reviewable change at a time. | read + edit |
| `ft-research` | Feature depth — options P/L graphs with Greeks, simulators — returning a spec draft shaped like `design/NN`, not a link dump. | WebSearch, WebFetch, read |
| `ft-conflict` | The deeper half of §0.1: reads the whole board *and* the repo, and reports where a row contradicts the code rather than another row — a task whose `ref` points at a moved line, a task already done in a commit nobody closed out, a decision whose recommendation the code has since outgrown. The in-page check sees only the board; this one sees both. | read-only + `ArtifactData` |

**General correctness stays with `/code-review`**, which already exists, is
tuned, and has a multi-agent cloud mode. Three identical checkers voting was
considered and rejected: three instances of one model on one diff mostly agree,
so the tie-break rarely fires and it costs triple for little information.
Disagreement is only informative when the checkers look for different things.

**Tie-break:** on an invariant in §2, `ft-doctrine` wins. On anything else, the
owner rules.

**The project manager does not increase throughput.** It exists to make the
build legible. Increasing how much lands per session is the failure this whole
file was written in response to.
