"""
plays.py — Foundations Trading

Per-ticker PLAYS, built from a fixed vocabulary, scored by how many of their
own conditions currently hold.

THE SHAPE
  A play is: a bias, one TRIGGER, and any number of CONDITIONS.

      bias      long | short
      trigger   the thing that has to happen for the play to be live at all
                ("price over PMH", "5/12 curls up")
      condition the gates that make it good or bad
                ("1h MTF above", "price over PDC", "volume above average")

  A ticker can hold several plays, ordered. Move 1 is the primary read, move
  2 the alternative, and so on. They are evaluated independently; the board
  shows the best LIVE one.

WHY THIS AND NOT A WEIGHTED SCORE
  A single 0-100 score across every ticker encodes one universal theory of a
  good setup, tuned by whoever set the weights. A play encodes what THIS
  stock is supposed to do today, in your words. Nothing is learned, nothing
  is fitted, and a wrong score is traceable to a condition you wrote rather
  than to a coefficient.

SCORING IS GPA, NOT PERCENT
  Each condition resolves to met (+1), violated (-1), or unknown (0).
  Unknown never counts against: missing data is not evidence.

      grade = 2 + 2 * (met - violated) / decidable

  Everything holding is a 4. Everything against is a 0. Nothing decidable,
  or an even split, is a 2. The same 0-4 scale the journal grades in, so a
  play at 3 and an entry graded 3 mean comparable things.
"""

import os
import json
import re
import uuid
from datetime import datetime, date

import config_manager as cm


PLAYS_FILE = os.path.join(cm.DATA_DIR, 'plays.jsonl')
_LOCK = cm._WRITE_LOCK


# ─── VOCABULARY ────────────────────────────────────────────────────────────────
# Every dropdown in the builder is generated from these. Adding a term here
# adds it to the UI; there is no second list to keep in sync.

BIASES = ('long', 'short')

# kind -> what the reference can be
LEVEL_REFS = ['PMH', 'PML', 'PDH', 'PDL', 'PDC', 'PDO', 'PWH', 'PWL',
              'PMonH', 'PMonL', 'ATH', 'P', 'R1', 'S1', 'R2', 'S2',
              'manual_support', 'manual_resistance', 'custom']
CLOUD_REFS = ['5/12', '34/50']
MTF_REFS = ['1h 34/50', 'daily 20/21', 'daily 50/55']

OPS = {
    'level': ['over', 'under', 'near'],
    'cloud': ['above', 'below', 'inside', 'curls up', 'curls down'],
    'mtf':   ['above', 'below'],
    'volume': ['above average', 'below average'],
}

KINDS = tuple(OPS)

LABELS = {
    'PMH': 'pre-market high', 'PML': 'pre-market low',
    'PMonH': 'prior month high', 'PMonL': 'prior month low',
    'PDH': 'prior day high', 'PDL': 'prior day low',
    'PDC': 'prior day close', 'PDO': 'prior day open',
    'PWH': 'prior week high', 'PWL': 'prior week low',
    'ATH': 'all-time high', 'P': 'floor pivot',
    'R1': 'resistance 1', 'S1': 'support 1',
    'R2': 'resistance 2', 'S2': 'support 2',
    'manual_support': 'your support zone',
    'manual_resistance': 'your resistance zone',
    'custom': 'a price you type',
}

# How near counts as "near", in ATR. Same threshold the board uses.
NEAR_ATR = 0.20


def vocabulary():
    """Everything the builder needs to render its dropdowns."""
    return {
        'biases': list(BIASES),
        'kinds': list(KINDS),
        'ops': {k: list(v) for k, v in OPS.items()},
        'refs': {'level': LEVEL_REFS, 'cloud': CLOUD_REFS,
                 'mtf': MTF_REFS, 'volume': ['session volume']},
        'labels': dict(LABELS),
    }


# ─── CONDITIONS ────────────────────────────────────────────────────────────────

