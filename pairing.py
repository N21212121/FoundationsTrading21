"""
pairing.py — Foundations Trading

A CONSUMER of the journal noun. Turns a flat stream of fills into paired
entry/exit legs with P/L attached.

THE RULE
  FIFO by quantity, per symbol. The first contract in is the first contract
  out. A position opens on the first fill in a symbol, and the side of that
  first fill decides whether the position is long (opened by a buy) or short
  (opened by a sell). Everything on the opening side scales in; everything on
  the opposite side closes.

WHY LEGS AND NOT POSITIONS
  A position built with two buys and closed with one sell produces TWO legs,
  not one. Each leg carries its own entry grade and shares the closing fill's
  exit grade. This is deliberate: the entry decisions were separate decisions
  and deserve separate rows in the heatmap. A single blended "position" would
  average away exactly the thing the grid is meant to expose.

UNCLOSED ENTRIES
  An entry with no matching exit is still a leg — role 'entry', exit None.
  For options past expiry it is booked at the full premium (lost if long,
  kept if short). For anything not yet expired, P/L is None and the leg is
  excluded from money math while still appearing in counts.

VALIDATION
  Realized P/L plus open cash flow must equal the raw sum of every fill's
  signed cash. reconcile() asserts this. If it ever fails, the pairing is
  creating or destroying money and the output should not be trusted.
"""

import math
from datetime import datetime

import journal as jn


EPS = 1e-6


def _dt(rec):
    return datetime.fromisoformat(rec['filled_at'])


def _cash(qty, price, mult):
    return round(qty * price * mult, 4)


# ─── PAIRING ───────────────────────────────────────────────────────────────────

def build_legs(records=None, now=None):
    """Pair fills into legs. Returns a list of leg dicts.

    records defaults to the whole journal. now defaults to the current time
    and is only used to decide whether an unclosed option has expired.
    """
    if records is None:
        records = jn.read_all()
    now = now or datetime.now()

    by_symbol = {}
    for r in records:
        by_symbol.setdefault(r['symbol'].strip(), []).append(r)

    legs = []
    for symbol, fills in by_symbol.items():
        fills.sort(key=lambda r: (_dt(r), r.get('logged_at') or ''))
        legs.extend(_pair_symbol(symbol, fills, now))

    legs.sort(key=lambda l: l['entry_at'])
    return legs


def _pair_symbol(symbol, fills, now):
    out = []
    pending = []          # [{'rec':..., 'left': qty}] — all on the opening side
    position = None       # 'long' | 'short'

    for r in fills:
        opening_side = 'buy' if position == 'long' else (
            'sell' if position == 'short' else None)

        if not pending:
            position = 'long' if r['side'] == 'buy' else 'short'
            pending.append({'rec': r, 'left': r['qty']})
            continue

        if r['side'] == opening_side:
            pending.append({'rec': r, 'left': r['qty']})
            continue

        # closing side: consume pending entries FIFO
        left = r['qty']
        while left > EPS and pending:
            e = pending[0]
            take = min(e['left'], left)
            out.append(_mk_leg(symbol, e['rec'], r, take, position))
            e['left'] -= take
            left -= take
            if e['left'] <= EPS:
                pending.pop(0)

        if not pending:
            position = None

        # An exit larger than everything open flips into a new position on
        # the opposite side rather than being silently dropped.
        if left > EPS:
            position = 'short' if r['side'] == 'sell' else 'long'
            pending.append({'rec': r, 'left': left})

    for e in pending:
        out.append(_mk_open_leg(symbol, e['rec'], e['left'], position, now))
    return out


