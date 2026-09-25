"""
screener.py — Foundations Trading

The environment tracker. A CONSUMER of the watchlist noun that answers one
question per ticker: how close is this to being decided?

WHAT IT DOES NOT DO
  It does not judge setups. It has no opinion about whether a move is worth
  taking. Ranking is by ADJACENCY and ALIGNMENT -- how near price sits to a
  level, and whether the cloud state agrees -- both of which are measurable.
  The play-sheet note rides along verbatim so the human supplies judgment.

  This is deliberate. The qualitative part of these plays is real, but it is
  a small fraction of them: the notes reference a closed vocabulary of levels
  that levels.py computes outright. Pretending the rest can be scored would
  produce confident nonsense.

WHY IT CAPS WHAT IT SHOWS
  Forty tickers on a bar-close cycle will always find something. A tool that
  surfaces forty items a day gets ignored inside a week, and then it is worse
  than nothing because you trust it while not reading it. The binding
  constraint is attention, not detection, so the result is a hard-capped
  ranked list. A sixth candidate has to displace one of the five.

CLOUD STATE COMES FROM signal_engine
  trend_context() already computes the 34/50 gate, fresh 5/12 crosses and
  price-vs-cloud for both clouds. The screener consumes it rather than
  reimplementing Ripster logic, so the screen and the engine can never
  disagree about what the chart says.
"""

from datetime import datetime

import levels as LV


# How close, in ATR, counts as "at" a level. Under a fifth of a daily range
# is roughly where a break or a rejection becomes imminent rather than
# hypothetical.
NEAR_ATR = 0.20
APPROACH_ATR = 0.50

DEFAULT_SLOTS = 5

# How much clear air counts as "room". One full daily ATR between the level
# and the next obstacle is treated as a clean runway; less is proportionally
# worse. A break that runs into a shelf 0.2 ATR later is a different trade
# from one with nothing in front of it, and nothing else here measures that.
ROOM_FULL_ATR = 1.0

# Not all levels carry the same weight. A pivot you typed off the sheet and
# a prior-day extreme are places other people are watching; a round number
# two dollars away is a place the price merely passes through.
LEVEL_QUALITY = {
    'manual': 1.00,
    # Pre-market extremes are where the overnight auction settled and are
    # watched by everyone trading the open, so they rate with your own
    # typed pivots rather than with a generic prior-period level.
    'premarket': 0.95,
    'prior_day': 0.85,
    'prior_month': 0.70,
    'extreme': 0.80,
    'prior_week': 0.70,
    'floor_pivot': 0.60,
    'psych': 0.40,
}

# Starting weights. These are JUDGEMENT, not calibration -- they are not
# derived from outcome data, and the UI exposes them precisely because they
# should not be trusted as given.
#
# The one exception is what 'edge' measures. It replaced 'alignment', which
# counted the three timeframes as equal votes. The journal grades those same
# clouds on every trade, and they did not trade equally, so 'edge' scores a
# ticker's current cloud state by how that state actually performed (see
# conditions.edge_for). Its WEIGHT is still judgement; its VALUE is data.
#
# The rest measure things the journal never recorded at fill time. The
# screener now logs its readings (screen_history.py) so that, once enough
# trades line up with a reading, calibration can suggest these from results.
DEFAULT_WEIGHTS = {
    'proximity': 0.22,
    'room': 0.20,
    'edge': 0.16,
    'level_quality': 0.15,
    'trigger': 0.14,
    'confluence': 0.08,
    'volume': 0.05,
}

COMPONENTS = tuple(DEFAULT_WEIGHTS)


