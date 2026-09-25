"""
backtest_context.py — Foundations Trading

A CONSUMER of backtester output. Takes the trades `run_symbol()` /
`run_basket()` produced plus the bars they were replayed over, and splits
them by the MARKET CONTEXT each trade was entered in: relative volume, ATR
regime, time of day.

WHY THIS EXISTS

  `compute_metrics()` answers "did this configuration make money." That is
  one number over one sample, and it is an average of conditions that never
  co-occurred. A fast timeframe can be excellent on high-relative-volume
  expansion days and awful on quiet contracting ones; the mean of those two
  describes no day that ever happened, and tuning against it tunes against
  a fiction.

  So: same trades, no re-run, sliced by the environment they were taken in.
  The question changes from "does this work" to "when did this work", which
  is the only version of the question the data can actually answer.

DESCRIPTIVE, NOT PREDICTIVE

  Every number in here is a statement about ONE historical sample: these
  trades, these symbols, these dates. A bucket that shows +0.4% per trade
  did well over that stretch. It is not a forecast, it is not an edge, and
  it carries no claim that the next high-RVOL day pays. Nothing in this
  module is fit, cross-validated, or held out. Read it as a description of
  what happened, and treat any bucket you like as a hypothesis to test on a
  different date range, not as a finding.

SLICING A SMALL SAMPLE IS THE WHOLE PROBLEM

  This is the failure mode the module is built around. A few hundred trades
  cut four ways produces cells of n=6, and cells of n=6 show enormous
  spurious edges every single time. Reporting those raw would be worse than
  reporting nothing, because they look like discoveries.

  So the statistics are `conditions.py`'s, imported rather than
  reimplemented, for the same reason and with the same constants:

    shrinkage   each bucket's average is pulled toward the book average by
                PRIOR pseudo-trades before it is scored, so a bucket needs
                real weight of evidence before its score moves off zero.
    Welch t     the bucket against every trade outside it, unpooled
                variance. Drives the confidence label only.
    THIN        below THIN trades the label is 'thin' and the bucket is
                marked `interpretable: False`.

  MIN_N is THIN, deliberately the same number and not a second one, so the
  two files cannot drift apart.

  The enforcement is structural, not advisory: `findings` — the only place
  this module ever states a conclusion — is BUILT by filtering on
  `interpretable` and a non-noise label. A thin bucket cannot reach it. The
  bucket is still reported in full, with its `n` beside its result, because
  hiding a thin cell is its own kind of lie: you would not know the split
  had been tried.

  ONE DELIBERATE DEVIATION from conditions.py: the headline variable is
  `ret_pct`, not dollars. The journal's legs are what they are; a backtest's
  per-trade dollars are an artifact of the pinned budget, so with
  `budget_by_symbol` weights a big-budget name would dominate every bucket
  on notional alone. `t` and `score_pct` are computed on return percent;
  `t_pl` and `score_pl` on dollars are reported alongside. For a flat
  `alloc_pct` run the two orderings agree.

MEASUREMENT AT ENTRY, NEVER AFTER

  A trade's context is the context at its ENTRY bar. Exit-time context is
  partly a consequence of the trade (a winner ran because the day expanded),
  so bucketing on it would build the same hindsight leak `conditions.py`
  quarantines its exit tags for. Every baseline here uses strictly prior
  bars: prior sessions for the volume baseline, prior sessions' dailies for
  ATR, today's bars only up to the entry timestamp.

THE THRESHOLDS ARE NOT NEW

  `design/02-measurements.md` already specifies relative volume and ATR
  regime for the screener's environment GPA, with its thresholds argued. The
  bands here are that document's, to the number, so a backtest bucket and a
  live GPA read mean the same thing by 'hot' or 'contracting'. When
  `environment.py` is implemented, DELETE the constants below and import
  them from it. Two copies of a threshold is how two copies of a threshold
  disagree.

WHAT THIS DOES NOT DO, ON PURPOSE

  No re-running, no optimizing, no parameter search. It does not pick a
  winning bucket and re-fit to it; that is how a backtest gets sawn into
  overfit slivers. It reports and stops.

  No `used` (today's range spent against ATR) even though
  design/02 §4 computes it. It is a within-session path variable — by 14:00
  it partly encodes what already happened — so it sits closer to a signal
  than to a regime, and it does not belong on the same axis as the other
  three.

  No market-wide regime (SPY/VIX). Every read here is per-symbol and
  computable from the bars the backtest already held. Adding an index feed
  would add a fetch, a cache and a failure mode for a dimension nobody has
  asked for yet.

OUTPUT SHAPE IS THE JOURNAL HEATMAP'S

  Each bucket is literally `heatmap._cell()`, called on the trades projected
  into journal-leg field names. Same keys, same win-rate-excludes-scratches
  convention, same sparse flag. A pair of dimensions comes back as a grid
  with `cells` keyed 'row|col' plus row and column margins, which is the
  shape `heatmap.build()` returns; `to_heatmap_grid()` renames the two axis
  keys to `grades`/`exit_keys` so an existing grid renderer takes it
  unmodified. The axis LABELS differ (they are context bands, not 0-4
  grades) and nothing else does.

Pure. Bars and trades in, buckets out. No Flask, no I/O, no module state.
"""

from datetime import datetime, timedelta, time as dtime
from statistics import median
from zoneinfo import ZoneInfo

import conditions as CD
import heatmap as HM
import levels as LV

ET = ZoneInfo('America/New_York')

