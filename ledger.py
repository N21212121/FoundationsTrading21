"""
ledger.py — Foundations Trading

The accounting layer. Two jobs, kept apart on purpose:

  STORED   cash events that are not trades — broker fees, dividends,
           interest, transfers, and the expenses you type in yourself.
           These live in DATA_DIR/ledger.jsonl.

  DERIVED  trading P/L. Never stored. It is recomputed from the journal's
           paired legs every time a report is asked for, so correcting a
           fill in the Journal tab moves the accounting immediately and the
           two can never disagree.

SIGN CONVENTION
  Every amount is the signed cash impact. Money in is positive, money out is
  negative. A fee is negative, a dividend positive, rent negative. Alpaca's
  `net_amount` already follows this, so broker rows are stored as-is with no
  sign flipping to get wrong.

WHY FEES ARE PULLED, NOT DERIVED
  Differencing the account balance against the day's fills looks like it
  should yield fees. It does not. Equity also moves on unrealized P/L,
  deposits, withdrawals, dividends, interest and assignments, and the
  unrealized component alone dwarfs a few dollars of fees the moment a
  position is held overnight. The broker reports fees as their own records
  with exact amounts; those are used instead.

  Alpaca posts fees end of day, so today's fees usually appear tomorrow. A
  report covering today will understate fees until the next sync.
"""

import os
import json
import uuid
from collections import OrderedDict
from datetime import datetime, date, timedelta

import config_manager as cm
import journal as jn
import pairing


LEDGER_FILE = os.path.join(cm.DATA_DIR, 'ledger.jsonl')
_LOCK = cm._WRITE_LOCK

# Broker activity types grouped into the buckets a report cares about.
# Anything unrecognized falls into 'other' rather than being dropped, so a
# new Alpaca type shows up in the report instead of silently vanishing.
FEE_TYPES = {'FEE', 'CFEE', 'REG', 'TAF', 'PTC', 'PTR', 'OFEE'}
TRANSFER_TYPES = {'CSD', 'CSW', 'JNL', 'JNLC', 'JNLS', 'TRANS', 'ACATC', 'ACATS'}
INCOME_TYPES = {'DIV', 'DIVCGL', 'DIVCGS', 'DIVNRA', 'DIVROC', 'DIVTXEX',
                'INT', 'INTNRA', 'INTTW'}

KINDS = ('fee', 'expense', 'income', 'transfer', 'other')

DEFAULT_CATEGORIES = [
    'Platform / data', 'Software', 'Education', 'Hardware',
    'Office', 'Taxes', 'Professional services', 'Other',
]


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def classify(activity_type):
    t = (activity_type or '').upper()
    if t in FEE_TYPES:
        return 'fee'
    if t in TRANSFER_TYPES:
        return 'transfer'
    if t in INCOME_TYPES:
        return 'income'
    return 'other'


# ─── STORE ─────────────────────────────────────────────────────────────────────

def make_entry(*, date_, amount, kind='expense', category='', description='',
               source='manual', activity_id=None, meta=None):
    d = date_
    if isinstance(d, (datetime, date)):
        d = d.strftime('%Y-%m-%d')
    d = str(d)[:10]
    datetime.strptime(d, '%Y-%m-%d')      # raises if malformed

    if kind not in KINDS:
        raise ValueError(f'kind must be one of {KINDS}, got {kind!r}')

    return {
        'id': f'l_{uuid.uuid4().hex[:12]}',
        'date': d,
        'amount': round(float(amount), 4),
        'kind': kind,
        'category': category or '',
        'description': description or '',
        'source': source,
        'activity_id': activity_id,
        'logged_at': _now_iso(),
        'meta': dict(meta or {}),
    }


def append_many(entries):
    entries = list(entries)
    if not entries:
        return []
    with _LOCK:
        with open(LEDGER_FILE, 'a', encoding='utf-8') as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return entries


def add(**kwargs):
    e = make_entry(**kwargs)
    return append_many([e])[0]


