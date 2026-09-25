"""
setup_routes.py — Foundations Trading

The Setup tab's backend: credentials, account type, and the two ways of
getting fills into the journal that don't involve a CSV file.

Built as a blueprint factory because it needs the live AlpacaManager and a
way to disarm the engine, both of which live in app.py. Register with:

    import setup_routes
    app.register_blueprint(setup_routes.make_bp(alpaca, _force_engine_off))

THE LIVE SWITCH
  Flipping paper -> live is the single most consequential setting in this
  program, because the bar loop can place orders without asking. Three
  guards, all deliberate:

    1. The request must carry confirm == 'LIVE'. A stray click cannot do it.
    2. ANY change to account type or credentials forces the engine off. You
       re-arm manually, after reconnecting, having seen which account you
       are pointed at.
    3. The connection is dropped. Nothing trades until you reconnect, which
       makes the account type visible in the connect banner first.

  The secret is never returned to the browser. GET reports only whether one
  is set and the last four characters of the key.
"""

from flask import Blueprint, jsonify, request

import config_manager as cm
import journal_sync as jsync


def make_bp(alpaca, force_engine_off):
    bp = Blueprint('setup', __name__)

    def _fail(msg, code=400):
        return jsonify({'ok': False, 'error': msg}), code

    # ─── CONFIG ───────────────────────────────────────────────────────────────

    @bp.route('/api/config', methods=['GET'])
    def get_config():
        cfg = cm.load_config()
        key = cfg.get('alpaca_key') or ''
        return jsonify({
            'ok': True,
            'has_key': bool(key),
            'has_secret': bool(cfg.get('alpaca_secret')),
            'key_tail': key[-4:] if len(key) >= 4 else '',
            'paper_trading': bool(cfg.get('paper_trading', True)),
            'notifications_enabled': bool(cfg.get('notifications_enabled', True)),
            'connected': alpaca.is_connected(),
            'connected_as': ('paper' if alpaca.paper else 'live')
                            if alpaca.is_connected() else None,
            'data_dir': cm.DATA_DIR,
        })

    @bp.route('/api/config', methods=['POST'])
    def set_config():
        """Update credentials and/or account type.

        Any change here disarms the engine and drops the connection. See the
        module docstring for why.
        """
        body = request.json or {}
        cfg = cm.load_config()

        want_paper = body.get('paper_trading')
        changing_account = (want_paper is not None
                            and bool(want_paper) != bool(cfg.get('paper_trading', True)))

        if changing_account and not want_paper:
            if str(body.get('confirm', '')).strip() != 'LIVE':
                return _fail("Switching to a live account requires confirm='LIVE'.")

        changed = []
        if body.get('alpaca_key'):
            cfg['alpaca_key'] = str(body['alpaca_key']).strip()
            changed.append('key')
        if body.get('alpaca_secret'):
            cfg['alpaca_secret'] = str(body['alpaca_secret']).strip()
            changed.append('secret')
        if want_paper is not None:
            cfg['paper_trading'] = bool(want_paper)
            if changing_account:
                changed.append('account type')
        if 'notifications_enabled' in body:
            cfg['notifications_enabled'] = bool(body['notifications_enabled'])

        if not changed:
            cm.save_config(cfg)
            return jsonify({'ok': True, 'changed': [], 'engine_disarmed': False,
                            'disconnected': False})

        cm.save_config(cfg)

        # Credentials or account type moved: stop everything and make the
        # user reconnect deliberately.
        force_engine_off()
        alpaca.disconnect()
        cm.log_forensic('conn_event', event='config_change', status='ok',
                        changed=','.join(changed),
                        paper=bool(cfg.get('paper_trading', True)))

        return jsonify({
            'ok': True, 'changed': changed,
            'engine_disarmed': True, 'disconnected': True,
            'paper_trading': bool(cfg.get('paper_trading', True)),
            'message': 'Engine disarmed and disconnected. Reconnect, confirm '
                       'the account shown, then re-arm the engine.',
        })

    # ─── JOURNAL SYNC ─────────────────────────────────────────────────────────

    @bp.route('/api/journal/sync', methods=['POST'])
    def journal_sync():
        """Pull FILL activities off the connected account into the journal.

        dry_run defaults to TRUE so the UI can show what it found before
        anything is written.
        """
        body = request.json or {}
        if not alpaca.is_connected():
            return _fail('Not connected to Alpaca. Connect first.', 409)
        try:
            res = jsync.sync_alpaca(
                alpaca, days=body.get('days'),
                start=body.get('start'), end=body.get('end'),
                after=body.get('after'), until=body.get('until'),
                dry_run=bool(body.get('dry_run', True)))
        except ValueError as e:
            return _fail(str(e))
        except Exception as e:
            cm.log_forensic('api_event', event='journal_sync', status='error',
                            error=f'{type(e).__name__}: {e}')
            return _fail(f'{type(e).__name__}: {e}', 500)
        return jsonify(res)

    @bp.route('/api/journal/paste', methods=['POST'])
    def journal_paste():
        """Ingest rows copied out of a broker page or spreadsheet."""
        body = request.json or {}
        text = body.get('text') or ''
        if not text.strip():
            return _fail('nothing pasted')
        try:
            res = jsync.import_paste(text, dry_run=bool(body.get('dry_run', True)))
        except Exception as e:
            return _fail(f'{type(e).__name__}: {e}', 500)
        return jsonify(res)

    return bp
