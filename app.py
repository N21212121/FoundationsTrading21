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
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

import config_manager as cm
import signal_engine as se
import trade_router as tr
from alpaca_manager import AlpacaManager

ET = ZoneInfo('America/New_York')
PORT = 5275

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
_macd_cache = {}                 # {ticker: {'spread_abs': float, 'bar_time': str}}
_last_bar_seen = {}              # {ticker: iso time of last evaluated bar}
_status_message = 'started'


def _positions():
    return cm.load_positions()


def _save_positions(p):
    cm.save_positions(p)


# ─── ENTRY / EXIT ACTIONS ──────────────────────────────────────────────────────

def _budget_for(ticker, cfg):
    """allocation_pct of current equity, in dollars."""
    for w in cfg.get('watchlist', []):
        if w['ticker'].upper() == ticker.upper():
            try:
                acct = alpaca.get_account()
                return acct['equity'] * float(w.get('allocation_pct', 0)) / 100.0
            except Exception:
                return 0.0
    return 0.0


def _do_entry(ticker, direction, cfg, source='engine'):
    """Plan and execute an entry. Returns a result dict for logging."""
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
    if direction == 'short':
        if tr.rebuy_blocked(_no_rebuy, ticker, 'options'):
            return {'ok': False, 'reason': 'same-day rebuy blocked (option stop-out)'}
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

    chain = alpaca.get_options_chain(ticker, spot,
                                     strike_range_pct=tr.STRIKE_RANGE_PCT,
                                     dte_min=tr.DTE_MIN, dte_max=tr.DTE_MAX)
    plan = tr.plan_entry(direction, budget, spot, chain)
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
    tr.reset_peaks_on_add(p)
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
        else:
            cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='SHARES',
                         action='BUY',
                         qty=int(fill.get('filled_qty') or leg.get('qty') or 0),
                         price=fill.get('filled_avg_price') or leg.get('est_price', ''),
                         status=fill.get('status', 'submitted'),
                         reason=f"{source} entry ({plan['reason']})")
    if result['failed']:
        cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='COMBO',
                     action='BUY', qty=0, price='', status='failed',
                     reason=f"{source} entry: {len(result['failed'])} leg(s) failed")
    return {'ok': True, 'filled': len(result['filled']),
            'failed': len(result['failed'])}


