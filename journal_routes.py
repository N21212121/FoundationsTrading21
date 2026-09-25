"""
journal_routes.py — Foundations Trading

Flask blueprint for the journal noun and its consumers.

Kept as a blueprint rather than more routes in app.py for two reasons: app.py
is already 2,500 lines, and a failure in here must not be able to take down
the bar loop. Register with two lines in app.py:

    import journal_routes
    app.register_blueprint(journal_routes.bp)

ENDPOINTS
  GET  /api/journal/stats          counts by source, graded vs ungraded
  GET  /api/journal/queue          fills awaiting a grade (the UI's worklist)
  GET  /api/journal/vocab          known tags + observed condition values
  POST /api/journal/grade          {id, grade, tags_good, tags_bad, note}
  POST /api/journal/fill           record a manual fill by hand
  POST /api/journal/conditions     {id, conditions} correct a fill's layers
  POST /api/journal/import         {path} import a broker/sheet export
  GET  /api/legs                   paired entry/exit legs (the detail table)
  GET  /api/heatmap                the grid, with optional layer filters
  GET  /api/heatmap/layers         filterable values for the layer picker
  GET  /api/heatmap/compare        one grid per value of a split key
  GET  /api/journal/reconcile      proof the pairing conserves money
  GET  /api/conditions/rank        every condition/tag scored vs the book
"""

from flask import Blueprint, jsonify, request

import config_manager as cm
import journal as jn
import pairing
import heatmap as hm
import journal_import as jimp
import conditions as cond


bp = Blueprint('journal', __name__)


