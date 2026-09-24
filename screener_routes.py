"""
screener_routes.py — Foundations Trading

Endpoints for the watchlist noun and the screener that consumes it.

  GET  /api/watchlist?date=            entries for a date (today by default)
  POST /api/watchlist/rows             bulk add from typed/pasted text
  POST /api/watchlist/entry            add one
  POST /api/watchlist/entry/update     edit one
  POST /api/watchlist/entry/delete     remove one
  POST /api/watchlist/clear            clear a date
  POST /api/watchlist/carry            copy a day's list onto another day

  GET  /api/screen                     latest board (cached by the service)
  POST /api/screen/run                 force a scan now
  GET  /api/screen/alerts              drain the alert queue
  GET  /api/screen/state               loop health + settings
  POST /api/screen/settings            slots, min_state, alert_on, enabled
  GET  /api/screen/levels/<ticker>     full level set for one ticker
"""

from datetime import date

from flask import Blueprint, jsonify, request

import levels as LV
import screener_service as SVC
import watchlist as WL


def make_bp(alpaca):
    bp = Blueprint('screener', __name__)

    def _fail(msg, code=400):
        return jsonify({'ok': False, 'error': msg}), code

    # ─── WATCHLIST ────────────────────────────────────────────────────────────

    @bp.route('/api/watchlist')
    def watchlist_get():
        d = request.args.get('date') or date.today().isoformat()
        return jsonify({'ok': True, 'date': d,
                        'rows': WL.read_all(d, active_only=False),
                        'stats': WL.stats()})

    @bp.route('/api/watchlist/rows', methods=['POST'])
    def watchlist_rows():
        b = request.json or {}
        text = b.get('text') or ''
        if not text.strip():
            return _fail('nothing to add')
        try:
            res = WL.import_rows(text, for_date=b.get('date'),
                                 dry_run=bool(b.get('dry_run', False)))
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        return jsonify(res)

    @bp.route('/api/watchlist/entry', methods=['POST'])
    def watchlist_add():
        b = request.json or {}
        try:
            e = WL.add(ticker=b.get('ticker'), support=b.get('support'),
                       resistance=b.get('resistance'), note=b.get('note', ''),
                       catalyst=bool(b.get('catalyst')),
                       mtf=bool(b.get('mtf')), for_date=b.get('date'))
        except (ValueError, TypeError) as ex:
            return _fail(str(ex))
        return jsonify({'ok': True, 'entry': e})

    @bp.route('/api/watchlist/entry/update', methods=['POST'])
    def watchlist_update():
        b = request.json or {}
        eid = b.pop('id', None)
        if not eid:
            return _fail('id required')
        try:
            e = WL.update(eid, **b)
        except (ValueError, TypeError) as ex:
            return _fail(str(ex))
        if e is None:
            return _fail('no such entry', 404)
        return jsonify({'ok': True, 'entry': e})

    @bp.route('/api/watchlist/entry/delete', methods=['POST'])
    def watchlist_delete():
        eid = (request.json or {}).get('id')
        if not eid:
            return _fail('id required')
        if WL.delete(eid) is None:
            return _fail('no such entry', 404)
        return jsonify({'ok': True})

    @bp.route('/api/watchlist/clear', methods=['POST'])
    def watchlist_clear():
        d = (request.json or {}).get('date') or date.today().isoformat()
        return jsonify({'ok': True, 'removed': WL.clear_date(d), 'date': d})

    @bp.route('/api/watchlist/carry', methods=['POST'])
    def watchlist_carry():
        b = request.json or {}
        src = b.get('from')
        if not src:
            # Default to the most recent day that has entries, so "carry
            # forward" works on a Monday without hunting for Friday's date.
            dates = sorted({e['for_date'] for e in WL.read_all(active_only=False)})
            today = date.today().isoformat()
            prior = [d for d in dates if d < today]
            if not prior:
                return _fail('no earlier watchlist to carry forward')
            src = prior[-1]
        made = WL.carry_forward(src, b.get('to'))
        return jsonify({'ok': True, 'from': src, 'copied': len(made)})

    # ─── PLAYS ────────────────────────────────────────────────────────────────

    @bp.route('/api/plays/vocabulary')
    def plays_vocab():
        """Everything the builder's dropdowns are generated from.

        Served rather than duplicated in JS, so adding a level reference or
        an operator in plays.py appears in the UI without a second edit.
        """
        import plays as PL
        return jsonify({'ok': True, **PL.vocabulary()})

    @bp.route('/api/plays')
    def plays_get():
        import plays as PL
        d = request.args.get('date') or date.today().isoformat()
        rows = PL.read_all(ticker=request.args.get('ticker'),
                           for_date=d, active_only=False)
        # Each play carries its own plain-English rendering so the card and
        # the builder never disagree about what a condition says.
        for p_ in rows:
            p_['branches'] = PL._as_branches(p_)
            p_['sentence'] = PL.describe_play(p_)
            for b in p_['branches']:
                b['trigger_text'] = PL.describe(b.get('trigger'))
                b['condition_text'] = [PL.describe(c)
                                       for c in b.get('conditions', [])]
        return jsonify({'ok': True, 'date': d, 'rows': rows,
                        'stats': PL.stats()})

    def _ensure_watched(ticker, for_date=None):
        """A play on an unwatched ticker is never evaluated, because the
        screener only scans the watchlist. Writing a play is a statement of
        intent to watch, so the entry is created if it is missing."""
        d = for_date or date.today().isoformat()
        t = str(ticker or '').strip().upper()
        if not t:
            return False
        if any(e['ticker'] == t for e in WL.read_all(d, active_only=False)):
            return False
        try:
            WL.add(ticker=t, for_date=d, source='play')
            return True
        except (ValueError, TypeError):
            return False

    @bp.route('/api/plays/presets')
    def plays_presets():
        import plays as PL
        out = []
        for pre in PL.presets():
            out.append({**pre, 'preview': [
                {'bias': t['bias'], 'label': t.get('label', ''),
                 'trigger_text': PL.describe(t['trigger']),
                 'condition_text': [PL.describe(c)
                                    for c in t.get('conditions', [])]}
                for t in pre['plays']],
                'branch_count': len(pre['plays'])})
        return jsonify({'ok': True, 'presets': out})

    @bp.route('/api/plays/preset/apply', methods=['POST'])
    def plays_preset_apply():
        import plays as PL
        b = request.json or {}
        ticker = b.get('ticker')
        if not ticker:
            return _fail('ticker required')
        try:
            made = PL.apply_preset(b.get('preset_id'), ticker,
                                   params=b.get('params') or {},
                                   for_date=b.get('date'),
                                   start_order=int(b.get('start_order', 1)))
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        watched = _ensure_watched(ticker, b.get('date'))
        return jsonify({'ok': True, 'created': len(made), 'plays': made,
                        'watchlisted': watched})

    @bp.route('/api/plays/preset/save', methods=['POST'])
    def plays_preset_save():
        import plays as PL
        b = request.json or {}
        try:
            pre = PL.save_preset(b.get('name'), ticker=b.get('ticker'),
                                 for_date=b.get('date') or date.today().isoformat(),
                                 play_ids=b.get('play_ids'),
                                 description=b.get('description', ''))
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        return jsonify({'ok': True, 'preset': pre})

    @bp.route('/api/plays/preset/delete', methods=['POST'])
    def plays_preset_delete():
        import plays as PL
        pid = (request.json or {}).get('id')
        if not pid:
            return _fail('id required')
        if not str(pid).startswith('custom_'):
            return _fail('built-in presets cannot be deleted')
        if PL.delete_preset(pid) is None:
            return _fail('no such preset', 404)
        return jsonify({'ok': True})

    @bp.route('/api/plays/entry', methods=['POST'])
    def plays_add():
        import plays as PL
        b = request.json or {}
        try:
            play = PL.add(
                ticker=b.get('ticker'), branches=b.get('branches'),
                bias=b.get('bias'), trigger=b.get('trigger'),
                conditions=b.get('conditions') or [],
                label=b.get('label', ''), note=b.get('note', ''),
                order=b.get('order', 1), for_date=b.get('date'))
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        watched = _ensure_watched(play['ticker'], b.get('date'))
        return jsonify({'ok': True, 'play': play, 'watchlisted': watched})

    @bp.route('/api/plays/entry/update', methods=['POST'])
    def plays_update():
        import plays as PL
        b = request.json or {}
        pid = b.pop('id', None)
        if not pid:
            return _fail('id required')
        try:
            play = PL.update(pid, **b)
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        if play is None:
            return _fail('no such play', 404)
        return jsonify({'ok': True, 'play': play})

    @bp.route('/api/plays/entry/delete', methods=['POST'])
    def plays_delete():
        import plays as PL
        pid = (request.json or {}).get('id')
        if not pid:
            return _fail('id required')
        if PL.delete(pid) is None:
            return _fail('no such play', 404)
        return jsonify({'ok': True})

    @bp.route('/api/plays/carry', methods=['POST'])
    def plays_carry():
        import plays as PL
        b = request.json or {}
        src = b.get('from')
        if not src:
            dates = sorted({p_['for_date'] for p_
                            in PL.read_all(active_only=False)})
            today_s = date.today().isoformat()
            prior = [d for d in dates if d < today_s]
            if not prior:
                return _fail('no earlier plays to carry forward')
            src = prior[-1]
        made = PL.carry_forward(src, b.get('to'))
        return jsonify({'ok': True, 'from': src, 'copied': len(made)})

    @bp.route('/api/plays/condition/preview', methods=['POST'])
    def plays_condition_preview():
        """Validate one condition and render it, for live feedback as the
        builder's dropdowns change. Catches a bad combination before it is
        ever attached to a play."""
        import plays as PL
        b = request.json or {}
        try:
            c = PL.make_condition(b.get('kind'), b.get('ref'),
                                  b.get('op'), b.get('value'))
        except (ValueError, TypeError) as e:
            return _fail(str(e))
        return jsonify({'ok': True, 'condition': c, 'text': PL.describe(c)})

    @bp.route('/api/plays/grade/<ticker>')
    def plays_grade(ticker):
        """Grade a ticker's plays against right now, with each condition's
        result. The detail behind the board card."""
        import plays as PL
        import screener as SC
        if not alpaca.is_connected():
            return _fail('Not connected to Alpaca.', 409)
        t = ticker.strip().upper()
        plays = PL.read_all(ticker=t, for_date=date.today().isoformat())
        if not plays:
            return _fail(f'no plays written for {t} today', 404)

        now_et = SVC.datetime.now(SVC.ET)
        daily = SVC.drop_forming(
            alpaca.get_bars(t, '1Day', limit=SVC.DAILY_BARS), '1Day', now_et)
        intra = SVC.drop_forming(
            alpaca.get_bars(t, SVC.INTRA_TF, limit=SVC.INTRA_BARS),
            SVC.INTRA_TF, now_et)
        hourly = SVC.drop_forming(
            alpaca.get_bars(t, '1Hour', limit=SVC.HOURLY_BARS), '1Hour', now_et)

        entry = next((e for e in WL.today() if e['ticker'] == t), {'ticker': t})
        row = SC.score_ticker(entry, daily, intra, hourly_bars=hourly,
                              plays=plays)
        return jsonify({'ok': True, 'ticker': t, 'price': row['price'],
                        'atr': row['atr'], 'plays': row['plays'],
                        'best': row['best_play']})

    # ─── SCREEN ───────────────────────────────────────────────────────────────

    @bp.route('/api/screen')
    def screen_get():
        st = SVC.state()
        return jsonify({'ok': True, 'result': st.get('result'),
                        'last_scan': st.get('last_scan'),
                        'last_error': st.get('last_error'),
                        'settings': st.get('settings'),
                        'connected': alpaca.is_connected()})

    @bp.route('/api/screen/run', methods=['POST'])
    def screen_run():
        try:
            res = SVC.scan_once(alpaca, raise_errors=True)
        except Exception as e:
            return _fail(f'{type(e).__name__}: {e}',
                         409 if 'connect' in str(e).lower() else 500)
        SVC._raise_alerts(res)
        with SVC._lock:
            SVC._state['result'] = res
            SVC._state['last_scan'] = res['at']
        return jsonify({'ok': True, 'result': res})

    @bp.route('/api/screen/alerts')
    def screen_alerts():
        return jsonify({'ok': True, 'alerts': SVC.drain_alerts()})

    @bp.route('/api/screen/state')
    def screen_state():
        return jsonify({'ok': True, **SVC.state(),
                        'connected': alpaca.is_connected()})

    @bp.route('/api/screen/settings', methods=['POST'])
    def screen_settings():
        b = request.json or {}
        slots = b.get('slots')
        if slots is not None:
            try:
                slots = max(1, min(20, int(slots)))
            except (TypeError, ValueError):
                return _fail('slots must be a number')
        return jsonify({'ok': True, 'settings': SVC.configure(
            slots=slots, min_state=b.get('min_state'),
            alert_on=b.get('alert_on'), enabled=b.get('enabled'))})

    @bp.route('/api/screen/weights', methods=['GET', 'POST'])
    def screen_weights():
        """Read or set the scoring weights.

        These are judgement, not calibration. They are exposed because they
        should be argued with, not because they are known to be right.
        """
        import screener as SC
        if request.method == 'GET':
            return jsonify({'ok': True,
                            'weights': SVC.settings()['weights'],
                            'defaults': SC.DEFAULT_WEIGHTS,
                            'components': list(SC.COMPONENTS)})
        b = request.json or {}
        if b.get('reset'):
            return jsonify({'ok': True, 'applied': 'defaults',
                            'weights': SVC.set_weights(SC.DEFAULT_WEIGHTS)})

        raw = b.get('weights') or b
        # The rule lives here, not only in the browser: whole numbers that
        # total exactly 100 are kept, anything else falls back to defaults.
        # Enforcing it server-side means a stray API call cannot leave the
        # board scored by a set of weights nobody chose.
        vals = {}
        for k in SC.COMPONENTS:
            v = raw.get(k, 0) or 0
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = None
            # int() would turn 5.5 into 5 and quietly accept a set of weights
            # the user never typed. A fractional value is rejected instead.
            if f is None or f != int(f):
                return jsonify({'ok': True, 'applied': 'defaults',
                                'reason': f'{k} must be a whole number',
                                'weights': SVC.set_weights(SC.DEFAULT_WEIGHTS)})
            vals[k] = int(f)

        if any(v < 0 for v in vals.values()):
            return jsonify({'ok': True, 'applied': 'defaults',
                            'reason': 'values cannot be negative',
                            'weights': SVC.set_weights(SC.DEFAULT_WEIGHTS)})

        total = sum(vals.values())
        if total != 100:
            return jsonify({'ok': True, 'applied': 'defaults',
                            'reason': f'values total {total}, not 100',
                            'total': total,
                            'weights': SVC.set_weights(SC.DEFAULT_WEIGHTS)})

        return jsonify({'ok': True, 'applied': 'custom', 'total': total,
                        'weights': SVC.set_weights(vals)})

    @bp.route('/api/screen/levels/<ticker>')
    def screen_levels(ticker):
        """Every level for one ticker, for the detail panel."""
        if not alpaca.is_connected():
            return _fail('Not connected to Alpaca.', 409)
        t = ticker.strip().upper()
        daily = alpaca.get_bars(t, '1Day', limit=SVC.DAILY_BARS)
        intra = alpaca.get_bars(t, SVC.INTRA_TF, limit=SVC.INTRA_BARS)
        if not daily:
            return _fail(f'no daily bars for {t}', 404)
        price = intra[-1]['close'] if intra else daily[-1]['close']
        entry = next((e for e in WL.today() if e['ticker'] == t), {})
        manual = {}
        if entry.get('support'):
            manual['support'] = entry['support']
        if entry.get('resistance'):
            manual['resistance'] = entry['resistance']
        # intraday bars carry the pre-market session that PMH/PML come from
        ls = LV.build(daily, price, manual=manual, intraday_bars=intra)
        return jsonify({'ok': True, 'ticker': t, 'price': ls['price'],
                        'atr': ls['atr'], 'levels': ls['levels'],
                        'confluence': LV.confluence(ls),
                        'note': entry.get('note', '')})

    @bp.route('/api/screen/chart/<ticker>')
    def screen_chart(ticker):
        """Everything the chart draws, computed server-side.

        EMAs come from signal_engine.ema — the same function the trading
        decisions use — so the clouds drawn are the clouds evaluated. The
        1H 34/50 is mapped onto the 6-min timeline as a step function: each
        intraday bar carries the last 1H value that had CLOSED by then, which
        is what an MTF cloud actually is. Interpolating it would draw a line
        the higher timeframe never printed.
        """
        import signal_engine as se

        if not alpaca.is_connected():
            return _fail('Not connected to Alpaca.', 409)
        t = ticker.strip().upper()
        tf = request.args.get('tf') or SVC.INTRA_TF
        try:
            limit = max(40, min(400, int(request.args.get('limit', 120))))
        except (TypeError, ValueError):
            limit = 120

        now_et = SVC.datetime.now(SVC.ET)
        intra = SVC.drop_forming(alpaca.get_bars(t, tf, limit=limit + 60), tf, now_et)
        hourly = SVC.drop_forming(alpaca.get_bars(t, '1Hour', limit=200), '1Hour', now_et)
        daily = SVC.drop_forming(alpaca.get_bars(t, '1Day', limit=SVC.DAILY_BARS),
                                 '1Day', now_et)
        if not intra:
            return _fail(f'no {tf} bars for {t}', 404)

        closes = [b['close'] for b in intra]
        import pandas as pd
        cs = pd.Series(closes)
        e5 = se.ema(cs, se.EMA_FAST).tolist()
        e12 = se.ema(cs, se.EMA_SLOW).tolist()
        e34 = se.ema(cs, se.EMA_REGIME_A).tolist()
        e50 = se.ema(cs, se.EMA_REGIME_B).tolist()

        # 1H 34/50 as a step function over the intraday timeline.
        mtf34 = mtf50 = None
        if len(hourly) > se.EMA_REGIME_B:
            hs = pd.Series([b['close'] for b in hourly])
            h34 = se.ema(hs, se.EMA_REGIME_A).tolist()
            h50 = se.ema(hs, se.EMA_REGIME_B).tolist()
            htimes = [b['time'] for b in hourly]
            mtf34, mtf50, j = [], [], 0
            for b in intra:
                while j + 1 < len(htimes) and htimes[j + 1] <= b['time']:
                    j += 1
                mtf34.append(h34[j] if htimes[j] <= b['time'] else None)
                mtf50.append(h50[j] if htimes[j] <= b['time'] else None)

        entry = next((e for e in WL.today() if e['ticker'] == t), {})
        manual = {}
        if entry.get('support'):
            manual['support'] = entry['support']
        if entry.get('resistance'):
            manual['resistance'] = entry['resistance']
        ls = (LV.build(daily, closes[-1], manual=manual, intraday_bars=intra)
              if daily else {'price': closes[-1], 'atr': None, 'levels': []})

        keep = intra[-limit:]
        off = len(intra) - len(keep)
        return jsonify({
            'ok': True, 'ticker': t, 'tf': tf,
            'bars': keep,
            'ema': {'e5': e5[off:], 'e12': e12[off:],
                    'e34': e34[off:], 'e50': e50[off:]},
            'mtf': ({'e34': mtf34[off:], 'e50': mtf50[off:]}
                    if mtf34 is not None else None),
            'levels': ls['levels'],
            'atr': ls['atr'],
            'confluence': LV.confluence(ls) if ls.get('levels') else [],
            'alerts': SVC.alert_history(t),
            'note': entry.get('note', ''),
            'support': entry.get('support', []),
            'resistance': entry.get('resistance', []),
        })

    return bp
