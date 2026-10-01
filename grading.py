"""grading.py — Foundations Trading

THE RUBRIC CORE. One ticker's measured state in, one transcript out.

Spec: design/01-grading-core.md. Trade types: design/09 §3.1. Measurements
come from the sibling described in design/02 and are INJECTED as a state dict
(§7.1) — this module fetches nothing, imports no journal, and can be unit
tested on a hand-written dict with no bars anywhere.

WHAT THIS REPLACES
  screener.DEFAULT_WEIGHTS, a 7-component weighted 0-1 score with sliders in
  the UI. It died because every change to it required re-normalising all seven
  weights, and because a tunable score fitted to the thing it grades is not a
  measurement. See design/01 §0.

WHAT IT GRADES, AND WHAT IT REFUSES TO
  The ENVIRONMENT, never the execution, and never the outcome. No realized
  P/L enters any number here. Nate's own words for why: "notes and actual P/L
  will speak for itself."

THE ARITHMETIC IS A CREDIT-HOUR GPA, run the way a registrar runs one.

        Σ (points × credit)     over conditions where na is False
  GPA = ───────────────────
            Σ credit           over the same conditions

Each condition is a course: it has credit hours fixed in code and earns 0, 2
or 4 points. With everything attempted the denominator is TOTAL_CREDITS and
the maximum numerator is 4 × that, so the scale is 0.0-4.0 by construction --
no normalisation constant, no clamping, and no rescaling when a condition is
added or dropped.

THAT PROPERTY IS THE WHOLE DESIGN. An N/A condition is a course not taken: it
leaves the numerator AND the denominator, and the survivors' shares rise in
exact proportion to their own credit, for free. No redistribution table, no
renormalising. It is also what makes a SCALP expressible -- design/09 §3.2
drops momentum and chop to N/A rather than to zero, and the arithmetic needed
no special case to absorb it.

CREDIT IS FOR CONTRIBUTION; CAPS ARE FOR PROHIBITION. A hard rule is a cap
(§5), never a heavy weight. signal_engine's docstring records what happens
otherwise: the old 3-vote engine let the 34/50 gate be outvoted, "and those
three gaps were the loss profile."

NUMBERS LIVE IN design/02's MODULE, NOT HERE. design/02's architectural rule:
this file must never contain a raw threshold like 0.35. The thresholds it does
carry are BORROWED by name from screener and levels, and the credit table is
the rubric itself rather than a tuning knob -- hardcoded by design, edit and
commit to change.
"""

import math
from datetime import datetime

import levels as LV
import screener as SC


# ─── GROUPS ────────────────────────────────────────────────────────────────────

GROUPS = (
    ('trigger',  'Next few bars'),
    ('structure', 'Where price is'),
    ('regime',   'The session'),
    ('mtf',      'The next few days'),
    ('tradable', 'Can it move'),
)


# ─── THE RUBRIC (module constants, NOT user-adjustable) ────────────────────────
#
# This is the RIPSTER table and it is the base. A trade type supplies an
# override map and inherits every row it does not mention (design/09 §3.1).

CREDIT = {
    # A — next few bars
    'curl_512':         3,
    'pos_512':          3,
    # B — structure
    'level_in_reach':   3,
    'level_quality':    2,
    'room_ahead':       2,
    'level_confluence': 1,
    # C — the session
    'gate_3450':        2,
    'cloud_support':    2,
    'momentum':         4,   # ruled up 2026-09-30, design/01 §9.1
    'chop':             3,   # ruled up 2026-09-30, design/01 §9.1
    # D — the next few days
    'mtf_1h':           1,
    'mtf_1d':           1,
    # E — preconditions
    'atr_structure':    1,
    'volume':           1,
}

# None means N/A -- the condition leaves the numerator AND the denominator.
# NOT zero. Zero is a verdict; N/A is a course not taken (§4).
TYPE_CREDIT = {
    'ripster': {},
    'scalp':   {'momentum': None, 'chop': None},
    'swing':   {'curl_512': 1, 'room_ahead': 3, 'mtf_1h': 2, 'mtf_1d': 3},
}
DEFAULT_TRADE_TYPE = 'ripster'

# Below this many ATTEMPTED credits the GPA is None and incomplete is True.
# Never 0 -- a 0.0 is a verdict and a None is an absence, and conflating them
# is how "no data" becomes "bad setup" on a board.
#
# 13 is not arbitrary: it is exactly what existing code can measure with no
# sibling work at all (level_in_reach 3 + level_quality 2 + room_ahead 2 +
# level_confluence 1 + pos_512 3 + gate_3450 2), so this module is computable
# the day it lands. It is an ABSOLUTE floor rather than a fraction, because it
# is a claim about measurability and not about proportion -- which is why a
# scalp's smaller table does not lower it.
MIN_CREDITS_FOR_GPA = 13

# Nate's rounding, ruled 2026-09-30. Two rules stacked:
#   .60 or more rounds up -- the same bar pairing.round_grade already applies
#   to his exit grades, so the two scores in this system round by one rule.
#   A 4 needs 3.75 regardless, which is stricter still.
# His reason, and it is the point of the instrument: "a pretty good trade at
# 3.48 GPA could be argued into a 4.0, but then it would undermine the point
# of scoring and improving trading."
ROUND_UP_AT = 0.60
FOUR_POINT_BAR = 3.75

LETTER = {4: 'A', 3: 'B', 2: 'C', 1: 'D', 0: 'F'}

# B2. screener.LEVEL_QUALITY encodes this judgement well but is a 0-1
# multiplier and this rubric needs 0/2/4, so grading carries its own table.
# screener's dict is NOT mutated: the old score is being deleted, not re-tuned,
# and other callers read it.
LEVEL_CREDIT = {
    'manual':      4,   # you typed it off the sheet this morning
    'premarket':   4,   # PMH/PML -- named explicitly; 0.95 in screener
    'prior_day':   3,
    'extreme':     3,   # ATH
    'floor_pivot': 2,   # ...but 3 for R1/S1, which he named
    'prior_week':  2,
    'prior_month': 2,
    'psych':       2,   # base; tiered upward below
}
NAMED_PIVOTS = ('R1', 'S1')

