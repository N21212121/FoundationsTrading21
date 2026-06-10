"""
trade_router.py — Foundations Trading

Owns everything between a signal-engine decision and the broker:

  PLANNING
    - Fill-and-spill sizing: whole option contracts first, remainder to
      shares. No qualifying contract -> 100% shares (long only).
    - Strike selection against the locked options spec.
    - Combo sleeve rules: long = calls + shares; short = puts only.

  EXECUTION
    - Policy A: options leg first, shares best-effort. A failed share leg
      leaves an options-only position; freed dollars recycle to cash.
    - Order intents -> alpaca_manager calls -> position state updates.

  MONITORING (the 10-second loop body)
    - Share stop  -5.00% unconditional        (suppressed before 10:00 ET)
    - Option stop -7.00% EMA12-break confirmed; -15% unconditional floor
                                               (suppressed before 10:00 ET)
    - Breakeven ratchet: premium ever +10% over entry -> exit at/below
      entry, unconditional (profit exit, no rebuy block)
    - Premium trail: sell options when premium <= 60% of peak since entry
    - MACD-collapse: sell sleeve when |EMA5-EMA12 spread| <= 70% of peak
    - First exit to fire wins. Stops set a no-same-day-rebuy flag;
      profit exits allow same-day rebuy.
    - Peak references reset when a position is added to.

HARDCODED CONSTANTS BY DESIGN — edit + commit to change.
"""

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import config_manager as cm

ET = ZoneInfo('America/New_York')

# ─── LOCKED OPTIONS SPEC ───────────────────────────────────────────────────────

STRIKE_RANGE_PCT = 0.04      # strikes within ±4% of spot
DTE_MIN = 4
DTE_MAX = 26
TARGET_DTE = 10              # prefer expiry nearest this
PREFER_FRIDAY = True
MAX_SPREAD_PCT = 15.0        # reject contracts with bid/ask spread > 15% of mid
CONTRACT_MULTIPLIER = 100

# ─── RISK / EXIT CONSTANTS ─────────────────────────────────────────────────────

SHARE_STOP_PCT = 5.0          # shares: unconditional, sell at -5.00% from entry
OPTION_STOP_PCT = 7.0         # options: fires ONLY with EMA12 break confirmation
OPTION_STOP_FLOOR_PCT = 15.0  # options: unconditional hard floor, no confirmation
PREMIUM_TRAIL_FRAC = 0.60    # sell options when premium <= 60% of peak
BREAKEVEN_ARM_PCT = 10.0     # once premium has been +10% over entry, option
                             # stop ratchets to breakeven (unconditional,
                             # treated as a profit exit: no rebuy block)
MACD_COLLAPSE_FRAC = 0.70    # sell sleeve when |spread| <= 70% of peak
STOPS_START = dtime(10, 0)   # stop losses OFF before 10:00 ET; profit exits ON


# ─── SIZING: FILL-AND-SPILL ────────────────────────────────────────────────────

def compute_fill_spill(budget_dollars, option_mid):
    """Whole contracts first, remainder to shares.

    Returns {'contracts', 'option_cost_est', 'share_dollars'}."""
    if budget_dollars <= 0:
        return {'contracts': 0, 'option_cost_est': 0.0, 'share_dollars': 0.0}
    if not option_mid or option_mid <= 0:
        return {'contracts': 0, 'option_cost_est': 0.0,
                'share_dollars': round(budget_dollars, 2)}
    per_contract = option_mid * CONTRACT_MULTIPLIER
    contracts = int(budget_dollars // per_contract)
    cost = contracts * per_contract
    return {'contracts': contracts,
            'option_cost_est': round(cost, 2),
            'share_dollars': round(budget_dollars - cost, 2)}


# ─── STRIKE SELECTION ──────────────────────────────────────────────────────────

def pick_contract(chain, want_type, spot):
    """Choose one contract from a chain (alpaca_manager format).

    Filters: correct type, spread <= MAX_SPREAD_PCT, usable mid.
    Rank: nearest expiry to TARGET_DTE (Friday tiebreak if PREFER_FRIDAY),
          then strike nearest spot.
    Returns the contract dict or None."""
    today = datetime.now(ET).date()
    candidates = []
    for c in chain:
        if c['type'] != want_type:
            continue
        if not c['mid'] or c['mid'] <= 0:
            continue
        if c['spread_pct'] is None or c['spread_pct'] > MAX_SPREAD_PCT:
            continue
        if not c['expiry'] or c['strike'] is None:
            continue
        try:
            exp = datetime.strptime(c['expiry'], '%Y-%m-%d').date()
        except ValueError:
            continue
        dte = (exp - today).days
        if dte < DTE_MIN or dte > DTE_MAX:
            continue
        is_friday = exp.weekday() == 4
        candidates.append((abs(dte - TARGET_DTE),
                           0 if (PREFER_FRIDAY and is_friday) else 1,
                           abs(c['strike'] - spot),
                           c))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[:3])
    return candidates[0][3]


