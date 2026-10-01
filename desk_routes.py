"""
desk_routes.py — Foundations Trading

The Trade Desk's HTTP surface. Per-ticker strategy and exit parameters: what
engine a name runs, whether it is live, and the exit ladder TEMPLATE that
governs how a position is let go.

WHY THE DESK OWNS THIS, in Nate's words (board d-signal-panel, 2026-10-01):

    "maybe this is where stop limits etc should live, not the part of the
    program that is buying or selling. The buying/selling portion should
    simply take orders from parameters set"

That is the separation engine_contract already asserts between "what to
trade" and "how to trade it", applied to exits. The Desk holds POLICY. The
execution layer -- trade_router, _do_exit, alpaca_manager -- is MECHANISM and
asks no questions. exit_ladder.py already works this way: it proposes intents
on bar close and the router disposes, so the Desk is the missing front panel
rather than a new mechanism.

A TEMPLATE, NOT A LADDER. exit_ladder.make_ladder needs the fill price and the
position size, neither of which exists while you are setting parameters. So
the Desk stores FRACTIONS and ATR DISTANCES, and the ladder is built at fill.
backtester.resolve_ladder_template already consumes exactly that shape, which
is the point: every knob here is a knob the backtester can sweep. That is the
rule this whole surface earns its existence under (design/07 §1) -- an exit
rule may exist as configuration if and only if the backtester can replay it
from bars alone.

WHY RUNGS MUST BE ATR-KEYED HERE. A price-keyed rung ("sell 3 at $421.50") is
meaningful only against a known entry. A template is written before the entry
exists, so distance is the only expressible unit, and ATR is the one that
travels across symbols and across time. make_ladder still accepts price rungs
for a hand-placed order through the panel; it is the TEMPLATE that cannot.
"""

from flask import Blueprint, jsonify, request

import config_manager as cm
import exit_ladder as el

MAX_RUNGS = 3          # three dials is a panel; eight is a spreadsheet
FRAC_MIN = 0.05        # below a twentieth of a position, the fill is noise
RATCHET_MIN = 0.05
RATCHET_MAX = 3.0
ATR_MIN = 0.05         # design/07 §6.1: the median per-trade excursion on SPY
ATR_MAX = 3.0          # was 0.82 ATR and only 3 of 209 trades travelled 1.0


