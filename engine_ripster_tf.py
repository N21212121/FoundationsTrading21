"""
engine_ripster_tf.py - Foundations Trading

The Ripster system with the BAR SIZE made a parameter, and the entry bar size
made independent of the exit bar size. Experimental. `engine_ripster.py` and
`signal_engine.py` are untouched and remain the live path; nothing in this
file is imported by app.py.

The question this exists to answer, in the owner's words: "3min is great for
entry but 6min or 10min might be more appropriate for exit... SPY moves FAST
sometimes, so a quick exit on a lower timeframe could help reduce losses or
lock in profits." That is two parameters, not one, so this engine carries two.

─── THE TRAP THIS FILE IS MOSTLY ABOUT ───────────────────────────────────────

signal_engine's EMA periods are BAR COUNTS, not durations:

    EMA_FAST 5   EMA_SLOW 12   EMA_REGIME_A 34   EMA_REGIME_B 50

Shrink the bar and every cloud silently shrinks with it:

    cloud   on 10Min   on 6Min   on 3Min
    5          50 min    30 min    15 min
    12        120 min    72 min    36 min
    34         5.7 h      3.4 h     1.7 h
    50         8.3 h      5.0 h     2.5 h

The 34/50 is the HARD TREND GATE. Long-only above it, short-only below it, no
counter-trend entry ever. Run that gate on a 1.7-hour horizon instead of a
5.7-hour one and you have not made the Ripster system faster, you have built a
different system that happens to share its shape. Both are legitimate things
to test. Reporting them in one column is not.

So every timeframe is tested in two PERIOD MODES, and the mode is part of the
engine's name so it cannot be lost on the way to a table:

  'stock'   periods stay 5/12/34/50. Faster bars => genuinely shorter
            horizons. This is "what if the whole system were quicker".
  'scaled'  periods are multiplied by 10 / tf_minutes, preserving the
            wall-clock horizon each cloud measures. 3Min -> 17/40/113/167,
            6Min -> 8/20/57/83. This is "same system, sampled more often".

In `scaled` mode an asymmetric config is an almost pure experiment in
REACTION SPEED: the entry cloud and the exit cloud describe the same curves
over the same wall clock, and the only difference is how finely each one is
sampled and therefore how soon it can speak. In `stock` mode, speed and
horizon move together and the two effects are not separable. That is the
whole reason both modes are run.

ROUNDING RULE (a judgement call, stated)
  period' = floor(period * 10 / tf_minutes + 0.5), floored at 2.
  Plain multiplication of the SPAN, half away from zero so it is
  deterministic (Python's round() is half-to-even and would send 12.5 to 12).
  NOT DONE: matching the EMA's centre of mass instead, (N'-1)/2 * tf' =
  (N-1)/2 * 10, which would give 14/38/111/164 at 3Min rather than
  17/40/113/167. The two rules agree to within ~2% on the 34 and the 50, and
  disagree by ~20% on the 5. The span rule is the one specified for this
  experiment and the one whose arithmetic a reader can check in their head;
  the centre-of-mass rule is the more defensible one if this ever stops being
  an experiment. Either way the 5-period fast cloud is where the choice bites,
  and that should be remembered before any conclusion is drawn about entries.

WARMUP RULE (also a judgement call, stated)
  warmup_bars = 5 * slowest EMA period on that stream;
  history      = round(1.2 * warmup).
  At 10Min with stock periods that is 5 * 50 = 250 and 300 -- EXACTLY
  signal_engine.WARMUP_BARS and engine_ripster.history['primary']. That
  coincidence is why this rule was chosen over "preserve the wall-clock
  warm-up": it reproduces the live numbers with no special case, and warm-up
  exists so the EMA can converge, which is a property of bars, not of clock.
  CONSEQUENCE, and it is not a small one: in `stock` mode a 3Min engine warms
  up in 250 three-minute bars, which on this feed (extended hours, ~330 bars
  a session at 3Min) is well under one session. signal_engine._volume_ok
  needs PRIOR SESSIONS in the window to estimate ADV, and at 10Min/250 it has
  about two. A fast stock-period config can therefore be running on a
  one-session ADV, or none, in which case the volume gate fails closed and
  SHORTS are blocked. Read short counts in fast/stock cells with that in mind.

─── HOW TWO TIMEFRAMES FIT THROUGH ONE BACKTESTER ────────────────────────────

engine_contract says 'primary' is "the stream whose bar close drives
evaluation", and backtester.run_symbol loops over the primary bars and knows
exactly one other role, 'macro'. So:

  primary = the FASTER of (entry_tf, exit_tf)
  macro   = the slower one, declared only when they differ

Evaluation therefore ticks on the fast clock, which is the only way a fast
exit can be fast. The slow stream is consulted as STATE: whatever its most
recently closed bar says. backtester.run_symbol already advances its macro
pointer only while `bar_start + width <= bar_close`, so the engine is handed
closed slow bars only; this file re-checks that anyway against now_et,
because "no decision is ever made on a forming bar" is load-bearing in the
original and a live caller's fetch is not as careful as the replay's.

TWO RULES THE ASYMMETRY FORCES, both provably no-ops when entry_tf == exit_tf:

  1. EDGE-TRIGGERED ENTRY. `fresh_long` off a slow stream stays true for
     every fast bar until the slow bar rolls. Left alone, an exit on the fast
     stream would be followed by an instant re-entry on the next fast bar,
     over and over: a churn machine, and a very profitable-looking one in a
     simulation that pays only 2bps. So a slow-stream entry may fire only on
     the first primary bar at which a NEW entry-stream bar has closed. This
     is derived from timestamps, not remembered -- the engine stays stateless,
     which is what lets one instance serve a whole basket.
     No-op when the entry stream IS the primary stream.

  2. NO ENTRY INTO AN EXIT. If the exit stream already says "exit this
     direction", the entry is refused rather than taken and immediately
     given back. No-op in the symmetric case by construction: `fresh_long`
     requires close > top of the 5/12, the trend gate requires close > top of
     the 34/50, and `long_exit` requires close below the bottom of one of
     them. The two cannot both be true on one bar of one stream. So the
     control row is untouched by this rule, which is the point.

WHICH CLOUD BELONGS TO WHICH STREAM
  The 34/50 trend gate is an ENTRY rule (rule 1 gates rule 2), so it is read
  off the entry stream. The 34/50 structural stop is an EXIT rule (rule 3),
  so it is read off the exit stream. They are the same cloud only when the
  timeframes are the same. An asymmetric config can therefore hold a position
  whose entry-side gate has flipped -- that is not a bug, it is what "exit on
  a different timeframe" means, and it is exactly the behaviour under test.

ONE PERFORMANCE DEVIATION, AND IT IS A DIAGNOSTIC ONE
  Eighteen cells x ~40,000 three-minute bars is millions of evaluations, and
  two things in the original cost most of an evaluation: rebuilding a
  DataFrame with parsed timestamps, and the ADV scan inside
  signal_engine._volume_ok. Neither is needed on a bar where nothing happens.
  So the four rules run off a bare Series of CLOSES (which is all they read),
  and the volume gate is called only once an entry candidate has survived
  every other test. _volume_ok is a pure function of the bars and the clock,
  so the ACTION is identical either way; what differs is the `volume_ok`
  DIAGNOSTIC on bars with no entry, which is emitted as True, the contract's
  documented default. Stated, not hidden -- see _volume_gate.

WHAT IS NOT DONE HERE, ON PURPOSE
  - No 1-hour confirmation. signal_engine.REQUIRE_1H is False, so the live
    engine's declared 'macro' 1Hour stream is fetched and ignored. This
    engine SPENDS that role on the second bar size. If REQUIRE_1H is ever
    turned on, this file will refuse to construct (see __init__) rather than
    quietly drop a rule; restoring it needs a third role and a backtester
    that understands one, which is a prerequisite written up in
    design/05-timeframe-matrix.md, not a change made here.
  - No new exits, no stops, no targets, no per-timeframe tuning of
    OPEN_VOL_MIN_FRAC or ENTRY_START. Every constant that is not the bar
    size or the EMA periods is read live from signal_engine, so this variant
    tracks any edit to the real engine's spec instead of forking it.
  - No options model. Same limitation as backtester.py: this measures the
    directional signal on the underlying. See the spec for why that matters
    more here than usual.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import signal_engine as se
from engine_contract import StrategyEngine, validate_decision
import halflife as _hlf

ET = ZoneInfo('America/New_York')

# The reference bar size the periods in signal_engine were chosen for.
BASE_TF = '10Min'
BASE_TF_MINUTES = 10

# Periods, in the order (fast, slow, regime_a, regime_b), read live from
# signal_engine so an edit there propagates here.
BASE_PERIODS = (se.EMA_FAST, se.EMA_SLOW, se.EMA_REGIME_A, se.EMA_REGIME_B)

PERIOD_MODES = ('stock', 'scaled')

# warmup = WARMUP_MULT * slowest period; history = HISTORY_MULT * warmup.
# At 10Min/stock these reproduce signal_engine.WARMUP_BARS (250) and
# engine_ripster.history['primary'] (300) exactly. See the module docstring.
WARMUP_MULT = 5
HISTORY_MULT = 1.2


# ─── PARAMETER DERIVATION ─────────────────────────────────────────────────────

def tf_min(tf):
    """Minutes per bar. Parsed, never looked up -- '3Min' is not in
    halflife.TF_MINUTES' curated menu by design, but parses fine."""
    m = _hlf.tf_minutes(tf)
    if m is None:
        raise ValueError(f'unsupported timeframe: {tf!r}')
    return m


