"""
journal_pairdetail.py — Foundations Trading

Imports the Pair Detail export (the paired entry/exit legs) back into the
journal as FILLS.

WHY THIS IS NOT A STRAIGHT READ
  Pair Detail is one row per LEG, not per fill. A single sell closing two
  separate buys appears as two rows sharing one exit. Reading those rows as
  fills would book that sell twice and the P/L would stop reconciling.

  Every row carries `Entry Row` and `Exit Row`: the line numbers of the two
  fills in the original Trade Log sheet. Those are stable identities, so the
  reconstruction keys each fill by its source row and merges every leg that
  references it. Quantities and cash sum; grade, conditions, tags and notes
  are identical across a row's legs by construction.

  One row can be both. A sell that closes a long and opens a short shows up
  as the exit of one leg and the entry of the next. Keying by row number
  collapses those into the one fill they always were, and summing across both
  roles recovers its true quantity.

WHAT IS LOST VERSUS THE TRADE LOG
  Timestamps are minute-truncated here; the Trade Log has seconds. Exit-side
  1H and 1D conditions are read if the export has Exit 1H / Exit 1D columns,
  but older Pair Detail exports do not carry them at all. If you still have
  the Trade Log export, prefer it. This exists so a Pair Detail file is not a
  dead end.
"""

import csv
import io
import re
from datetime import datetime

import journal as jn


REQUIRED = {'Asset', 'Entry Grade', 'Exit Grade', 'Entry Row',
            'Entry Date/Time', 'Qty'}

ENTRY_CONDITIONS = {
    'Entry S/R/P': 'srp', 'Entry 5/12': 'ema_5_12',
    'Entry 34/50': 'ema_34_50', 'Entry 1H': 'mtf_1h', 'Entry 1D': 'mtf_1d',
}
# Exit 1H / Exit 1D are read when present. Older Pair Detail exports omit
# them, in which case exit-side MTF simply has no values to filter on and the
# Trade Log export is the only source that carries them.
EXIT_CONDITIONS = {
    'Exit S/R/P': 'srp', 'Exit 5/12': 'ema_5_12', 'Exit 34/50': 'ema_34_50',
    'Exit 1H': 'mtf_1h', 'Exit 1D': 'mtf_1d',
}


def looks_like(text):
    """True if this content is a Pair Detail export."""
    try:
        head = next(csv.reader(io.StringIO(text)))
    except StopIteration:
        return False
    return REQUIRED.issubset({h.strip() for h in head})


def _money(x):
    """'$1,234.56' / '($4.07)' -> float."""
    if x is None:
        return None
    s = str(x).strip()
    if not s or s in ('-', '—'):
        return None
    neg = s.startswith('(') and s.endswith(')')
    s = re.sub(r'[^0-9.\-]', '', s)
    if s in ('', '-', '.'):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _num(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def _dt(x):
    s = str(x or '').strip()
    for f in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
              '%m/%d/%Y %H:%M', '%m/%d/%Y %I:%M %p'):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _tags(x):
    if not x or not str(x).strip():
        return []
    parts = re.findall(r'"[^"]*"|[^,]+', str(x))
    out = [p.strip().strip('"').strip() for p in parts]
    return [p for p in out if p and p.lower() != 'nothing']


def _grade(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def parse_text(text):
    """Reconstruct fills from a Pair Detail export. Writes nothing.

    Returns (records, problems).
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [], [{'row': 0, 'issue': 'no rows in file'}]

    fills = {}      # source row number -> accumulating fill
    problems = []

    def contribute(src_row, *, symbol, side, qty, cash, when, grade,
                   conditions, good, bad, note, leg_no):
        if not src_row:
            return
        f = fills.get(src_row)
        if f is None:
            fills[src_row] = {
                'symbol': symbol, 'side': side, 'qty': qty or 0.0,
                'cash': cash or 0.0, 'when': when, 'grade': grade,
                'conditions': dict(conditions), 'good': list(good),
                'bad': list(bad), 'note': note, 'legs': [leg_no],
            }
            return
        # Merge a second leg that references this same fill.
        if f['side'] != side:
            problems.append({'row': leg_no, 'issue':
                             f'source row {src_row} claims both {f["side"]} '
                             f'and {side}; kept {f["side"]}'})
        if f['grade'] != grade:
            problems.append({'row': leg_no, 'issue':
                             f'source row {src_row} has conflicting grades '
                             f'{f["grade"]} and {grade}; kept {f["grade"]}'})
        f['qty'] += qty or 0.0
        f['cash'] += cash or 0.0
        f['legs'].append(leg_no)

    for i, r in enumerate(rows, start=2):
        asset = (r.get('Asset') or '').strip()
        qty = _num(r.get('Qty'))
        direction = (r.get('Direction') or 'Long').strip().lower()
        if not asset or not qty:
            problems.append({'row': i, 'issue': 'missing Asset or Qty'})
            continue

        long_ = direction != 'short'
        entry_side, exit_side = ('buy', 'sell') if long_ else ('sell', 'buy')

        e_when = _dt(r.get('Entry Date/Time'))
        if e_when is None:
            problems.append({'row': i, 'issue':
                             f"unreadable Entry Date/Time "
                             f"{r.get('Entry Date/Time')!r}"})
            continue

        contribute(
            (r.get('Entry Row') or '').strip(),
            symbol=asset, side=entry_side, qty=qty,
            cash=_money(r.get('Entry $')), when=e_when,
            grade=_grade(r.get('Entry Grade')),
            conditions={v: (r.get(k) or '').strip()
                        for k, v in ENTRY_CONDITIONS.items()
                        if (r.get(k) or '').strip()},
            good=_tags(r.get('Entry Good Tags')),
            bad=_tags(r.get('Entry Bad Tags')),
            note=(r.get('Entry Notes') or '').strip(), leg_no=i)

        x_row = (r.get('Exit Row') or '').strip()
        x_when = _dt(r.get('Exit Date/Time'))
        if x_row and x_when is not None:
            contribute(
                x_row, symbol=asset, side=exit_side, qty=qty,
                cash=_money(r.get('Exit $')), when=x_when,
                grade=_grade(r.get('Exit Grade')),
                conditions={v: (r.get(k) or '').strip()
                            for k, v in EXIT_CONDITIONS.items()
                            if (r.get(k) or '').strip()},
                good=_tags(r.get('Exit Good Tags')),
                bad=_tags(r.get('Exit Bad Tags')),
                note=(r.get('Exit Notes') or '').strip(), leg_no=i)

    records = []
    for src_row, f in sorted(fills.items(), key=lambda kv: kv[1]['when']):
        mult = 100 if jn.is_occ(f['symbol']) else 1
        if not f['qty']:
            problems.append({'row': src_row, 'issue': 'zero quantity'})
            continue
        price = (f['cash'] / (f['qty'] * mult)) if f['cash'] else 0.0
        try:
            records.append(jn.make_record(
                ticker=(f['symbol'][:-15] if jn.is_occ(f['symbol'])
                        else f['symbol']),
                symbol=f['symbol'], side=f['side'], qty=f['qty'],
                price=round(price, 6), filled_at=f['when'], source='import',
                conditions=f['conditions'], grade=f['grade'],
                tags_good=f['good'], tags_bad=f['bad'], note=f['note'],
                meta={'src_row': int(src_row) if str(src_row).isdigit()
                      else src_row,
                      'via': 'pair_detail', 'legs': f['legs']}))
        except ValueError as e:
            problems.append({'row': src_row, 'issue': str(e)})

    return records, problems
