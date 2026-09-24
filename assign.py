"""
assign.py - Foundations Trading

Turn a measured reversion timescale into an engine assignment. Deterministic.
Never looks at P&L. This is the whole point.

WHY THIS IS NOT A SEARCH

A stock's half-life in BARS is HL_real / bar_size. The OU engine's competence
band is [MIN_HL_BARS, MAX_HL_BARS] = [2, 12] bars:

    below 2   Nyquist. Fewer than two samples per half-life and you cannot
              reconstruct the cycle at all.
    above 12  statistical power. The variance-ratio error grows like
              sqrt(k/n) and k must track the half-life, so a slow spring
              spends bar count buying nothing.

So the bar size determines whether a given stock is visible at all. Pick it
wrong and a real spring reads 'invalid'.

The temptation is to let the screener try every timeframe and keep the best.
DO NOT. Eleven timeframes at alpha=0.05 is a 1 - 0.95^11 = 43% chance of at
least one false pass per name. Across a 2,786-name universe that is over a
thousand phantom springs, and every one of them will backtest beautifully.
That is the same disease as ranking engines per ticker and as picking a
lookback off a peak instead of a plateau: selection at the level where you
have the least data.

Instead: MEASURE the timescale, ASSIGN from it by arithmetic, FREEZE, then
screen. The screener measures P&L given the assignment. It never chooses it.

WHY THE TARGET IS THE GEOMETRIC CENTRE

Half-life estimates carry MULTIPLICATIVE error: rel_se runs 0.25-0.45, and
errors in theta propagate as ratios, not differences. So the target should sit
at the geometric centre of the band, giving equal multiplicative headroom in
both directions:

    T = sqrt(MIN_HL_BARS * MAX_HL_BARS) = sqrt(2 * 12) = 4.899

That is 2.45x of room to each edge, which covers roughly a 2-sigma error on a
35% relative standard error. The arithmetic centre (7) would give 3.5x of room
below and only 1.7x above -- and above is where power is thinnest, so you would
fall out the slow end first.

Snapping is done in LOG space, because the timeframe menu is geometric.

THE SESSION IS MEASURED PER NAME, NOT ASSUMED

The engine bands convert bars to sessions through sessions-per-bar. The
960-minute extended-hours model is right for a liquid name on the SIP feed
and wrong by ~2.46x for a name that only prints regular hours (390 min).
halflife.theta_line already measures sessions-per-bar empirically from the
name's own bars; the SNAP must use the same tape, or hl_bars lies at the
band edges for exactly the illiquid names most likely to sit there. So
assign_from_bars() derives a per-name intraday scale from the fetched bars
and applies it to the engine table. The 960 model remains the fallback and
the display default in bands().

PRECONDITION: THE NAME MUST HAVE A CLOCK

Only a name whose theta line reads 'ou' gets assigned. 'multi_scale' means the
apparent half-life grows with bar size, i.e. the stock has more than one spring
and no single theta describes it. Asking for its natural clock is not hard,
it is undefined. Abstention is a first-class answer.

WHY ASSIGNMENTS ARE WRITTEN TO CONFIG, NOT THE RESOLVER

app.py's _sync_engine_overrides rebuilds resolver.by_ticker from the config
watchlist on EVERY sweep and clears anything not backed by a watchlist
'engine' key. Config is the source of truth by design; a direct resolver
write is state the next sweep erases. apply_assignments() therefore writes
w['engine'] on watchlist entries -- the tier-1 store that survives the sync --
and the caller saves config and re-syncs. It never touches the resolver.
"""
import math

import halflife as hlf
import engine_ou as ou

# Target half-life in bars: the geometric centre of the engine's band.
TARGET_HL_BARS = math.sqrt(ou.MIN_HL_BARS * ou.MAX_HL_BARS)

# The engines we are allowed to assign to, fastest first.
CANDIDATES = tuple(cls.name for cls in ou.FAMILY)

# Every name this module may write or clear on a watchlist entry. Includes
# the legacy alias so a hand-set 'ou_reversion' is still recognised as ours.
OU_ENGINE_NAMES = frozenset(CANDIDATES) | {ou.OUReversionEngine.name}

# Per-name intraday scale = measured spb / 960-model spb. Clamped: a name
# cannot trade more than the full extended tape (scale just under 1) and
# RTH-only is ~2.46; anything past 3 is a data artifact, not a schedule.
SCALE_MIN, SCALE_MAX = 0.9, 3.0


def _spb(timeframe):
    """MODEL sessions per bar, from the timeframe alone (no data needed).
    Daily and slower from the constant map; intraday assumes the full
    960-minute extended session. Per-name reality enters via `scale` in
    _engine_table()."""
    if timeframe in hlf.SESSIONS_PER_BAR:
        return hlf.SESSIONS_PER_BAR[timeframe]
    return hlf.TF_MINUTES[timeframe] / 960.0     # extended-hours session


