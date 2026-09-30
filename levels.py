"""
levels.py — Foundations Trading

Computes the reference levels Ripster plays are written against.

THE POINT
  A play sheet says things like "34/50 curl long VS PML, bullish bias over
  PDC, short below PML". That reads as qualitative, but PML and PDC are just
  numbers derived from bars. The vocabulary in those notes is a CLOSED SET:

    PMH PML          PRE-MARKET high / low, today's 04:00-09:30 ET session
    PDH PDL PDC      prior day high / low / close
    PWH PWL          prior week high / low
    PMonH PMonL      prior month high / low
    ATH              running high over available history
    P R1 S1 R2 S2    classic floor pivots off the prior session
    psych            round numbers near price
    manual           the support/resistance zones typed in from the sheet

  So the notes never have to be parsed. Every level they can name is
  computed for every ticker, and the note is shown to the human verbatim
  when something happens near one.

DISTANCE IS IN ATR, NOT DOLLARS
  "Near a level" cannot be a dollar amount: 0.50 is noise on a $1,870 stock
  and a full day's range on a $17 one. Every distance here is expressed in
  multiples of daily ATR, which makes SNDK and RGTI directly comparable.
  Most of what feels un-quantifiable about these setups is a units problem.

This module MEASURES. It never decides what is noteworthy — that is the
screener's job, and even there the ranking is by adjacency, not quality.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


# ─── SMALL HELPERS ─────────────────────────────────────────────────────────────

def _f(x):
    try:
        v = float(x)
        return v if v == v else None          # NaN check
    except (TypeError, ValueError):
        return None


def _bar_date(b):
    t = b.get('t') or b.get('time') or b.get('timestamp')
    if isinstance(t, datetime):
        return t.date()
    try:
        return datetime.fromisoformat(str(t).replace('Z', '+00:00')).date()
    except ValueError:
        return None


def _ohlc(b):
    return (_f(b.get('h') or b.get('high')),
            _f(b.get('l') or b.get('low')),
            _f(b.get('c') or b.get('close')),
            _f(b.get('o') or b.get('open')))


def atr(daily_bars, period=14):
    """Average true range on daily bars. None if there is not enough history."""
    bars = [b for b in (daily_bars or []) if all(
        v is not None for v in _ohlc(b)[:3])]
    if len(bars) < 2:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        h, l, _c, _o = _ohlc(cur)
        pc = _ohlc(prev)[2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    window = trs[-period:]
    return sum(window) / len(window)


# ─── PSYCH LEVELS ──────────────────────────────────────────────────────────────

def psych_step(price):
    """Round-number spacing that matches the instrument's scale.

    A $17 name coils around whole dollars; a $1,870 name coils around 50s.
    One fixed step would either flood the small names or miss the big ones.
    """
    p = abs(price or 0)
    if p < 5:
        return 0.50
    if p < 25:
        return 1.0
    if p < 100:
        return 5.0
    if p < 300:
        return 10.0
    if p < 1000:
        return 25.0
    return 50.0


# Nate's three classes, 2026-09-30, and ONLY his three. He asked for psych
# levels "weighted with strong importance... especially at levels which are
# 10s, 50s, or 100s, relative to stock price."
#
# NOT A GENERIC DECADE LADDER. A first draft used one and made $75 outrank $70
# on a $66 stock, because 75 is a multiple of 25. There is nothing
# psychological about $75.
PSYCH_CLASSES = (10.0, 50.0, 100.0)


def psych_tier(value, price):
    """How many of the 10/50/100 classes a round number satisfies. 0-3.

    Only classes LARGER than this instrument's own grid step count, and that
    is the "relative to stock price" in the instruction. On a $2,000 name
    psych_step is already $50, so being a multiple of 50 says nothing about a
    level and only the 100s separate; on a $66 name the step is $5 and all
    three classes discriminate.

    Nate's worked case, a $66 stock (step $5):
        $65 -> 0 (base grid)    $70 -> 1 (a ten)
        $60 -> 1 (a ten)        $50 -> 2 (a ten and a fifty)
        $75 -> 0 -- see PSYCH_CLASSES

    This reports a FACT about the number. Whether a tier earns credit also
    depends on distance, and that judgement belongs to grading.py's B2
    (design/01 §9.2.1), not here.
    """
    v, p = _f(value), _f(price)
    if v is None or p is None or v <= 0:
        return 0
    base = psych_step(p)
    return sum(1 for c in PSYCH_CLASSES
               if c > base and abs(v / c - round(v / c)) < 1e-9)


def psych_levels(price, span_atr=2.0, atr_value=None):
    """Round numbers within reach of price."""
    if price is None:
        return []
    step = psych_step(price)
    reach = (atr_value or step * 2) * span_atr
    lo, hi = price - reach, price + reach
    first = int(lo / step) * step
    out, v = [], first
    while v <= hi + 1e-9:
        if v > 0 and abs(v - price) <= reach:
            out.append(round(v, 4))
        v += step
    return out


# ─── PRE-MARKET ────────────────────────────────────────────────────────────────

PREMARKET_START = (4, 0)      # 04:00 ET, when the pre-market session opens
PREMARKET_END = (9, 30)       # 09:30 ET, the cash open


def _bar_dt_et(b):
    """A bar's timestamp as Eastern wall-clock.

    Alpaca stamps bars in UTC. Comparing those against 04:00-09:30 without
    converting would select the wrong five hours entirely -- 04:00 UTC is
    midnight Eastern, which is not a session at all.
    """
    t = b.get('time') or b.get('t') or b.get('timestamp')
    if t is None:
        return None
    if not hasattr(t, 'astimezone'):
        try:
            t = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
        except ValueError:
            return None
    if t.tzinfo is None:
        return t                      # already local wall-clock
    return t.astimezone(ET).replace(tzinfo=None)


def premarket_extremes(intraday_bars, session_date=None):
    """High and low of the pre-market session, from intraday bars.

    session_date defaults to the date of the most recent bar, so this works
    the same whether it runs during pre-market, during the session, or on a
    replay of yesterday.

    Returns None when no bars fall in the window. That happens legitimately
    -- a name with no pre-market prints, or a data feed that does not carry
    extended hours -- and None means "unknown", never zero.
    """
    rows = []
    for b in intraday_bars or []:
        dt = _bar_dt_et(b)
        h, l = _f(b.get('h') or b.get('high')), _f(b.get('l') or b.get('low'))
        if dt is None or h is None or l is None:
            continue
        rows.append((dt, h, l))
    if not rows:
        return None

    day = session_date or max(r[0] for r in rows).date()
    lo_t = time(*PREMARKET_START)
    hi_t = time(*PREMARKET_END)
    window = [r for r in rows
              if r[0].date() == day and lo_t <= r[0].time() < hi_t]
    if not window:
        return None
    return {'high': max(r[1] for r in window),
            'low': min(r[2] for r in window),
            'bars': len(window)}


# ─── PERIOD EXTREMES ───────────────────────────────────────────────────────────

def _prior_period(daily_bars, key):
    """High/low/close of the last COMPLETE prior day, week or month.

    'Prior' means finished. The current day, week or month is excluded, so
    PDH on a Tuesday is Monday's high and does not creep as today trades.
    """
    bars = [b for b in (daily_bars or []) if _bar_date(b)]
    if not bars:
        return None
    bars.sort(key=_bar_date)
    today = _bar_date(bars[-1])

    def bucket(d):
        if key == 'day':
            return d
        if key == 'week':
            return (d - timedelta(days=d.weekday()))
        return (d.year, d.month)

    cur = bucket(today)
    prior = [b for b in bars if bucket(_bar_date(b)) != cur]
    if not prior:
        return None
    last = bucket(_bar_date(prior[-1]))
    group = [b for b in prior if bucket(_bar_date(b)) == last]
    highs = [_ohlc(b)[0] for b in group if _ohlc(b)[0] is not None]
    lows = [_ohlc(b)[1] for b in group if _ohlc(b)[1] is not None]
    closes = [_ohlc(b)[2] for b in group if _ohlc(b)[2] is not None]
    if not highs or not lows:
        return None
    return {'high': max(highs), 'low': min(lows),
            'close': closes[-1] if closes else None,
            'open': _ohlc(group[0])[3]}


def floor_pivots(prior_day):
    """Classic floor-trader pivots off the prior session."""
    if not prior_day or prior_day.get('close') is None:
        return {}
    h, l, c = prior_day['high'], prior_day['low'], prior_day['close']
    p = (h + l + c) / 3.0
    return {
        'P':  p,
        'R1': 2 * p - l,
        'S1': 2 * p - h,
        'R2': p + (h - l),
        'S2': p - (h - l),
    }


# ─── THE LEVEL SET ─────────────────────────────────────────────────────────────

# Anything the play-sheet notes can name. Ordered roughly by how often it
# gets referenced, which is also how the UI lists them.
LEVEL_ORDER = ['manual_support', 'manual_resistance',
               'PMH', 'PML',
               'PDH', 'PDL', 'PDC', 'PDO',
               'PWH', 'PWL', 'PMonH', 'PMonL',
               'ATH', 'P', 'R1', 'S1', 'R2', 'S2', 'psych']


def build(daily_bars, price, manual=None, atr_value=None,
          include_psych=True, psych_span_atr=2.0, intraday_bars=None):
    """Every reference level for one ticker.

    manual: {'support': [..], 'resistance': [..]} — the pivot zones typed in
    from the sheet. Each zone may hold several prices; the sheet quotes them
    as bands (239.25/239) because a level is a band, not a line.

    Returns a list of dicts: name, kind, price, distance, distance_atr, side.
    """
    price = _f(price)
    a = atr_value if atr_value is not None else atr(daily_bars)

    day = _prior_period(daily_bars, 'day')
    week = _prior_period(daily_bars, 'week')
    month = _prior_period(daily_bars, 'month')

    out = []

    def add(name, kind, value, tier=None):
        v = _f(value)
        if v is None or v <= 0:
            return
        # tier is the psych magnitude class (psych_tier); None on every other
        # kind, and present on all of them so a consumer never has to .get().
        out.append({'name': name, 'kind': kind, 'price': round(v, 4),
                    'tier': tier})

    for label, prices in (manual or {}).items():
        key = f'manual_{label}'
        for v in (prices if isinstance(prices, (list, tuple)) else [prices]):
            add(key, 'manual', v)

    if day:
        add('PDH', 'prior_day', day['high'])
        add('PDL', 'prior_day', day['low'])
        add('PDC', 'prior_day', day['close'])
        add('PDO', 'prior_day', day['open'])
    if week:
        add('PWH', 'prior_week', week['high'])
        add('PWL', 'prior_week', week['low'])
    if month:
        add('PMonH', 'prior_month', month['high'])
        add('PMonL', 'prior_month', month['low'])

    # Pre-market. These are the PMH/PML the play sheet means, and they need
    # intraday bars -- a daily bar has no 04:00-09:30 detail in it.
    pre = premarket_extremes(intraday_bars)
    if pre:
        add('PMH', 'premarket', pre['high'])
        add('PML', 'premarket', pre['low'])

    highs = [_ohlc(b)[0] for b in (daily_bars or []) if _ohlc(b)[0] is not None]
    if highs:
        add('ATH', 'extreme', max(highs))

    for name, v in floor_pivots(day).items():
        add(name, 'floor_pivot', v)

    if include_psych and price:
        for v in psych_levels(price, psych_span_atr, a):
            add('psych', 'psych', v, tier=psych_tier(v, price))

    # Distances last, once every level is in hand.
    for lv in out:
        if price is None:
            lv['distance'] = lv['distance_atr'] = None
            lv['side'] = None
            continue
        d = lv['price'] - price
        lv['distance'] = round(d, 4)
        lv['distance_atr'] = round(abs(d) / a, 3) if a else None
        lv['side'] = 'above' if d > 0 else ('below' if d < 0 else 'at')

    out.sort(key=lambda l: (l['distance_atr'] is None, l['distance_atr'] or 0))
    return {'price': price, 'atr': round(a, 4) if a else None, 'levels': out}


def nearest(level_set, within_atr=None, limit=None):
    """Levels closest to price, optionally capped by distance."""
    rows = level_set['levels']
    if within_atr is not None:
        rows = [l for l in rows if l['distance_atr'] is not None
                and l['distance_atr'] <= within_atr]
    return rows[:limit] if limit else rows


def confluence(level_set, band_atr=0.15):
    """Groups of distinct levels sitting on top of each other.

    Three levels inside a fifth of an ATR is a shelf, and a shelf is where
    these plays tend to resolve. Reported as a fact about the chart, not as
    a recommendation.
    """
    rows = [l for l in level_set['levels'] if l['distance_atr'] is not None]
    rows.sort(key=lambda l: l['price'])
    a = level_set.get('atr')
    if not a or not rows:
        return []

    # Chaining guard. Testing only the gap to the previous level lets a run
    # of evenly spaced levels grow without bound -- seven levels each 0.1 ATR
    # apart would report as one 0.6 ATR "shelf", which is a range, not a
    # shelf. The group's total width is capped as well as the gap.
    max_width = band_atr * 2.5
    groups, cur = [], [rows[0]]
    for lv in rows[1:]:
        gap_ok = abs(lv['price'] - cur[-1]['price']) / a <= band_atr
        width_ok = abs(lv['price'] - cur[0]['price']) / a <= max_width
        if gap_ok and width_ok:
            cur.append(lv)
        else:
            if len(cur) > 1:
                groups.append(cur)
            cur = [lv]
    if len(cur) > 1:
        groups.append(cur)

    return [{
        'names': [l['name'] for l in g],
        'low': round(min(l['price'] for l in g), 4),
        'high': round(max(l['price'] for l in g), 4),
        'mid': round(sum(l['price'] for l in g) / len(g), 4),
        'count': len(g),
        'distance_atr': round(min(l['distance_atr'] for l in g), 3),
    } for g in groups]


# ─── SELFTEST ──────────────────────────────────────────────────────────────────

def selftest():
    """Covers psych_step / psych_tier and the tier's arrival in build().

    This file had no selftest before 2026-09-30. It gained one because
    psych_tier is arithmetic that decides GPA credit (design/01 §9.2.1), and
    the first draft of it was wrong in a way that only a worked case caught.
    """
    fails = []

    def ck(label, cond, got=None):
        if cond:
            print(f'  ok   {label}')
        else:
            fails.append(label)
            print(f'  FAIL {label}' + (f'   got {got!r}' if got is not None else ''))

    print('psych_step scales with the instrument')
    ck('a $17 name coils on whole dollars', psych_step(17) == 1.0)
    ck('a $66 name coils on fives', psych_step(66) == 5.0)
    ck('a $660 name coils on twenty-fives', psych_step(660) == 25.0)
    ck('a $1,870 name coils on fifties', psych_step(1870) == 50.0)

    print("psych_tier -- Nate's worked case, a $66 stock")
    ck('$65 is base grid', psych_tier(65, 66) == 0, psych_tier(65, 66))
    ck('$70 is a ten', psych_tier(70, 66) == 1, psych_tier(70, 66))
    ck('$60 is a ten', psych_tier(60, 66) == 1, psych_tier(60, 66))
    ck('$50 is a ten AND a fifty', psych_tier(50, 66) == 2, psych_tier(50, 66))
    ck('$100 is all three', psych_tier(100, 66) == 3, psych_tier(100, 66))
    ck('$75 is NOT promoted -- 25 is not one of the classes',
       psych_tier(75, 66) == 0, psych_tier(75, 66))
    ck('$55 is base grid', psych_tier(55, 66) == 0, psych_tier(55, 66))

    print('psych_tier is relative to the price, not absolute')
    ck('on a $660 name a fifty still separates', psych_tier(650, 660) == 1)
    ck('on a $660 name a hundred outranks it', psych_tier(700, 660) == 2)
    ck('on a $660 name the base grid is flat', psych_tier(675, 660) == 0)
    ck('on a $2,000 name only hundreds separate, because the step IS fifty',
       (psych_tier(2000, 2000), psych_tier(2050, 2000)) == (1, 0),
       (psych_tier(2000, 2000), psych_tier(2050, 2000)))
    ck('the same $70 means nothing on a $2,000 name',
       psych_tier(70, 2000) == 0, psych_tier(70, 2000))

    print('psych_tier refuses to guess')
    ck('no price -> 0', psych_tier(70, None) == 0)
    ck('no value -> 0', psych_tier(None, 66) == 0)
    ck('a non-positive level -> 0', psych_tier(0, 66) == 0)

    print('build() carries the tier out')
    ls = build([], 66.0, atr_value=5.0)
    psych = {l['price']: l['tier'] for l in ls['levels'] if l['kind'] == 'psych'}
    ck('the $70 level arrives tiered', psych.get(70.0) == 1, psych)
    ck('the $65 level arrives untiered', psych.get(65.0) == 0, psych)
    ck('every level dict has the key, so no consumer needs .get()',
       all('tier' in l for l in ls['levels']))
    ck('a non-psych level carries None, not 0',
       all(l['tier'] is None for l in build([], 66.0, atr_value=5.0,
                                           manual={'support': [64.13]})['levels']
           if l['kind'] != 'psych'))

    print('the distance half of the B2 rule is levels\u0027 job to REPORT only')
    ls = build([], 66.0, atr_value=5.0)
    by = {l['price']: l['distance_atr'] for l in ls['levels'] if l['kind'] == 'psych'}
    ck('$70 is inside one daily ATR', by.get(70.0) is not None and by[70.0] <= 1.0,
       by.get(70.0))
    ck('$60 is outside it, which is what separates them in his example',
       by.get(60.0) is not None and by[60.0] > 1.0, by.get(60.0))

    print()
    if fails:
        print(f'SELFTEST FAILED -- {len(fails)} of the above')
    else:
        print('SELFTEST PASSED')
    return not fails


if __name__ == '__main__':
    import sys
    sys.exit(0 if selftest() else 1)
