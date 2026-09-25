"""
halflife.py - Foundations Trading

Mean-reversion half-life estimator. MEASUREMENT ONLY.

Like the screener, this module judges nothing and selects nothing. It answers
one question per ticker: does the deviation of price from a slow reference
mean-revert, and if so, how fast — and is that speed STABLE, or noise?

Method (the poor-man's residual):
  1. x = log(close) - EMA(log(close), ref_span).
     The slow EMA is the drifting fair-value reference; x is the leftover.
     One FIXED ref_span across all fit windows, so window-to-window
     differences measure estimation stability, not a moving target.
  2. Fit x[t] = a + b * x[t-1] by OLS over the last W bars, for each W in
     `windows`.
  3. Kendall small-sample correction on b (OLS biases b down by ~(1+3b)/n,
     which at b near 1 makes every half-life read far too fast), then
     theta = -ln(b); half_life = ln(2) / theta, in bars.
     Guards: b <= 0 or b >= 1 means no valid mean reversion in that window
     (unit root or oscillation) -> None for that window.
     Honest limits, verified on synthetic OU series: half-lives under ~1/10
     of the window recover tightly; half-lives past ~1/6 of the window get
     noisy. A RANDOM WALK typically reads invalid, or unstable with a
     half-life that GROWS with the window - that signature is the point.

THREE CHECKS. They catch DIFFERENT failures; none subsumes another.

  A. Window spread. Half-lives from different fit windows agree within
     STABLE_RATIO. Necessary, not sufficient; a random walk passes it
     routinely. Cheapest and weakest.

  B. Span drift. Refit everything against a 3x-slower reference. A true OU
     half-life is invariant to the reference; a random walk minus its own
     EMA shows mechanical pseudo-reversion whose apparent half-life SCALES
     with the span. Drift past SPAN_DRIFT_MAX forces 'unstable'.
     THIS is the check that rejects random walks. Verified: 0/12 synthetic
     random walks pass it, 11/12 true OU processes do.

  C. Cross-timeframe consistency. A real spring is a property of the STOCK,
     not of the bar size, so its half-life in SESSIONS should read the same
     on 10-min bars and on 1-hour bars.

     What C does NOT do: catch random walks. Every timeframe samples the
     SAME price path, so their estimation errors are correlated, and a
     random walk will happily report a consistent pseudo-half-life across
     timeframes. B is what stops that.

     What C DOES catch: sampling artifacts. Bid-ask bounce puts negative
     autocorrelation into 1-min closes that has nothing to do with the
     company, making fast timeframes read artificially FAST. Verified on a
     synthetic bounce: 1Min reads 0.66 sessions where 1Hour reads 1.05.
     So 1Min and 2Min are a CONTROL GROUP, not a signal. The trustworthy
     answer is the PLATEAU: the band of timeframes where the session
     half-life stops changing.

ON THE LEVEL vs THE SHAPE. The half-life LEVEL is a noisy statistic. Fitting
an AR(1) coefficient that sits at 0.998 means recovering (1 - b) from data
where b is estimated to a few parts in a thousand; the resulting half-life
carries a standard error that is routinely 30-50% of its own value, even on
clean synthetic data with a known answer. Every row therefore reports
`hl_se` and `rel_se`. Read the half-life as an order of magnitude, not a
number. The stability verdicts and the cross-timeframe SHAPE are far better
determined than the level, which is exactly why this tool reports verdicts
and not just a number to plug into a cloud length.

Pure logic. Bars in, dict out. No broker calls, no I/O, no threads.

HARDCODED CONSTANTS BY DESIGN — edit + commit to change.
"""
import re
import math

import numpy as np
import pandas as pd

# --- ESTIMATOR CONSTANTS (the spec - edit + commit to change) -----------------

BASE_WINDOWS = (300, 600, 1200)     # OLS fit windows, calibrated for 10Min
REF_SPAN_FRAC = 0.5                 # ref_span = max(windows) * this
STABLE_RATIO = 2.0                  # check A: max/min half-life across windows
SPAN_DRIFT_MAX = 1.35               # check B: median HL at 3x ref_span vs 1x
CONSISTENT_RATIO = 2.0              # check C: max/min HL_sessions across TFs
MIN_HALF_LIFE_BARS = 1.0            # below this, faster than one bar: junk
MAX_HALF_LIFE_FRAC = 0.5            # HL > window/2 means the window never saw
                                    # a full reversion: unmeasured, not slow
MIN_WINDOW = 100                    # no fit window smaller than this
WINDOW_HEADROOM = 2.2               # bars needed per largest window
WINDOW_LADDER = (1, 2, 4)           # windows are T, 2T, 4T for a target T

# Base fit target T, in bars, for timeframes where a bar is not clock time.
# Intraday targets scale off 10Min automatically (see auto_windows).
#   1Day 150 -> largest window 600 bars, ~2.4 years of history needed.
#   1Week 60 -> largest 240 bars; 1Month 30 -> largest 120. Slower than that
#   and no realistic history supports three windows; the filter says so.
BASE_TARGET = {'1Day': 150, '1Week': 60, '1Month': 30}

