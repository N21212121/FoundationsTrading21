"""
trade_router.py — Foundations Trading

Owns everything between a signal-engine decision and the broker:

  PLANNING
    - Fill-and-spill sizing: whole option contracts first, remainder to
      shares. No qualifying contract -> 100% shares (long only).
    - Strike selection against the locked options spec.
    - Combo sleeve rules: long = calls + shares; short = puts only.
    - shares_only rules (OU): shares both ways; a short leg is a genuine
      short sale and stays dormant behind engine_ou.ALLOW_SHORTS.

  EXECUTION
    - Policy A: options leg first, shares best-effort. A failed share leg
      leaves an options-only position; freed dollars recycle to cash.
    - Order intents -> alpaca_manager calls -> order log. Fills are
      confirmed via get_order before the caller updates position state.

  EXITS — ENGINE-DRIVEN ONLY. READ THIS.
    There are NO price stops, trails, ratchets, premium monitors, or
    MACD-collapse exits in this file anymore. The engines emit EXIT on a
    bar close (Ripster: 5/12 ride-end, 34/50 structural; OU: profit
    target, time stop, invalidation, circuit breaker) and plan_exit()
    only builds the closing legs. A gap through a level exits on the
    NEXT bar close, not at the level. That is the accepted design.
    Stop-outs (exit_kind='structural') set a same-day no-rebuy flag;
    profit exits do not.

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

def plan_entry(direction, budget_dollars, spot, chain, execution='options_combo'):
    """Build the entry plan.

    execution='options_combo' (Ripster):
      long  -> calls + shares (fill-and-spill)
      short -> puts only (shares are long-only); no qualifying put -> no trade.

    execution='shares_only' (OU reversion):
      shares both ways, no options leg ever. A reversion trade expects one or
      two sigma of a residual over a few half-lives; option premium and theta
      eat that before it arrives, and you would be buying IV right after the
      spike that created the signal. The engine declares this, not the user.

    Returns {'legs': [...], 'reason': str}. Legs in EXECUTION ORDER
    (options first per Policy A)."""
    if execution == 'shares_only':
        return _plan_shares_only(direction, budget_dollars, spot)
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


def _plan_shares_only(direction, budget_dollars, spot):
    """Shares both ways. A short leg here is a genuine short sale: it needs a
    margin account and it carries unbounded loss. engine_ou.ALLOW_SHORTS is
    False by default precisely so this path stays dormant until you turn it on
    deliberately."""
    if spot <= 0 or budget_dollars <= 0:
        return {'legs': [], 'reason': 'shares_only: no budget or no price'}
    shares = int(budget_dollars // spot)
    if shares < 1:
        return {'legs': [],
                'reason': f'shares_only: budget ${budget_dollars:.0f} < '
                          f'one share at ${spot:.2f}'}
    side = 'buy' if direction == 'long' else 'sell'
    return {'legs': [{'kind': 'shares', 'side': side, 'qty': shares,
                      'est_price': spot}],
            'reason': f'shares_only: {side} {shares} share(s)'}


def _option_leg(contract, contracts):
    return {'kind': 'option', 'side': 'buy', 'symbol': contract['symbol'],
            'contracts': contracts, 'est_premium': contract['mid'],
            'strike': contract['strike'], 'expiry': contract['expiry'],
            'type': contract['type']}


def plan_exit(position, sleeve='both'):
    """Closing legs for an open position. sleeve: 'both'|'shares'|'options'.
    All-at-once, no stepping out.

    Direction-aware on the share leg: a long position SELLS to close; a
    short-shares position (shares_only engines with ALLOW_SHORTS on) BUYS
    to cover. Without this, the day ALLOW_SHORTS flips, every short exit
    would sell deeper into the short instead of closing it. Share counts
    are stored as positive quantities with `direction` carrying the sign;
    that convention is what makes the cover side computable here. Options
    are always long premium in this system (BUY_TO_OPEN only), so the
    option leg always sells, regardless of direction."""
    direction = (position.get('direction') or 'long').lower()
    legs = []
    if sleeve in ('both', 'options') and position.get('option_contracts', 0) > 0:
        legs.append({'kind': 'option', 'side': 'sell',
                     'symbol': position['option_symbol'],
                     'contracts': position['option_contracts']})
    sh = int(position.get('shares') or 0)
    if sleeve in ('both', 'shares') and sh > 0:
        legs.append({'kind': 'shares',
                     'side': 'buy' if direction == 'short' else 'sell',
                     'qty': sh})
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
