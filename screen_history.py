"""
screen_history.py — Foundations Trading

What the screener saw, kept so the journal can be joined to it later.

WHY
  Five of the screener's seven components -- proximity, room, level quality,
  confluence, volume -- describe things the journal never recorded at fill
  time. Their weights are therefore judgement with nothing to check them
  against. Logging each reading means every future trade on a screened
  ticker arrives with the screener's view of it attached, and after enough
  of them the weights can be suggested from results instead of guessed.

STORAGE
  DATA_DIR/screen_history/YYYY-MM.jsonl, one line per ticker reading. The
  screener scans every minute; a reading is kept only when RECORD_EVERY_MIN
  has passed for that ticker or its state changed, which is roughly one per
  closed 6-minute bar. Monthly files keep any one of them small and make old
  history easy to drop by hand.

TIMES
  Naive Eastern wall-clock, the same convention journal.filled_at uses, so a
  reading and a fill compare directly whatever the machine's timezone is.
"""

import os
import json
import math
import bisect
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config_manager as cm

ET = ZoneInfo('America/New_York')
HISTORY_DIR = os.path.join(cm.DATA_DIR, 'screen_history')

RECORD_EVERY_MIN = 6       # one reading per ticker per closed 6-minute bar
MATCH_WINDOW_MIN = 10      # a reading older than this says nothing about a fill
MIN_TRADES = 30            # below this, calibration only reports progress

_lock = threading.Lock()
_last = {}                 # ticker -> (datetime, state) of the last kept reading


def _now_et():
    return datetime.now(ET).replace(tzinfo=None)


def _path(dt):
    return os.path.join(HISTORY_DIR, dt.strftime('%Y-%m') + '.jsonl')


def record(result, now=None):
    """Append this scan's readings. Returns how many were kept."""
    if not result:
        return 0
    now = now or _now_et()
    rows = list(result.get('board') or []) + list(result.get('bench') or [])
    keep = []
    with _lock:
        for r in rows:
            t = r.get('ticker')
            if not t or not r.get('components'):
                continue
            prev = _last.get(t)
            if prev and prev[1] == r.get('state') and \
                    now - prev[0] < timedelta(minutes=RECORD_EVERY_MIN):
                continue
            _last[t] = (now, r.get('state'))
            keep.append({
                'at': now.isoformat(timespec='seconds'),
                'ticker': t,
                'price': r.get('price'),
                'direction': r.get('direction'),
                'state': r.get('state'),
                'score': r.get('score'),
                'components': r.get('components'),
                'journal_conditions': r.get('journal_conditions'),
            })
        if not keep:
            return 0
        os.makedirs(HISTORY_DIR, exist_ok=True)
        with open(_path(now), 'a', encoding='utf-8') as f:
            for k in keep:
                f.write(json.dumps(k) + '\n')
    return len(keep)


def read_all():
    """Every stored reading, grouped by ticker and sorted by time."""
    by = {}
    if not os.path.isdir(HISTORY_DIR):
        return by
    for name in sorted(os.listdir(HISTORY_DIR)):
        if not name.endswith('.jsonl'):
            continue
        with open(os.path.join(HISTORY_DIR, name), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue      # a torn last line from a crash mid-write
                by.setdefault(r['ticker'].upper(), []).append(r)
    for rows in by.values():
        rows.sort(key=lambda r: r['at'])
    return by


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None           # a component that never varied says nothing
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def match_trades(legs=None, readings=None, window_min=MATCH_WINDOW_MIN):
    """Pair each closed trade with the screener's last reading before entry.

    Only readings for the same ticker, at most window_min old at the fill,
    and pointing the same way as the trade count. A reading of the long
    break says nothing about a put bought on the same ticker.
    """
    import pairing
    import conditions as CO
    legs = legs if legs is not None else pairing.build_legs()
    readings = readings if readings is not None else read_all()
    out = []
    for l in legs:
        if l.get('pl') is None or not l.get('entry_at'):
            continue
        rows = readings.get((l.get('ticker') or '').upper())
        if not rows:
            continue
        at = str(l['entry_at'])[:19]
        i = bisect.bisect_right([r['at'] for r in rows], at) - 1
        if i < 0:
            continue
        r = rows[i]
        age = (datetime.fromisoformat(at) - datetime.fromisoformat(r['at']))
        if age > timedelta(minutes=window_min):
            continue
        want = 'long' if CO.bias(l) == 'bull' else 'short'
        if r.get('direction') != want:
            continue
        out.append({'leg': l, 'reading': r})
    return out


def calibrate(current_weights, min_trades=MIN_TRADES):
    """Suggest weights from how each component related to trade P/L.

    For every component, the correlation between its value at entry and the
    trade's P/L. Components that tracked P/L upward share the weight in
    proportion to that correlation; those that did not get zero.

    'edge' is left at its current weight and excluded from the fit. Its
    value is computed FROM the journal's P/L, so correlating it against that
    same P/L would reward it for agreeing with itself.
    """
    import screener as SC
    pairs = match_trades()
    comps = [k for k in SC.COMPONENTS if k != 'edge']
    stats = {}
    for k in comps:
        xs = [p['reading']['components'].get(k, 0.0) or 0.0 for p in pairs]
        ys = [p['leg']['pl'] for p in pairs]
        r = _pearson(xs, ys)
        stats[k] = {'r': round(r, 3) if r is not None else None}

    out = {'matched': len(pairs), 'min_trades': min_trades,
           'window_min': MATCH_WINDOW_MIN, 'components': stats,
           'ready': len(pairs) >= min_trades, 'suggested': None}
    if not out['ready']:
        return out

    edge_w = float((current_weights or {}).get('edge', 0) or 0)
    pos = {k: max(stats[k]['r'] or 0.0, 0.0) for k in comps}
    total = sum(pos.values())
    if total <= 0:
        out['note'] = 'No component tracked P/L upward; nothing to suggest.'
        return out
    rest = max(0.0, 1.0 - edge_w)
    sug = {k: rest * pos[k] / total for k in comps}
    sug['edge'] = edge_w
    out['suggested'] = SC.normalize_weights(sug)
    return out
