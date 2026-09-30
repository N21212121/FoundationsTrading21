"""Where should CURL_SPREAD_QUIET sit, now that the field is RANGE not BODY?

E_N = mean(spread, last R bars) / mean(spread, N bars). The guard calls a tape
'flat' when E_N <= QUIET. To pick QUIET honestly you need the distribution of
E_N on a tape where NOTHING is happening -- a driftless random walk -- and then
take a percentile of that. design/10 derived 0.70 that way for the body field.

Nate ruled for range (high-low). Range is a far more efficient volatility
estimator than a close-based one (Parkinson 1980), so its ratio distribution is
TIGHTER and the same null percentile sits at a different number. Carrying 0.70
across would silently change how often the guard fires.
"""
import numpy as np

RNG = np.random.default_rng(20260930)
SUBSTEPS = 48           # intra-bar path resolution
DRAWS = 200_000

def draw(n_bars, draws=DRAWS):
    """(body, range) for draws x n_bars driftless bars, unit bar volatility."""
    steps = RNG.standard_normal((draws, n_bars, SUBSTEPS)) / np.sqrt(SUBSTEPS)
    path = np.cumsum(steps, axis=2)
    close = path[:, :, -1]
    hi = np.maximum(path.max(axis=2), 0.0)     # the open (0.0) counts
    lo = np.minimum(path.min(axis=2), 0.0)
    return np.abs(close), hi - lo

def ratio(vals, R):
    return vals[:, -R:].mean(axis=1) / vals.mean(axis=1)

R = 3
print(f'E_N null distribution, R={R} recent bars, {DRAWS:,} draws, '
      f'{SUBSTEPS} substeps/bar')
print()
for N, tf in ((15, '10Min'), (25, '6Min'), (50, '3Min')):
    body, rng_ = draw(N)
    rb, rr = ratio(body, R), ratio(rng_, R)
    print(f'N={N:>2} ({tf})')
    print(f'  {"field":6} {"p10":>7} {"p15":>7} {"p20":>7} {"p22":>7} '
          f'{"p25":>7} {"p30":>7} {"med":>7} {"sd":>7}')
    for nm, r in (('body', rb), ('range', rr)):
        q = np.percentile(r, [10, 15, 20, 22, 25, 30, 50])
        print(f'  {nm:6} ' + ' '.join(f'{v:7.3f}' for v in q) + f' {r.std():7.3f}')
    p_of_070 = (rb <= 0.70).mean() * 100
    equiv = np.percentile(rr, p_of_070)
    fires_body = (rb <= 0.70).mean() * 100
    fires_range = (rr <= 0.70).mean() * 100
    print(f'  design/10 put QUIET at 0.70 = p{p_of_070:.1f} of the BODY null')
    print(f'  the same percentile of the RANGE null is {equiv:.3f}')
    print(f'  reusing 0.70 on range would fire on {fires_range:.1f}% of a dead '
          f'tape instead of {fires_body:.1f}%  <- the silent change')
    print()
