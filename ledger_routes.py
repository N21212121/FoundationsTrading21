"""
ledger_routes.py — Foundations Trading

Endpoints for the accounting tab. A blueprint factory because the broker
sync needs the live AlpacaManager. Register in app.py with:

    import ledger_routes
    app.register_blueprint(ledger_routes.make_bp(alpaca))

  GET  /api/ledger/report?start=&end=   one period
  GET  /api/ledger/months?year=         one report per month
  GET  /api/ledger/days?start=&end=     realized P/L per day (Journal calendar)
  GET  /api/ledger/ytd                  year to date
  GET  /api/ledger/entries?start=&end=  the stored rows
  GET  /api/ledger/categories           categories for the entry form
  POST /api/ledger/entry                add an expense or other cash event
  POST /api/ledger/entry/update         edit one
  POST /api/ledger/entry/delete         remove one
  POST /api/ledger/sync                 pull broker fees/dividends/transfers
"""

from datetime import date

from flask import Blueprint, jsonify, request

import ledger as L
import pairing


def make_bp(alpaca):
    bp = Blueprint('ledger', __name__)

    def _fail(msg, code=400):
        return jsonify({'ok': False, 'error': msg}), code

    def _range():
        return request.args.get('start') or None, request.args.get('end') or None

    @bp.route('/api/ledger/report')
    def ledger_report():
        start, end = _range()
        return jsonify({'ok': True, 'report': L.report(start, end)})

    @bp.route('/api/ledger/months')
    def ledger_months():
        year = request.args.get('year') or None
        # One build_legs for every month rather than one per month.
        legs = pairing.build_legs()
        return jsonify({'ok': True, 'months': L.by_month(year, legs=legs),
                        'ytd': L.ytd(legs=legs)})

    @bp.route('/api/ledger/days')
    def ledger_days():
        start, end = _range()
        return jsonify({'ok': True, 'days': L.by_day(start, end)})

    @bp.route('/api/ledger/ytd')
    def ledger_ytd():
        return jsonify({'ok': True, 'report': L.ytd()})

    @bp.route('/api/ledger/entries')
    def ledger_entries():
        start, end = _range()
        return jsonify({'ok': True,
                        'rows': L.read_all(start=start, end=end,
                                           kind=request.args.get('kind') or None)})

    @bp.route('/api/ledger/categories')
    def ledger_categories():
        return jsonify({'ok': True, 'categories': L.categories(),
                        'kinds': list(L.KINDS)})

    @bp.route('/api/ledger/entry', methods=['POST'])
    def ledger_add():
        b = request.json or {}
        if b.get('amount') in (None, ''):
            return _fail('amount required')
        try:
            amount = float(b['amount'])
        except (TypeError, ValueError):
            return _fail('amount must be a number')
        kind = b.get('kind', 'expense')
        # An expense is money out. Typing 49 rather than -49 is the obvious
        # mistake, so the sign is enforced by kind instead of trusted.
        if kind == 'expense':
            amount = -abs(amount)
        elif kind == 'income':
            amount = abs(amount)
        try:
            e = L.add(date_=b.get('date') or date.today().isoformat(),
                      amount=amount, kind=kind,
                      category=b.get('category', ''),
                      description=b.get('description', ''))
        except (ValueError, TypeError) as ex:
            return _fail(str(ex))
        return jsonify({'ok': True, 'entry': e})

    @bp.route('/api/ledger/entry/update', methods=['POST'])
    def ledger_update():
        b = request.json or {}
        eid = b.pop('id', None)
        if not eid:
            return _fail('id required')
        if 'amount' in b and b.get('kind') == 'expense':
            b['amount'] = -abs(float(b['amount']))
        try:
            e = L.update(eid, **b)
        except (ValueError, TypeError) as ex:
            return _fail(str(ex))
        if e is None:
            return _fail('no such entry', 404)
        return jsonify({'ok': True, 'entry': e})

    @bp.route('/api/ledger/entry/delete', methods=['POST'])
    def ledger_delete():
        eid = (request.json or {}).get('id')
        if not eid:
            return _fail('id required')
        gone = L.delete(eid)
        if gone is None:
            return _fail('no such entry', 404)
        return jsonify({'ok': True, 'removed': gone})

    @bp.route('/api/ledger/sync', methods=['POST'])
    def ledger_sync():
        b = request.json or {}
        if not alpaca.is_connected():
            return _fail('Not connected to Alpaca. Connect first.', 409)
        try:
            res = L.sync_broker(alpaca, start=b.get('start'),
                                end=b.get('end'),
                                dry_run=bool(b.get('dry_run', True)))
        except Exception as e:
            return _fail(f'{type(e).__name__}: {e}', 500)
        return jsonify(res)

    return bp