# Clock minutes per bar. Used only for window auto-scaling.
_TF_RE = re.compile(r'^(\d+)(Min|Hour|Day|Week|Month)$', re.I)
_TF_UNIT_MINUTES = {'min': 1, 'hour': 60, 'day': 1440,
                    'week': 10080, 'month': 43200}


def _parse_tf(tf):
    """'45Min' -> 45. None when the string is not a timeframe."""
    if not tf:
        return None
    m = _TF_RE.match(str(tf).strip())
    if not m:
        return None
    return int(m.group(1)) * _TF_UNIT_MINUTES[m.group(2).lower()]


class _TFMinutes(dict):
    """The timeframe menu, but self-extending.

    Eight call sites across the app index this with brackets. Enumerating
    the menu meant every one of them was a KeyError waiting for the next
    timeframe somebody added -- which is exactly what adding '6Min' caused.

    __missing__ parses a well-formed timeframe on demand, so TF_MINUTES['3Min']
    returns 3 without '3Min' ever being listed. Membership is deliberately
    NOT extended: `'3Min' in TF_MINUTES` stays False and `list(TF_MINUTES)`
    still yields the menu, so the UI dropdown and the input validation in
    app.py keep showing the curated set rather than every string that parses.
    """

    def __missing__(self, tf):
        m = _parse_tf(tf)
        if m is None:
            raise KeyError(tf)
        return m


TF_MINUTES = _TFMinutes({
    '1Min': 1, '2Min': 2, '5Min': 5, '6Min': 6, '10Min': 10, '15Min': 15, '30Min': 30,
    '1Hour': 60, '2Hour': 120, '4Hour': 240,
    '1Day': 1440, '1Week': 10080, '1Month': 43200,
})

# Sessions each bar spans, where a bar is NOT clock time. Intraday
# timeframes measure sessions-per-bar empirically from the data instead,
# which handles extended-hours feeds without a hardcoded 39.
SESSIONS_PER_BAR = {'1Day': 1.0, '1Week': 5.0, '1Month': 21.0}

TIMEFRAMES = tuple(TF_MINUTES)      # the menu, in speed order

def tf_minutes(tf, default=None):
    """Minutes per bar for a timeframe string, PARSED rather than looked up.

    TF_MINUTES is a menu, not a contract. Every module that keyed a private
    dict off it broke the day a new timeframe was added -- adding '6Min' in
    one place raised KeyError in the four others that had their own copy.
    Parsing the string means any well-formed timeframe works everywhere, and
    an unknown one returns `default` instead of exploding mid-scan.
    """
    m = _parse_tf(tf)
    return default if m is None else m


# --- WINDOW SCALING ------------------------------------------------------------

def auto_windows(timeframe, n_bars=None):
    """Fit windows for a timeframe, scaled off BASE_WINDOWS (tuned for 10Min).

    A stock's half-life is a fixed amount of real time, so its half-life IN
    BARS scales inversely with bar size: a 2-session spring is ~130 ten-min
    bars but ~1,300 one-min bars. A fixed 300-bar window would be blind to
    the latter. So intraday windows scale by 10 / tf_minutes.

    Daily and slower keep BASE_WINDOWS: a bar is a session, not clock time,
    and the same scaling would produce four-bar windows.

    If n_bars is given, windows needing more than n_bars / WINDOW_HEADROOM
    are dropped (the reference warmup and the 3x span-drift refit both eat
    history before the fit sees a bar). Fewer than 2 surviving windows means
    this timeframe cannot be estimated on this data.
    """
    if timeframe not in TF_MINUTES:
        raise KeyError(f'unknown timeframe {timeframe!r}; '
                       f'have: {list(TF_MINUTES)}')
    if timeframe in BASE_TARGET:
        target = BASE_TARGET[timeframe]
    else:
        # Scale the 10Min target inversely with bar size. Clamp the TARGET,
        # not each window: clamping windows individually collapses the
        # ladder at slow intraday timeframes (2Hour would give 100/100/100).
        target = max(MIN_WINDOW,
                     int(round(BASE_WINDOWS[0] * 10.0 / TF_MINUTES[timeframe])))
    wins = [target * k for k in WINDOW_LADDER]
    if n_bars is not None:
        cap = n_bars / WINDOW_HEADROOM
        wins = [w for w in wins if w <= cap]
    return wins


def bars_needed(timeframe):
    """Minimum bars to estimate a timeframe at all. The fetch layer sizes its
    request from this so a 1Min run doesn't drag six years of ticks."""
    wins = auto_windows(timeframe)
    return int(max(wins) * WINDOW_HEADROOM) + 10


def sessions_per_bar(timeframe, bars):
    """Trading sessions one bar spans. Measured from the data for intraday
    (extended hours handled for free); from the constant map otherwise."""
    if timeframe in SESSIONS_PER_BAR:
        return SESSIONS_PER_BAR[timeframe]
    bpd = bars_per_day(bars)
    return (1.0 / bpd) if bpd else None