def _mk_leg(symbol, entry, exit_, qty, position):
    mult = entry.get('multiplier', 1)
    e_cash = _cash(qty, entry['price'], mult)
    x_cash = _cash(qty, exit_['price'], mult)
    # long  : profit = proceeds (exit) - cost (entry)
    # short : profit = proceeds (entry) - cost (exit to cover)
    pl = (x_cash - e_cash) if position == 'long' else (e_cash - x_cash)

    return {
        'symbol': symbol,
        'ticker': entry['ticker'],
        'instrument': entry.get('instrument'),
        'direction': position,
        'outcome': 'closed',
        'qty': round(qty, 6),
        'entry_id': entry['id'], 'exit_id': exit_['id'],
        'entry_at': entry['filled_at'], 'exit_at': exit_['filled_at'],
        'entry_price': entry['price'], 'exit_price': exit_['price'],
        'entry_cash': e_cash, 'exit_cash': x_cash,
        'pl': round(pl, 2),
        # Unrounded, for reconciliation only. Rounding 199 legs to the cent
        # accumulates about a cent of drift, which would otherwise look like
        # a pairing bug. Display uses 'pl'; arithmetic uses this.
        'pl_exact': pl,
        'pl_pct': round(pl / e_cash, 6) if e_cash else None,
        'hold_minutes': round(
            (_dt(exit_) - _dt(entry)).total_seconds() / 60, 1),
        'same_day': _dt(exit_).date() == _dt(entry).date(),
        'entry_grade': entry.get('grade'),
        'exit_grade': exit_.get('grade'),
        'entry_conditions': entry.get('conditions') or {},
        'exit_conditions': exit_.get('conditions') or {},
        'entry_tags_good': entry.get('tags_good') or [],
        'entry_tags_bad': entry.get('tags_bad') or [],
        'exit_tags_good': exit_.get('tags_good') or [],
        'exit_tags_bad': exit_.get('tags_bad') or [],
        'entry_note': entry.get('note', ''), 'exit_note': exit_.get('note', ''),
        'source': entry.get('source'),
    }


def _mk_open_leg(symbol, entry, qty, position, now):
    mult = entry.get('multiplier', 1)
    e_cash = _cash(qty, entry['price'], mult)
    parts = jn.occ_parts(symbol)

    if parts is not None and parts[1] <= now:
        outcome = 'expired'
        # long loses the premium paid; short keeps the premium received
        pl = -e_cash if position == 'long' else e_cash
    elif parts is not None:
        outcome, pl = 'open', None
    else:
        outcome, pl = 'open', None

    return {
        'symbol': symbol,
        'ticker': entry['ticker'],
        'instrument': entry.get('instrument'),
        'direction': position,
        'outcome': outcome,
        'qty': round(qty, 6),
        'entry_id': entry['id'], 'exit_id': None,
        'entry_at': entry['filled_at'], 'exit_at': None,
        'entry_price': entry['price'], 'exit_price': None,
        'entry_cash': e_cash, 'exit_cash': None,
        'pl': round(pl, 2) if pl is not None else None,
        'pl_exact': pl,
        'pl_pct': (round(pl / e_cash, 6) if pl is not None and e_cash else None),
        'hold_minutes': None,
        'same_day': None,
        'entry_grade': entry.get('grade'),
        'exit_grade': 'none',        # the sixth column of the grid
        'entry_conditions': entry.get('conditions') or {},
        'exit_conditions': {},
        'entry_tags_good': entry.get('tags_good') or [],
        'entry_tags_bad': entry.get('tags_bad') or [],
        'exit_tags_good': [], 'exit_tags_bad': [],
        'entry_note': entry.get('note', ''), 'exit_note': '',
        'source': entry.get('source'),
    }


# ─── VALIDATION ────────────────────────────────────────────────────────────────

def reconcile(records=None, legs=None, now=None):
    """Prove the pairing neither created nor destroyed money.

    realized P/L + open cash flow must equal the signed sum of every fill.
    Returns a dict; 'ok' is False if they disagree by more than a cent.
    """
    if records is None:
        records = jn.read_all()
    if legs is None:
        legs = build_legs(records, now=now)

    raw = sum((r['cash'] if r['side'] == 'sell' else -r['cash'])
              for r in records)

    realized = sum(l['pl_exact'] for l in legs if l.get('pl_exact') is not None)
    open_cash = 0.0
    for l in legs:
        if l.get('pl_exact') is None:
            open_cash += (-l['entry_cash'] if l['direction'] == 'long'
                          else l['entry_cash'])

    diff = (realized + open_cash) - raw
    return {
        'ok': abs(diff) < 0.01,
        'raw_net': round(raw, 2),
        'realized_pl': round(realized, 2),
        'open_cash_flow': round(open_cash, 2),
        'difference': round(diff, 4),
        'legs': len(legs),
        'fills': len(records),
    }



