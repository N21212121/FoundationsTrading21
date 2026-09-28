# 07 — Screener order placement and laddered exits

**Components:** a new `exit_ladder.py`; an order panel in `static/index.html` launched from a
screener card; additive fields on the position record; two new `exit_kind` values.
**Status:** design spec only. Nothing here is built. The arm path is explicitly NOT part of
this spec — it belongs to `03-auto-assign.md` §7 and its preconditions are restated in §9.
**Settled with Nate:** 2026-09-27.

---

## WHAT THIS IS

A screener card that has something to say gets a button. The button opens an order panel
pre-filled for that ticker in the direction the play points: live price refreshing on the
same 1-second feed the card already uses, a dollar or share size, a sleeve, and — this is
the new part — an **exit ladder**: a list of rungs saying *sell this much at this level*,
plus an optional ratchet that raises a floor under the remainder as price advances.

The ladder is attached to the position, not to the order. It survives the fill, it is
visible and editable while the position is open, and every rung it fires is a normal
journal fill that `pairing.py` books like any other.

## WHAT THIS DOES NOT DO

- **It does not place an order without a press.** Every order in this spec is sent by a
  human clicking Send. Pre-loading means *the panel is filled in before the trigger*, not
  *the order goes without you*. Unattended sending is the arm path, is fenced off in
  `03` §7, and has preconditions this spec does not satisfy (§9).
- **It does not fire on a screener alert.** The screener alert opens the panel. What the
  panel sends is a manual entry, recorded `source='manual'`. No code path in this spec
  turns a screener condition into an order. The screener grades levels and environment;
  the engine decides entries. Those are different triggers and only one of them has ever
  been backtested.
- **It does not add a monitor thread, a premium watcher, or a tick loop.** Rungs and the
  ratchet are evaluated on bar close, in the existing `bar_loop`, alongside every other
  exit. §4 is the whole argument for that and §4.1 fences off the alternative.
- **It does not introduce a per-rung UI knob that nothing can referee.** A ladder shape is
  backtestable (§6). That is the entire reason it is allowed to exist as configuration
  rather than as a constant in a commit.
- **It does not change how entries are sized.** `_budget_for` / `allocation_pct` and
  `trade_router.compute_fill_spill` stay the single source of entry size. The panel may
  *override* the dollar figure for a manual entry, exactly as `/api/manual/buy` already
  allows, and it may not invent a second sizing rule.

---

## 1. THE DOCTRINE COLLISION, AND WHY THIS IS ALLOWED ANYWAY

`trade_router.py` says it in capitals:

> **EXITS — ENGINE-DRIVEN ONLY. READ THIS.** There are NO price stops, trails, ratchets,
> premium monitors, or MACD-collapse exits in this file anymore.

That was a removal, not an omission. This spec puts two of those words back — *ratchets*
and, in effect, *price stops*. So it has to answer why.

The reason the old ones were removed is that they were **unrefereeable**. A premium
monitor that exits on a 20% drawdown in the option is not something `backtester.py` can
replay, because the backtest has no premium series — it has bars on the underlying. A knob
you cannot score is a knob you tune by vibe, and that is the thing this project
deliberately refuses (*tune the instrument, not the verdict*).

A ladder keyed to the **underlying price** is different in exactly the way that matters:
the backtester already holds every bar it needs to replay one. Rungs and a ratchet are
measurable. They can be swept the way the timeframe matrix was swept, and they can come
back and say *no*, as the timeframe matrix did. That is the whole licence.

**So the rule this spec adopts:** an exit rule may exist as configuration if and only if
`backtester.py` can replay it from bars alone. Anything keyed to premium, to the spread,
to an indicator the backtest does not compute, or to wall-clock latency stays out.

**And the constraint that follows:** §6 is not optional polish. If the ladder ships in the
live path before the backtester can score it, this spec has smuggled an unrefereeable knob
in through the front door while citing the rule that forbids it.

---

## 2. THE LADDER, AS DATA

One ladder per position per sleeve. Stored on the position record in
`%LOCALAPPDATA%\FoundationsTrading\positions.json`, which is already where open-position
state lives.