# Borrowed outright so the two files cannot drift. See the docstring.
PRIOR = CD.PRIOR          # 10 pseudo-trades of shrinkage toward the book
THIN = CD.THIN            # 8
MIN_N = THIN              # below this a bucket is REPORTED but NOT INTERPRETED

# ─── CONTEXT THRESHOLDS ───────────────────────────────────────────────────────
# Every one of these is design/02-measurements.md's, unchanged. The basis for
# each is argued there; the short version is in design/06-backtest-context.md.

RVOL_SESSIONS = 10        # prior sessions in the time-of-day volume baseline
RVOL_MIN_SESSIONS = 3     # below this the baseline is None, not a guess
RVOL_HOT = 1.50
RVOL_NORMAL = 1.00
RVOL_LIGHT = 0.60

ATR_PERIOD = 14           # levels.atr's own default, for atr_pct
ATR_FAST = 5              # ATR(5) / ATR(20) is the expansion read
ATR_SLOW = 20
ATR_PCT_WIDE = 0.035
ATR_PCT_OK = 0.020
ATR_PCT_THIN = 0.012
ATR_TREND_EXPANDING = 1.15
ATR_TREND_CONTRACTING = 0.85

RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)

# Time-of-day cuts, ET, as (label, start, end_exclusive). The four RTH blocks
# are the owner's own; premarket and after-hours exist because the SIP feed
# prints 04:00-20:00 and a trade entered there must land somewhere it can be
# counted rather than vanish into 'unknown'.
TOD_BUCKETS = [
    ('premarket',   dtime(0, 0),   dtime(9, 30)),
    ('09:30-10:00', dtime(9, 30),  dtime(10, 0)),
    ('10:00-12:00', dtime(10, 0),  dtime(12, 0)),
    ('12:00-14:00', dtime(12, 0),  dtime(14, 0)),
    ('14:00-16:00', dtime(14, 0),  dtime(16, 0)),
    ('after-hours', dtime(16, 0),  dtime(23, 59, 59)),
]
TOD_ORDER = [b[0] for b in TOD_BUCKETS]
FIRST30 = '09:30-10:00'

UNKNOWN = 'unknown'       # a real bucket, never a silent drop

DIMENSIONS = {
    'tod': {
        'label': 'Time of day (ET)',
        'values': TOD_ORDER,
        'note': 'entry bar timestamp, converted to America/New_York',
    },
    'rvol': {
        'label': 'Relative volume',
        'values': ['hot', 'normal', 'light', 'dead'],
        'note': f'cumulative RTH volume to the entry bar vs the median of '
                f'the prior {RVOL_SESSIONS} sessions at the same time of day',
    },
    'atr_pct': {
        'label': 'ATR % of price',
        'values': ['wide', 'ok', 'thin', 'dead'],
        'note': f'ATR({ATR_PERIOD}) on prior daily bars, over the entry price',
    },
    'atr_trend': {
        'label': 'ATR regime',
        'values': ['expanding', 'steady', 'contracting'],
        'note': f'ATR({ATR_FAST}) / ATR({ATR_SLOW}) on prior daily bars',
    },
}

# Pairs worth cutting, chosen rather than enumerated. Every pair costs sample:
# four dimensions is six pairs and 100+ cells, and a few hundred trades cannot
# populate that. These three are the ones that answer a question someone asked.
DEFAULT_PAIRS = [
    ('tod', 'rvol'),        # is the open special, or is the open just volume?
    ('tod', 'atr_trend'),   # does the open hold up on contracting days?
    ('rvol', 'atr_trend'),  # do the two environment reads say the same thing?
]


# ─── TIME HANDLING ────────────────────────────────────────────────────────────

def _parse_et(ts):
    """ISO timestamp -> ET-aware datetime, or None.

    A NAIVE timestamp is read as UTC, because that is what produced it:
    alpaca_manager emits `b.timestamp.isoformat()` from a UTC-aware bar and
    backtester copies it through verbatim. Assuming local time instead would
    silently shift every bucket boundary by the machine's offset, which on a
    laptop that travels is a different answer on a different day.
    """
    if isinstance(ts, datetime):
        dt = ts
    elif ts:
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo('UTC'))
    return dt.astimezone(ET)


def _tod_bucket(dt_et):
    if dt_et is None:
        return UNKNOWN
    t = dt_et.time()
    for label, lo, hi in TOD_BUCKETS:
        if lo <= t < hi:
            return label
    return 'after-hours'


def _minutes_from_open(dt_et):
    return (dt_et.hour * 60 + dt_et.minute + dt_et.second / 60.0) - (9 * 60 + 30)


# ─── PER-SYMBOL BAR STRUCTURE ─────────────────────────────────────────────────

def _bar_width_minutes(bars):
    """Modal gap between consecutive bars, in minutes. None if undecidable.

    Measured, not passed in. The caller handing us `bars_by_symbol` does not
    necessarily know which timeframe the engine ran on, and getting this
    wrong shifts every volume bucket.
    """
    gaps = {}
    prev = None
    for b in bars:
        t = _parse_et(b.get('time'))
        if t is None:
            continue
        if prev is not None:
            g = round((t - prev).total_seconds() / 60.0, 3)
            if 0 < g <= 240:
                gaps[g] = gaps.get(g, 0) + 1
        prev = t
    if not gaps:
        return None
    return max(gaps.items(), key=lambda kv: kv[1])[0]


