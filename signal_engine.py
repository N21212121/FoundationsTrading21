"""
signal_engine.py - Foundations Trading

The Ripster EMA Cloud system, faithfully. Four rules, in strict priority:

  1. TREND GATE (34/50 cloud, 10-min): price ABOVE the cloud = long-only,
     BELOW = short-only, INSIDE = chop (no entries). This is a HARD GATE,
     not a vote. Nothing goes long below the 34/50; nothing shorts above it.

  2. ENTRY TRIGGER (5/12 cloud, 10-min): in the trend direction, a 10-min
     candle CLOSES across the 5/12 cloud (the cross / bounce). Confirmed by
     close, not by a forming bar.

  3. EXIT (10-min close):
       - candle closes back under the 5/12 cloud (the ride is over), OR
       - price closes through the 34/50 cloud (you were wrong = structural stop)
     Either one closes the position.

  4. GATE: entries allowed from ENTRY_START until ENTRY_END -- regular hours
     only. Volume gates OPTIONS only, as
     a PACE test: volume so far in the 9:30-10:00 window must be on pace
     for OPEN_VOL_MIN_FRAC of ADV by 10:00 (prorated by elapsed time, so it
     works while the window is still filling). 1-hour confirmation is
     available but OFF by default, to measure the core strategy cleanly.

Pure logic. Bars in, decision out. No broker calls, no I/O, no threads.

This is a deliberate simplification of the prior 3-vote engine, which let the
34/50 be outvoted (allowing counter-trend entries), had no 5/12 ride-exit, and
no structural 34/50 stop. Those three gaps were the loss profile. This file
removes them by making the document's hierarchy the code's hierarchy.
"""

import pandas as pd
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# --- ENGINE CONSTANTS (the spec - edit + commit to change) -------------------

EMA_FAST = 5
EMA_SLOW = 12
EMA_REGIME_A = 34
EMA_REGIME_B = 50

WARMUP_BARS = 250            # min closed 10-min bars before any signal
ENTRY_START = dtime(9, 30)   # EXPERIMENT: pause OFF, entries from the open (was 10:00)
ENTRY_END = dtime(16, 0)     # no entries on a bar closing at or after the bell

# Why ENTRY_END exists, and why the window is HALF-OPEN [09:30, 16:00).
# now_et is the bar's CLOSE time, and a fill happens at the NEXT bar's open.
#   - A bar closing at 09:30 is the 09:20-09:30 pre-market bar; it fills at the
#     09:30 open, which is the opening drive ENTRY_START was moved here to
#     catch. So 09:30 is IN.
#   - A bar closing at 16:00 is the 15:50-16:00 bar; it would fill at the next
#     bar's open, i.e. after the bell. So 16:00 is OUT.
# Live was never exposed to this -- bar_loop checks alpaca.is_market_open()
# before evaluating anything -- but the SIP feed the backtester replays covers
# 04:00-20:00 ET, and with only a lower bound the replay entered on
# extended-hours bars: 31% of AAPL entries and 46% of SPY entries in the
# 2026-08 sweep. This is the producer-side fix for that, and it supersedes
# backtest_sweep.RTHGate(mode='entries'), which patched the same hole
# replay-side so the control cell could stay bit-identical to live.
# Exits are deliberately NOT gated: launch_gate only ever guards entries, so a
# position is never trapped behind the bell.

# Volume gate (Ripster): a stock that trades a big share of its average daily
# volume in the first 30 minutes is having a trend day. The original rule
# compared the COMPLETED 9:30-10:00 volume to the threshold, which was sound
# only while entries were paused until 10:00. With ENTRY_START at 9:30 the
# window is still filling when entries go live, so the raw ratio read low by
# construction and blocked options in exactly the opening-drive window this
# strategy targets. The rule is therefore a PACE test now: volume so far in
# the window must be >= OPEN_VOL_MIN_FRAC * ADV * (elapsed / 30 min). From
# 10:00 onward that is arithmetically identical to the original rule. ADV is
# approximated from the prior days present in the 10-min history already in
# hand (no extra data feed needed).
REQUIRE_VOLUME = True        # volume gates OPTIONS only (see evaluate/volume_ok)
OPEN_VOL_MIN_FRAC = 0.20     # first-30-min vol must be >= 20% of avg daily vol
OPEN_WINDOW_END = dtime(10, 0)   # first-30-min window is 9:30 -> 10:00 ET