def _round_half_up(x):
    """Deterministic rounding. round() is half-to-even and would map a 12.5
    period to 12; this maps it to 13. Stated because a rounding rule that
    changes with the parity of the input is not a rounding rule."""
    import math
    return int(math.floor(x + 0.5))


def periods_for(tf, mode):
    """(fast, slow, regime_a, regime_b) for one stream.

    'stock'  -> BASE_PERIODS unchanged, whatever the bar size.
    'scaled' -> multiplied by 10 / tf_minutes, floored at 2 (an EMA of span 1
                is the price itself and a cloud of two such is not a cloud).
    """
    if mode not in PERIOD_MODES:
        raise ValueError(f'period_mode must be one of {PERIOD_MODES}, '
                         f'got {mode!r}')
    if mode == 'stock':
        return tuple(BASE_PERIODS)
    k = BASE_TF_MINUTES / float(tf_min(tf))
    return tuple(max(2, _round_half_up(p * k)) for p in BASE_PERIODS)


def warmup_for(periods):
    """Closed bars required before this stream may say anything."""
    return int(WARMUP_MULT * max(periods))


def history_for(periods):
    """Bars the data layer should deliver for this stream."""
    return int(_round_half_up(HISTORY_MULT * warmup_for(periods)))


def horizon_minutes(tf, periods):
    """Wall-clock minutes each cloud spans, for the spec's tables. This is
    the number the matrix exists to keep honest."""
    m = tf_min(tf)
    return tuple(p * m for p in periods)


