"""
journal.py — Foundations Trading

The trade journal NOUN. One append-only record per FILL, from any source:
the live engine, a manual entry, or an imported broker export.

Stored as JSON Lines at DATA_DIR/journal.jsonl — one JSON object per line.

WHY JSONL AND NOT CSV
  trade_log.csv has a fixed header. Grades, tag lists, free-text notes and an
  open-ended set of condition layers do not fit a fixed header without either
  a schema migration per new field or a wall of mostly-empty columns. JSONL
  keeps the append-only, crash-tolerant, greppable properties of CSV while
  letting each record carry only the fields it actually has.

  The existing CSV logs are NOT replaced. trade_log.csv, signal_log.csv and
  order_log.csv keep recording exactly what they record today. This file is
  additive: it is the graded, paired, analyzable view that the Performance
  tab reads. If this file is deleted the trading side keeps working.

WHAT THIS MODULE DOES NOT DO
  It does not pair entries to exits — that is pairing.py, a consumer.
  It does not grade — grades arrive from the journaling UI via set_grade().
  It does not know about Alpaca, engines, or baskets.

ONE RECORD = ONE FILL. A position scaled into with three buys and closed with
one sell is four records, not one. Pairing splits them into legs afterwards.
"""

import os
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import config_manager as cm


JOURNAL_FILE = os.path.join(cm.DATA_DIR, 'journal.jsonl')

# Reuse the config_manager process lock. Two modules appending to two
# different files is fine, but a rewrite here and a rewrite there racing on
# the same directory is not worth reasoning about separately.
_LOCK = cm._WRITE_LOCK

GRADE_MIN, GRADE_MAX = 0, 4

# Every filled_at in this file is Eastern wall-clock, stored naive.
#
# The journal mixes sources that disagree about time: Alpaca reports UTC, the
# sheet export records the broker's local Eastern, and a pasted block is
# whatever the person copied. Pairing sorts fills by filled_at within a
# symbol, so one UTC timestamp among Eastern ones puts a sell four hours
# adrift and pairs it against the wrong buy. Normalizing at the door is the
# only place this is cheap to fix.
ET = ZoneInfo('America/New_York')


def to_et(dt):
    """Any datetime -> naive Eastern wall-clock.

    A tz-aware value is converted. A naive one is assumed to already be
    Eastern and passed through, because that is what both the sheet importer
    and a human pasting from a broker page produce.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return dt
    if dt.tzinfo is not None:
        dt = dt.astimezone(ET).replace(tzinfo=None)
    return dt

# The condition keys the heatmap offers as layers. Records may carry keys
# outside this list; they are preserved and simply not offered in the UI
# until added here. Ordering is the order they appear in the layer picker.
CONDITION_KEYS = [
    'trade_type',   # ripster / scalp / swing -- see TRADE_TYPES
    'srp',          # S/R/P followed: Correctly / Incorrectly / N/A
    'ema_5_12',     # price vs the 5/12 cloud: above / below / inside / at
    'ema_34_50',    # price vs the 34/50 cloud
    'mtf_1h',       # 1H 34/50 multi-timeframe
    'mtf_1d',       # 1D 20/21 multi-timeframe
    'trend',        # engine 34/50 verdict: up / down / chop
    'trigger',      # engine 5/12 cross: fresh_long / fresh_short / none
    'exit_kind',    # structural / ride_end
]

SOURCES = ('engine', 'manual', 'import')

# ─── TRADE TYPE ────────────────────────────────────────────────────────────────
#
# WHAT KIND OF TRADE THIS WAS MEANT TO BE. Three, and only three:
#
#   ripster   the system proper -- 34/50 hard trend gate, 5/12 cross entry,
#             structural exit. What the engine trades and what the whole book
#             before 2026-09-28 was, by blanket assignment.
#   scalp     a deliberate quick in-and-out, typically on an index name. The
#             owner's own description: he trades Ripster but likes to scalp SPY.
#   swing     a hold measured in days, governed by the 1H and 1D clouds rather
#             than by the working timeframe.
#
# WHY THIS IS A CONDITION AND NOT A TOP-LEVEL FIELD
#   `conditions` is already the thing the heatmap slices by -- heatmap.py maps
#   every key here to an `entry_<key>` layer with no per-key code. Putting the
#   trade type anywhere else would mean writing a layer by hand to answer the
#   first question anyone will ask of it ("how do my scalps grade against my
#   Ripster trades?"). It is the same free win `exit_kind` got.
#
# WHY THE VOCABULARY IS CLOSED
#   The other condition keys are open on purpose: a record may carry a value
#   the UI has not seen. This one may not. A stray 'Scalp' or 'scalping' would
#   silently split a heatmap column in two and every count on both sides would
#   be wrong without looking wrong. Normalised at the door, same argument as
#   `filled_at` being forced to Eastern.
TRADE_TYPES = ('ripster', 'scalp', 'swing')

# The date the owner started choosing a type by hand. Everything filled BEFORE
# this is `ripster` by his blanket ruling of 2026-09-30; everything on or after
# it he grades and types himself. It happens to fall exactly on the graded /
# ungraded boundary in the live journal (386 graded before, 28 ungraded after),
# which is why the backfill can be stated as a date rather than a list of ids.
TRADE_TYPE_BLANKET_BEFORE = '2026-09-28'


def normalize_trade_type(value):
    """'' / None -> None; anything else -> a member of TRADE_TYPES, or raise."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v not in TRADE_TYPES:
        raise ValueError(
            f'trade_type must be one of {TRADE_TYPES} (or blank), got {value!r}')
    return v


