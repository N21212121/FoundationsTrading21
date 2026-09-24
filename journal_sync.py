"""
journal_sync.py — Foundations Trading

Two ways to get fills into the journal without typing them:

  sync_alpaca()  pulls FILL activities straight off the account.
  parse_paste()  reads whatever you copied out of a broker page or sheet.

Grading stays manual in both cases. These land ungraded, with conditions
blank, and show up in the Journal queue.

DEDUPING
  Alpaca fills carry a stable activity id, stored at meta.activity_id. A
  second sync over the same window adds nothing. Partial fills of one order
  arrive as separate activities with separate ids, and are kept separate —
  they are separate fills, and the pairing layer already splits scale-ins
  into their own legs.

  Pasted rows have no such id, so they dedupe on the same fingerprint the CSV
  importer uses: symbol, timestamp to the second, side, qty, price. Paste the
  same block twice and the second one is a no-op. Paste two genuinely
  identical fills in the same second and you will get one -- rare enough to
  accept, and the manual-entry form is the way around it.
"""

import csv
import io
import re
from datetime import datetime, timedelta, timezone

import journal as jn


# ─── ALPACA SYNC ───────────────────────────────────────────────────────────────

def _existing_activity_ids():
    return {(r.get('meta') or {}).get('activity_id')
            for r in jn.read_all()
            if (r.get('meta') or {}).get('activity_id')}


def _fingerprints():
    return {(r['symbol'].strip().upper(), str(r['filled_at'])[:19],
             r['side'], round(float(r['qty']), 4), round(float(r['price']), 4))
            for r in jn.read_all()}


def _et_day_start(d):
    """'YYYY-MM-DD' -> midnight Eastern that day, timezone-aware."""
    return datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=jn.ET)


def _et_day_end(d):
    """'YYYY-MM-DD' -> 23:59:59 Eastern that day, timezone-aware."""
    return datetime.strptime(d, '%Y-%m-%d').replace(
        hour=23, minute=59, second=59, tzinfo=jn.ET)


def sync_alpaca(alpaca, days=None, after=None, until=None,
                start=None, end=None, dry_run=False):
    """Pull fills off the connected account into the journal.

    start/end are Eastern calendar days as 'YYYY-MM-DD', inclusive at both
    ends. They are converted to timezone-aware Eastern boundaries so the
    window means the trading days you picked, not a UTC approximation of
    them -- a UTC-midnight cutoff would reach back into the prior session's
    evening and re-pull trades you already have.

    after/until are accepted raw for callers that want exact timestamps, and
    days remains as 'the last N days'. start/end win when given.

    Returns a summary; nothing is written when dry_run is set.
    """
    if not alpaca.is_connected():
        raise RuntimeError('Not connected to Alpaca.')

    if start:
        after = _et_day_start(start)
    if end:
        until = _et_day_end(end)

    if after is None and days:
        after = _et_day_start(
            (datetime.now(jn.ET) - timedelta(days=int(days)))
            .strftime('%Y-%m-%d'))

    if (isinstance(after, datetime) and isinstance(until, datetime)
            and after > until):
        raise ValueError('Start date is after the end date.')

    activities = alpaca.get_fill_activities(after=after, until=until)

    seen_ids = _existing_activity_ids()
    seen_fps = _fingerprints()

    fresh, dupes, bad = [], 0, []
    for a in activities:
        if a['activity_id'] and a['activity_id'] in seen_ids:
            dupes += 1
            continue

        ts = jn.to_et(_parse_ts(a['transaction_time']))
        if not isinstance(ts, datetime):
            bad.append({'symbol': a['symbol'],
                        'issue': f"unparseable time {a['transaction_time']!r}"})
            continue

        symbol = a['symbol']
        fp = (symbol.upper(), ts.isoformat(timespec='seconds')[:19], a['side'],
              round(a['qty'], 4), round(a['price'], 4))
        if fp in seen_fps:
            dupes += 1
            continue

        try:
            rec = jn.make_record(
                ticker=_underlying(symbol), symbol=symbol, side=a['side'],
                qty=a['qty'], price=a['price'], filled_at=ts, source='engine',
                note='', meta={'activity_id': a['activity_id'],
                               'order_id': a['order_id'],
                               'fill_type': a['type'],
                               'via': 'alpaca_sync',
                               'account': 'paper' if alpaca.paper else 'live'})
        except ValueError as e:
            bad.append({'symbol': symbol, 'issue': str(e)})
            continue

        seen_ids.add(a['activity_id'])
        seen_fps.add(fp)
        fresh.append(rec)

    if not dry_run:
        jn.append_many(fresh)

    return {'ok': True, 'fetched': len(activities), 'imported': len(fresh),
            'skipped_duplicates': dupes, 'problems': bad, 'dry_run': dry_run,
            'window_after': str(after) if after else None,
            'window_until': str(until) if until else None,
            'window_start': start, 'window_end': end,
            'account': 'paper' if alpaca.paper else 'live'}


def _underlying(symbol):
    return symbol[:-15] if jn.is_occ(symbol) else symbol


def _parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace('Z', '+00:00')
    try:
        # Keep the offset. make_record converts to Eastern; discarding the
        # zone here is what silently stored Alpaca's UTC as if it were local.
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    return _loose_date(s)


# ─── PASTE ─────────────────────────────────────────────────────────────────────

