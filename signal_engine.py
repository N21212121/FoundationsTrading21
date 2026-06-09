"""
signal_engine.py — Foundations Trading

The Ripster decision tree (Layer 3 of the spec), as code.

HARDCODED CONSTANTS BY DESIGN. Stops/gates/windows are module constants,
not config. Changing one means editing this file and committing.

Pure logic: takes bar data in, returns decisions out. No broker calls,
no file I/O, no threads. The caller owns all of that.

Decision flow per 10-min bar close:
  1. Launch Gate  (10:00 ET pause + RVOL floor + candle-ratio gate)
  2. Step 2       (three votes: L / H / S)
  3. Step 3       (1-hour macro confirmation)
  plus signal-flip detection against any open position.
"""

import pandas as pd
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# ─── ENGINE CONSTANTS (the spec — edit + commit to change) ────────────────────

EMA_FAST = 5
EMA_SLOW = 12
EMA_REGIME_A = 34
EMA_REGIME_B = 50

WARMUP_BARS = 250            # min closed 10-min bars before any signal
ENTRY_START = dtime(10, 0)   # no entries before 10:00:00 ET
RVOL_MIN = 0.90              # today's 9:30→now vol ≥ 90% of yesterday's same window
CANDLE_OVERLAP_MAX = 0.85    # block if last two closed bodies overlap ≥ 85%


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def bars_to_df(bars):
    """List of bar dicts (alpaca_manager format) -> DataFrame with ET times.
    Returns None if empty."""
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df['time'] = pd.to_datetime(df['time'], utc=True).dt.tz_convert(ET)
    return df.reset_index(drop=True)


def _body_overlap_frac(bar_a, bar_b):
    """Overlap of two candle BODIES as a fraction of the smaller body.
    Zero-height bodies (open == close) count as full overlap if inside
    the other body's range — degenerate, treat as blocked-level overlap."""
    a_lo, a_hi = sorted((bar_a['open'], bar_a['close']))
    b_lo, b_hi = sorted((bar_b['open'], bar_b['close']))
    overlap = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    smaller = min(a_hi - a_lo, b_hi - b_lo)
    if smaller <= 0:
        # doji body: if it sits inside the other body, that's full retrace
        inside = (b_lo <= a_lo <= b_hi) or (a_lo <= b_lo <= a_hi)
        return 1.0 if inside else 0.0
    return overlap / smaller


# ─── LAUNCH GATE ───────────────────────────────────────────────────────────────

def launch_gate(df10, now_et=None):
    """All three conditions required for ANY new entry (calls, puts, shares).

    Returns {'passed': bool, 'reason': str, plus diagnostic fields}.
    Fails closed on missing data.
    """
    now_et = now_et or datetime.now(ET)

    # ── 1. 30-min opening pause ──
    if now_et.time() < ENTRY_START:
        return {'passed': False,
                'reason': f'opening pause: no entries before 10:00 ET '
                          f'(now {now_et.strftime("%H:%M:%S")})'}

    if df10 is None or len(df10) < WARMUP_BARS:
        n = 0 if df10 is None else len(df10)
        return {'passed': False,
                'reason': f'warming up: {n}/{WARMUP_BARS} bars'}

    # ── 2. RVOL floor ──
    today = now_et.date()
    sessions = sorted({t.date() for t in df10['time']})
    prior_days = [d for d in sessions if d < today]
    if not prior_days:
        return {'passed': False, 'reason': 'RVOL: no prior session data (fail closed)'}
    yesterday = prior_days[-1]

    cutoff = now_et.time()
    open_t = dtime(9, 30)

    def _window_vol(d):
        mask = (df10['time'].dt.date == d) & \
               (df10['time'].dt.time >= open_t) & \
               (df10['time'].dt.time < cutoff)
        return float(df10.loc[mask, 'volume'].sum())

    vol_today = _window_vol(today)
    vol_prior = _window_vol(yesterday)
    if vol_prior <= 0:
        return {'passed': False,
                'reason': f'RVOL: no prior-day volume in window (fail closed)'}
    rvol = vol_today / vol_prior
    if rvol < RVOL_MIN:
        return {'passed': False,
                'reason': f'RVOL {rvol:.2f} < {RVOL_MIN} '
                          f'(today {vol_today:,.0f} vs prior {vol_prior:,.0f})',
                'rvol': round(rvol, 3)}

    # ── 3. Candle-ratio gate (two most recently CLOSED bars) ──
    b1 = df10.iloc[-1]   # most recently closed
    b2 = df10.iloc[-2]   # the one before it
    frac = _body_overlap_frac(b1, b2)
    if frac >= CANDLE_OVERLAP_MAX:
        return {'passed': False,
                'reason': f'candle ratio: body overlap {frac:.0%} >= '
                          f'{CANDLE_OVERLAP_MAX:.0%} (retrace pair)',
                'overlap': round(frac, 3)}

    return {'passed': True, 'reason': 'all gates passed',
            'rvol': round(rvol, 3), 'overlap': round(frac, 3)}