# Psych, ruled 2026-09-30 (design/01 §9.2.1). Weight is earned through
# SCARCITY rather than table inflation: a psych level is promoted only when it
# is a 10/50/100 class for its own price scale AND price is within one daily
# ATR of it. That keeps the psych grid sparse enough that level_in_reach can
# still score 0, which is what keeps the no_setup cap alive -- the objection
# this design answers rather than overrides.
PSYCH_TIER_CREDIT = {0: 2, 1: 3, 2: 4, 3: 4}

# Nate's "especially on stocks where ATR is within 1 day or less of the level".
# SUBSUMED IN PRACTICE, and worth knowing rather than discovering later: B2
# only ever grades the level B1 chose, and B1 filters to APPROACH_ATR (0.50),
# so anything reaching here is already inside half a daily ATR. The gate is
# kept because it is one comparison and it becomes live the moment APPROACH_ATR
# widens -- but the operative half of his ruling is the TIER, not the distance.
PSYCH_NEAR_ATR = 1.0

# C2's band edges. NAMED rather than inline, per design/00 §2's "one threshold
# in one place: grading.py must never contain a raw number like 0.35." They
# were inline at first commit and that was a breach -- caught by eye, which is
# precisely the argument for running ft-doctrine on a diff rather than
# trusting the eye.
#
# THEY BELONG IN THE MEASUREMENT MODULE, not here. C2 is a judgement condition
# computed from ctx, so it had nowhere else to live on the day grading.py
# landed; when environment.py exists these move there and this file borrows
# them by name the way it borrows screener's. Board: t-c2-thresholds.
CLOUD_NEAR_ATR = 0.5     # a cloud this close behind price will catch a pullback
CLOUD_REACH_ATR = 1.0    # beyond this it is too far to catch anything


# ─── ALIGNMENT AND DIRECTION (§2) ──────────────────────────────────────────────

def align(raw, direction):
    """'with' | 'against' | 'at' | None. Direction-relative, never raw.

    raw is journal/engine vocabulary: 'above', 'below', 'at', 'inside'. None
    means unreadable -- a blank, or a mixed value like the 'at, above' records
    in the journal.

    PINNED INVARIANT, tested in selftest():
        align(raw, 'long')  == conditions._align(raw, 'bull')
        align(raw, 'short') == conditions._align(raw, 'bear')

    conditions._align is deliberately NOT imported. Reaching into another
    module's private is how a refactor over there silently regrades everything
    over here. The test is the contract; the duplication is the cheaper of the
    two couplings.
    """
    v = str(raw or '').strip().lower()
    if v in ('above', 'below'):
        up = v == 'above'
        return 'with' if up == (direction == 'long') else 'against'
    if v in ('at', 'inside'):
        return 'at'
    return None


def direction_for(level_set, ctx):
    """('long'|'short'|None, why). The side of the in-play level decides; with
    no level in play the 34/50 trend decides.

    Same rule as screener._alignment(), lifted rather than imported because
    that function also returns an `aligned` boolean which C1 now supersedes.
    Lifting keeps the GPA's direction and the old board's direction from ever
    disagreeing.
    """
    near = _levels_within(level_set, SC.APPROACH_ATR)
    if near:
        side = near[0].get('side')
        if side == 'above':
            return 'long', f"nearest level {near[0].get('name')} is above price"
        if side == 'below':
            return 'short', f"nearest level {near[0].get('name')} is below price"
    trend = (ctx or {}).get('trend')
    if trend == 'up':
        return 'long', 'no level in reach; 34/50 trend is up'
    if trend == 'down':
        return 'short', 'no level in reach; 34/50 trend is down'
    return None, 'no level in reach and no 34/50 trend'


def round_gpa(raw):
    """0-4 in 1.0 increments. Nate's .60 bar, and 3.75 required for a 4."""
    if raw is None:
        return None
    if raw >= FOUR_POINT_BAR:
        return 4
    return max(0, min(3, int(math.floor(raw + (1.0 - ROUND_UP_AT)))))


# ─── READING HELPERS ───────────────────────────────────────────────────────────

def _levels(level_set):
    return list((level_set or {}).get('levels') or [])


def _levels_within(level_set, within_atr):
    return [l for l in _levels(level_set)
            if l.get('distance_atr') is not None
            and l['distance_atr'] <= within_atr]


def _sibling(state, key):
    """A sibling block, or None. Enforces design/02's rule that a measurement
    returns None or a dict and NEVER a bare float -- a bare float cannot
    distinguish 'momentum is 0.0' from 'momentum is unknown', and that single
    ambiguity is what turns a missing condition into a silent failure."""
    v = (state or {}).get(key)
    if isinstance(v, dict):
        return v
    if v is None:
        return None
    raise TypeError(f'{key} must be a dict or None, got {type(v).__name__}')


def _band(label, table):
    """A closed-vocabulary label -> points, or None when unknown."""
    return table.get(str(label or '').strip().lower())


NA = object()   # a reader returning this means "not applicable", not "zero"


def _na(why):
    return (NA, 'na', None, None, why)


# ─── THE CONDITIONS (§1) ───────────────────────────────────────────────────────
#
# Each reader takes (state, direction) and returns
#     (points, state_label, value, value_label, why)
# points is 0 | 2 | 4, or NA. Nothing else is ever produced, so the UI can key
# colour straight off it.

# A1 --------------------------------------------------------------------------
# design/02 §1 emits a richer vocabulary than design/01 §7.1 anticipated. The
# bridge is here rather than in the sibling, because the sibling's states are
# the measurement and these are the rubric's reading of it.
_CURL_SIDE = {
    'up': 'up', 'curl_up': 'up', 'expand_up': 'up',
    'down': 'down', 'curl_down': 'down', 'expand_down': 'down',
    'flat': None, 'coiled': None,
}


