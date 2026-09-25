"""
engine_ou.py - Foundations Trading

Engine #2. Ornstein-Uhlenbeck mean reversion on the deviation from a slow
reference. The opposite thesis to Ripster: Ripster buys strength and rides;
this fades stretch and waits for the snap-back.

THE FOUR RULES

  1. CHARACTER GATE. The name must read 'reverting' on a Lo-MacKinlay
     variance ratio AND 'stable' on the half-life estimator, on the same
     window the trade is taken from. A trending name, or one whose spring is
     noise, is not traded. Abstention is a first-class answer.

  2. ENTRY. s-score past +/- ENTRY_S. s < -1.25 is cheap -> long. s > +1.25
     is rich -> short. No confirmation bar, no cross, no volume gate. The
     deviation IS the signal.

  3. EXIT (profit). |s| < EXIT_S. Not zero: the last half-sigma takes as long
     as the first two and is mostly noise. Known before entry.

  4. STOPS. Three of them, and NONE is a price level.

     A price stop on a reversion trade is systematically wrong. Enter short at
     s = +2, watch it go to +3, and the model likes the trade MORE, not less:
     dx/dt = -theta*x, so a bigger deviation means a stronger expected pull.
     The position losing money is the position with more edge. A conventional
     stop sells you out of your best entries, every time.

     What replaces it:

     TIME STOP. The model does not merely claim reversion, it claims a rate.
     Held TIME_STOP_HALFLIVES half-lives with the deviation still open? Then
     ~87% of it should have decayed and did not. Not "hasn't yet" -- failed.
     Exit at the clock, whatever the P&L.

     INVALIDATION STOP. Refit each bar. If the name stops reading 'stable' or
     'reverting', or the sigma consistency check breaks, the spring is gone.
     You are not stopping out of a trade; you are stopping out of a MODEL.

     CIRCUIT BREAKER. |s| >= BREAKER_S. A five-sigma deviation under a
     Gaussian is once in millions. Seeing one means the likelier explanation
     is that the model is broken, not that the trade is spectacular. Fraud,
     halt, buyout, restatement: something changed the company and the
     reference now describes a stock that no longer exists. This stop has
     NEGATIVE expected value inside the model. Take it anyway. It is
     insurance against the model, and the model is sometimes wrong.

THE FREE CONSISTENCY CHECK
  sigma_eq = sigma_eps / sqrt(1 - b^2) is what the OU model says the band
  width must be. std(x) is what the band width actually is. Under a true OU
  those agree. When they diverge, the process is not OU and the s-score's
  denominator is meaningless -- so the gate fails. One line, and it catches
  model mismatch that the half-life alone would sail straight past.

EXECUTION: SHARES ONLY, and this is part of the strategy, not a preference.
  The expected move is one or two sigma of a residual that is often 1-2%
  wide, over a few half-lives. Buy calls for that and theta plus IV crush eat
  the edge before it arrives -- and you would be buying premium precisely
  after a spike, which is when IV is richest. Mount this on the options
  router and the router kills it before the strategy gets to fail honestly.

WHAT THIS ENGINE DOES NOT DO
  It does not hedge. The deviation is measured against the name's own EMA,
  not against market and sector factors. Every position therefore carries
  full beta. A market drawdown hits every name in the book at once, in the
  same direction -- which is exactly the correlated failure that "hundreds of
  small independent bets" was supposed to prevent. This is a naive
  mean-reversion band system with the OU math done honestly, NOT stat-arb.
  Read the backtest with that in mind. The hedge is the next build, not this
  one.

Pure logic. Bars in, decision out. No broker calls, no I/O, no threads.

HARDCODED CONSTANTS BY DESIGN - edit + commit to change.
"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import halflife as hlf
from engine_contract import StrategyEngine, validate_decision

ET = ZoneInfo('America/New_York')

# --- ENGINE CONSTANTS (the spec - edit + commit to change) -------------------

# THE REFERENCE AND THE LAG BOTH SCALE WITH THE HALF-LIFE. This is not a
# refinement, it is the difference between the gate working and the gate
# refusing every slow spring in your universe.
#
#   The reference must be ~10 half-lives. Shorter and the EMA absorbs the very
#   deviation it is supposed to expose: at REF_SPAN=100 a 30-bar spring reads
#   sigma_ratio 1.35, i.e. the observed band no longer matches sigma/sqrt(2t)
#   and the process is no longer OU in the coordinates we measured it in.
#
#   The variance-ratio lag must be ~1 half-life. VR(k) has power only when k
#   is on the order of the reversion time; at k=5 a true 30-bar spring reads
#   |z|=1.31 and gets thrown out as a random walk.
#
# Neither is known before the fit, so the fit is iterated. Two passes converge.
REF_SPAN_MULT = 10           # reference EMA span, in half-lives
REF_SPAN_MIN, REF_SPAN_MAX = 40, 300
VR_LAG_MIN, VR_LAG_MAX = 2, 20
FIT_PASSES = 2
HL_SEED = 10.0               # starting guess, in bars

FIT_WINDOW = 250             # AR(1) fit window, in primary bars
WARMUP_BARS = 800            # FIT_WINDOW + REF_SPAN_MAX, plus enough
                             # extra for the VR test to have power

# Windows for the stability check inside the gate. These must be sized to the
# history THIS engine carries (see `history` below), not to halflife.py's
# defaults, which are tuned for 10-min bars and would leave only one surviving
# window on a daily series -- and one window is a point, not a trend, so the
# gate would read 'unstable' on every name forever.
GATE_WINDOWS = (FIT_WINDOW // 2, FIT_WINDOW)

ENTRY_S = 1.25               # |s| past this opens
EXIT_S = 0.50                # |s| inside this closes at a profit
BREAKER_S = 5.0              # |s| past this is a model failure, not a gift

TIME_STOP_HALFLIVES = 3.0    # ~87% of the deviation should have decayed

MIN_HL_BARS = 2.0            # faster than this, costs eat it

# The upper bound is set by STATISTICAL POWER, not by preference and not by
# the reference span. The variance ratio's standard error is
#     sqrt( 2(2k-1)(k-1) / (3kn) )
# and the lag k must track the half-life for the test to have any power at
# all. So the error grows like sqrt(k/n): certifying a slow spring costs data
# quadratically. Measured on synthetic OU with n=1100 daily bars, a true
# half-life of 8 reads |z|=2.6 (certified) and a true half-life of 15 reads
# |z|=1.9 (refused, correctly, as indistinguishable from a random walk).
# Certifying a 15-bar half-life on daily bars needs roughly a decade of
# history. It is not available, so the engine does not pretend.
#
# Raising this constant does not buy you slower springs. It buys you springs
# you cannot prove exist. Raise `history` instead, and re-measure.
MAX_HL_BARS = 12.0
SIGMA_MISMATCH_MAX = 1.5     # observed vs model-implied band width

# SHORTS ARE OFF BY DEFAULT.
# Flipping this to True requires (a) the shares-short path in trade_router,
# which exists, and (b) a margin account that permits short sales, and (c)
# your acceptance of unbounded loss on the short leg. The engine emits no
# ENTER_SHORT while this is False, so BACKTEST AND LIVE STAY IN AGREEMENT --
# the backtest will show you the long-only strategy you are actually running,
# not a short-enabled one you are not.
ALLOW_SHORTS = False


# --- HELPERS -----------------------------------------------------------------

def _closed_bars(bars, tf_minutes, now_et):
    """Drop a forming bar, for any timeframe.

    Alpaca hands back the in-progress bar for the current period. A daily bar
    at 11am is a partial day; a 30-min bar at 10:15 is half a bar. Evaluating
    on either is a lookahead bug wearing a disguise: the same bar will have a
    different close later. A bar is closed only once its start + width has
    passed. Everything in this engine keys off CLOSED bars."""
    if not bars:
        return bars
    now_et = now_et or datetime.now(ET)
    try:
        last = datetime.fromisoformat(bars[-1]['time'])
    except (ValueError, KeyError):
        return bars
    if last.tzinfo is None:
        last = last.replace(tzinfo=ET)
    close_time = last.astimezone(ET) + timedelta(minutes=tf_minutes)
    return bars[:-1] if close_time > now_et else bars


def _bars_held(bars, position, now_et):
    """How many primary bars since the position opened.

    The engine holds no state. The caller hands over `opened_at`; the engine
    counts bars in the window that closed after it. Returns None when the
    entry time is unknown or unparseable, and the caller must then treat the
    time stop as unavailable rather than as satisfied. Fail closed."""
    if not position or not bars:
        return None
    raw = position.get('opened_at') or position.get('entry_time')
    if not raw:
        return None
    try:
        t0 = datetime.fromisoformat(str(raw).replace(' ', 'T'))
    except ValueError:
        return None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=ET)      # app.py stores naive ET strings
    n = 0
    for b in reversed(bars):
        try:
            bt = datetime.fromisoformat(b['time'])
        except (ValueError, KeyError):
            break
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=ET)
        if bt <= t0:
            break
        n += 1
    return n


def _ref_span_for(hl):
    return int(min(REF_SPAN_MAX, max(REF_SPAN_MIN, round(REF_SPAN_MULT * hl))))


def _vr_lag_for(hl):
    return int(min(VR_LAG_MAX, max(VR_LAG_MIN, round(hl))))


def _fit(logc):
    """AR(1) on the deviation from the slow reference, with the reference span
    ITERATED to ~10 half-lives.

    A fixed span cannot serve a universe of springs with different rates: too
    short and it eats the deviation, too long and it stops tracking the drift.
    Two passes from a seed guess converge; the span-drift check inside
    halflife.estimate() independently verifies the answer is not an artifact
    of the span we chose.

    Returns a dict, or None on thin data."""
    hl = HL_SEED
    out = None
    for _ in range(FIT_PASSES):
        out = _fit_once(logc, _ref_span_for(hl))
        if out is None:
            return None
        hl = out['half_life']
    return out


def _fit_once(logc, ref_span):
    if len(logc) < FIT_WINDOW + ref_span + 1:
        return None
    ref = pd.Series(logc).ewm(span=ref_span, adjust=False).mean().to_numpy()
    x = logc - ref
    x = x[ref_span:]                     # drop the reference's own warmup
    if len(x) < FIT_WINDOW + 1:
        return None

    xs = x[-(FIT_WINDOW + 1):]
    lag, cur = xs[:-1], xs[1:]
    lag_c = lag - lag.mean()
    denom = float(np.dot(lag_c, lag_c))
    if denom <= 0:
        return None
    b = float(np.dot(lag_c, cur - cur.mean()) / denom)
    a = float(cur.mean() - b * lag.mean())
    # Kendall small-sample correction; see halflife.py for why it is not
    # optional at b near 1.
    b = b + (1.0 + 3.0 * b) / FIT_WINDOW
    if b <= 0.0 or b >= 1.0:
        return None

    theta = -math.log(b)
    hl = math.log(2.0) / theta
    mu = a / (1.0 - b)

    eps = cur - (a + b * lag)
    sigma_eps = float(np.std(eps, ddof=2))
    sigma_model = sigma_eps / math.sqrt(1.0 - b * b) if b * b < 1 else None
    sigma_obs = float(np.std(xs, ddof=1))
    if not sigma_obs or sigma_obs <= 0 or not sigma_model or sigma_model <= 0:
        return None
    ratio = max(sigma_obs, sigma_model) / min(sigma_obs, sigma_model)

    return {'x_now': float(xs[-1]), 'mu': mu,
            'sigma_obs': sigma_obs, 'sigma_model': sigma_model,
            'sigma_ratio': ratio, 'b': b, 'theta': theta, 'half_life': hl,
            'ref_span': ref_span, 'vr_lag': _vr_lag_for(hl), 'x': x}


def _gate(fit, bars_window):
    """Rule 1. Every reason a name is untradeable, in one place.
    Returns (passed: bool, reason: str, extra: dict)."""
    if fit is None:
        return False, ('no valid AR(1) fit: unit root, trending, or thin '
                       'data (b outside (0,1))'), {}

    # The lag is chosen from the fitted half-life: VR(k) only has power when
    # k is on the order of the reversion time.
    cls = hlf.classify(bars_window, k=fit['vr_lag'])
    if cls['verdict'] != 'reverting':
        return False, f"character: {cls['verdict']} ({cls['reason']})", cls

    hl_bars = fit['half_life']
    if hl_bars < MIN_HL_BARS:
        return False, f'half-life {hl_bars:.1f} < {MIN_HL_BARS} bars: ' \
                      f'too fast to trade after costs', cls
    if hl_bars > MAX_HL_BARS:
        return False, f'half-life {hl_bars:.1f} > {MAX_HL_BARS} bars: ' \
                      f'capital held too long', cls
    if fit['sigma_ratio'] > SIGMA_MISMATCH_MAX:
        return False, (f"sigma mismatch {fit['sigma_ratio']:.2f} > "
                       f'{SIGMA_MISMATCH_MAX}: observed band width disagrees '
                       f'with sigma/sqrt(2*theta); process is not OU'), cls

    # bars_per_day_=False: this runs once per bar in the replay, and that one
    # field costs more than every fit in this function combined.
    est = hlf.estimate(bars_window, windows=GATE_WINDOWS, bars_per_day_=False)
    if est['stability'] != 'stable':
        return False, (f"half-life {est['stability']} "
                       f"(spread {est['spread_ratio']}, "
                       f"drift {est['span_drift']})"), cls

    return True, (f"reverting, hl={hl_bars:.1f} bars, "
                  f"sigma_ratio={fit['sigma_ratio']:.2f}, stable"), cls


# --- ENGINE -------------------------------------------------------------------

class _OUBase(StrategyEngine):
    """The OU engine, parameterised only by bar size.

    A NOTE ON WHAT DOES AND DOES NOT SCALE. I expected this family to need
    per-timeframe constants. It does not, and the reason is worth stating.

    Every constant in this module is expressed in BARS, not in clock time:
    WARMUP_BARS, FIT_WINDOW, REF_SPAN_*, MIN/MAX_HL_BARS, TIME_STOP_HALFLIVES.
    Statistical power depends on the NUMBER of bars, not on how much wall time
    they span. So a 30-minute engine with 1,200 bars has exactly the same
    competence band, the same variance-ratio power, and the same estimator
    bias as a daily engine with 1,200 bars.

    What changes is the REAL TIME those bars represent, and therefore which
    stocks the engine can see. A half-life of 2 to 12 bars is:
        1Day  -> 2 to 12 sessions
        1Hour -> ~0.15 to 0.9 sessions
        30Min -> ~0.08 to 0.46 sessions
    Three windows onto the same competence band. That is the entire family.

    What ALSO changes, and is not free, is COST. A 30-minute engine holding
    13 bars turns over ~13x more often than a daily one holding 13 bars, and
    the residual it harvests is a fraction of a percent. Turnover multiplies
    spread. These engines can pass every statistical test in this system and
    still lose money net of costs. Read backtester's breakeven_slippage_bps
    before you deploy any of them; on the fast end it is the number that
    decides whether the sleeve exists at all.
    """
    name = 'ou_reversion'
    timeframes = {'primary': '1Day'}
    # The variance-ratio test spends data to buy power; shrink this and the
    # gate quietly stops certifying anything but the fastest springs.
    history = {'primary': 1200}
    execution = 'shares_only'

    def characterize(self, ticker, bars):
        """Does a spring exist on this name, and is it a spring at all?
        MEASURES. Never selects."""
        primary = bars.get('primary') if isinstance(bars, dict) else bars
        if not primary:
            return {'verdict': 'unknown', 'reason': 'no bars'}
        fit = _fit(np.log(np.array([b['close'] for b in primary],
                                   dtype=float)))
        k = fit['vr_lag'] if fit else 5
        cls = hlf.classify(primary, k=k)
        est = hlf.estimate(primary, windows=GATE_WINDOWS)
        return {
            'verdict': cls['verdict'], 'reason': cls['reason'],
            'vr': cls['vr'], 'z': cls['z'],
            'half_life_bars': est['median_hl_bars'],
            'hl_se': est['hl_se'], 'rel_se': est['rel_se'],
            'stability': est['stability'],
            'spread_ratio': est['spread_ratio'],
            'span_drift': est['span_drift'],
            'sigma_dev': est['sigma_dev'],
            'vr_lag': k,
            'ref_span': fit['ref_span'] if fit else None,
            'sigma_ratio': round(fit['sigma_ratio'], 2) if fit else None,
            'tradeable': (cls['verdict'] == 'reverting'
                          and est['stability'] == 'stable'),
        }

    def evaluate(self, ticker, bars, position_direction=None, now_et=None,
                 position=None):
        primary = _closed_bars(bars.get('primary'),
                               hlf.TF_MINUTES[self.timeframes['primary']],
                               now_et)

        d = {'ticker': ticker, 'action': 'NONE', 'gate': None, 'step2': None,
             'step3': None, 'flip': None, 'exit_kind': None, 'volume_ok': True}

        if not primary or len(primary) < WARMUP_BARS:
            n = 0 if not primary else len(primary)
            d['gate'] = {'passed': False,
                         'reason': f'warming up ({n}/{WARMUP_BARS})'}
            return validate_decision(d)

        logc = np.log(np.array([b['close'] for b in primary], dtype=float))
        if np.any(~np.isfinite(logc)):
            d['gate'] = {'passed': False, 'reason': 'bad prices'}
            return validate_decision(d)

        fit = _fit(logc)
        passed, reason, cls = _gate(fit, primary)

        held = position_direction
        s = None
        if fit is not None:
            s = (fit['x_now'] - fit['mu']) / fit['sigma_obs']

        # step2 is the "regime" line the signal log renders. Keep the same
        # key names the Ripster log uses where they carry the same meaning,
        # so one CSV holds both engines without a schema fork.
        d['step2'] = {
            'trend': (cls or {}).get('verdict', ''),
            'result': f"s={s:.2f}" if s is not None else '',
            's_score': round(s, 3) if s is not None else None,
            'vr': (cls or {}).get('vr'), 'z': (cls or {}).get('z'),
            'half_life_bars': round(fit['half_life'], 1) if fit else None,
            'sigma_ratio': round(fit['sigma_ratio'], 2) if fit else None,
            'ref_span': fit['ref_span'] if fit else None,
            'vr_lag': fit['vr_lag'] if fit else None,
            'sigma_obs': round(fit['sigma_obs'], 5) if fit else None,
            'fresh_long': False, 'fresh_short': False,
            'price_vs_5_12': '', 'price_vs_34_50': '',
        }
        d['gate'] = {'passed': passed, 'reason': reason, 'volume_ok': True}

        # ── RULE 4 + 3: exits. Never gated. Order matters: the breaker and
        # the invalidation stop must fire even when the profit target has not
        # been hit, and especially when the gate has just failed. ──
        exit_action = exit_kind = exit_why = exit_tag = None
        if held and (fit is None or s is None):
            # The fit collapsed: b left (0,1), meaning the series no longer has
            # a mean to revert to. There is no s-score, so none of the ladder
            # below can run. Without this branch a held position would be
            # carried FOREVER on a name whose model has completely failed --
            # the exact scenario the invalidation stop exists for.
            exit_kind, exit_tag = 'structural', 'model_invalidated'
            exit_why = f'model invalidated: {reason}'
            exit_action = 'EXIT_LONG' if held == 'long' else 'EXIT_SHORT'
        elif held and s is not None:
            held_bars = _bars_held(primary, position, now_et)
            d['step2']['bars_held'] = held_bars

            if abs(s) >= BREAKER_S:
                exit_kind, exit_tag = 'structural', 'circuit_breaker'
                exit_why = f'circuit breaker |s|={abs(s):.2f}'
            elif not passed:
                exit_kind, exit_tag = 'structural', 'model_invalidated'
                exit_why = f'model invalidated: {reason}'
            elif (held_bars is not None
                  and held_bars >= TIME_STOP_HALFLIVES * fit['half_life']):
                exit_kind, exit_tag = 'structural', 'time_stop'
                exit_why = (f'time stop: {held_bars} bars held >= '
                            f"{TIME_STOP_HALFLIVES}x half-life "
                            f"({fit['half_life']:.1f})")
            elif abs(s) <= EXIT_S:
                exit_kind, exit_tag = 'ride_end', 'profit_target'
                exit_why = f'reverted to |s|={abs(s):.2f}'
            # A position whose sign flipped past the far entry threshold has
            # overshot through the mean; close it and let the entry logic
            # below decide whether the other side is worth taking.
            elif ((held == 'long' and s >= ENTRY_S)
                  or (held == 'short' and s <= -ENTRY_S)):
                exit_kind, exit_tag = 'ride_end', 'overshoot'
                exit_why = f'overshot to s={s:.2f}'

            if exit_kind:
                exit_action = 'EXIT_LONG' if held == 'long' else 'EXIT_SHORT'

        d['exit_kind'] = exit_kind
        # Stable machine tag for grouping in reports; `flip.reason` carries the
        # human sentence. Without this, exits_by_reason grows one bucket per
        # trade because the reason string embeds the s-score.
        d['exit_tag'] = exit_tag
        d['flip'] = {'flip': exit_action is not None,
                     'close_direction': held if exit_action else None,
                     'reason': exit_why}

        # ── RULE 2: entries. Gated by character, stability, and sigma sanity. ──
        entry_dir = None
        if passed and s is not None and abs(s) < BREAKER_S:
            if s <= -ENTRY_S and held != 'long':
                entry_dir = 'long'
            elif s >= ENTRY_S and held != 'short' and ALLOW_SHORTS:
                entry_dir = 'short'
            elif s >= ENTRY_S and not ALLOW_SHORTS:
                d['gate'] = {'passed': False, 'volume_ok': True,
                             'reason': f'{reason}; short signal suppressed '
                                       f'(ALLOW_SHORTS=False)'}

        if exit_action and entry_dir and entry_dir != held:
            d['action'] = f'{exit_action}_THEN_ENTER_{entry_dir.upper()}'
        elif exit_action:
            d['action'] = exit_action
        elif entry_dir and held is None:
            d['action'] = f'ENTER_{entry_dir.upper()}'

        return validate_decision(d)


# --- THE FAMILY ---------------------------------------------------------------
# Same logic, same constants, different sampling rate. Each one sees a
# different slice of the universe, because a half-life of 2-12 BARS is a
# different amount of real time at each bar size.
#
# Assignment is deterministic and never looks at P&L: see assign.py. Do not
# let a screener try all three and keep the best. Eleven timeframes at
# alpha=0.05 is a 43% chance of at least one false positive per name; across
# a 2,786-name universe that is over a thousand phantom springs, each of which
# will backtest beautifully.


class OUReversion2Hour(_OUBase):
    name = 'ou_reversion_2h'
    timeframes = {'primary': '2Hour'}
    history = {'primary': 1200}


class OUReversion4Hour(_OUBase):
    name = 'ou_reversion_4h'
    timeframes = {'primary': '4Hour'}
    history = {'primary': 1200}


class OUReversionDaily(_OUBase):
    name = 'ou_reversion_1d'
    timeframes = {'primary': '1Day'}
    history = {'primary': 1200}         # ~4.8 years of sessions


class OUReversionHourly(_OUBase):
    name = 'ou_reversion_1h'
    timeframes = {'primary': '1Hour'}
    history = {'primary': 1200}         # ~75 sessions of extended-hours bars


class OUReversion30Min(_OUBase):
    name = 'ou_reversion_30m'
    timeframes = {'primary': '30Min'}
    history = {'primary': 1200}         # ~37 sessions


# Back-compat: the original registry name. Same engine as the daily variant.
class OUReversionEngine(OUReversionDaily):
    name = 'ou_reversion'


# Bands must OVERLAP, or a stock whose half-life lands between two engines has
# no home. 30m: .06-.38  1h: .13-.75  2h: .25-1.5  4h: .5-3.0  1d: 2-12
# sessions. Contiguous coverage from ~3 minutes to ~2.4 weeks of reversion.
FAMILY = (OUReversion30Min, OUReversionHourly, OUReversion2Hour,
          OUReversion4Hour, OUReversionDaily)