# --- HELPERS -------------------------------------------------------------------

def _closes(bars):
    """List of bar dicts (alpaca_manager format) -> np.array of log closes.
    Returns None on empty/thin input."""
    if not bars or len(bars) < 3:
        return None
    c = np.array([float(b['close']) for b in bars], dtype=float)
    if np.any(c <= 0):
        return None
    return np.log(c)


def bars_per_day(bars):
    """Median bars per distinct session date, measured from the data itself.
    Robust to extended-hours feeds and half days; no hardcoded 39."""
    if not bars:
        return None
    dates = pd.to_datetime([b['time'] for b in bars], utc=True).date
    counts = pd.Series(dates).value_counts()
    if len(counts) > 4:
        counts = counts.iloc[1:-1]     # drop partial first/last sessions
    return float(counts.median()) if len(counts) else None


def _fit_window(x, window):
    """OLS of x[t] on x[t-1] over the last `window` points of x.

    Returns (half_life_bars, standard_error) or (None, None) if the window
    shows no valid mean reversion. Fails closed on thin data.

    The standard error is not decoration. b sits near 1, the half-life
    depends on 1/ln(b), and the derivative d(HL)/db = HL^2 / (ln2 * b)
    blows up as b -> 1. So a tiny error in b becomes a large error in HL,
    and the caller needs to see it."""
    if len(x) < window + 1:
        return None, None
    xs = x[-(window + 1):]
    lag, cur = xs[:-1], xs[1:]
    lag_c = lag - lag.mean()
    denom = float(np.dot(lag_c, lag_c))
    if denom <= 0:
        return None, None
    b = float(np.dot(lag_c, cur - cur.mean()) / denom)
    # Kendall small-sample correction: OLS biases b DOWNWARD by roughly
    # (1+3b)/n, which at b near 1 dwarfs (1-b) itself and makes every
    # half-life read far too fast. Verified against synthetic OU series:
    # uncorrected, a true 100-bar HL measures ~35 at n=300.
    b = b + (1.0 + 3.0 * b) / window
    if b <= 0.0 or b >= 1.0:
        return None, None                # unit root / oscillation: no HL
    hl = math.log(2.0) / -math.log(b)
    if hl < MIN_HALF_LIFE_BARS or hl > window * MAX_HALF_LIFE_FRAC:
        return None, None                # sub-bar junk or unmeasured-slow
    se_b = math.sqrt(max(0.0, 1.0 - b * b) / window)
    se_hl = hl * hl / (math.log(2.0) * b) * se_b
    return hl, se_hl


