"""
app.py — Foundations Trading

The orchestrator. Owns:
  - Flask server on 127.0.0.1:5275 (loopback only)
  - pywebview desktop window
  - The bar-close loop (wall-aligned to 10-min boundaries + 15s grace)
  - The 10-second exit monitor
  - Position state (persisted via config_manager)
  - All API routes for the dashboard

Decision logic lives in signal_engine. Planning/execution/exit rules live
in trade_router. Broker I/O lives in alpaca_manager. This file only wires
them together.
"""

import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

import config_manager as cm
import signal_engine as se          # still used by /api/bars for chart EMAs
import trade_router as tr
from alpaca_manager import AlpacaManager
from bar_cache import BarCache
from engines_bootstrap import resolver, registry
import backtester
import baskets as bk
import halflife as hlf
import sectors as sec
import assign as asg
from engine_ou import ALLOW_SHORTS as _OU_ALLOW_SHORTS
from backtest_run import _fetch as _bt_fetch, warmup_start


def _sync_engine_overrides(cfg):
    """Config + baskets.json are the source of truth for engine routing.

    Resolution priority (first hit wins), rebuilt from disk on every sync so
    there is no hidden state:

      1. per-ticker: watchlist entry carries 'engine': <registered name>
      2. per-basket: watchlist entry carries 'basket': <name>, and that
         basket in baskets.json carries 'engine': <registered name>
      3. global default

    Unknown engine names are ignored (fail soft: the name falls through to
    the next tier rather than killing the sweep)."""
    valid = set(registry.names())

    # ── tier 1: per-ticker overrides ──
    seen = set()
    for w in cfg.get('watchlist', []):
        t = w['ticker'].upper()
        e = w.get('engine')
        if e and e in valid:
            resolver.set_ticker(t, e)
            seen.add(t)
    for t in list(resolver.by_ticker):
        if t not in seen:
            resolver.clear_ticker(t)

    # ── tier 2: basket layer, rebuilt clean each sync ──
    resolver.by_basket.clear()
    resolver.ticker_basket.clear()
    all_baskets = bk.load_baskets()
    for bname, b in all_baskets.items():
        e = (b or {}).get('engine')
        if e and e in valid:
            resolver.by_basket[bname] = e
    for w in cfg.get('watchlist', []):
        bn = w.get('basket')
        if bn and bn in all_baskets:
            resolver.ticker_basket[w['ticker'].upper()] = bn

ET = ZoneInfo('America/New_York')
PORT = 5275

# ── SIGNAL INVERSION ──
# When True, the engine trades the exact MIRROR of its decisions: every LONG
# becomes SHORT and every SHORT becomes LONG, on entries AND exits. Execution
# is unchanged — "short" still means BUY-to-open puts, "long" still means BUY
# calls + shares — so nothing is ever naked-sold. The engine reasons in a
# fully mirrored world: we invert the held direction on the way in and invert
# the action on the way out, so its flip/close logic stays internally consistent.
INVERT_SIGNALS = False


def _journal_fill(**kw):
    """Mirror a fill into the journal noun.

    Wrapped whole. The journal is an analysis surface, not part of the
    execution path: a broken journal must never stop a trade from being
    recorded in trade_log.csv or block the bar loop. Failures go to the
    forensics log and are otherwise swallowed.
    """
    try:
        import journal as _jn
        _jn.log_fill(source='engine', **kw)
    except Exception as e:
        try:
            cm.log_forensic('api_event', event='journal_write',
                            status='error', error=f'{type(e).__name__}: {e}')
        except Exception:
            pass


def _cond_from_ctx(ctx, decision):
    """Map the engine's step2 context onto the journal's condition keys.

    These are the same layer names the imported sheet uses, so an engine fill
    and a manual fill are filterable through one picker rather than two.
    """
    ctx = ctx or {}
    return {
        'ema_5_12': ctx.get('price_vs_5_12', ''),
        'ema_34_50': ctx.get('price_vs_34_50', ''),
        'trend': ctx.get('trend', ''),
        'trigger': ('fresh_long' if ctx.get('fresh_long')
                    else ('fresh_short' if ctx.get('fresh_short') else 'none')),
        'exit_kind': (decision or {}).get('exit_kind') or '',
    }


def _invert_dir(d):
    if d == 'long':
        return 'short'
    if d == 'short':
        return 'long'
    return d


def _invert_action(a):
    if not a:
        return a
    if 'LONG' in a:
        return a.replace('LONG', 'SHORT')
    if 'SHORT' in a:
        return a.replace('SHORT', 'LONG')
    return a

app = Flask(__name__)

# ─── SHARED STATE ──────────────────────────────────────────────────────────────

alpaca = AlpacaManager()

_state_lock = threading.RLock()
_exiting = set()                 # tickers with an exit in flight; monitor skips
_engine_enabled = False          # auto-trading switch; OFF until user enables

# _no_rebuy persists to disk so a restart mid-session does NOT clear stop-out
# blocks. Same-day stops MUST keep blocking same-day rebuys even if the user
# kills and restarts the app. Format: {ticker: {sleeve: 'YYYY-MM-DD'}}.
import json as _json
_NO_REBUY_FILE = os.path.join(cm.DATA_DIR, 'no_rebuy_flags.json')


def _load_no_rebuy():
    try:
        with open(_NO_REBUY_FILE, 'r', encoding='utf-8') as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError):
        return {}


def _save_no_rebuy():
    tmp = _NO_REBUY_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            _json.dump(_no_rebuy, f, indent=2)
        os.replace(tmp, _NO_REBUY_FILE)
    except OSError as e:
        print(f'[INIT] no_rebuy save failed: {e}')


_no_rebuy = _load_no_rebuy()    # {ticker: {'shares': day, 'options': day}}
_last_bar_seen = {}              # {ticker: iso time of last evaluated bar}
_status_message = 'started'

# Day-start cash snapshot: position budgets are pinned to the cash available at
# the first evaluation of each trading day, so every position gets the SAME
# dollar size regardless of how much cash has already been deployed. Resets on a
# new ET date.
_daycash = {'date': None, 'cash': 0.0}


def _session_cash():
    """Cash available pinned to the start of today's session. Captured once per
    ET date from the broker; reused all day so budgets don't shrink as cash is
    consumed."""
    today = datetime.now(ET).date().isoformat()
    if _daycash['date'] != today:
        try:
            acct = alpaca.get_account()
            # Prefer cash; fall back to equity if cash isn't present.
            _daycash['cash'] = float(acct.get('cash', acct.get('equity', 0)) or 0)
            _daycash['date'] = today
            cm.log_forensic('conn_event', event='day_cash_snapshot',
                            status='set', detail=f"{_daycash['cash']:.2f}")
        except Exception:
            pass
    return _daycash['cash']


def _positions():
    return cm.load_positions()


def _save_positions(p):
    cm.save_positions(p)


# ─── ENTRY / EXIT ACTIONS ──────────────────────────────────────────────────────

def _budget_for(ticker, cfg):
    """allocation_pct of the day's STARTING cash, in dollars. Pinned per session
    so every position is sized the same regardless of cash already deployed."""
    for w in cfg.get('watchlist', []):
        if w['ticker'].upper() == ticker.upper():
            try:
                base = _session_cash()
                return base * float(w.get('allocation_pct', 0)) / 100.0
            except Exception:
                return 0.0
    return 0.0


def _do_entry(ticker, direction, cfg, source='engine', allow_options=True,
              conditions=None):
    """Plan and execute an entry. Returns a result dict for logging.

    conditions carries the engine's read of the clouds at the deciding bar.
    It is recorded on the journal fill so the heatmap can layer on it later;
    it has no effect on execution.
    """
    # ── ONE POSITION PER TICKER (engine only) ──
    # The engine must never add to a ticker it already holds. Signals
    # re-firing on consecutive bars must NOT pyramid. Manual adds are
    # deliberate and remain allowed (they reset peaks elsewhere).
    if source == 'engine' and ticker in _positions():
        return {'ok': False,
                'reason': 'position already open; engine add blocked'}

    # ── SET-BUDGET CEILING (engine only) ──
    # Total position value (both sleeves) may never exceed the ticker's Set
    # budget = allocation_pct x equity. Hard refuse, no trim. Manual entries
    # are exempt by the user's rule. Belt-and-suspenders with the guard above:
    # even if a position somehow existed without blocking, this stops growth.
    if source == 'engine':
        try:
            set_dollars = _budget_for(ticker, cfg)
            cur_val = 0.0
            p_existing = _positions().get(ticker)
            if p_existing:
                q0 = alpaca.get_quote(ticker)
                if q0 and p_existing.get('shares'):
                    cur_val += p_existing['shares'] * q0['mid']
                osym = p_existing.get('option_symbol')
                if osym and p_existing.get('option_contracts'):
                    oq0 = alpaca.get_options_quote(osym)
                    if oq0 and oq0.get('mid') is not None:
                        cur_val += p_existing['option_contracts'] * oq0['mid'] * 100
            if cur_val >= set_dollars > 0:
                return {'ok': False,
                        'reason': f'at/over Set budget '
                                  f'(${cur_val:.0f} >= ${set_dollars:.0f}); '
                                  f'engine entry refused'}
        except Exception as e:
            cm.log_forensic('api_event', event='set_cap_check', ticker=ticker,
                            status='error', error=str(e))

    # No-rebuy guard (stops set these; profit exits don't).
    # Long entries open both sleeves -> refuse if EITHER sleeve was stopped.
    # Short entries are puts-only -> refuse only on an options block (a short
    # never trades shares, so shares-blocked is meaningless for shorts).
    _exec_mode = getattr(resolver.resolve(ticker), 'execution', 'options_combo')
    if direction == 'short' and _exec_mode != 'shares_only':
        if tr.rebuy_blocked(_no_rebuy, ticker, 'options'):
            return {'ok': False, 'reason': 'same-day rebuy blocked (option stop-out)'}
    elif direction == 'short':
        if tr.rebuy_blocked(_no_rebuy, ticker, 'shares'):
            return {'ok': False, 'reason': 'same-day rebuy blocked (share stop-out)'}
    else:
        if tr.rebuy_blocked(_no_rebuy, ticker, 'options'):
            return {'ok': False, 'reason': 'same-day rebuy blocked (option stop-out)'}
        if tr.rebuy_blocked(_no_rebuy, ticker, 'shares'):
            return {'ok': False, 'reason': 'same-day rebuy blocked (share stop-out)'}

    budget = _budget_for(ticker, cfg)
    if budget <= 0:
        return {'ok': False, 'reason': 'zero budget (ticker not in watchlist?)'}

    q = alpaca.get_quote(ticker)
    if not q:
        return {'ok': False, 'reason': 'no quote'}
    spot = q['mid']

    # The ENGINE declares the trade shape, not the caller. A reversion engine
    # filled with calls is a reversion engine that loses; see engine_ou.py.
    execution = getattr(resolver.resolve(ticker), 'execution', 'options_combo')

    if execution == 'shares_only':
        plan = tr.plan_entry(direction, budget, spot, None,
                             execution='shares_only')
    elif allow_options:
        chain = alpaca.get_options_chain(ticker, spot,
                                         strike_range_pct=tr.STRIKE_RANGE_PCT,
                                         dte_min=tr.DTE_MIN, dte_max=tr.DTE_MAX)
        plan = tr.plan_entry(direction, budget, spot, chain)
    else:
        # Volume below threshold: options blocked for automatic entry.
        # A long takes the full budget in SHARES; a short (puts-only) can't
        # trade, since shares are long-only.
        if direction != 'long':
            return {'ok': False,
                    'reason': 'volume below threshold: options blocked, '
                              'short needs options (no share fallback)'}
        qty = int(budget // spot) if spot > 0 else 0
        if qty < 1:
            return {'ok': False,
                    'reason': 'volume below threshold: options blocked, '
                              'budget below one share'}
        plan = {'legs': [{'kind': 'shares', 'side': 'buy', 'qty': qty,
                          'est_price': spot}],
                'reason': f'shares-only ({qty} sh); options blocked by volume'}
    if not plan['legs']:
        return {'ok': False, 'reason': plan['reason']}

    result = tr.execute_plan(alpaca, ticker, plan['legs'],
                             reason=f'{source}: {direction} entry')

    # Build/refresh position state from filled legs
    pos = _positions()
    p = pos.get(ticker, {})
    p['ticker'] = ticker
    p['direction'] = direction
    p.setdefault('opened_at', datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S'))
    for f in result['filled']:
        leg = f['leg']
        fill = f.get('fill') or {}
        if leg['kind'] == 'option':
            p['option_symbol'] = leg['symbol']
            real_qty = int(fill.get('filled_qty') or 0) or leg['contracts']
            p['option_contracts'] = p.get('option_contracts', 0) + real_qty
            p['option_entry_premium'] = (fill.get('filled_avg_price')
                                         or leg['est_premium'])
            p['option_type'] = leg['type']
        else:
            real_qty = int(fill.get('filled_qty') or 0) or leg['qty']
            p['shares'] = p.get('shares', 0) + real_qty
            p['share_entry_price'] = (fill.get('filled_avg_price')
                                      or leg['est_price'])
    pos[ticker] = p
    _save_positions(pos)

    for f in result['filled']:
        leg = f['leg']; fill = f.get('fill') or {}
        if leg['kind'] == 'option':
            cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='OPTIONS',
                         action='BUY',
                         qty=int(fill.get('filled_qty') or leg.get('contracts') or 0),
                         price=fill.get('filled_avg_price') or leg.get('est_premium', ''),
                         status=fill.get('status', 'submitted'),
                         reason=f"{source} entry ({plan['reason']})",
                         option_symbol=leg.get('symbol', ''),
                         option_strike=leg.get('strike', ''),
                         option_expiry=leg.get('expiry', ''),
                         option_type=leg.get('type', ''))
            _journal_fill(
                ticker=ticker, symbol=leg.get('symbol', ticker), side='buy',
                qty=float(fill.get('filled_qty') or leg.get('contracts') or 0) or 1,
                price=float(fill.get('filled_avg_price')
                            or leg.get('est_premium') or 0),
                filled_at=datetime.now(ET).replace(tzinfo=None),
                conditions=dict(conditions or {}),
                note=f"{source} entry ({plan['reason']})")
        else:
            cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='SHARES',
                         action='BUY',
                         qty=int(fill.get('filled_qty') or leg.get('qty') or 0),
                         price=fill.get('filled_avg_price') or leg.get('est_price', ''),
                         status=fill.get('status', 'submitted'),
                         reason=f"{source} entry ({plan['reason']})")
            _journal_fill(
                ticker=ticker, symbol=ticker, side='buy',
                qty=float(fill.get('filled_qty') or leg.get('qty') or 0) or 1,
                price=float(fill.get('filled_avg_price')
                            or leg.get('est_price') or 0),
                filled_at=datetime.now(ET).replace(tzinfo=None),
                conditions=dict(conditions or {}),
                note=f"{source} entry ({plan['reason']})")
    if result['failed']:
        cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='COMBO',
                     action='BUY', qty=0, price='', status='failed',
                     reason=f"{source} entry: {len(result['failed'])} leg(s) failed")
    return {'ok': True, 'filled': len(result['filled']),
            'failed': len(result['failed'])}