# ─── RECORD CONSTRUCTION ───────────────────────────────────────────────────────

def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def new_id(prefix='f'):
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


def make_record(*, ticker, side, qty, price, filled_at,
                symbol=None, instrument=None, source='manual',
                conditions=None, grade=None, tags_good=None, tags_bad=None,
                note='', signal_id=None, meta=None, trade_type=None):
    """Build one fill record. Does not write it — see append().

    filled_at accepts a datetime or an ISO string. qty is always positive;
    direction lives in `side`. price is per share, or per contract for an
    option (NOT per-contract x100 — the multiplier is applied in `cash`).
    """
    if side not in ('buy', 'sell'):
        raise ValueError(f'side must be buy or sell, got {side!r}')
    if source not in SOURCES:
        raise ValueError(f'source must be one of {SOURCES}, got {source!r}')
    if qty is None or qty <= 0:
        raise ValueError(f'qty must be positive, got {qty!r}')
    if price is None or price < 0:
        raise ValueError(f'price must be non-negative, got {price!r}')

    conditions = dict(conditions or {})
    tt = normalize_trade_type(
        trade_type if trade_type is not None else conditions.get('trade_type'))
    if tt is None:
        conditions.pop('trade_type', None)
    else:
        conditions['trade_type'] = tt

    filled_at = to_et(filled_at)
    if isinstance(filled_at, datetime):
        filled_at = filled_at.isoformat(timespec='seconds')

    symbol = (symbol or ticker).strip()
    if instrument is None:
        instrument = 'option' if is_occ(symbol) else 'stock'
    mult = 100 if instrument == 'option' else 1

    return {
        'id': new_id(),
        'trade_id': None,          # assigned by pairing.py, not here
        'role': None,              # entry / exit, also assigned by pairing
        'source': source,
        'ticker': ticker.strip().upper(),
        'symbol': symbol,
        'instrument': instrument,
        'side': side,
        'qty': float(qty),
        'price': float(price),
        'multiplier': mult,
        'cash': round(float(qty) * float(price) * mult, 4),
        'filled_at': filled_at,
        'conditions': conditions,
        'grade': grade,
        'tags_good': list(tags_good or []),
        'tags_bad': list(tags_bad or []),
        'note': note or '',
        'signal_id': signal_id,
        'graded_at': _now_iso() if grade is not None else None,
        'logged_at': _now_iso(),
        'meta': dict(meta or {}),
    }