REQUIRE_1H = False           # if True, require 1-hour 34/50 to agree with entry


# --- HELPERS -----------------------------------------------------------------

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


# --- TREND CONTEXT: the 34/50 gate + 5/12 trigger/exit, all off closed bars --

def trend_context(df10):
    """Compute everything the four rules need from CLOSED 10-min bars.

    Returns a dict with the trend (vs 34/50), fresh 5/12 crosses, and exit
    flags. All decisions are made on the most recently CLOSED bar, never a
    forming bar.
    """
    c = df10['close']
    e5s, e12s = ema(c, EMA_FAST), ema(c, EMA_SLOW)
    e34s, e50s = ema(c, EMA_REGIME_A), ema(c, EMA_REGIME_B)

    e5, e12 = float(e5s.iloc[-1]), float(e12s.iloc[-1])
    e34, e50 = float(e34s.iloc[-1]), float(e50s.iloc[-1])
    cur = float(c.iloc[-1])             # most recently closed bar
    prev = float(c.iloc[-2])            # the bar before it
    p_e5, p_e12 = float(e5s.iloc[-2]), float(e12s.iloc[-2])

    f_top, f_bot = max(e5, e12), min(e5, e12)        # 5/12 cloud
    r_top, r_bot = max(e34, e50), min(e34, e50)      # 34/50 cloud
    pf_top, pf_bot = max(p_e5, p_e12), min(p_e5, p_e12)

    # RULE 1 - trend gate (hard): which side of the 34/50 cloud is price on?
    if cur > r_top:
        trend = 'up'
    elif cur < r_bot:
        trend = 'down'
    else:
        trend = 'chop'

    # RULE 2 - fresh 5/12 cross (close crossing the cloud this bar)
    cur_above, prev_above = cur > f_top, prev > pf_top
    cur_below, prev_below = cur < f_bot, prev < pf_bot
    fresh_long = cur_above and not prev_above
    fresh_short = cur_below and not prev_below

    # RULE 3 - exits: close back under 5/12 OR close through the 34/50
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
    price_vs_512 = _vs(cur, f_bot, f_top)
    price_vs_3450 = _vs(cur, r_bot, r_top)
    result = f'{trend}/{trig}'

    return {
        'trend': trend, 'fresh_long': fresh_long, 'fresh_short': fresh_short,
        'long_exit': long_exit, 'short_exit': short_exit,
        'result': result, 'tally': {},   # tally kept empty for log compatibility
        'price_vs_5_12': price_vs_512, 'price_vs_34_50': price_vs_3450,
        'emas': {'e5': round(e5, 4), 'e12': round(e12, 4),
                 'e34': round(e34, 4), 'e50': round(e50, 4)},
        'closes': {'cur': round(cur, 4), 'prev': round(prev, 4)},
        'clouds': {'fast': [round(f_bot, 4), round(f_top, 4)],
                   'regime': [round(r_bot, 4), round(r_top, 4)]},
    }


# --- OPTIONAL GATE -----------------------------------------------------------

def launch_gate(df10, now_et=None):
    """Session window + warmup (always), RVOL (only if REQUIRE_RVOL).

    Returns {'passed': bool, 'reason': str, ...}. Fails closed on thin data.
    """
    now_et = now_et or datetime.now(ET)

    if now_et.time() < ENTRY_START:
        return {'passed': False, 'volume_ok': False,
                'reason': f'opening pause: no entries before '
                          f'{ENTRY_START.strftime("%H:%M")} ET '
                          f'(now {now_et.strftime("%H:%M:%S")})'}

    if now_et.time() >= ENTRY_END:
        return {'passed': False, 'volume_ok': False,
                'reason': f'after hours: no entries from '
                          f'{ENTRY_END.strftime("%H:%M")} ET '
                          f'(now {now_et.strftime("%H:%M:%S")})'}

    if df10 is None or len(df10) < WARMUP_BARS:
        n = 0 if df10 is None else len(df10)
        return {'passed': False, 'volume_ok': False,
                'reason': f'warming up: {n}/{WARMUP_BARS} bars'}

    # Warmup + pause passed -> entries are allowed. Volume no longer blocks the
    # entry; it only decides whether OPTIONS may be traded automatically. When
    # volume is below threshold, volume_ok=False and the caller restricts an
    # engine entry to SHARES (longs). Shorts (puts-only) then can't trade.
    vol = _volume_ok(df10, now_et)
    return {'passed': True, 'volume_ok': vol['ok'],
            'reason': 'gate open; ' + vol['reason'],
            'open_vol_frac': vol.get('frac')}