def _do_exit(ticker, sleeve, trigger, is_stop, conditions=None):
    """Execute an exit for one position sleeve and update state.

    Guarded against duplicate fires: if an exit is already in flight on this
    ticker, return immediately. The monitor loop also skips in-flight tickers,
    but this is defense in depth."""
    with _state_lock:
        if ticker in _exiting:
            return {'ok': False, 'reason': 'exit already in flight'}
        pos = _positions()
        p = pos.get(ticker)
        if not p:
            return {'ok': False, 'reason': 'no position'}
        _exiting.add(ticker)

    try:
        legs = tr.plan_exit(p, sleeve=sleeve)
        if not legs:
            return {'ok': False, 'reason': 'nothing to sell for that sleeve'}

        # Snapshot entry prices BEFORE we clear state, for P/L.
        opt_entry = float(p.get('option_entry_premium') or 0)
        sh_entry = float(p.get('share_entry_price') or 0)
        opt_qty = int(p.get('option_contracts') or 0)
        sh_qty = int(p.get('shares') or 0)
        opt_sym = p.get('option_symbol', '')
        opt_type = p.get('option_type', '')
        direction = (p.get('direction') or '').upper()

        result = tr.execute_plan(alpaca, ticker, legs, reason=trigger)

        if is_stop:
            for leg in legs:
                tr.mark_stop_out(_no_rebuy, ticker,
                                 'options' if leg['kind'] == 'option' else 'shares')
            _save_no_rebuy()

        # Realized P/L per sleeve, computed from confirmed exit fills against
        # the stored entry prices. Options are always long premium
        # (BUY_TO_OPEN / SELL_TO_CLOSE), so their P/L is (exit - entry).
        # Shares carry the position's direction: long sells to close, a
        # short-shares position (ALLOW_SHORTS sleeves) buys to cover, and
        # its P/L is (entry - exit). plan_exit picks the side; the sign
        # here must match it.
        sh_sign = -1 if direction == 'SHORT' else 1
        for f in result.get('filled', []):
            leg = f['leg']
            fill = f.get('fill') or {}
            xprice = fill.get('filled_avg_price')
            if leg['kind'] == 'option':
                xqty = int(fill.get('filled_qty') or opt_qty)
                exitpx = float(xprice) if xprice not in (None, '') else None
                pnl_d = ((exitpx - opt_entry) * xqty * 100
                         if exitpx is not None and opt_entry else None)
                pnl_p = ((exitpx - opt_entry) / opt_entry * 100
                         if exitpx is not None and opt_entry else None)
                cm.log_trade(ticker=ticker, side=direction, sleeve='OPTIONS',
                             action='SELL', qty=xqty,
                             price=round(exitpx, 4) if exitpx is not None else '',
                             status=fill.get('status', 'submitted'),
                             reason=trigger,
                             pnl_dollars=round(pnl_d, 2) if pnl_d is not None else '',
                             pnl_pct=round(pnl_p, 2) if pnl_p is not None else '',
                             option_symbol=opt_sym, option_type=opt_type)
                _journal_fill(
                    ticker=ticker, symbol=opt_sym or ticker, side='sell',
                    qty=float(xqty) or 1,
                    price=float(exitpx) if exitpx is not None else 0.0,
                    filled_at=datetime.now(ET).replace(tzinfo=None),
                    conditions=dict(conditions or {}), note=trigger)
            else:
                xqty = int(fill.get('filled_qty') or sh_qty)
                exitpx = float(xprice) if xprice not in (None, '') else None
                pnl_d = ((exitpx - sh_entry) * xqty * sh_sign
                         if exitpx is not None and sh_entry else None)
                pnl_p = ((exitpx - sh_entry) / sh_entry * 100 * sh_sign
                         if exitpx is not None and sh_entry else None)
                cm.log_trade(ticker=ticker, side=direction, sleeve='SHARES',
                             action='SELL', qty=xqty,
                             price=round(exitpx, 4) if exitpx is not None else '',
                             status=fill.get('status', 'submitted'),
                             reason=trigger,
                             pnl_dollars=round(pnl_d, 2) if pnl_d is not None else '',
                             pnl_pct=round(pnl_p, 2) if pnl_p is not None else '')
                _journal_fill(
                    ticker=ticker, symbol=ticker, side='sell',
                    qty=float(xqty) or 1,
                    price=float(exitpx) if exitpx is not None else 0.0,
                    filled_at=datetime.now(ET).replace(tzinfo=None),
                    conditions=dict(conditions or {}), note=trigger)

        # If nothing confirmed filled, still log the attempt so the exit is visible.
        if not result.get('filled'):
            cm.log_trade(ticker=ticker, side=direction, sleeve=sleeve.upper(),
                         action='SELL', qty=0, price='', status='failed',
                         reason=trigger + ' (no confirmed fill)')

        # Clear sold sleeves from state (under the same lock as the in-flight flag)
        with _state_lock:
            pos = _positions()       # re-read; another path may have touched it
            p = pos.get(ticker, p)
            for leg in legs:
                if leg['kind'] == 'option':
                    p['option_contracts'] = 0
                    p['option_symbol'] = ''
                else:
                    p['shares'] = 0
            if not p.get('option_contracts') and not p.get('shares'):
                pos.pop(ticker, None)
            else:
                pos[ticker] = p
            _save_positions(pos)

        return {'ok': True}
    finally:
        with _state_lock:
            _exiting.discard(ticker)


# ─── RECONCILIATION (Tier 1) ───────────────────────────────────────────────────

def _occ_underlying(symbol):
    """Underlying ticker from an OCC option symbol.
    OCC format: ROOT + YYMMDD + C/P + 8-digit strike — last 15 chars are
    fixed-width, everything before is the root."""
    return symbol[:-15] if len(symbol) > 15 else symbol