def normalize_weights(w):
    """Clamp to >=0 and rescale to sum 1, so the score stays 0-1 whatever
    the user types. A slider set to zero switches a component off rather
    than corrupting the scale."""
    w = dict(w or {})
    # Weights saved before 'alignment' became 'edge' carry the old key. The
    # component's slot is the same, so the chosen weight moves across rather
    # than silently dropping to zero.
    if 'edge' not in w and 'alignment' in w:
        w['edge'] = w['alignment']
    clean = {k: max(0.0, float(w.get(k, 0) or 0)) for k in COMPONENTS}
    total = sum(clean.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in clean.items()}


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def room_ahead(level_set, level, direction):
    """Clear air, in ATR, between a level and the next one beyond it.

    Beyond means further in the direction of the break: for a long that is
    above the level, for a short below it. Returns None when nothing lies
    beyond, which is treated as open road.
    """
    a = level_set.get('atr')
    if not a or not level:
        return None
    px = level['price']
    if direction == 'long':
        beyond = [l['price'] for l in level_set['levels'] if l['price'] > px + 1e-9]
        nxt = min(beyond) if beyond else None
    else:
        beyond = [l['price'] for l in level_set['levels'] if l['price'] < px - 1e-9]
        nxt = max(beyond) if beyond else None
    if nxt is None:
        return None
    return round(abs(nxt - px) / a, 3)


def timeframe_votes(direction, ctx, mtf_1h, mtf_1d):
    """How many of the three timeframes agree with the break direction.

    34/50 on the working timeframe, 34/50 on the hour, 20/21 on the day --
    the three the play sheet keeps referring to. Each is 'up', 'down' or
    None; None abstains rather than counting against.
    """
    want = 'up' if direction == 'long' else 'down'
    votes, cast = 0, 0
    for v in ((ctx or {}).get('trend'), mtf_1h, mtf_1d):
        if v in (None, '', 'chop'):
            continue
        cast += 1
        if v == want:
            votes += 1
    return votes, cast


def mtf_state(bars, fast, slow):
    """'up' / 'down' / None from a higher-timeframe EMA pair."""
    try:
        import signal_engine as se
        import pandas as pd
        rows = _normalize_bars(bars)
        if len(rows) <= slow + 2:
            return None
        cs = pd.Series([b['close'] for b in rows])
        f = se.ema(cs, fast).iloc[-1]
        sl = se.ema(cs, slow).iloc[-1]
        if f != f or sl != sl:
            return None
        return 'up' if f > sl else 'down'
    except Exception:
        return None


def rel_volume(bars, lookback=20):
    """Latest closed bar's volume against its recent average."""
    rows = _normalize_bars(bars)
    vols = [float(b.get('volume') or 0) for b in rows][-(lookback + 1):]
    if len(vols) < 5:
        return None
    hist = vols[:-1]
    avg = sum(hist) / len(hist)
    if avg <= 0:
        return None
    return round(vols[-1] / avg, 3)


def _normalize_bars(bars):
    """Accept either bar dialect and emit the one signal_engine expects.

    alpaca_manager writes time/open/high/low/close/volume; most other places
    in this codebase (and any hand-built test fixture) use t/o/h/l/c/v.
    Converting here means the screener works with both instead of silently
    producing no cloud state, which is exactly the failure this replaced.
    """
    out = []
    for b in bars or []:
        t = b.get('time') or b.get('t') or b.get('timestamp')
        row = {
            'time': t.isoformat() if hasattr(t, 'isoformat') else t,
            'open': b.get('open', b.get('o')),
            'high': b.get('high', b.get('h')),
            'low': b.get('low', b.get('l')),
            'close': b.get('close', b.get('c')),
            'volume': b.get('volume', b.get('v', 0)),
        }
        if row['time'] is None or row['close'] is None:
            continue
        out.append(row)
    return out


def _trend_context(bars10):
    """Cloud state via signal_engine.

    Returns (context, reason). A None context always carries the reason --
    swallowing the cause here is how a bar-schema mismatch hides as "no
    signal" for weeks.
    """
    rows = _normalize_bars(bars10)
    if len(rows) < 51:
        return None, f'need 51 closed bars, have {len(rows)}'
    try:
        import signal_engine as se
        df = se.bars_to_df(rows)
        if df is None:
            return None, 'bars_to_df returned nothing'
        return se.trend_context(df), None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def _levels_needed(bars10):
    return 51