def make_condition(kind, ref, op, value=None):
    kind = str(kind or '').strip()
    if kind not in OPS:
        raise ValueError(f'kind must be one of {KINDS}, got {kind!r}')
    op = str(op or '').strip()
    if op not in OPS[kind]:
        raise ValueError(f'{kind} op must be one of {OPS[kind]}, got {op!r}')
    ref = str(ref or '').strip()
    if kind == 'level' and ref not in LEVEL_REFS:
        raise ValueError(f'level ref must be one of {LEVEL_REFS}')
    if kind == 'cloud' and ref not in CLOUD_REFS:
        raise ValueError(f'cloud ref must be one of {CLOUD_REFS}')
    if kind == 'mtf' and ref not in MTF_REFS:
        raise ValueError(f'mtf ref must be one of {MTF_REFS}')
    if kind == 'level' and ref == 'custom':
        if value in (None, ''):
            raise ValueError('a custom level needs a price')
        value = float(value)
    return {'kind': kind, 'ref': ref, 'op': op,
            'value': float(value) if value not in (None, '') else None}


def describe(cond):
    """Plain English, for the chip in the UI and the board card."""
    if not cond:
        return ''
    k, r, o, v = cond['kind'], cond['ref'], cond['op'], cond.get('value')
    if k == 'level':
        if r != 'custom':
            target = r
        elif isinstance(v, str):
            # An unfilled preset placeholder: show what it is asking for
            # rather than blowing up formatting a template as a number.
            target = v.strip('${}').replace('_', ' ')
        elif v is None:
            target = '(price)'
        else:
            target = f'{v:g}'
        return f'price {o} {target}'
    if k == 'cloud':
        return f'{r} {o}' if o.startswith('curls') else f'price {o} {r} cloud'
    if k == 'mtf':
        return f'{r} MTF {o}'
    return f'volume {o}'


def evaluate_condition(cond, state):
    """met (True) / violated (False) / unknown (None).

    `state` is the measurement bundle the screener already builds: price,
    atr, the level set, cloud states, fresh-cross flags, MTF states and
    relative volume. Nothing is inferred here that is not measured there --
    a missing input returns None rather than a guess.
    """
    k, r, o = cond['kind'], cond['ref'], cond['op']
    price = state.get('price')

    if k == 'level':
        target = cond.get('value') if r == 'custom' else state.get('levels', {}).get(r)
        if target is None or price is None:
            return None
        if o == 'over':
            return price > target
        if o == 'under':
            return price < target
        atr = state.get('atr')
        if not atr:
            return None
        return abs(price - target) / atr <= NEAR_ATR

    if k == 'cloud':
        if o in ('curls up', 'curls down'):
            fresh = state.get('fresh', {}).get(r)
            if fresh is None:
                return None
            return fresh == ('up' if o == 'curls up' else 'down')
        pos = state.get('cloud', {}).get(r)
        if pos is None:
            return None
        if o == 'above':
            return pos == 'above'
        if o == 'below':
            return pos == 'below'
        return pos == 'inside'

    if k == 'mtf':
        st = state.get('mtf', {}).get(r)
        if st is None:
            return None
        return st == ('up' if o == 'above' else 'down')

    rv = state.get('rel_volume')
    if rv is None:
        return None
    return rv >= 1.0 if o == 'above average' else rv < 1.0


# ─── PLAYS ─────────────────────────────────────────────────────────────────────

def make_branch(*, bias, trigger, conditions=None, label=''):
    """One arm of a play: IF this trigger holds, go this way.

    Branches are ordered and read as if/else. The first one whose trigger
    holds is the play's current read; the rest are what you would do
    instead.
    """
    if bias not in BIASES:
        raise ValueError(f'bias must be one of {BIASES}')
    if not trigger:
        raise ValueError('a branch needs a trigger')
    return {
        'bias': bias,
        'label': (label or '').strip(),
        'trigger': trigger,
        'conditions': list(conditions or []),
    }


def _as_branches(play):
    """Read branches off a play, old shape or new.

    Plays written before branches existed carry bias/trigger/conditions at
    the top level. Rather than migrate the file and risk mangling records,
    those are presented as a single branch on read. Old plays keep working
    and are upgraded the next time they are saved.
    """
    if play.get('branches'):
        return play['branches']
    if play.get('trigger'):
        return [{'bias': play.get('bias', 'long'),
                 'label': play.get('label', ''),
                 'trigger': play['trigger'],
                 'conditions': play.get('conditions', [])}]
    return []