# ─── CLOUD CONTEXT, WITH THE PERIODS MADE ARGUMENTS ──────────────────────────

def context(df, periods):
    """signal_engine.trend_context, with the four periods passed in. Takes a
    DataFrame, like the original, so --selftest can put the two side by side.
    The engine's hot path calls context_closes() instead; see there."""
    return context_closes(df['close'], periods)


def context_closes(c, periods):
    """The body of trend_context, over a bare Series of closes.

    Every one of the four rules is a function of CLOSES ALONE. The original
    takes a DataFrame because the caller already had one. Building one per
    evaluation costs a pd.to_datetime over the whole window -- ~6ms at a
    300-bar window, more at 1,002 -- for timestamps nothing in this function
    reads. A matrix of eighteen cells over 40,000 three-minute bars cannot
    afford it, and does not have to: the timestamps are still parsed, but
    only on the bars where something actually needs them (the volume gate).

    Line-for-line the same arithmetic. It is duplicated rather than imported
    because signal_engine reads its periods from module constants, and the
    one thing this file must never do is reach in and rebind them: that would
    make the live engine's behaviour depend on whether an experiment happened
    to be imported. backtest_sweep.py --selftest asserts that this function
    and se.trend_context agree bar for bar when periods == BASE_PERIODS, on
    real data, which is the only assurance worth having that the copy has not
    drifted.
    """
    p_fast, p_slow, p_a, p_b = periods
    e5s, e12s = se.ema(c, p_fast), se.ema(c, p_slow)
    e34s, e50s = se.ema(c, p_a), se.ema(c, p_b)

    e5, e12 = float(e5s.iloc[-1]), float(e12s.iloc[-1])
    e34, e50 = float(e34s.iloc[-1]), float(e50s.iloc[-1])
    cur = float(c.iloc[-1])             # most recently CLOSED bar
    prev = float(c.iloc[-2])
    p_e5, p_e12 = float(e5s.iloc[-2]), float(e12s.iloc[-2])

    f_top, f_bot = max(e5, e12), min(e5, e12)
    r_top, r_bot = max(e34, e50), min(e34, e50)
    pf_top, pf_bot = max(p_e5, p_e12), min(p_e5, p_e12)

    if cur > r_top:
        trend = 'up'
    elif cur < r_bot:
        trend = 'down'
    else:
        trend = 'chop'

    cur_above, prev_above = cur > f_top, prev > pf_top
    cur_below, prev_below = cur < f_bot, prev < pf_bot
    fresh_long = cur_above and not prev_above
    fresh_short = cur_below and not prev_below

    long_exit = (cur < f_bot) or (cur < r_bot)
    short_exit = (cur > f_top) or (cur > r_top)

    if fresh_long:
        trig = 'fresh_long'
    elif fresh_short:
        trig = 'fresh_short'
    else:
        trig = 'none'

    def _vs(price, lo, hi):
        return 'above' if price > hi else ('below' if price < lo else 'inside')

    return {
        'trend': trend, 'fresh_long': fresh_long, 'fresh_short': fresh_short,
        'long_exit': long_exit, 'short_exit': short_exit,
        'result': f'{trend}/{trig}', 'tally': {},
        'price_vs_5_12': _vs(cur, f_bot, f_top),
        'price_vs_34_50': _vs(cur, r_bot, r_top),
        'emas': {'e5': round(e5, 4), 'e12': round(e12, 4),
                 'e34': round(e34, 4), 'e50': round(e50, 4)},
        'closes': {'cur': round(cur, 4), 'prev': round(prev, 4)},
        'clouds': {'fast': [round(f_bot, 4), round(f_top, 4)],
                   'regime': [round(r_bot, 4), round(r_top, 4)]},
    }