def _synth_daily(rth_by_date):
    """Daily OHLCV built from the session's regular-hours intraday bars.

    NOT identical to Alpaca's daily bars. Alpaca's daily open and close are
    the official auction prints; the first and last RTH 10-min bar are close
    to them but not the same tick, and the high/low can differ by a print
    that landed outside the intraday bar grid. The error is small and it is
    the same sign every day, so ATR ratios are barely affected and `atr_pct`
    may run a hair narrow.

    This exists because `backtest_run.py` fetches primary and macro bars and
    no dailies at all. Pass `daily_by_symbol` to `annotate()` and this is
    never called — that is the better input and it is worth fetching if you
    have the request budget.
    """
    out = []
    for d in sorted(rth_by_date):
        bars = rth_by_date[d]
        if not bars:
            continue
        highs = [b.get('high') for b in bars if b.get('high') is not None]
        lows = [b.get('low') for b in bars if b.get('low') is not None]
        if not highs or not lows:
            continue
        out.append({
            'time': d.isoformat(),
            'open': bars[0].get('open'), 'close': bars[-1].get('close'),
            'high': max(highs), 'low': min(lows),
            'volume': sum(b.get('volume') or 0.0 for b in bars),
            'date': d,
        })
    return out


def _prep_symbol(bars, daily_bars=None):
    """Everything the context reads need for one symbol, computed once.

    Returns None when there is nothing usable, so the caller degrades to
    unknown rather than dividing by a structure it half-built.
    """
    rows = []
    for b in bars or []:
        t = _parse_et(b.get('time'))
        if t is None:
            continue
        rows.append((t, b))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])

    width = _bar_width_minutes([b for _t, b in rows])

    rth_by_date = {}
    for t, b in rows:
        if RTH_OPEN <= t.time() < RTH_CLOSE:
            rth_by_date.setdefault(t.date(), []).append(b)

    # Per session: [(minutes-from-09:30 of the bar's CLOSE, cumulative volume)]
    cum_by_date = {}
    for d, bs in rth_by_date.items():
        running = 0.0
        series = []
        for b in bs:
            t = _parse_et(b.get('time'))
            running += float(b.get('volume') or 0.0)
            close_min = _minutes_from_open(t) + (width or 0.0)
            series.append((close_min, running))
        cum_by_date[d] = series

    if daily_bars:
        dl = []
        for b in daily_bars:
            t = _parse_et(b.get('time'))
            if t is None:
                continue
            d = dict(b)
            d['date'] = t.date()
            dl.append(d)
        dl.sort(key=lambda b: b['date'])
    else:
        dl = _synth_daily(rth_by_date)

    return {
        'width': width,
        'sessions': sorted(cum_by_date),
        'cum_by_date': cum_by_date,
        'daily': dl,
        'daily_source': 'provided' if daily_bars else 'synthesized-from-rth',
    }


# ─── THE THREE CONTEXT READS ──────────────────────────────────────────────────

def _cum_through(series, elapsed_min):
    """Cumulative volume through the last bar that CLOSED at or before
    `elapsed_min` minutes after 09:30. None if no bar had closed yet."""
    best = None
    for close_min, running in series:
        if close_min <= elapsed_min + 1e-9:
            best = running
        else:
            break
    return best


def rel_volume(prep, dt_et, sessions=RVOL_SESSIONS,
               min_sessions=RVOL_MIN_SESSIONS):
    """Cumulative relative volume at `dt_et` against the same time of day on
    prior sessions. Returns {'rvol', 'band', 'n_sessions', 'reason'}.

    Strictly prior sessions and strictly closed bars: a bar is counted only
    once its close is at or before the entry timestamp, which for a
    next-bar-open fill is every bar the engine could have seen and no others.

    MEDIAN over the baseline sessions, not mean, because one earnings gap
    triples a mean for a month.
    """
    out = {'rvol': None, 'band': None, 'today_cum': None, 'base_cum': None,
           'n_sessions': None, 'reason': ''}
    if prep is None or dt_et is None:
        out['reason'] = 'no bars for this symbol'
        return out
    if not (RTH_OPEN <= dt_et.time() < RTH_CLOSE):
        out['reason'] = 'entry outside regular hours; no time-of-day baseline'
        return out

    d = dt_et.date()
    elapsed = _minutes_from_open(dt_et)
    today = prep['cum_by_date'].get(d)
    if not today:
        out['reason'] = 'no RTH bars on the entry session'
        return out
    today_cum = _cum_through(today, elapsed)
    if today_cum is None or today_cum <= 0:
        out['reason'] = 'no closed RTH bar before the entry'
        return out

    prior = [s for s in prep['sessions'] if s < d][-sessions:]
    vals = [v for v in (_cum_through(prep['cum_by_date'][s], elapsed)
                        for s in prior) if v and v > 0]
    out['today_cum'] = round(today_cum, 1)
    out['n_sessions'] = len(vals)
    if len(vals) < min_sessions:
        out['reason'] = (f'{len(vals)} prior sessions at this time of day, '
                         f'need {min_sessions}')
        return out

    base = median(vals)
    rv = today_cum / base
    out['base_cum'] = round(base, 1)
    out['rvol'] = round(rv, 3)
    out['band'] = ('hot' if rv >= RVOL_HOT else
                   'normal' if rv >= RVOL_NORMAL else
                   'light' if rv >= RVOL_LIGHT else 'dead')
    out['reason'] = f'{rv:.2f}x the {len(vals)}-session median at +{elapsed:.0f}min'
    return out


