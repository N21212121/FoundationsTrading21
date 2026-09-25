"""
heatmap.py — Foundations Trading

A CONSUMER of the journal noun, via pairing.py. Computes the entry-grade
by exit-grade grid, with optional layer filters.

THE GRID
  Rows are entry grade 0-4. Columns are exit grade 0-4 plus 'none' for an
  entry that never got an exit. Each cell reports trade count, total P/L,
  average P/L, win rate and median hold time.

  There is no row for "exit with no entry" because it cannot happen: the
  pairing layer opens a position on whatever fill comes first, so every exit
  necessarily has an entry.

LAYERS
  A layer is a filter on a condition recorded at the entry or the exit --
  the 5/12 position, the 1H multi-timeframe, whether S/R/P was followed. The
  grid is recomputed over the filtered subset. Layering is how you ask "does
  the entry grade still matter once I only look at trades taken above the
  34/50 cloud", which the flat grid cannot answer.

  Filters are AND-ed. A filter naming several values matches any of them.

SPARSE CELLS
  A cell holding one or two trades is an anecdote. summary() reports the
  share of cells below a threshold so the UI can gray them out rather than
  invite you to read noise as signal.
"""

import statistics

import pairing


GRADES = [0, 1, 2, 3, 4]
EXIT_KEYS = [0, 1, 2, 3, 4, 'none']

# Where a filter key is looked up. Prefixes pick a side; a bare key means
# "entry side", because that is the common case.
SIDE_PREFIXES = {'entry_': 'entry_conditions', 'exit_': 'exit_conditions'}


def _cond(leg, key):
    for prefix, field in SIDE_PREFIXES.items():
        if key.startswith(prefix):
            return (leg.get(field) or {}).get(key[len(prefix):])
    return (leg.get('entry_conditions') or {}).get(key)


def apply_filters(legs, filters=None, *, instrument=None, source=None,
                  direction=None, outcome=None, same_day=None,
                  tag_any=None, tag_none=None):
    """Narrow the leg set. Every argument is optional and AND-ed.

    filters: {condition_key: value or [values]} -- the layer filters.
    tag_any: keep legs carrying at least one of these tags (either side).
    tag_none: drop legs carrying any of these tags.
    """
    out = list(legs)

    for key, want in (filters or {}).items():
        if want in (None, '', []):
            continue
        wanted = {str(w) for w in (want if isinstance(want, (list, tuple, set))
                                   else [want])}
        out = [l for l in out if str(_cond(l, key)) in wanted]

    if instrument:
        out = [l for l in out if l.get('instrument') == instrument]
    if source:
        out = [l for l in out if l.get('source') == source]
    if direction:
        out = [l for l in out if l.get('direction') == direction]
    if outcome:
        out = [l for l in out if l.get('outcome') == outcome]
    if same_day is not None:
        out = [l for l in out if l.get('same_day') is same_day]

    if tag_any:
        want = set(tag_any)
        out = [l for l in out if want & _all_tags(l)]
    if tag_none:
        drop = set(tag_none)
        out = [l for l in out if not (drop & _all_tags(l))]

    return out


def _all_tags(leg):
    return set(leg.get('entry_tags_good') or []) | \
           set(leg.get('entry_tags_bad') or []) | \
           set(leg.get('exit_tags_good') or []) | \
           set(leg.get('exit_tags_bad') or [])


def _cell(legs):
    """Aggregate one bucket of legs. P/L stats ignore still-open legs.

    WIN RATE EXCLUDES SCRATCHES
      A scratch is a leg that closed at exactly the entry price, P/L of zero.
      It is neither a win nor a loss, so counting it in the denominator but
      never in the numerator drags the rate down for a trade that cost
      nothing: 8 wins, 2 losses and 1 scratch reads as 73% instead of 80%.

      win_rate is therefore wins / (wins + losses). `scratches` is reported
      alongside so the count is never hidden, and `n_priced` still covers all
      three so the totals stay honest.

      Note these are gross. A true scratch is a small loss once broker fees
      are counted; the Ledger tab is where fees live.
    """
    priced = [l for l in legs if l.get('pl') is not None]
    pls = [l['pl'] for l in priced]
    holds = [l['hold_minutes'] for l in legs
             if l.get('hold_minutes') is not None]
    wins = sum(1 for p in pls if p > 0)
    losses = sum(1 for p in pls if p < 0)
    scratches = sum(1 for p in pls if p == 0)
    decided = wins + losses
    return {
        'n': len(legs),
        'n_priced': len(priced),
        'total_pl': round(sum(pls), 2) if pls else None,
        'avg_pl': round(sum(pls) / len(pls), 2) if pls else None,
        'wins': wins,
        'losses': losses,
        'scratches': scratches,
        'win_rate': round(wins / decided, 4) if decided else None,
        'median_hold_min': round(statistics.median(holds), 1) if holds else None,
        'open_legs': len(legs) - len(priced),
    }