def _read_curl(state, direction):
    m = _sibling(state, 'curl_512')
    if not m:
        return _na('curl not measured (sibling absent or too few bars)')
    label = str(m.get('state') or '').strip().lower()
    if label not in _CURL_SIDE:
        return _na(f'unknown curl state {label!r}')
    side = _CURL_SIDE[label]
    val, lab = m.get('value'), label
    if side is None:
        # 'coiled' carries side=None by design (design/02 §1): the compression
        # is real information and its direction is not. A bucket, not a debit,
        # which is why it scores the same 2 as flat rather than 0.
        why = ('tape is coiled -- compressed, untested, and directionless'
               if label == 'coiled' else 'the 5/12 pair is not turning')
        return 2, label, val, lab, why
    want = 'up' if direction == 'long' else 'down'
    if side == want:
        return 4, label, val, lab, f'5/12 {label} agrees with a {direction}'
    return 0, label, val, lab, f'5/12 {label} opposes a {direction}'


def _read_pos_512(state, direction):
    ctx = (state or {}).get('ctx')
    if not ctx:
        return _na('no cloud state (fewer than 51 closed bars)')
    a = align(ctx.get('price_vs_5_12'), direction)
    if a is None:
        return _na(f'unreadable price_vs_5_12 {ctx.get("price_vs_5_12")!r}')
    # 'at' scores 0, not 2. The rubric's default middle state is 2 and this is
    # the one place it is overridden on outcome grounds: inside the 5/12 cloud
    # measured -$13.66 against -$19.47 for outright against, so it sits nearer
    # to against than to neutral. Scoring it 2 would hand a free half-credit to
    # the most common bad state in the book. design/01 A2.
    pts = {'with': 4, 'against': 0, 'at': 0}[a]
    return pts, a, None, a, f'price is {a} the 5/12 cloud for a {direction}'


# B ---------------------------------------------------------------------------

def _b1_level(state, direction):
    """The level B1 graded, so B2-B4 grade the same one. None when B1 scored 0."""
    want = 'above' if direction == 'long' else 'below'
    near = [l for l in _levels_within(state.get('level_set'), SC.APPROACH_ATR)
            if l.get('side') == want]
    return near[0] if near else None


def _read_level_in_reach(state, direction):
    lv = _b1_level(state, direction)
    if lv is None:
        # NEVER N/A. "No level anywhere near" is a fact about the chart, not a
        # gap in the data, and it is the best-evidenced bad state in the book
        # (no setup: -$36.10, n=30, mean owner grade 0.75). Letting it go N/A
        # would let the worst environment score by omission.
        return 0, 'far', None, f'nothing within {SC.APPROACH_ATR} ATR', \
            f'no level within {SC.APPROACH_ATR} ATR on the {direction} side'
    d = lv['distance_atr']
    lab = f"{lv.get('name')} at {d} ATR"
    if d <= SC.NEAR_ATR:
        return 4, 'near', d, lab, f"{lv.get('name')} is in play at {d} ATR"
    return 2, 'approaching', d, lab, f"{lv.get('name')} is approaching at {d} ATR"


def _read_level_quality(state, direction):
    lv = _b1_level(state, direction)
    if lv is None:
        return _na('no level in reach whose quality could be graded')
    kind, name = lv.get('kind'), lv.get('name')
    if kind == 'psych':
        tier = lv.get('tier') or 0
        near = (lv.get('distance_atr') is not None
                and lv['distance_atr'] <= PSYCH_NEAR_ATR)
        pts = PSYCH_TIER_CREDIT.get(tier, 2) if near else LEVEL_CREDIT['psych']
        why = (f'psych tier {tier} within {PSYCH_NEAR_ATR} daily ATR' if near
               else f'psych tier {tier} but beyond {PSYCH_NEAR_ATR} daily ATR')
        return pts, 'psych', tier, f'psych t{tier}', why
    pts = LEVEL_CREDIT.get(kind, 2)
    if kind == 'floor_pivot' and name in NAMED_PIVOTS:
        pts = 3
    # 0 is never awarded: a level that exists has some quality.
    pts = max(2, min(4, pts))
    return pts, kind, None, str(name), f'{name} is a {kind} level'


def _read_room_ahead(state, direction):
    lv = _b1_level(state, direction)
    if lv is None:
        return _na('no level in reach to measure room beyond')
    ls = state.get('level_set') or {}
    if ls.get('atr') is None:
        return _na('no ATR, so room cannot be expressed')
    room = SC.room_ahead(ls, lv, direction)
    if room is None:
        # Nothing lies beyond: open road. The same reading screener gives it.
        return 4, 'open_road', None, 'open road', 'nothing lies beyond the level'
    if room >= SC.ROOM_FULL_ATR:
        return 4, 'open_road', room, f'{room} ATR', f'{room} ATR of clear air'
    if room >= SC.ROOM_FULL_ATR / 2:
        return 2, 'some', room, f'{room} ATR', f'{room} ATR before the next level'
    return 0, 'blocked', room, f'{room} ATR', f'only {room} ATR before the next level'


def _read_level_confluence(state, direction):
    ls = state.get('level_set') or {}
    if ls.get('atr') is None:
        return _na('no ATR, so confluence cannot be measured')
    groups = [g for g in (LV.confluence(ls) or [])
              if g.get('distance_atr') is not None
              and g['distance_atr'] <= SC.APPROACH_ATR]
    if not groups:
        return 0, 'single', 0, 'no shelf', 'no stacked levels within reach'
    best = max(groups, key=lambda g: g.get('count', 0))
    n = best.get('count', 0)
    if n >= 3:
        return 4, 'shelf', n, f'{n} levels', f'a shelf of {n} levels'
    return 2, 'pair', n, f'{n} levels', f'{n} levels stacked'


# C ---------------------------------------------------------------------------

def _read_gate(state, direction):
    ctx = (state or {}).get('ctx')
    if not ctx:
        return _na('no cloud state (fewer than 51 closed bars)')
    a = align(ctx.get('price_vs_34_50'), direction)
    if a is None:
        return _na(f'unreadable price_vs_34_50 {ctx.get("price_vs_34_50")!r}')
    pts = {'with': 4, 'against': 0, 'at': 0}[a]
    return pts, a, None, a, f'price is {a} the 34/50 gate for a {direction}'