```
'ladder': {
  'sleeve': 'shares' | 'options',
  'anchor': 12.34,              # entry fill price on the UNDERLYING, pinned at fill
  'atr': 1.87,                  # ATR at fill, pinned; see below
  'rungs': [
    {'id': 'r1', 'qty': 4, 'at': {'kind': 'price', 'value': 512.50}, 'fired_at': None},
    {'id': 'r2', 'qty': 3, 'at': {'kind': 'atr',   'value': 1.5},    'fired_at': None},
  ],
  'ratchet': {'enabled': True, 'atr_mult': 1.0,
              'high_water': 509.80, 'floor': 507.93, 'armed_at': '...'},
  'remainder': 'engine',        # what happens to what the rungs do not sell
}
```

Five decisions are embedded there and each one is load-bearing.

**Rungs are keyed to the underlying, in one of two units.** `price` is an absolute level —
what you actually think when you look at a chart, and what `levels.py` hands you. `atr` is
a multiple of ATR from the anchor, which is the portable form: the same ladder shape can be
applied to SPY and to a $40 name without being retyped. Both resolve to a price before
anything is compared, so there is exactly one comparison in the code.

**`atr` is pinned at fill and never recomputed.** A rung that drifts because volatility
expanded is a rung you did not set. `levels.atr` already computes this, and `ATR_PERIOD = 14`
on prior daily bars is the existing convention in `backtest_context.py`; this reuses it
rather than introducing a second ATR.

**Quantities are absolute, not percentages.** You said ten shares, sell four, sell three.
Percentages of a position that a rung has already reduced are a rounding argument waiting
to happen, and on a 3-contract options position a percentage cannot express what you mean.
The panel may offer "half" as a *fill-in helper* that writes an integer.

**Rung quantities may sum to less than the position, and may not sum to more.** The
leftover is the runner, and `remainder` says who owns it: `'engine'` (default — the 5/12
ride-end and 34/50 structural exits still govern it, unchanged) or `'ratchet'` (the floor
in §3 closes it). A ladder whose rungs sum to the full position has no runner and the
engine never gets a say in that position's exit; that is allowed, and the panel says so in
words before you send it.

**`fired_at` is on the rung, not in a side log.** Idempotence is the whole problem with
partial exits: a rung that fires twice sells shares you no longer own and gets rejected —
or worse, sells into a position you have since re-entered. The rung carries its own state
and the check is `if rung['fired_at'] is None`.

---

## 3. THE RATCHET

```
floor = max(previous_floor, high_water - atr_mult * atr)
```

`high_water` is the highest **bar close** since the ladder armed, not the highest print.
Closes, because that is the resolution every other decision in this system is made at, and
because a wick-driven floor ratchets to a price that never existed as a close and then
stops you out on a normal pullback.

The floor only rises. That is the entire mechanic and it is why this is a ratchet and not a
stop: it cannot be walked down to give a losing position more room, which is the failure
mode a discretionary stop has.

On a bar whose close is at or below `floor`, the remainder exits with
`exit_kind='ratchet'`. Short positions mirror everything: `low_water`, floor descends only,
exit on a close at or above it.

**The ratchet does not set an initial stop.** Until `high_water - atr_mult * atr` clears
the entry, the floor sits below water and does nothing, and the 34/50 structural exit is
still the thing that says you were wrong. Two mechanisms competing to own the initial stop
is how you end up with neither of them being the reason you got out.

---

## 4. WHERE THIS IS EVALUATED, AND WHY NOT AT THE BROKER

Rungs and the ratchet are evaluated **in `bar_loop`, on bar close**, by a new pure module
`exit_ladder.py` that takes bars and a ladder and returns intents. No threads, no polling,
no broker-resting orders.

The ladder is evaluated on *every* closed bar, including the ones where the engine says
`NONE` — which is most of them, and all of the ones a scale-out cares about — because the
ratchet's high-water mark is a function of the bars and of nothing else. But the engine's
exit **takes precedence**: on a bar where it fires, pending rungs are voided and the
position closes whole in one order rather than racing two against each other (§8). So the
ladder runs first and acts second, which is not the same as running ahead of the engine.

