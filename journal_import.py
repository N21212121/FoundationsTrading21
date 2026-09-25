"""
journal_import.py — Foundations Trading

Imports a Google Sheets trade-log export into the journal noun.

WHY THIS IS NOT A TEN-LINE CSV READER
  The exported sheet is a working document, not a data file. Specifically:

  1. THREE COLUMN LAYOUTS. Rows where the broker recorded both a Submitted
     and a Filled timestamp shift every column after Asset one place left.
     Rows where Qty is blank shift differently again. The header row only
     describes one of the three.
  2. WEEK BANNERS AND GPA ROWS interleaved with data rows.
  3. CORRUPTED NUMERIC CELLS. In a handful of rows the Total was pasted into
     the price column, or a limit price landed in the Qty column. These are
     resolved against a reference price built from the same symbol's clean
     rows, because a $933 "price" on a stock trading at $90 is a duplicated
     total while $0.88 on an option really is the price.
  4. PER-CONTRACT OPTION AMOUNTS. A $1.80 option entry means $180 of cash.

  ROW_OVERRIDES below hardcodes the cases that no rule resolves, each one
  worked out against the sibling leg of the same position. They are keyed by
  source row number and only apply to the file they were derived from, so a
  freshly exported sheet is matched on symbol+timestamp instead.

IDEMPOTENCY
  import_file() refuses to create a record whose (symbol, filled_at, side,
  qty, price) already exists in the journal. Re-importing the same sheet
  after adding a week of trades adds only the new rows.
"""

import csv
import io
import os
import re
import statistics
from datetime import datetime

import journal as jn


OPTION_RE = re.compile(r'^[A-Za-z]+\d{6}[CP]\d{8}$')
LIMIT_RE = re.compile(r'Limit @ \$([\d.]+)')

# Sheet column -> journal condition key.
CONDITION_MAP = {
    'S/R/P Followed': 'srp',
    'EMA 5/12': 'ema_5_12',
    'EMA 34/50': 'ema_34_50',
    '1H MTF 34/50': 'mtf_1h',
    '1D MTF 20/21': 'mtf_1d',
}

# Rows whose qty/price cells are demonstrably corrupted, each resolved by
# cross-referencing the other leg(s) of the same position. Keyed by the row
# index in the ORIGINAL export. See the module docstring.
ROW_OVERRIDES = {
    105: (2,   0.88),    # Total cell duplicated the price; sibling sell is a clean 2-lot
    180: (10,  93.330),  # Price cell duplicated the Total; MRNX trades ~$90
    181: (10,  93.529),
    182: (10,  91.386),
    218: (100, 9.4574),  # Qty cell held the limit price; note says "100 shares"
    258: (1,   2.02),    # two buys against a clean 2-lot sell => 1 contract each
    259: (1,   2.93),    # UNCERTAIN: cells read 2.29 and 2.93
    260: (1,   4.12),    # fill above the 4.07 buy, matching the note's "tiny profit"
    261: (1,   4.07),
    262: (1,   1.25),    # Qty cell duplicated the price; single contract
    263: (1,   1.04),
    264: (1,   0.51),
    265: (1,   1.21),
    330: (10,  0.39),    # Total 3.90 at the 0.39 limit => 10 contracts
}

UNCERTAIN_ROWS = {259: 'cells read 2.29 and 2.93; 2.93 used'}

# Fills that happened but never made it into the sheet, supplied by hand.
#
# Without these the pairing sees an exit with no entry and opens a SHORT on
# it, which inverts the sign: the DELL put below was a long that lost almost
# everything, but the lone sell read as a short that expired for a $1 gain.
#
# Each is injected only if the parsed file does not already contain a fill
# with the same symbol/time/side/qty/price, so adding the row to the sheet
# later supersedes this entry rather than duplicating it.
MISSING_FILLS = [
    dict(symbol='DELL260911P00450000', side='buy', qty=1.0, price=6.65,
         filled_at='2026-09-02T14:37:57', grade=0,
         conditions={'srp': 'Incorrectly', 'ema_5_12': 'above',
                     'ema_34_50': 'above', 'mtf_1h': 'above',
                     'mtf_1d': 'above'},
         tags_good=[], tags_bad=['Reversal, no setup', '5/12 Not Followed'],
         note='Worst idea possible.',
         why='entry omitted from the sheet; supplied from the broker record'),
]