def _read_cloud_support(state, direction):
    ctx = (state or {}).get('ctx')
    ls = state.get('level_set') or {}
    atr = ls.get('atr')
    if not ctx or atr in (None, 0):
        return _na('no cloud state or no ATR')
    regime = (ctx.get('clouds') or {}).get('regime')
    price = ls.get('price')
    if not regime or price is None:
        return _na('no regime cloud')
    gap = ((price - regime[1]) if direction == 'long' else (regime[0] - price)) / atr
    gap = round(gap, 3)
    if gap <= 0:
        return 0, 'wrong_side', gap, f'{gap} ATR', 'price is not on the near side'
    if gap <= CLOUD_NEAR_ATR:
        return 4, 'close', gap, f'{gap} ATR', f'cloud sits {gap} ATR behind price'
    if gap <= CLOUD_REACH_ATR:
        return 2, 'mid', gap, f'{gap} ATR', f'cloud sits {gap} ATR behind price'
    return 0, 'far', gap, f'{gap} ATR', f'cloud is {gap} ATR away, too far to catch'


# design/02 §2 emits four momentum bands where design/01 C3 named three, and
# §3 emits a `coil` state design/01 C4 did not anticipate. Both bridges are
# here, and both are flagged in design/01 rather than decided silently.
_MOM_POINTS = {'strong': 4, 'moderate': 2, 'weak': 0, 'low': 0, 'none': 0}
_CHOP_POINTS = {'clean': 4, 'trend': 4, 'coil': 4, 'mixed': 2,
                'choppy': 0, 'chop': 0}


def _read_momentum(state, direction):
    m = _sibling(state, 'momentum')
    if not m:
        return _na('momentum not measured')
    lab = str(m.get('state') or '').strip().lower()
    pts = _band(lab, _MOM_POINTS)
    if pts is None:
        return _na(f'unknown momentum state {lab!r}')
    return pts, lab, m.get('value'), lab, f'momentum reads {lab}'


def _read_chop(state, direction):
    m = _sibling(state, 'chop')
    if not m:
        return _na('chop not measured')
    lab = str(m.get('state') or '').strip().lower()
    pts = _band(lab, _CHOP_POINTS)
    if pts is None:
        return _na(f'unknown chop state {lab!r}')
    # A COIL IS NOT CHOP, and design/02 §3 exists largely to say so: it is
    # compression before a move, and design/02 §1's measurement found coiled
    # bars moved 26.47% of the time against 8.45% for flat ones. Scoring it
    # clean follows from that. Flagged as a bridge, not a ruling -- design/01
    # C4 names three states and the sibling emits four.
    return pts, lab, m.get('value'), lab, f'tape reads {lab}'


# D ---------------------------------------------------------------------------

def _read_mtf(key, label):
    def reader(state, direction):
        m = ((state or {}).get('mtf') or {}).get(key)
        if not isinstance(m, dict):
            return _na(f'{label} not measured')
        a = align(m.get('pos'), direction)
        if a is None:
            return _na(f'unreadable {label} position {m.get("pos")!r}')
        # 'at' scores 2 here, unlike A2: inside a higher-timeframe cloud
        # genuinely is "no information", where inside the 5/12 is "no fast
        # direction". The journal supports it (+$1.76, noise, n=27).
        pts = {'with': 4, 'at': 2, 'against': 0}[a]
        return pts, a, m.get('value'), a, f'{label} is {a} the trade'
    return reader


# E ---------------------------------------------------------------------------

_ATR_POINTS = {'ample': 4, 'thin': 2, 'dead': 0}
_VOL_POINTS = {'heavy': 4, 'normal': 2, 'light': 0}


def _read_banded(key, table, noun):
    def reader(state, direction):
        m = _sibling(state, key)
        if not m:
            return _na(f'{noun} not measured')
        lab = str(m.get('state') or '').strip().lower()
        pts = _band(lab, table)
        if pts is None:
            return _na(f'unknown {noun} state {lab!r}')
        return pts, lab, m.get('value'), lab, f'{noun} reads {lab}'
    return reader


# ─── THE CONDITION LIST, IN RUBRIC ORDER ───────────────────────────────────────
#
# (id, label, group, measured, reader). `label` is owner-facing and is his
# vocabulary, not ours -- design/00's rule about not inventing new names for
# things he already has words for.
#
# NOTE FOR THE SPEC: design/01 §6 says "all 16 rows" in two places. There are
# FOURTEEN conditions and the credit table sums to 29 across fourteen entries.
# 16 is a spec error, recorded here rather than silently obeyed.

CONDITIONS = (
    ('curl_512',         '5/12 Curl',                'trigger',   True,  _read_curl),
    ('pos_512',          'EMA 5/12 10m',             'trigger',   True,  _read_pos_512),
    ('level_in_reach',   'Level In Play',            'structure', True,  _read_level_in_reach),
    ('level_quality',    'Level Quality',            'structure', False, _read_level_quality),
    ('room_ahead',       'Room To Run',              'structure', True,  _read_room_ahead),
    ('level_confluence', 'Levels Stacked',           'structure', True,  _read_level_confluence),
    ('gate_3450',        'EMA 34/50 10m',            'regime',    True,  _read_gate),
    ('cloud_support',    'Cloud As Support/Magnet',  'regime',    False, _read_cloud_support),
    ('momentum',         'Momentum Present',         'regime',    True,  _read_momentum),
    ('chop',             'Not Choppy',               'regime',    True,  _read_chop),
    ('mtf_1h',           'MTF 1HR 34/50',            'mtf',       True,  _read_mtf('1h_34_50', 'MTF 1HR 34/50')),
    ('mtf_1d',           'MTF 1D 20/21',             'mtf',       True,  _read_mtf('1d_20_21', 'MTF 1D 20/21')),
    ('atr_structure',    'Range Big Enough',         'tradable',  True,  _read_banded('atr_structure', _ATR_POINTS, 'range')),
    ('volume',           'Volume Participating',     'tradable',  True,  _read_banded('volume', _VOL_POINTS, 'volume')),
)

assert len(CONDITIONS) == len(CREDIT)
assert {c[0] for c in CONDITIONS} == set(CREDIT)


def credits_for(trade_type=None):
    """The resolved credit table for one trade type. None values are N/A."""
    tt = (trade_type or DEFAULT_TRADE_TYPE).strip().lower()
    if tt not in TYPE_CREDIT:
        raise ValueError(f'unknown trade_type {trade_type!r}')
    return {**CREDIT, **TYPE_CREDIT[tt]}