# Header aliases, lowercased. A pasted block is matched against these so a
# broker calling it "Filled At" and another calling it "Time" both work.
HEADER_ALIASES = {
    'symbol': {'symbol', 'asset', 'ticker', 'instrument', 'contract'},
    'side': {'side', 'action', 'buy/sell', 'transaction', 'type'},
    'qty': {'qty', 'quantity', 'shares', 'contracts', 'filled qty', 'size',
            'amount'},
    'price': {'price', 'avg price', 'average price', 'fill price',
              'avg fill price', 'filled avg price', 'avg $ or # opts',
              'execution price'},
    'time': {'time', 'timestamp', 'filled at', 'fill time', 'date',
             'date/time', 'transaction time', 'executed at', 'submitted at'},
}

DATE_FORMATS = [
    '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
    '%b %d, %Y, %I:%M:%S %p', '%b %d, %Y, %I:%M %p', '%b %d, %Y %I:%M %p',
    '%b %d, %Y', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y %I:%M:%S %p',
    '%m/%d/%Y %I:%M %p', '%m/%d/%Y %H:%M', '%m/%d/%Y', '%m/%d/%y %H:%M',
    '%m/%d/%y', '%d %b %Y %H:%M', '%I:%M:%S %p', '%I:%M %p',
]


def _loose_date(s, default_date=None):
    s = str(s).strip().strip('"')
    if not s:
        return None
    for f in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, f)
            if dt.year == 1900:              # a bare clock time
                base = default_date or datetime.now()
                dt = dt.replace(year=base.year, month=base.month, day=base.day)
            return dt
        except ValueError:
            continue
    return None


def _sniff(text):
    """Split a pasted block into rows. Handles TSV, CSV and pipe tables."""
    text = text.strip('\n')
    if not text.strip():
        return []
    if '\t' in text:
        return [r for r in csv.reader(io.StringIO(text), delimiter='\t') if r]
    if text.count('|') >= text.count(',') and '|' in text:
        rows = []
        for line in text.splitlines():
            if set(line.strip()) <= set('|-: '):     # markdown separator
                continue
            rows.append([c.strip() for c in line.strip().strip('|').split('|')])
        return [r for r in rows if r]
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=',;\t|')
        return [r for r in csv.reader(io.StringIO(text), dialect) if r]
    except csv.Error:
        return [r for r in csv.reader(io.StringIO(text)) if r]


def _map_header(row):
    """Map a candidate header row to field -> column index, or None."""
    norm = [str(c).strip().lower().strip('"') for c in row]
    out = {}
    for field, names in HEADER_ALIASES.items():
        for i, c in enumerate(norm):
            if c in names and field not in out:
                out[field] = i
                break
    # A header must at least identify the symbol and one of qty/price.
    if 'symbol' in out and ({'qty', 'price'} & set(out)):
        return out
    return None


def _num(x):
    if x is None:
        return None
    x = re.sub(r'[^0-9.\-]', '', str(x))
    if x in ('', '-', '.', '-.'):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _side(x):
    s = str(x).strip().lower()
    if s.startswith(('buy', 'bought', 'b ', 'btо', 'bto', 'btc')):
        return 'buy'
    if s.startswith(('sell', 'sold', 's ', 'sto', 'stc')):
        return 'sell'
    if s in ('b', 'long'):
        return 'buy'
    if s in ('s', 'short'):
        return 'sell'
    return None


def parse_paste(text, default_date=None):
    """Parse pasted rows into journal records. Writes nothing.

    Returns (records, problems). A row that can't be read becomes a problem
    entry rather than silently vanishing, because a trade quietly dropped
    from the journal is worse than one flagged for retyping.
    """
    rows = _sniff(text)
    if not rows:
        return [], [{'row': 0, 'issue': 'nothing to parse'}]

    header, start = None, 0
    for i, r in enumerate(rows[:5]):
        m = _map_header(r)
        if m:
            header, start = m, i + 1
            break
    if header is None:
        return [], [{'row': 0, 'issue':
                     'no header row found. Include a header line naming at '
                     'least symbol, side, qty, price and time.'}]

    records, problems = [], []
    for i, r in enumerate(rows[start:], start=start + 1):
        if not any(str(c).strip() for c in r):
            continue

        def cell(f):
            idx = header.get(f)
            return r[idx] if idx is not None and idx < len(r) else None

        symbol = (cell('symbol') or '').strip().strip('"')
        side = _side(cell('side'))
        qty = _num(cell('qty'))
        price = _num(cell('price'))
        ts = _loose_date(cell('time') or '', default_date)

        missing = [n for n, v in (('symbol', symbol), ('side', side),
                                  ('qty', qty), ('price', price),
                                  ('time', ts)) if not v]
        if missing:
            problems.append({'row': i, 'raw': ' | '.join(str(c) for c in r)[:120],
                             'issue': 'missing ' + ', '.join(missing)})
            continue

        try:
            records.append(jn.make_record(
                ticker=_underlying(symbol), symbol=symbol, side=side,
                qty=qty, price=price, filled_at=ts, source='manual',
                meta={'via': 'paste', 'src_row': i}))
        except ValueError as e:
            problems.append({'row': i, 'issue': str(e)})

    return records, problems


def import_paste(text, default_date=None, dry_run=False):
    """parse_paste + dedupe + append."""
    records, problems = parse_paste(text, default_date)
    seen = _fingerprints()
    fresh, dupes = [], 0
    for r in records:
        fp = (r['symbol'].upper(), str(r['filled_at'])[:19], r['side'],
              round(r['qty'], 4), round(r['price'], 4))
        if fp in seen:
            dupes += 1
            continue
        seen.add(fp)
        fresh.append(r)

    if not dry_run:
        jn.append_many(fresh)

    return {'ok': True, 'parsed': len(records), 'imported': len(fresh),
            'skipped_duplicates': dupes, 'problems': problems,
            'dry_run': dry_run}