# ─── THE ENGINE ───────────────────────────────────────────────────────────────

def engine_name(entry_tf, exit_tf, mode):
    """Stable registry id. The mode is IN THE NAME so a row of a results
    table cannot be read without it."""
    return (f'ripster_tf_e{tf_min(entry_tf)}m_'
            f'x{tf_min(exit_tf)}m_{mode}')


class RipsterTFEngine(StrategyEngine):
    """Ripster with (entry_tf, exit_tf, period_mode) as construction
    parameters. Stateless, like every engine here: one instance serves a
    whole basket, and everything it needs to know about time it derives from
    the timestamps it is handed."""

    execution = 'options_combo'      # unchanged: calls+shares long, puts short

    def __init__(self, entry_tf=BASE_TF, exit_tf=BASE_TF, period_mode='stock'):
        if se.REQUIRE_1H:
            # The 'macro' role is spent on the second bar size. Refusing is
            # the only honest option; silently dropping rule 4's optional
            # 1-hour confirmation would make this variant a different
            # strategy than the one it claims to vary.
            raise RuntimeError(
                'signal_engine.REQUIRE_1H is True; this variant spends the '
                "'macro' role on the second bar size and cannot also carry "
                'the 1-hour stream. See design/05-timeframe-matrix.md.')

        self.entry_tf = entry_tf
        self.exit_tf = exit_tf
        self.period_mode = period_mode

        e_min, x_min = tf_min(entry_tf), tf_min(exit_tf)
        self.symmetric = (entry_tf == exit_tf)

        # primary = the faster stream, because primary's bar close drives
        # evaluation and a fast exit that ticks on a slow clock is not fast.
        if e_min <= x_min:
            fast_tf, slow_tf = entry_tf, exit_tf
        else:
            fast_tf, slow_tf = exit_tf, entry_tf
        self.primary_tf = fast_tf
        self.slow_tf = None if self.symmetric else slow_tf

        self.entry_role = 'primary' if entry_tf == fast_tf else 'macro'
        self.exit_role = 'primary' if exit_tf == fast_tf else 'macro'

        self.periods = {entry_tf: periods_for(entry_tf, period_mode),
                        exit_tf: periods_for(exit_tf, period_mode)}
        self.entry_periods = self.periods[entry_tf]
        self.exit_periods = self.periods[exit_tf]

        self.timeframes = {'primary': self.primary_tf}
        self.history = {'primary': history_for(periods_for(self.primary_tf,
                                                           period_mode))}
        self.warmup = {'primary': warmup_for(periods_for(self.primary_tf,
                                                         period_mode))}
        if not self.symmetric:
            self.timeframes['macro'] = self.slow_tf
            slow_p = periods_for(self.slow_tf, period_mode)
            self.history['macro'] = history_for(slow_p)
            self.warmup['macro'] = warmup_for(slow_p)

        self.name = engine_name(entry_tf, exit_tf, period_mode)
        self.is_control = (entry_tf == BASE_TF and exit_tf == BASE_TF
                           and period_mode == 'stock')

    # -- description helpers, used by the sweep's table ------------------------

    def label(self):
        return (f'entry {self.entry_tf} / exit {self.exit_tf} / '
                f'{self.period_mode}')

    def describe(self):
        """Everything a reader needs to know that the name does not say."""
        return {
            'name': self.name, 'label': self.label(),
            'entry_tf': self.entry_tf, 'exit_tf': self.exit_tf,
            'period_mode': self.period_mode,
            'primary_tf': self.primary_tf, 'macro_tf': self.slow_tf,
            'entry_periods': self.entry_periods,
            'exit_periods': self.exit_periods,
            'entry_horizon_min': horizon_minutes(self.entry_tf,
                                                 self.entry_periods),
            'exit_horizon_min': horizon_minutes(self.exit_tf,
                                                self.exit_periods),
            'warmup': dict(self.warmup), 'history': dict(self.history),
            'is_control': self.is_control,
        }

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _closed_only(bars, tf_minutes, now_et):
        """Drop any trailing bar that has not finished. The replay already
        guarantees this; a live fetch does not, and a forming bar deciding
        anything is the failure mode signal_engine was written to avoid."""
        if not bars or now_et is None:
            return bars
        w = timedelta(minutes=tf_minutes)
        out = bars
        while out:
            t = datetime.fromisoformat(out[-1]['time'])
            if t.astimezone(ET) + w > now_et:
                out = out[:-1]
            else:
                break
        return out

    @staticmethod
    def _time_gate(now_et):
        """The session-window half of signal_engine.launch_gate. Cheap, read
        live from se.ENTRY_START/se.ENTRY_END so it cannot drift from the real
        spec. Half-open [start, end): see the note at signal_engine.ENTRY_END
        for why a bar closing at 16:00 is out and one closing at 09:30 is in."""
        return se.ENTRY_START <= now_et.time() < se.ENTRY_END

    @staticmethod
    def _volume_gate(raw_primary, now_et):
        """The volume half, DEFERRED until an entry actually depends on it.

        se._volume_ok verbatim -- the pace test is not reimplemented, because
        it is subtle and it is right. Only the timing of the call changed.

        WHY IT IS EVALUATED ON THE PRIMARY (FASTEST) STREAM in every config,
        entry stream or not: volume is a property of the tape, not of a
        cloud, and se._volume_ok compares volume closed so far against
        minutes elapsed so far. Feed it a slow stream while now_et ticks on a
        fast one and you compare an up-to-date denominator against a
        numerator up to one slow bar stale, reading low by construction --
        the exact bug the pace test was written to fix when ENTRY_START moved
        to 09:30. It is otherwise bar-size invariant: the numerator sums
        whole bars inside 09:30-10:00, the ADV denominator sums whole
        sessions, both are the same shares however they are diced, and at
        every bar size the closed volume covers exactly `elapsed` minutes.

        WHY DEFERRING IS SAFE, precisely: _volume_ok is a pure function of
        the bars and the clock, so calling it later in the same evaluation
        cannot change its answer. backtester.run_symbol reads
        decision['volume_ok'] only when the action is an entry, and live it
        gates OPTIONS on an entry. On a bar with no entry candidate the value
        is therefore never consulted -- so it is emitted as True, the
        contract's documented default for engines that do not compute it.
        The ACTION is identical either way. The DIAGNOSTIC differs on
        non-entry bars, and that is the whole of the deviation. It is stated
        rather than hidden because if this variant is ever mounted live and
        app.py's signal log is read for the volume_ok column, that column
        will be uninformative on quiet bars.
        """
        df = se.bars_to_df(raw_primary)
        return se._volume_ok(df, now_et)

    def _entry_bar_just_closed(self, raw_entry, now_et):
        """True when a NEW entry-stream bar closed within the last primary
        bar. Rule 1 of the two the asymmetry forces; see the module docstring.
        Always True when the entry stream is the primary stream."""
        if self.entry_role == 'primary':
            return True
        end = (datetime.fromisoformat(raw_entry[-1]['time']).astimezone(ET)
               + timedelta(minutes=tf_min(self.entry_tf)))
        return (now_et - end) < timedelta(minutes=tf_min(self.primary_tf))

    # -- the contract ----------------------------------------------------------

    def evaluate(self, ticker, bars, position_direction=None, now_et=None,
                 position=None):
        """One decision at a PRIMARY bar close. Same four rules, same strict
        priority as signal_engine.evaluate: exits first and never gated, then
        entries, hard-gated by the 34/50.

        `position` is unused: Ripster's exits are structural, never timed.
        """
        decision = {'ticker': ticker, 'action': 'NONE', 'gate': None,
                    'step2': None, 'step2_exit': None, 'step3': None,
                    'flip': None, 'exit_kind': None, 'exit_tag': None,
                    'volume_ok': True,
                    'config': self.name}

        raw_p = bars.get('primary') if isinstance(bars, dict) else bars
        raw_m = bars.get('macro') if isinstance(bars, dict) else None

        # Cheap warmup rejection BEFORE any DataFrame is built. The replay
        # evaluates every bar from the second one, exactly as the live loop
        # does, so most calls in a run are this one and it must be free.
        need_p = self.warmup['primary']
        if not raw_p or len(raw_p) < need_p:
            n = 0 if not raw_p else len(raw_p)
            decision['gate'] = {'passed': False, 'volume_ok': False,
                                'reason': f'warming up primary '
                                          f'({n}/{need_p} {self.primary_tf})'}
            return validate_decision(decision)
        if not self.symmetric:
            need_m = self.warmup['macro']
            if not raw_m or len(raw_m) < need_m:
                n = 0 if not raw_m else len(raw_m)
                decision['gate'] = {'passed': False, 'volume_ok': False,
                                    'reason': f'warming up macro '
                                              f'({n}/{need_m} {self.slow_tf})'}
                return validate_decision(decision)

        now = now_et or datetime.now(ET)
        raw_p = self._closed_only(raw_p, tf_min(self.primary_tf), now_et)
        if len(raw_p) < need_p:
            decision['gate'] = {'passed': False, 'volume_ok': False,
                                'reason': 'warming up primary (post-trim)'}
            return validate_decision(decision)

        if not self.symmetric:
            raw_m = self._closed_only(raw_m, tf_min(self.slow_tf), now_et)
            if len(raw_m) < self.warmup['macro']:
                decision['gate'] = {'passed': False, 'volume_ok': False,
                                    'reason': 'warming up macro (post-trim)'}
                return validate_decision(decision)

        raw_by_role = {'primary': raw_p, 'macro': raw_m}

        def _closes(role):
            return pd.Series([b['close'] for b in raw_by_role[role]],
                             dtype='float64')

        ctx_entry = context_closes(_closes(self.entry_role),
                                   self.entry_periods)
        ctx_exit = (ctx_entry if self.exit_role == self.entry_role
                    else context_closes(_closes(self.exit_role),
                                        self.exit_periods))
        decision['step2'] = ctx_entry          # the "regime" line, as before
        decision['step2_exit'] = (None if ctx_exit is ctx_entry else ctx_exit)

        time_ok = self._time_gate(now)
        gate = {'passed': time_ok, 'volume_ok': True,
                'reason': ('gate open' if time_ok else
                           f'outside entry window '
                           f'{se.ENTRY_START.strftime("%H:%M")}-'
                           f'{se.ENTRY_END.strftime("%H:%M")} ET '
                           f'(bar closed {now.strftime("%H:%M")})')}
        decision['gate'] = gate

        held = position_direction

        # --- RULE 3: exits, off the EXIT stream's closed bar, never gated ---
        exit_action = exit_kind = None
        x_bot, x_top = ctx_exit['clouds']['regime']
        x_cur = ctx_exit['closes']['cur']
        if held == 'long' and ctx_exit['long_exit']:
            exit_action = 'EXIT_LONG'
            exit_kind = 'structural' if x_cur < x_bot else 'ride_end'
        elif held == 'short' and ctx_exit['short_exit']:
            exit_action = 'EXIT_SHORT'
            exit_kind = 'structural' if x_cur > x_top else 'ride_end'
        decision['exit_kind'] = exit_kind
        # exit_tag groups a report by WHICH stream ended the trade. The router
        # never branches on it; backtester only appends it to exit_reason.
        decision['exit_tag'] = (f'{self.exit_tf}' if exit_action else None)
        decision['flip'] = {'flip': exit_action is not None,
                            'close_direction': held if exit_action else None}

        # --- RULES 1+2: entry, off the ENTRY stream, gated by its 34/50 ---
        entry_dir = None
        if gate['passed'] and self._entry_bar_just_closed(
                raw_by_role[self.entry_role], now):
            if ctx_entry['trend'] == 'up' and ctx_entry['fresh_long'] \
                    and held != 'long':
                entry_dir = 'long'
            elif ctx_entry['trend'] == 'down' and ctx_entry['fresh_short'] \
                    and held != 'short':
                entry_dir = 'short'

            # Rule 2 of the two the asymmetry forces: never enter into a
            # direction the exit stream is already telling you to leave.
            # Provably a no-op when entry and exit share a stream.
            if entry_dir == 'long' and ctx_exit['long_exit']:
                entry_dir = None
            elif entry_dir == 'short' and ctx_exit['short_exit']:
                entry_dir = None

            # Rule 4's volume half, computed only now that an entry actually
            # depends on it. See _volume_gate for why the deferral is safe.
            if entry_dir:
                vol = self._volume_gate(raw_p, now)
                gate['volume_ok'] = vol['ok']
                gate['reason'] = 'gate open; ' + vol['reason']
                gate['open_vol_frac'] = vol.get('frac')
                decision['volume_ok'] = vol['ok']

        # --- compose, same order as signal_engine.evaluate ---
        if exit_action and entry_dir and entry_dir != held:
            decision['action'] = f'{exit_action}_THEN_ENTER_{entry_dir.upper()}'
        elif exit_action:
            decision['action'] = exit_action
        elif entry_dir and held is None:
            decision['action'] = f'ENTER_{entry_dir.upper()}'

        return validate_decision(decision)

    def characterize(self, ticker, bars):
        """Same statistic and same verdict as engine_ripster: does this name
        actually trend? MEASURES, never selects. Note it is computed on the
        PRIMARY bars, so the verdict is about this bar size, not about the
        name in the abstract -- a name can trend at 10 minutes and be a
        random walk at 3."""
        primary = bars.get('primary') if isinstance(bars, dict) else bars
        if not primary:
            return {'verdict': 'unknown', 'reason': 'no bars'}
        cls = _hlf.classify(primary)
        cls['tradeable'] = cls['verdict'] == 'trending'
        cls['timeframe'] = self.primary_tf
        return cls


