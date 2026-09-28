"""
exit_ladder.py — Foundations Trading

Partial exits: scale-out rungs and an ATR ratchet. Spec: `design/07-order-placement.md`.

Pure logic. A ladder and a bar close in, exit intents out. No broker calls, no I/O,
no threads, no clock it does not receive. Mirrors signal_engine's shape on purpose:
the thing that decides is separable from the thing that places orders, so both can
be replayed by backtester.py and asserted by a selftest.

WHAT A LADDER IS
  One entry, several partial exits. `rungs` say "sell this much once the UNDERLYING
  trades at this level"; the `ratchet` raises a floor under whatever the rungs do not
  sell. Both are keyed to the underlying price, never to premium — see §1 of the spec
  for why that restriction is what makes this configurable at all.

PROPOSE, DO NOT COMMIT
  evaluate() returns INTENTS. It never marks a rung fired, because a rung is only
  fired when the broker says so. The caller runs the order, confirms the fill, and
  then calls mark_fired(). A rejected rung is therefore still unfired and is retried
  on the next close, which is what the failure table in §8 requires. The one piece of
  state evaluate() does advance is the ratchet's high-water mark, because that is a
  pure function of the bars and owes nothing to the broker.

WHOLE UNITS ONLY
  Quantities are shares or contracts, integers, absolute. No percentages: see §2.

DIRECTION
  'long' and 'short' mirror completely. A long's rungs sit above the anchor and its
  floor rises; a short's sit below and its floor falls. There is one comparison in
  this module and `_favourable` is it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

EPS = 1e-9

SLEEVES = ('shares', 'options')
REMAINDERS = ('engine', 'ratchet')
RUNG_KINDS = ('price', 'atr')

# exit_kind values this module produces. Both are additive to journal.CONDITION_KEYS,
# which already carries 'structural' and 'ride_end', so the heatmap can layer on them
# with no UI work.
KIND_RUNG = 'rung'
KIND_RATCHET = 'ratchet'


# ─── CONSTRUCTION ─────────────────────────────────────────────────────────────

def make_ladder(*, sleeve, anchor, atr, position_qty, rungs,
                ratchet=None, remainder='engine'):
    """Validate and build a ladder. Raises ValueError on anything malformed.

    anchor is the entry fill price ON THE UNDERLYING. atr is ATR at fill (see
    levels.atr) and is PINNED here: it is never recomputed, so a rung cannot drift
    because volatility expanded after you set it. atr may be None only if no rung
    uses kind 'atr' and the ratchet is off.

    rungs is a list of {'qty': int, 'at': {'kind': 'price'|'atr', 'value': float}}.
    An 'id' is assigned if absent. Order is preserved and is the FIFO order rungs
    are reported in.

    ratchet is {'enabled': bool, 'atr_mult': float} and gains its running state here.
    """
    if sleeve not in SLEEVES:
        raise ValueError(f'sleeve must be one of {SLEEVES}, got {sleeve!r}')
    if remainder not in REMAINDERS:
        raise ValueError(f'remainder must be one of {REMAINDERS}, got {remainder!r}')
    anchor = float(anchor)
    if anchor <= 0:
        raise ValueError(f'anchor must be positive, got {anchor}')
    position_qty = int(position_qty)
    if position_qty < 1:
        raise ValueError(f'position_qty must be >= 1, got {position_qty}')

    rt = dict(ratchet or {})
    rt_on = bool(rt.get('enabled'))

    clean = []
    needs_atr = rt_on
    for i, r in enumerate(rungs or []):
        at = dict(r.get('at') or {})
        kind = at.get('kind')
        if kind not in RUNG_KINDS:
            raise ValueError(f'rung {i}: at.kind must be one of {RUNG_KINDS}, '
                             f'got {kind!r}')
        try:
            value = float(at['value'])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'rung {i}: at.value must be a number')
        if kind == 'price' and value <= 0:
            raise ValueError(f'rung {i}: a price rung must be positive')
        if kind == 'atr':
            needs_atr = True
            if value <= 0:
                raise ValueError(f'rung {i}: an atr rung must be a positive '
                                 f'multiple (direction comes from the position, '
                                 f'not from the sign)')
        qty = int(r.get('qty', 0))
        if qty < 1:
            raise ValueError(f'rung {i}: qty must be >= 1, got {qty}')
        clean.append({'id': r.get('id') or f'r{i + 1}',
                      'qty': qty,
                      'at': {'kind': kind, 'value': value},
                      'fired_at': r.get('fired_at'),
                      'void': r.get('void')})

    ids = [r['id'] for r in clean]
    if len(set(ids)) != len(ids):
        raise ValueError(f'duplicate rung ids: {ids}')

    if needs_atr:
        if atr is None:
            raise ValueError('atr is required when a rung uses kind "atr" or the '
                             'ratchet is enabled')
        atr = float(atr)
        if atr <= 0:
            raise ValueError(f'atr must be positive, got {atr}')
    else:
        atr = None if atr is None else float(atr)

    # §2: rungs may sum to LESS than the position (the leftover is the runner) and
    # may not sum to more. Overselling is a broker rejection at best and a surprise
    # short at worst.
    total = sum(r['qty'] for r in clean)
    if total > position_qty:
        raise ValueError(f'rung quantities sum to {total}, which exceeds the '
                         f'position of {position_qty}')

    if remainder == 'ratchet' and not rt_on:
        raise ValueError("remainder='ratchet' requires ratchet.enabled")
    if rt_on:
        mult = float(rt.get('atr_mult', 0))
        if mult <= 0:
            raise ValueError(f'ratchet.atr_mult must be positive, got {mult}')
        rt = {'enabled': True, 'atr_mult': mult,
              'high_water': rt.get('high_water'),
              'floor': rt.get('floor'),
              'armed_at': rt.get('armed_at')}
    else:
        rt = {'enabled': False}

    return {'sleeve': sleeve, 'anchor': anchor, 'atr': atr,
            'position_qty': position_qty, 'rungs': clean,
            'ratchet': rt, 'remainder': remainder}


# ─── GEOMETRY ─────────────────────────────────────────────────────────────────

def _sign(direction):
    """+1 for a long, -1 for a short. The only place direction becomes arithmetic."""
    if direction == 'long':
        return 1
    if direction == 'short':
        return -1
    raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")


def _favourable(direction, a, b):
    """Is `a` at least as far in the profitable direction as `b`?

    Long: a >= b. Short: a <= b. Every trigger test in this module goes through
    here so there is exactly one place the mirror can be wrong.
    """
    return (a - b) * _sign(direction) >= -EPS


def rung_price(rung, ladder, direction):
    """Resolve a rung to an absolute underlying price.

    'price' is taken as given — it is the level you read off the chart. 'atr' is a
    multiple away from the anchor IN THE PROFITABLE DIRECTION, which is what makes
    one ladder shape portable across SPY and a $40 name.
    """
    at = rung['at']
    if at['kind'] == 'price':
        return float(at['value'])
    atr = ladder.get('atr')
    if not atr:
        raise ValueError('atr rung on a ladder with no pinned atr')
    return ladder['anchor'] + _sign(direction) * at['value'] * atr


def pending(ladder):
    """Rungs that have neither fired nor been voided, in ladder order."""
    return [r for r in ladder['rungs']
            if r.get('fired_at') is None and not r.get('void')]


def remaining_qty(ladder, held_qty):
    """What the ratchet would close: held, less everything still owed to rungs.

    A rung that has not fired is still earmarked, so the ratchet does not close
    shares a rung is waiting for. Never negative.
    """
    owed = sum(r['qty'] for r in pending(ladder))
    return max(0, int(held_qty) - owed)


# ─── THE RATCHET ──────────────────────────────────────────────────────────────

def advance_ratchet(ladder, direction, bar_close, now=None):
    """Update high_water and floor from one bar CLOSE. Returns a new ladder.

    Closes, not prints: a wick-driven floor ratchets to a price that never existed
    as a close and then stops you out on an ordinary pullback (§3).

    The current close is folded into high_water BEFORE the floor is recomputed, so
    a bar that makes a new high can never also trigger the floor. The floor is
    monotonic — max() for a long, min() for a short — which is the whole difference
    between a ratchet and a stop you can talk downwards.
    """
    rt = ladder['ratchet']
    if not rt.get('enabled'):
        return ladder
    s = _sign(direction)
    close = float(bar_close)

    hw = rt.get('high_water')
    hw = close if hw is None else (max(hw, close) if s > 0 else min(hw, close))

    raw = hw - s * rt['atr_mult'] * ladder['atr']
    fl = rt.get('floor')
    fl = raw if fl is None else (max(fl, raw) if s > 0 else min(fl, raw))

    new_rt = dict(rt, high_water=hw, floor=fl,
                  armed_at=rt.get('armed_at') or _iso(now))
    return dict(ladder, ratchet=new_rt)


# ─── EVALUATION ───────────────────────────────────────────────────────────────

def evaluate(ladder, direction, bar_close, held_qty, now=None):
    """Decide what a ladder wants to do on one bar close.

    Returns {'ladder', 'intents', 'notes'}.

      ladder   a NEW ladder with the ratchet advanced. Rungs are untouched: see
               PROPOSE, DO NOT COMMIT in the module docstring.
      intents  zero or one intent. Each is
               {'kind', 'qty', 'at', 'rung_ids', 'reason'}.
      notes    human-readable strings for the forensic log. A clamp is always a
               note, because it means app state and broker state disagreed.

    THE RATCHET WINS A TIE. If the floor is broken on the same bar that clears a
    rung, the floor closes everything that is left, so the rung is moot and is
    reported as voided rather than fired. One exit, one exit_kind, no ambiguity
    about which rule got you out.

    ALL CLEARED RUNGS FIRE AS ONE ORDER. If a bar gaps through three rungs, they
    aggregate into a single intent rather than being served one per bar. Serving
    them across bars would fill the later ones further from their levels for no
    reason; the constraint that actually matters is never having two unconfirmed
    partial exits in flight on one sleeve, and one aggregated order satisfies it.
    """
    held_qty = int(held_qty)
    notes = []
    out = advance_ratchet(ladder, direction, bar_close, now=now)

    if held_qty <= 0:
        return {'ladder': out, 'intents': [],
                'notes': ['nothing held; ladder is inert']}

    close = float(bar_close)
    rt = out['ratchet']

    # ── the ratchet, first, because it wins a tie ──
    if rt.get('enabled') and rt.get('floor') is not None:
        if not _favourable(direction, close, rt['floor']):
            voided = [r['id'] for r in pending(out)]
            if voided:
                notes.append(f'ratchet closed the position; rungs voided: '
                             f'{", ".join(voided)}')
            return {'ladder': out,
                    'intents': [{'kind': KIND_RATCHET, 'qty': held_qty,
                                 'at': round(rt['floor'], 4),
                                 'rung_ids': [],
                                 'reason': f'close {close:.4f} through ratchet '
                                           f'floor {rt["floor"]:.4f} '
                                           f'({rt["atr_mult"]}x ATR '
                                           f'{out["atr"]:.4f} under '
                                           f'{rt["high_water"]:.4f})'}],
                    'notes': notes}

    # ── rungs ──
    cleared = [r for r in pending(out)
               if _favourable(direction, close, rung_price(r, out, direction))]
    if not cleared:
        return {'ladder': out, 'intents': [], 'notes': notes}

    want = sum(r['qty'] for r in cleared)
    qty = min(want, held_qty)
    if qty < want:
        notes.append(f'rung quantity clamped {want} -> {qty}: only {held_qty} held. '
                     f'Position was reduced outside the ladder.')
    ids = [r['id'] for r in cleared]
    levels = ', '.join(f'{r["id"]}@{rung_price(r, out, direction):.4f}'
                       for r in cleared)
    return {'ladder': out,
            'intents': [{'kind': KIND_RUNG, 'qty': qty,
                         'at': round(close, 4),
                         'rung_ids': ids,
                         'reason': f'close {close:.4f} cleared {levels}'}],
            'notes': notes}


# ─── COMMITTING ───────────────────────────────────────────────────────────────

def mark_fired(ladder, rung_ids, now=None):
    """Stamp rungs as fired. Call this ONLY after confirm_fill returns broker truth.

    Idempotent: a rung already fired keeps its original stamp.
    """
    stamp = _iso(now)
    ids = set(rung_ids or [])
    rungs = [dict(r, fired_at=(r.get('fired_at') or stamp)) if r['id'] in ids
             else r for r in ladder['rungs']]
    return dict(ladder, rungs=rungs)


def void_rungs(ladder, reason, now=None):
    """Retire every pending rung with a reason. Used when the engine's structural
    exit closes the whole position and the rungs no longer have anything to sell."""
    stamp = _iso(now)
    rungs = [dict(r, void={'reason': reason, 'at': stamp})
             if (r.get('fired_at') is None and not r.get('void')) else r
             for r in ladder['rungs']]
    return dict(ladder, rungs=rungs)


def describe(ladder, direction):
    """One line per rung plus the ratchet, for the panel and the log."""
    lines = []
    for r in ladder['rungs']:
        state = ('fired ' + r['fired_at']) if r.get('fired_at') else (
            'void: ' + r['void']['reason'] if r.get('void') else 'pending')
        lines.append(f'{r["id"]}: sell {r["qty"]} at '
                     f'{rung_price(r, ladder, direction):.4f} ({state})')
    rt = ladder['ratchet']
    if rt.get('enabled'):
        fl = 'not yet set' if rt.get('floor') is None else f'{rt["floor"]:.4f}'
        lines.append(f'ratchet: {rt["atr_mult"]}x ATR, floor {fl}')
    else:
        lines.append('ratchet: off')
    lines.append(f'remainder: {ladder["remainder"]}')
    return lines


def _iso(now=None):
    return (now or datetime.now(ET)).isoformat(timespec='seconds')


# ─── SELFTEST ─────────────────────────────────────────────────────────────────

def selftest():
    """py -3 exit_ladder.py --selftest"""
    fails = []

    def ck(name, cond, extra=''):
        if cond:
            print(f'  ok   {name}')
        else:
            print(f'  FAIL {name} {extra}')
            fails.append(name)

    # ── a long ladder: 10 shares, sell 4 at 512.50, sell 3 at +1.5 ATR ──
    L = make_ladder(sleeve='shares', anchor=500.0, atr=2.0, position_qty=10,
                    rungs=[{'qty': 4, 'at': {'kind': 'price', 'value': 512.5}},
                           {'qty': 3, 'at': {'kind': 'atr', 'value': 1.5}}],
                    ratchet={'enabled': True, 'atr_mult': 1.0},
                    remainder='ratchet')
    ck('atr rung resolves above the anchor for a long',
       abs(rung_price(L['rungs'][1], L, 'long') - 503.0) < 1e-9,
       rung_price(L['rungs'][1], L, 'long'))
    ck('runner is what the rungs do not claim', remaining_qty(L, 10) == 3)

    # nothing triggers below every rung; the floor is set but under water
    r = evaluate(L, 'long', 501.0, 10)
    ck('no intent below the first rung', r['intents'] == [])
    ck('floor tracks one ATR under the high close',
       abs(r['ladder']['ratchet']['floor'] - 499.0) < 1e-9,
       r['ladder']['ratchet']['floor'])

    # 503.2 clears the atr rung (503.0) but not the price rung (512.5)
    r = evaluate(r['ladder'], 'long', 503.2, 10)
    ck('one rung fires when only it is cleared',
       len(r['intents']) == 1 and r['intents'][0]['qty'] == 3
       and r['intents'][0]['rung_ids'] == ['r2'], r['intents'])
    ck('evaluate does not mark the rung fired',
       r['ladder']['rungs'][1]['fired_at'] is None)

    committed = mark_fired(r['ladder'], ['r2'])
    ck('mark_fired stamps exactly one rung',
       committed['rungs'][1]['fired_at'] is not None
       and committed['rungs'][0]['fired_at'] is None)
    stamp = committed['rungs'][1]['fired_at']
    ck('mark_fired is idempotent',
       mark_fired(committed, ['r2'])['rungs'][1]['fired_at'] == stamp)
    r2 = evaluate(committed, 'long', 504.0, 7)
    ck('a fired rung does not fire again', r2['intents'] == [])

    # a gap straight through both rungs aggregates into ONE order
    G = make_ladder(sleeve='shares', anchor=500.0, atr=2.0, position_qty=10,
                    rungs=[{'qty': 4, 'at': {'kind': 'price', 'value': 512.5}},
                           {'qty': 3, 'at': {'kind': 'atr', 'value': 1.5}}])
    g = evaluate(G, 'long', 520.0, 10)
    ck('a gap through both rungs is one intent of 7',
       len(g['intents']) == 1 and g['intents'][0]['qty'] == 7
       and g['intents'][0]['rung_ids'] == ['r1', 'r2'], g['intents'])

    # clamp: app thinks 10, broker holds 5
    c = evaluate(G, 'long', 520.0, 5)
    ck('quantity clamps to what is actually held',
       c['intents'][0]['qty'] == 5 and any('clamped' in n for n in c['notes']),
       c['notes'])

    # ── the ratchet ──
    R = make_ladder(sleeve='shares', anchor=500.0, atr=2.0, position_qty=10,
                    rungs=[], ratchet={'enabled': True, 'atr_mult': 1.0},
                    remainder='ratchet')
    s = evaluate(R, 'long', 510.0, 10)          # high 510 -> floor 508
    ck('floor sits one ATR under the high', abs(s['ladder']['ratchet']['floor'] - 508.0) < 1e-9)
    s = evaluate(s['ladder'], 'long', 509.0, 10)
    ck('floor does not fall when price pulls back',
       abs(s['ladder']['ratchet']['floor'] - 508.0) < 1e-9,
       s['ladder']['ratchet']['floor'])
    ck('no exit above the floor', s['intents'] == [])
    s2 = evaluate(s['ladder'], 'long', 507.9, 10)
    ck('close under the floor exits the remainder',
       len(s2['intents']) == 1 and s2['intents'][0]['kind'] == KIND_RATCHET
       and s2['intents'][0]['qty'] == 10, s2['intents'])
    hi = evaluate(s['ladder'], 'long', 600.0, 10)
    ck('a new high can never trigger its own floor', hi['intents'] == [])

    # ratchet wins a tie and voids the rungs it makes moot
    T = make_ladder(sleeve='shares', anchor=500.0, atr=2.0, position_qty=10,
                    rungs=[{'qty': 4, 'at': {'kind': 'price', 'value': 505.0}}],
                    ratchet={'enabled': True, 'atr_mult': 1.0})
    t = evaluate(T, 'long', 520.0, 10)           # floor 518, rung 505 cleared too
    ck('a new high does not break its own floor even with a rung cleared',
       len(t['intents']) == 1 and t['intents'][0]['kind'] == KIND_RUNG,
       t['intents'])
    t = evaluate(t['ladder'], 'long', 517.0, 10)  # now through the floor, rung still cleared
    ck('floor break beats a cleared rung',
       t['intents'][0]['kind'] == KIND_RATCHET
       and any('voided' in n for n in t['notes']), (t['intents'], t['notes']))

    # ── shorts mirror ──
    S = make_ladder(sleeve='options', anchor=500.0, atr=2.0, position_qty=3,
                    rungs=[{'qty': 2, 'at': {'kind': 'atr', 'value': 1.5}}],
                    ratchet={'enabled': True, 'atr_mult': 1.0})
    ck('a short atr rung resolves below the anchor',
       abs(rung_price(S['rungs'][0], S, 'short') - 497.0) < 1e-9)
    sh = evaluate(S, 'short', 498.0, 3)
    ck('short rung does not fire above its level', sh['intents'] == [])
    ck('short floor sits one ATR above the low',
       abs(sh['ladder']['ratchet']['floor'] - 500.0) < 1e-9,
       sh['ladder']['ratchet']['floor'])
    sh = evaluate(sh['ladder'], 'short', 496.9, 3)
    ck('short rung fires below its level',
       sh['intents'] and sh['intents'][0]['kind'] == KIND_RUNG, sh['intents'])
    sh2 = evaluate(sh['ladder'], 'short', 500.1, 3)
    ck('short exits when price closes back above the floor',
       sh2['intents'] and sh2['intents'][0]['kind'] == KIND_RATCHET, sh2['intents'])

    # ── voiding ──
    v = void_rungs(G, 'engine structural exit')
    ck('void_rungs retires every pending rung',
       all(r['void'] for r in v['rungs']) and evaluate(v, 'long', 999.0, 10)['intents'] == [])

    # ── validation ──
    def raises(fn, frag):
        try:
            fn()
        except ValueError as e:
            return frag in str(e)
        return False

    ck('rungs cannot oversell the position',
       raises(lambda: make_ladder(sleeve='shares', anchor=500.0, atr=2.0,
                                  position_qty=5,
                                  rungs=[{'qty': 4, 'at': {'kind': 'price', 'value': 510}},
                                         {'qty': 3, 'at': {'kind': 'price', 'value': 520}}]),
              'exceeds the position'))
    ck('an atr rung needs a pinned atr',
       raises(lambda: make_ladder(sleeve='shares', anchor=500.0, atr=None,
                                  position_qty=5,
                                  rungs=[{'qty': 1, 'at': {'kind': 'atr', 'value': 1}}]),
              'atr is required'))
    ck("remainder='ratchet' needs the ratchet on",
       raises(lambda: make_ladder(sleeve='shares', anchor=500.0, atr=2.0,
                                  position_qty=5, rungs=[], remainder='ratchet'),
              'requires ratchet.enabled'))
    ck('duplicate rung ids are rejected',
       raises(lambda: make_ladder(sleeve='shares', anchor=500.0, atr=2.0,
                                  position_qty=9,
                                  rungs=[{'id': 'x', 'qty': 1, 'at': {'kind': 'price', 'value': 510}},
                                         {'id': 'x', 'qty': 1, 'at': {'kind': 'price', 'value': 520}}]),
              'duplicate rung ids'))
    ck('a fractional-looking qty below one share is rejected',
       raises(lambda: make_ladder(sleeve='shares', anchor=500.0, atr=2.0,
                                  position_qty=5,
                                  rungs=[{'qty': 0, 'at': {'kind': 'price', 'value': 510}}]),
              'qty must be >= 1'))

    # ── the contract with trade_router.plan_exit ──
    # A pure module reaching into trade_router is deliberate here: the intents
    # this module emits are only correct if plan_exit turns them into orders of
    # the right size, and a partial exit is the one place that can silently
    # oversell. plan_exit is itself pure, so nothing is mocked.
    import trade_router as tr
    POS = {'direction': 'long', 'shares': 10,
           'option_contracts': 3, 'option_symbol': 'SPY260102C00500000'}

    full = tr.plan_exit(POS)
    ck('a full exit still closes both sleeves whole',
       sorted((l['kind'], l.get('qty') or l.get('contracts')) for l in full)
       == [('option', 3), ('shares', 10)], full)

    part = tr.plan_exit(POS, sleeve='shares', qty=4)
    ck('a partial share exit sells exactly the rung quantity',
       len(part) == 1 and part[0]['kind'] == 'shares' and part[0]['qty'] == 4
       and part[0]['side'] == 'sell', part)

    over = tr.plan_exit(POS, sleeve='shares', qty=99)
    ck('a partial cannot oversell what is held', over[0]['qty'] == 10, over)

    opt = tr.plan_exit(POS, sleeve='options', qty=2)
    ck('a partial option exit sells contracts, not shares',
       len(opt) == 1 and opt[0]['contracts'] == 2, opt)

    ck("a partial with sleeve='both' is refused, not guessed",
       raises(lambda: tr.plan_exit(POS, sleeve='both', qty=3),
              'must name one sleeve'))

    ck('a zero-or-negative partial is a no-op, not a full exit',
       tr.plan_exit(POS, sleeve='shares', qty=0) == []
       and tr.plan_exit(POS, sleeve='shares', qty=-5) == [])

    short = tr.plan_exit({'direction': 'short', 'shares': 8},
                         sleeve='shares', qty=3)
    ck('a partial on short shares BUYS to cover',
       short[0]['side'] == 'buy' and short[0]['qty'] == 3, short)

    print()
    if fails:
        print(f'SELFTEST FAILED: {len(fails)} case(s): {", ".join(fails)}')
        return False
    print('SELFTEST PASSED')
    return True


if __name__ == '__main__':
    import sys
    sys.exit(0 if selftest() else 1)