def make_play(*, ticker, branches=None, bias=None, trigger=None,
              conditions=None, label='', note='', order=1, for_date=None,
              active=True):
    """A play is a ticker, a note, and one or more ordered branches.

    The single-branch keyword form (bias/trigger/conditions) is still
    accepted so existing callers and presets do not have to change shape.
    """
    t = str(ticker or '').strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', t):
        raise ValueError(f'{ticker!r} does not look like a ticker')

    if branches:
        built = [make_branch(**b) if not isinstance(b, dict) or 'bias' not in b
                 else make_branch(bias=b['bias'], trigger=b.get('trigger'),
                                  conditions=b.get('conditions'),
                                  label=b.get('label', ''))
                 for b in branches]
    elif trigger:
        built = [make_branch(bias=bias or 'long', trigger=trigger,
                             conditions=conditions, label=label)]
    else:
        raise ValueError('a play needs at least one branch')

    d = for_date or date.today().isoformat()
    if isinstance(d, (datetime, date)):
        d = d.strftime('%Y-%m-%d')
    datetime.strptime(str(d)[:10], '%Y-%m-%d')

    return {
        'id': f'p_{uuid.uuid4().hex[:12]}',
        'ticker': t,
        'for_date': str(d)[:10],
        'order': int(order),
        'label': (label or '').strip(),
        'branches': built,
        'note': (note or '').strip(),
        'active': bool(active),
        'logged_at': datetime.now().isoformat(timespec='seconds'),
    }


def grade_branch(branch, state):
    """Evaluate one branch. Live when its trigger holds; graded on the rest."""
    trig = evaluate_condition(branch['trigger'], state)
    rows = [{'what': describe(branch['trigger']), 'result': trig,
             'role': 'trigger', **branch['trigger']}]

    met = violated = unknown = 0
    for c in branch.get('conditions', []):
        r = evaluate_condition(c, state)
        rows.append({'what': describe(c), 'result': r, 'role': 'condition', **c})
        if r is True:
            met += 1
        elif r is False:
            violated += 1
        else:
            unknown += 1

    decidable = met + violated
    grade = 2.0 if not decidable else 2.0 + 2.0 * (met - violated) / decidable
    return {
        'bias': branch['bias'],
        'label': branch.get('label', ''),
        'live': trig is True,
        'trigger_result': trig,
        'grade': round(max(0.0, min(4.0, grade)), 2),
        'met': met, 'violated': violated, 'unknown': unknown,
        'decidable': decidable,
        'rows': rows,
    }


def grade_play(play, state):
    """Evaluate every branch and report the play's current read.

    Branches are if/else in the order you wrote them, so the FIRST live one
    is the read even if a later branch would grade higher. That is the point
    of ordering them: a higher-grading branch whose trigger has not fired is
    describing something that is not happening.
    """
    branches = [grade_branch(b, state) for b in _as_branches(play)]
    live = [b for b in branches if b['live']]
    active = live[0] if live else None

    return {
        'play_id': play['id'],
        'ticker': play['ticker'],
        'order': play.get('order', 1),
        'label': play.get('label', ''),
        'note': play.get('note', ''),
        'branches': branches,
        'active_index': branches.index(active) if active else None,
        # Flattened to the active branch so callers that predate branching
        # keep reading the same fields.
        'bias': active['bias'] if active else (
            branches[0]['bias'] if branches else None),
        'live': active is not None,
        'grade': active['grade'] if active else (
            max((b['grade'] for b in branches), default=2.0)),
        'met': active['met'] if active else 0,
        'violated': active['violated'] if active else 0,
        'unknown': active['unknown'] if active else 0,
        'decidable': active['decidable'] if active else 0,
        'rows': active['rows'] if active else (
            branches[0]['rows'] if branches else []),
        'trigger_result': active['trigger_result'] if active else None,
    }


def describe_play(play):
    """The play as an if/else sentence, for cards and lists."""
    parts = []
    for i, b in enumerate(_as_branches(play)):
        head = 'IF' if i == 0 else 'ELSE IF'
        conds = [describe(c) for c in b.get('conditions', [])]
        parts.append(f"{head} {describe(b['trigger'])} go {b['bias']}"
                     + (f" ({', '.join(conds)})" if conds else ''))
    return ', '.join(parts)