# ─── THE MATRIX, AND REGISTRATION ────────────────────────────────────────────

MATRIX_TFS = ('3Min', '6Min', '10Min')
CONTROL = (BASE_TF, BASE_TF, 'stock')     # the live configuration


def matrix_configs(tfs=MATRIX_TFS, modes=PERIOD_MODES):
    """Every (entry_tf, exit_tf, mode) cell, control first.

    Control first is not cosmetic: every other cell is only meaningful
    relative to it, and a table read top-down should hit the baseline before
    it hits anything that could be mistaken for a result.
    """
    cells = [CONTROL]
    for mode in modes:
        for e in tfs:
            for x in tfs:
                if (e, x, mode) != CONTROL:
                    cells.append((e, x, mode))
    return cells


def build(entry_tf, exit_tf, period_mode):
    return RipsterTFEngine(entry_tf, exit_tf, period_mode)


def build_matrix(tfs=MATRIX_TFS, modes=PERIOD_MODES):
    return [build(*c) for c in matrix_configs(tfs, modes)]


def register_all(registry=None, tfs=MATRIX_TFS, modes=PERIOD_MODES):
    """Add every matrix variant to the registry, skipping names already
    there. NEVER touches 'ripster_ema_cloud': these are additions, and the
    live default stays the faithful engine.

    Default target is engines_bootstrap.registry, so `import
    engine_ripster_tf` is enough to make the existing tooling see the
    variants in-process. It is NOT enough for backtester.run_basket_parallel,
    whose workers re-import engines_bootstrap in a fresh process and would
    not see them -- which is why backtest_sweep.py parallelises across
    CONFIGS itself and calls the serial run_basket. Wiring these into the
    shared bootstrap permanently is a one-line prerequisite written up in
    design/05-timeframe-matrix.md, not a change made from here.
    """
    if registry is None:
        from engines_bootstrap import registry as registry_
        registry = registry_
    existing = set(registry.names())
    added = []
    for eng in build_matrix(tfs, modes):
        if eng.name in existing:
            continue
        registry.register(eng)
        added.append(eng.name)
    return added


try:                     # import-time registration, best effort
    register_all()
except Exception:        # a broken/absent bootstrap must not break an import
    pass


if __name__ == '__main__':
    print(f'{"engine":34} {"primary":8} {"macro":8} '
          f'{"entry periods":>22} {"exit periods":>22}  warmup')
    for eng in build_matrix():
        d = eng.describe()
        mark = '  <- CONTROL (live config)' if d['is_control'] else ''
        print(f"{d['name']:34} {d['primary_tf']:8} "
              f"{str(d['macro_tf'] or '-'):8} "
              f"{str(d['entry_periods']):>22} {str(d['exit_periods']):>22}  "
              f"{d['warmup']}{mark}")
    print()
    print('cloud horizons in wall-clock minutes (entry stream):')
    for eng in build_matrix():
        d = eng.describe()
        print(f"  {d['label']:44} {d['entry_horizon_min']}")