def total_credits(trade_type=None):
    return sum(v for v in credits_for(trade_type).values() if v is not None)


# ─── DISQUALIFIERS (§5) ────────────────────────────────────────────────────────
#
# (id, label, cap, why, measured). Caps are min() on the composite, applied
# before rounding. Multiple caps take the lowest.

CAPS = (
    ('no_setup', 'No level in play', 1.0,
     'the best-evidenced bad state in the book: -$36.10 over 30 trades', True),
    ('against_gate', 'Price against the 34/50 cloud', 1.0,
     'Ripster hard gate, and the engine will refuse the entry anyway', True),
    ('no_environment', 'No environment: low momentum AND choppy', 1.0,
     'honoured rather than evidenced -- his own grading gives this a mean of '
     '0.60 against a book mean of 1.36', False),
    ('inside_gate', 'Price inside the 34/50 cloud', 2.0,
     'the engine declines rather than contradicts', True),
    ('curl_against', 'The 5/12 curl opposes the trade', 2.0,
     'a trigger-timeframe fact that can flip in two bars, where a level '
     'either exists or does not', True),
)
_CAP_BY_ID = {c[0]: c for c in CAPS}


def disqualifiers(rows, direction=None):
    """Which caps fire, lowest cap first. `rows` is keyed by condition id.

    A cap can only fire on a condition that was actually GRADED. An N/A
    condition forbids nothing -- which is why a scalp, whose momentum and chop
    are N/A by design, can never trip no_environment. That falls out of the
    N/A rule rather than needing a rule of its own.
    """
    def pts(cid):
        r = rows.get(cid) or {}
        return None if r.get('na') else r.get('points')

    def st(cid):
        r = rows.get(cid) or {}
        return None if r.get('na') else r.get('state')

    fired = []
    if pts('level_in_reach') == 0:
        fired.append('no_setup')
    if st('gate_3450') == 'against':
        fired.append('against_gate')
    if st('gate_3450') == 'at':
        fired.append('inside_gate')
    if pts('momentum') == 0 and pts('chop') == 0:
        fired.append('no_environment')
    if pts('curl_512') == 0:
        fired.append('curl_against')

    out = [{'id': c[0], 'label': c[1], 'cap': c[2], 'why': c[3], 'measured': c[4]}
           for c in (_CAP_BY_ID[i] for i in fired)]
    out.sort(key=lambda d: d['cap'])
    return out


# ─── EVALUATION ────────────────────────────────────────────────────────────────

_UNSET = object()


def evaluate(spec, state, direction, credit=_UNSET):
    """One condition -> one transcript row (§6).

    NEVER RAISES. A reader that throws returns an N/A row carrying the
    exception text in `why`: one bad measurement must not lose the other
    thirteen.
    """
    cid, label, group, measured, reader = spec
    # credit=None MEANS N/A for this trade type (design/09 §3.1), so a
    # separate sentinel is needed for "caller did not supply one". Conflating
    # them made every scalp grade on the ripster table while reporting itself
    # as a scalp -- caught by the selftest, which is the whole reason it says
    # "and a scalp attempts 22 credits" rather than just checking the number.
    cr = CREDIT.get(cid) if credit is _UNSET else credit
    row = {
        'id': cid, 'label': label, 'group': group,
        'group_label': dict(GROUPS).get(group, group),
        'credit': cr, 'points': None, 'weighted': 0, 'share': 0.0,
        'na': True, 'state': 'na', 'value': None, 'value_label': None,
        'why': '', 'source': getattr(reader, '__name__', 'reader'),
        'measured': measured,
    }
    if cr is None:
        row['why'] = 'not graded for this trade type'
        return row
    if direction is None:
        row['why'] = 'no direction, so nothing is direction-relative'
        return row
    try:
        pts, st, val, vlab, why = reader(state or {}, direction)
    except Exception as exc:                       # noqa: BLE001 -- deliberate
        row['why'] = f'measurement failed: {exc.__class__.__name__}: {exc}'
        return row
    row.update({'state': st, 'value': val, 'value_label': vlab, 'why': why})
    if pts is NA:
        return row
    row.update({'na': False, 'points': pts, 'weighted': pts * cr})
    return row


def composite(rows, caps=None):
    """Graded rows -> the GPA block. Pure arithmetic: no measurement, no I/O.

    An N/A row leaves BOTH sums, so the survivors' shares rise in exact
    proportion to their own credit and nothing is redistributed by hand.
    """
    live = [r for r in rows if not r.get('na') and r.get('credit')]
    attempted = sum(r['credit'] for r in live)
    earned = sum(r['weighted'] for r in live)
    possible = 4 * attempted

    for r in rows:
        r['share'] = round(r['credit'] / attempted, 4) if (
            attempted and not r.get('na') and r.get('credit')) else 0.0

    caps = list(caps or [])
    cap_value = min((c['cap'] for c in caps), default=None)

    out = {
        'gpa': None, 'gpa_raw': None, 'gpa_earned': None, 'letter': None,
        'credits_attempted': attempted, 'points_earned': earned,
        'points_possible': possible, 'incomplete': attempted < MIN_CREDITS_FOR_GPA,
        'incomplete_why': None, 'cap_applied': cap_value,
    }
    if not attempted:
        out['incomplete_why'] = 'nothing measurable'
        return out

    gpa_earned = round(earned / attempted, 2)
    out['gpa_earned'] = gpa_earned

    if out['incomplete'] and not caps:
        out['incomplete_why'] = (f'only {attempted} of '
                                 f'{MIN_CREDITS_FOR_GPA} credits measurable')
        return out
    if out['incomplete']:
        # A CAP ALWAYS PRODUCES A GPA. If level_in_reach scores 0 three
        # dependents go N/A and attempted can fall below the floor -- without
        # this rule the worst state in the book would return None.
        out['incomplete_why'] = (f'only {attempted} credits measurable, but a '
                                 f'cap fired and a cap always produces a grade')

    raw = gpa_earned if cap_value is None else min(gpa_earned, cap_value)
    out['gpa_raw'] = round(raw, 2)
    out['gpa'] = round_gpa(raw)
    out['letter'] = LETTER.get(out['gpa'])

    assert abs(sum(r['weighted'] for r in rows if not r.get('na')) - earned) < 1e-9
    assert possible == 4 * attempted
    return out