def reconcile_positions(source='scheduled'):
    """Diff broker truth against positions.json and repair the state file.

    The broker is authoritative for WHAT is held (symbols, quantities,
    entry prices). The state file is authoritative for strategy metadata
    (direction, peaks, opened_at). Repairs:

      - Broker option/share position missing from state -> ADD it
        (entry from broker avg price; peaks reset; direction inferred
        from option type: put=short, call=long; shares=long).
      - State claims a sleeve the broker doesn't hold -> CLEAR it.
      - Quantity mismatch -> broker wins.

    Every repair is logged to the conn_event forensics stream and printed.
    Returns the number of repairs made."""
    if not alpaca.is_connected():
        return 0
    try:
        broker = alpaca.get_positions()
    except Exception as e:
        cm.log_forensic('conn_event', event='reconcile', status='error',
                        error=str(e))
        return 0

    # Index broker holdings: shares by ticker, options by underlying.
    b_shares = {}
    b_options = {}
    for b in broker:
        sym = b['symbol']
        raw_qty = b.get('qty') or 0
        is_option = (str(b.get('asset_class', '')).lower().endswith('option')
                     or len(sym) > 15)
        # SAFETY: a negative option quantity is a SHORT option — outside this
        # system's universe entirely. The engine never sells options to open,
        # so any short option is alien (manual order, prior system, or broker
        # anomaly). Refuse to adopt or manage it; alert loudly and leave it
        # for the user to handle by hand.
        if is_option and raw_qty < 0:
            msg = (f'ALIEN SHORT OPTION at broker: {sym} qty {raw_qty}. '
                   f'Engine will NOT manage this. Close it manually.')
            print(f'[RECON][ALERT] {msg}')
            cm.log_forensic('conn_event', event='alien_short_option',
                            status='ALERT', ticker=_occ_underlying(sym),
                            detail=msg, source=source)
            continue
        qty = abs(raw_qty)
        if qty <= 0:
            continue
        if is_option:
            b_options[_occ_underlying(sym)] = {
                'symbol': sym, 'contracts': int(qty),
                'avg_entry': b.get('avg_entry_price'),
            }
        else:
            # Negative share qty = a short share position. ADOPT it with
            # direction='short' (broker is truth; plan_exit knows to buy to
            # cover), rather than skipping it -- skipping would let pass 2
            # clear the state, the engine would see flat, and could pyramid
            # a fresh short on top of the broker's. If ALLOW_SHORTS is off,
            # its existence is alien (manual order or prior system): adopt
            # it anyway so it can be closed through the normal path, but
            # alert loudly.
            is_short = raw_qty < 0
            if is_short and not _OU_ALLOW_SHORTS:
                msg = (f'ALIEN SHORT SHARES at broker: {sym} qty {raw_qty}. '
                       f'ALLOW_SHORTS is off, so this system did not open '
                       f'it. Adopted as direction=short so manual sell can '
                       f'cover it; investigate the origin.')
                print(f'[RECON][ALERT] {msg}')
                cm.log_forensic('conn_event', event='alien_short_shares',
                                status='ALERT', ticker=sym, detail=msg,
                                source=source)
            b_shares[sym] = {'qty': qty, 'short': is_short,
                             'avg_entry': b.get('avg_entry_price')}

    repairs = 0
    with _state_lock:
        pos = _positions()

        # Pass 1: broker holdings missing or mismatched in state.
        for und, o in b_options.items():
            p = pos.get(und, {})
            if p.get('option_symbol') != o['symbol'] or \
               int(p.get('option_contracts') or 0) != o['contracts']:
                opt_type = 'put' if 'P' in o['symbol'][-9:] else 'call'
                p.setdefault('ticker', und)
                p.setdefault('direction',
                             'short' if opt_type == 'put' else 'long')
                p.setdefault('opened_at',
                             datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S'))
                p['option_symbol'] = o['symbol']
                p['option_contracts'] = o['contracts']
                p['option_type'] = opt_type
                if not p.get('option_entry_premium'):
                    p['option_entry_premium'] = float(o['avg_entry'] or 0)
                pos[und] = p
                repairs += 1
                msg = (f'repaired {und}: broker holds {o["contracts"]}x '
                       f'{o["symbol"]} @ {o["avg_entry"]}, state was '
                       f'missing/mismatched')
                print(f'[RECON] {msg}')
                cm.log_forensic('conn_event', event='reconcile_repair',
                                status='added_options', ticker=und,
                                detail=msg, source=source)

        for t, s in b_shares.items():
            p = pos.get(t, {})
            if float(p.get('shares') or 0) != float(s['qty']):
                p.setdefault('ticker', t)
                p.setdefault('direction',
                             'short' if s.get('short') else 'long')
                p.setdefault('opened_at',
                             datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S'))
                p['shares'] = s['qty']
                if not p.get('share_entry_price'):
                    p['share_entry_price'] = float(s['avg_entry'] or 0)
                pos[t] = p
                repairs += 1
                msg = f'repaired {t}: broker holds {s["qty"]} shares, state disagreed'
                print(f'[RECON] {msg}')
                cm.log_forensic('conn_event', event='reconcile_repair',
                                status='added_shares', ticker=t,
                                detail=msg, source=source)

        # Pass 2: state claims sleeves the broker doesn't hold.
        for t in list(pos.keys()):
            p = pos[t]
            changed = False
            if p.get('option_contracts', 0) > 0 and t not in b_options:
                p['option_contracts'] = 0
                p['option_symbol'] = ''
                changed = True
            if p.get('shares', 0) > 0 and t not in b_shares:
                p['shares'] = 0
                changed = True
            if changed:
                repairs += 1
                msg = f'cleared {t}: state claimed holdings the broker does not have'
                print(f'[RECON] {msg}')
                cm.log_forensic('conn_event', event='reconcile_repair',
                                status='cleared', ticker=t, detail=msg,
                                source=source)
            if not p.get('option_contracts') and not p.get('shares'):
                # Position fully flat — drop the entry so the dashboard and
                # monitor stop tracking a ghost.
                pos.pop(t, None)

        if repairs:
            _save_positions(pos)
    return repairs


# ─── BAR-CLOSE LOOP ────────────────────────────────────────────────────────────

def _seconds_to_next_boundary(grace=15):
    now = datetime.now(ET)
    past = (now.minute % 10) * 60 + now.second
    wait = (600 - past) + grace
    return wait if wait > 0 else grace


def bar_loop():
    global _status_message
    print('[BAR] loop started (wall-aligned, 15s grace)')
    while True:
        try:
            time.sleep(_seconds_to_next_boundary())
            if not alpaca.is_connected():
                continue
            cfg = cm.load_config()
            try:
                if not alpaca.is_market_open():
                    _status_message = 'market closed'
                    continue
            except Exception:
                continue

            watch = [w['ticker'].upper() for w in cfg.get('watchlist', [])]
            _sync_engine_overrides(cfg)
            pos = _positions()
            bars_by_ticker = _fetch_basket_bars(watch)
            for ticker in watch:
                try:
                    _evaluate_ticker(ticker, cfg, pos,
                                     bars_by_ticker.get(ticker, {}))
                except Exception as e:
                    cm.log_forensic('signal_eval', ticker=ticker,
                                    status='error', error=str(e))
            _status_message = f'last bar sweep {datetime.now(ET):%H:%M:%S}'
        except Exception as e:
            print(f'[BAR] loop error: {e}')
            time.sleep(30)


# One BarCache per (timeframe, role) stream. Built lazily because streams
# depend on which engines the resolver maps the basket to.
_bar_caches = {}     # {(tf, role): BarCache}


def clear_bar_caches():
    """Full refill on next sweep. Call after any reconnect: a data gap must
    never leave a silent hole inside an EMA window."""
    for c in _bar_caches.values():
        c.clear()


def _drop_forming(bars, tf, now_et):
    """Drop the in-progress bar for any timeframe, CENTRALLY.

    Alpaca hands back the partial current-period bar once it has trades. A
    bar is closed only when its start + width has passed. Before this, the
    Ripster path trusted the loop's boundary+15s timing to keep forming bars
    out of the feed -- which held until a slow sweep (1,000 names, a retry)
    crossed the next minute boundary. Then a symbol evaluated a bar with 60
    seconds of data as closed, _evaluate_ticker recorded that TIMESTAMP as
    seen, and the completed bar (same timestamp) was skipped forever.

    Dropping here fixes both halves at once: every engine sees closed bars
    only, and the dedupe key is always a closed bar's time. engine_ou keeps
    its own internal drop as defense in depth; on filtered input it is a
    no-op."""
    if not bars:
        return bars
    tf_min = hlf.TF_MINUTES.get(tf)
    if not tf_min:
        return bars
    try:
        last = datetime.fromisoformat(bars[-1]['time'])
    except (ValueError, KeyError, TypeError):
        return bars
    if last.tzinfo is None:
        last = last.replace(tzinfo=ET)
    if last.astimezone(ET) + timedelta(minutes=tf_min) > now_et:
        return bars[:-1]
    return bars


def _fetch_basket_bars(watch):
    """Batched, cached data pull for the whole basket.

    Asks the resolver which (timeframe, role) streams the basket's engines
    consume and which symbols need each, refreshes one BarCache per stream,
    and returns {ticker: {role: [bars]}} ready for engine.evaluate().
    Forming bars are stripped per stream on the way out (see _drop_forming);
    the cache itself keeps them so its incremental refresh window stays
    anchored to the true newest bar.

    Cost at 1,000 names: first sweep pays full history (a few requests per
    stream); every later sweep pulls only bars newer than the cache tail.
    Serial per-ticker fetching is gone entirely."""
    out = {t: {} for t in watch}
    if not watch:
        return out
    plan = resolver.fetch_plan(watch)
    now = datetime.now(ET)
    for (tf, role), spec in plan.items():
        cache = _bar_caches.get((tf, role))
        if cache is None or cache.limit < spec['limit']:
            cache = BarCache(tf, spec['limit'])
            _bar_caches[(tf, role)] = cache
        try:
            got = cache.refresh(alpaca, spec['symbols'])
        except Exception as e:
            cm.log_forensic('api_event', event='basket_fetch', status='error',
                            detail=f'{tf}/{role}', error=str(e))
            continue
        for sym, bars in got.items():
            out[sym][role] = _drop_forming(bars, tf, now)
    return out


def _evaluate_ticker(ticker, cfg, pos, bars):
    bars10 = bars.get('primary')
    if not bars10:
        return
    # Skip if we already evaluated this bar (slow data / double boundary)
    newest = bars10[-1]['time']
    if _last_bar_seen.get(ticker) == newest:
        return
    _last_bar_seen[ticker] = newest

    open_dir = pos.get(ticker, {}).get('direction')
    # Mirror the engine's worldview: present the inverted held direction so its
    # internal flip/close reasoning matches our inverted execution.
    eval_dir = _invert_dir(open_dir) if INVERT_SIGNALS else open_dir
    engine = resolver.resolve(ticker)
    # Stateless engines that need a time stop get the entry timestamp handed
    # to them; they never hold it. Ripster ignores this argument.
    d = engine.evaluate(ticker, bars, position_direction=eval_dir,
                        position=pos.get(ticker))

    # Log every decision
    ctx = d.get('step2') or {}
    cm.log_signal(
        ticker=ticker, bar_time=newest,
        gate_passed=(d.get('gate') or {}).get('passed', ''),
        gate_reason=(d.get('gate') or {}).get('reason', ''),
        trend=ctx.get('trend', ''),
        trigger=('fresh_long' if ctx.get('fresh_long')
                 else ('fresh_short' if ctx.get('fresh_short') else 'none')),
        price_vs_5_12=ctx.get('price_vs_5_12', ''),
        price_vs_34_50=ctx.get('price_vs_34_50', ''),
        exit_kind=d.get('exit_kind') or '',
        final_action=(_invert_action(d['action']) if INVERT_SIGNALS
                      else d['action']),
        notes=(f'[{engine.name}] '
               + ('INVERTED; ' if INVERT_SIGNALS else '')
               + ('engine_enabled' if _engine_enabled else 'OBSERVE ONLY')),
    )

    if not _engine_enabled or d['action'] == 'NONE':
        return

    a = _invert_action(d['action']) if INVERT_SIGNALS else d['action']
    # Exits are never gated by per-ticker pause (design A: pause blocks
    # entries only; open positions keep full exit protection).
    if a.startswith('EXIT_LONG') or a.startswith('EXIT_SHORT'):
        ekind = d.get('exit_kind') or 'ride_end'
        is_stop = (ekind == 'structural')
        # Engines that explain themselves (flip.reason) win over the generic
        # Ripster prose, so one order log reads correctly for both engines.
        reason = (d.get('flip') or {}).get('reason')
        if not reason:
            reason = ('34/50 structural stop (close through cloud)' if is_stop
                      else '5/12 close (ride over)')
        _do_exit(ticker, 'both', reason, is_stop=is_stop,
                 conditions=_cond_from_ctx(ctx, d))

    mode = 'pause'
    for w in cfg.get('watchlist', []):
        if w['ticker'].upper() == ticker:
            mode = w.get('mode', 'pause')
            break
    if mode != 'run':
        return   # paused: evaluated and logged above, but no new entries

    if a.endswith('ENTER_LONG'):
        _do_entry(ticker, 'long', cfg, allow_options=d.get('volume_ok', True),
                  conditions=_cond_from_ctx(ctx, d))
    elif a.endswith('ENTER_SHORT'):
        _do_entry(ticker, 'short', cfg, allow_options=d.get('volume_ok', True),
                  conditions=_cond_from_ctx(ctx, d))


# ─── 10-SECOND MONITOR ─────────────────────────────────────────────────────────

def monitor_loop():
    print('[MON] monitor started (reconcile every ~5 min; exits are engine-driven on bar close)')
    tick = 0
    while True:
        try:
            time.sleep(10)
            if not alpaca.is_connected():
                continue
            tick += 1
            if tick % 30 == 0:          # ~every 5 minutes
                reconcile_positions(source='scheduled')
            # PURE-STRUCTURAL EXITS: the engine's 5/12-close and 34/50-close
            # are the ONLY exits, fired from bar_loop on each 10-min bar close
            # via evaluate(). The monitor no longer watches premiums or sets
            # stops — its sole job here is periodic broker reconciliation.
        except Exception as e:
            print(f'[MON] loop error: {e}')
            time.sleep(10)


# ─── ROUTES ────────────────────────────────────────────────────────────────────

def _force_engine_off():
    """Disarm the engine from outside the routes module.

    Used by the Setup tab: any credential or account-type change drops the
    engine to OBSERVE ONLY so a live account is never inherited by an engine
    that was armed against paper.
    """
    global _engine_enabled
    _engine_enabled = False


import journal_routes
import ledger_routes
import screener_routes
import setup_routes
app.register_blueprint(journal_routes.bp)
app.register_blueprint(ledger_routes.make_bp(alpaca))
app.register_blueprint(screener_routes.make_bp(alpaca))
app.register_blueprint(setup_routes.make_bp(alpaca, _force_engine_off))


@app.route('/api/health')
def health():
    return jsonify({
        'connected': alpaca.is_connected(),
        'paper': alpaca.paper,
        'engine_enabled': _engine_enabled,
        'status': _status_message,
        'positions': len(_positions()),
    })


@app.route('/api/connect', methods=['POST'])
def connect():
    cfg = cm.load_config()
    r = alpaca.connect(cfg.get('alpaca_key', ''), cfg.get('alpaca_secret', ''),
                       paper=cfg.get('paper_trading', True))
    if r.get('status') == 'ok':
        clear_bar_caches()   # gap-safety: full refill after any reconnect
    return jsonify(r)


@app.route('/api/disconnect', methods=['POST'])
def disconnect():
    alpaca.disconnect()
    return jsonify({'status': 'ok'})


@app.route('/api/account')
def account():
    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400
    return jsonify(alpaca.get_account())


@app.route('/api/engine', methods=['GET', 'POST'])
def engine_toggle():
    global _engine_enabled
    if request.method == 'POST':
        _engine_enabled = bool((request.json or {}).get('enabled', False))
        cm.log_forensic('conn_event', event='engine_toggle',
                        status='on' if _engine_enabled else 'off')
    return jsonify({'enabled': _engine_enabled})


@app.route('/api/engines')
def engines():
    """Registered engines, the global default, and per-ticker overrides.
    Feeds the dashboard's engine picker."""
    return jsonify({'engines': registry.names(),
                    'default': resolver.default_name,
                    'overrides': dict(resolver.by_ticker)})


# ─── BASKETS (the noun's API; builder UI comes later) ─────────────────────────

@app.route('/api/baskets', methods=['GET', 'POST'])
def baskets_route():
    if request.method == 'GET':
        out = {}
        for name, b in bk.load_baskets().items():
            b = b or {}
            out[name] = {'n_symbols': len(b.get('symbols', [])),
                         'engine': b.get('engine'),
                         'default_allocation_pct':
                             b.get('default_allocation_pct', 1.0),
                         'notes': b.get('notes', '')}
        return jsonify(out)

    body = request.json or {}
    action = body.get('action')
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'basket name required'}), 400

    if action == 'save':
        eng = (body.get('engine') or '').strip()
        if eng and eng not in registry.names():
            return jsonify({'error': f'unknown engine: {eng}'}), 400
        syms, seenb = [], set()
        for s in body.get('symbols', []):
            u = str(s).strip().upper()
            if u and u not in seenb:
                seenb.add(u)
                syms.append(u)
        if not syms:
            return jsonify({'error': 'basket needs at least one symbol'}), 400
        baskets = bk.load_baskets()
        baskets[name] = {
            'symbols': syms,
            'engine': eng or None,
            'default_allocation_pct':
                float(body.get('default_allocation_pct', 1.0)),
            'notes': body.get('notes', ''),
        }
        bk.save_baskets(baskets)
        cm.log_forensic('conn_event', event='basket_save', status='ok',
                        detail=f'{name}: {len(syms)} symbols, '
                               f'engine={eng or "(default)"}')
        return jsonify({'saved': name, 'n_symbols': len(syms)})

    if action == 'delete':
        baskets = bk.load_baskets()
        if name not in baskets:
            return jsonify({'error': f'no basket {name}'}), 404
        baskets.pop(name)
        bk.save_baskets(baskets)
        # Watchlist entries keep trading; they just lose the basket's engine
        # mapping and fall through to per-ticker/global on the next sync.
        return jsonify({'deleted': name})

    if action == 'deploy':
        # Basket -> watchlist. New symbols enter PAUSED at the basket's
        # default allocation (same safety posture as bulk_add). Existing
        # symbols keep their allocation and mode; only the basket tag is
        # set so the resolver's basket tier picks them up.
        try:
            b = bk.get_basket(name)
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        cfg = cm.load_config()
        existing = {w['ticker'].upper(): w for w in cfg.get('watchlist', [])}
        alloc = float(b.get('default_allocation_pct', 1.0))
        added, tagged = [], []
        for s in b['symbols']:
            if s in existing:
                if existing[s].get('basket') != name:
                    existing[s]['basket'] = name
                    tagged.append(s)
            else:
                cfg['watchlist'].append({'ticker': s,
                                         'allocation_pct': alloc,
                                         'mode': 'pause',
                                         'basket': name})
                added.append(s)
        cm.save_config(cfg)
        _sync_engine_overrides(cfg)
        cm.log_forensic('conn_event', event='basket_deploy', status='ok',
                        detail=f'{name}: +{len(added)} new (paused), '
                               f'{len(tagged)} retagged')
        return jsonify({'deployed': name, 'added': added, 'tagged': tagged,
                        'engine': (resolver.by_basket.get(name)
                                   or resolver.default_name)})

    return jsonify({'error': f'unknown action: {action}'}), 400