def _fail(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


# ─── JOURNAL ───────────────────────────────────────────────────────────────────

@bp.route('/api/journal/stats')
def journal_stats():
    return jsonify(jn.stats())


@bp.route('/api/journal/queue')
def journal_queue():
    """Ungraded fills, oldest first. This is the journaling worklist."""
    limit = int(request.args.get('limit', 50))
    rows = jn.ungraded(limit=limit)
    return jsonify({'count': len(jn.ungraded()), 'rows': rows})


@bp.route('/api/journal/vocab')
def journal_vocab():
    return jsonify({'tags': jn.known_tags(),
                    'conditions': jn.condition_values(),
                    'condition_keys': jn.CONDITION_KEYS})


@bp.route('/api/journal/grade', methods=['POST'])
def journal_grade():
    body = request.json or {}
    rid = body.get('id')
    if not rid:
        return _fail('id required')
    try:
        rec = jn.set_grade(rid, body.get('grade'),
                           tags_good=body.get('tags_good'),
                           tags_bad=body.get('tags_bad'),
                           note=body.get('note'))
    except ValueError as e:
        return _fail(str(e))
    if rec is None:
        return _fail('no such record', 404)
    return jsonify({'ok': True, 'record': rec,
                    'remaining': len(jn.ungraded())})


@bp.route('/api/journal/conditions', methods=['POST'])
def journal_conditions():
    body = request.json or {}
    rid = body.get('id')
    if not rid:
        return _fail('id required')
    rec = jn.set_conditions(rid, body.get('conditions') or {})
    if rec is None:
        return _fail('no such record', 404)
    return jsonify({'ok': True, 'record': rec})


@bp.route('/api/journal/fill', methods=['POST'])
def journal_fill():
    """Record a fill by hand — a trade taken in the broker, not the engine."""
    body = request.json or {}
    required = ('ticker', 'side', 'qty', 'price', 'filled_at')
    missing = [k for k in required if body.get(k) in (None, '')]
    if missing:
        return _fail(f"missing: {', '.join(missing)}")
    try:
        rec = jn.log_fill(
            ticker=body['ticker'], symbol=body.get('symbol') or body['ticker'],
            side=body['side'], qty=float(body['qty']),
            price=float(body['price']), filled_at=body['filled_at'],
            source='manual', conditions=body.get('conditions') or {},
            grade=body.get('grade'), tags_good=body.get('tags_good'),
            tags_bad=body.get('tags_bad'), note=body.get('note', ''))
    except (ValueError, TypeError) as e:
        return _fail(str(e))
    return jsonify({'ok': True, 'record': rec})


@bp.route('/api/journal/import', methods=['POST'])
def journal_import_route():
    """Import a sheet/broker export, by uploaded content or by path.

    Prefers `text` (the browser already read and decoded the file, so drive
    letters, quoting and encodings never enter the picture). Falls back to
    `path` for the typed-path box and the CLI.

    dry_run defaults TRUE so the UI can show parse problems before writing.
    """
    body = request.json or {}
    dry = bool(body.get('dry_run', True))
    text = body.get('text')
    path = body.get('path')

    try:
        if text:
            res = jimp.import_text(text, dry_run=dry)
        elif path:
            res = jimp.import_file(path, dry_run=dry)
        else:
            return _fail('choose a file, or give a path')
    except FileNotFoundError as e:
        return _fail(f'No such file: {e}', 404)
    except (ValueError, IsADirectoryError, PermissionError, OSError) as e:
        # Everything a bad path or wrong file type produces. These are the
        # user's problem to fix, not a server fault, and the message says
        # exactly what to do about it.
        return _fail(str(e) or f'{type(e).__name__}', 400)
    except Exception as e:
        # Anything genuinely unexpected: keep the traceback where it can be
        # found instead of leaving a bare 500 in the console.
        import traceback
        cm.log_forensic('api_event', event='journal_import', status='error',
                        error=f'{type(e).__name__}: {e}',
                        traceback=traceback.format_exc()[-1500:])
        return _fail(f'{type(e).__name__}: {e}', 500)
    res['ok'] = True
    return jsonify(res)


@bp.route('/api/journal/fills')
def journal_fills():
    """Browse/search fills. scope: all | ungraded | graded."""
    return jsonify({'ok': True, 'rows': jn.search(
        query=request.args.get('q', ''),
        scope=request.args.get('scope', 'all'),
        limit=int(request.args.get('limit', 200)))})


@bp.route('/api/journal/fill/update', methods=['POST'])
def journal_fill_update():
    """Edit an existing fill's details. cash/multiplier/instrument are
    recomputed server-side and are not accepted from the caller."""
    body = request.json or {}
    rid = body.pop('id', None)
    if not rid:
        return _fail('id required')
    try:
        rec = jn.edit_fill(rid, **body)
    except (ValueError, TypeError) as e:
        return _fail(str(e))
    if rec is None:
        return _fail('no such record', 404)
    return jsonify({'ok': True, 'record': rec})


@bp.route('/api/journal/fill/delete', methods=['POST'])
def journal_fill_delete():
    """Remove one fill. Reports whether the pairing still reconciles after,
    because deleting half a pair leaves an orphan that pairs wrong."""
    body = request.json or {}
    rid = body.get('id')
    if not rid:
        return _fail('id required')
    gone = jn.delete_record(rid)
    if gone is None:
        return _fail('no such record', 404)
    return jsonify({'ok': True, 'removed': gone,
                    'reconcile': pairing.reconcile()})


@bp.route('/api/journal/purge', methods=['POST'])
def journal_purge():
    """Bulk-remove records. dry_run defaults TRUE.

    Intended use: undo an Alpaca sync that re-pulled history you had already
    imported with grades. Pass ungraded_only so hand-graded work is never in
    the blast radius.
    """
    body = request.json or {}
    if not any(k in body for k in ('source', 'via', 'ungraded_only')):
        return _fail('specify at least one of: source, via, ungraded_only')
    res = jn.purge(source=body.get('source'), via=body.get('via'),
                   ungraded_only=bool(body.get('ungraded_only', False)),
                   dry_run=bool(body.get('dry_run', True)))
    res['ok'] = True
    return jsonify(res)


@bp.route('/api/journal/cutoff')
def journal_cutoff():
    """Newest fill on file, so the UI can default a sync window that does not
    reach back into already-imported history."""
    return jsonify({'ok': True,
                    'latest': jn.latest_filled_at(),
                    'latest_graded': jn.latest_filled_at(graded_only=True)})


# ─── PAIRING / HEATMAP ─────────────────────────────────────────────────────────

def _narrow_from_args():
    """Pull the non-layer filters out of the query string."""
    a = request.args
    same_day = a.get('same_day')
    return {
        'instrument': a.get('instrument') or None,
        'source': a.get('source') or None,
        'direction': a.get('direction') or None,
        'outcome': a.get('outcome') or None,
        'same_day': (None if same_day in (None, '')
                     else same_day.lower() in ('1', 'true', 'yes')),
        'tag_any': a.getlist('tag_any') or None,
        'tag_none': a.getlist('tag_none') or None,
    }


def _filters_from_args():
    """Layer filters arrive as repeated ?layer=key:value pairs."""
    out = {}
    for raw in request.args.getlist('layer'):
        if ':' not in raw:
            continue
        k, v = raw.split(':', 1)
        out.setdefault(k.strip(), []).append(v.strip())
    return out


@bp.route('/api/legs')
def legs_route():
    legs = pairing.build_legs()
    legs = hm.apply_filters(legs, _filters_from_args(), **_narrow_from_args())
    limit = int(request.args.get('limit', 500))
    return jsonify({'count': len(legs), 'legs': legs[:limit]})


@bp.route('/api/heatmap')
def heatmap_route():
    sparse = int(request.args.get('sparse_below', 3))
    grid = hm.build(filters=_filters_from_args(),
                    sparse_below=sparse, **_narrow_from_args())
    return jsonify(grid)


@bp.route('/api/conditions/rank')
def conditions_rank():
    """Honours the same layer filters as the heatmap, so the ranking can be
    asked within a slice ("among trades with the 34/50 with me, what else
    mattered")."""
    return jsonify(cond.rank(filters=_filters_from_args(),
                             **_narrow_from_args()))


@bp.route('/api/heatmap/layers')
def heatmap_layers():
    return jsonify(hm.layer_options())


@bp.route('/api/heatmap/compare')
def heatmap_compare():
    key = request.args.get('key')
    if not key:
        return _fail('key required')
    return jsonify(hm.compare(key, **_narrow_from_args()))


@bp.route('/api/journal/reconcile')
def journal_reconcile():
    """Proof the pairing conserves money. If ok is false, distrust the grid."""
    return jsonify(pairing.reconcile())