The ladder also respects `_engine_enabled`. A ladder is unattended order placement, so the
master switch that means OBSERVE ONLY has to cover it or it does not mean what it says —
with the consequence that a ladder attached while the switch is off will not fire, and the
panel has to say so in words.

The obvious alternative is to rest limit orders at the broker. It was rejected for four
reasons, in ascending order of how much they cost:

1. **The plumbing is not there.** `alpaca_manager` places market orders only and has no
   `cancel_order` and no open-order query. Resting orders would mean adding all three.
   That is the cheapest objection and the least important.
2. **`TimeInForce.DAY`, against a system that holds overnight.** Ripster holds positions
   across sessions — `backtest_sweep` says so explicitly. DAY rungs evaporate at the bell
   while the position lives on, so the app would have to re-place them every morning,
   which means the ladder is *already* app-side state and the broker orders are only a
   projection of it. Two representations of one truth, and they disagree the first time a
   re-place fails.
3. **It races the engine.** The engine fires a structural exit as a market sell for the
   full remaining quantity. If rungs are resting for part of that quantity, the market sell
   is either rejected for overselling or fills and leaves orphan rungs that sell a position
   you no longer hold. Every resting rung would have to be cancelled before any engine
   exit, and the cancel would have to be *confirmed* before the sell — inside the bar loop,
   on a broker that can be slow. That is a new class of bug in the one path that must
   never fail.
4. **It splits the two sleeves.** A share rung can rest as a limit on the underlying. An
   options rung cannot: the ladder's levels are underlying prices and the order is on a
   contract, and converting one to the other needs delta, which moves. So options rungs
   would have to be app-side regardless. Resting the share rungs and not the options ones
   means the two sleeves of the same position exit at systematically different prices,
   which corrupts the sleeve attribution the journal exists to produce.

**The cost of evaluating on bar close, stated plainly:** a spike that runs through a rung
and retraces inside the same 10-min bar is missed, and a rung that is cleared fills at the
next bar's open rather than at the level. On a 10-min chart that give-back is real.

It is also **the cost this system has already accepted, in writing**, for every other exit:
*"A gap through a level exits on the NEXT bar close, not at the level. That is the accepted
design."* Paying it once more, consistently, is a smaller price than holding two exit
mechanisms with two different fill models and trying to compare their results later.

### 4.1 The faster tick — fenced off

`price_feed` already polls quotes at 1 second for the screener. Evaluating rungs against
that stream would fill much nearer the level without any broker order.

**It is not in this build**, for the same reason `03` §7 fences the arm path: the decision
should be taken against a measurement rather than in a moment of enthusiasm. The
measurement exists as soon as §6 lands — the backtest can report, per rung, the gap between
the rung price and the fill it actually got. If that give-back is material, this is the fix,
and the ladder's shape does not have to change to adopt it. If it is not material, a second
exit path has been avoided for free.

---

## 5. JOURNAL AND HEATMAP — ALREADY SOLVED, WITH ONE CAVEAT

**`pairing.py` needs no changes.** It is FIFO by quantity per symbol, and a ten-share entry
closed by fills of 4, 3 and 3 already produces three legs, each carrying that entry's grade
and its own exit fill and its own P/L. `reconcile()` still asserts that realized P/L plus
open cash flow equals the signed sum of every fill, so a ladder that creates or destroys
money fails loudly. This is the single biggest reason the feature is cheap: the hard part
was built before it was needed.

**`exit_kind` gains two values**, `'rung'` and `'ratchet'`, alongside `'structural'` and
`'ride_end'`. It is already in `journal.CONDITION_KEYS`, so the heatmap can layer on it the
day the first ladder fires, with no UI work: *what did scaling out actually do.*

**The caveat, and it touches the grading data.** Because one entry decision now produces
several legs, a laddered trade contributes several rows to the heatmap where an unladdered
one contributes one. Leg counts stop being decision counts. That is correct for money math
and misleading for anything that counts trades — including the population your grades are
read against. Two consequences:

- Any count the heatmap presents as *trades* must either de-duplicate on the entry fill id
  or be relabelled *legs*. This is adjacent to the unmerged `b31206f`
  ("Heatmap: segregate no-exit legs from graded exits") and the two should land together.