def atr_regime(prep, dt_et, price=None):
    """ATR as a share of price, and ATR(5)/ATR(20), on sessions STRICTLY
    BEFORE the entry date.

    Excluding the entry session is not fussiness. Including it would fold the
    day's own realized range into the regime the trade was supposedly taken
    in, and days with big ranges are days with big trade outcomes: the
    bucketing would then partly sort trades by their own results.
    """
    out = {'atr': None, 'atr_pct': None, 'atr_pct_band': None,
           'atr_trend': None, 'atr_trend_band': None, 'n_daily': 0,
           'reason': ''}
    if prep is None or dt_et is None:
        out['reason'] = 'no bars for this symbol'
        return out
    prior = [b for b in prep['daily'] if b['date'] < dt_et.date()]
    out['n_daily'] = len(prior)
    if len(prior) < ATR_PERIOD + 1:
        out['reason'] = (f'{len(prior)} prior daily bars, '
                         f'need {ATR_PERIOD + 1}')
        return out

    a = LV.atr(prior, ATR_PERIOD)
    if a is None or a <= 0:
        out['reason'] = 'ATR undefined on the prior dailies'
        return out
    out['atr'] = round(a, 4)

    px = price if price else prior[-1].get('close')
    if px:
        pct = a / float(px)
        out['atr_pct'] = round(pct, 5)
        out['atr_pct_band'] = ('wide' if pct >= ATR_PCT_WIDE else
                               'ok' if pct >= ATR_PCT_OK else
                               'thin' if pct >= ATR_PCT_THIN else 'dead')

    if len(prior) >= ATR_SLOW + 1:
        fast, slow = LV.atr(prior, ATR_FAST), LV.atr(prior, ATR_SLOW)
        if fast is not None and slow:
            r = fast / slow
            out['atr_trend'] = round(r, 3)
            out['atr_trend_band'] = (
                'expanding' if r >= ATR_TREND_EXPANDING else
                'contracting' if r <= ATR_TREND_CONTRACTING else 'steady')
        else:
            out['reason'] = 'ATR ratio undefined'
    else:
        # Not an error. backtest_run's warmup pad for 300 10-min bars is about
        # 15 sessions, so the earliest trades in a run genuinely cannot have
        # an ATR(20). They report None and are counted as 'unknown'.
        out['reason'] = (f'{len(prior)} prior dailies, ATR({ATR_SLOW}) needs '
                         f'{ATR_SLOW + 1}; trend read unavailable')
    return out


# ─── ANNOTATION ───────────────────────────────────────────────────────────────

def annotate(trades, bars_by_symbol=None, daily_by_symbol=None, *,
             rvol_sessions=RVOL_SESSIONS,
             rvol_min_sessions=RVOL_MIN_SESSIONS):
    """One context dict per trade, in the trades' own order.

    Public because joining context onto trades is useful on its own — a CSV
    of trades with an `rvol` column answers questions this module does not
    ask. `bucket()` calls it and does not repeat the work.

    Each context carries the band for every dimension (or `None`), the raw
    scalars the band came from, and a `reason` per read saying why a `None`
    is `None`. A missing read is never filled with a default: 'unknown' is a
    bucket you can see and count, a defaulted 'normal' is a wrong number
    wearing the right clothes.
    """
    bars_by_symbol = bars_by_symbol or {}
    daily_by_symbol = daily_by_symbol or {}
    preps = {}

    out = []
    for t in trades or []:
        sym = t.get('symbol')
        if sym not in preps:
            preps[sym] = _prep_symbol(bars_by_symbol.get(sym),
                                      daily_by_symbol.get(sym))
        prep = preps[sym]
        dt = _parse_et(t.get('entry_time'))

        rv = rel_volume(prep, dt, rvol_sessions, rvol_min_sessions)
        ar = atr_regime(prep, dt, _f(t.get('entry_price')))
        out.append({
            'symbol': sym,
            'entry_et': dt.isoformat() if dt else None,
            'tod': _tod_bucket(dt),
            'rvol': rv['band'] or UNKNOWN,
            'rvol_value': rv['rvol'],
            'rvol_n_sessions': rv['n_sessions'],
            'rvol_reason': rv['reason'],
            'atr_pct': ar['atr_pct_band'] or UNKNOWN,
            'atr_pct_value': ar['atr_pct'],
            'atr_trend': ar['atr_trend_band'] or UNKNOWN,
            'atr_trend_value': ar['atr_trend'],
            'atr_value': ar['atr'],
            'atr_reason': ar['reason'],
            'daily_source': prep['daily_source'] if prep else None,
        })
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── BUCKET STATISTICS ────────────────────────────────────────────────────────

def _as_leg(t):
    """A backtest trade projected into the journal-leg field names, so
    `heatmap._cell` aggregates it unchanged and the two surfaces cannot
    report a win rate by two different conventions."""
    entry, exit_ = _parse_et(t.get('entry_time')), _parse_et(t.get('exit_time'))
    hold = ((exit_ - entry).total_seconds() / 60.0
            if entry and exit_ else None)
    return {'pl': _f(t.get('pnl_dollars')) or 0.0,
            'pl_pct': _f(t.get('ret_pct')),
            'hold_minutes': round(hold, 1) if hold is not None else None}


def _book(trades):
    legs = [_as_leg(t) for t in trades]
    c = HM._cell(legs)
    pcts = [l['pl_pct'] for l in legs if l['pl_pct'] is not None]
    c['avg_pct'] = round(sum(pcts) / len(pcts), 4) if pcts else None
    return c