# ─── STEP 2: THE 5/12 REGIME (three votes) ────────────────────────────────────

def step2_votes(df10, current_open):
    """Three sub-signals vote L / H / S.

    2a: current (forming) bar's OPEN vs the 5/12 EMA pair
    2b: 5/12 cloud vs 34/50 cloud
    2c: previous CLOSED bar's close vs EMA12

    EMAs computed on closed bars only. Returns
    {'votes': {'2a','2b','2c'}, 'tally': {'L','H','S'}, 'result': str,
     'direction': 'long'|'short'|None}
    where result is one of AUTO / ADVANCE / NONE.
    """
    c = df10['close']
    e5 = float(ema(c, EMA_FAST).iloc[-1])
    e12 = float(ema(c, EMA_SLOW).iloc[-1])
    e34 = float(ema(c, EMA_REGIME_A).iloc[-1])
    e50 = float(ema(c, EMA_REGIME_B).iloc[-1])

    fast_lo, fast_hi = min(e5, e12), max(e5, e12)
    reg_lo, reg_hi = min(e34, e50), max(e34, e50)

    # 2a — current bar's open vs EMA5/EMA12
    if current_open > e5:
        v2a = 'L'
    elif current_open < e12:
        v2a = 'S'
    else:
        v2a = 'H'

    # 2b — fast cloud vs regime cloud (both EMAs clear of the cloud)
    if fast_lo > reg_hi:
        v2b = 'L'
    elif fast_hi < reg_lo:
        v2b = 'S'
    else:
        v2b = 'H'

    # 2c — previous closed bar's close vs EMA12
    prev_close = float(c.iloc[-1])
    if prev_close > e12:
        v2c = 'L'
    elif prev_close < e12:
        v2c = 'S'
    else:
        v2c = 'H'

    votes = {'2a': v2a, '2b': v2b, '2c': v2c}
    tally = {'L': sum(1 for v in votes.values() if v == 'L'),
             'H': sum(1 for v in votes.values() if v == 'H'),
             'S': sum(1 for v in votes.values() if v == 'S')}

    if tally['L'] == 3:
        result, direction = 'AUTO', 'long'
    elif tally['S'] == 3:
        result, direction = 'AUTO', 'short'
    elif tally['L'] == 2:
        result, direction = 'ADVANCE', 'long'
    elif tally['S'] == 2:
        result, direction = 'ADVANCE', 'short'
    else:
        result, direction = 'NONE', None

    return {'votes': votes, 'tally': tally, 'result': result,
            'direction': direction,
            'emas': {'e5': round(e5, 4), 'e12': round(e12, 4),
                     'e34': round(e34, 4), 'e50': round(e50, 4)},
            'current_open': current_open, 'prev_close': prev_close}


# ─── STEP 3: WHEELIN' & DEALIN' (1-hour macro confirmation) ───────────────────