def build(legs=None, filters=None, sparse_below=3, **narrow):
    """Compute the grid. Returns cells, margins and a summary.

    sparse_below marks any cell with fewer than this many legs, so the UI can
    render it as unreliable instead of as a finding.
    """
    if legs is None:
        legs = pairing.build_legs()
    legs = apply_filters(legs, filters, **narrow)

    buckets = {(e, x): [] for e in GRADES for x in EXIT_KEYS}
    ungraded = 0
    for l in legs:
        e, x = l.get('entry_grade'), l.get('exit_grade')
        if e is None:
            ungraded += 1
            continue
        if x is None:
            ungraded += 1
            continue
        key = (e, x if x in EXIT_KEYS else 'none')
        if key not in buckets:
            ungraded += 1
            continue
        buckets[key].append(l)

    cells = {}
    for (e, x), group in buckets.items():
        c = _cell(group)
        c['sparse'] = 0 < c['n'] < sparse_below
        c['entry_grade'], c['exit_grade'] = e, x
        cells[f'{e}|{x}'] = c

    row_margin = {str(e): _cell([l for l in legs
                                 if l.get('entry_grade') == e])
                  for e in GRADES}
    col_margin = {str(x): _cell([l for l in legs
                                 if (l.get('exit_grade') if
                                     l.get('exit_grade') in EXIT_KEYS
                                     else 'none') == x])
                  for x in EXIT_KEYS}

    placed = sum(c['n'] for c in cells.values())
    sparse_cells = sum(1 for c in cells.values() if c['sparse'])
    populated = sum(1 for c in cells.values() if c['n'] > 0)

    return {
        'grades': GRADES,
        'exit_keys': [str(k) for k in EXIT_KEYS],
        'cells': cells,
        'row_margin': row_margin,
        'col_margin': col_margin,
        'summary': {
            'legs_in_grid': placed,
            'legs_excluded_ungraded': ungraded,
            'overall': _cell(legs),
            'populated_cells': populated,
            'sparse_cells': sparse_cells,
            'sparse_below': sparse_below,
        },
        'filters_applied': filters or {},
    }


# Display names and, more importantly, DISPLAY ORDER. Sorting these
# alphabetically puts 34/50 above 5/12 and 1D above 1H, which is neither the
# order the clouds stack in nor the order they are read in.
CONDITION_LABELS = [
    ('srp',        'S/R/P Followed'),
    ('ema_5_12',   'EMA 5/12 10m'),
    ('ema_34_50',  'EMA 34/50 10m'),
    ('mtf_1h',     'MTF 1HR 34/50'),
    ('mtf_1d',     'MTF 1D 20/21'),
    ('trend',      'Trend (engine)'),
    ('trigger',    'Trigger (engine)'),
    ('exit_kind',  'Exit kind'),
]
CONDITION_ORDER = [k for k, _ in CONDITION_LABELS]
CONDITION_NAME = dict(CONDITION_LABELS)


def layer_options(legs=None):
    """Every filterable value present in the current leg set, for the UI.

    `groups` is ordered for display: entry side first, then exit, each in
    cloud order rather than alphabetical. `conditions` is kept as a flat map
    for any caller that just wants the values.
    """
    if legs is None:
        legs = pairing.build_legs()
    out = {}
    for side, field in (('entry', 'entry_conditions'),
                        ('exit', 'exit_conditions')):
        for l in legs:
            for k, v in (l.get(field) or {}).items():
                if v in (None, ''):
                    continue
                out.setdefault(f'{side}_{k}', set()).add(str(v))

    def rank(full_key):
        side, _, base = full_key.partition('_')
        try:
            return (0 if side == 'entry' else 1, CONDITION_ORDER.index(base))
        except ValueError:
            return (0 if side == 'entry' else 1, len(CONDITION_ORDER))

    groups = []
    for full_key in sorted(out, key=rank):
        side, _, base = full_key.partition('_')
        groups.append({
            'key': full_key, 'side': side, 'base': base,
            'label': CONDITION_NAME.get(base, base.replace('_', ' ')),
            'values': sorted(out[full_key]),
        })

    tags = set()
    for l in legs:
        tags |= _all_tags(l)
    return {'groups': groups,
            'conditions': {k: sorted(v) for k, v in sorted(out.items())},
            'tags': sorted(tags),
            'instruments': sorted({l.get('instrument') for l in legs
                                   if l.get('instrument')}),
            'sources': sorted({l.get('source') for l in legs
                               if l.get('source')}),
            'directions': sorted({l.get('direction') for l in legs
                                  if l.get('direction')}),
            'outcomes': sorted({l.get('outcome') for l in legs
                                if l.get('outcome')})}


def compare(split_key, legs=None, **narrow):
    """Build one grid per value of a condition, to see a layer's effect.

    This is what answers "does holding overnight change the shape of the
    grid, or just shift it down" -- compare('exit_kind') or a tag split.
    """
    if legs is None:
        legs = pairing.build_legs()
    legs = apply_filters(legs, None, **narrow)
    values = sorted({str(_cond(l, split_key)) for l in legs
                     if _cond(l, split_key) not in (None, '')})
    return {v: build(legs, filters={split_key: v}) for v in values}


if __name__ == '__main__':
    import json
    g = build()
    print(json.dumps(g['summary'], indent=2))