def _intraday_scale(by_tf):
    """Measured-vs-model session ratio for THIS name.

    For each fetched INTRADAY timeframe, take halflife's empirically measured
    sessions-per-bar and divide by the 960-minute model. Median across
    timeframes, clamped to [SCALE_MIN, SCALE_MAX]. ~1.0 for a name that
    prints the full extended session; ~2.46 for RTH-only. Falls back to 1.0
    when nothing intraday is measurable, so the model answer degrades to the
    old behavior rather than failing."""
    ratios = []
    for tf, bars in (by_tf or {}).items():
        if tf in hlf.SESSIONS_PER_BAR or tf not in hlf.TF_MINUTES:
            continue                              # daily+ or unknown: skip
        spb = hlf.sessions_per_bar(tf, bars)
        model = hlf.TF_MINUTES[tf] / 960.0
        if spb and model > 0:
            ratios.append(spb / model)
    if not ratios:
        return 1.0
    ratios.sort()
    n = len(ratios)
    med = (ratios[n // 2] if n % 2
           else (ratios[n // 2 - 1] + ratios[n // 2]) / 2.0)
    return min(SCALE_MAX, max(SCALE_MIN, med))


def _engine_table(scale=1.0):
    """[(engine_name, timeframe, sessions_per_bar)] fastest first.
    `scale` multiplies the INTRADAY model spb only; daily and slower are a
    session by definition and do not scale."""
    out = []
    for cls in ou.FAMILY:
        tf = cls.timeframes['primary']
        base = _spb(tf)
        if tf not in hlf.SESSIONS_PER_BAR:
            base *= scale
        out.append((cls.name, tf, base))
    return sorted(out, key=lambda r: r[2])


def bands(scale=1.0):
    """The half-life band each engine can resolve, in SESSIONS. For display.
    Default scale=1.0 shows the extended-hours (960-min) model; pass a
    measured scale to see a specific name's effective bands."""
    return [{'engine': n, 'timeframe': tf,
             'hl_sessions_min': round(ou.MIN_HL_BARS * s, 4),
             'hl_sessions_max': round(ou.MAX_HL_BARS * s, 4)}
            for n, tf, s in _engine_table(scale)]


def assign_one(hl_sessions, verdict='ou', scale=1.0):
    """One name's engine, from its measured session half-life.

    Returns {'engine', 'timeframe', 'hl_bars', 'reason'} or
            {'engine': None, 'reason': str} when the name has no home.

    `scale` is the name's measured-vs-model session ratio (see
    _intraday_scale); it corrects the intraday engine bands for names that
    do not print the full extended tape.

    Two independent reasons to refuse:
      1. The theta line did not read 'ou'. No single timescale exists.
      2. The snapped timeframe puts the half-life outside [2, 12] bars.
         This happens at the edges: a spring faster than the fastest engine
         can resolve, or slower than the slowest can afford to hold.
    """
    if verdict != 'ou':
        return {'engine': None, 'timeframe': None, 'hl_bars': None,
                'reason': f"theta line reads '{verdict}': no single timescale "
                          f'to assign to'}
    if not hl_sessions or hl_sessions <= 0:
        return {'engine': None, 'timeframe': None, 'hl_bars': None,
                'reason': 'no half-life measured'}

    want_spb = hl_sessions / TARGET_HL_BARS
    table = _engine_table(scale)

    # Snap in LOG space: the timeframe menu is geometric, so the nearest
    # candidate by ratio is the right nearest, not the nearest by difference.
    name, tf, spb = min(table, key=lambda r: abs(math.log(r[2] / want_spb)))
    hl_bars = hl_sessions / spb
    scale_note = f' (session scale {scale:.2f})' if abs(scale - 1) > 0.05 else ''

    if hl_bars < ou.MIN_HL_BARS:
        fastest = table[0]
        return {'engine': None, 'timeframe': None,
                'hl_bars': round(hl_bars, 2),
                'reason': f'half-life {hl_sessions:.4f} sessions is faster '
                          f'than {fastest[1]} bars can resolve '
                          f'({hl_bars:.1f} < {ou.MIN_HL_BARS} bars: Nyquist)'
                          f'{scale_note}'}
    if hl_bars > ou.MAX_HL_BARS:
        slowest = table[-1]
        return {'engine': None, 'timeframe': None,
                'hl_bars': round(hl_bars, 2),
                'reason': f'half-life {hl_sessions:.4f} sessions is slower '
                          f'than {slowest[1]} bars can certify '
                          f'({hl_bars:.1f} > {ou.MAX_HL_BARS} bars: no power)'
                          f'{scale_note}'}

    return {'engine': name, 'timeframe': tf, 'hl_bars': round(hl_bars, 2),
            'reason': f'HL {hl_sessions:.4f} sessions -> {hl_bars:.1f} bars '
                      f'on {tf} (target {TARGET_HL_BARS:.1f}){scale_note}'}


def assign_from_estimator(results):
    """Map estimator output to engine assignments.

    Deliberately unimplemented: the pooled theta line refits from BARS, not
    from the summary rows that estimate_basket_multi() reports, and the
    per-name session scale needs the bars too. Use assign_from_bars()."""
    raise NotImplementedError('use assign_from_bars(); the pooled theta line '
                              'refits from bars, not from summary rows')


def assign_from_bars(bars_by_symbol_tf, progress=None):
    """The real entry point. {symbol: {timeframe: bars}} -> assignments.

    Never raises per-name: an unusable series lands as engine=None with a
    reason, not a dead sweep."""
    out, total = [], len(bars_by_symbol_tf)
    for i, (sym, by_tf) in enumerate(sorted(bars_by_symbol_tf.items()), 1):
        try:
            tl = hlf.theta_line(by_tf)
        except Exception as e:
            tl = {'verdict': 'insufficient', 'hl_sessions': None,
                  'r2': None, 'drift': None, 'reason': f'error: {e}',
                  'n_points': 0}
        try:
            scale = _intraday_scale(by_tf)
        except Exception:
            scale = 1.0
        a = assign_one(tl.get('hl_sessions'),
                       tl.get('verdict', 'insufficient'), scale=scale)
        out.append({
            'ticker': sym,
            'engine': a['engine'], 'timeframe': a['timeframe'],
            'hl_sessions': tl.get('hl_sessions'),
            'hl_bars': a.get('hl_bars'),
            'session_scale': round(scale, 3),
            'theta_verdict': tl.get('verdict'),
            'theta': tl.get('theta'),
            'r2': tl.get('r2'), 'drift': tl.get('drift'),
            'n_timeframes': tl.get('n_points'),
            'reason': a['reason'] if a['engine'] else
                      (a['reason'] if tl.get('verdict') == 'ou'
                       else tl.get('reason', a['reason'])),
        })
        if progress and progress(i, total, 'names') == 'stop':
            # Caller asked to cancel mid-fit. Return what we have; the job
            # runner discards it. Partial-and-thrown-away beats not stopping.
            return out
    # assigned first, then fastest half-life; unassigned sink
    out.sort(key=lambda r: (0 if r['engine'] else 1,
                            r['hl_sessions'] if r['hl_sessions'] else 1e18))
    return out


def apply_assignments(cfg, assignments, dry_run=True):
    """Write assignments into the CONFIG WATCHLIST -- the only store that
    survives _sync_engine_overrides. Mutates cfg in place when dry_run is
    False; the caller owns save_config() and the re-sync.

    dry_run=True by default. This function writes an arithmetic result,
    never a ranking, and it only manages what is OURS:

      - engine set   -> w['engine'] = <ou_reversion_*>. One line, tier 1.
      - engine None  -> only if the ticker's CURRENT engine is in
                        OU_ENGINE_NAMES: clear w['engine'] AND set
                        mode='pause'. A stock that lost its clock must STOP
                        TRADING; merely clearing the override would drop it
                        onto the Ripster default, which is trading the OLD
                        assumption under a new flag. Names running Ripster
                        (or no override) are not this module's to touch.
      - not on the watchlist -> skipped and reported. Deploying a name is a
                        human decision (basket deploy); assignment only
                        routes names already deployed.

    Returns {'changed': [...], 'skipped': [...]}."""
    by_ticker = {w['ticker'].upper(): w for w in cfg.get('watchlist', [])}
    changed, skipped = [], []
    for a in assignments:
        t = a['ticker'].upper()
        w = by_ticker.get(t)
        want = a['engine']
        if w is None:
            skipped.append({'ticker': t, 'engine': want,
                            'reason': 'not on watchlist; deploy first'})
            continue
        cur = w.get('engine')
        if want:
            if cur == want:
                continue
            changed.append({'ticker': t, 'from': cur, 'to': want,
                            'reason': a['reason']})
            if not dry_run:
                w['engine'] = want
        else:
            if cur not in OU_ENGINE_NAMES:
                continue                 # not ours to manage
            changed.append({'ticker': t, 'from': cur, 'to': None,
                            'mode': 'pause', 'reason': a['reason']})
            if not dry_run:
                w.pop('engine', None)
                w['mode'] = 'pause'      # lost its clock: stop, don't fall back
    return {'changed': changed, 'skipped': skipped}