def grade_ticker(state, direction=None, *, edge=None, ticker=None, now=None,
                 trade_type=None):
    """One ticker's measured state -> a full transcript (§6).

    state       the measurement bundle (design/01 §7.1). Everything is
                optional; anything absent becomes an N/A row, never a zero.
    direction   'long'|'short'. None derives it with direction_for().
    edge        conditions.edge_for() output, passed through UNTOUCHED and
                never an input to any number here. Injected rather than called
                so this module has no journal dependency.
    trade_type  design/09. None grades as ripster and says so on the
                transcript, so an assumption is never invisible.
    """
    state = state or {}
    assumed = trade_type in (None, '')
    tt = DEFAULT_TRADE_TYPE if assumed else str(trade_type).strip().lower()
    table = credits_for(tt)

    why_dir = 'given'
    if direction is None:
        direction, why_dir = direction_for(state.get('level_set'), state.get('ctx'))

    rows = [evaluate(spec, state, direction, credit=table[spec[0]])
            for spec in CONDITIONS]
    by_id = {r['id']: r for r in rows}
    caps = disqualifiers(by_id, direction)
    block = composite(rows, caps)

    by_group = []
    for gid, glabel in GROUPS:
        g = [r for r in rows if r['group'] == gid]
        live = [r for r in g if not r['na'] and r['credit']]
        att = sum(r['credit'] for r in live)
        got = sum(r['weighted'] for r in live)
        by_group.append({
            'id': gid, 'label': glabel,
            'credit': sum(c for c in (table[r['id']] for r in g) if c),
            'attempted': att, 'points_earned': got, 'points_possible': 4 * att,
            'gpa': round(got / att, 2) if att else None,
        })

    at = now or datetime.now()
    return {
        'version': 'grading/1',
        'ticker': ticker,
        'at': at.replace(microsecond=0).isoformat(),
        'direction': direction,
        'direction_why': why_dir,
        'trade_type': tt,
        'trade_type_assumed': assumed,
        'credits_possible': total_credits(tt),
        'caps': caps,
        'conditions': rows,
        'by_group': by_group,
        'edge': edge,
        'edge_note': 'journal P/L for this cloud state — not part of the grade',
        **block,
    }


def transcript_for_journal(t):
    """The small flat subset written onto a journal record or screen_history
    row. A transcript is ~4KB and screen_history writes one per ticker per
    scan, so what is persisted is deliberately not the whole thing."""
    return {
        'gpa': t.get('gpa'),
        'gpa_raw': t.get('gpa_raw'),
        'trade_type': t.get('trade_type'),
        'credits_attempted': t.get('credits_attempted'),
        'caps': [c['id'] for c in (t.get('caps') or [])],
        'points': {r['id']: r['points'] for r in (t.get('conditions') or [])
                   if not r['na']},
    }


# ─── SELFTEST ──────────────────────────────────────────────────────────────────

def _state(**kw):
    """A gradeable state with every sibling present, overridable per test."""
    base = {
        'ctx': {'price_vs_5_12': 'above', 'price_vs_34_50': 'above',
                'trend': 'up', 'clouds': {'regime': [98.0, 99.0],
                                          'fast': [99.5, 99.8]}},
        'level_set': {'price': 100.0, 'atr': 5.0, 'levels': [
            {'name': 'PMH', 'kind': 'premarket', 'price': 100.5,
             'distance_atr': 0.10, 'side': 'above', 'tier': None},
            {'name': 'PDH', 'kind': 'prior_day', 'price': 104.0,
             'distance_atr': 0.80, 'side': 'above', 'tier': None},
        ]},
        'curl_512': {'state': 'up', 'value': 0.62, 'bars': 2},
        'momentum': {'state': 'strong', 'value': 2.4},
        'chop': {'state': 'clean', 'value': 0.71},
        'atr_structure': {'state': 'ample', 'value': 0.03},
        'volume': {'state': 'normal', 'value': 1.1},
        'mtf': {'1h_34_50': {'pos': 'above', 'value': 1.0},
                '1d_20_21': {'pos': 'above', 'value': 1.0}},
    }
    base.update(kw)
    return base