- The 382 hand-graded records predate this entirely and are unaffected. What must not
  happen is a future comparison that reads pre-ladder trade counts against post-ladder leg
  counts and concludes something about the strategy.

---

## 6. BACKTESTING THE LADDER — THE PART THAT MAKES THIS LEGITIMATE

`plan_exit(position, sleeve='both')` currently builds closing legs for a full exit. The
ladder needs partial exits, so the contract grows: `exit_ladder.evaluate(bars, ladder)`
returns zero or more `{'rung_id', 'qty', 'kind'}` intents, and `plan_exit` gains a `qty` so
it can close part of a position.

`backtester.py` then has to carry partial positions. Today a trade is opened and closed
whole; with a ladder, one entry produces several closes and the equity curve has to book
them as they happen. This is the real work in the feature, and it is the work that buys the
right to have the knob at all.

What the sweep should answer before any ladder becomes a default:

- Does a two-rung scale-out beat holding to the engine exit, on the same window and symbols
  the timeframe matrix used, with the RTH gate now in place?
- Does the ratchet help, or does it just convert winners into smaller winners? A ratchet
  that raises the win rate and lowers expectancy is the classic result and it is worth
  knowing before it is trusted.
- Per rung, the give-back between the rung price and the realized fill (§4.1's input).

**Note on baselines:** as of 2026-09-27 `signal_engine.ENTRY_END` closed the extended-hours
hole, so every number in `05-timeframe-matrix.md` is stale. The control for any ladder sweep
must be re-measured, not read off `05`.

### 6.1 FIRST MEASUREMENTS — 2026-09-27

Step 3 is built, so the sweep in §6 can run. SPY, 10-min, 2026-03-11 to 2026-09-19,
$10k per entry, 2bps slippage, post-`ENTRY_END`. Shares sleeve only — see the caveat at
the end, which matters more than any number above it.

**The scale is not what this spec implied.** Daily ATR on SPY over the window is **6.53**,
0.88% of price. The median per-trade excursion `|exit - entry|` is **0.82**, p90 is 2.85,
and only **3 of 209** trades ever travelled a full ATR. A rung at 1.0 ATR is roughly eight
times the typical trade's whole range, so it fires once in a six-month run. For this
strategy rungs belong at **0.1–0.5 ATR**, and the examples in §2 read as if whole multiples
were the natural unit. They are not.

**The scale-out raises the win rate and loses money.** Rungs at 0.25 / 0.50 ATR, 40% then
30% of the position:

| | control | 0.25/0.50 ATR rungs |
|---|---|---|
| P&L | **-206.89** | **-353.71** |
| win rate | 28.71% | **43.56%** |
| entries | 209 | 209 |
| legs | 209 | 264 (1.26/entry) |
| rung fires | — | 55 |

That is precisely the result §6 said to watch for, arriving on the first run: more winners,
worse expectancy. Taking 40% off at a quarter-ATR converts the few trades that would have
paid for the rest into small winners.

**The ratchet works and finds nothing.** Ratchet-only, sweeping the multiplier:

| ATR mult | distance | fires | entries | P&L |
|---|---|---|---|---|
| 0.01 | 0.07 | 189 | 289 | -737.83 |
| 0.02 | 0.13 | 179 | 288 | -613.11 |
| 0.05 | 0.33 | 117 | 269 | -703.22 |
| 0.10 | 0.65 | 42 | 229 | -251.60 |
| 0.20 | 1.31 | 6 | 210 | **-168.95** |
| 0.40 | 2.61 | 0 | 209 | -206.89 |
| none | — | 0 | 209 | -206.89 |

Fires ramp monotonically with tightness, which is how we know the mechanism works rather
than being silently inert at 0.40. P&L does **not** move monotonically, which is how we
know 0.20's -168.95 is noise and not an edge. Nothing here beats the control by a margin
that survives being looked at twice.

**A side effect worth carrying forward.** Entries rise from 209 to 289 as the ratchet
tightens, because a ratchet exit is not a structural stop and so never sets the same-day
no-rebuy flag. A tight ratchet is a churn multiplier as well as an exit rule, and the churn
is not visible in the exit statistics at all.

**The caveat that bounds all of it.** `backtester.py` models the SHARES sleeve only — it
sizes `int(budget // price)` and has no premium series. The live system trades combo: calls
plus shares long, puts only short. The timeframe matrix found essentially the entire
realized loss on the short side, which is the puts-only sleeve. So §1's licence is narrower
than it sounded: a ladder is refereeable **on shares**, and on options it is not refereeable
at all by this backtester. A ladder for the options sleeve would need either an options
pricing model in the replay or a live measurement, and until one exists the honest position
is that the options ladder is unmeasured.

---

## 7. THE ORDER PANEL

Launched from a screener card, and reachable from the positions list for an open position.
Pre-filled from the card: ticker, direction, and the anchor level the play is working off,
which becomes the natural first rung suggestion.

It shows: live price on the existing 1-second feed; size in dollars or shares, with the
`allocation_pct` default shown as what it would be; sleeve (`combo` / `shares` / `options`);
for options, the contract `trade_router.pick_contract` would choose, with its strike,
expiry, mid, spread and the Greeks `get_options_chain` already returns — displayed, not
chosen, because the locked spec in `trade_router` picks it and a hand-picked strike is a
second sizing rule; the ladder editor; and the resulting cash and remainder in words.

It sends to `/api/manual/buy`, which already accepts `{ticker, direction, sleeve, dollars}`
and already handles all three sleeves. The only additive change is an optional `ladder` in
the body, validated and pinned to the position after `confirm_fill` returns broker truth —
**never before**, because a ladder attached to an unfilled order is a ladder against a
position that does not exist.

**Pre-loading** is a saved, named ladder-plus-size template, per ticker, in `config.json`.
It fills the panel in. It does not send it. A template that sends itself is the arm path.

---

## 8. FAILURE MODES THIS MUST HANDLE

| case | required behaviour |
|---|---|
| a bar clears several rungs at once | they aggregate into ONE order per sleeve, not one rung per bar; serving them across bars fills the later ones further from their levels for nothing, and the constraint that matters is never having two unconfirmed partial exits in flight on one sleeve |
| rung qty exceeds what is actually held (manual sell in between) | clamp to the broker's position, log the discrepancy, mark the rung fired |
| broker rejects a rung sell | rung stays unfired, error to forensics, retried next close; never silently dropped |
| engine structural exit while rungs remain | engine wins, remaining rungs marked `void` with a reason, position closed whole |
| position closed manually | ladder is discarded with the position, not orphaned |
| app restarted mid-position | ladder is on the position record, so it reloads; `high_water` reloads with it |
| ladder on a position with zero remainder | rungs summing to the full position is legal; the engine simply never fires |
| re-entry into the same ticker same day | `rebuy_blocked` still governs; a new position starts with no ladder unless one is attached |

---

## 9. WHAT THIS SPEC DEPENDS ON, AND WHAT IT DOES NOT TOUCH

**The arm path stays fenced.** `03` §7 already specifies it: `armed` per assignment, a
process-global `AUTO_SEND_ENABLED` that never persists, `MAX_AUTO_SENDS_PER_DAY`,
`ARM_MIN_GPA = 4` measured on the trigger bar. Two of its preconditions are unmet today:
the environment GPA is **still unimplemented** (`screener.py` remains on `DEFAULT_WEIGHTS`),
so there is no 4 to gate on; and `_do_entry`'s pyramiding guard still reads
`source == 'engine'` where §7 says in bold it must read `source in ('engine', 'assigned')`
*before* the arm path is built. Pre-loaded templates in §7 of this spec are deliberately
inert so that neither of those is on this feature's critical path.

**Build order.** (1) `exit_ladder.py` as a pure module, with its own tests — bars and a
ladder in, intents out, no I/O. (2) Wire it into `bar_loop` ahead of the engine exit check,
and into `plan_exit` with a `qty`. (3) Partial positions in `backtester.py` and the sweep in
§6. (4) The panel. (5) Only then, with numbers in hand, §4.1 and the arm path.

Steps 1–3 before 4 is deliberate and it is the opposite of how this feature wants to be
built. The panel is the fun part and it is also the part that makes an unmeasured ladder
easy to start trading.