# ─── DECISION ROWS ─────────────────────────────────────────────────────────────
#
# WHY THIS EXISTS
#   A leg is one entry fill matched to one exit fill. That is the right unit
#   when several ENTRIES share an exit -- two buys closed by one sell were two
#   separate decisions and deserve two rows, which is what the module docstring
#   argues above and what the heatmap was built on.
#
#   The exit ladder creates the mirror case, and the mirror case is different.
#   ONE entry closed by three rungs is ONE decision, closed in instalments.
#   Left as legs it paints three cells of the grid, inflates every count
#   labelled 'trades', and weights that single decision three times as heavily
#   as an unladdered one. Grouping by entry_id fixes exactly that and nothing
#   else: the multi-entry case is untouched, because those legs carry
#   different entry_ids and stay separate rows.
#
# THE GRADE
#   Each exit keeps its own integer grade -- journal.set_grade enforces [0, 4].
#   Only the collapse produces a fraction, as a QUANTITY-WEIGHTED mean: closing
#   8 of 10 shares well and 2 badly is mostly a good exit, and weighting by
#   quantity is what says so. Nate's rounding rule (2026-09-28) then puts it on
#   the integer grid: a fraction of .60 or more earns the next grade up,
#   anything less does not. That is deliberately stricter than round-half-up --
#   a 2.5 is a 2, not a 3 -- so a grade has to be clearly earned.
#
#   exit_grade_exact keeps the unrounded mean. Trade averages and anything
#   reading a single trade should use it; only the grid rounds.

GRADE_ROUND_UP_AT = 0.60

# HOW MUCH OF A POSITION MUST BE GRADED BEFORE THE GRID WILL PRINT A GRADE.
# Settled 2026-09-29. The case that forced it: an AMC entry of 10 contracts
# where 1 was sold and graded 2 and the other 9 expired worthless with no exit
# at all. The weighted mean happily returned 2.0 -- a grade resting on a tenth
# of the position, standing for the whole decision, indistinguishable in the
# grid from a trade whose entire exit was graded 2.
#
# A majority is the only non-arbitrary bar available, and it is the same claim
# the collapse makes elsewhere: conditions come from the largest rung because
# size is what earns the right to speak for the row. Below the bar the grid
# reads 'none' -- not a downgrade, an abstention. `exit_grade_exact`,
# `exit_grade_qty` and `exit_grade_coverage` still carry the full picture, so
# nothing is destroyed and the rule is invertible. It also self-heals: close
# and grade more of the position and the grade appears.
GRADE_COVERAGE_MIN = 0.50


def round_grade(value):
    """Nate's rule: round up from .60, down below it. None passes through.

    Not round-half-up. 2.59 -> 2, 2.60 -> 3. Grades are non-negative so the
    floor is unambiguous; EPS absorbs the float representation of .60, which
    lands a hair under it.
    """
    if value is None:
        return None
    whole = math.floor(value)
    return whole + 1 if (value - whole) >= GRADE_ROUND_UP_AT - EPS else whole


def _is_grade(g):
    """True only for a real numeric grade. 'none', None and bools are not
    grades -- a bool would otherwise read as 0 or 1."""
    return isinstance(g, (int, float)) and not isinstance(g, bool)