def _volume_ok(df10, now_et):
    """Opening-volume PACE vs ADV, prorated over the 9:30->10:00 window.

    required = OPEN_VOL_MIN_FRAC * min(elapsed, 30) / 30. Before 10:00 the
    numerator only contains the closed bars so far, so the threshold scales
    with it; from 10:00 onward this is arithmetically the original rule.
    Returns {'ok': bool, 'reason': str, 'frac': float|None} where frac is
    the raw window-volume / ADV so far. If REQUIRE_VOLUME is off, always
    ok. Fails to ok=False (options blocked) when data is thin, never
    raises."""
    if not REQUIRE_VOLUME:
        return {'ok': True, 'reason': 'vol gate off'}
    today = now_et.date()
    open_t, win_end = dtime(9, 30), OPEN_WINDOW_END

    open_dt = datetime.combine(today, open_t, tzinfo=now_et.tzinfo or ET)
    end_dt = datetime.combine(today, win_end, tzinfo=now_et.tzinfo or ET)
    win_min = (end_dt - open_dt).total_seconds() / 60.0
    elapsed = (min(now_et, end_dt) - open_dt).total_seconds() / 60.0
    if elapsed <= 0 or win_min <= 0:
        return {'ok': False,
                'reason': 'vol: opening window not started (options blocked)',
                'frac': None}
    required = OPEN_VOL_MIN_FRAC * min(1.0, elapsed / win_min)

    def _first30(d):
        m = ((df10['time'].dt.date == d) & (df10['time'].dt.time >= open_t)
             & (df10['time'].dt.time < win_end))
        return float(df10.loc[m, 'volume'].sum())

    def _dayvol(d):
        return float(df10.loc[df10['time'].dt.date == d, 'volume'].sum())

    sessions = sorted({t.date() for t in df10['time']})
    prior = [d for d in sessions if d < today]
    if not prior:
        return {'ok': False, 'reason': 'vol: no prior ADV (options blocked)',
                'frac': None}
    day_vols = [v for v in (_dayvol(d) for d in prior) if v > 0]
    if not day_vols:
        return {'ok': False, 'reason': 'vol: no prior-day vol (options blocked)',
                'frac': None}
    adv = sum(day_vols) / len(day_vols)
    open_vol = _first30(today)
    frac = open_vol / adv if adv > 0 else 0.0
    if frac < required:
        return {'ok': False,
                'reason': f'open vol {frac:.0%} of ADV < {required:.0%} '
                          f'required ({elapsed:.0f}/{win_min:.0f} min pace '
                          f'of {OPEN_VOL_MIN_FRAC:.0%}) '
                          f'(options blocked, shares ok)',
                'frac': round(frac, 3)}
    return {'ok': True,
            'reason': f'open vol {frac:.0%} of ADV >= {required:.0%} '
                      f'pace (options allowed)',
            'frac': round(frac, 3)}


# --- OPTIONAL 1-HOUR CONFIRMATION --------------------------------------------