def _stats(inside, outside, book, *, min_n, sparse_below):
    """One bucket: heatmap cell keys, plus shrunk scores and a label.

    `n` is always present and always beside the result; there is no path
    through this function that returns a number without its sample size.
    """
    legs_in = [_as_leg(t) for t in inside]
    legs_out = [_as_leg(t) for t in outside]
    cell = HM._cell(legs_in)

    pl_in = [l['pl'] for l in legs_in]
    pl_out = [l['pl'] for l in legs_out]
    pct_in = [l['pl_pct'] for l in legs_in if l['pl_pct'] is not None]
    pct_out = [l['pl_pct'] for l in legs_out if l['pl_pct'] is not None]
    n = len(legs_in)

    b_pl = book['avg_pl'] or 0.0
    b_pct = book['avg_pct'] or 0.0
    b_wr = book['win_rate'] or 0.0
    w, lo = cell['wins'], cell['losses']

    # The headline test is on return percent. See the module docstring for
    # why that is the deviation from conditions.py and when it matters.
    t_pct = CD._welch_t(pct_in, pct_out) if pct_in and pct_out else None
    t_pl = CD._welch_t(pl_in, pl_out) if pl_in and pl_out else None

    if n < THIN or t_pct is None:
        conf = 'thin'
    elif abs(t_pct) >= 2:
        conf = 'strong'
    elif abs(t_pct) >= 1:
        conf = 'some'
    else:
        conf = 'noise'

    cell.update({
        'n': n,
        'avg_pct': round(sum(pct_in) / len(pct_in), 4) if pct_in else None,
        'rest_avg_pl': (round(sum(pl_out) / len(pl_out), 2)
                        if pl_out else None),
        'rest_avg_pct': (round(sum(pct_out) / len(pct_out), 4)
                         if pct_out else None),
        'score_pl': (round((sum(pl_in) + PRIOR * b_pl) / (n + PRIOR) - b_pl, 2)
                     if n else None),
        'score_pct': (round((sum(pct_in) + PRIOR * b_pct)
                            / (len(pct_in) + PRIOR) - b_pct, 4)
                      if pct_in else None),
        'score_wr': (round((w + PRIOR * b_wr) / (w + lo + PRIOR) - b_wr, 4)
                     if w + lo else None),
        't': t_pct,
        't_pl': t_pl,
        'confidence': conf,
        'interpretable': n >= min_n,
        'sparse': 0 < n < sparse_below,
    })
    return cell


def _multiplicity(singles, grids, min_n):
    """How many tests were run, and how many 'strong' labels pure noise owes.

    THE FAILURE THE SHRINKAGE DOES NOT CATCH. `conditions.py`'s machinery
    protects a bucket against its own small sample. It does nothing about
    the fact that this module runs the test dozens of times: four dimensions
    of bands plus three grids is on the order of seventy comparisons, and at
    |t| >= 2 a two-sided normal hands out a 'strong' label about 4.6% of the
    time to data with no structure in it whatsoever.

    Verified, not asserted: on synthetic trades with returns drawn i.i.d.
    from one distribution — no context effect present by construction — this
    module produced two 'strong' buckets at |t| ~ 2.0 on the first run.
    Both were noise. That is the number this function exists to put next to
    the findings list.

    `expected_strong_by_chance` is a LOWER bound. It uses the normal tail;
    the t-distribution at n=10 has fatter tails than that, and the buckets
    are not independent (a trade sits in one band of every dimension and in
    every grid), so the true rate is higher and is not worth pretending to
    compute exactly. If `n_strong` is not comfortably above this number, the
    honest reading of the findings list is "nothing here yet".
    """
    tested = []
    for s in singles.values():
        tested += [c for c in s['buckets'].values() if c['interpretable']]
    for g in grids.values():
        tested += [c for c in g['cells'].values() if c['interpretable']]
    n_tests = len(tested)
    n_strong = sum(1 for c in tested if c['confidence'] == 'strong')
    n_some = sum(1 for c in tested if c['confidence'] == 'some')
    return {
        'tests_run': n_tests,
        'min_n': min_n,
        'n_strong': n_strong,
        'n_some': n_some,
        'expected_strong_by_chance': round(0.0455 * n_tests, 1),
        'expected_some_by_chance': round(0.3173 * n_tests, 1),
        'note': ('Lower bound, normal tail, and the tests are not '
                 'independent. Treat n_strong <= expected as no signal.'),
    }


def _split(trades, ctxs, dim):
    """{band: [trades]} for one dimension, 'unknown' included as a real key."""
    groups = {}
    for t, c in zip(trades, ctxs):
        groups.setdefault(c.get(dim) or UNKNOWN, []).append(t)
    return groups


def _values_for(dim, groups):
    """Declared band order first, then anything unexpected, then unknown —
    so a report reads in regime order rather than alphabetically, which puts
    'contracting' above 'expanding' and 'dead' above 'hot'."""
    declared = DIMENSIONS[dim]['values']
    extra = sorted(k for k in groups
                   if k not in declared and k != UNKNOWN)
    return [v for v in declared if v in groups] + extra + \
           ([UNKNOWN] if UNKNOWN in groups else [])


# ─── THE PUBLIC CALL ──────────────────────────────────────────────────────────