# ─── BACKTEST JOB (one at a time, background thread, poll for status) ──────────

# ─── JOB PROGRESS ─────────────────────────────────────────────────────────────
# One shape for every long-running job, so the UI has one renderer:
#   {'phase': str, 'done': int, 'total': int, 'phase_i': int, 'n_phases': int}
# The GET route stamps elapsed_s at request time rather than the worker writing
# it, so a stalled worker still shows a climbing clock instead of a frozen one.

def _mk_progress(lock, get_job, phase, phase_i=1, n_phases=1, unit='symbols'):
    """Return a progress(done, total, unit=None) callback bound to a job dict.

    The unit varies by path: a 1,000-name screen counts SYMBOLS, while a
    one-symbol backtest has no symbol-level resolution and counts BARS. The
    callee decides and says so; the UI just prints what it is.

    Stamps phase_started the first time a given phase reports, so the ETA is
    computed from THIS phase's rate. Fetching and replaying run at wildly
    different speeds; one global rate would produce an ETA that lurches every
    time the phase changes."""
    def _p(done, total, unit_=None):
        # PAUSE/STOP LEVER. This callback fires between symbols on every job,
        # so it is the one place all three can be frozen or cancelled without
        # touching worker processes. 'paused' blocks here (the pool goes idle,
        # in-flight symbols having already been dispatched); 'stopping' and
        # 'stopped' return 'stop', which the backtester's dispatch loop reads
        # as cancel. No pause ceiling by design: stop is honored FROM paused,
        # so a forgotten paused job is always one button from released.
        while True:
            with lock:
                job = get_job()
                st = job.get('state')
                if st in ('stopping', 'stopped'):
                    return 'stop'
                if st != 'paused':
                    break
            time.sleep(0.15)      # paused: hold outside the lock, then recheck
        with lock:
            job = get_job()
            if job.get('state') != 'running':
                return 'stop' if job.get('state') in ('stopping',
                                                      'stopped') else None
            u = unit_ or unit
            prev = job.get('progress') or {}
            same = prev.get('phase') == phase and prev.get('unit') == u
            started = (prev.get('phase_started') if same
                       else datetime.now(ET).isoformat())
            job['progress'] = {'phase': phase, 'done': int(done),
                               'total': int(total), 'unit': u,
                               'phase_i': phase_i,
                               'n_phases': n_phases, 'phase_started': started}
        return None
    # Seed the phase at zero. Without this, phase_started is stamped by the
    # FIRST chunk report -- which for a 2-chunk fetch is already 57% done, so
    # phase_elapsed is ~0 and the ETA can never be computed for that phase.
    _p.seed = lambda total, unit_=None: _p(0, total, unit_)
    return _p


_BUSY_STATES = ('running', 'paused', 'stopping')


def _job_busy(job):
    """A job in any of these states owns the single run slot. A new start
    must 409 until it reaches a terminal state (done/error/stopped/idle).
    Paused counts as busy: the pool and partial state are still held."""
    return job.get('state') in _BUSY_STATES


def _apply_control(job, action):
    """Map a control action onto the next state, or return an error string.
    stop wins from any live state (the no-ceiling escape hatch); pause only
    from running; resume only from paused."""
    st = job.get('state')
    if action == 'stop':
        if st not in _BUSY_STATES:
            return None, f'nothing to stop (state: {st})'
        return 'stopping', None
    if action == 'pause':
        if st != 'running':
            return None, f'can only pause a running job (state: {st})'
        return 'paused', None
    if action == 'resume':
        if st != 'paused':
            return None, f'can only resume a paused job (state: {st})'
        return 'running', None
    return None, f'unknown action: {action!r}'


def _stop_requested(get_job, lock):
    """True if a control route asked this job to stop. Read under the lock
    so a stop landing mid-write is seen on the next check."""
    with lock:
        return get_job().get('state') in ('stopping', 'stopped')


def _with_elapsed(job):
    """Copy of a job with elapsed_s / eta_s filled in at REQUEST time.

    Computed here, not in the worker, so a stalled worker shows a climbing
    clock instead of a frozen one. Never mutates the job."""
    if job.get('state') != 'running' or not job.get('started'):
        return job
    out = dict(job)
    now = datetime.now(ET)
    try:
        out['elapsed_s'] = max(
            0.0, (now - datetime.fromisoformat(job['started'])).total_seconds())
    except ValueError:
        pass

    p = job.get('progress') or {}
    done, total = p.get('done') or 0, p.get('total') or 0
    if done > 0 and total > done and p.get('phase_started'):
        try:
            ps = datetime.fromisoformat(p['phase_started'])
            phase_elapsed = (now - ps).total_seconds()
            if phase_elapsed > 0.5:
                prog = dict(p)
                prog['phase_elapsed_s'] = phase_elapsed
                prog['eta_s'] = phase_elapsed / done * (total - done)
                out['progress'] = prog
        except ValueError:
            pass
    return out


_bt_lock = threading.Lock()
_bt_job = {'state': 'idle'}       # idle | running | done | error


def _run_backtest_job(params):
    """Thread body. Fetch -> replay -> CSV. All outcomes land in _bt_job;
    this thread must never raise out."""
    global _bt_job
    try:
        symbols = params['symbols']
        engine = registry.get(params['engine'])
        base_start = datetime.strptime(params['start'], '%Y-%m-%d')
        end = (datetime.strptime(params['end'], '%Y-%m-%d')
               if params.get('end') else None)

        has_macro = 'macro' in engine.timeframes
        n_phases = 3 if has_macro else 2
        _get = lambda: _bt_job

        # Warmup padding is per ROLE, derived from that stream's timeframe and
        # the engine's declared history. A daily engine needs ~5 years; a
        # 10-min engine needs ~3 weeks. One shared pad starves one of them.
        tf_p = engine.timeframes['primary']
        n_p = engine.history.get('primary', 300)
        start = warmup_start(base_start, tf_p, n_p)

        p_primary = _mk_progress(_bt_lock, _get, f'fetching {tf_p}', 1, n_phases)
        p_primary.seed(len(symbols))
        bars = _bt_fetch(alpaca, symbols, tf_p, start, end, progress=p_primary)
        macro = {}
        if has_macro:
            tf_m = engine.timeframes['macro']
            n_m = engine.history.get('macro', 200)
            m_start = warmup_start(base_start, tf_m, n_m)
            p_macro = _mk_progress(_bt_lock, _get, f'fetching {tf_m}', 2, n_phases)
            p_macro.seed(len(symbols))
            macro = _bt_fetch(alpaca, symbols, tf_m, m_start, end,
                              progress=p_macro)
        missing = [s for s in symbols if not bars.get(s)]

        result = backtester.run_basket_parallel(
            engine.name, bars, macro,
            capital=params['capital'], alloc_pct=params['alloc_pct'],
            alloc_dollars=params.get('alloc_dollars'),
            slippage_bps=params['slippage_bps'],
            progress=_mk_progress(_bt_lock, _get, 'replaying',
                                  n_phases, n_phases))

        if result.get('stopped') or _stop_requested(_get, _bt_lock):
            with _bt_lock:
                _bt_job = {'state': 'stopped', 'params': params,
                           'finished': datetime.now(ET).isoformat()}
            cm.log_forensic('conn_event', event='backtest', status='stopped',
                            detail=params['label'])
            return

        # float('inf') is not valid JSON; a run with zero losing trades
        # would poison the status payload. None at the API boundary; the
        # UI renders it as 'inf'.
        if result['metrics'].get('profit_factor') == float('inf'):
            result['metrics']['profit_factor'] = None

        csv_path = ''
        trades = result['trades']
        if trades:
            import csv as _csv
            csv_path = os.path.join(
                cm.DATA_DIR,
                f"backtest_{params['label']}_{engine.name}_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv")
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                w = _csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(sorted(trades, key=lambda t: t['entry_time']))

        bars_got = {s: len(b) for s, b in bars.items()}
        thin = sorted(s for s, n in bars_got.items() if n < n_p)
        with _bt_lock:
            _bt_job = {'state': 'done', 'params': params,
                       'warmup_bars_required': n_p,
                       'thin_history': thin[:20],
                       'n_thin_history': len(thin),
                       'metrics': result['metrics'],
                       'equity_curve': result['equity_curve'],
                       'per_symbol': result.get('per_symbol', {}),
                       'n_trades': len(trades), 'trades_csv': csv_path,
                       'skipped_no_data': missing,
                       'finished': datetime.now(ET).isoformat()}
        cm.log_forensic('conn_event', event='backtest', status='done',
                        detail=f"{params['label']} {engine.name}: "
                               f"{len(trades)} trades")
    except Exception as e:
        with _bt_lock:
            _bt_job = {'state': 'error', 'params': params, 'error': str(e)}
        cm.log_forensic('conn_event', event='backtest', status='error',
                        error=str(e))


@app.route('/api/baskets/<name>')
def basket_one(name):
    """Full basket (symbols included) for the editor."""
    try:
        return jsonify(bk.get_basket(name))
    except KeyError as e:
        return jsonify({'error': str(e)}), 404


@app.route('/api/backtest', methods=['GET', 'POST'])
def backtest_route():
    """GET: current job status (trades live in the CSV, not the payload).
    POST: start a run. Body: {basket | symbols[], engine?, start, end?,
    capital?, alloc_pct?, slippage_bps?}. One job at a time; 409 if busy."""
    global _bt_job
    if request.method == 'GET':
        with _bt_lock:
            return jsonify(_with_elapsed(_bt_job))

    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400
    with _bt_lock:
        if _job_busy(_bt_job):
            return jsonify({'error': 'backtest already running',
                            'params': _bt_job.get('params')}), 409

    body = request.json or {}
    basket_engine = None
    if body.get('basket'):
        try:
            b = bk.get_basket(body['basket'])
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        symbols = b['symbols']
        basket_engine = b.get('engine')
        label = body['basket']
    else:
        symbols = [str(s).strip().upper() for s in body.get('symbols', [])
                   if str(s).strip()]
        label = 'adhoc'
    if not symbols:
        return jsonify({'error': 'no symbols'}), 400
    if not body.get('start'):
        return jsonify({'error': 'start (YYYY-MM-DD) required'}), 400

    eng_name = body.get('engine') or basket_engine or resolver.default_name
    if eng_name not in registry.names():
        return jsonify({'error': f'unknown engine: {eng_name}'}), 400

    params = {'symbols': symbols, 'engine': eng_name, 'label': label,
              'start': body['start'], 'end': body.get('end'),
              'capital': float(body.get('capital', 100000.0)),
              'alloc_pct': float(body.get('alloc_pct', 1.0)),
              'alloc_dollars': (float(body['alloc_dollars'])
                                if body.get('alloc_dollars') else None),
              'slippage_bps': float(body.get('slippage_bps', 2.0))}
    with _bt_lock:
        _bt_job = {'state': 'running', 'params': params,
                   'started': datetime.now(ET).isoformat()}
    threading.Thread(target=_run_backtest_job, args=(params,),
                     daemon=True).start()
    return jsonify({'started': True, 'params': params})


