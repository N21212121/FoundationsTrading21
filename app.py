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

    cm.log_trade(ticker=ticker, side=direction.upper(), sleeve='COMBO',
                 action='BUY', qty=len(result['filled']),
                 price=spot, status='submitted',
                 reason=f"{source} entry ({plan['reason']}); "
                        f"{len(result['failed'])} leg(s) failed")
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

        tr.execute_plan(alpaca, ticker, legs, reason=trigger)

        if is_stop:
            for leg in legs:
                tr.mark_stop_out(_no_rebuy, ticker,
                                 'options' if leg['kind'] == 'option' else 'shares')
            _save_no_rebuy()

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

        cm.log_trade(ticker=ticker, side=(p.get('direction') or '').upper(),
                     sleeve=sleeve.upper(), action='SELL', qty=len(legs),
                     price='', status='submitted', reason=trigger)
        return {'ok': True}
    finally:
        with _state_lock:
            _exiting.discard(ticker)


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
    print('[MON] monitor started (10s)')
    while True:
        try:
            time.sleep(10)
            if not alpaca.is_connected():
                continue
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
    return jsonify(_positions())


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

    rows = []
    tickers = {w['ticker'].upper() for w in cfg.get('watchlist', [])} | set(pos.keys())
    for t in sorted(tickers):
        p = pos.get(t, {})
        # share leg value from broker (symbol == ticker)
        sh = broker.get(t, {})
        value = float(sh.get('market_value') or 0)
        pl = float(sh.get('unrealized_pl') or 0)
        # option leg value from broker (symbol == OCC option symbol)
        osym = p.get('option_symbol')
        if osym and osym in broker:
            value += float(broker[osym].get('market_value') or 0)
            pl += float(broker[osym].get('unrealized_pl') or 0)
        rows.append({
            'ticker': t,
            'mode': next((w.get('mode', 'pause')
                          for w in cfg.get('watchlist', [])
                          if w['ticker'].upper() == t), None),
            'direction': p.get('direction', ''),
            'shares': p.get('shares', 0),
            'contracts': p.get('option_contracts', 0),
            'value': round(value, 2),
            'port_pct': round(value / equity * 100, 2) if equity else 0.0,
            'pl': round(pl, 2),
            'opened_at': p.get('opened_at', ''),
            'watched': t in {w['ticker'].upper() for w in cfg.get('watchlist', [])},
        })
    return jsonify({'equity': equity, 'rows': rows})

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    cfg = cm.load_config()
    if cfg.get('alpaca_key'):
        r = alpaca.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                           paper=cfg.get('paper_trading', True))
        print(f"[INIT] alpaca: {r['message']}")

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
