"""
conditions.py — Foundations Trading

A CONSUMER of the journal noun, via pairing.py. Ranks every condition and
tag recorded on graded trades by how the trades carrying it performed,
measured against every trade that did not carry it.

WITH / AGAINST THE TRADE
  The journal records the cloud conditions as raw price position: above,
  below, at. Raw position means opposite things for a call and a put -- price
  below the 34/50 is the trend working FOR a long put and AGAINST a long call.
  Ranking the raw values would average those together into mush.

  So each cloud condition is restated relative to the trade's bias:
    'with'     price on the side the trade profits from
    'against'  price on the side the trade loses from
    'at'       at the cloud, no side
  A bullish trade is a long call, a short put, or long shares; a bearish one
  is the mirror. S/R/P, engine trend/trigger and tags carry no direction and
  are used as recorded.

SCORING
  Small samples lie. Three trades averaging +$60 is not an edge. Each factor's
  average is shrunk toward the book average by PRIOR pseudo-trades before it
  is compared:

      shrunk = (sum + PRIOR * book_avg) / (n + PRIOR)
      score  = shrunk - book_avg

  A factor needs many trades before its score moves far from zero, so the
  ranking favours consistent evidence over a lucky handful. The same shrink
  is applied to win rate and P/L %.

  `t` is a Welch t-statistic of the factor's P/L against the rest of the
  book. It drives the confidence label only, not the ranking:
      thin    fewer than THIN trades -- not enough to say anything
      strong  |t| >= 2   unlikely to be noise
      some    |t| >= 1
      noise   indistinguishable from the rest

  None of this is predictive. It describes what the journal holds.

EXIT-SIDE FACTORS ARE PARTLY HINDSIGHT
  Exit tags such as 'Quick Exit' or 'Early Exit' are judged after the result
  is known, so they will correlate with P/L by construction. They are ranked
  in their own groups so they are never mistaken for entry setups.
"""

import math
from itertools import combinations

import journal as jn
import pairing
import heatmap as hm


PRIOR = 10         # pseudo-trades pulling each factor toward the book average
THIN = 8           # below this many trades the confidence label is 'thin'

CLOUD_KEYS = ('ema_5_12', 'ema_34_50', 'mtf_1h', 'mtf_1d')
ALIGN_LABEL = {'with': 'with trade', 'against': 'against trade', 'at': 'at cloud'}

GROUPS = [
    ('entry',      'Entry condition'),
    ('entry_tag',  'Entry tag'),
    ('combo',      'Entry combo'),
    ('trade',      'Trade type'),
    ('exit',       'Exit condition'),
    ('exit_tag',   'Exit tag'),
]


def bias(leg):
    """'bull' or 'bear' for the direction the trade profits in."""
    long_ = leg.get('direction') != 'short'
    parts = jn.occ_parts(leg.get('symbol'))
    if parts is None:
        return 'bull' if long_ else 'bear'
    call = parts[2] == 'C'
    return 'bull' if call == long_ else 'bear'


def _align(raw, b):
    v = str(raw or '').strip().lower()
    if v in ('above', 'below'):
        up = v == 'above'
        return 'with' if up == (b == 'bull') else 'against'
    if v in ('at', 'inside'):
        return 'at'
    return None          # blank, or a mixed value such as 'at, above'


def _cond_factors(conds, side, b):
    out = []
    for key, raw in (conds or {}).items():
        if raw in (None, ''):
            continue
        name = hm.CONDITION_NAME.get(key, key.replace('_', ' '))
        if key in CLOUD_KEYS:
            a = _align(raw, b)
            if a is None:
                continue
            out.append((side, f'{side}:{key}={a}', name, ALIGN_LABEL[a]))
        else:
            out.append((side, f'{side}:{key}={raw}', name, str(raw)))
    return out