def grade_ticker(plays, state):
    """Grade every play on a ticker, best live one first.

    A live play always outranks a dormant one regardless of grade: a 4 whose
    trigger has not fired is a description of something that is not
    happening. Among live plays, higher grade wins, then the order you set.
    """
    graded = [grade_play(p, state) for p in plays]
    graded.sort(key=lambda g: (not g['live'], -g['grade'], g['order']))
    return graded


# ─── PRESETS ───────────────────────────────────────────────────────────────────
#
# Seeded from the shapes the play sheet actually repeats. Across 38 notes,
# "trade vs MTF, long over or short under, use MTF Cloud Rules" appears 6
# times near-verbatim and "trade vs 1h MTF, long as long as it holds" 4 times
# identically. Building those from empty dropdowns 80 times a week is how a
# tool like this gets abandoned by the second Tuesday.
#
# A preset produces BOTH sides where the sheet implies both. "Long over or
# short under" is one sentence describing two plays, and writing only the
# long half means the short never gets evaluated.

def _c(kind, ref, op, value=None):
    return {'kind': kind, 'ref': ref, 'op': op, 'value': value}


BUILTIN_PRESETS = [
    {
        'id': 'mtf_cloud_rules',
        'name': 'MTF Cloud Rules (1h)',
        'description': 'Trade vs the 1h 34/50. Long over it, short under it.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': 'Long over 1h MTF', 'order': 1,
             'trigger': _c('mtf', '1h 34/50', 'above'),
             'conditions': [_c('cloud', '34/50', 'above')]},
            {'bias': 'short', 'label': 'Short under 1h MTF', 'order': 2,
             'trigger': _c('mtf', '1h 34/50', 'below'),
             'conditions': [_c('cloud', '34/50', 'below')]},
        ],
    },
    {
        'id': 'double_mtf',
        'name': 'Double MTF',
        'description': 'Both the 1h 34/50 and the daily 20/21 on the same side.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': 'Long over double MTF', 'order': 1,
             'trigger': _c('mtf', '1h 34/50', 'above'),
             'conditions': [_c('mtf', 'daily 20/21', 'above'),
                            _c('cloud', '34/50', 'above')]},
            {'bias': 'short', 'label': 'Short under double MTF', 'order': 2,
             'trigger': _c('mtf', '1h 34/50', 'below'),
             'conditions': [_c('mtf', 'daily 20/21', 'below'),
                            _c('cloud', '34/50', 'below')]},
        ],
    },
    {
        'id': 'daily_mtf',
        'name': 'Daily 50/55 MTF',
        'description': 'Sitting on the daily MTF. Long while it holds.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': 'Long while daily MTF holds', 'order': 1,
             'trigger': _c('mtf', 'daily 50/55', 'above'),
             'conditions': [_c('cloud', '34/50', 'above')]},
            {'bias': 'short', 'label': 'Short if daily MTF fails', 'order': 2,
             'trigger': _c('mtf', 'daily 50/55', 'below'),
             'conditions': [_c('level', 'PDC', 'under')]},
        ],
    },
    {
        'id': 'pmh_break_curl',
        'name': 'Long over pre-market high',
        'description': 'Break the pre-market high, or take the 5/12 curl. '
                       'Short under the pre-market low.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': 'Long over pre-market high', 'order': 1,
             'trigger': _c('level', 'PMH', 'over'),
             'conditions': [_c('cloud', '5/12', 'curls up'),
                            _c('level', 'PDH', 'over'),
                            _c('mtf', '1h 34/50', 'above')]},
            {'bias': 'short', 'label': 'Short under pre-market low', 'order': 2,
             'trigger': _c('level', 'PML', 'under'),
             'conditions': [_c('cloud', '34/50', 'below'),
                            _c('mtf', '1h 34/50', 'below')]},
        ],
    },
    {
        'id': 'curl_vs_pml',
        'name': '34/50 curl vs pre-market low',
        'description': 'Curl long off the pre-market low, bullish over PDC.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': '34/50 curl long vs pre-market low', 'order': 1,
             'trigger': _c('cloud', '34/50', 'curls up'),
             'conditions': [_c('level', 'PML', 'over'),
                            _c('level', 'PDC', 'over')]},
            {'bias': 'short', 'label': 'Short below pre-market low', 'order': 2,
             'trigger': _c('level', 'PML', 'under'),
             'conditions': [_c('cloud', '34/50', 'below')]},
        ],
    },
    {
        'id': 'pdl_hold',
        'name': 'Trade vs PDL',
        'description': 'Curl long while the prior day low holds. Short if it '
                       'breaks and price stays under.',
        'needs': [],
        'plays': [
            {'bias': 'long', 'label': 'Long while PDL holds', 'order': 1,
             'trigger': _c('level', 'PDL', 'over'),
             'conditions': [_c('cloud', '34/50', 'curls up'),
                            _c('mtf', '1h 34/50', 'above')]},
            {'bias': 'short', 'label': 'Short if PDL breaks', 'order': 2,
             'trigger': _c('level', 'PDL', 'under'),
             'conditions': [_c('cloud', '34/50', 'below')]},
        ],
    },
    {
        'id': 'starter_levels',
        'name': 'Starter over / under typed prices',
        'description': 'Two prices from the sheet: long over the first, '
                       'short under the second.',
        'needs': [{'key': 'long_over', 'label': 'Long over'},
                  {'key': 'short_under', 'label': 'Short under'}],
        'plays': [
            {'bias': 'long', 'label': 'Starter long', 'order': 1,
             'trigger': _c('level', 'custom', 'over', '${long_over}'),
             'conditions': [_c('cloud', '34/50', 'curls up'),
                            _c('level', 'PDH', 'over')]},
            {'bias': 'short', 'label': 'Starter short', 'order': 2,
             'trigger': _c('level', 'custom', 'under', '${short_under}'),
             'conditions': [_c('cloud', '34/50', 'below')]},
        ],
    },
]