@app.route('/api/backtest/control', methods=['POST'])
def backtest_control():
    """{action: pause|resume|stop}. Flips the job state; the running thread
    reads it at its next progress tick. stop discards all work."""
    global _bt_job
    action = (request.json or {}).get('action')
    with _bt_lock:
        nxt, err = _apply_control(_bt_job, action)
        if err:
            return jsonify({'error': err}), 409
        _bt_job['state'] = nxt
    return jsonify({'ok': True, 'state': nxt})


# ─── UNIVERSE SCREENER ────────────────────────────────────────────────────────
# Runs the mounted engine's backtest across a sector (or the whole universe)
# and LOGS per-symbol stats: Sharpe, expectancy, profit factor, win rate,
# trade count. It does NOT rank-and-filter into a basket. The output is a
# table to study; selection is a later, human decision. A LOW_SAMPLE_FLOOR
# flags names whose trade count is too small to trust the Sharpe, without
# deleting them.

LOW_SAMPLE_FLOOR = 20

def _seed_sectors():
    """Copy the shipped sectors.json into DATA_DIR on first run. It's a
    hand-built universe asset, not app-generated state, so it travels with
    the code and seeds the user's data dir once. Never overwrites: if the
    user has curated their own, we leave it alone."""
    dst = sec.SECTORS_FILE
    if os.path.exists(dst):
        return
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'sectors.json')
    if os.path.exists(src):
        import shutil
        shutil.copy(src, dst)
        cm.log_forensic('conn_event', event='seed_sectors', status='ok',
                        detail=dst)

_scr_lock = threading.Lock()
_scr_job = {'state': 'idle'}


@app.route('/api/sectors')
def sectors_route():
    """The hardcoded universe: sectors with ticker counts and subsectors."""
    return jsonify({'sectors': [
        {'name': name, 'n': n, 'subsectors': sec.subsectors(name)}
        for name, n in sec.list_sectors()]})


def _bars_between(by_sym, lo=None, hi=None):
    """Slice each symbol's bars to lo.date() <= bar date <= hi.date().
    Bars are oldest-first, so the hi cut can stop early."""
    out = {}
    for s, bs in (by_sym or {}).items():
        kept = []
        for b in bs:
            d = datetime.fromisoformat(b['time']).date()
            if hi is not None and d > hi.date():
                break
            if lo is not None and d < lo.date():
                continue
            kept.append(b)
        if kept:
            out[s] = kept
    return out


def _port_metrics(result):
    """Pooled metrics -> JSON-safe portfolio block (inf and errors out)."""
    m = dict(result['metrics'])
    m.pop('errors', None)
    if m.get('profit_factor') == float('inf'):
        m['profit_factor'] = None
    return m


def _run_screen_job(params):
    """Thread body: [optional OU gate] -> fetch -> parallel replay ->
    per-symbol table + pooled portfolio -> optional two-pass Sharpe eval ->
    dated CSVs. Never raises out; all outcomes land in _scr_job.

    OU GATE (ou_gate=True): before any P&L is simulated, every name's
    pooled theta line is fit across ASSIGN_TFS_DEFAULT and snapped onto the
    OU family by assign.py's arithmetic. Names with no clock (multi_scale /
    no_spring / out-of-band) are EXCLUDED from the sim, and each survivor
    replays on ITS OWN assigned engine via the engine-map path. This is the
    characterize-first doctrine: the gate selects on a bar-level statistic
    with ~3 orders of magnitude more samples than P&L, and the timeframe
    comes from arithmetic, not search. Residual honesty note: the gate is
    measured on the same window the P&L is graded on, so a name that
    mean-reverted in-sample passes AND scores partly by the same luck. The
    sharpe_weight split below does not seal that; only a walk-forward gate
    would. Known, accepted, stated.

    Sizing (alloc_mode): 'pct' | 'dollars' | 'even' (capital / N).
    compound=True compounds each name's sleeve on its own realized P/L
    (see backtester.run_symbol); live compounds the shared pot instead.

    sharpe_weight=True: weights fit on the FIRST half (even split),
    portfolio graded on the SECOND half next to an even-split leg on the
    same eval window. The in-sample full-window number is deliberately
    not computed."""
    global _scr_job
    try:
        symbols = params['symbols']
        base_start = datetime.strptime(params['start'], '%Y-%m-%d')
        end = (datetime.strptime(params['end'], '%Y-%m-%d')
               if params.get('end') else None)
        capital = params['capital']
        mode = params['alloc_mode']
        val = params['alloc_value']
        sharpe_w = bool(params.get('sharpe_weight'))
        ou_gate = bool(params.get('ou_gate'))
        compound = bool(params.get('compound'))
        slippage = params['slippage_bps']
        _get = lambda: _scr_job

        gate = None
        engine_of = None                 # {sym: engine_name} in pipeline mode
        macro = {}

        if ou_gate:
            # ── stage 1: measure clocks, assign engines, filter ──
            theta_tfs = list(ASSIGN_TFS_DEFAULT)
            stage1 = len(theta_tfs) + 1
            by_symbol = {s: {} for s in symbols}
            for i, tf in enumerate(theta_tfs, 1):
                p_tf = _mk_progress(_scr_lock, _get, f'gate: fetching {tf}',
                                    i, stage1)
                p_tf.seed(len(symbols))
                got = _hl_fetch(alpaca, symbols, tf, base_start, end,
                                progress=p_tf)
                for s in symbols:
                    if got.get(s):
                        by_symbol[s][tf] = got[s]
            p_g = _mk_progress(_scr_lock, _get, 'gate: measuring clocks',
                               stage1, stage1)
            p_g.seed(len(symbols))
            assignments = asg.assign_from_bars(by_symbol, progress=p_g)
            if _stop_requested(_get, _scr_lock):
                with _scr_lock:
                    _scr_job = {'state': 'stopped', 'params': params,
                                'finished': datetime.now(ET).isoformat()}
                cm.log_forensic('conn_event', event='screen',
                                status='stopped', detail=params['label'])
                return
            passed = [a for a in assignments if a['engine']]
            gate = {'n_measured': len(assignments), 'n_passed': len(passed),
                    'timeframes': theta_tfs, 'assignments': assignments}
            engine_of = {a['ticker']: a['engine'] for a in passed}
            engine_display = 'ou_pipeline'

            if not passed:
                with _scr_lock:
                    _scr_job = {'state': 'done', 'params': params,
                                'table': [], 'n_screened': 0, 'csv': '',
                                'portfolio': None, 'sizing': '',
                                'sharpe_eval': None, 'gate': gate,
                                'engine_display': engine_display,
                                'low_sample_floor': LOW_SAMPLE_FLOOR,
                                'skipped_no_data': [], 'errors': {},
                                'finished': datetime.now(ET).isoformat()}
                cm.log_forensic('conn_event', event='screen', status='done',
                                detail=f"{params['label']}: OU gate passed "
                                       f"0/{len(assignments)}; no sim")
                return

            # ── stage 2: sim-grade fetch per assigned engine's stream ──
            by_stream = {}
            for t, en in engine_of.items():
                e = registry.get(en)
                key = (e.timeframes['primary'],
                       e.history.get('primary', 300))
                by_stream.setdefault(key, []).append(t)
            used = sorted(by_stream.items(),
                          key=lambda kv: hlf.TF_MINUTES[kv[0][0]])
            n_phases = stage1 + len(used) + 1 + (3 if sharpe_w else 0)
            bars = {}
            for j, ((tf, nbars), syms) in enumerate(used):
                pj = _mk_progress(_scr_lock, _get, f'fetching {tf} (sim)',
                                  stage1 + 1 + j, n_phases)
                pj.seed(len(syms))
                got = _bt_fetch(alpaca, syms, tf,
                                warmup_start(base_start, tf, nbars), end,
                                progress=pj)
                bars.update(got)
            replay_slot = stage1 + len(used) + 1
            pool = list(engine_of)
        else:
            engine = registry.get(params['engine'])
            engine_display = engine.name
            has_macro = 'macro' in engine.timeframes
            n_fetch = 2 if has_macro else 1
            n_phases = n_fetch + 1 + (3 if sharpe_w else 0)

            tf_p = engine.timeframes['primary']
            n_p = engine.history.get('primary', 300)
            start = warmup_start(base_start, tf_p, n_p)
            p_primary = _mk_progress(_scr_lock, _get, f'fetching {tf_p}',
                                     1, n_phases)
            p_primary.seed(len(symbols))
            bars = _bt_fetch(alpaca, symbols, tf_p, start, end,
                             progress=p_primary)
            if has_macro:
                tf_m = engine.timeframes['macro']
                n_m = engine.history.get('macro', 200)
                m_start = warmup_start(base_start, tf_m, n_m)
                p_macro = _mk_progress(_scr_lock, _get, f'fetching {tf_m}',
                                       2, n_phases)
                p_macro.seed(len(symbols))
                macro = _bt_fetch(alpaca, symbols, tf_m, m_start, end,
                                  progress=p_macro)
            replay_slot = n_fetch + 1
            pool = symbols

        live = [s for s in pool if bars.get(s)]
        if not live:
            raise RuntimeError('no symbols returned any primary bars')

        def _sim_engine(subset):
            """Engine argument for a run over `subset` symbols."""
            if ou_gate:
                return {s: engine_of[s] for s in subset}
            return params['engine']

        def _pdepth(s):
            en = engine_of[s] if ou_gate else params['engine']
            e = registry.get(en)
            return e.timeframes['primary'], e.history.get('primary', 300)

        # ── sizing ──
        alloc_pct, alloc_dollars, budgets = 1.0, None, None
        if mode == 'pct':
            alloc_pct = val
            sizing = (f'{val:g}% of ${capital:,.0f} = '
                      f'${capital * val / 100:,.0f}/name')
        elif mode == 'dollars':
            alloc_dollars = val
            sizing = f'${val:,.0f}/name'
        else:
            per = capital / len(live)
            budgets = {s: per for s in live}
            sizing = (f'even split: ${capital:,.0f} / {len(live)} names '
                      f'= ${per:,.0f}/name')
        if compound:
            sizing += ' · compounded (per-name sleeve)'

        result = backtester.run_basket_parallel(
            _sim_engine(live), bars, macro, capital=capital,
            alloc_pct=alloc_pct, alloc_dollars=alloc_dollars,
            budget_by_symbol=budgets, compound=compound,
            slippage_bps=slippage,
            progress=_mk_progress(_scr_lock, _get, 'replaying',
                                  replay_slot, n_phases))
        if result.get('stopped') or _stop_requested(_get, _scr_lock):
            with _scr_lock:
                _scr_job = {'state': 'stopped', 'params': params,
                            'finished': datetime.now(ET).isoformat()}
            cm.log_forensic('conn_event', event='screen', status='stopped',
                            detail=params['label'])
            return
        portfolio = _port_metrics(result)

        table = []
        for sym, m in result.get('per_symbol', {}).items():
            s_sec, s_sub = sec.sector_of(sym)
            n = m.get('n_trades', 0)
            pf = m.get('profit_factor')
            if pf == float('inf'):
                pf = None
            table.append({
                'ticker': sym,
                'engine': engine_of.get(sym) if ou_gate else engine_display,
                'sector': s_sec or '', 'subsector': s_sub or '',
                'n_trades': n, 'sharpe': m.get('sharpe'),
                'sortino': m.get('sortino'), 'expectancy': m.get('expectancy'),
                'profit_factor': pf, 'win_rate_pct': m.get('win_rate_pct'),
                'total_pnl': m.get('total_pnl'),
                'low_sample': n < LOW_SAMPLE_FLOOR,
            })
        def _key(row):
            sh = row['sharpe'] if row['sharpe'] is not None else -1e9
            return (0 if not row['low_sample'] else 1, -sh)
        table.sort(key=_key)

        # ── optional Sharpe-weighted eval: fit first half, grade second ──
        sharpe_eval = None
        if sharpe_w:
            end_eff = end or datetime.now()
            mid = base_start + (end_eff - base_start) / 2

            fit_bars = _bars_between(bars, hi=mid)
            fit_macro = _bars_between(macro, hi=mid) if macro else {}
            fit_live = [s for s in live if fit_bars.get(s)]
            fit_bud = {s: capital / len(fit_live) for s in fit_live}
            fr = backtester.run_basket_parallel(
                _sim_engine(fit_live), fit_bars, fit_macro, capital=capital,
                budget_by_symbol=fit_bud, compound=compound,
                slippage_bps=slippage,
                progress=_mk_progress(_scr_lock, _get, 'fit: first half',
                                      n_phases - 2, n_phases))
            w_raw = {}
            for s, m in fr.get('per_symbol', {}).items():
                sh, n = m.get('sharpe'), m.get('n_trades', 0)
                w_raw[s] = max(sh, 0.0) if (sh is not None and n > 0) else 0.0

            # eval slice: per-symbol warmup from ITS engine's stream, so a
            # mixed pipeline book gives every engine a full window at mid.
            ev_bars = {}
            for s in live:
                tf_s, nb_s = _pdepth(s)
                lo = warmup_start(mid, tf_s, nb_s)
                ks = [b for b in bars[s]
                      if datetime.fromisoformat(b['time']).date()
                      >= lo.date()]
                if ks:
                    ev_bars[s] = ks
            ev_macro = {}
            if macro:
                tf_m = registry.get(params['engine']).timeframes['macro']
                n_m = registry.get(params['engine']).history.get('macro', 200)
                ev_macro = _bars_between(macro,
                                         lo=warmup_start(mid, tf_m, n_m))
            ev_live = [s for s in live if ev_bars.get(s)]

            ev_even_bud = {s: capital / len(ev_live) for s in ev_live}
            er_even = backtester.run_basket_parallel(
                _sim_engine(ev_live), ev_bars, ev_macro, capital=capital,
                budget_by_symbol=ev_even_bud, compound=compound,
                slippage_bps=slippage,
                progress=_mk_progress(_scr_lock, _get, 'eval: even split',
                                      n_phases - 1, n_phases))

            wt = sum(w_raw.get(s, 0.0) for s in ev_live)
            weights_table = []
            mets_sharpe = None
            if wt > 0:
                sh_syms = [s for s in ev_live if w_raw.get(s, 0.0) > 0]
                sh_bud = {s: capital * w_raw[s] / wt for s in sh_syms}
                er_sh = backtester.run_basket_parallel(
                    _sim_engine(sh_syms), {s: ev_bars[s] for s in sh_syms},
                    {s: ev_macro[s] for s in sh_syms if ev_macro.get(s)},
                    capital=capital, budget_by_symbol=sh_bud,
                    compound=compound, slippage_bps=slippage,
                    progress=_mk_progress(_scr_lock, _get,
                                          'eval: sharpe-weighted',
                                          n_phases, n_phases))
                mets_sharpe = _port_metrics(er_sh)
            fit_ps = fr.get('per_symbol', {})
            for s in ev_live:
                fm = fit_ps.get(s, {})
                weights_table.append({
                    'ticker': s,
                    'fit_sharpe': fm.get('sharpe'),
                    'fit_trades': fm.get('n_trades', 0),
                    'weight_pct': round(100 * w_raw.get(s, 0.0) / wt, 2)
                                  if wt > 0 else 0.0,
                    'budget': round(capital * w_raw.get(s, 0.0) / wt, 2)
                              if wt > 0 else 0.0,
                })
            weights_table.sort(key=lambda r: -r['weight_pct'])

            sharpe_eval = {
                'mid': mid.strftime('%Y-%m-%d'),
                'fit_window': [params['start'], mid.strftime('%Y-%m-%d')],
                'eval_window': [mid.strftime('%Y-%m-%d'),
                                params.get('end') or 'now'],
                'weights': weights_table,
                'n_weighted': sum(1 for r in weights_table
                                  if r['weight_pct'] > 0),
                'even': _port_metrics(er_even),
                'sharpe': mets_sharpe,
                'error': (None if wt > 0 else
                          'no positive-Sharpe names in the fit window; '
                          'Sharpe portfolio skipped'),
            }

        missing = [s for s in pool if not bars.get(s)]
        errors = result['metrics'].get('errors', {})

        if _stop_requested(_get, _scr_lock):
            with _scr_lock:
                _scr_job = {'state': 'stopped', 'params': params,
                            'finished': datetime.now(ET).isoformat()}
            cm.log_forensic('conn_event', event='screen', status='stopped',
                            detail=params['label'])
            return

        stamp = f'{datetime.now():%Y%m%d_%H%M%S}'
        eng_tag = engine_display
        csv_path = os.path.join(
            cm.DATA_DIR, f"screen_{params['label']}_{eng_tag}_{stamp}.csv")
        if table:
            import csv as _csv
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                w = _csv.DictWriter(f, fieldnames=list(table[0].keys()))
                w.writeheader()
                w.writerows(table)
            if sharpe_eval and sharpe_eval['weights']:
                wpath = os.path.join(
                    cm.DATA_DIR, f"screen_{params['label']}_{eng_tag}_"
                                 f"weights_{stamp}.csv")
                with open(wpath, 'w', newline='', encoding='utf-8') as f:
                    w = _csv.DictWriter(
                        f, fieldnames=list(sharpe_eval['weights'][0].keys()))
                    w.writeheader()
                    w.writerows(sharpe_eval['weights'])
                sharpe_eval['csv'] = wpath
        if gate and gate['assignments']:
            import csv as _csv
            gpath = os.path.join(
                cm.DATA_DIR, f"screen_{params['label']}_gate_{stamp}.csv")
            with open(gpath, 'w', newline='', encoding='utf-8') as f:
                w = _csv.DictWriter(
                    f, fieldnames=list(gate['assignments'][0].keys()))
                w.writeheader()
                w.writerows(gate['assignments'])
            gate['csv'] = gpath

        with _scr_lock:
            _scr_job = {'state': 'done', 'params': params, 'table': table,
                        'n_screened': len(table), 'csv': csv_path,
                        'portfolio': portfolio, 'sizing': sizing,
                        'sharpe_eval': sharpe_eval, 'gate': gate,
                        'engine_display': engine_display,
                        'low_sample_floor': LOW_SAMPLE_FLOOR,
                        'skipped_no_data': missing, 'errors': errors,
                        'finished': datetime.now(ET).isoformat()}
        cm.log_forensic('conn_event', event='screen', status='done',
                        detail=f"{params['label']} {eng_tag}: {len(table)} "
                               f"names, {len(missing)} no-data, sizing "
                               f"{mode}" + (', gated' if ou_gate else '')
                               + (', compounded' if compound else '')
                               + (', sharpe eval' if sharpe_w else ''))
    except Exception as e:
        with _scr_lock:
            _scr_job = {'state': 'error', 'params': params, 'error': str(e)}
        cm.log_forensic('conn_event', event='screen', status='error',
                        error=str(e))