def _fit_all(logc, windows, ref_span):
    """Deviation series vs an EMA(ref_span) reference, then one OLS fit per
    window. Returns ({window: hl|None}, sigma_dev), or (None, None) if the
    series is too thin after the reference warmup."""
    ref = pd.Series(logc).ewm(span=ref_span, adjust=False).mean().to_numpy()
    x = logc - ref
    # Drop the reference's own warmup: deviations there hug zero by
    # construction and bias b upward.
    burn = min(ref_span, len(x) // 4)
    x = x[burn:]
    if len(x) < min(windows) + 1:
        return None, None
    sigma = float(np.std(x[-max(windows):], ddof=1)) if len(x) >= 30 else None
    return {w: _fit_window(x, w) for w in windows}, sigma   # w -> (hl, se)


# --- SINGLE TIMEFRAME ------------------------------------------------------------

def estimate(bars, windows=BASE_WINDOWS, ref_span=None, bars_per_day_=True):
    """One ticker, one timeframe. Runs checks A and B. Check C needs more
    than one timeframe and lives in estimate_timeframes().

    bars_per_day_=False skips the session-count measurement. That single line
    parses every timestamp in the window through pandas and costs ~75% of this
    function; it is a REPORTING field only. Callers on a hot path (an engine
    gate running once per bar) must turn it off. Callers building a table for
    a human leave it on."""
    windows = sorted(int(w) for w in windows)
    if ref_span is None:
        ref_span = max(1, int(max(windows) * REF_SPAN_FRAC))

    out = {'half_lives': {w: None for w in windows},
           'median_hl_bars': None, 'hl_se': None, 'rel_se': None,
           'sigma_dev': None,
           'stability': 'invalid', 'spread_ratio': None, 'span_drift': None,
           'n_bars': len(bars) if bars else 0,
           'bars_per_day': bars_per_day(bars) if bars_per_day_ else None}

    logc = _closes(bars)
    if logc is None:
        return out

    hls, sigma = _fit_all(logc, windows, ref_span)
    if hls is None:
        return out
    out['sigma_dev'] = sigma

    valid, ses = [], []
    for w in windows:
        hl, se = hls[w]
        out['half_lives'][w] = round(hl, 1) if hl is not None else None
        if hl is not None:
            valid.append(hl)
            ses.append(se)
    if not valid:
        return out

    med = float(np.median(valid))
    out['median_hl_bars'] = round(med, 1)
    # Combine the per-window standard errors. The windows are nested (the
    # 300-bar fit is a subset of the 1200-bar fit), so they are NOT
    # independent and the errors do not shrink like 1/sqrt(k). Worse, the
    # analytic SE only prices SAMPLING error; it is blind to the bias the
    # EMA reference injects and to window sensitivity. Measured coverage
    # using min(ses) alone was ~50%, not 95%. So take the WIDER of the
    # analytic error and the observed spread across windows, which prices
    # both. Never claim a tighter error than the windows themselves show.
    if ses:
        analytic = float(np.median(ses))
        dispersion = (max(valid) - min(valid)) / 2.0 if len(valid) > 1 else 0.0
        se = max(analytic, dispersion)
        out['hl_se'] = round(se, 1)
        out['rel_se'] = round(se / med, 2) if med else None

    # check A: do the windows agree?
    if len(valid) >= 2:
        ratio = max(valid) / min(valid)
        out['spread_ratio'] = round(ratio, 2)
        out['stability'] = 'stable' if ratio <= STABLE_RATIO else 'unstable'
    else:
        out['stability'] = 'unstable'    # one window is a point, not a trend

    # check B: does the half-life survive a 3x slower reference?
    hls3, _ = _fit_all(logc, windows, ref_span * 3)
    if hls3 is not None:
        valid3 = [h for h, _se in hls3.values() if h is not None]
        if valid3:
            med3 = float(np.median(valid3))
            drift = max(med, med3) / min(med, med3)
            out['span_drift'] = round(drift, 2)
            if drift > SPAN_DRIFT_MAX and out['stability'] == 'stable':
                out['stability'] = 'unstable'
    return out


# --- MULTI-TIMEFRAME: ONE TICKER --------------------------------------------------

def _blank_row(tf, wins, stability, n_bars):
    return {'timeframe': tf, 'windows': list(wins), 'stability': stability,
            'median_hl_bars': None, 'median_hl_sessions': None,
            'hl_se': None, 'rel_se': None,
            'spread_ratio': None, 'span_drift': None, 'sigma_dev': None,
            'n_bars': n_bars, 'half_lives': {}}


def estimate_timeframes(bars_by_tf, windows_by_tf=None):
    """One ticker across several timeframes, plus check C.

    bars_by_tf     {timeframe: [bar dicts]}; a timeframe with no bars is
                   reported as 'no_data', never dropped silently.
    windows_by_tf  optional {timeframe: [windows]}; default auto_windows()
                   sized to the timeframe and its available history.

    check C runs only over timeframes that already passed A and B. One
    stable timeframe cannot corroborate itself, so it reports
    'insufficient': suggestive, not evidence.
    """
    rows = []
    for tf in TIMEFRAMES:
        if tf not in bars_by_tf:
            continue
        bars = bars_by_tf[tf] or []
        wins = (windows_by_tf or {}).get(tf) or auto_windows(tf, len(bars))

        if not bars:
            rows.append(_blank_row(tf, wins, 'no_data', 0))
            continue
        if len(wins) < 2:
            rows.append(_blank_row(tf, auto_windows(tf),
                                   'insufficient_history', len(bars)))
            continue

        try:
            r = estimate(bars, windows=wins)
        except Exception:
            rows.append(_blank_row(tf, wins, 'invalid', len(bars)))
            continue

        spb = sessions_per_bar(tf, bars)
        hl_sess = (round(r['median_hl_bars'] * spb, 3)
                   if (r['median_hl_bars'] and spb) else None)
        rows.append({
            'timeframe': tf, 'windows': list(wins),
            'stability': r['stability'],
            'median_hl_bars': r['median_hl_bars'],
            'median_hl_sessions': hl_sess,
            'hl_se': r['hl_se'], 'rel_se': r['rel_se'],
            'spread_ratio': r['spread_ratio'],
            'span_drift': r['span_drift'],
            'sigma_dev': (round(r['sigma_dev'], 5)
                          if r['sigma_dev'] is not None else None),
            'n_bars': r['n_bars'],
            'half_lives': {str(w): r['half_lives'][w] for w in wins},
        })

    # check C: do the stable timeframes agree on a session half-life?
    sess = [r['median_hl_sessions'] for r in rows
            if r['stability'] == 'stable' and r['median_hl_sessions']]
    out = {'rows': rows, 'consistency': 'insufficient',
           'consistency_ratio': None, 'median_hl_sessions': None,
           'n_stable_tf': len(sess)}
    if sess:
        out['median_hl_sessions'] = round(float(np.median(sess)), 3)
    if len(sess) >= 2:
        ratio = max(sess) / min(sess)
        out['consistency_ratio'] = round(ratio, 2)
        out['consistency'] = ('consistent' if ratio <= CONSISTENT_RATIO
                              else 'inconsistent')
    return out


# --- MULTI-TIMEFRAME: BASKET -------------------------------------------------------

def estimate_basket_multi(bars_by_symbol_tf, progress=None):
    """estimate_timeframes() across {symbol: {timeframe: bars}}.
    Never raises per-name: a bad series lands as an 'insufficient' entry,
    not a dead sweep."""
    out, total = [], len(bars_by_symbol_tf)
    for i, (sym, by_tf) in enumerate(sorted(bars_by_symbol_tf.items()), 1):
        try:
            r = estimate_timeframes(by_tf)
        except Exception:
            r = {'rows': [], 'consistency': 'insufficient',
                 'consistency_ratio': None, 'median_hl_sessions': None,
                 'n_stable_tf': 0}
        r['ticker'] = sym
        out.append(r)
        if progress:
            progress(i, total)
    return out


def flatten_rows(results):
    """Long-format table for CSV: one row per (ticker, timeframe).
    Per-window half-lives collapse into one 'window_fits' cell (e.g.
    '300:121.5 600:126.4') because the window set varies by timeframe."""
    flat = []
    for res in results:
        for r in res['rows']:
            fits = ' '.join(f'{w}:{v}' for w, v in
                            sorted(r.get('half_lives', {}).items(),
                                   key=lambda kv: int(kv[0]))
                            if v is not None)
            flat.append({
                'ticker': res['ticker'],
                'timeframe': r['timeframe'],
                'stability': r['stability'],
                'median_hl_bars': r['median_hl_bars'],
                'median_hl_sessions': r['median_hl_sessions'],
                'hl_se': r['hl_se'],
                'rel_se': r['rel_se'],
                'spread_ratio': r['spread_ratio'],
                'span_drift': r['span_drift'],
                'sigma_dev': r['sigma_dev'],
                'n_bars': r['n_bars'],
                'window_fits': fits,
                'ticker_consistency': res['consistency'],
                'ticker_consistency_ratio': res['consistency_ratio'],
                'ticker_median_hl_sessions': res['median_hl_sessions'],
            })
    return flat


# --- VARIANCE RATIO: THE TREND / REVERSION DISCRIMINATOR ---------------------------
#
# The clean statistic for "does this name trend or revert", and the one thing
# that lets a trending engine and a reverting engine answer the SAME question.
#
#   VR(k) = Var(k-period return) / (k * Var(1-period return))
#
# Under a random walk, variance grows linearly with horizon, so VR = 1.
# VR > 1: moves persist. Yesterday's move predicts today's. Trending.
# VR < 1: moves reverse. Yesterday's move predicts today's opposite. Reverting.
#
# The z-statistic is Lo-MacKinlay's heteroskedasticity-ROBUST form. The simpler
# homoskedastic version assumes constant volatility, which no equity has, and
# it rejects the random-walk null far too often on real data. Using it would
# hand you a universe full of "reverting" names that are just volatility
# clustering. That is not a detail; it is the difference between a real filter
# and an expensive random number generator.

VR_LAG = 5                          # k, in bars
VR_Z_SIGNIFICANT = 2.0              # |z| below this = indistinguishable from RW


def variance_ratio(logp, k=VR_LAG):
    """Lo-MacKinlay variance ratio with the robust z-statistic.

    logp   np.array of log prices, oldest first
    k      aggregation lag in bars

    Returns (vr, z) or (None, None) on thin/degenerate data.
    """
    logp = np.asarray(logp, dtype=float)
    n = len(logp) - 1                       # number of 1-period returns
    if n < 4 * k or k < 2:
        return None, None

    r = np.diff(logp)
    mu = r.mean()
    d = r - mu
    var1 = float(np.dot(d, d)) / (n - 1)
    if var1 <= 0:
        return None, None

    # k-period overlapping returns, with the standard (n-k+1) correction
    rk = logp[k:] - logp[:-k]
    m = n - k + 1
    if m < 2:
        return None, None
    dk = rk - k * mu
    vark = float(np.dot(dk, dk)) / (m * (1.0 - k / n))
    vr = vark / (k * var1)

    # Robust variance of VR: weighted sum of autocovariance-of-squares terms.
    #   delta_j = sum_t d_t^2 d_{t-j}^2 / (sum_t d_t^2)^2
    # No extra n multiplier: it is already implicit in the squared denominator.
    # Under iid Gaussian this collapses to delta_j = 1/n, and theta to the
    # textbook homoskedastic variance 2(2k-1)(k-1)/(3kn) -- which is the
    # sanity check the tests assert.
    theta = 0.0
    den = float(np.dot(d, d)) ** 2
    if den <= 0:
        return vr, None
    for j in range(1, k):
        num = float(np.dot(d[j:] ** 2, d[:-j] ** 2))
        delta = num / den
        theta += (2.0 * (k - j) / k) ** 2 * delta
    if theta <= 0:
        return vr, None
    z = (vr - 1.0) / math.sqrt(theta)
    return float(vr), float(z)


def classify(bars, k=VR_LAG):
    """Two buckets and a trash can. The ONLY selection primitive in this file.

    Returns {'verdict', 'vr', 'z', 'reason'}. verdict is:
      'trending'    VR significantly > 1
      'reverting'   VR significantly < 1
      'random_walk' VR indistinguishable from 1  -> trade neither engine
      'unknown'     not enough data to say

    Abstention is a first-class answer. A name that cannot be classified is
    not a name to guess at.
    """
    logc = _closes(bars)
    if logc is None or len(logc) < 4 * k + 2:
        return {'verdict': 'unknown', 'vr': None, 'z': None,
                'reason': f'need > {4 * k + 2} bars, have '
                          f'{0 if logc is None else len(logc)}'}
    vr, z = variance_ratio(logc, k)
    if vr is None or z is None:
        return {'verdict': 'unknown', 'vr': vr, 'z': z,
                'reason': 'degenerate series'}
    if abs(z) < VR_Z_SIGNIFICANT:
        v, why = 'random_walk', f'VR={vr:.3f} but |z|={abs(z):.2f} < ' \
                                f'{VR_Z_SIGNIFICANT}: not distinguishable'
    elif vr > 1.0:
        v, why = 'trending', f'VR={vr:.3f} > 1 (z={z:.2f}): moves persist'
    else:
        v, why = 'reverting', f'VR={vr:.3f} < 1 (z={z:.2f}): moves reverse'
    return {'verdict': v, 'vr': round(vr, 4), 'z': round(z, 2), 'reason': why}


# --- THE THETA LINE: pooled cross-timeframe fit -----------------------------------
#
# Sampling an OU process at ANY interval yields exactly an AR(1) with
#     b(delta) = exp(-theta * delta)
# so -ln(b) is LINEAR in the bar size, through the origin, with slope theta.
# OU is closed under subsampling: coarsen the bars however you like and the
# same theta generates every one of them. That is the real invariant. It is
# not harmonic -- OU is overdamped, its autocorrelation decays monotonically
# with zero sign changes, so it has no frequency and no overtones. Half-lives
# at different timeframes need not be integer multiples of anything. They need
# to be generated by one theta.
#
# So instead of comparing HL_sessions pairwise (crude, throws away data), fit
# the line across every timeframe at once. You get:
#   - one pooled theta, using all the evidence
#   - an R^2 that measures how OU-like the name actually is
#   - a DRIFT that names the disease when it isn't
#
# Verified on 2,000,000 simulated minutes:
#   single OU (hl 60min): theta constant to 1% from 1-min to 120-min bars
#   two superposed OU (hl 8min + 600min): apparent HL climbs 283 -> 512 min
#       monotonically as bars coarsen. Coarse bars low-pass the fast spring
#       away and report only the slow one. A SUM of OU processes is not OU.
#   random walk: theta constant too (!), but ~0. Invariance is NECESSARY,
#       NOT SUFFICIENT. It certifies one theta governs; it says nothing about
#       theta being nonzero. That is what MAX/MIN half-life bounds are for.

CONTROL_TFS = ('1Min', '2Min')      # bid-ask bounce fakes fast reversion here
THETA_R2_MIN = 0.97                 # below this, the line is not a line
# d ln(theta) / d ln(delta). Zero for a true OU. Calibrated on synthetic data
# across 4 half-lives x 3 seeds (null) and 4 spring separations (alternative):
#   null (single OU):        drift in [-0.027, +0.060]
#   alt  (two superposed):   drift in [-0.223, -0.104]
# The gap is clean and the null skews slightly POSITIVE, so the test is
# one-sided: only a negative slope means multi-scale. A positive slope means
# something else is wrong and the name is simply 'inconsistent'.
THETA_DRIFT_MAX = 0.08
THETA_DRIFT_POS_MAX = 0.15
# Span drift, pooled. A true theta is invariant to the reference kernel; a
# random walk's PSEUDO-theta is proportional to 1/ref_span. This check is not
# optional and it is not redundant with the drift slope above:
#
#   The reference is iterated to 10 half-lives. For a random walk that
#   iteration has a SELF-CONSISTENT FIXED POINT -- the pseudo-half-life it
#   manufactures is proportional to the reference span, so the loop converges
#   on a fake spring and every timeframe agrees about it. Invariance across
#   BAR SIZE is necessary and not sufficient. Invariance across REFERENCE
#   SPAN is what rejects the random walk.
THETA_SPAN_RATIO = 3.0              # refit against a 3x slower reference
# theta(1x) / theta(3x). ~1 for a real OU. Measured null (6 true springs from
# 0.125 to 6.25 sessions): max 1.342. A random walk's pseudo-theta is
# proportional to 1/ref, which would give 3.0 -- but in practice it never gets
# that far, because its reference iteration DIVERGES (see below).
THETA_SPAN_DRIFT_MAX = 1.60
# A real spring's reference iteration converges: ref = 10*HL, refit, same HL.
# A random walk's does not. Its pseudo-HL is proportional to the reference, so
# each pass makes the reference longer, which makes the pseudo-HL longer. The
# runaway is the tell, and it is cheaper and sharper than any threshold.
THETA_CONVERGE_MAX = 1.50           # ratio of hl between the last two passes
THETA_REF_SESSIONS_MULT = 10.0      # reference kernel = 10 half-lives
# The fit window must be several times the SLOW reference (3x the normal one),
# or the span-drift refit has a kernel longer than its own data and a true slow
# spring reads 'no_spring'. 120 half-lives = 4x the slow reference kernel.
THETA_WINDOW_MULT = 120.0           # fit window, in half-lives
THETA_WINDOW_MIN_SESSIONS = 20.0
THETA_HL_SEED = 1.0                 # sessions
THETA_PASSES = 2
THETA_MIN_BARS_PER_FIT = 120


def _theta_one_tf(bars, spb, ref_sessions, window_sessions):
    """AR(1) on the deviation from a reference whose kernel is fixed in
    SESSIONS, over a window fixed in SESSIONS.

    This is the whole trick. If each timeframe uses a reference span measured
    in BARS, every timeframe filters a DIFFERENT process, and the theta they
    report cannot be compared -- a single true OU spring will look multi-scale
    purely because you smoothed it differently at each sampling rate. Fixing
    the reference and the window in real time makes each series an honest
    subsample of one continuous process, which is the only condition under
    which b(delta) = exp(-theta*delta) holds.

    Returns theta per SESSION, or None."""
    logc = _closes(bars)
    if logc is None:
        return None
    ref_bars = max(10, int(round(ref_sessions / spb)))
    win_bars = int(round(window_sessions / spb))
    if win_bars < THETA_MIN_BARS_PER_FIT:
        return None
    if len(logc) < ref_bars + win_bars + 2:
        return None

    ref = pd.Series(logc).ewm(span=ref_bars, adjust=False).mean().to_numpy()
    x = (logc - ref)[ref_bars:]
    if len(x) < win_bars + 1:
        return None
    xs = x[-(win_bars + 1):]
    lag, cur = xs[:-1], xs[1:]
    lc = lag - lag.mean()
    den = float(np.dot(lc, lc))
    if den <= 0:
        return None
    b = float(np.dot(lc, cur - cur.mean()) / den)
    b = b + (1.0 + 3.0 * b) / win_bars          # Kendall, as everywhere else
    if b <= 0.0 or b >= 1.0:
        return None
    return -math.log(b) / spb                    # theta per session


def theta_line(bars_by_tf, exclude_control=True):
    """Pooled OU fit across timeframes: regress -ln(b) on bar size.

    bars_by_tf  {timeframe: [bar dicts]}

    Returns {'theta', 'hl_sessions', 'r2', 'drift', 'n_points', 'verdict',
             'reason', 'points': [(tf, delta_sessions, theta_i)]}

    verdict:
      'ou'             one theta generates every timeframe. Assignable.
      'multi_scale'    apparent half-life GROWS with bar size: superposed
                       springs at different timescales. No single theta.
      'no_spring'      theta scales with the reference kernel, so the spring
                       is an artifact of the EMA. Random walks land here.
      'inconsistent'   the line is not a line for some other reason.
      'insufficient'   fewer than 2 usable timeframes.

    `drift` is the log-log slope d ln(theta) / d ln(delta). Zero means theta
    is invariant to bar size, which is what OU demands. Negative means theta
    shrinks as bars coarsen, i.e. the apparent half-life grows, i.e. coarse
    bars are low-passing a fast spring away and reporting a slow one. It is a
    slope over ALL points, not a ratio of the two endpoints, because one noisy
    endpoint should not decide a name's fate.

    Abstention is a first-class answer. A name whose reversion timescale is
    not a property of the STOCK has no natural clock to be assigned to.
    """
    tfs = [t for t in TIMEFRAMES if t in bars_by_tf and bars_by_tf[t]]
    if exclude_control:
        core = [t for t in tfs if t not in CONTROL_TFS]
        if len(core) >= 2:
            tfs = core

    out = {'theta': None, 'hl_sessions': None, 'r2': None, 'drift': None,
           'span_drift': None, 'n_points': 0, 'verdict': 'insufficient',
           'reason': '', 'points': []}
    if len(tfs) < 2:
        out['reason'] = f'{len(tfs)} usable timeframe(s); need 2'
        return out

    spbs = {t: sessions_per_bar(t, bars_by_tf[t]) for t in tfs}
    tfs = [t for t in tfs if spbs.get(t)]
    if len(tfs) < 2:
        out['reason'] = 'cannot measure bar size in sessions'
        return out

    # Reference kernel scales with the half-life we are trying to find, so
    # iterate from a seed. Two passes converge; span drift is checked
    # independently by estimate().
    hl_sess = THETA_HL_SEED
    hl_hist = []
    pts = []
    for _p in range(THETA_PASSES + 1):
        ref_sessions = THETA_REF_SESSIONS_MULT * hl_sess
        window_sessions = max(THETA_WINDOW_MIN_SESSIONS,
                              THETA_WINDOW_MULT * hl_sess)
        pts = []
        for t in tfs:
            th = _theta_one_tf(bars_by_tf[t], spbs[t], ref_sessions,
                               window_sessions)
            if th and th > 0:
                pts.append((t, spbs[t], th))
        if len(pts) < 2:
            # A diverging reference starves its own fit: the window demand
            # grows without bound. That is the random-walk signature, not a
            # data problem, so name it correctly.
            if len(hl_hist) >= 2 and hl_hist[-1] / hl_hist[-2] > THETA_CONVERGE_MAX:
                out['verdict'] = 'no_spring'
                out['reason'] = (
                    'the reference iteration diverges: each pass lengthens '
                    'the reference, which lengthens the apparent half-life. '
                    'A real spring converges; a random walk minus its own EMA '
                    'runs away like this.')
            else:
                out['reason'] = ('fewer than 2 timeframes produced a valid fit '
                                 f'(needs ~{window_sessions:.0f} sessions)')
            return out
        theta_med = float(np.median([p[2] for p in pts]))
        hl_sess = math.log(2.0) / theta_med
        hl_hist.append(hl_sess)
    window_sessions = max(THETA_WINDOW_MIN_SESSIONS,
                          THETA_WINDOW_MULT * hl_sess)

    if len(hl_hist) >= 2 and hl_hist[-1] / hl_hist[-2] > THETA_CONVERGE_MAX:
        out['verdict'] = 'no_spring'
        out['reason'] = (f'reference iteration diverging '
                         f'(HL {hl_hist[-2]:.3f} -> {hl_hist[-1]:.3f} sessions '
                         f'between passes): pseudo-spring, not a stock')
        return out

    pts.sort(key=lambda p: p[1])
    x = np.array([p[1] for p in pts])                 # sessions per bar
    y = np.array([p[2] * p[1] for p in pts])          # -ln(b) = theta * delta
    theta_i = np.array([p[2] for p in pts])

    theta = float(np.dot(x, y) / np.dot(x, x))        # regression through 0
    if theta <= 0:
        out['reason'] = 'non-positive pooled theta'
        return out

    # SPAN DRIFT, pooled. Refit everything against a slower reference kernel.
    ref_slow = THETA_REF_SESSIONS_MULT * hl_sess * THETA_SPAN_RATIO
    slow_pts = []
    for t in tfs:
        th = _theta_one_tf(bars_by_tf[t], spbs[t], ref_slow, window_sessions)
        if th and th > 0:
            slow_pts.append((spbs[t], th))
    span_drift = None
    if len(slow_pts) >= 2:
        xs = np.array([p[0] for p in slow_pts])
        ys = np.array([p[1] * p[0] for p in slow_pts])
        theta_slow = float(np.dot(xs, ys) / np.dot(xs, xs))
        if theta_slow > 0:
            span_drift = theta / theta_slow

    resid = y - theta * x
    ss = float(np.dot(y, y))
    r2 = 1.0 - float(np.dot(resid, resid)) / ss if ss > 0 else 0.0

    # log-log slope of theta vs bar size; 0 for a true OU
    lx, ly = np.log(x), np.log(theta_i)
    lx_c = lx - lx.mean()
    drift = float(np.dot(lx_c, ly - ly.mean()) / np.dot(lx_c, lx_c))

    out.update({'theta': round(theta, 6), 'n_points': len(pts),
                'hl_sessions': round(math.log(2.0) / theta, 4),
                'r2': round(r2, 4), 'drift': round(drift, 3),
                'span_drift': round(span_drift, 3) if span_drift else None,
                'points': [(t, round(d, 6), round(th, 5)) for t, d, th in pts]})

    # ORDER MATTERS. multi_scale is the more specific diagnosis and a
    # superposition also trips span drift (its slow component is partly
    # reference-dependent). Test the bar-size slope first, so a name that has
    # two springs is told it has two springs rather than none.
    if drift < -THETA_DRIFT_MAX:
        out['verdict'] = 'multi_scale'
        out['reason'] = (f'theta shrinks as bars coarsen (slope {drift:+.2f}): '
                         f'apparent half-life grows with bar size. Superposed '
                         f'springs at different timescales; no single theta.')
    elif span_drift is None or span_drift > THETA_SPAN_DRIFT_MAX:
        out['verdict'] = 'no_spring'
        out['reason'] = (f'theta scales with the reference kernel '
                         f'(span drift {span_drift}): the "spring" is an '
                         f'artifact of the EMA, not the stock. A random walk '
                         f'minus its own EMA does exactly this, and the '
                         f'reference iteration converges on a self-consistent '
                         f'fake half-life for it.')
    elif r2 >= THETA_R2_MIN and drift <= THETA_DRIFT_POS_MAX:
        out['verdict'] = 'ou'
        out['reason'] = (f'one theta fits every bar size (R2={r2:.3f}, '
                         f'drift={drift:+.2f}, span drift={span_drift:.2f})')
    else:
        out['verdict'] = 'inconsistent'
        out['reason'] = (f'theta line does not hold '
                         f'(R2={r2:.3f}, drift={drift:+.2f})')
    return out