def bucket(trades, bars_by_symbol=None, daily_by_symbol=None, *,
           daily=None, min_n=MIN_N, sparse_below=3, pairs=None,
           rvol_sessions=RVOL_SESSIONS, rvol_min_sessions=RVOL_MIN_SESSIONS,
           contexts=None):
    """Split `trades` by market context and report each bucket with its n.

    trades           the list `run_symbol()`/`run_basket()` returned, or any
                     list of dicts with the same keys.
    bars_by_symbol   the same {symbol: [bars]} handed to `run_basket()`. The
                     backtest already holds every session in memory, so the
                     volume baseline needs no fetch and no VolumeProfile
                     cache — the one thing this path has that the live path
                     does not.
    daily_by_symbol  real daily bars, if you have them. Without them dailies
                     are synthesized from the RTH intraday bars; see
                     `_synth_daily`.
    min_n            below this a bucket is reported but marked
                     `interpretable: False` and cannot enter `findings`.
    contexts         a precomputed `annotate()` result, to skip the work.

    Returns {'book', 'singles', 'pairs', 'findings', 'first30', 'coverage',
    'params'}. Nothing is dropped: every trade lands in exactly one band of
    every dimension, 'unknown' included.
    """
    trades = list(trades or [])
    pairs = DEFAULT_PAIRS if pairs is None else pairs
    daily_by_symbol = daily_by_symbol if daily_by_symbol is not None else daily

    ctxs = contexts if contexts is not None else annotate(
        trades, bars_by_symbol, daily_by_symbol,
        rvol_sessions=rvol_sessions, rvol_min_sessions=rvol_min_sessions)

    book = _book(trades)
    params = {
        'min_n': min_n, 'prior': PRIOR, 'thin': THIN,
        'sparse_below': sparse_below,
        'rvol_sessions': rvol_sessions,
        'rvol_min_sessions': rvol_min_sessions,
        'rvol_bands': {'hot': RVOL_HOT, 'normal': RVOL_NORMAL,
                       'light': RVOL_LIGHT},
        'atr_period': ATR_PERIOD, 'atr_fast': ATR_FAST, 'atr_slow': ATR_SLOW,
        'atr_pct_bands': {'wide': ATR_PCT_WIDE, 'ok': ATR_PCT_OK,
                          'thin': ATR_PCT_THIN},
        'atr_trend_bands': {'expanding': ATR_TREND_EXPANDING,
                            'contracting': ATR_TREND_CONTRACTING},
        'headline_variable': 'ret_pct',
        'daily_source': (sorted({c['daily_source'] for c in ctxs
                                 if c.get('daily_source')}) or None),
    }

    if not trades:
        return {'book': book, 'singles': {}, 'pairs': {}, 'findings': [],
                'first30': {'verdict': 'no trades', 'n_first30': 0,
                            'n_rest': 0, 'interpretable': False},
                'multiplicity': _multiplicity({}, {}, min_n),
                'coverage': {}, 'contexts': ctxs, 'params': params,
                'descriptive_only': True}

    # ── single dimensions ──
    singles, coverage = {}, {}
    for dim in DIMENSIONS:
        groups = _split(trades, ctxs, dim)
        buckets = {}
        for v in _values_for(dim, groups):
            inside = groups[v]
            outside = [t for k, g in groups.items() if k != v for t in g]
            c = _stats(inside, outside, book, min_n=min_n,
                       sparse_below=sparse_below)
            c['value'] = v
            # An 'unknown' band is a data gap, not an environment. It is
            # reported so the count reconciles, never interpreted.
            if v == UNKNOWN:
                c['interpretable'] = False
            buckets[v] = c
        singles[dim] = {
            'dim': dim, 'label': DIMENSIONS[dim]['label'],
            'note': DIMENSIONS[dim]['note'],
            'values': _values_for(dim, groups), 'buckets': buckets,
        }
        known = sum(c['n'] for v, c in buckets.items() if v != UNKNOWN)
        coverage[dim] = {
            'classified': known, 'unknown': len(trades) - known,
            'pct_classified': round(known / len(trades) * 100, 1),
        }

    # ── pairs ──
    grids = {}
    for a, b in pairs:
        grids[f'{a}|{b}'] = _grid(trades, ctxs, a, b, book,
                                  min_n=min_n, sparse_below=sparse_below)

    # ── conclusions: structurally filtered, not filtered by hand ──
    findings = []
    for dim, s in singles.items():
        for v, c in s['buckets'].items():
            if c['interpretable'] and c['confidence'] in ('strong', 'some'):
                findings.append({
                    'dim': dim, 'label': s['label'], 'value': v, 'n': c['n'],
                    'avg_pct': c['avg_pct'], 'score_pct': c['score_pct'],
                    'avg_pl': c['avg_pl'], 'win_rate': c['win_rate'],
                    't': c['t'], 'confidence': c['confidence'],
                })
    findings.sort(key=lambda r: (-abs(r['t'] or 0), -r['n']))

    return {
        'book': book,
        'singles': singles,
        'pairs': grids,
        'findings': findings,
        'first30': first_thirty(trades, ctxs, book, min_n=min_n),
        'multiplicity': _multiplicity(singles, grids, min_n),
        'coverage': coverage,
        'contexts': ctxs,
        'params': params,
        # Carried in the payload, not only in the prose, so a UI or a
        # downstream report cannot present these as forecasts by omission.
        'descriptive_only': True,
    }