@app.route('/api/screen', methods=['GET', 'POST'])
def screen_route():
    """GET: current screen job + result table. POST: start a screen.
    Body: {basket | symbols[] | sector (+subsector?), engine?, start, end?,
    capital?, alloc_mode? ('pct'|'dollars'|'even'), alloc_value?,
    ou_gate? (bool: half-life gate + per-name engine assignment; engine is
    ignored), compound? (bool: per-sleeve compounding), sharpe_weight?,
    slippage_bps?}. Legacy alloc_pct still accepted as mode 'pct'.
    No source = the whole universe. One job at a time."""
    global _scr_job
    if request.method == 'GET':
        with _scr_lock:
            return jsonify(_with_elapsed(_scr_job))

    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400
    with _scr_lock:
        if _job_busy(_scr_job):
            return jsonify({'error': 'screen already running',
                            'params': _scr_job.get('params')}), 409

    body = request.json or {}
    if body.get('basket'):
        try:
            b = bk.get_basket(body['basket'])
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        symbols, label = b['symbols'], body['basket']
        sector = subsector = None
    elif body.get('symbols'):
        symbols = [str(s).strip().upper() for s in body.get('symbols', [])
                   if str(s).strip()]
        label = 'adhoc'
        sector = subsector = None
    else:
        sector = body.get('sector') or None
        subsector = body.get('subsector') or None
        try:
            symbols = sec.tickers_for(sector, subsector)
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        label = subsector or sector or 'universe'
    if not symbols:
        return jsonify({'error': 'no symbols'}), 400
    if not body.get('start'):
        return jsonify({'error': 'start (YYYY-MM-DD) required'}), 400

    ou_gate = bool(body.get('ou_gate', False))
    if ou_gate:
        eng_name = None
        n_gate = len(symbols) * len(ASSIGN_TFS_DEFAULT)
        if n_gate > HL_MAX_FETCHES:
            return jsonify({'error': f'OU gate: {len(symbols)} symbols x '
                                     f'{len(ASSIGN_TFS_DEFAULT)} timeframes '
                                     f'= {n_gate} fetches, over the '
                                     f'{HL_MAX_FETCHES} cap. Screen a '
                                     f'subsector or a basket.'}), 400
    else:
        eng_name = body.get('engine') or resolver.default_name
        if eng_name not in registry.names():
            return jsonify({'error': f'unknown engine: {eng_name}'}), 400

    mode = str(body.get('alloc_mode', 'pct')).lower()
    if mode not in ('pct', 'dollars', 'even'):
        return jsonify({'error': f'alloc_mode {mode!r}: use pct, dollars, '
                                 f'or even'}), 400
    capital = float(body.get('capital', 100000.0))
    if capital <= 0:
        return jsonify({'error': 'capital must be > 0'}), 400
    val = 0.0
    if mode != 'even':
        val = float(body.get('alloc_value', body.get('alloc_pct', 1.0)))
        if val <= 0:
            return jsonify({'error': 'alloc_value must be > 0'}), 400

    sharpe_weight = bool(body.get('sharpe_weight', False))
    if sharpe_weight:
        s_dt = datetime.strptime(body['start'], '%Y-%m-%d')
        e_dt = (datetime.strptime(body['end'], '%Y-%m-%d')
                if body.get('end') else datetime.now())
        if (e_dt - s_dt).days < 14:
            return jsonify({'error': 'sharpe_weight needs a window of at '
                                     'least 14 days to split into fit and '
                                     'eval halves'}), 400

    label = ''.join(c if c.isalnum() else '_' for c in label)
    params = {'symbols': symbols, 'engine': eng_name, 'label': label,
              'sector': sector, 'subsector': subsector,
              'start': body['start'], 'end': body.get('end'),
              'capital': capital,
              'alloc_mode': mode, 'alloc_value': val,
              'ou_gate': ou_gate,
              'compound': bool(body.get('compound', False)),
              'sharpe_weight': sharpe_weight,
              'slippage_bps': float(body.get('slippage_bps', 2.0))}
    with _scr_lock:
        _scr_job = {'state': 'running', 'params': params,
                    'n_symbols': len(symbols),
                    'started': datetime.now(ET).isoformat()}
    threading.Thread(target=_run_screen_job, args=(params,),
                     daemon=True).start()
    return jsonify({'started': True, 'n_symbols': len(symbols),
                    'params': params})


@app.route('/api/screen/control', methods=['POST'])
def screen_control():
    """{action: pause|resume|stop}. stop discards all work, gate included."""
    global _scr_job
    action = (request.json or {}).get('action')
    with _scr_lock:
        nxt, err = _apply_control(_scr_job, action)
        if err:
            return jsonify({'error': err}), 409
        _scr_job['state'] = nxt
    return jsonify({'ok': True, 'state': nxt})


# ─── MULTI-TIMEFRAME FETCH (shared by the OU gate and engine assignment) ─────
# One symbol x one timeframe = one fetch, sized from hlf.bars_needed() because
# the theta fit only reads its last max(window) bars. The dedicated half-life
# estimator card/routes were folded into the screener's OU gate; the module's
# estimation machinery is untouched and this fetch helper is its front door.

# Guardrail against a 500-name sweep across 12 timeframes quietly turning
# into 6,000 API calls.
HL_MAX_FETCHES = 400


def _hl_fetch(alpaca_, symbols, tf, start, end, progress=None):
    """Bars for one timeframe, sized to what the fit actually consumes.

    The estimator's largest window for `tf` is all the history it reads, so
    we request that many bars (plus headroom) and no more. `start` still
    clamps the window: a user asking for 2020 onward on 1Day gets 2020
    onward; asking for 2020 on 1Min gets the most recent slice that the
    1-min fit can use, because six years of 1-min bars is ~500k rows the
    fit would throw away."""
    need = hlf.bars_needed(tf)
    tf_min = hlf.TF_MINUTES[tf]
    anchor = end or datetime.now()
    days = max(2, int(need * tf_min / 390 * 3) + 2)
    eff_start = max(start, anchor - timedelta(days=days))
    got = alpaca_.get_bars_multi(symbols, timeframe=tf, limit=need,
                                 start=eff_start, progress=progress)
    if end is None:
        return got
    out = {}
    for sym, bars_ in got.items():
        out[sym] = [b for b in bars_
                    if datetime.fromisoformat(b['time']).date() <= end.date()]
    return out