def _fail(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def validate_template(tpl):
    """A Desk ladder template -> a normalised copy. Raises ValueError.

    Returns None for "no ladder", which is a legitimate and the DEFAULT state:
    design/07 §6.1 measured scale-outs raising the win rate and losing money,
    so an empty template is the position the evidence supports.
    """
    if not tpl:
        return None
    rungs = tpl.get('rungs') or []
    if not isinstance(rungs, list):
        raise ValueError('rungs must be a list')
    if len(rungs) > MAX_RUNGS:
        raise ValueError(f'at most {MAX_RUNGS} rungs')

    out, total = [], 0.0
    for i, r in enumerate(rungs, 1):
        try:
            frac = float(r.get('frac'))
            atr = float(r.get('atr'))
        except (TypeError, ValueError):
            raise ValueError(f'rung {i}: frac and atr must be numbers')
        if not FRAC_MIN <= frac <= 1.0:
            raise ValueError(f'rung {i}: frac {frac} outside '
                             f'{FRAC_MIN}-1.0')
        if not ATR_MIN <= atr <= ATR_MAX:
            raise ValueError(f'rung {i}: atr {atr} outside '
                             f'{ATR_MIN}-{ATR_MAX}')
        total += frac
        out.append({'frac': round(frac, 4), 'atr': round(atr, 4)})
    if total > 1.0 + 1e-9:
        raise ValueError(f'rung fractions total {round(total, 4)}; '
                         f'cannot exceed the whole position')

    # Rungs are reported and filled in the order given, so a template whose
    # distances run backwards would fire the far rung first. Sorting is a
    # silent fix; refusing says what is wrong.
    if [r['atr'] for r in out] != sorted(r['atr'] for r in out):
        raise ValueError('rungs must be ordered by increasing ATR distance')

    rat = tpl.get('ratchet') or {}
    ratchet = None
    if rat.get('enabled'):
        try:
            mult = float(rat.get('atr_mult'))
        except (TypeError, ValueError):
            raise ValueError('ratchet atr_mult must be a number')
        if not RATCHET_MIN <= mult <= RATCHET_MAX:
            raise ValueError(f'ratchet atr_mult {mult} outside '
                             f'{RATCHET_MIN}-{RATCHET_MAX}')
        ratchet = {'enabled': True, 'atr_mult': round(mult, 4)}

    remainder = tpl.get('remainder') or 'engine'
    if remainder not in el.REMAINDERS:
        raise ValueError(f'remainder must be one of {sorted(el.REMAINDERS)}')

    if not out and not ratchet:
        return None
    return {'rungs': out, 'ratchet': ratchet, 'remainder': remainder}


def describe(tpl):
    """Plain English for the readout. The panel shows this, not the JSON."""
    if not tpl:
        return ('No exit parameters. The engine closes the whole position on '
                'its own structural signal, which is what design/07 §6.1 '
                'measured as the best of the options tested.')
    bits = []
    for i, r in enumerate(tpl['rungs'], 1):
        bits.append(f"rung {i}: sell {round(r['frac'] * 100)}% at "
                    f"+{r['atr']} ATR from entry")
    if tpl.get('ratchet'):
        bits.append(f"ratchet: trail the high-water mark by "
                    f"{tpl['ratchet']['atr_mult']} ATR")
    held = 1.0 - sum(r['frac'] for r in tpl['rungs'])
    if held > 1e-9:
        bits.append(f"the remaining {round(held * 100)}% is left to the "
                    f"{tpl['remainder']}")
    return '; '.join(bits) + '.'


def make_bp(registry, resolver, sync_overrides):
    """`sync_overrides(cfg)` re-points the resolver after a config write, so
    the Desk never mutates resolver state directly -- app.py owns that."""
    bp = Blueprint('desk', __name__)

    def _entry(cfg, ticker):
        for w in cfg.get('watchlist', []):
            if w.get('ticker', '').upper() == ticker:
                return w
        return None

    @bp.route('/api/desk')
    def desk_get():
        """Every name on the desk, with its strategy and its exit template."""
        cfg = cm.load_config()
        rows = []
        for w in cfg.get('watchlist', []):
            t = (w.get('ticker') or '').upper()
            if not t:
                continue
            eng = w.get('engine') or resolver.default_name
            tpl = w.get('ladder_template')
            rows.append({
                'ticker': t,
                'engine': eng,
                'engine_explicit': bool(w.get('engine')),
                'mode': w.get('mode', 'run'),
                'ladder': tpl,
                'ladder_says': describe(tpl),
            })
        return jsonify({'ok': True, 'rows': rows,
                        'engines': registry.names(),
                        'default_engine': resolver.default_name,
                        'limits': {'max_rungs': MAX_RUNGS,
                                   'frac_min': FRAC_MIN,
                                   'atr_min': ATR_MIN, 'atr_max': ATR_MAX,
                                   'ratchet_min': RATCHET_MIN,
                                   'ratchet_max': RATCHET_MAX}})

    @bp.route('/api/desk/<ticker>', methods=['POST'])
    def desk_set(ticker):
        """Set one name's engine and/or its exit template.

        Partial: a key that is absent is left alone. `engine: ''` clears the
        override so the name follows the default again, which is how the old
        /api/watchlist set_engine behaved and is worth keeping.
        """
        t = (ticker or '').strip().upper()
        body = request.json or {}
        cfg = cm.load_config()
        w = _entry(cfg, t)
        if w is None:
            return _fail(f'{t} is not on the watchlist', 404)

        if 'engine' in body:
            eng = (body.get('engine') or '').strip()
            if eng and eng not in registry.names():
                return _fail(f'unknown engine: {eng}')
            if eng:
                w['engine'] = eng
            else:
                w.pop('engine', None)

        if 'ladder' in body:
            try:
                tpl = validate_template(body.get('ladder'))
            except ValueError as e:
                return _fail(f'ladder: {e}')
            if tpl:
                w['ladder_template'] = tpl
            else:
                w.pop('ladder_template', None)

        cm.save_config(cfg)
        sync_overrides(cfg)
        tpl = w.get('ladder_template')
        cm.log_forensic('conn_event', event='desk_set', ticker=t,
                        status=(w.get('engine') or 'default'))
        return jsonify({'ok': True, 'ticker': t,
                        'engine': w.get('engine') or resolver.default_name,
                        'engine_explicit': bool(w.get('engine')),
                        'ladder': tpl, 'ladder_says': describe(tpl)})

    return bp


# ─── SELFTEST ──────────────────────────────────────────────────────────────────

def selftest():
    """validate_template and describe only. No Flask, no config, no I/O."""
    fails = []

    def ck(label, cond, got=None):
        if cond:
            print(f'  ok   {label}')
        else:
            fails.append(label)
            print(f'  FAIL {label}' + (f'   got {got!r}' if got is not None else ''))

    def bad(tpl):
        try:
            validate_template(tpl)
        except ValueError:
            return True
        return False

    print('an empty template is a real answer, not a missing one')
    ck('None stays None', validate_template(None) is None)
    ck('{} is None', validate_template({}) is None)
    ck('no rungs and no ratchet is None',
       validate_template({'rungs': []}) is None)
    ck('a ratchet alone is a template',
       validate_template({'ratchet': {'enabled': True, 'atr_mult': 0.5}})
       is not None)

    print('fractions')
    one = {'rungs': [{'frac': 0.5, 'atr': 0.25}]}
    ck('a single half-position rung is fine',
       validate_template(one)['rungs'] == [{'frac': 0.5, 'atr': 0.25}])
    ck('fractions totalling exactly 1.0 are allowed',
       validate_template({'rungs': [{'frac': 0.5, 'atr': 0.2},
                                    {'frac': 0.5, 'atr': 0.4}]}) is not None)
    ck('over the whole position RAISES',
       bad({'rungs': [{'frac': 0.6, 'atr': 0.2}, {'frac': 0.6, 'atr': 0.4}]}))
    ck('a frac under the floor raises', bad({'rungs': [{'frac': 0.01, 'atr': 0.2}]}))
    ck('a frac over 1.0 raises', bad({'rungs': [{'frac': 1.5, 'atr': 0.2}]}))
    ck('a non-numeric frac raises', bad({'rungs': [{'frac': 'half', 'atr': 0.2}]}))

    print('distances are ATR, ordered, and bounded by what the tape does')
    ck('too many rungs raises',
       bad({'rungs': [{'frac': 0.2, 'atr': 0.1 * i} for i in range(1, 6)]}))
    ck('out-of-order distances RAISE rather than being silently sorted',
       bad({'rungs': [{'frac': 0.3, 'atr': 0.8}, {'frac': 0.3, 'atr': 0.2}]}))
    ck('a 5-ATR rung raises -- only 3 of 209 SPY trades travelled 1.0',
       bad({'rungs': [{'frac': 0.5, 'atr': 5.0}]}))
    ck('a 0.01-ATR rung raises', bad({'rungs': [{'frac': 0.5, 'atr': 0.01}]}))

    print('ratchet')
    ck('disabled ratchet is dropped',
       validate_template({'rungs': [{'frac': 0.5, 'atr': 0.25}],
                          'ratchet': {'enabled': False, 'atr_mult': 0.5}}
                         )['ratchet'] is None)
    ck('an out-of-range multiple raises',
       bad({'ratchet': {'enabled': True, 'atr_mult': 99}}))
    ck('a non-numeric multiple raises',
       bad({'ratchet': {'enabled': True, 'atr_mult': 'tight'}}))

    print('remainder')
    ck('defaults to the engine',
       validate_template(one)['remainder'] == 'engine')
    ck('an unknown remainder raises',
       bad({'rungs': [{'frac': 0.5, 'atr': 0.25}], 'remainder': 'hope'}))

    print('describe() says what the knobs mean, not what they are')
    ck('an empty template explains WHY it is empty',
       'structural' in describe(None))
    d = describe(validate_template({'rungs': [{'frac': 0.5, 'atr': 0.25}],
                                    'ratchet': {'enabled': True,
                                                'atr_mult': 0.5}}))
    ck('names the percentage and the distance', 'sell 50%' in d and '0.25 ATR' in d)
    ck('names the ratchet', 'ratchet' in d)
    ck('accounts for what is left', 'remaining 50%' in d, d)

    print()
    if fails:
        print(f'SELFTEST FAILED -- {len(fails)} of the above')
    else:
        print('SELFTEST PASSED')
    return not fails


if __name__ == '__main__':
    import sys
    sys.exit(0 if selftest() else 1)