def _alignment(ctx, level):
    """Does the cloud state agree with a break of this level?

    Returns (direction, aligned, why). Direction is the way price would have
    to go to take the level. Alignment is whether the 34/50 gate is on that
    same side -- Ripster's hard gate, not a preference.
    """
    if not ctx or not level or level.get('side') is None:
        return None, False, 'no cloud state'
    side = level['side']
    if side == 'above':
        direction = 'long'
    elif side == 'below':
        direction = 'short'
    else:
        direction = 'long' if ctx.get('trend') == 'up' else 'short'

    trend = ctx.get('trend')
    if trend == 'chop':
        return direction, False, 'price inside the 34/50 cloud'
    aligned = (direction == 'long' and trend == 'up') or \
              (direction == 'short' and trend == 'down')
    return direction, aligned, f'34/50 {trend}'


def build_state(ls, ctx, m1h, m1d_2021, m1d_5055, rv):
    """Pack the screener's measurements into the shape plays.py evaluates.

    Everything here is already computed for the board; this is a translation
    layer, not new analysis. Anything unmeasured stays absent rather than
    defaulting, because plays treat a missing input as unknown and a
    defaulted one as fact.
    """
    levels = {}
    for lv in ls.get('levels', []):
        # First wins: levels arrive sorted nearest-first, and for a banded
        # reference like manual_support the nearest edge is the one price is
        # actually interacting with.
        levels.setdefault(lv['name'], lv['price'])

    cloud, fresh = {}, {}
    if ctx:
        cloud['5/12'] = ctx.get('price_vs_5_12')
        cloud['34/50'] = ctx.get('price_vs_34_50')
        fresh['5/12'] = ('up' if ctx.get('fresh_long')
                         else ('down' if ctx.get('fresh_short') else None))
        # The 34/50 has no fresh-cross flag in trend_context; its regime
        # state is the trend itself.
        fresh['34/50'] = ctx.get('trend') if ctx.get('trend') in ('up', 'down') else None

    return {
        'price': ls.get('price'),
        'atr': ls.get('atr'),
        'levels': levels,
        'cloud': cloud,
        'fresh': fresh,
        'mtf': {'1h 34/50': m1h,
                'daily 20/21': m1d_2021,
                'daily 50/55': m1d_5055},
        'rel_volume': rv,
    }


def journal_conditions(ctx, m1h, m1d):
    """This ticker's cloud state in the journal's vocabulary.

    The journal records price against each cloud (above / below / at). The
    working-chart clouds come straight from trend_context. The 1H and 1D
    reads here are the EMA pair's direction rather than price against it --
    the closest the screener measures -- so 'up' is taken as 'above'.
    """
    out = {}
    if ctx:
        if ctx.get('price_vs_5_12'):
            out['ema_5_12'] = ctx['price_vs_5_12']
        if ctx.get('price_vs_34_50'):
            out['ema_34_50'] = ctx['price_vs_34_50']
    for key, v in (('mtf_1h', m1h), ('mtf_1d', m1d)):
        if v in ('up', 'down'):
            out[key] = 'above' if v == 'up' else 'below'
    return out