def _weighted_exit_grade(legs):
    """Quantity-weighted mean of the exit grades present. None if there are
    none -- an entry still fully open, or one whose exits are all ungraded.

    Weights are leg quantities, so a rung's influence is the size it closed.
    This is the mean of what WAS graded and says nothing about how much of the
    position that was; `_graded_exit_qty` answers that, and the two are read
    together in `collapse_to_entries`.
    """
    num = den = 0.0
    for l in legs:
        g, q = l.get('exit_grade'), l.get('qty')
        if not _is_grade(g):
            continue                      # 'none', None, or anything odd
        if not q or q <= 0:
            continue
        num += g * q
        den += q
    return (num / den) if den else None


def _graded_exit_qty(legs):
    """How much quantity carries a graded exit. The denominator of coverage."""
    return sum((l.get('qty') or 0) for l in legs
               if _is_grade(l.get('exit_grade')) and (l.get('qty') or 0) > 0)


def collapse_to_entries(legs=None):
    """Group legs into one row per entry decision, FIFO order preserved.

    Returns rows shaped like legs, plus:
      n_legs            how many exits closed this entry (1 for everything
                        that predates the ladder)
      qty_closed        quantity actually closed, which is < qty while a
                        ladder still has rungs pending
      exit_grade_exact  the unrounded quantity-weighted mean
      exit_grade_qty    how much quantity actually carries a graded exit
      exit_grade_coverage  that as a fraction of the whole position
      exit_grade        that mean under round_grade, or 'none' -- the sixth
                        column of the grid. It reads 'none' both when nothing
                        is graded yet AND when what is graded covers less
                        than GRADE_COVERAGE_MIN of the position: a grade on a
                        minority of the size does not get to speak for the
                        decision. `exit_grade_exact` is unconditional, so the
                        sliver's own grade is still there to read.

    WHAT AN EXIT-SIDE FILTER MEANS AFTER A COLLAPSE
      A ladder's rungs can close under different conditions, so there is no
      single honest answer. Conditions are taken from the LARGEST exit by
      quantity -- the one that dominates the weighted grade -- while tags are
      UNIONED, because a tag records a thing that happened and it did happen.
      For a single-exit trade both rules are identities, which is almost all
      of the history -- but NOT all of it. See below.

    THE HISTORY IS NOT INERT
      Four entries in the journal as of 2026-09-28 were already closed in
      instalments by hand, long before the ladder existed: two AMC options
      (2026-07-21) and two MUU stock (2026-08-28, 2026-09-09). They account
      for 6 absorbed legs, 208 -> 202 rows. Three collapse without moving a
      grade; MUU 2026-08-28 held legs graded 2 and 4 at equal size and now
      grids at 3, a cell it was never in before, and MUU 2026-09-09 weights
      1/2/3/4 into 2.2 and grids at 2.

      This is the collapse working, not failing -- those were single decisions
      all along. But it means the grid is not bit-identical to what it showed
      yesterday, and a saved screenshot of the old grid will not reconcile.
    """
    if legs is None:
        legs = build_legs()

    order, groups = [], {}
    for l in legs:
        k = l.get('entry_id')
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(l)

    rows = []
    for k in order:
        group = groups[k]
        base = dict(group[0])
        priced = [l for l in group if l.get('pl_exact') is not None]
        closed = [l for l in group if l.get('exit_id') is not None]

        qty_total = sum(l.get('qty') or 0 for l in group)
        qty_closed = sum(l.get('qty') or 0 for l in closed)
        entry_cash_priced = sum(l.get('entry_cash') or 0 for l in priced)
        pl_exact = sum(l['pl_exact'] for l in priced) if priced else None

        # Hold time is quantity-weighted for the same reason the grade is: the
        # rung that closed most of the position should dominate the number.
        hold_num = hold_den = 0.0
        for l in closed:
            h, q = l.get('hold_minutes'), l.get('qty')
            if h is not None and q:
                hold_num += h * q
                hold_den += q

        biggest = (max(closed, key=lambda l: l.get('qty') or 0)
                   if closed else None)
        exact = _weighted_exit_grade(group)
        graded_qty = _graded_exit_qty(group)
        coverage = (graded_qty / qty_total) if qty_total else 0.0
        grade_stands = (exact is not None
                        and coverage >= GRADE_COVERAGE_MIN - EPS)

        base.update({
            'n_legs': len(group),
            'qty': round(qty_total, 6),
            'qty_closed': round(qty_closed, 6),
            'entry_cash': round(sum(l.get('entry_cash') or 0
                                    for l in group), 4),
            'exit_cash': (round(sum(l.get('exit_cash') or 0
                                    for l in closed), 4) if closed else None),
            'pl_exact': pl_exact,
            'pl': round(pl_exact, 2) if pl_exact is not None else None,
            # Against the entry cash of the PRICED legs only, so a half-closed
            # ladder is not diluted by the part still open.
            'pl_pct': (round(pl_exact / entry_cash_priced, 6)
                       if pl_exact is not None and entry_cash_priced else None),
            'hold_minutes': (round(hold_num / hold_den, 1)
                             if hold_den else None),
            'exit_at': max((l['exit_at'] for l in closed if l.get('exit_at')),
                           default=None),
            'exit_price': biggest.get('exit_price') if biggest else None,
            'exit_id': biggest.get('exit_id') if biggest else None,
            'outcome': ('closed' if closed and len(closed) == len(group)
                        else group[0].get('outcome') if not closed
                        else 'partial'),
            'exit_grade_exact': exact,
            'exit_grade_qty': round(graded_qty, 6),
            'exit_grade_coverage': round(coverage, 6),
            'exit_grade': round_grade(exact) if grade_stands else 'none',
            'exit_conditions': ((biggest.get('exit_conditions') or {})
                                if biggest else {}),
            'exit_tags_good': sorted({t for l in group
                                      for t in (l.get('exit_tags_good') or [])}),
            'exit_tags_bad': sorted({t for l in group
                                     for t in (l.get('exit_tags_bad') or [])}),
            'exit_note': ' | '.join(l['exit_note'] for l in group
                                    if l.get('exit_note')),
        })
        base['same_day'] = (all(l.get('same_day') for l in closed)
                            if closed else None)
        rows.append(base)

    return rows