def _grid(trades, ctxs, dim_a, dim_b, book, *, min_n, sparse_below):
    """One pair of dimensions, in `heatmap.build()`'s grid shape."""
    ga, gb = _split(trades, ctxs, dim_a), _split(trades, ctxs, dim_b)
    rows, cols = _values_for(dim_a, ga), _values_for(dim_b, gb)

    keyed = [((x.get(dim_a) or UNKNOWN), (x.get(dim_b) or UNKNOWN), t)
             for t, x in zip(trades, ctxs)]
    cells, placed = {}, 0
    for r in rows:
        for c in cols:
            inside = [t for ra, cb, t in keyed if ra == r and cb == c]
            outside = [t for ra, cb, t in keyed if not (ra == r and cb == c)]
            cell = _stats(inside, outside, book, min_n=min_n,
                          sparse_below=sparse_below)
            cell['row'], cell['col'] = r, c
            if UNKNOWN in (r, c):
                cell['interpretable'] = False
            cells[f'{r}|{c}'] = cell
            placed += cell['n']

    def _margin(dim, groups, v):
        inside = groups.get(v, [])
        outside = [t for k, g in groups.items() if k != v for t in g]
        m = _stats(inside, outside, book, min_n=min_n,
                   sparse_below=sparse_below)
        if v == UNKNOWN:
            m['interpretable'] = False
        return m

    return {
        'row_dim': dim_a, 'col_dim': dim_b,
        'row_label': DIMENSIONS[dim_a]['label'],
        'col_label': DIMENSIONS[dim_b]['label'],
        'rows': rows, 'cols': cols, 'cells': cells,
        'row_margin': {r: _margin(dim_a, ga, r) for r in rows},
        'col_margin': {c: _margin(dim_b, gb, c) for c in cols},
        'summary': {
            'trades_in_grid': placed,
            'overall': book,
            'populated_cells': sum(1 for c in cells.values() if c['n'] > 0),
            'sparse_cells': sum(1 for c in cells.values() if c['sparse']),
            'interpretable_cells': sum(1 for c in cells.values()
                                       if c['interpretable']),
            'total_cells': len(cells),
            'sparse_below': sparse_below,
            'min_n': min_n,
        },
    }


def to_heatmap_grid(grid):
    """Rename a pair grid's two axis keys to the journal heatmap's.

    `heatmap.build()` returns `grades` (rows) and `exit_keys` (columns); a
    renderer written against it reads those names. The CELLS are already
    identical — they come out of `heatmap._cell` — so this two-key rename is
    the entire difference between a context grid and a grade grid as far as
    display is concerned.

    The semantics are NOT identical and the caller should not pretend they
    are: 'grades' here holds context bands like 'hot' and 'contracting', and
    the axes are not ordinal, so a diagonal means nothing.
    """
    out = dict(grid)
    out['grades'] = grid['rows']
    out['exit_keys'] = grid['cols']
    out['filters_applied'] = {}
    out['summary'] = dict(grid['summary'])
    out['summary']['legs_in_grid'] = grid['summary']['trades_in_grid']
    out['summary']['legs_excluded_ungraded'] = 0
    return out


# ─── THE OWNER'S CLAIM, TESTED ────────────────────────────────────────────────

def first_thirty(trades, contexts, book=None, *, min_n=MIN_N):
    """Test "most of the Ripster plays happen in the first 30 minutes".

    Two claims live inside that sentence and they need separate answers:

      FREQUENCY  do a disproportionate share of entries occur 09:30-10:00?
                 Share of RTH trades in the window against the share of RTH
                 clock it occupies (30 of 390 minutes = 7.7%).
      QUALITY    are those trades better? First-30 return percent against
                 every other trade, Welch t.

    A belief can be right on one and wrong on the other, and conflating them
    is how "that's where the plays are" survives a losing morning book.

    `verdict` is 'insufficient sample' whenever either side is below `min_n`,
    and that string is produced by the same branch that would have produced
    a real verdict — there is no way to read a conclusion out of this
    function that the sample does not support.
    """
    trades = list(trades or [])
    book = book if book is not None else _book(trades)
    rth = [(t, c) for t, c in zip(trades, contexts)
           if c.get('tod') in TOD_ORDER
           and c.get('tod') not in ('premarket', 'after-hours')]
    inside = [t for t, c in rth if c['tod'] == FIRST30]
    rest = [t for t, c in rth if c['tod'] != FIRST30]

    c_in = _stats(inside, rest, book, min_n=min_n, sparse_below=3)
    c_rest = _stats(rest, inside, book, min_n=min_n, sparse_below=3)

    n_rth = len(rth)
    share = len(inside) / n_rth if n_rth else None
    clock_share = 30.0 / 390.0

    if len(inside) < min_n or len(rest) < min_n:
        verdict = 'insufficient sample'
        freq = quality = None
        note = (f'{len(inside)} trades in the window and {len(rest)} outside '
                f'it; {min_n} on each side is the floor for an opinion. '
                f'The counts below are real and the comparison is not.')
    else:
        freq = ('over-represented' if share and share > clock_share * 1.5 else
                'under-represented' if share and share < clock_share * 0.5 else
                'proportional')
        t = c_in['t']
        if t is None or abs(t) < 1:
            quality = 'no difference the sample can see'
        elif t >= 1:
            quality = 'better than the rest of the day'
        else:
            quality = 'worse than the rest of the day'
        verdict = f'entries {freq}; outcomes {quality}'
        note = (f'{len(inside)}/{n_rth} RTH entries ({share:.1%}) in 7.7% of '
                f'the session clock. Welch t on return % = {t}.')

    return {
        'window': FIRST30,
        'n_first30': len(inside), 'n_rest': len(rest), 'n_rth': n_rth,
        'share_of_entries': round(share, 4) if share is not None else None,
        'share_of_clock': round(clock_share, 4),
        'first30': c_in, 'rest_of_day': c_rest,
        'frequency': freq, 'quality': quality,
        'verdict': verdict, 'note': note,
        'interpretable': len(inside) >= min_n and len(rest) >= min_n,
        'min_n': min_n,
    }