# ─── ENGINE ASSIGNMENT (theta line -> OU family, deterministic) ───────────────
# Fetches multi-timeframe bars, fits the pooled theta line per name, snaps
# each 'ou' name onto the OU family by arithmetic (assign.py), and -- only on
# apply=true -- writes the result into the CONFIG WATCHLIST, the one store
# _sync_engine_overrides preserves across sweeps. Dry-run by default: the
# table and the would-be diff come back, nothing is written. Names not on
# the watchlist are reported, never auto-deployed; deploying stays a human
# decision (basket deploy).

_as_lock = threading.Lock()
_as_job = {'state': 'idle'}

# theta_line needs >=2 usable timeframes and rewards a spread of bar sizes.
# 1Min/2Min are its bid-ask-bounce control group and excluded from the fit
# by default, so they are not worth fetching here.
ASSIGN_TFS_DEFAULT = ('10Min', '30Min', '1Hour', '2Hour', '1Day')


def _run_assign_job(params):
    """Thread body: per-timeframe fetch -> theta line -> assignment table ->
    dry-run diff or config write-through. Never raises out."""
    global _as_job
    try:
        symbols = params['symbols']
        tfs = params['timeframes']
        start = datetime.strptime(params['start'], '%Y-%m-%d')
        end = (datetime.strptime(params['end'], '%Y-%m-%d')
               if params.get('end') else None)

        by_symbol = {s: {} for s in symbols}
        n_phases = len(tfs) + 1
        _get = lambda: _as_job
        for i, tf in enumerate(tfs, 1):
            p_tf = _mk_progress(_as_lock, _get, f'fetching {tf}', i, n_phases)
            p_tf.seed(len(symbols))
            got = _hl_fetch(alpaca, symbols, tf, start, end, progress=p_tf)
            for s in symbols:
                if got.get(s):
                    by_symbol[s][tf] = got[s]

        p_as = _mk_progress(_as_lock, _get, 'fitting theta lines',
                            n_phases, n_phases)
        p_as.seed(len(symbols))
        assignments = asg.assign_from_bars(by_symbol, progress=p_as)

        if _stop_requested(lambda: _as_job, _as_lock):
            with _as_lock:
                _as_job = {'state': 'stopped', 'params': params,
                           'finished': datetime.now(ET).isoformat()}
            cm.log_forensic('conn_event', event='assign', status='stopped',
                            detail=params['label'])
            return

        apply_now = bool(params.get('apply'))
        cfg = cm.load_config()
        diff = asg.apply_assignments(cfg, assignments, dry_run=not apply_now)
        if apply_now and diff['changed']:
            cm.save_config(cfg)
            _sync_engine_overrides(cfg)
            for c in diff['changed']:
                cm.log_forensic(
                    'conn_event', event='assign_apply', status='ok',
                    ticker=c['ticker'],
                    detail=f"{c.get('from')} -> {c.get('to')}"
                           + (' (paused)' if c.get('mode') else ''))

        csv_path = ''
        if assignments:
            import csv as _csv
            csv_path = os.path.join(
                cm.DATA_DIR,
                f"assign_{params['label']}_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv")
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                w = _csv.DictWriter(f, fieldnames=list(assignments[0].keys()))
                w.writeheader()
                w.writerows(assignments)

        n_assigned = sum(1 for a in assignments if a['engine'])
        with _as_lock:
            _as_job = {'state': 'done', 'params': params,
                       'assignments': assignments,
                       'n_assigned': n_assigned,
                       'n_names': len(assignments),
                       'applied': apply_now,
                       'changed': diff['changed'],
                       'skipped': diff['skipped'],
                       'csv': csv_path,
                       'finished': datetime.now(ET).isoformat()}
        cm.log_forensic('conn_event', event='assign', status='done',
                        detail=f"{params['label']}: {n_assigned}/"
                               f"{len(assignments)} assigned, "
                               f"{len(diff['changed'])} change(s), "
                               f"{'APPLIED' if apply_now else 'dry-run'}")
    except Exception as e:
        with _as_lock:
            _as_job = {'state': 'error', 'params': params, 'error': str(e)}
        cm.log_forensic('conn_event', event='assign', status='error',
                        error=str(e))


@app.route('/api/assign/bands')
def assign_bands():
    """The OU family's resolvable half-life bands in sessions (960-min
    extended-hours model) plus the snap target, so the UI never hardcodes
    what assign.py knows."""
    return jsonify({'bands': asg.bands(),
                    'target_hl_bars': round(asg.TARGET_HL_BARS, 3),
                    'default_timeframes': list(ASSIGN_TFS_DEFAULT)})


@app.route('/api/assign', methods=['GET', 'POST'])
def assign_route():
    """GET: current assignment job (table + diff). POST: start one.
    Body: {basket | symbols[] | sector (+subsector?), start, end?,
    timeframes?, apply?: bool}. apply=false (the default) is a dry run.
    One job at a time; 409 if busy."""
    global _as_job
    if request.method == 'GET':
        with _as_lock:
            return jsonify(_with_elapsed(_as_job))

    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400
    with _as_lock:
        if _job_busy(_as_job):
            return jsonify({'error': 'assignment already running',
                            'params': _as_job.get('params')}), 409

    body = request.json or {}
    if body.get('basket'):
        try:
            b = bk.get_basket(body['basket'])
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        symbols, label = b['symbols'], body['basket']
    elif body.get('sector'):
        try:
            symbols = sec.tickers_for(body['sector'],
                                      body.get('subsector') or None)
        except KeyError as e:
            return jsonify({'error': str(e)}), 404
        label = body.get('subsector') or body['sector']
    else:
        symbols = [str(s).strip().upper() for s in body.get('symbols', [])
                   if str(s).strip()]
        label = 'adhoc'
    if not symbols:
        return jsonify({'error': 'no symbols'}), 400
    if not body.get('start'):
        return jsonify({'error': 'start (YYYY-MM-DD) required'}), 400

    tfs = body.get('timeframes') or list(ASSIGN_TFS_DEFAULT)
    unknown = [t for t in tfs if t not in hlf.TF_MINUTES]
    if unknown:
        return jsonify({'error': f'unknown timeframe(s): {unknown}; '
                                 f'have {list(hlf.TF_MINUTES)}'}), 400
    tfs = [t for t in hlf.TIMEFRAMES if t in set(tfs)]
    if len(tfs) < 2:
        return jsonify({'error': 'theta line needs at least 2 '
                                 'timeframes'}), 400

    n_fetches = len(symbols) * len(tfs)
    if n_fetches > HL_MAX_FETCHES:
        return jsonify({'error': f'{len(symbols)} symbols x {len(tfs)} '
                                 f'timeframes = {n_fetches} fetches, over the '
                                 f'{HL_MAX_FETCHES} cap. Narrow the basket or '
                                 f'the timeframe list.'}), 400

    label = ''.join(c if c.isalnum() else '_' for c in label)
    params = {'symbols': symbols, 'label': label, 'timeframes': tfs,
              'start': body['start'], 'end': body.get('end'),
              'apply': bool(body.get('apply', False))}
    with _as_lock:
        _as_job = {'state': 'running', 'params': params,
                   'n_symbols': len(symbols),
                   'started': datetime.now(ET).isoformat()}
    threading.Thread(target=_run_assign_job, args=(params,),
                     daemon=True).start()
    return jsonify({'started': True, 'n_symbols': len(symbols),
                    'params': params})


@app.route('/api/assign/control', methods=['POST'])
def assign_control():
    """{action: pause|resume|stop}. stop discards before any config write;
    an already-applied write cannot be rolled back here."""
    global _as_job
    action = (request.json or {}).get('action')
    with _as_lock:
        nxt, err = _apply_control(_as_job, action)
        if err:
            return jsonify({'error': err}), 409
        _as_job['state'] = nxt
    return jsonify({'ok': True, 'state': nxt})


@app.route('/api/watchlist', methods=['GET', 'POST'])
def watchlist():
    cfg = cm.load_config()
    if request.method == 'POST':
        body = request.json or {}
        action = body.get('action')
        ticker = (body.get('ticker') or '').upper()
        if action == 'set_engine' and ticker:
            # '' or absent = clear the override; ticker follows the default.
            eng = (body.get('engine') or '').strip()
            if eng and eng not in registry.names():
                return jsonify({'error': f'unknown engine: {eng}'}), 400
            for w in cfg['watchlist']:
                if w['ticker'] == ticker:
                    if eng:
                        w['engine'] = eng
                    else:
                        w.pop('engine', None)
            cm.save_config(cfg)
            _sync_engine_overrides(cfg)
            cm.log_forensic('conn_event', event='set_engine', ticker=ticker,
                            status=eng or 'default')
            return jsonify({'ticker': ticker, 'engine': eng or None,
                            'resolved': resolver.resolve(ticker).name})
        if action == 'add' and ticker:
            if not any(w['ticker'] == ticker for w in cfg['watchlist']):
                cfg['watchlist'].append({
                    'ticker': ticker,
                    'allocation_pct': float(body.get('allocation_pct', 5.0)),
                    'mode': 'pause',          # new tickers come in passive
                })
        elif action == 'remove' and ticker:
            cfg['watchlist'] = [w for w in cfg['watchlist']
                                if w['ticker'] != ticker]
        elif action == 'update' and ticker:
            for w in cfg['watchlist']:
                if w['ticker'] == ticker:
                    w['allocation_pct'] = float(body.get('allocation_pct',
                                                         w['allocation_pct']))
        elif action == 'toggle_mode' and ticker:
            for w in cfg['watchlist']:
                if w['ticker'] == ticker:
                    w['mode'] = 'pause' if w.get('mode', 'pause') == 'run' \
                                else 'run'
        elif action == 'set_mode_bulk':
            # Mode change on a SUBSET (the checked rows). Same effect as
            # toggle_mode but for many tickers at once, no per-ticker confirm.
            # Controls entries only; never touches open positions. Unknown
            # tickers in the list are ignored.
            want = 'run' if body.get('mode') == 'run' else 'pause'
            wanted = {t.upper() for t in body.get('tickers', [])}
            n = 0
            for w in cfg['watchlist']:
                if w['ticker'].upper() in wanted:
                    w['mode'] = want
                    n += 1
            cm.save_config(cfg)
            return jsonify({'mode': want, 'count': n,
                            'watchlist': cfg.get('watchlist', [])})
        elif action == 'remove_bulk':
            # Remove a SUBSET from the watchlist. Config-only: this does NOT
            # sell anything. The UI decides which tickers are safe to remove
            # (flat) versus which need a market sell first, and sells those via
            # /api/manual/sell BEFORE calling this. Unknown tickers ignored.
            drop = {t.upper() for t in body.get('tickers', [])}
            before = len(cfg['watchlist'])
            cfg['watchlist'] = [w for w in cfg['watchlist']
                                if w['ticker'].upper() not in drop]
            cm.save_config(cfg)
            return jsonify({'removed': before - len(cfg['watchlist']),
                            'watchlist': cfg.get('watchlist', [])})
        elif action == 'set_all_mode':
            # Master switch: flip EVERY watchlist ticker to 'run' or 'pause' at
            # once. Controls only whether the engine may trade each name; does
            # NOT touch open positions.
            want = body.get('mode', 'pause')
            want = 'run' if want == 'run' else 'pause'
            for w in cfg['watchlist']:
                w['mode'] = want
            cm.save_config(cfg)
            return jsonify({'mode': want, 'count': len(cfg['watchlist']),
                            'watchlist': cfg.get('watchlist', [])})
        elif action == 'bulk_add':
            # Paste-a-list import. Each new ticker enters at the given default
            # allocation (1% of equity by default -> ~$800 on an $80k account)
            # and in PAUSE mode (signals logged, no live trades) so a big basket
            # generates data without opening dozens of positions.
            raw = body.get('tickers', '')
            alloc = float(body.get('allocation_pct', 1.0))
            # Accept commas, whitespace, or newlines as separators.
            import re as _re
            syms = [s.upper() for s in _re.split(r'[,\s]+', raw) if s.strip()]
            existing = {w['ticker'] for w in cfg['watchlist']}
            added, skipped = [], []
            for s in syms:
                if s in existing:
                    skipped.append(s)
                    continue
                cfg['watchlist'].append({'ticker': s,
                                         'allocation_pct': alloc,
                                         'mode': 'pause'})
                existing.add(s)
                added.append(s)
            cm.save_config(cfg)
            return jsonify({'added': added, 'skipped': skipped,
                            'watchlist': cfg.get('watchlist', [])})
        cm.save_config(cfg)
    return jsonify(cfg.get('watchlist', []))