def _inject_missing(records, problems):
    """Add any MISSING_FILLS the parsed file does not already contain."""
    have = {_fingerprint(r['symbol'], r['filled_at'], r['side'],
                         r['qty'], r['price']) for r in records}
    for m in MISSING_FILLS:
        fp = _fingerprint(m['symbol'], m['filled_at'], m['side'],
                          m['qty'], m['price'])
        if fp in have:
            continue
        records.append(jn.make_record(
            ticker=(m['symbol'][:-15] if OPTION_RE.match(m['symbol'])
                    else m['symbol']),
            symbol=m['symbol'], side=m['side'], qty=m['qty'],
            price=m['price'], filled_at=m['filled_at'], source='import',
            conditions=m.get('conditions') or {}, grade=m.get('grade'),
            tags_good=m.get('tags_good'), tags_bad=m.get('tags_bad'),
            note=m.get('note', ''),
            meta={'via': 'missing_fill', 'reason': m.get('why', '')}))
        problems.append({'row': 0, 'asset': m['symbol'],
                         'issue': f"added by hand: {m.get('why', '')}"})
    records.sort(key=lambda r: r['filled_at'])
    return records, problems


# ─── CELL HELPERS ──────────────────────────────────────────────────────────────

def _num(x):
    if x is None:
        return None
    x = x.replace('$', '').replace(',', '').strip()
    if x in ('', '-'):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _date(s):
    if not s:
        return None
    for fmt in ('%b %d, %Y, %I:%M:%S %p', '%b %d, %Y, %I:%M %p',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _split_tags(s):
    """Tags are comma-separated, but some contain commas inside quotes."""
    if not s or not s.strip():
        return []
    parts = re.findall(r'"[^"]*"|[^,]+', s)
    out = [p.strip().strip('"').strip() for p in parts]
    return [p for p in out if p and p.lower() != 'nothing']


# ─── PARSE ─────────────────────────────────────────────────────────────────────

ENCODINGS = ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1')


def read_text(path):
    """Read a CSV off disk, forgiving about how the path was pasted.

    Windows "Copy as path" wraps the path in double quotes, which raises
    OSError 22 rather than FileNotFoundError, so it never looked like a
    missing file. Excel also writes cp1252 often enough that a hard
    utf-8 decode fails on a single smart quote.
    """
    raw = str(path or '').strip().strip('"').strip("'").strip()
    raw = os.path.expanduser(os.path.expandvars(raw))

    if not raw:
        raise ValueError('No path given.')
    if not os.path.exists(raw):
        raise FileNotFoundError(raw)
    if os.path.isdir(raw):
        raise ValueError(f'That is a folder, not a file: {raw}')

    ext = os.path.splitext(raw)[1].lower()
    if ext in ('.xlsx', '.xls', '.xlsm', '.numbers', '.ods'):
        raise ValueError(
            f'{ext} is a spreadsheet, not a CSV. In Sheets use '
            'File > Download > Comma-separated values, then point at '
            'the .csv it saves.')

    last = None
    for enc in ENCODINGS:
        try:
            with open(raw, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError as e:
            last = e
            continue
    raise ValueError(
        'Could not decode this file as text. If it is a spreadsheet, export '
        f'it to CSV first. ({last})')


def _raw_rows(text):
    """Yield dicts of the raw cells, layout-normalized, skipping banners."""
    reader = list(csv.reader(io.StringIO(text)))

    week = None
    for i, r in enumerate(reader):
        if i < 3 or not any(c.strip() for c in r):
            continue
        head = r[0].strip()
        if head.startswith('Week'):
            week = head
            continue
        if head.startswith('Trades') or 'GPA' in head:
            continue
        if len(r) < 18:
            continue

        idx2 = r[2].strip()
        both_stamps = _date(r[6]) is not None

        if both_stamps:                         # shifted left by one
            qty_c, price_c, total_c = _num(r[2]), _num(r[3]), _num(r[4])
        elif idx2.lower() in ('buy', 'sell'):   # side occupies the Qty column
            qty_c, price_c, total_c = _num(r[3]), _num(r[4]), _num(r[5])
        elif idx2 == '':                        # Qty blank
            qty_c, price_c, total_c = None, _num(r[3]), _num(r[4])
        else:
            qty_c, price_c, total_c = _num(r[2]), _num(r[3]), _num(r[4])

        lim = LIMIT_RE.search(r[1] or '')
        yield {
            'row': i, 'week': week,
            'asset': r[0].strip(), 'order_type': r[1].strip(),
            'qty_c': qty_c, 'price_c': price_c, 'total_c': total_c,
            'limit': float(lim.group(1)) if lim else None,
            'filled_at': _date(r[7]), 'side': r[8].strip().lower(),
            'conditions': {CONDITION_MAP[k]: v for k, v in (
                ('S/R/P Followed', r[9].strip()), ('EMA 5/12', r[10].strip()),
                ('EMA 34/50', r[11].strip()), ('1H MTF 34/50', r[12].strip()),
                ('1D MTF 20/21', r[13].strip())) if v},
            'grade_raw': r[14].strip(),
            'note': r[15].strip(),
            'tags_good': _split_tags(r[16]), 'tags_bad': _split_tags(r[17]),
        }


def _internally_consistent(d):
    if None in (d['qty_c'], d['price_c'], d['total_c']) or not d['total_c']:
        return False
    return abs(d['qty_c'] * d['price_c'] - d['total_c']) / d['total_c'] < 0.02


def _reference_prices(raws):
    """Median price per symbol, from internally consistent rows only."""
    buckets = {}
    for d in raws:
        if _internally_consistent(d) and d['price_c']:
            buckets.setdefault(d['asset'].strip(), []).append(d['price_c'])
    ref = {k: statistics.median(v) for k, v in buckets.items()}
    for d in raws:                      # fall back for symbols with no clean row
        a = d['asset'].strip()
        if a not in ref and d['price_c']:
            ref.setdefault(a, d['price_c'])
    return ref


def _resolve_qty_price(d, ref):
    """Return (qty, price, how). See the module docstring for the rules."""
    if d['row'] in ROW_OVERRIDES:
        q, p = ROW_OVERRIDES[d['row']]
        return q, p, 'override'

    if _internally_consistent(d):
        return d['qty_c'], d['price_c'], 'direct'

    qc, pc, tc, lim = d['qty_c'], d['price_c'], d['total_c'], d['limit']
    R = ref.get(d['asset'].strip())

    cands = []
    if pc and tc:
        cands.append((tc / pc, pc, 'qty=total/price'))
    if qc and tc:
        cands.append((qc, tc / qc, 'price=total/qty'))
    if qc and pc:
        cands.append((qc, pc, 'qty*price'))
    if lim and tc:
        cands.append((tc / lim, lim, 'price=limit'))
    if pc and tc and abs(pc - tc) / max(tc, 1e-9) < 0.02:
        cands.append((1.0, pc, 'single unit'))
    if not cands:
        return None, None, 'unresolved'

    def score(c):
        q, p, _ = c
        if q <= 0 or p <= 0:
            return 1e18
        s = abs(p - R) / R if R else 0.0
        if abs(q - round(q)) > 0.02 and q < 50:   # prefer whole lots
            s += 0.25
        return s

    q, p, how = min(cands, key=score)
    return q, p, how


# ─── IMPORT ────────────────────────────────────────────────────────────────────

def _fingerprint(symbol, filled_at, side, qty, price):
    return (symbol.strip().upper(), str(filled_at)[:19], side,
            round(float(qty), 4), round(float(price), 4))


def parse_file(path):
    """Parse an export on disk. Does not write anything."""
    return parse_text(read_text(path))


def detect_format(text):
    """Which export is this? 'pair_detail', 'trade_log', or None.

    The person should not have to know which sheet the importer wants. Both
    exports carry grades; they just carry them in different shapes.
    """
    import journal_pairdetail as jpd
    if jpd.looks_like(text):
        return 'pair_detail'
    head = text[:4000]
    if 'Entry/Exit Grade' in head or 'S/R/P Followed' in head:
        return 'trade_log'
    return None


def parse_text(text):
    """Parse export CONTENT into journal records. Does not write anything.

    Separate from parse_file so the browser can hand us the bytes it already
    decoded, which sidesteps paths, drive letters and encodings entirely.
    Dispatches on the detected format.
    """
    if detect_format(text) == 'pair_detail':
        import journal_pairdetail as jpd
        recs, probs = jpd.parse_text(text)
        return _inject_missing(recs, probs)

    raws = list(_raw_rows(text))
    if not raws:
        return [], [{'row': 0, 'issue':
                     'No trade rows found. Is this the Trade Log export? It '
                     'needs the Asset/Side/Filled At/Grade columns.'}]
    ref = _reference_prices(raws)

    records, problems = [], []
    for d in raws:
        qty, price, how = _resolve_qty_price(d, ref)
        if qty is None or price is None or not d['filled_at'] \
                or d['side'] not in ('buy', 'sell'):
            problems.append({'row': d['row'], 'asset': d['asset'],
                             'issue': 'could not resolve', 'how': how})
            continue

        try:
            grade = int(d['grade_raw'])
        except (TypeError, ValueError):
            grade = None
            problems.append({'row': d['row'], 'asset': d['asset'],
                             'issue': f"bad grade {d['grade_raw']!r}"})

        meta = {'src_row': d['row'], 'week': d['week'],
                'order_type': d['order_type'], 'resolution': how}
        if d['row'] in UNCERTAIN_ROWS:
            meta['uncertain'] = UNCERTAIN_ROWS[d['row']]
        if how not in ('direct',):
            problems.append({'row': d['row'], 'asset': d['asset'],
                             'issue': f'inferred via {how}',
                             'qty': round(qty, 4), 'price': round(price, 4)})

        records.append(jn.make_record(
            ticker=(d['asset'][:-15] if OPTION_RE.match(d['asset'])
                    else d['asset']),
            symbol=d['asset'], side=d['side'], qty=qty, price=price,
            filled_at=d['filled_at'], source='import',
            conditions=d['conditions'], grade=grade,
            tags_good=d['tags_good'], tags_bad=d['tags_bad'],
            note=d['note'], meta=meta))

    return _inject_missing(records, problems)


def import_file(path, dry_run=False):
    """Parse a file on disk and append. Skips fills already present."""
    return import_text(read_text(path), dry_run=dry_run)


def import_text(text, dry_run=False):
    """Parse uploaded content and append. Skips fills already present."""
    records, problems = parse_text(text)
    res = _commit(records, problems, dry_run)
    res['format'] = detect_format(text) or 'unrecognized'
    return res


def _commit(records, problems, dry_run):
    # Zero rows off a file that read fine almost always means the wrong CSV
    # was picked -- the Pair Detail export rather than the Trade Log, say.
    # Saying "imported 0" without saying why sends people hunting.
    if not records:
        problems = list(problems) + [{'row': 0, 'issue':
            'No trade rows recognized. The importer reads two exports: the '
            'Trade Log sheet (one row per order) and the Pair Detail sheet '
            '(one row per entry/exit leg). This matches neither.'}]
        return {'parsed': 0, 'imported': 0, 'skipped_duplicates': 0,
                'problems': problems, 'dry_run': dry_run}

    existing = {_fingerprint(r['symbol'], r['filled_at'], r['side'],
                             r['qty'], r['price']) for r in jn.read_all()}
    fresh, dupes = [], 0
    for r in records:
        fp = _fingerprint(r['symbol'], r['filled_at'], r['side'],
                          r['qty'], r['price'])
        if fp in existing:
            dupes += 1
            continue
        existing.add(fp)
        fresh.append(r)

    if not dry_run:
        jn.append_many(fresh)

    return {'parsed': len(records), 'imported': len(fresh),
            'skipped_duplicates': dupes, 'problems': problems,
            'dry_run': dry_run}


if __name__ == '__main__':
    import sys
    import pprint
    if len(sys.argv) < 2:
        print('usage: python journal_import.py <export.csv> [--commit]')
        raise SystemExit(1)
    pprint.pprint(import_file(sys.argv[1],
                              dry_run='--commit' not in sys.argv))