def read_all(start=None, end=None, kind=None, source=None):
    if not os.path.exists(LEDGER_FILE):
        return []
    out = []
    with open(LEDGER_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if start and e.get('date', '') < start:
                continue
            if end and e.get('date', '') > end:
                continue
            if kind and e.get('kind') != kind:
                continue
            if source and e.get('source') != source:
                continue
            out.append(e)
    out.sort(key=lambda e: (e.get('date', ''), e.get('logged_at', '')))
    return out


def get(entry_id):
    for e in read_all():
        if e['id'] == entry_id:
            return e
    return None


def _rewrite(entries):
    tmp = LEDGER_FILE + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEDGER_FILE)


EDITABLE = ('date', 'amount', 'kind', 'category', 'description')


def update(entry_id, **changes):
    rows = read_all()
    hit = None
    patch = {k: v for k, v in changes.items() if k in EDITABLE}
    if 'kind' in patch and patch['kind'] not in KINDS:
        raise ValueError(f'kind must be one of {KINDS}')
    if 'amount' in patch:
        patch['amount'] = round(float(patch['amount']), 4)
    if 'date' in patch:
        d = str(patch['date'])[:10]
        datetime.strptime(d, '%Y-%m-%d')
        patch['date'] = d
    for e in rows:
        if e['id'] == entry_id:
            e.update(patch)
            e['edited_at'] = _now_iso()
            hit = e
            break
    if hit is None:
        return None
    _rewrite(rows)
    return hit


def delete(entry_id):
    rows = read_all()
    keep = [e for e in rows if e['id'] != entry_id]
    if len(keep) == len(rows):
        return None
    gone = next(e for e in rows if e['id'] == entry_id)
    _rewrite(keep)
    return gone


# ─── BROKER SYNC ───────────────────────────────────────────────────────────────

def sync_broker(alpaca, start=None, end=None, dry_run=False):
    """Pull fees, dividends, interest and transfers into the ledger.

    Deduped on Alpaca's activity id, so re-running over the same window adds
    nothing. Because fees post end of day, a window ending today will pick
    up the rest on the next run.
    """
    if not alpaca.is_connected():
        raise RuntimeError('Not connected to Alpaca.')

    after = jn.to_et(datetime.strptime(start, '%Y-%m-%d').replace(
        tzinfo=jn.ET)) if start else None
    until = (datetime.strptime(end, '%Y-%m-%d').replace(
        hour=23, minute=59, second=59, tzinfo=jn.ET)) if end else None

    rows = alpaca.get_nontrade_activities(after=after, until=until)

    seen = {e.get('activity_id') for e in read_all() if e.get('activity_id')}
    fresh, dupes = [], 0
    by_kind = {}
    for r in rows:
        if r['activity_id'] and r['activity_id'] in seen:
            dupes += 1
            continue
        kind = classify(r['activity_type'])
        by_kind[kind] = by_kind.get(kind, 0) + 1
        seen.add(r['activity_id'])
        fresh.append(make_entry(
            date_=r['date'], amount=r['net_amount'], kind=kind,
            category=r['activity_type'],
            description=r['description'] or r['activity_type'],
            source='alpaca', activity_id=r['activity_id'],
            meta={'symbol': r['symbol'], 'status': r['status']}))

    if not dry_run:
        append_many(fresh)

    return {'ok': True, 'fetched': len(rows), 'imported': len(fresh),
            'skipped_duplicates': dupes, 'by_kind': by_kind,
            'dry_run': dry_run, 'start': start, 'end': end,
            'account': 'paper' if alpaca.paper else 'live'}


# ─── REPORTING ─────────────────────────────────────────────────────────────────

def _realized_legs(legs=None):
    """Legs with P/L, tagged with the date the P/L was realized.

    A closed leg realizes on its exit; an expired one on the entry's expiry.
    Attributing to the entry date instead would push a loss into the month
    the position was opened, which is not when the money moved.
    """
    legs = legs if legs is not None else pairing.build_legs()
    out = []
    for l in legs:
        if l.get('pl') is None:
            continue
        when = l.get('exit_at')
        if not when:
            parts = jn.occ_parts(l['symbol'])
            when = parts[1].isoformat() if parts else l.get('entry_at')
        if not when:
            continue
        out.append({**l, 'realized_on': str(when)[:10]})
    return out