@app.route('/api/positions')
def positions():
    pos = _positions()
    if alpaca.is_connected():
        for t, p in pos.items():
            sym = p.get('option_symbol')
            if p.get('option_contracts', 0) > 0 and sym:
                try:
                    oq = alpaca.get_options_quote(sym)
                    if oq and oq.get('mid') is not None:
                        p['current_premium'] = round(float(oq['mid']), 4)
                except Exception:
                    pass
    return jsonify(pos)


@app.route('/api/manual/buy', methods=['POST'])
def manual_buy():
    body = request.json or {}
    ticker = (body.get('ticker') or '').upper()
    direction = body.get('direction', 'long')
    sleeve = body.get('sleeve', 'combo')      # combo | shares | options
    if not ticker:
        return jsonify({'ok': False, 'reason': 'no ticker'}), 400
    if not alpaca.is_connected():
        return jsonify({'ok': False, 'reason': 'not connected'}), 400
    cfg = cm.load_config()

    if sleeve == 'combo':
        r = _do_entry(ticker, direction, cfg, source='manual')
        return jsonify(r)

    # single-sleeve manual entries
    q = alpaca.get_quote(ticker)
    if not q:
        return jsonify({'ok': False, 'reason': 'no quote'}), 400
    budget = float(body.get('dollars') or _budget_for(ticker, cfg))
    if budget <= 0:
        return jsonify({'ok': False, 'reason': 'no budget'}), 400

    legs = []
    if sleeve == 'shares':
        if direction == 'short':
            return jsonify({'ok': False, 'reason': 'shares are long-only'}), 400
        qty = int(budget // q['mid'])
        if qty < 1:
            return jsonify({'ok': False, 'reason': 'budget below one share'}), 400
        legs = [{'kind': 'shares', 'side': 'buy', 'qty': qty,
                 'est_price': q['mid']}]
    elif sleeve == 'options':
        chain = alpaca.get_options_chain(ticker, q['mid'],
                                         strike_range_pct=tr.STRIKE_RANGE_PCT,
                                         dte_min=tr.DTE_MIN, dte_max=tr.DTE_MAX)
        want = 'call' if direction == 'long' else 'put'
        c = tr.pick_contract(chain, want, q['mid'])
        if not c:
            return jsonify({'ok': False, 'reason': 'no qualifying contract'}), 400
        split = tr.compute_fill_spill(budget, c['mid'])
        if split['contracts'] < 1:
            return jsonify({'ok': False,
                            'reason': 'budget below one contract'}), 400
        legs = [{'kind': 'option', 'side': 'buy', 'symbol': c['symbol'],
                 'contracts': split['contracts'], 'est_premium': c['mid'],
                 'strike': c['strike'], 'expiry': c['expiry'],
                 'type': c['type']}]

    result = tr.execute_plan(alpaca, ticker, legs, reason='manual single-sleeve')
    # update position state
    pos = _positions()
    p = pos.get(ticker, {'ticker': ticker, 'direction': direction,
                         'opened_at': datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')})
    for f in result['filled']:
        leg = f['leg']
        if leg['kind'] == 'option':
            p['option_symbol'] = leg['symbol']
            p['option_contracts'] = p.get('option_contracts', 0) + leg['contracts']
            p['option_entry_premium'] = leg['est_premium']
            p['option_type'] = leg['type']
        else:
            p['shares'] = p.get('shares', 0) + leg['qty']
            p['share_entry_price'] = leg['est_price']
    pos[ticker] = p
    _save_positions(pos)
    return jsonify({'ok': True, 'filled': len(result['filled']),
                    'failed': len(result['failed'])})


@app.route('/api/manual/sell', methods=['POST'])
def manual_sell():
    body = request.json or {}
    ticker = (body.get('ticker') or '').upper()
    sleeve = body.get('sleeve', 'both')
    if not ticker:
        return jsonify({'ok': False, 'reason': 'no ticker'}), 400
    r = _do_exit(ticker, sleeve, 'manual sell', is_stop=False)
    return jsonify(r)


@app.route('/api/logs/<kind>')
def logs(kind):
    ticker = request.args.get('ticker')
    limit = int(request.args.get('limit', 200))
    reader = {'trades': cm.read_trades, 'signals': cm.read_signals,
              'orders': cm.read_orders}.get(kind)
    if not reader:
        return jsonify({'error': 'unknown log kind'}), 404
    return jsonify(reader(ticker=ticker, limit=limit))


@app.route('/api/quote/<ticker>')
def quote(ticker):
    """Single live quote for the chart price ticker (polled ~1/s)."""
    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400
    q = alpaca.get_quote(ticker.upper())
    if not q:
        return jsonify({'error': 'no quote'}), 404
    return jsonify(q)


@app.route('/api/bars/<ticker>')
def bars(ticker):
    """Bars + EMAs for the chart. Server-side EMA computation using the
    SAME signal_engine.ema the trading decisions use, so the clouds drawn
    are exactly the clouds the engine evaluates on.

    Query params:
      tf     - timeframe (default '10Min')
      limit  - bar count (default 500, max 1000)

    Returns:
      {
        'ticker', 'tf',
        'bars':  [{t, o, h, l, c, v}, ...],
        'ema':   {'e5':[...], 'e12':[...], 'e34':[...], 'e50':[...]},
        'macro': {'time':[...], 'e34':[...], 'e50':[...]}  # 1H cloud
      }
    EMA arrays align 1:1 with bars (None until warmed). macro arrays are the
    1-hour cloud sampled at its own bar times; the client steps them onto the
    10-min axis."""
    ticker = ticker.upper()
    tf = request.args.get('tf', '10Min')
    try:
        limit = max(50, min(1000, int(request.args.get('limit', 500))))
    except (TypeError, ValueError):
        limit = 500

    if not alpaca.is_connected():
        return jsonify({'error': 'not connected'}), 400

    bars = alpaca.get_bars(ticker, tf, limit=limit)
    if not bars:
        return jsonify({'ticker': ticker, 'tf': tf, 'bars': [],
                        'ema': {}, 'macro': {}})

    df = se.bars_to_df(bars)
    closes = df['close']

    def series(period):
        s = se.ema(closes, period)
        # Mask the warmup region (first `period` points) to None so the cloud
        # doesn't draw a misleading line before it's meaningful.
        out = []
        for i, v in enumerate(s.tolist()):
            out.append(round(float(v), 4) if i >= period else None)
        return out

    P_FAST = getattr(se, 'EMA_FAST', 5)
    P_SLOW = getattr(se, 'EMA_SLOW', 12)
    P_C1 = getattr(se, 'EMA_CLOUD_FAST', getattr(se, 'EMA_CLOUD1', 34))
    P_C2 = getattr(se, 'EMA_CLOUD_SLOW', getattr(se, 'EMA_CLOUD2', 50))

    ema = {'e8': series(8), 'e9': series(9),
           'e5': series(P_FAST), 'e12': series(P_SLOW),
           'e34': series(P_C1), 'e50': series(P_C2)}

    out_bars = [{'t': b['time'], 'o': b['open'], 'h': b['high'],
                 'l': b['low'], 'c': b['close'], 'v': b['volume']}
                for b in bars]

    # Macro 1-hour cloud (Step 3 filter made visible).
    macro = {}
    try:
        hbars = alpaca.get_bars(ticker, '1Hour', limit=250)
        if hbars and len(hbars) > P_C2:
            hdf = se.bars_to_df(hbars)
            hc = hdf['close']
            he34 = se.ema(hc, P_C1).tolist()
            he50 = se.ema(hc, P_C2).tolist()
            macro = {
                'time': [b['time'] for b in hbars],
                'e34': [round(float(x), 4) for x in he34],
                'e50': [round(float(x), 4) for x in he50],
            }
    except Exception as e:
        cm.log_forensic('api_event', event='bars_macro', ticker=ticker,
                        status='error', error=str(e))

    return jsonify({'ticker': ticker, 'tf': tf, 'bars': out_bars,
                    'ema': ema, 'macro': macro,
                    'periods': {'fast': P_FAST, 'slow': P_SLOW,
                                'c1': P_C1, 'c2': P_C2}})


@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/dashboard')
def dashboard():
    """Merged view: every watchlist ticker + any held position, with live
    values. Tickers with no position show zeros."""
    cfg = cm.load_config()
    pos = _positions()
    equity = None
    broker = {}              # raw broker rows by exact symbol
    broker_opt_by_under = {} # option broker rows grouped by underlying ticker
    broker_shares = {}       # share broker rows by ticker
    if alpaca.is_connected():
        try:
            equity = alpaca.get_account()['equity']
            for b in alpaca.get_positions():
                sym = b['symbol']
                broker[sym] = b
                is_option = (str(b.get('asset_class', '')).lower().endswith('option')
                             or len(sym) > 15)
                if is_option:
                    broker_opt_by_under.setdefault(_occ_underlying(sym), []).append(b)
                else:
                    broker_shares[sym] = b
        except Exception:
            pass

    # allocation_pct per ticker, for Set $ (= allocation_pct x equity).
    alloc_by_ticker = {w['ticker'].upper(): float(w.get('allocation_pct', 0))
                       for w in cfg.get('watchlist', [])}
    watched = set(alloc_by_ticker.keys())

    rows = []
    tickers = watched | set(pos.keys())
    for t in sorted(tickers):
        p = pos.get(t, {})
        # share leg (broker symbol == ticker)
        sh = broker_shares.get(t, {})
        value = float(sh.get('market_value') or 0)
        pl = float(sh.get('unrealized_pl') or 0)
        cost = float(sh.get('cost_basis') or 0)
        shares_qty = int(float(sh.get('qty') or p.get('shares') or 0))

        # option leg: match ANY broker option whose underlying is this ticker,
        # so a broker position shows even if state's symbol is missing/stale.
        opt_contracts = 0
        opt_symbol = p.get('option_symbol', '')
        for bo in broker_opt_by_under.get(t, []):
            value += float(bo.get('market_value') or 0)
            pl += float(bo.get('unrealized_pl') or 0)
            cost += float(bo.get('cost_basis') or 0)
            opt_contracts += int(abs(float(bo.get('qty') or 0)))
            opt_symbol = opt_symbol or bo.get('symbol', '')
        # fall back to state's count only if the broker showed nothing
        if opt_contracts == 0:
            opt_contracts = int(p.get('option_contracts') or 0)

        # Set $: the engine's per-entry budget for this ticker = allocation_pct
        # x the day's STARTING cash (pinned), matching _budget_for exactly so the
        # displayed Set equals the actual budget and doesn't drift intraday.
        set_dollars = round(alloc_by_ticker.get(t, 0) / 100.0 * _session_cash(), 2)

        # P/L %: return on the whole position's cost basis (shares + options
        # combined). Updates correctly as sleeves close because both value
        # and cost come live from the broker's remaining legs.
        pl_pct = (pl / cost * 100) if cost else 0.0

        rows.append({
            'ticker': t,
            'mode': next((w.get('mode', 'pause')
                          for w in cfg.get('watchlist', [])
                          if w['ticker'].upper() == t), None),
            'engine': resolver.resolve(t).name,
            'engine_override': t in resolver.by_ticker,
            'basket': resolver.ticker_basket.get(t),
            'direction': p.get('direction', ''),
            'shares': shares_qty,
            'contracts': opt_contracts,
            'set_dollars': round(set_dollars, 2),
            'value': round(value, 2),
            'pl': round(pl, 2),
            'pl_pct': round(pl_pct, 2),
            'opened_at': p.get('opened_at', ''),
            'watched': t in watched,
        })
    return jsonify({'equity': equity, 'rows': rows})

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    cfg = cm.load_config()
    _seed_sectors()
    _sync_engine_overrides(cfg)
    if cfg.get('alpaca_key'):
        r = alpaca.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                           paper=cfg.get('paper_trading', True))
        print(f"[INIT] alpaca: {r['message']}")
        if r.get('status') == 'ok':
            n = reconcile_positions(source='startup')
            print(f'[INIT] reconcile: {n} repair(s)')

    threading.Thread(target=bar_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    # Its own thread on purpose: the screener is read-only convenience, and a
    # bug in it must never be able to stall the loop that manages positions.
    import screener_service
    screener_service.start(alpaca)

    flask_thread = threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=PORT, debug=False,
                               use_reloader=False),
        daemon=True)
    flask_thread.start()
    time.sleep(1)

    try:
        import webview
        webview.create_window('Foundations Trading',
                              f'http://127.0.0.1:{PORT}',
                              width=1480, height=920)
        webview.start()
    except ImportError:
        print(f'[INIT] pywebview not installed; running headless.')
        print(f'[INIT] open http://127.0.0.1:{PORT} in a browser. Ctrl+C to quit.')
        while True:
            time.sleep(60)


if __name__ == '__main__':
    main()