def is_occ(symbol):
    """True if symbol looks like an OCC option symbol (SPY260929P00768000)."""
    s = (symbol or '').strip()
    if len(s) < 16:
        return False
    tail = s[-15:]
    return (tail[:6].isdigit() and tail[6] in 'CP' and tail[7:].isdigit())


def occ_parts(symbol):
    """Split an OCC symbol into (underlying, expiry_date, 'C'/'P', strike).

    Returns None for anything that isn't an OCC symbol. Used by the pairing
    layer to decide whether an unclosed position expired or is still open.
    """
    s = (symbol or '').strip()
    if not is_occ(s):
        return None
    tail = s[-15:]
    under = s[:-15]
    try:
        exp = datetime(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    except ValueError:
        return None
    return under, exp, tail[6], int(tail[7:]) / 1000.0


# ─── WRITE ─────────────────────────────────────────────────────────────────────

def append(record):
    """Append one record. Returns the record (with its id)."""
    with _LOCK:
        with open(JOURNAL_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return record


def append_many(records):
    """Append a batch in one open/fsync. Used by the importer."""
    records = list(records)
    if not records:
        return []
    with _LOCK:
        with open(JOURNAL_FILE, 'a', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return records


def log_fill(**kwargs):
    """make_record + append in one call. What the engine calls."""
    return append(make_record(**kwargs))


# ─── READ ──────────────────────────────────────────────────────────────────────

def read_all(ticker=None, source=None, since=None, ungraded_only=False):
    """Read every record, optionally filtered. Sorted by filled_at.

    A corrupt line (partial write from a hard kill mid-append) is skipped
    rather than raising, so one bad line can't take out the whole journal.
    """
    if not os.path.exists(JOURNAL_FILE):
        return []
    out = []
    with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ticker and r.get('ticker', '').upper() != ticker.upper():
                continue
            if source and r.get('source') != source:
                continue
            if since and (r.get('filled_at') or '') < since:
                continue
            if ungraded_only and r.get('grade') is not None:
                continue
            out.append(r)
    out.sort(key=lambda r: (r.get('filled_at') or '', r.get('logged_at') or ''))
    return out


def get(record_id):
    for r in read_all():
        if r.get('id') == record_id:
            return r
    return None


def ungraded(limit=None):
    """Fills still waiting on a grade — what the journaling UI works through."""
    rows = read_all(ungraded_only=True)
    return rows[:limit] if limit else rows


# ─── UPDATE ────────────────────────────────────────────────────────────────────

def _rewrite(records):
    """Atomically replace the whole file. JSONL is append-only by habit, not
    by law; grading edits an existing record, so a full rewrite is the honest
    implementation. At journal scale (tens of thousands of lines) this is
    milliseconds, and the temp-then-replace keeps it crash-safe."""
    tmp = JOURNAL_FILE + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOURNAL_FILE)


def update(record_id, **changes):
    """Patch one record by id. Returns the updated record, or None."""
    rows = read_all()
    hit = None
    for r in rows:
        if r.get('id') == record_id:
            r.update(changes)
            hit = r
            break
    if hit is None:
        return None
    _rewrite(rows)
    return hit


def set_grade(record_id, grade, *, tags_good=None, tags_bad=None, note=None):
    """Grade one fill. This is the write the journaling UI performs.

    grade must be an int in [0, 4], or None to un-grade (which puts the fill
    back in the ungraded queue rather than deleting anything).
    """
    if grade is not None:
        grade = int(grade)
        if not GRADE_MIN <= grade <= GRADE_MAX:
            raise ValueError(f'grade must be {GRADE_MIN}-{GRADE_MAX}, got {grade}')
    changes = {'grade': grade,
               'graded_at': _now_iso() if grade is not None else None}
    if tags_good is not None:
        changes['tags_good'] = list(tags_good)
    if tags_bad is not None:
        changes['tags_bad'] = list(tags_bad)
    if note is not None:
        changes['note'] = note
    return update(record_id, **changes)


EDITABLE = ('symbol', 'ticker', 'side', 'qty', 'price', 'filled_at',
            'note', 'grade', 'tags_good', 'tags_bad', 'source')


def edit_fill(record_id, **changes):
    """Edit a fill's own details, not just its grade.

    `cash`, `multiplier` and `instrument` are DERIVED, never passed in: they
    are recomputed from whatever symbol/qty/price end up on the record. Left
    to a caller they drift, and a stale `cash` silently corrupts the
    reconciliation that proves the pairing is sound.

    Only the fields in EDITABLE are accepted; anything else is ignored rather
    than written, so a typo in a payload cannot invent a field.
    """
    rec = get(record_id)
    if rec is None:
        return None

    patch = {k: v for k, v in changes.items() if k in EDITABLE}

    if 'side' in patch:
        if patch['side'] not in ('buy', 'sell'):
            raise ValueError(f"side must be buy or sell, got {patch['side']!r}")
    if 'qty' in patch:
        patch['qty'] = float(patch['qty'])
        if patch['qty'] <= 0:
            raise ValueError('qty must be positive')
    if 'price' in patch:
        patch['price'] = float(patch['price'])
        if patch['price'] < 0:
            raise ValueError('price must be non-negative')
    if 'grade' in patch and patch['grade'] is not None:
        patch['grade'] = int(patch['grade'])
        if not GRADE_MIN <= patch['grade'] <= GRADE_MAX:
            raise ValueError(f'grade must be {GRADE_MIN}-{GRADE_MAX}')
        patch['graded_at'] = _now_iso()
    if 'filled_at' in patch:
        when = to_et(patch['filled_at'])
        if isinstance(when, datetime):
            when = when.isoformat(timespec='seconds')
        if not when:
            raise ValueError('filled_at is required')
        patch['filled_at'] = when
    if 'symbol' in patch:
        patch['symbol'] = str(patch['symbol']).strip()
        if not patch['symbol']:
            raise ValueError('symbol is required')
        patch.setdefault('ticker', (patch['symbol'][:-15]
                                    if is_occ(patch['symbol'])
                                    else patch['symbol']).upper())

    merged = {**rec, **patch}
    merged['instrument'] = 'option' if is_occ(merged['symbol']) else 'stock'
    merged['multiplier'] = 100 if merged['instrument'] == 'option' else 1
    merged['cash'] = round(float(merged['qty']) * float(merged['price'])
                           * merged['multiplier'], 4)
    merged['edited_at'] = _now_iso()

    return update(record_id, **{k: merged[k] for k in merged
                                if k not in ('id',)})


def delete_record(record_id):
    """Remove one fill. Returns the removed record, or None.

    Deleting an entry whose exit remains turns that exit into an orphan, and
    the pairing will read it as opening a position on the wrong side. Check
    reconcile() after deleting half a pair.
    """
    rows = read_all()
    keep = [r for r in rows if r.get('id') != record_id]
    if len(keep) == len(rows):
        return None
    gone = next(r for r in rows if r.get('id') == record_id)
    _rewrite(keep)
    return gone


def search(query='', scope='all', limit=200):
    """Fills matching a text query, newest first. scope: all | ungraded | graded."""
    q = (query or '').strip().lower()
    rows = read_all()
    if scope == 'ungraded':
        rows = [r for r in rows if r.get('grade') is None]
    elif scope == 'graded':
        rows = [r for r in rows if r.get('grade') is not None]
    if q:
        rows = [r for r in rows
                if q in r.get('symbol', '').lower()
                or q in r.get('ticker', '').lower()
                or q in (r.get('note') or '').lower()
                or q in (r.get('filled_at') or '')]
    rows.sort(key=lambda r: r.get('filled_at') or '', reverse=True)
    return rows[:limit]


def set_conditions(record_id, conditions):
    """Replace the condition layer values on one fill (manual correction)."""
    r = get(record_id)
    if r is None:
        return None
    merged = dict(r.get('conditions') or {})
    merged.update(conditions or {})
    # The one key with a closed vocabulary. Raise rather than drop: a typo the
    # UI swallowed would read as "I never set it" and the row would sit in the
    # untyped bucket looking like an honest omission.
    tt = normalize_trade_type(merged.get('trade_type'))
    if tt is None:
        merged.pop('trade_type', None)
    else:
        merged['trade_type'] = tt
    return update(record_id, conditions=merged)


def backfill_trade_type(value='ripster', before=TRADE_TYPE_BLANKET_BEFORE,
                        overwrite=False, dry_run=True):
    """Stamp a trade_type on every fill filled strictly BEFORE `before`.

    The owner's ruling of 2026-09-30: everything he traded before he started
    distinguishing scalps was the Ripster system, so it is one blanket write
    rather than 386 hand edits. `before` is a date string compared against the
    date part of filled_at -- half-open, so a fill ON that date is his to type.

    Skips records that already carry a trade_type unless `overwrite`. Returns
    a summary dict; writes nothing when dry_run.
    """
    value = normalize_trade_type(value)
    if value is None:
        raise ValueError('backfill needs a trade_type')
    rows = read_all()
    hit = 0
    for r in rows:
        if (r.get('filled_at') or '')[:10] >= before:
            continue
        conds = r.get('conditions')
        if conds is None:
            conds = r['conditions'] = {}
        if conds.get('trade_type') and not overwrite:
            continue
        hit += 1
        if not dry_run:
            conds['trade_type'] = value
    if not dry_run and hit:
        _rewrite(rows)
    return {'records': len(rows), 'stamped': hit, 'value': value,
            'before': before, 'dry_run': dry_run,
            'untouched_on_or_after': sum(
                1 for r in rows if (r.get('filled_at') or '')[:10] >= before)}


def purge(source=None, via=None, ungraded_only=False, dry_run=True):
    """Remove records in bulk. Returns what was (or would be) removed.

    The escape hatch for a sync that pulled history you already had graded.
    Deleting ungraded engine fills is safe by construction: a graded record is
    work you did by hand and is never removed by the ungraded_only path.

    dry_run defaults True. Nothing goes without asking.
    """
    rows = read_all()
    keep, drop = [], []
    for r in rows:
        hit = True
        if source is not None and r.get('source') != source:
            hit = False
        if via is not None and (r.get('meta') or {}).get('via') != via:
            hit = False
        if ungraded_only and r.get('grade') is not None:
            hit = False
        (drop if hit else keep).append(r)

    if not dry_run and drop:
        _rewrite(keep)

    return {'removed': len(drop), 'remaining': len(keep), 'dry_run': dry_run,
            'sample': [{'symbol': r['symbol'], 'side': r['side'],
                        'filled_at': r['filled_at'], 'source': r['source'],
                        'graded': r.get('grade') is not None}
                       for r in drop[:15]]}


def latest_filled_at(graded_only=False):
    """Timestamp of the newest fill on file, for picking a sync cutoff."""
    rows = [r for r in read_all()
            if not graded_only or r.get('grade') is not None]
    return max((r['filled_at'] for r in rows), default=None)


# ─── VOCABULARY ────────────────────────────────────────────────────────────────

def known_tags():
    """Every tag string that appears anywhere in the journal, split good/bad.

    The tag vocabulary is not hardcoded: it grows from what gets used. The
    journaling UI offers these as chips so spelling stays consistent, while
    still allowing a new tag to be typed.
    """
    good, bad = set(), set()
    for r in read_all():
        good.update(r.get('tags_good') or [])
        bad.update(r.get('tags_bad') or [])
    return {'good': sorted(good), 'bad': sorted(bad)}


def condition_values():
    """Observed values for each condition key, for building layer filters."""
    seen = {k: set() for k in CONDITION_KEYS}
    for r in read_all():
        for k, v in (r.get('conditions') or {}).items():
            if v in (None, ''):
                continue
            seen.setdefault(k, set()).add(str(v))
    # trade_type is the one closed vocabulary, so offer all of it whether or
    # not it has been used yet -- otherwise the first scalp has to be typed
    # blind against a datalist that only knows 'ripster'.
    seen['trade_type'] = set(TRADE_TYPES)
    return {k: sorted(v) for k, v in seen.items() if v}


def stats():
    rows = read_all()
    return {
        'records': len(rows),
        'graded': sum(1 for r in rows if r.get('grade') is not None),
        'ungraded': sum(1 for r in rows if r.get('grade') is None),
        'by_source': {s: sum(1 for r in rows if r.get('source') == s)
                      for s in SOURCES},
        'file': JOURNAL_FILE,
    }


def selftest():
    """Covers the trade_type vocabulary only. Reads the live journal but never
    writes it -- the backfill is exercised with dry_run=True."""
    fails = []

    def ck(label, cond, got=None):
        if cond:
            print(f'  ok   {label}')
        else:
            fails.append(label)
            print(f'  FAIL {label}' + (f'   got {got!r}' if got is not None else ''))

    def raises(fn, *a, **k):
        try:
            fn(*a, **k)
        except ValueError:
            return True
        return False

    print('normalize_trade_type')
    ck('blank is None, not a value', normalize_trade_type('') is None)
    ck('None is None', normalize_trade_type(None) is None)
    ck('whitespace-only is None', normalize_trade_type('   ') is None)
    ck('case and padding are normalised',
       normalize_trade_type('  Scalp ') == 'scalp')
    ck('a near-miss RAISES rather than being dropped',
       raises(normalize_trade_type, 'scalping'))
    ck('an unrelated string raises', raises(normalize_trade_type, 'ripsterish'))
    ck('every member of the vocabulary survives a round trip',
       all(normalize_trade_type(t) == t for t in TRADE_TYPES))

    print('make_record')
    base = dict(ticker='spy', side='buy', qty=1, price=1.0,
                filled_at='2026-09-30T10:00:00')
    ck('the kwarg lands in conditions',
       make_record(trade_type='SCALP', **base)['conditions']['trade_type'] == 'scalp')
    ck('a value passed inside conditions is normalised too',
       make_record(conditions={'trade_type': ' Swing'}, **base)
       ['conditions']['trade_type'] == 'swing')
    ck('the kwarg wins over the conditions dict',
       make_record(trade_type='scalp', conditions={'trade_type': 'swing'}, **base)
       ['conditions']['trade_type'] == 'scalp')
    ck('absent means absent -- no empty key left behind',
       'trade_type' not in make_record(**base)['conditions'])
    ck('a blank does not create the key',
       'trade_type' not in make_record(trade_type='', **base)['conditions'])
    ck('a bad type refuses to build a record at all',
       raises(make_record, trade_type='daytrade', **base))
    ck('other conditions are untouched',
       make_record(conditions={'srp': 'Mixed'}, trade_type='scalp', **base)
       ['conditions'] == {'srp': 'Mixed', 'trade_type': 'scalp'})

    print('vocabulary offered to the UI')
    ck('trade_type is a condition key', 'trade_type' in CONDITION_KEYS)
    ck('it is first, because it governs how the rest are read',
       CONDITION_KEYS[0] == 'trade_type')
    ck('all three are offered whether or not they have been used',
       sorted(condition_values()['trade_type']) == sorted(TRADE_TYPES))

    print('backfill (dry run against the live book)')
    dry = backfill_trade_type(dry_run=True)
    ck('is idempotent -- nothing left to stamp', dry['stamped'] == 0,
       dry['stamped'])
    ck('the boundary is half-open, so fills ON the date are his',
       all((r.get('filled_at') or '')[:10] < TRADE_TYPE_BLANKET_BEFORE
           for r in read_all()
           if (r.get('conditions') or {}).get('trade_type') == 'ripster'))
    ck('it refuses a type outside the vocabulary',
       raises(backfill_trade_type, value='ripsters', dry_run=True))

    print()
    if fails:
        print(f'SELFTEST FAILED -- {len(fails)} of the above')
    else:
        print('SELFTEST PASSED')
    return not fails


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
    import pprint
    pprint.pprint(stats())