def report(start=None, end=None, legs=None):
    """One period's accounting. All figures are signed cash impact."""
    realized = [l for l in _realized_legs(legs)
                if (not start or l['realized_on'] >= start)
                and (not end or l['realized_on'] <= end)]
    entries = read_all(start=start, end=end)

    def total(kind):
        return round(sum(e['amount'] for e in entries
                         if e['kind'] == kind), 2)

    trading_pl = round(sum(l['pl'] for l in realized), 2)
    fees = total('fee')
    expenses = total('expense')
    income = total('income')
    transfers = total('transfer')
    other = total('other')

    # Scratches (P/L exactly zero) are neither wins nor losses, so they are
    # kept out of the win-rate denominator. Same rule as the Performance tab.
    wins = sum(1 for l in realized if l['pl'] > 0)
    losses = sum(1 for l in realized if l['pl'] < 0)
    scratches = sum(1 for l in realized if l['pl'] == 0)
    decided = wins + losses

    return {
        'start': start, 'end': end,
        'trading_pl': trading_pl,
        'fees': fees,
        'expenses': expenses,
        'income': income,
        'other': other,
        # Transfers move your own money in and out. They are reported but
        # deliberately excluded from net: counting a deposit as income would
        # make a losing month look profitable.
        'transfers': transfers,
        'net': round(trading_pl + fees + expenses + income + other, 2),
        'trades': len(realized),
        'wins': wins,
        'losses': losses,
        'scratches': scratches,
        'win_rate': round(wins / decided, 4) if decided else None,
        'gross_win': round(sum(l['pl'] for l in realized if l['pl'] > 0), 2),
        'gross_loss': round(sum(l['pl'] for l in realized if l['pl'] < 0), 2),
        'entry_count': len(entries),
    }


def by_month(year=None, legs=None):
    """One report per month. Defaults to every month with any activity."""
    legs = legs if legs is not None else pairing.build_legs()
    realized = _realized_legs(legs)
    months = {l['realized_on'][:7] for l in realized}
    months |= {e['date'][:7] for e in read_all()}
    if year:
        months = {m for m in months if m.startswith(str(year))}

    out = OrderedDict()
    for m in sorted(months):
        first = f'{m}-01'
        y, mo = int(m[:4]), int(m[5:7])
        nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
        last = (nxt - timedelta(days=1)).isoformat()
        out[m] = report(first, last, legs=legs)
    return out


def by_day(start=None, end=None, legs=None):
    """Realized trading P/L per calendar day, for the Journal calendar.

    Uses the same realization date as report(), so a month of calendar days
    sums to that month's trading_pl on the Ledger tab. Gross of fees, which
    Alpaca posts as their own end-of-day records.
    """
    out = {}
    for l in _realized_legs(legs):
        d = l['realized_on']
        if (start and d < start) or (end and d > end):
            continue
        day = out.setdefault(d, {'pl': 0.0, 'n': 0, 'wins': 0, 'losses': 0,
                                 'scratches': 0, 'legs': []})
        day['pl'] += l['pl']
        day['n'] += 1
        day['wins' if l['pl'] > 0 else 'losses' if l['pl'] < 0
            else 'scratches'] += 1
        day['legs'].append({k: l.get(k) for k in (
            'symbol', 'ticker', 'instrument', 'direction', 'outcome', 'qty',
            'entry_at', 'exit_at', 'entry_price', 'exit_price', 'pl',
            'pl_pct', 'hold_minutes', 'entry_grade', 'exit_grade')})
    for day in out.values():
        day['pl'] = round(day['pl'], 2)
        day['legs'].sort(key=lambda l: l.get('exit_at') or l.get('entry_at') or '')
    return dict(sorted(out.items()))


def ytd(today=None, legs=None):
    today = today or date.today()
    return report(f'{today.year}-01-01', today.isoformat(), legs=legs)


def categories():
    """Categories in use, plus the defaults, for the entry form."""
    used = {e['category'] for e in read_all()
            if e.get('category') and e.get('source') == 'manual'}
    return sorted(set(DEFAULT_CATEGORIES) | used)


def stats():
    rows = read_all()
    return {'entries': len(rows), 'file': LEDGER_FILE,
            'by_kind': {k: sum(1 for e in rows if e['kind'] == k)
                        for k in KINDS},
            'by_source': {s: sum(1 for e in rows if e.get('source') == s)
                          for s in ('alpaca', 'manual')}}