def step3_macro(df1h, direction):
    """Last CLOSED 1-hour bar's close vs the 1-hour 34/50 EMA cloud.

    Returns {'state': 'above'|'below'|'chop', 'confirmed': bool, 'reason': str}.
    Fails closed if hourly data is thin.
    """
    if df1h is None or len(df1h) < EMA_REGIME_B * 3:
        n = 0 if df1h is None else len(df1h)
        return {'state': 'unknown', 'confirmed': False,
                'reason': f'insufficient hourly bars ({n})  (fail closed)'}

    c = df1h['close']
    e34 = float(ema(c, EMA_REGIME_A).iloc[-1])
    e50 = float(ema(c, EMA_REGIME_B).iloc[-1])
    lo, hi = min(e34, e50), max(e34, e50)
    px = float(c.iloc[-1])

    if px > hi:
        state = 'above'
    elif px < lo:
        state = 'below'
    else:
        state = 'chop'

    confirmed = (state == 'above' and direction == 'long') or \
                (state == 'below' and direction == 'short')
    return {'state': state, 'confirmed': confirmed,
            'reason': f'1h close {px:.2f} vs cloud [{lo:.2f}, {hi:.2f}] '
                      f'= {state}; direction {direction} '
                      f'{"CONFIRMED" if confirmed else "rejected"}'}


# ─── SIGNAL FLIP ───────────────────────────────────────────────────────────────

def detect_signal_flip(step2, open_position_direction):
    """An opposing 3-vote forces the exit of the open sleeve.

    open_position_direction: 'long', 'short', or None.
    Returns {'flip': bool, 'close_direction': str|None}.
    """
    if open_position_direction is None or step2['result'] != 'AUTO':
        return {'flip': False, 'close_direction': None}
    if step2['direction'] != open_position_direction:
        return {'flip': True, 'close_direction': open_position_direction}
    return {'flip': False, 'close_direction': None}


# ─── TOP-LEVEL EVALUATION ──────────────────────────────────────────────────────

def evaluate(ticker, bars10, bars1h, current_open,
             open_position_direction=None, now_et=None):
    """One full decision-tree pass at a 10-min bar close.

    Args:
      ticker: str
      bars10: list of 10-min bar dicts (alpaca_manager format), closed bars
      bars1h: list of 1-hour bar dicts, closed bars
      current_open: price at evaluation moment (the just-opened bar's open;
                    caller feeds a fresh quote)
      open_position_direction: 'long' | 'short' | None
      now_et: override clock for testing

    Returns a decision dict ready for log_signal + the caller's action switch:
      action: 'ENTER_LONG' | 'ENTER_SHORT' | 'EXIT_LONG' | 'EXIT_SHORT'
              | 'EXIT_LONG_THEN_ENTER_SHORT' | 'EXIT_SHORT_THEN_ENTER_LONG'
              | 'NONE'
    """
    df10 = bars_to_df(bars10)
    df1h = bars_to_df(bars1h)

    decision = {'ticker': ticker, 'action': 'NONE',
                'gate': None, 'step2': None, 'step3': None, 'flip': None}

    # Step 2 runs regardless of the gate, because signal-flip exits are not
    # gated — only ENTRIES are. A 3-vote opposing flip must fire even at 9:45.
    if df10 is None or len(df10) < WARMUP_BARS:
        decision['gate'] = {'passed': False,
                            'reason': f'warming up '
                                      f'({0 if df10 is None else len(df10)}/{WARMUP_BARS})'}
        return decision

    step2 = step2_votes(df10, current_open)
    decision['step2'] = step2

    flip = detect_signal_flip(step2, open_position_direction)
    decision['flip'] = flip

    gate = launch_gate(df10, now_et=now_et)
    decision['gate'] = gate

    # ── Exits from flips happen regardless of gate ──
    if flip['flip']:
        exit_action = 'EXIT_LONG' if flip['close_direction'] == 'long' else 'EXIT_SHORT'
        # Can the same bar also open the new direction? Only if gate passes
        # and Step 3 confirms.
        if gate['passed']:
            step3 = step3_macro(df1h, step2['direction'])
            decision['step3'] = step3
            if step3['confirmed']:
                decision['action'] = (exit_action + '_THEN_ENTER_' +
                                      step2['direction'].upper())
                return decision
        decision['action'] = exit_action
        return decision

    # ── Entries require the gate ──
    if not gate['passed']:
        return decision

    if step2['result'] in ('AUTO', 'ADVANCE'):
        step3 = step3_macro(df1h, step2['direction'])
        decision['step3'] = step3
        if step3['confirmed']:
            decision['action'] = 'ENTER_' + step2['direction'].upper()

    return decision