def factors(leg):
    """Every (group, id, label, value) a leg carries."""
    b = bias(leg)
    out = []
    entry = _cond_factors(leg.get('entry_conditions'), 'entry', b)
    out += entry
    out += _cond_factors(leg.get('exit_conditions'), 'exit', b)

    for side in ('entry', 'exit'):
        for kind in ('good', 'bad'):
            for t in leg.get(f'{side}_tags_{kind}') or []:
                out.append((f'{side}_tag', f'{side}_tag:{t}', t,
                            'marked good' if kind == 'good' else 'marked bad'))

    out.append(('trade', 'trade:bias=' + b, 'Bias',
                'bullish' if b == 'bull' else 'bearish'))
    out.append(('trade', 'trade:inst=' + str(leg.get('instrument')),
                'Instrument', str(leg.get('instrument'))))

    # Pairs of entry conditions: "34/50 with trade AND 1H with trade".
    for a, c in combinations(sorted(entry, key=lambda f: f[1]), 2):
        out.append(('combo', f'combo:{a[1]}&{c[1]}',
                    f'{a[2]} + {c[2]}', f'{a[3]} + {c[3]}'))
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _welch_t(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = _mean(a), _mean(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((ma - mb) / se, 2) if se else None


def rank(legs=None, filters=None, min_n=1, **narrow):
    """Score every factor. Returns the book baseline and one row per factor."""
    if legs is None:
        legs = pairing.build_legs()
    legs = hm.apply_filters(legs, filters, **narrow)
    legs = [l for l in legs if l.get('pl') is not None]

    pls = [l['pl'] for l in legs]
    pcts = [l['pl_pct'] for l in legs if l.get('pl_pct') is not None]
    wins = sum(1 for p in pls if p > 0)
    losses = sum(1 for p in pls if p < 0)
    book = {
        'n': len(legs),
        'avg_pl': round(_mean(pls), 2) if pls else None,
        'avg_pct': round(_mean(pcts), 4) if pcts else None,
        'win_rate': round(wins / (wins + losses), 4) if wins + losses else None,
        'total_pl': round(sum(pls), 2),
    }
    if not legs:
        return {'book': book, 'rows': [], 'groups': GROUPS,
                'prior': PRIOR, 'thin': THIN}

    members = {}
    meta = {}
    for i, l in enumerate(legs):
        for group, fid, label, value in factors(l):
            members.setdefault(fid, set()).add(i)
            meta[fid] = (group, label, value)

    b_pl, b_pct, b_wr = _mean(pls), _mean(pcts) if pcts else 0.0, book['win_rate'] or 0.0
    rows = []
    for fid, idx in members.items():
        n = len(idx)
        if n < min_n:
            continue
        inn = [legs[i] for i in idx]
        rest = [l for i, l in enumerate(legs) if i not in idx]
        f_pl = [l['pl'] for l in inn]
        f_pct = [l['pl_pct'] for l in inn if l.get('pl_pct') is not None]
        w = sum(1 for p in f_pl if p > 0)
        lo = sum(1 for p in f_pl if p < 0)
        t = _welch_t(f_pl, [l['pl'] for l in rest])

        if n < THIN or t is None:
            conf = 'thin'
        elif abs(t) >= 2:
            conf = 'strong'
        elif abs(t) >= 1:
            conf = 'some'
        else:
            conf = 'noise'

        group, label, value = meta[fid]
        rows.append({
            'id': fid, 'group': group, 'label': label, 'value': value,
            'n': n, 'wins': w, 'losses': lo, 'scratches': n - w - lo,
            'win_rate': round(w / (w + lo), 4) if w + lo else None,
            'avg_pl': round(_mean(f_pl), 2),
            'total_pl': round(sum(f_pl), 2),
            'avg_pct': round(_mean(f_pct), 4) if f_pct else None,
            'rest_avg_pl': round(_mean([l['pl'] for l in rest]), 2) if rest else None,
            'score_pl': round((sum(f_pl) + PRIOR * b_pl) / (n + PRIOR) - b_pl, 2),
            'score_pct': (round((sum(f_pct) + PRIOR * b_pct) / (len(f_pct) + PRIOR)
                                - b_pct, 4) if f_pct else 0.0),
            'score_wr': round((w + PRIOR * b_wr) / (w + lo + PRIOR) - b_wr, 4),
            't': t, 'confidence': conf,
        })
    rows.sort(key=lambda r: -r['score_pl'])
    return {'book': book, 'rows': rows, 'groups': GROUPS,
            'prior': PRIOR, 'thin': THIN}


# ─── LIVE LOOKUP (the screener's journal-edge component) ───────────────────────

_EDGE_CACHE = {'key': None, 'table': None}


def edge_table():
    """Entry-side factor scores, rebuilt only when the journal file changes.

    The screener asks on every pass (once a minute); re-pairing the whole
    journal each time would be wasted work when nothing was graded.
    """
    import os
    try:
        st = os.stat(jn.JOURNAL_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and key == _EDGE_CACHE['key']:
        return _EDGE_CACHE['table']

    r = rank()
    rows = {row['id']: row for row in r['rows']
            if row['group'] in ('entry', 'combo') and row['n'] >= THIN}
    scale = max([abs(row['score_pl']) for row in rows.values()] or [0])
    table = {'rows': rows, 'scale': scale, 'book': r['book']}
    _EDGE_CACHE.update(key=key, table=table)
    return table


def edge_for(conditions, direction, table=None):
    """Score a set of cloud conditions the way the journal says they traded.

    conditions uses journal vocabulary ({'ema_5_12': 'above', ...}); direction
    is 'long' or 'short' for the move being considered. Every entry factor
    and entry combo the setup matches is looked up, thin ones skipped, and
    their shrunk P/L edges averaged.

    Returns {'edge': $ per trade vs the book or None, 'sub': 0-1 where 0.5 is
    book average, 'matched': [...]}.
    """
    table = table if table is not None else edge_table()
    leg = {'symbol': '', 'direction': 'short' if direction == 'short' else 'long',
           'entry_conditions': conditions}
    matched = []
    for group, fid, label, value in factors(leg):
        if group not in ('entry', 'combo'):
            continue
        row = table['rows'].get(fid)
        if row:
            matched.append({'id': fid, 'label': label, 'value': value,
                            'edge': row['score_pl'], 'n': row['n'],
                            'confidence': row['confidence']})
    if not matched:
        return {'edge': None, 'sub': 0.5, 'matched': []}
    edge = sum(m['edge'] for m in matched) / len(matched)
    scale = table['scale'] or 1.0
    sub = 0.5 + 0.5 * max(-1.0, min(1.0, edge / scale))
    matched.sort(key=lambda m: -abs(m['edge']))
    return {'edge': round(edge, 2), 'sub': round(sub, 4), 'matched': matched}


if __name__ == '__main__':
    r = rank(min_n=5)
    print('book', r['book'])
    for row in r['rows'][:12] + r['rows'][-6:]:
        print(f"{row['score_pl']:>8} {row['confidence']:>6} n={row['n']:<3} "
              f"{row['group']:<9} {row['label']}: {row['value']}")