def selftest():
    fails = []

    def ck(label, cond, got=None):
        if cond:
            print(f'  ok   {label}')
        else:
            fails.append(label)
            print(f'  FAIL {label}' + (f'   got {got!r}' if got is not None else ''))

    print('the rubric adds up')
    ck('ripster attempts 29', total_credits('ripster') == 29, total_credits('ripster'))
    ck('scalp attempts 22', total_credits('scalp') == 22, total_credits('scalp'))
    ck('swing attempts 31', total_credits('swing') == 31, total_credits('swing'))
    ck('fourteen conditions, not sixteen', len(CONDITIONS) == 14, len(CONDITIONS))
    ck('an unknown trade type raises rather than defaulting',
       _raises(credits_for, 'daytrade'))

    print("rounding -- Nate's .60 bar with 3.75 for a 4")
    ck('3.48 is a 3', round_gpa(3.48) == 3, round_gpa(3.48))
    ck('3.60 is still a 3, because a 4 needs 3.75', round_gpa(3.60) == 3, round_gpa(3.60))
    ck('3.75 is a 4', round_gpa(3.75) == 4)
    ck('2.60 rounds up', round_gpa(2.60) == 3, round_gpa(2.60))
    ck('2.59 rounds down', round_gpa(2.59) == 2, round_gpa(2.59))
    ck('2.50 rounds DOWN, unlike half-up', round_gpa(2.50) == 2, round_gpa(2.50))
    ck('0.0 stays 0', round_gpa(0.0) == 0)
    ck('None stays None', round_gpa(None) is None)

    print('align() is pinned to conditions._align (§2)')
    import conditions as CO
    raws = ('above', 'below', 'at', 'inside', '', None, 'at, above')
    ck('long == bull for every raw',
       all(align(r, 'long') == CO._align(r, 'bull') for r in raws))
    ck('short == bear for every raw',
       all(align(r, 'short') == CO._align(r, 'bear') for r in raws))

    print('a good long grades, and the arithmetic is internally consistent')
    t = grade_ticker(_state(), 'long', ticker='TEST')
    ck('every condition is present, N/A included',
       len(t['conditions']) == 14, len(t['conditions']))
    ck('points are only ever 0, 2, 4 or None',
       all(r['points'] in (0, 2, 4, None) for r in t['conditions']))
    ck('share sums to 1.0 across graded rows',
       abs(sum(r['share'] for r in t['conditions']) - 1.0) < 1e-6,
       sum(r['share'] for r in t['conditions']))
    ck('points_possible == 4 x credits_attempted',
       t['points_possible'] == 4 * t['credits_attempted'])
    ck('a good-but-not-perfect setup is a 3, not a 4',
       (t['gpa'], t['gpa_raw']) == (3, 3.66), (t['gpa'], t['gpa_raw']))
    ck('letter agrees with the integer', t['letter'] == 'B')
    ck('...and what held it back is visible on the transcript',
       _points(t, 'volume') == 2 and _points(t, 'level_confluence') == 0)

    print('4.0 is hard to reach, which is the point of the 3.75 bar')
    best = _state(volume={'state': 'heavy', 'value': 2.0},
                  level_set={'price': 100.0, 'atr': 5.0, 'levels': [
                      {'name': 'PMH', 'kind': 'premarket', 'price': 100.5,
                       'distance_atr': 0.10, 'side': 'above', 'tier': None},
                      {'name': 'PDH', 'kind': 'prior_day', 'price': 106.0,
                       'distance_atr': 1.20, 'side': 'above', 'tier': None}]})
    b = grade_ticker(best, 'long')
    ck('everything right except a shelf still only reaches 3.86',
       b['gpa_raw'] == 3.86, b['gpa_raw'])
    ck('...but that IS a 4, because 3.86 clears 3.75', b['gpa'] == 4, b['gpa'])

    print('level_confluence and room_ahead are structurally opposed')
    shelf = _state(level_set={'price': 100.0, 'atr': 5.0, 'levels': [
        {'name': 'PMH', 'kind': 'premarket', 'price': 100.5,
         'distance_atr': 0.10, 'side': 'above', 'tier': None},
        {'name': 'PDC', 'kind': 'prior_day', 'price': 100.6,
         'distance_atr': 0.12, 'side': 'above', 'tier': None},
        {'name': 'R1', 'kind': 'floor_pivot', 'price': 100.7,
         'distance_atr': 0.14, 'side': 'above', 'tier': None}]},
        volume={'state': 'heavy', 'value': 2.0})
    sh = grade_ticker(shelf, 'long')
    ck('a shelf scores confluence 4', _points(sh, 'level_confluence') == 4,
       _points(sh, 'level_confluence'))
    ck('...and the same shelf is what blocks the room beyond it',
       _points(sh, 'room_ahead') == 0, _points(sh, 'room_ahead'))
    ck('so a RAW 4.00 is unreachable, by construction rather than by tuning',
       sh['gpa_raw'] < 4.0 and b['gpa_raw'] < 4.0)

    print('N/A leaves BOTH sums -- the property the whole design rests on (§4)')
    # composite() is pure arithmetic over rows, which is what lets the N/A rule
    # be tested exactly rather than through a fixture that happens to be close.
    def _rows(*specs):
        return [{'id': f'c{i}', 'credit': c, 'points': (None if p is None else p),
                 'weighted': (0 if p is None else p * c), 'na': p is None,
                 'group': 'trigger'} for i, (c, p) in enumerate(specs)]

    # Both sides stay above MIN_CREDITS_FOR_GPA so the floor is not what is
    # being measured. The 1-credit row scores 2, which IS the weighted mean.
    base = composite(_rows((7, 4), (7, 0), (1, 2)))
    dropped = composite(_rows((7, 4), (7, 0), (1, None)))
    ck('dropping a row that scored the mean moves the GPA NOT AT ALL',
       base['gpa_raw'] == dropped['gpa_raw'] == 2.0,
       (base['gpa_raw'], dropped['gpa_raw']))
    ck('...and the denominator really did shrink',
       (base['credits_attempted'], dropped['credits_attempted']) == (15, 14),
       (base['credits_attempted'], dropped['credits_attempted']))
    ck('dropping an ABOVE-mean row lowers it, which is correct and not a bug',
       composite(_rows((7, 4), (7, 0), (1, None)))['gpa_raw']
       > composite(_rows((7, 4), (7, 0), (1, 4)))['gpa_raw'] - 0.14,
       (composite(_rows((7, 4), (7, 0), (1, None)))['gpa_raw'],
        composite(_rows((7, 4), (7, 0), (1, 4)))['gpa_raw']))
    rr = _rows((7, 4), (7, 0), (1, None))
    composite(rr)
    ck('share still sums to 1.0 after a row goes N/A',
       abs(sum(r['share'] for r in rr) - 1.0) < 1e-6,
       sum(r['share'] for r in rr))
    ck('and the N/A row carries share 0.0, not a fraction of nothing',
       rr[-1]['share'] == 0.0)

    full = grade_ticker(_state(), 'long')
    thin = grade_ticker(_state(momentum=None, chop=None), 'long')
    ck('on a real transcript a missing sibling lowers credits_attempted',
       thin['credits_attempted'] < full['credits_attempted'])
    ck('the N/A row is still there, visibly -- a gap must never be silent',
       any(r['id'] == 'momentum' and r['na'] for r in thin['conditions']))

    print('trade type selects the rubric (design/09 §3.1)')
    tape = _state(momentum={'state': 'low', 'value': 0.2},
                  chop={'state': 'choppy', 'value': 0.2})
    rip = grade_ticker(tape, 'long', trade_type='ripster')
    scalp = grade_ticker(tape, 'long', trade_type='scalp')
    ck('a flat tape costs a ripster 0.86 of a point',
       abs((rip['gpa_earned'] + 0.86) - scalp['gpa_earned']) < 0.01,
       (rip['gpa_earned'], scalp['gpa_earned']))
    ck('the numerator is identical -- only the denominator changed',
       rip['points_earned'] == scalp['points_earned'],
       (rip['points_earned'], scalp['points_earned']))
    ck('because momentum and chop are N/A, not zero',
       all(r['na'] for r in scalp['conditions']
           if r['id'] in ('momentum', 'chop')))
    ck('and a scalp attempts 22 credits', scalp['credits_attempted'] == 22,
       scalp['credits_attempted'])
    ck('no trade type grades as ripster AND says so',
       grade_ticker(tape, 'long')['trade_type_assumed'] is True)
    ck('swing weights the daily cloud harder than ripster does',
       _credit_of(grade_ticker(tape, 'long', trade_type='swing'), 'mtf_1d') == 3)
    ck('swing attempts 31', grade_ticker(_state(), 'long',
       trade_type='swing')['credits_attempted'] == 31)

    print('caps are prohibition, not weight (§5)')
    nolevel = grade_ticker(_state(level_set={'price': 100.0, 'atr': 5.0,
                                             'levels': []}), 'long')
    ck('no level in play caps at 1.0', nolevel['cap_applied'] == 1.0,
       nolevel['cap_applied'])
    ck('and the cap binds the raw grade', nolevel['gpa_raw'] <= 1.0)
    ck('a cap always produces a grade, even below the credit floor',
       nolevel['gpa'] is not None)
    ck('three dependents went N/A with it',
       all(_row(nolevel, k)['na'] for k in
           ('level_quality', 'room_ahead')))

    against = _state()
    against['ctx'] = {**against['ctx'], 'price_vs_34_50': 'below'}
    g = grade_ticker(against, 'long')
    ck('price against the gate caps at 1.0', g['cap_applied'] == 1.0, g['cap_applied'])
    inside = _state()
    inside['ctx'] = {**inside['ctx'], 'price_vs_34_50': 'at'}
    ck('price inside the gate caps at 2.0',
       grade_ticker(inside, 'long')['cap_applied'] == 2.0)

    print('chop alone caps NOTHING -- the curl-in-chop bucket was his best')
    choppy = grade_ticker(_state(chop={'state': 'choppy', 'value': 0.1}), 'long')
    ck('choppy on its own fires no cap', choppy['caps'] == [], choppy['caps'])
    dead = grade_ticker(_state(chop={'state': 'choppy', 'value': 0.1},
                               momentum={'state': 'low', 'value': 0.1}), 'long')
    ck('but choppy AND low momentum together cap at 1.0',
       dead['cap_applied'] == 1.0, dead['cap_applied'])
    ck('a scalp cannot trip that cap, because both are N/A',
       grade_ticker(dead and _state(chop={'state': 'choppy', 'value': 0.1},
                                    momentum={'state': 'low', 'value': 0.1}),
                    'long', trade_type='scalp')['caps'] == [])

    print('psych tiering, ruled 2026-09-30 (§9.2.1)')
    def psych_at(price, dist_atr, tier):
        return _state(level_set={'price': 66.0, 'atr': 5.0, 'levels': [
            {'name': 'psych', 'kind': 'psych', 'price': price,
             'distance_atr': dist_atr, 'side': 'above', 'tier': tier}]})
    ck('a $70 ten inside one ATR outranks the base grid',
       _points(grade_ticker(psych_at(70.0, 0.20, 1), 'long'), 'level_quality') == 3)
    ck('a level beyond APPROACH_ATR is not in play AT ALL, so B2 goes N/A -- '
       'which is why PSYCH_NEAR_ATR is subsumed in practice',
       _row(grade_ticker(psych_at(70.0, 1.20, 1), 'long'),
            'level_quality')['na'] is True)
    ck('a fifty inside one ATR reaches the top',
       _points(grade_ticker(psych_at(50.0, 0.20, 2), 'long'), 'level_quality') == 4)
    ck('a base-grid round number stays at 2',
       _points(grade_ticker(psych_at(65.0, 0.10, 0), 'long'), 'level_quality') == 2)

    print('it refuses to guess')
    ck('no state at all -> no direction, everything N/A, gpa None',
       grade_ticker({})['gpa'] is None)
    ck('...and incomplete says why', grade_ticker({})['incomplete'] is True)
    ck('a sibling returning a bare float raises INSIDE evaluate and is caught',
       _row(grade_ticker(_state(momentum=0.5), 'long'), 'momentum')['na'] is True)
    ck('one bad measurement does not lose the other thirteen',
       _row(grade_ticker(_state(momentum=0.5), 'long'), 'chop')['na'] is False)
    ck('an unknown sibling label is N/A, not zero',
       _row(grade_ticker(_state(chop={'state': 'wobbly'}), 'long'), 'chop')['na'])

    print('direction is derived the same way the old board derives it')
    up = {'level_set': {'price': 100.0, 'atr': 5.0, 'levels': [
        {'name': 'PDH', 'kind': 'prior_day', 'price': 101.0,
         'distance_atr': 0.2, 'side': 'above', 'tier': None}]}, 'ctx': None}
    ck('a level above price means long', grade_ticker(up)['direction'] == 'long')
    down = {'level_set': {'price': 100.0, 'atr': 5.0, 'levels': [
        {'name': 'PDL', 'kind': 'prior_day', 'price': 99.0,
         'distance_atr': 0.2, 'side': 'below', 'tier': None}]}, 'ctx': None}
    ck('a level below price means short', grade_ticker(down)['direction'] == 'short')
    ck('no level and no trend means no direction',
       grade_ticker({'level_set': {'price': 100.0, 'atr': 5.0, 'levels': []},
                     'ctx': {'trend': 'chop'}})['direction'] is None)

    print('the journal subset stays small and flat')
    j = transcript_for_journal(t)
    ck('it carries the grade and what capped it', set(j) == {
        'gpa', 'gpa_raw', 'trade_type', 'credits_attempted', 'caps', 'points'})
    ck('and no N/A rows', None not in j['points'].values())

    print()
    if fails:
        print(f'SELFTEST FAILED -- {len(fails)} of the above')
    else:
        print(f'SELFTEST PASSED')
    return not fails


def _raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _row(t, cid):
    return next(r for r in t['conditions'] if r['id'] == cid)


def _points(t, cid):
    return _row(t, cid)['points']


def _credit_of(t, cid):
    return _row(t, cid)['credit']


def _shares(rows):
    composite(rows)
    return rows


if __name__ == '__main__':
    import sys
    sys.exit(0 if selftest() else 1)
