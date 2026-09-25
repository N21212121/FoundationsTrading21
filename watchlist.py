"""
watchlist.py — Foundations Trading

The play-sheet NOUN. One entry per ticker you are watching today, holding
whatever the sheet said plus whatever you want to remember about it.

WHY THIS IS NOT A BASKET
  A basket is a durable set of symbols the engines trade. A watchlist entry
  is dated, disposable, and carries a note written for a human to read. They
  are different lifetimes, so they are different files. A watchlist entry can
  name a basket, and the screener will happily watch both.

WHAT YOU TYPE, WHAT IS COMPUTED
  You type the ticker, and optionally the support/resistance zones and the
  note from the sheet. Everything else — PDH, PDL, PDC, PWH, PWL, PMH, PML,
  ATH, floor pivots, psych levels — is computed from bars by levels.py.

  This matters: the notes on a play sheet reference levels, and every level
  they can reference is derivable. So the note never has to be parsed. It is
  carried verbatim and shown to you when the screener surfaces that ticker.

  The minimum useful entry is a bare ticker.
"""

import os
import json
import re
import uuid
from datetime import datetime, date

import config_manager as cm


WATCHLIST_FILE = os.path.join(cm.DATA_DIR, 'watchlist.jsonl')
_LOCK = cm._WRITE_LOCK


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def _zone(v):
    """Accept 239.25/239, '239.25, 239', [239.25, 239] or a bare number.

    The sheet quotes pivots as bands because a level is a band, not a line,
    and both prices matter. All forms collapse to a list of floats.
    """
    if v in (None, ''):
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(_zone(x))
        return out
    txt = str(v).replace('$', '').strip()
    # Strip THOUSANDS separators before splitting. The sheet writes SNDK's
    # pivot as "1,870/1,857"; splitting on the comma first turns that into
    # four numbers (1, 870, 1, 857) and quietly ruins the level.
    txt = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', txt)
    out = []
    for p in re.split(r'[,;|/\s]+', txt):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(float(p))
        except ValueError:
            continue
    return out


def make_entry(*, ticker, support=None, resistance=None, note='',
               catalyst=False, mtf=False, for_date=None, source='manual',
               active=True, meta=None):
    t = str(ticker or '').strip().upper()
    if not t:
        raise ValueError('ticker required')
    d = for_date or date.today().isoformat()
    if isinstance(d, (datetime, date)):
        d = d.strftime('%Y-%m-%d')
    datetime.strptime(str(d)[:10], '%Y-%m-%d')

    return {
        'id': f'w_{uuid.uuid4().hex[:12]}',
        'ticker': t,
        'for_date': str(d)[:10],
        'support': sorted(_zone(support)),
        'resistance': sorted(_zone(resistance)),
        'note': (note or '').strip(),
        'catalyst': bool(catalyst),
        'mtf': bool(mtf),
        'active': bool(active),
        'source': source,
        'logged_at': _now_iso(),
        'meta': dict(meta or {}),
    }


def _append(entries):
    entries = list(entries)
    if not entries:
        return []
    with _LOCK:
        with open(WATCHLIST_FILE, 'a', encoding='utf-8') as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return entries


def add(**kwargs):
    return _append([make_entry(**kwargs)])[0]


def add_many(rows, for_date=None):
    """Add several at once. Replaces a same-ticker entry for the same date
    rather than stacking duplicates, since re-typing a row means correcting
    it, not adding a second one."""
    made = [make_entry(for_date=for_date, **r) for r in rows]
    keys = {(m['ticker'], m['for_date']) for m in made}
    kept = [e for e in read_all(active_only=False)
            if (e['ticker'], e['for_date']) not in keys]
    _rewrite(kept + made)
    return made


def read_all(for_date=None, active_only=True):
    if not os.path.exists(WATCHLIST_FILE):
        return []
    out = []
    with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if for_date and e.get('for_date') != for_date:
                continue
            if active_only and not e.get('active', True):
                continue
            out.append(e)
    out.sort(key=lambda e: (e.get('for_date', ''), e.get('ticker', '')))
    return out


def today(active_only=True):
    return read_all(date.today().isoformat(), active_only=active_only)


def _rewrite(entries):
    tmp = WATCHLIST_FILE + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WATCHLIST_FILE)