def score_ticker(entry, daily_bars, bars10, price=None, hourly_bars=None,
                 weights=None, plays=None, edge_table=None):
    """One watchlist entry -> a state vector plus a component-wise score.

    Every component is reported raw AND normalized, so the number can always
    be taken apart. A score with no visible parts is an opinion with a
    decimal point.
    """
    ticker = entry['ticker']
    ctx, ctx_reason = _trend_context(bars10)
    W = normalize_weights(weights or DEFAULT_WEIGHTS)

    if price is None and bars10:
        last = _normalize_bars(bars10)[-1] if _normalize_bars(bars10) else {}
        price = last.get('close')
    if price is None and ctx:
        price = (ctx.get('closes') or {}).get('cur')

    manual = {}
    if entry.get('support'):
        manual['support'] = entry['support']
    if entry.get('resistance'):
        manual['resistance'] = entry['resistance']

    # Intraday bars go in too: PMH/PML are the PRE-MARKET extremes and only
    # exist at intraday resolution. Passing daily bars alone silently omits
    # the two levels the play sheet references most.
    ls = LV.build(daily_bars, price, manual=manual, intraday_bars=bars10)
    near = LV.nearest(ls, within_atr=APPROACH_ATR)
    closest = near[0] if near else None
    groups = LV.confluence(ls)
    shelf = groups[0] if groups else None

    direction, aligned, why = _alignment(ctx, closest)

    # Higher timeframes: 34/50 on the hour, 20/21 on the day. Both are named
    # constantly on the play sheet and neither was being read until now.
    m1h = mtf_state(hourly_bars, 34, 50) if hourly_bars else None
    m1d = mtf_state(daily_bars, 20, 21) if daily_bars else None
    m1d_5055 = mtf_state(daily_bars, 50, 55) if daily_bars else None
    votes, cast = timeframe_votes(direction, ctx, m1h, m1d)

    d = closest['distance_atr'] if closest else None
    room = room_ahead(ls, closest, direction) if closest and direction else None
    rv = rel_volume(bars10)
    fired = bool(ctx and (ctx.get('fresh_long') or ctx.get('fresh_short')))
    trig = (ctx or {}).get('result', '')

    # Journal edge: how trades taken in this same cloud state performed.
    # Without a cloud read or a direction there is nothing to look up.
    jconds = journal_conditions(ctx, m1h, m1d)
    edge = None
    if ctx and direction and jconds:
        try:
            import conditions as CO
            edge = CO.edge_for(jconds, direction, edge_table)
        except Exception:
            edge = None

    raw = {
        'proximity_atr': d,
        'room_atr': room,
        'alignment_votes': f'{votes}/{cast}' if cast else None,
        'journal_edge': edge['edge'] if edge else None,
        'level_kind': closest['kind'] if closest else None,
        'trigger_fired': fired,
        'confluence_count': shelf['count'] if shelf else 0,
        'rel_volume': rv,
    }

    sub = {
        'proximity': _clamp(1.0 - d / APPROACH_ATR) if d is not None else 0.0,
        # None room means nothing lies beyond the level: open road, full marks.
        'room': 1.0 if room is None and closest else (
            _clamp(room / ROOM_FULL_ATR) if room is not None else 0.0),
        # 0.5 is the book average; no read at all scores nothing.
        'edge': edge['sub'] if edge else 0.0,
        'level_quality': LEVEL_QUALITY.get(
            closest['kind'], 0.5) if closest else 0.0,
        'trigger': 1.0 if fired else 0.0,
        'confluence': _clamp(min(shelf['count'], 4) / 4.0) if shelf and
            shelf.get('distance_atr') is not None and
            shelf['distance_atr'] <= APPROACH_ATR else 0.0,
        'volume': _clamp((rv or 0) / 1.5) if rv is not None else 0.0,
    }

    score = round(sum(sub[k] * W[k] for k in COMPONENTS), 4)
    contrib = {k: round(sub[k] * W[k], 4) for k in COMPONENTS}

    # Plays, if any are written for this ticker. When they exist they are the
    # read -- the weighted score above is a fallback for tickers you are
    # watching but have not written a play for yet.
    graded, best = [], None
    if plays:
        import plays as PL
        st = build_state(ls, ctx, m1h, m1d, m1d_5055, rv)
        graded = PL.grade_ticker(plays, st)
        best = graded[0] if graded else None

    state = 'idle'
    if fired:
        state = 'triggered'
    elif d is not None and d <= NEAR_ATR and aligned:
        state = 'at_level'
    elif d is not None and d <= APPROACH_ATR:
        state = 'approaching'

    return {
        'ticker': ticker,
        'price': ls['price'],
        'atr': ls['atr'],
        'state': state,
        'score': score,
        'components': sub,
        'contribution': contrib,
        'raw': raw,
        'weights': W,
        'direction': direction,
        'aligned': aligned,
        'alignment_why': why,
        'journal_conditions': jconds,
        'edge_matched': (edge or {}).get('matched', []),
        'trend': (ctx or {}).get('trend'),
        'mtf_1h': m1h,
        'mtf_1d': m1d,
        'mtf_1d_5055': m1d_5055,
        'plays': graded,
        'best_play': best,
        'play_grade': best['grade'] if best else None,
        'play_live': bool(best and best['live']),
        'trigger': trig,
        'price_vs_5_12': (ctx or {}).get('price_vs_5_12'),
        'price_vs_34_50': (ctx or {}).get('price_vs_34_50'),
        'closest': closest,
        'near_levels': near[:6],
        'shelf': shelf,
        'note': entry.get('note', ''),
        'catalyst': entry.get('catalyst', False),
        'mtf': entry.get('mtf', False),
        'entry_id': entry.get('id'),
        'has_cloud': ctx is not None,
        'cloud_reason': ctx_reason,
        'at': datetime.now().isoformat(timespec='seconds'),
    }