# ─── REPORT ───────────────────────────────────────────────────────────────────

def _pct(v):
    return '   --  ' if v is None else f'{v:+6.3f}'


def format_report(res, title=''):
    """Text report. Every line that carries a result carries its n."""
    L = []
    if title:
        L += [title, '=' * len(title)]
    b = res['book']
    bwr = '--' if b['win_rate'] is None else '{:.1%}'.format(b['win_rate'])
    L += [
        f"trades {b['n']}   win rate {bwr}   "
        f"total ${b['total_pl']}   avg ${b['avg_pl']}   "
        f"avg ret {_pct(b.get('avg_pct'))}%",
        f"DESCRIPTIVE ONLY - one sample, no hold-out. Buckets under n="
        f"{res['params']['min_n']} are shown but not interpreted.",
        '',
    ]

    for dim, s in res['singles'].items():
        cov = res['coverage'][dim]
        L.append(f"-- {s['label']}  ({cov['pct_classified']}% classified, "
                 f"{cov['unknown']} unknown)")
        L.append(f"   {s['note']}")
        L.append(f"   {'band':<14}{'n':>4} {'win':>7} {'avg$':>9} "
                 f"{'avg%':>8} {'shrunk%':>9} {'t':>6}  label")
        for v in s['values']:
            c = s['buckets'][v]
            wr = '   --  ' if c['win_rate'] is None else f"{c['win_rate']:.1%}"
            flag = '' if c['interpretable'] else '  (not interpreted)'
            L.append(f"   {v:<14}{c['n']:>4} {wr:>7} "
                     f"{'--' if c['avg_pl'] is None else c['avg_pl']:>9} "
                     f"{_pct(c['avg_pct']):>8} {_pct(c['score_pct']):>9} "
                     f"{'--' if c['t'] is None else c['t']:>6}  "
                     f"{c['confidence']}{flag}")
        L.append('')

    f30 = res['first30']
    L += ['-- "most of the plays are in the first 30 minutes"',
          f"   verdict: {f30['verdict']}",
          f"   {f30['note']}", '']

    for key, g in res['pairs'].items():
        s = g['summary']
        L.append(f"-- {g['row_label']} x {g['col_label']}  "
                 f"({s['interpretable_cells']}/{s['total_cells']} cells reach "
                 f"n={s['min_n']})")
        head = ' ' * 14 + ''.join(f'{c:>16}' for c in g['cols'])
        L.append(head)
        for r in g['rows']:
            row = f'   {r:<11}'
            for c in g['cols']:
                cell = g['cells'][f'{r}|{c}']
                mark = '' if cell['interpretable'] else '*'
                row += f"{_pct(cell['avg_pct']).strip()}%/n={cell['n']}{mark}".rjust(16)
            L.append(row)
        L.append('   * n below the floor: shown for the count, not for reading.')
        L.append('')

    if res['findings']:
        L.append('-- buckets the sample can speak to (n >= '
                 f"{res['params']['min_n']}, |t| >= 1)")
        for f in res['findings']:
            L.append(f"   {f['label']} = {f['value']:<14} n={f['n']:<4} "
                     f"avg {_pct(f['avg_pct'])}%  shrunk {_pct(f['score_pct'])}%"
                     f"  t={f['t']}  {f['confidence']}")
        L.append('   Descriptive. These describe one sample; they are '
                 'hypotheses for another date range, not edges.')
    else:
        L.append(f"-- no bucket reaches n={res['params']['min_n']} with a "
                 "distinguishable result. That is the honest output of this "
                 "sample size, not a failure of the split.")

    m = res.get('multiplicity') or {}
    if m:
        L += ['',
              f"-- how many times the test was run: {m['tests_run']} buckets "
              f"reached n={m['min_n']}.",
              f"   'strong' found {m['n_strong']}, pure noise owes about "
              f"{m['expected_strong_by_chance']}.",
              f"   'some' found {m['n_some']}, pure noise owes about "
              f"{m['expected_some_by_chance']}.",
              f"   {m['note']}"]
    return '\n'.join(L)


if __name__ == '__main__':
    import argparse
    import csv
    import json

    ap = argparse.ArgumentParser(
        description='Bucket backtest trades by market context')
    ap.add_argument('trades_csv', help='a backtest_run.py trades CSV')
    ap.add_argument('--bars', default=None,
                    help='JSON {symbol: [bars]} for the same run; without it '
                         'only the time-of-day dimension can be computed')
    ap.add_argument('--daily', default=None, help='JSON {symbol: [daily bars]}')
    ap.add_argument('--min-n', type=int, default=MIN_N)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    with open(a.trades_csv, newline='', encoding='utf-8') as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ('pnl_dollars', 'ret_pct', 'entry_price', 'exit_price'):
            r[k] = _f(r.get(k))

    bars = json.load(open(a.bars, encoding='utf-8')) if a.bars else None
    dly = json.load(open(a.daily, encoding='utf-8')) if a.daily else None
    res = bucket(rows, bars, dly, min_n=a.min_n)
    if a.json:
        res.pop('contexts', None)
        print(json.dumps(res, indent=2, default=str))
    else:
        print(format_report(res, f'context breakdown - {a.trades_csv}'))