EDITABLE = ('ticker', 'support', 'resistance', 'note', 'catalyst', 'mtf',
            'active', 'for_date')


def update(entry_id, **changes):
    rows = read_all(active_only=False)
    patch = {k: v for k, v in changes.items() if k in EDITABLE}
    if 'support' in patch:
        patch['support'] = sorted(_zone(patch['support']))
    if 'resistance' in patch:
        patch['resistance'] = sorted(_zone(patch['resistance']))
    if 'ticker' in patch:
        patch['ticker'] = str(patch['ticker']).strip().upper()
    hit = None
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
    rows = read_all(active_only=False)
    keep = [e for e in rows if e['id'] != entry_id]
    if len(keep) == len(rows):
        return None
    gone = next(e for e in rows if e['id'] == entry_id)
    _rewrite(keep)
    return gone


def clear_date(for_date):
    rows = read_all(active_only=False)
    keep = [e for e in rows if e.get('for_date') != for_date]
    removed = len(rows) - len(keep)
    if removed:
        _rewrite(keep)
    return removed


def carry_forward(from_date, to_date=None):
    """Copy a day's list onto another day. Sheets repeat heavily week to
    week, so re-typing forty tickers every morning is the main reason a tool
    like this stops getting used."""
    to_date = to_date or date.today().isoformat()
    src = read_all(from_date, active_only=False)
    if not src:
        return []
    return add_many([{'ticker': e['ticker'], 'support': e['support'],
                      'resistance': e['resistance'], 'note': e['note'],
                      'catalyst': e['catalyst'], 'mtf': e['mtf'],
                      'source': 'carried'} for e in src], for_date=to_date)


# ─── BULK TEXT ENTRY ───────────────────────────────────────────────────────────

def parse_rows(text, for_date=None):
    """Parse pasted or typed rows into entries. Writes nothing.

    Accepts one ticker per line, optionally followed by support, resistance
    and a note, separated by tabs, pipes or commas:

        NBIS  230/228   232.85/236   Trade vs 1h MTF, long over or short under
        COIN  195.85/195 197.2/200   200 Psych Setup above
        HOOD

    A bare ticker is valid. Because every standard level is computed from
    bars, typing only the ticker still gives a full level set -- the pivots
    just add the sheet's own zones on top.
    """
    entries, problems = [], []
    for i, raw in enumerate((text or '').splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t')]
        elif '|' in line:
            parts = [p.strip() for p in line.split('|')]
        else:
            parts = line.split()
            # Re-join everything past the third field: the note has spaces.
            if len(parts) > 3:
                parts = parts[:3] + [' '.join(parts[3:])]
        parts = [p for p in parts if p != '']
        if not parts:
            continue

        ticker = parts[0].upper().strip(',')
        # A ticker starts with a letter. Without this, a stray numeric row
        # from a pasted sheet becomes a watchlist entry that can never
        # resolve to bars.
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', ticker):
            problems.append({'line': i, 'raw': line[:80],
                             'issue': f'{ticker!r} does not look like a ticker'})
            continue
        try:
            entries.append({
                'ticker': ticker,
                'support': parts[1] if len(parts) > 1 else None,
                'resistance': parts[2] if len(parts) > 2 else None,
                'note': parts[3] if len(parts) > 3 else '',
            })
        except ValueError as e:
            problems.append({'line': i, 'raw': line[:80], 'issue': str(e)})
    return entries, problems


def import_rows(text, for_date=None, dry_run=False):
    rows, problems = parse_rows(text, for_date)
    if not rows:
        problems.append({'line': 0, 'issue': 'no tickers found'})
        return {'ok': True, 'parsed': 0, 'imported': 0, 'problems': problems,
                'dry_run': dry_run}
    made = [] if dry_run else add_many(rows, for_date=for_date)
    return {'ok': True, 'parsed': len(rows), 'imported': len(made),
            'problems': problems, 'dry_run': dry_run,
            'tickers': [r['ticker'] for r in rows]}


def stats():
    rows = read_all(active_only=False)
    dates = sorted({e['for_date'] for e in rows})
    return {'entries': len(rows), 'dates': dates[-10:],
            'today': len(today()), 'file': WATCHLIST_FILE}
