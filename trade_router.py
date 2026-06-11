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
    - Share stop  -5.00% unconditional         (suppressed before 10:00 ET)
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

# Alpaca order statuses that count as a real fill/working order. 'rejected'
# and 'canceled' come back with a non-error status string and a zero fill,
# so we must whitelist explicitly rather than reject only 'error'.
_OK_ORDER_STATUSES = {'filled', 'partially_filled', 'accepted',
                      'new', 'pending_new', 'accepted_for_bidding'}


def _order_ok(result):
    """True only if the broker accepted the order. A status outside the
    OK set (e.g. 'rejected', 'canceled', 'error') is a failure even if no
    exception fired."""
    status = (result or {}).get('status', '')
    if status == 'error':
        return False
    return str(status).lower() in _OK_ORDER_STATUSES


def confirm_fill(alpaca, order_id, timeout_s=4.0, poll_s=0.5):
    """Poll the broker for an order's real fill. Market orders on liquid
    names fill in well under a second; we poll briefly and return
    {'filled_qty', 'filled_avg_price', 'status'} from the broker's record.

    Times out gracefully: returns whatever the last poll said. The caller
    treats filled_qty > 0 as confirmation and uses filled_avg_price as the
    true entry/exit price instead of the planning-time estimate."""
    import time as _t
    deadline = _t.monotonic() + timeout_s
    last = {}
    while _t.monotonic() < deadline:
        o = alpaca.get_order(order_id)
        if o.get('status') == 'error':
            return {'filled_qty': 0, 'filled_avg_price': None,
                    'status': 'error'}
        last = o
        if str(o.get('status', '')).lower() == 'filled':
            break
        _t.sleep(poll_s)
    return {'filled_qty': last.get('filled_qty') or 0,
            'filled_avg_price': last.get('filled_avg_price'),
            'status': last.get('status', 'unknown')}


def execute_plan(alpaca, ticker, legs, reason=''):
    """Fire legs in order. Options first, shares best-effort.

    Returns {'filled': [...], 'failed': [...]}. Each filled entry carries
    a 'fill' dict with the broker-confirmed quantity and average price —
    the caller updates position state from CONFIRMED fills, not estimates.

    A broker rejection returns a non-error status but a zero fill. We
    treat anything outside the explicit OK status set as a failure, and we
    additionally confirm the fill via get_order before classifying a leg
    as filled."""
    filled, failed = [], []
    for leg in legs:
        if leg['kind'] == 'option':
            r = alpaca.place_option_order(leg['symbol'], leg['side'],
                                          leg['contracts'])
            sleeve, qty, symbol = 'OPTIONS', leg['contracts'], leg['symbol']
        else:
            r = alpaca.place_share_order(ticker, leg['side'], leg['qty'])
            sleeve, qty, symbol = 'SHARES', leg['qty'], ticker

        ok = _order_ok(r)
        fill = None
        if ok and r.get('order_id'):
            fill = confirm_fill(alpaca, r['order_id'])
            # An accepted order that confirms zero fill within the window is
            # suspicious but not definitively failed (slow fill). We keep it
            # in 'filled' if the broker status is still working; the
            # reconciler will catch any divergence.
            if str(fill.get('status', '')).lower() in ('rejected', 'canceled',
                                                       'expired'):
                ok = False

        cm.log_order(ticker=ticker, side=leg['side'], sleeve=sleeve,
                     qty=qty, symbol=symbol,
                     order_id=r.get('order_id', ''),
                     status=(fill or {}).get('status',
                                             r.get('status', '')) if ok
                            else 'failed',
                     fill_price=(fill or {}).get('filled_avg_price', ''),
                     fill_qty=(fill or {}).get('filled_qty', ''),
                     reason=reason or r.get('message', ''))
        entry = {'leg': leg, 'result': r, 'fill': fill}
        (filled if ok else failed).append(entry)
        # Policy A: an option-leg failure on a LONG entry doesn't stop the
        # share leg — the budget already spilled at planning time. A share
        # failure just logs; freed dollars recycle implicitly (cash was
        # never spent).
    return {'filled': filled, 'failed': failed}


# ─── EXITS ARE ENGINE-DRIVEN ──────────────────────────────────────────────────
# Pure-structural exits: signal_engine.evaluate() emits EXIT on a 10-min close
# under the 5/12 cloud (ride over) or through the 34/50 cloud (structural stop).
# trade_router no longer watches premiums or computes stops/trails. plan_exit
# (above) builds the sell legs; the engine decides WHEN.


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