def _do_exit(ticker, sleeve, trigger, is_stop):
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
        # the stored entry prices. Both instruments are long-the-position
        # (buy-to-open, sell-to-close), so P/L = (exit - entry) * qty * mult.
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
            else:
                xqty = int(fill.get('filled_qty') or sh_qty)
                exitpx = float(xprice) if xprice not in (None, '') else None
                pnl_d = ((exitpx - sh_entry) * xqty
                         if exitpx is not None and sh_entry else None)
                pnl_p = ((exitpx - sh_entry) / sh_entry * 100
                         if exitpx is not None and sh_entry else None)
                cm.log_trade(ticker=ticker, side=direction, sleeve='SHARES',
                             action='SELL', qty=xqty,
                             price=round(exitpx, 4) if exitpx is not None else '',
                             status=fill.get('status', 'submitted'),
                             reason=trigger,
                             pnl_dollars=round(pnl_d, 2) if pnl_d is not None else '',
                             pnl_pct=round(pnl_p, 2) if pnl_p is not None else '')

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
            b_shares[sym] = {'qty': qty, 'avg_entry': b.get('avg_entry_price')}

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
                p.setdefault('peak_premium', 0.0)
                p.setdefault('peak_macd_spread', 0.0)
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
                p.setdefault('direction', 'long')
                p.setdefault('opened_at',
                             datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S'))
                p['shares'] = s['qty']
                if not p.get('share_entry_price'):
                    p['share_entry_price'] = float(s['avg_entry'] or 0)
                p.setdefault('peak_premium', 0.0)
                p.setdefault('peak_macd_spread', 0.0)
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


def prime_macd_cache():
    """Fill the bar-close cache for every held ticker so carried positions
    are never blind on the first bar after startup. Without this, the
    EMA12-confirmed stop falls through to its fire-blind branch for up to
    one full bar."""
    pos = _positions()
    primed = 0
    for ticker in list(pos.keys()):
        try:
            bars10 = alpaca.get_bars(ticker, '10Min', limit=60)
            if not bars10 or len(bars10) < 15:
                continue
            df = se.bars_to_df(bars10)
            e5 = float(se.ema(df['close'], se.EMA_FAST).iloc[-1])
            e12 = float(se.ema(df['close'], se.EMA_SLOW).iloc[-1])
            _macd_cache[ticker] = {'spread_abs': abs(e5 - e12),
                                   'bar_time': bars10[-1]['time'],
                                   'last_close': float(df['close'].iloc[-1]),
                                   'e12': e12}
            primed += 1
        except Exception as e:
            cm.log_forensic('api_event', event='prime_cache', ticker=ticker,
                            status='error', error=str(e))
    if primed:
        print(f'[INIT] cache primed for {primed} held ticker(s)')
    return primed


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
            pos = _positions()
            for ticker in watch:
                try:
                    _evaluate_ticker(ticker, cfg, pos)
                except Exception as e:
                    cm.log_forensic('signal_eval', ticker=ticker,
                                    status='error', error=str(e))
            _status_message = f'last bar sweep {datetime.now(ET):%H:%M:%S}'
        except Exception as e:
            print(f'[BAR] loop error: {e}')
            time.sleep(30)


def _evaluate_ticker(ticker, cfg, pos):
    bars10 = alpaca.get_bars(ticker, '10Min', limit=300)
    if not bars10:
        return
    # Skip if we already evaluated this bar (slow data / double boundary)
    newest = bars10[-1]['time']
    if _last_bar_seen.get(ticker) == newest:
        return
    _last_bar_seen[ticker] = newest

    bars1h = alpaca.get_bars(ticker, '1Hour', limit=200)
    q = alpaca.get_quote(ticker)
    if not q:
        return

    # Cache MACD spread for the monitor
    df = se.bars_to_df(bars10)
    e5 = float(se.ema(df['close'], se.EMA_FAST).iloc[-1])
    e12 = float(se.ema(df['close'], se.EMA_SLOW).iloc[-1])
    _macd_cache[ticker] = {'spread_abs': abs(e5 - e12), 'bar_time': newest,
                           'last_close': float(df['close'].iloc[-1]),
                           'e12': e12}

    open_dir = pos.get(ticker, {}).get('direction')
    d = se.evaluate(ticker, bars10, bars1h, current_open=q['mid'],
                    open_position_direction=open_dir)

    # Log every decision
    s2 = d.get('step2') or {}
    cm.log_signal(
        ticker=ticker, bar_time=newest,
        launch_gate_passed=(d.get('gate') or {}).get('passed', ''),
        launch_gate_reason=(d.get('gate') or {}).get('reason', ''),
        step2_votes_long=(s2.get('tally') or {}).get('L', ''),
        step2_votes_short=(s2.get('tally') or {}).get('S', ''),
        step2_votes_hold=(s2.get('tally') or {}).get('H', ''),
        step2_result=s2.get('result', ''),
        step3_macro_state=(d.get('step3') or {}).get('state', ''),
        step3_result='confirmed' if (d.get('step3') or {}).get('confirmed')
                     else ('rejected' if d.get('step3') else ''),
        final_action=d['action'],
        notes='engine_enabled' if _engine_enabled else 'OBSERVE ONLY',
    )

    if not _engine_enabled or d['action'] == 'NONE':
        return

    a = d['action']
    # Exits are never gated by per-ticker pause (design A: pause blocks
    # entries only; open positions keep full exit protection).
    if a.startswith('EXIT_LONG') or a.startswith('EXIT_SHORT'):
        _do_exit(ticker, 'both', f'signal flip ({a})', is_stop=False)

    mode = 'pause'
    for w in cfg.get('watchlist', []):
        if w['ticker'].upper() == ticker:
            mode = w.get('mode', 'pause')
            break
    if mode != 'run':
        return   # paused: evaluated and logged above, but no new entries

    if a.endswith('ENTER_LONG'):
        _do_entry(ticker, 'long', cfg)
    elif a.endswith('ENTER_SHORT'):
        _do_entry(ticker, 'short', cfg)


# ─── 10-SECOND MONITOR ─────────────────────────────────────────────────────────

def monitor_loop():
    print('[MON] monitor started (10s; reconcile every ~5 min)')
    tick = 0
    while True:
        try:
            time.sleep(10)
            if not alpaca.is_connected():
                continue
            tick += 1
            if tick % 30 == 0:          # ~every 5 minutes
                reconcile_positions(source='scheduled')
            pos = _positions()
            if not pos:
                continue
            changed = False
            for ticker, p in list(pos.items()):
                if ticker in _exiting:
                    continue            # exit already firing; don't re-evaluate
                try:
                    q = alpaca.get_quote(ticker)
                    share_price = q['mid'] if q else None
                    premium = None
                    if p.get('option_contracts', 0) > 0 and p.get('option_symbol'):
                        oq = alpaca.get_options_quote(p['option_symbol'])
                        premium = oq['mid'] if oq else None
                    cache = _macd_cache.get(ticker, {})

                    r = tr.check_position(p, share_price, premium,
                                          cache.get('spread_abs'),
                                          last_close=cache.get('last_close'),
                                          e12=cache.get('e12'))
                    if r['peak_premium'] != p.get('peak_premium') or \
                       r['peak_macd_spread'] != p.get('peak_macd_spread'):
                        p['peak_premium'] = r['peak_premium']
                        p['peak_macd_spread'] = r['peak_macd_spread']
                        changed = True

                    if r['exit'] and _engine_enabled:
                        ex = r['exit']
                        _do_exit(ticker, ex['sleeve'], ex['trigger'],
                                 ex['is_stop'])
                except Exception as e:
                    cm.log_forensic('api_event', event='monitor', ticker=ticker,
                                    status='error', error=str(e))
            if changed:
                _save_positions(pos)
        except Exception as e:
            print(f'[MON] loop error: {e}')
            time.sleep(10)


# ─── ROUTES ────────────────────────────────────────────────────────────────────

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


@app.route('/api/watchlist', methods=['GET', 'POST'])
def watchlist():
    cfg = cm.load_config()
    if request.method == 'POST':
        body = request.json or {}
        action = body.get('action')
        ticker = (body.get('ticker') or '').upper()
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
    tr.reset_peaks_on_add(p)
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
    broker = {}
    if alpaca.is_connected():
        try:
            equity = alpaca.get_account()['equity']
            for b in alpaca.get_positions():
                broker[b['symbol']] = b
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
        sh = broker.get(t, {})
        value = float(sh.get('market_value') or 0)
        pl = float(sh.get('unrealized_pl') or 0)
        cost = float(sh.get('cost_basis') or 0)
        # option leg (broker symbol == OCC option symbol)
        osym = p.get('option_symbol')
        if osym and osym in broker:
            bo = broker[osym]
            value += float(bo.get('market_value') or 0)
            pl += float(bo.get('unrealized_pl') or 0)
            cost += float(bo.get('cost_basis') or 0)

        # Set $: the engine's per-entry budget for this ticker, stable and
        # independent of current deployment = allocation_pct x equity.
        set_dollars = (alloc_by_ticker.get(t, 0) / 100.0 * equity) \
            if equity else 0.0

        # P/L %: return on the whole position's cost basis (shares + options
        # combined). Updates correctly as sleeves close because both value
        # and cost come live from the broker's remaining legs.
        pl_pct = (pl / cost * 100) if cost else 0.0

        rows.append({
            'ticker': t,
            'mode': next((w.get('mode', 'pause')
                          for w in cfg.get('watchlist', [])
                          if w['ticker'].upper() == t), None),
            'direction': p.get('direction', ''),
            'shares': p.get('shares', 0),
            'contracts': p.get('option_contracts', 0),
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
    if cfg.get('alpaca_key'):
        r = alpaca.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                           paper=cfg.get('paper_trading', True))
        print(f"[INIT] alpaca: {r['message']}")
        if r.get('status') == 'ok':
            n = reconcile_positions(source='startup')
            print(f'[INIT] reconcile: {n} repair(s)')
            prime_macd_cache()

    threading.Thread(target=bar_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

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