def rank(rows, slots=DEFAULT_SLOTS, min_state='approaching'):
    """Cap and order.

    A ticker with a LIVE play outranks everything else, best grade first:
    you wrote that condition down this morning and it has fired. Below
    those, the generic proximity read applies as before, so tickers with no
    play written are still surfaced rather than hidden.
    """
    order = {'triggered': 0, 'at_level': 1, 'approaching': 2, 'idle': 3}
    cutoff = order.get(min_state, 2)
    keep = [r for r in rows
            if order.get(r['state'], 3) <= cutoff or r.get('play_live')]
    keep.sort(key=lambda r: (
        0 if r.get('play_live') else 1,
        -(r.get('play_grade') or 0) if r.get('play_live') else 0,
        order.get(r['state'], 3),
        -r['score'],
    ))
    return keep[:slots]


def scan(entries, bar_getter, slots=DEFAULT_SLOTS, min_state='approaching',
         weights=None, plays_by_ticker=None):
    """Score every entry and return the capped board plus everything else.

    bar_getter(ticker) -> (daily_bars, intraday_bars) or
    (daily_bars, intraday_bars, hourly_bars). Injected so this module stays
    testable without Alpaca and without a running bar loop.
    """
    scored, errors = [], []
    for e in entries:
        try:
            got = bar_getter(e['ticker'])
            daily, ten = got[0], got[1]
            hourly = got[2] if len(got) > 2 else None
            scored.append(score_ticker(
                e, daily, ten, hourly_bars=hourly, weights=weights,
                plays=(plays_by_ticker or {}).get(e['ticker'])))
        except Exception as ex:
            errors.append({'ticker': e.get('ticker'),
                           'error': f'{type(ex).__name__}: {ex}'})
    board = rank(scored, slots=slots, min_state=min_state)
    # Keyed on entry id, not ticker: the same symbol can legitimately appear
    # twice (two plays, two notes), and keying on ticker silently drops one.
    on_board = {id(r) for r in board}
    return {
        'board': board,
        'watched': len(entries),
        'scored': len(scored),
        'bench': sorted([r for r in scored if id(r) not in on_board],
                        key=lambda r: -r['score']),
        'errors': errors,
        'slots': slots,
        'at': datetime.now().isoformat(timespec='seconds'),
    }


# ─── ALERT DEDUPE ──────────────────────────────────────────────────────────────

class AlertGate:
    """Decides whether a state change is worth telling the human about.

    Without this, a ticker hovering either side of NEAR_ATR alerts on every
    bar. An alert fires only when a ticker's state ESCALATES, and then not
    again for that state until it cools off or the cooldown expires.
    """

    def __init__(self, cooldown_minutes=30):
        self.cooldown = cooldown_minutes
        self._last = {}          # ticker -> (state, datetime)

    RANK = {'idle': 0, 'approaching': 1, 'at_level': 2, 'triggered': 3}

    def should_alert(self, row, now=None):
        now = now or datetime.now()
        state = row['state']
        if self.RANK.get(state, 0) < self.RANK['at_level']:
            self._last[row['ticker']] = (state, now)
            return False
        prev = self._last.get(row['ticker'])
        self._last[row['ticker']] = (state, now)
        if prev is None:
            return True
        prev_state, prev_at = prev
        if self.RANK.get(state, 0) > self.RANK.get(prev_state, 0):
            return True
        if state == prev_state:
            return (now - prev_at).total_seconds() / 60 >= self.cooldown
        return False

    def reset(self):
        self._last.clear()