# ─── PLANNING ──────────────────────────────────────────────────────────────────

def plan_entry(direction, budget_dollars, spot, chain):
    """Build the entry plan for a combo position.

    long  -> calls + shares (fill-and-spill)
    short -> puts only (shares are long-only); no qualifying put -> no trade.

    Returns {'legs': [...], 'reason': str}. Legs in EXECUTION ORDER
    (options first per Policy A)."""
    want_type = 'call' if direction == 'long' else 'put'
    contract = pick_contract(chain, want_type, spot)

    legs = []
    if direction == 'short':
        if not contract:
            return {'legs': [], 'reason': 'short: no qualifying put; no trade'}
        split = compute_fill_spill(budget_dollars, contract['mid'])
        if split['contracts'] <= 0:
            return {'legs': [],
                    'reason': f"short: budget ${budget_dollars:.0f} < one put "
                              f"at ${contract['mid'] * 100:.0f}"}
        legs.append(_option_leg(contract, split['contracts']))
        return {'legs': legs,
                'reason': f"short: {split['contracts']} put(s), no share leg"}

    # long
    if contract:
        split = compute_fill_spill(budget_dollars, contract['mid'])
        if split['contracts'] > 0:
            legs.append(_option_leg(contract, split['contracts']))
            share_dollars = split['share_dollars']
        else:
            share_dollars = budget_dollars
    else:
        share_dollars = budget_dollars

    if share_dollars > 0 and spot > 0:
        shares = int(share_dollars // spot)
        if shares > 0:
            legs.append({'kind': 'shares', 'side': 'buy', 'qty': shares,
                         'est_price': spot})

    if not legs:
        return {'legs': [], 'reason': 'long: budget too small for either sleeve'}
    return {'legs': legs, 'reason': f"long: {len(legs)} leg(s)"}


def _option_leg(contract, contracts):
    return {'kind': 'option', 'side': 'buy', 'symbol': contract['symbol'],
            'contracts': contracts, 'est_premium': contract['mid'],
            'strike': contract['strike'], 'expiry': contract['expiry'],
            'type': contract['type']}


def plan_exit(position, sleeve='both'):
    """Sell legs for an open position. sleeve: 'both'|'shares'|'options'.
    All-at-once, no stepping out."""
    legs = []
    if sleeve in ('both', 'options') and position.get('option_contracts', 0) > 0:
        legs.append({'kind': 'option', 'side': 'sell',
                     'symbol': position['option_symbol'],
                     'contracts': position['option_contracts']})
    if sleeve in ('both', 'shares') and position.get('shares', 0) > 0:
        legs.append({'kind': 'shares', 'side': 'sell',
                     'qty': position['shares']})
    return legs


# ─── EXECUTION (Policy A) ──────────────────────────────────────────────────────

def execute_plan(alpaca, ticker, legs, reason=''):
    """Fire legs in order. Options first, shares best-effort.

    Returns {'filled': [...], 'failed': [...]}. Every order attempt is
    logged to order_log; the caller updates position state from 'filled'."""
    filled, failed = [], []
    for leg in legs:
        if leg['kind'] == 'option':
            r = alpaca.place_option_order(leg['symbol'], leg['side'],
                                          leg['contracts'])
            ok = r.get('status') not in (None, 'error')
            cm.log_order(ticker=ticker, side=leg['side'], sleeve='OPTIONS',
                         qty=leg['contracts'], symbol=leg['symbol'],
                         order_id=r.get('order_id', ''),
                         status='submitted' if ok else 'error',
                         reason=reason or r.get('message', ''))
        else:
            r = alpaca.place_share_order(ticker, leg['side'], leg['qty'])
            ok = r.get('status') not in (None, 'error')
            cm.log_order(ticker=ticker, side=leg['side'], sleeve='SHARES',
                         qty=leg['qty'], symbol=ticker,
                         order_id=r.get('order_id', ''),
                         status='submitted' if ok else 'error',
                         reason=reason or r.get('message', ''))
        (filled if ok else failed).append({'leg': leg, 'result': r})
        # Policy A: an option-leg failure on a LONG entry doesn't stop the
        # share leg — the budget already spilled at planning time. A share
        # failure just logs; freed dollars recycle implicitly (cash was
        # never spent).
    return {'filled': filled, 'failed': failed}


# ─── MONITOR (10-second loop body) ─────────────────────────────────────────────

def _stops_active(now_et=None):
    now_et = now_et or datetime.now(ET)
    return now_et.time() >= STOPS_START


def check_position(position, share_price, option_premium, macd_spread_abs,
                   last_close=None, e12=None, now_et=None):
    """Evaluate every exit trigger for one position. Pure function:
    mutates NOTHING — returns updated tracking values plus any exit.

    position fields used:
      shares, share_entry_price, option_contracts, option_entry_premium,
      peak_premium, peak_macd_spread, direction

    Returns {
      'exit': None | {'sleeve': 'shares'|'options'|'both',
                      'trigger': str, 'is_stop': bool},
      'peak_premium': float, 'peak_macd_spread': float,
    }
    First trigger in priority-free order wins — we check stops first only
    because a stop and a profit exit firing on the same tick should record
    as the stop (it sets the no-rebuy flag; conservative bookkeeping)."""
    now_et = now_et or datetime.now(ET)

    peak_prem = position.get('peak_premium') or 0.0
    if option_premium and option_premium > peak_prem:
        peak_prem = option_premium
    peak_macd = position.get('peak_macd_spread') or 0.0
    if macd_spread_abs and macd_spread_abs > peak_macd:
        peak_macd = macd_spread_abs

    out = {'exit': None, 'peak_premium': peak_prem,
           'peak_macd_spread': peak_macd}

    has_shares = position.get('shares', 0) > 0
    has_options = position.get('option_contracts', 0) > 0

    # ── STOPS (suppressed before 10:00 ET) ──
    if _stops_active(now_et):
        if has_shares and share_price and position.get('share_entry_price'):
            loss_pct = (position['share_entry_price'] - share_price) \
                       / position['share_entry_price'] * 100
            if loss_pct >= SHARE_STOP_PCT:
                out['exit'] = {'sleeve': 'shares',
                               'trigger': f'share stop -{loss_pct:.2f}%',
                               'is_stop': True}
                return out
        if has_options and option_premium and position.get('option_entry_premium'):
            loss_pct = (position['option_entry_premium'] - option_premium) \
                       / position['option_entry_premium'] * 100

            # Hard floor: unconditional. Caps theta bleed on a structurally
            # intact chart — the one case the confirmation would hold forever.
            if loss_pct >= OPTION_STOP_FLOOR_PCT:
                out['exit'] = {'sleeve': 'options',
                               'trigger': f'option FLOOR stop -{loss_pct:.2f}% '
                                          f'(unconditional)',
                               'is_stop': True}
                return out

            # Confirmed stop: -7% fires only if the last closed 10-min bar
            # broke the EMA12 in the position's direction. Premium-only
            # drawdown (theta/IV) with intact price structure -> hold.
            if loss_pct >= OPTION_STOP_PCT:
                if last_close is None or e12 is None:
                    # No bar data to confirm with. Fail toward capital
                    # protection: fire the stop blind rather than hold blind.
                    out['exit'] = {'sleeve': 'options',
                                   'trigger': f'option stop -{loss_pct:.2f}% '
                                              f'(unconfirmed: no bar data)',
                                   'is_stop': True}
                    return out
                direction = position.get('direction', 'long')
                broken = (last_close < e12) if direction == 'long' \
                         else (last_close > e12)
                if broken:
                    out['exit'] = {'sleeve': 'options',
                                   'trigger': f'option stop -{loss_pct:.2f}% '
                                              f'(EMA12 break confirmed: close '
                                              f'{last_close:.2f} vs e12 {e12:.2f})',
                                   'is_stop': True}
                    return out
                # else: structure intact, hold through the premium noise

    # ── PROFIT EXITS (always on) ──
    # Breakeven ratchet (options): once the premium has been ARM_PCT over
    # entry, an exit fires unconditionally at/below entry. A winner that
    # fades exits flat instead of riding down to the -7% stop. Scratch,
    # not a stop: same-day rebuy stays allowed.
    entry_prem = position.get('option_entry_premium') or 0.0
    if has_options and option_premium and entry_prem > 0:
        armed = peak_prem >= entry_prem * (1 + BREAKEVEN_ARM_PCT / 100.0)
        if armed and option_premium <= entry_prem:
            out['exit'] = {'sleeve': 'options',
                           'trigger': f'breakeven ratchet: premium '
                                      f'{option_premium:.2f} <= entry '
                                      f'{entry_prem:.2f} after peak '
                                      f'{peak_prem:.2f} (armed at +'
                                      f'{BREAKEVEN_ARM_PCT:.0f}%)',
                           'is_stop': False}
            return out

    # Premium trail (options) — ARMED only once peak exceeds entry.
    # Until the position has actually been profitable, the -7% stop is
    # the only downside exit. Prevents the trail from firing below cost.
    if has_options and option_premium and peak_prem > entry_prem > 0:
        if option_premium <= PREMIUM_TRAIL_FRAC * peak_prem:
            # only meaningful if we're actually off a real peak above entry
            out['exit'] = {'sleeve': 'options',
                           'trigger': f'premium trail: {option_premium:.2f} <= '
                                      f'{PREMIUM_TRAIL_FRAC:.0%} of peak {peak_prem:.2f}',
                           'is_stop': False}
            return out

    # MACD collapse (whole sleeve set: shares + options both exit on this)
    if (has_shares or has_options) and macd_spread_abs is not None and peak_macd > 0:
        if macd_spread_abs <= MACD_COLLAPSE_FRAC * peak_macd:
            out['exit'] = {'sleeve': 'both',
                           'trigger': f'MACD collapse: |spread| {macd_spread_abs:.4f} '
                                      f'<= {MACD_COLLAPSE_FRAC:.0%} of peak {peak_macd:.4f}',
                           'is_stop': False}
            return out

    return out


def reset_peaks_on_add(position):
    """Adding to a position resets the trailing references to the latest
    purchase point. Caller invokes after a successful add."""
    position['peak_premium'] = 0.0
    position['peak_macd_spread'] = 0.0
    return position


def mark_stop_out(no_rebuy_flags, ticker, sleeve, now_et=None):
    """Record a stop-out: blocks same-day automatic rebuy of that sleeve.
    no_rebuy_flags: dict {ticker: {'shares': 'YYYY-MM-DD', 'options': ...}}"""
    now_et = now_et or datetime.now(ET)
    day = now_et.strftime('%Y-%m-%d')
    no_rebuy_flags.setdefault(ticker, {})[sleeve] = day
    return no_rebuy_flags


def rebuy_blocked(no_rebuy_flags, ticker, sleeve, now_et=None):
    now_et = now_et or datetime.now(ET)
    day = now_et.strftime('%Y-%m-%d')
    return no_rebuy_flags.get(ticker, {}).get(sleeve) == day
