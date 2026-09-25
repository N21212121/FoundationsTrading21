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


if __name__ == '__main__':
    import pprint
    pprint.pprint(reconcile())