# ─── SELFTEST ──────────────────────────────────────────────────────────────────

def selftest():
    """py -3 pairing.py --selftest"""
    fails = []

    def ck(name, cond, extra=''):
        if cond:
            print(f'  ok   {name}')
        else:
            print(f'  FAIL {name} {extra}')
            fails.append(name)

    # -- round_grade: the .60 boundary --
    ck('a .5 does not earn the next grade', round_grade(2.5) == 2)
    ck('a .59 does not earn it either', round_grade(2.59) == 2)
    ck('exactly .60 earns it', round_grade(2.60) == 3)
    ck('a .61 earns it', round_grade(2.61) == 3)
    ck('a whole grade is unchanged', round_grade(3.0) == 3)
    ck('zero stays zero', round_grade(0.0) == 0)
    ck('a top grade cannot be pushed past 4', round_grade(4.0) == 4)
    ck('None passes through', round_grade(None) is None)
    ck('.60 survives its own float representation',
       round_grade(0.6) == 1 and round_grade(1.6) == 2)

    # -- the weighted mean --
    W = [{'exit_grade': 4, 'qty': 4}, {'exit_grade': 2, 'qty': 3},
         {'exit_grade': 1, 'qty': 3}]
    ck('quantity weights the mean, not leg count',
       abs(_weighted_exit_grade(W) - 2.5) < 1e-9, _weighted_exit_grade(W))
    ck('one leg is its own mean',
       _weighted_exit_grade([{'exit_grade': 3, 'qty': 10}]) == 3)
    ck("a 'none' exit is skipped, not read as zero",
       _weighted_exit_grade([{'exit_grade': 4, 'qty': 5},
                             {'exit_grade': 'none', 'qty': 5}]) == 4)
    ck('an all-open entry has no exit grade',
       _weighted_exit_grade([{'exit_grade': 'none', 'qty': 5}]) is None)
    ck('a zero-qty leg cannot vote',
       _weighted_exit_grade([{'exit_grade': 4, 'qty': 0},
                             {'exit_grade': 1, 'qty': 5}]) == 1)

    # -- graded coverage --
    ck('coverage counts graded quantity only',
       _graded_exit_qty(W) == 10 and _graded_exit_qty(
           [{'exit_grade': 4, 'qty': 1},
            {'exit_grade': 'none', 'qty': 9}]) == 1)
    ck('a grade of 0 still counts as covered',
       _graded_exit_qty([{'exit_grade': 0, 'qty': 5}]) == 5)
    ck('a bool is not a grade',
       _graded_exit_qty([{'exit_grade': True, 'qty': 5}]) == 0)

    # -- the collapse --
    def leg(eid, xid, qty, xg, pl, hold=10.0, eg=3):
        return {'symbol': 'SPY', 'ticker': 'SPY', 'entry_id': eid,
                'exit_id': xid, 'qty': qty, 'entry_grade': eg,
                'exit_grade': xg, 'pl': round(pl, 2), 'pl_exact': pl,
                'entry_cash': qty * 100.0, 'exit_cash': qty * 100.0 + pl,
                'hold_minutes': hold, 'same_day': True, 'outcome': 'closed',
                'entry_at': '2026-09-28T10:00:00',
                'exit_at': '2026-09-28T1{}:00:00'.format(xid),
                'exit_price': 100.0, 'exit_conditions': {'w': xid},
                'exit_tags_good': ['g{}'.format(xid)], 'exit_tags_bad': [],
                'exit_note': '', 'entry_conditions': {}, 'entry_tags_good': [],
                'entry_tags_bad': [], 'entry_note': ''}

    ladder = [leg('E1', '1', 4, 4, 40.0, hold=10),
              leg('E1', '2', 3, 2, -9.0, hold=20),
              leg('E1', '3', 3, 1, -6.0, hold=30)]
    rows = collapse_to_entries(ladder)
    ck('three rungs become one decision row', len(rows) == 1)
    r = rows[0]
    ck('the collapsed row keeps the exact weighted grade',
       abs(r['exit_grade_exact'] - 2.5) < 1e-9, r['exit_grade_exact'])
    ck('and grids at 2, because .5 is under the bar', r['exit_grade'] == 2)
    ck('P/L is the sum of the rungs', r['pl'] == 25.0, r['pl'])
    ck('quantity is the whole position', r['qty'] == 10)
    ck('n_legs records the instalments', r['n_legs'] == 3)
    ck('hold time is quantity-weighted',
       abs(r['hold_minutes'] - 19.0) < 1e-9, r['hold_minutes'])
    ck('conditions come from the largest rung',
       r['exit_conditions'] == {'w': '1'}, r['exit_conditions'])
    ck('tags are unioned across rungs',
       r['exit_tags_good'] == ['g1', 'g2', 'g3'], r['exit_tags_good'])
    ck('the entry grade is untouched', r['entry_grade'] == 3)

    # -- the cases that must NOT change --
    plain = [leg('E9', '1', 10, 3, 50.0)]
    p = collapse_to_entries(plain)[0]
    ck('an unladdered trade is unchanged by the collapse',
       len(collapse_to_entries(plain)) == 1 and p['exit_grade'] == 3
       and p['pl'] == 50.0 and p['n_legs'] == 1)
    ck('its exact grade is the integer it always was',
       p['exit_grade_exact'] == 3.0)

    two_entries = [leg('E1', '1', 5, 4, 20.0, eg=4),
                   leg('E2', '1', 5, 4, 20.0, eg=1)]
    ck('two entries closed by one sell stay two rows',
       len(collapse_to_entries(two_entries)) == 2)
    ck('and keep their own entry grades',
       [x['entry_grade'] for x in collapse_to_entries(two_entries)] == [4, 1])

    # -- partial ladders --
    open_leg = leg('E1', None, 6, 'none', 0.0)
    open_leg.update({'exit_id': None, 'pl': None, 'pl_exact': None,
                     'exit_cash': None, 'hold_minutes': None,
                     'outcome': 'open', 'exit_at': None})
    half = [leg('E1', '1', 4, 4, 40.0), open_leg]
    h = collapse_to_entries(half)[0]
    ck('a half-closed ladder is one row', len(collapse_to_entries(half)) == 1)
    # CHANGED 2026-09-29. This used to read 4: the one fired rung's grade
    # stood for the whole decision. 4 of 10 is a minority, so the grid now
    # abstains -- while exit_grade_exact still reports the rung's own 4.
    ck('a rung covering a minority of the position does not grade the row',
       h['exit_grade'] == 'none', h['exit_grade'])
    ck('but the fired rung keeps its exact grade',
       h['exit_grade_exact'] == 4.0 and h['exit_grade_coverage'] == 0.4,
       (h['exit_grade_exact'], h['exit_grade_coverage']))
    ck('qty_closed is less than qty while rungs are pending',
       h['qty_closed'] == 4 and h['qty'] == 10)
    ck("its outcome reads 'partial'", h['outcome'] == 'partial', h['outcome'])
    ck('P/L counts the closed part only', h['pl'] == 40.0)
    ck('pl_pct is not diluted by the open remainder',
       abs(h['pl_pct'] - 0.1) < 1e-9, h['pl_pct'])

    def open_rest(eid, qty):
        o = leg(eid, None, qty, 'none', 0.0)
        o.update({'exit_id': None, 'pl': None, 'pl_exact': None,
                  'exit_cash': None, 'hold_minutes': None,
                  'outcome': 'open', 'exit_at': None})
        return o

    exact_half = collapse_to_entries(
        [leg('E1', '1', 5, 4, 50.0), open_rest('E1', 5)])[0]
    ck('exactly half the position is enough -- a majority bar includes .50',
       exact_half['exit_grade'] == 4, exact_half['exit_grade'])

    # The AMC shape: 1 of 10 contracts sold and graded, 9 expired worthless
    # with no exit record, so they read as open forever.
    amc = collapse_to_entries(
        [leg('E1', '1', 1, 2, -5.0), open_rest('E1', 9)])[0]
    ck('a tenth of the position cannot grade the decision',
       amc['exit_grade'] == 'none', amc['exit_grade'])
    ck('its coverage says why', amc['exit_grade_coverage'] == 0.1,
       amc['exit_grade_coverage'])
    ck('and the sliver keeps its own grade on the row',
       amc['exit_grade_exact'] == 2.0 and amc['exit_grade_qty'] == 1)

    # Fully closed, but most of what closed was never graded. Coverage is
    # about the POSITION, not about how much happened to be gradeable.
    ungraded_bulk = collapse_to_entries(
        [leg('E1', '1', 2, 4, 20.0), leg('E1', '2', 8, 'none', -8.0)])[0]
    ck('a closed trade mostly ungraded also abstains',
       ungraded_bulk['exit_grade'] == 'none', ungraded_bulk['exit_grade'])
    ck('its P/L is unaffected by the abstention',
       ungraded_bulk['pl'] == 12.0, ungraded_bulk['pl'])

    full = collapse_to_entries([leg('E1', '1', 10, 3, 50.0)])[0]
    ck('a fully graded exit reads 1.0 coverage and keeps its grade',
       full['exit_grade_coverage'] == 1.0 and full['exit_grade'] == 3)

    wide = [leg('E1', '1', 9, 3, 10.0), leg('E1', '2', 1, 0, -1.0)]
    w = collapse_to_entries(wide)[0]
    ck('a small bad rung cannot drag down a mostly-good exit',
       abs(w['exit_grade_exact'] - 2.7) < 1e-9 and w['exit_grade'] == 3,
       w['exit_grade_exact'])

    print()
    if fails:
        print('SELFTEST FAILED: {} case(s): {}'.format(
            len(fails), ', '.join(fails)))
        return False
    print('SELFTEST PASSED')
    return True


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
    import pprint
    pprint.pprint(reconcile())