def step3_macro(df1h, direction):
    """Last closed 1-hour bar vs the 1-hour 34/50 cloud. Used only if
    REQUIRE_1H. Fails closed on thin data."""
    if df1h is None or len(df1h) < EMA_REGIME_B * 3:
        n = 0 if df1h is None else len(df1h)
        return {'state': 'unknown', 'confirmed': False,
                'reason': f'insufficient hourly bars ({n}) (fail closed)'}
    c = df1h['close']
    e34, e50 = float(ema(c, EMA_REGIME_A).iloc[-1]), float(ema(c, EMA_REGIME_B).iloc[-1])
    lo, hi = min(e34, e50), max(e34, e50)
    px = float(c.iloc[-1])
    state = 'above' if px > hi else ('below' if px < lo else 'chop')
    confirmed = ((state == 'above' and direction == 'long')
                 or (state == 'below' and direction == 'short'))
    return {'state': state, 'confirmed': confirmed,
            'reason': f'1h {px:.2f} vs [{lo:.2f},{hi:.2f}] = {state}'}


# --- TOP-LEVEL EVALUATION ----------------------------------------------------

def evaluate(ticker, bars10, bars1h, current_open,
             open_position_direction=None, now_et=None):
    """One faithful decision pass at a 10-min bar close.

    Priority: exits first (not gated), then entries (gated). The 34/50 is a
    hard gate on entries; the 5/12 close is both the entry trigger and the
    primary exit; a close through the 34/50 is the structural stop.

    Returns the same dict contract the caller already consumes:
      action in {ENTER_LONG, ENTER_SHORT, EXIT_LONG, EXIT_SHORT,
                 EXIT_LONG_THEN_ENTER_SHORT, EXIT_SHORT_THEN_ENTER_LONG, NONE}
      plus gate / step2 / step3 / flip for logging.

    `current_open` is accepted for signature compatibility but intentionally
    NOT used for decisions - everything keys off closed bars.
    """
    df10 = bars_to_df(bars10)
    df1h = bars_to_df(bars1h)

    decision = {'ticker': ticker, 'action': 'NONE', 'gate': None,
                'step2': None, 'step3': None, 'flip': None, 'exit_kind': None,
                'volume_ok': True}

    if df10 is None or len(df10) < WARMUP_BARS:
        n = 0 if df10 is None else len(df10)
        decision['gate'] = {'passed': False,
                            'reason': f'warming up ({n}/{WARMUP_BARS})'}
        return decision

    ctx = trend_context(df10)
    decision['step2'] = ctx          # logged as the "regime" line

    gate = launch_gate(df10, now_et=now_et)
    decision['gate'] = gate
    decision['volume_ok'] = gate.get('volume_ok', True)

    held = open_position_direction

    # --- RULE 3: exits (never gated) ---
    # Distinguish the two exit causes so the caller can treat them correctly:
    #   structural  = close through the 34/50 (you were wrong -> stop, block rebuy)
    #   ride_end    = close back under/over the 5/12 only (scratch -> rebuy ok)
    exit_action = None
    exit_kind = None
    r_bot = ctx['clouds']['regime'][0]
    r_top = ctx['clouds']['regime'][1]
    cur = ctx['closes']['cur']
    if held == 'long' and ctx['long_exit']:
        exit_action = 'EXIT_LONG'
        exit_kind = 'structural' if cur < r_bot else 'ride_end'
    elif held == 'short' and ctx['short_exit']:
        exit_action = 'EXIT_SHORT'
        exit_kind = 'structural' if cur > r_top else 'ride_end'
    decision['exit_kind'] = exit_kind
    decision['flip'] = {'flip': exit_action is not None,
                        'close_direction': held if exit_action else None}

    # --- RULES 1+2: entry trigger (gated by 34/50 trend + launch gate) ---
    entry_dir = None
    if gate['passed']:
        if ctx['trend'] == 'up' and ctx['fresh_long'] and held != 'long':
            entry_dir = 'long'
        elif ctx['trend'] == 'down' and ctx['fresh_short'] and held != 'short':
            entry_dir = 'short'

        if entry_dir and REQUIRE_1H:
            step3 = step3_macro(df1h, entry_dir)
            decision['step3'] = step3
            if not step3['confirmed']:
                entry_dir = None

    # --- compose action ---
    if exit_action and entry_dir and entry_dir != held:
        decision['action'] = f'{exit_action}_THEN_ENTER_{entry_dir.upper()}'
    elif exit_action:
        decision['action'] = exit_action
    elif entry_dir and held is None:
        decision['action'] = f'ENTER_{entry_dir.upper()}'

    return decision