CUSTOM_PRESETS_FILE = os.path.join(cm.DATA_DIR, 'play_presets.jsonl')


def _read_custom_presets():
    if not os.path.exists(CUSTOM_PRESETS_FILE):
        return []
    out = []
    with open(CUSTOM_PRESETS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Later definitions win, so re-saving a name replaces it.
    dedup = {}
    for p_ in out:
        dedup[p_['id']] = p_
    return list(dedup.values())


def presets():
    return BUILTIN_PRESETS + _read_custom_presets()


def get_preset(preset_id):
    return next((p_ for p_ in presets() if p_['id'] == preset_id), None)


def _fill(value, params):
    """Substitute ${key} placeholders with the numbers the user supplied."""
    if not isinstance(value, str) or not value.startswith('${'):
        return value
    key = value[2:-1]
    if key not in params or params[key] in (None, ''):
        raise ValueError(f'{key.replace("_", " ")} is required for this preset')
    return float(params[key])


def apply_preset(preset_id, ticker, params=None, for_date=None, start_order=1):
    """Turn a preset into real plays on a ticker. Nothing is written until
    every placeholder resolves, so a half-applied preset is impossible."""
    pre = get_preset(preset_id)
    if pre is None:
        raise ValueError(f'no preset {preset_id!r}')
    params = params or {}

    # A preset's arms become BRANCHES of one play, not separate plays. The
    # long and the short are the same read on the ticker -- "long over or
    # short under" is one sentence -- and splitting them loses the if/else.
    branches = []
    for tpl in pre['plays']:
        trig = dict(tpl['trigger'])
        trig['value'] = _fill(trig.get('value'), params)
        trigger = make_condition(trig['kind'], trig['ref'], trig['op'],
                                 trig.get('value'))
        conds = []
        for c in tpl.get('conditions', []):
            cc = dict(c)
            cc['value'] = _fill(cc.get('value'), params)
            conds.append(make_condition(cc['kind'], cc['ref'], cc['op'],
                                        cc.get('value')))
        branches.append(make_branch(bias=tpl['bias'], trigger=trigger,
                                    conditions=conds,
                                    label=tpl.get('label', '')))
    play = make_play(ticker=ticker, branches=branches,
                     label=pre.get('name', ''),
                     note=pre.get('description', ''),
                     order=start_order, for_date=for_date)
    return _append([play])


def save_preset(name, ticker=None, for_date=None, play_ids=None,
                description=''):
    """Turn plays you have already built into a reusable preset."""
    rows = read_all(ticker=ticker, for_date=for_date, active_only=False)
    if play_ids:
        rows = [p_ for p_ in rows if p_['id'] in set(play_ids)]
    if not rows:
        raise ValueError('no plays to save')
    pid = re.sub(r'[^a-z0-9]+', '_', str(name).strip().lower()).strip('_')
    if not pid:
        raise ValueError('a preset needs a name')
    arms = []
    for p_ in rows:
        for b in _as_branches(p_):
            arms.append({'bias': b['bias'], 'label': b.get('label', ''),
                         'order': len(arms) + 1, 'trigger': b['trigger'],
                         'conditions': b.get('conditions', [])})
    pre = {
        'id': f'custom_{pid}', 'name': str(name).strip(),
        'description': description or f'Saved from {rows[0]["ticker"]}',
        'needs': [], 'custom': True, 'plays': arms,
    }
    with _LOCK:
        with open(CUSTOM_PRESETS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(pre, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return pre


def delete_preset(preset_id):
    keep = [p_ for p_ in _read_custom_presets() if p_['id'] != preset_id]
    if len(keep) == len(_read_custom_presets()):
        return None
    tmp = CUSTOM_PRESETS_FILE + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            for p_ in keep:
                f.write(json.dumps(p_, ensure_ascii=False) + '\n')
        os.replace(tmp, CUSTOM_PRESETS_FILE)
    return preset_id


# ─── STORE ─────────────────────────────────────────────────────────────────────

def _append(rows):
    rows = list(rows)
    if not rows:
        return []
    with _LOCK:
        with open(PLAYS_FILE, 'a', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return rows


def add(**kw):
    return _append([make_play(**kw)])[0]


def read_all(ticker=None, for_date=None, active_only=True):
    if not os.path.exists(PLAYS_FILE):
        return []
    out = []
    with open(PLAYS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ticker and p.get('ticker') != ticker.upper():
                continue
            if for_date and p.get('for_date') != for_date:
                continue
            if active_only and not p.get('active', True):
                continue
            out.append(p)
    out.sort(key=lambda p: (p.get('for_date', ''), p.get('ticker', ''),
                            p.get('order', 1)))
    return out


def by_ticker(for_date=None, active_only=True):
    grouped = {}
    for p in read_all(for_date=for_date, active_only=active_only):
        grouped.setdefault(p['ticker'], []).append(p)
    return grouped


def _rewrite(rows):
    tmp = PLAYS_FILE + '.tmp'
    with _LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PLAYS_FILE)


EDITABLE = ('branches', 'bias', 'label', 'trigger', 'conditions', 'note',
            'order', 'active', 'for_date', 'ticker')


def update(play_id, **changes):
    rows = read_all(active_only=False)
    patch = {k: v for k, v in changes.items() if k in EDITABLE}
    if 'bias' in patch and patch['bias'] not in BIASES:
        raise ValueError(f'bias must be one of {BIASES}')
    if 'ticker' in patch:
        patch['ticker'] = str(patch['ticker']).strip().upper()
    hit = None
    for p in rows:
        if p['id'] == play_id:
            p.update(patch)
            p['edited_at'] = datetime.now().isoformat(timespec='seconds')
            hit = p
            break
    if hit is None:
        return None
    _rewrite(rows)
    return hit


def delete(play_id):
    rows = read_all(active_only=False)
    keep = [p for p in rows if p['id'] != play_id]
    if len(keep) == len(rows):
        return None
    gone = next(p for p in rows if p['id'] == play_id)
    _rewrite(keep)
    return gone


def carry_forward(from_date, to_date=None):
    to_date = to_date or date.today().isoformat()
    src = read_all(for_date=from_date, active_only=False)
    made = []
    for p in src:
        made.append(make_play(
            ticker=p['ticker'], branches=_as_branches(p),
            label=p.get('label', ''), note=p.get('note', ''),
            order=p.get('order', 1), for_date=to_date))
    return _append(made)


def stats():
    rows = read_all(active_only=False)
    return {'plays': len(rows),
            'tickers': len({p['ticker'] for p in rows}),
            'dates': sorted({p['for_date'] for p in rows})[-10:],
            'file': PLAYS_FILE}
