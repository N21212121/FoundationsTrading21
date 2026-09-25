"""
backtest_sweep.py - Foundations Trading

The timeframe matrix. Replays one basket over one date range through every
(entry timeframe x exit timeframe x period mode) cell of engine_ripster_tf and
prints a comparison table, with the LIVE configuration as the control row.

  py -3 backtest_sweep.py --symbols SPY --start 2026-06-01 --end 2026-09-19
  py -3 backtest_sweep.py --selftest --start 2026-08-01

Nothing in the live path is touched. This file CALLS backtester.run_symbol,
backtester.compute_metrics and backtest_run.warmup_start; it does not modify
any of them.

─── THE ONE THING THAT WOULD HAVE MADE THIS TABLE A LIE ──────────────────────

The cells have different warm-ups. `ripster_tf_e3m_x3m_scaled` needs 835
three-minute bars before it may speak; the control needs 250 ten-minute ones.
Hand each one a warm-up-padded fetch and let backtester.run_basket replay the
lot, and each cell goes live on a DIFFERENT DAY -- the fast/scaled cells last,
after the others have already banked or lost a week of trades. Every
difference in the table would then be part strategy and part calendar, with
no way to tell which.

So each symbol is replayed through backtester.run_symbol directly with
`min_window` set to the index of the first bar at or after --start. Evaluation
then begins on the same calendar bar for every cell, with each cell's own full
history sitting behind it. run_symbol's docstring warns that min_window must
never exceed the engine's own warm-up "or the replay diverges from live": that
is exactly what is being done here and it is deliberate. The evals being
skipped are the ones inside the warm-up pad, which is data borrowed to fill
the EMAs and not part of the measured window. Inside the measured window the
replay is bar-for-bar the live loop.

The aggregation below (pooled trades, summed daily marks, one equity curve,
one compute_metrics) is backtester.run_basket's, line for line. It is repeated
here only because run_basket does not pass min_window through. A one-line
`min_window=None` passthrough on run_basket would let this file delete
_aggregate entirely; that is written up as a prerequisite in
design/05-timeframe-matrix.md rather than made here.

─── THE OTHER THING THAT WOULD HAVE MADE IT A LIE: EXTENDED HOURS ────────────

The feed is SIP and covers 04:00-20:00 ET. signal_engine.ENTRY_START is
enforced as a LOWER bound only -- `if now_et.time() < ENTRY_START` -- and
there is no upper bound anywhere in the system. Live, the market closing is
the upper bound and no code is needed. In a replay there is no market, so the
engine cheerfully enters at 18:40 on a 900-share after-hours bar and the
backtester fills it.

Measured, not assumed: replaying SPY through the CONTROL cell
(ripster_tf_e10m_x10m_stock, identical to the live engine -- see --selftest)
from 2026-08-01 put 31 of 68 entries, 46%, outside 09:30-16:00 ET. A sibling
audit found the same thing on AAPL at 31%.

This is not a wash across the matrix, which is why it had to be fixed before a
single number was produced. Extended hours are 600 of the session's 990
minutes, so the COUNT of extended-hours bars scales inversely with bar size: a
3Min cell sees 3.3x as many of them as a 10Min cell and would book
proportionally more trades the live book could never take. The bias runs in
favour of exactly the timeframes under test.

So a regular-trading-hours gate is applied here, in this file, to EVERY cell
including the control:

  --rth entries   (default) ENTER_* is suppressed unless the deciding bar
                  closes in [09:30, 16:00) ET. A combined
                  EXIT_X_THEN_ENTER_Y degrades to EXIT_X, so a position is
                  never trapped by the gate. This is the minimal fix: it
                  removes trades that could not have happened and touches
                  nothing else.
  --rth all       No DECISION at all outside the window. Arguably the more
                  faithful model -- an 18:40 exit is as impossible as an
                  18:40 entry -- at the cost of holding to the next session's
                  open, which is genuine overnight gap risk the live book
                  also carries.
  --rth off       Unfiltered. Kept so the size of the contamination can be
                  measured rather than asserted.

The bars themselves are NOT filtered. Extended-hours bars stay in the EMA
windows, because the live cache contains them and the live clouds are shaped
by them. Dropping them would be a different strategy, not a cleaner backtest.
That does mean a residual the gate cannot remove: overnight and pre-market
tape is thin, thin tape whipsaws the 5/12, and the number of thin bars in the
window scales inversely with bar size too. The fast cells still carry more of
that than the control. It is a real cost of trading a 3-minute chart on this
feed, not an artifact, but it is not separated out here either.

The producer-side fix -- an upper bound next to ENTRY_START in signal_engine
-- is written up as a prerequisite in design/05-timeframe-matrix.md and was
NOT made. Adopting it would change every historical backtest number in the
system.

─── WHAT THE TABLE CANNOT TELL YOU ───────────────────────────────────────────

One basket, one date range, one market. Eighteen numbers drawn from one sample
are eighteen observations, not eighteen findings, and the spread between the
best and worst cell is an upper bound on how much of this is noise, not a
measure of edge. The sample size is printed on every table for that reason.

And the cost column is the one that decides. Halving the bar size roughly
doubles the trade count; every trade pays the spread twice. BREAKEVEN SLIP is
the slippage at which a cell's gross P/L becomes zero, straight out of
compute_metrics. A fast cell that beats the control on total P/L while showing
a breakeven slippage below what you actually pay has not beaten anything. The
simulation is on the UNDERLYING; the live trade is options, where the round
trip costs vastly more than the share spread, so the real breakeven bar is
higher than the one printed.
"""
import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, time as dtime

import config_manager as cm
import backtester
import backtest_run as br
import engine_ripster_tf as tfx
from engine_contract import validate_decision

ET = tfx.ET

CACHE_DIR = os.path.join(cm.DATA_DIR, 'tf_sweep_cache')

# Regular trading hours, as a half-open interval on the DECIDING BAR'S CLOSE.
# Half-open at 16:00 on purpose: backtester fills at the NEXT bar's open, so a
# bar closing exactly at 16:00 would fill after the bell. A bar closing at
# 09:30 is the 09:20-09:30 pre-market bar and fills at the 09:30 open, which
# is the opening drive signal_engine.ENTRY_START was moved to 09:30 to catch.
RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)
RTH_MODES = ('entries', 'all', 'off')

# House default, same as backtest_run.py's --slippage-bps. 2bps of a $600 SPY
# share is about 12 cents a round trip, which is generous for SPY shares and
# meaningless for SPY options. A judgement call, and the wrong one to lean on:
# read BREAKEVEN SLIP, not this.
DEFAULT_SLIPPAGE_BPS = 2.0


# ─── BAR FETCH + ON-DISK CACHE ────────────────────────────────────────────────
#
# 3Min SPY for seven months is ~52,000 bars and took 80 seconds to pull. The
# matrix asks for the same three streams eighteen times. Fetch once, keep it
# on disk, and a re-run of the sweep costs nothing but CPU. The cache is also
# how the worker processes get their bars: pickling 50k dicts per config into
# a pool is slower than re-reading the file.

def _cache_path(symbol, tf, start, end):
    os.makedirs(CACHE_DIR, exist_ok=True)
    e = end.strftime('%Y%m%d') if end else 'now'
    return os.path.join(CACHE_DIR,
                        f'{symbol}_{tf}_{start:%Y%m%d}_{e}.json')


def _cached_bars(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


_BAR_MEM = {}


def _load_bars(path):
    """Process-local memo so a worker handling several configs on the same
    stream reads the file once."""
    if path not in _BAR_MEM:
        _BAR_MEM[path] = _cached_bars(path)
    return _BAR_MEM[path]


def fetch_streams(symbols, tf_needs, base_start, end, force=False):
    """Fill the cache for every stream the matrix consumes.

    tf_needs is {timeframe: max history bars any cell wants of it}. Each
    stream is fetched from backtest_run.warmup_start(base_start, tf, bars),
    which is the project's own calendar padding -- reused rather than
    reinvented, because a 3Min cell with 167-period EMAs needs a lot of
    history and a home-made pad that is silently short produces a table of
    zeros rather than an error.

    Returns {(symbol, tf): path}. Connects to Alpaca only for what is missing.
    """
    paths, missing = {}, {}
    for tf, bars in tf_needs.items():
        start = br.warmup_start(base_start, tf, bars)
        for s in symbols:
            p = _cache_path(s, tf, start, end)
            paths[(s, tf)] = p
            if force or not os.path.exists(p):
                missing.setdefault((tf, start), []).append(s)

    if not missing:
        print(f'bars: all {len(paths)} streams already cached in {CACHE_DIR}')
        return paths

    from alpaca_manager import AlpacaManager
    cfg = cm.load_config()
    if not (cfg.get('alpaca_key') and cfg.get('alpaca_secret')):
        sys.exit('no alpaca credentials in config.json; cannot fetch bars')
    alpaca = AlpacaManager()
    r = alpaca.connect(cfg.get('alpaca_key', ''), cfg.get('alpaca_secret', ''),
                       paper=cfg.get('paper_trading', True))
    if r.get('status') != 'ok':
        sys.exit(f"alpaca: {r.get('message')}")

    for (tf, start), syms in sorted(missing.items(),
                                    key=lambda kv: kv[0][0]):
        print(f'fetching {tf} for {syms} from {start:%Y-%m-%d} ...')
        got = br._fetch(alpaca, syms, tf, start, end)
        for s in syms:
            bars = got.get(s) or []
            if not bars:
                print(f'  WARNING: no {tf} bars for {s}')
            with open(_cache_path(s, tf, start, end), 'w',
                      encoding='utf-8') as f:
                json.dump(bars, f)
            print(f'  {s} {tf}: {len(bars)} bars')
    return paths


# ─── THE RTH GATE ─────────────────────────────────────────────────────────────

class RTHGate:
    """Wraps any StrategyEngine and refuses actions outside regular hours.

    A WRAPPER, not an engine edit, for two reasons. It keeps the fix in this
    file, where it is in scope. And it keeps engine_ripster_tf's control cell
    bit-identical to the live engine_ripster, which is what --selftest
    asserts and what makes the control row mean anything -- the gate is then
    demonstrably an artifact of the REPLAY, applied identically to all
    eighteen cells, rather than a change to the strategy.

    Stateless, like everything it wraps. Delegates timeframes/history/
    execution so backtester.run_symbol cannot tell the difference.
    """

    def __init__(self, engine, mode='entries'):
        if mode not in RTH_MODES:
            raise ValueError(f'rth mode must be one of {RTH_MODES}')
        self.engine = engine
        self.mode = mode
        self.name = engine.name if mode == 'off' else f'{engine.name}+rth'
        self.timeframes = engine.timeframes
        self.history = engine.history
        self.execution = engine.execution

    def __getattr__(self, k):          # primary_tf, slow_tf, describe, ...
        return getattr(self.engine, k)

    def evaluate(self, ticker, bars, position_direction=None, now_et=None,
                 position=None):
        d = self.engine.evaluate(ticker, bars,
                                 position_direction=position_direction,
                                 now_et=now_et, position=position)
        if self.mode == 'off':
            return d
        t = (now_et or datetime.now(ET)).astimezone(ET).time()
        if RTH_START <= t < RTH_END:
            return d
        a = d['action']
        if a == 'NONE':
            return d
        if self.mode == 'all':
            d = dict(d, action='NONE', exit_kind=None, exit_tag=None,
                     rth_blocked=a)
        elif a.startswith('ENTER_'):
            d = dict(d, action='NONE', rth_blocked=a)
        elif '_THEN_ENTER_' in a:
            # Never trap a position behind the gate: the exit still fires,
            # only the re-entry is dropped.
            d = dict(d, action=a.split('_THEN_')[0], rth_blocked=a)
        return validate_decision(d)


# ─── ONE CELL ─────────────────────────────────────────────────────────────────

def _first_index_at(bars, start_dt):
    """Index of the first bar at or after start_dt (ET midnight of --start).
    Every cell begins evaluating here, whatever its warm-up."""
    for i, b in enumerate(bars):
        if datetime.fromisoformat(b['time']).astimezone(ET) >= start_dt:
            return i
    return len(bars)


def _aggregate(per_symbol_results, capital, slippage_bps):
    """backtester.run_basket's aggregation, verbatim in behaviour. See the
    module docstring for why it is repeated instead of called."""
    all_trades, marks_by_day, evals = [], {}, 0
    for r in per_symbol_results.values():
        all_trades.extend(r['trades'])
        evals += r['evals']
        for day, m in r['daily_marks'].items():
            marks_by_day[day] = marks_by_day.get(day, 0.0) + m
    equity = backtester._equity_curve(all_trades, marks_by_day, capital)
    m = backtester.compute_metrics(all_trades, equity, capital,
                                   slippage_bps=slippage_bps)
    m['evals'] = evals
    m['symbols'] = len(per_symbol_results)
    return {'trades': all_trades, 'equity_curve': equity, 'metrics': m}


def run_cell(entry_tf, exit_tf, mode, symbols, paths, start_dt,
             capital, budget, slippage_bps, rth='entries'):
    """Replay one matrix cell. Returns a row dict, or {'error': ...}."""
    base = tfx.build(entry_tf, exit_tf, mode)
    eng = RTHGate(base, rth)
    d = base.describe()
    results = {}
    eval_bars = 0          # primary bars actually evaluated, for time-in-market
    for s in symbols:
        primary = _load_bars(paths[(s, eng.primary_tf)])
        if not primary:
            continue
        macro = (_load_bars(paths[(s, eng.slow_tf)])
                 if eng.slow_tf else None)
        i0 = _first_index_at(primary, start_dt)
        if i0 >= len(primary) - 1:
            continue
        r = backtester.run_symbol(eng, s, primary, macro,
                                  budget_dollars=budget,
                                  slippage_bps=slippage_bps,
                                  min_window=i0 + 1)
        results[s] = r
        eval_bars += len(primary) - max(1, i0)
    if not results:
        return {'error': 'no bars in window', **d}

    agg = _aggregate(results, capital, slippage_bps)
    m = agg['metrics']
    p_min = tfx.tf_min(eng.primary_tf)

    # TIME IN MARKET. compute_metrics does not carry it, so it is derived
    # here from the trades it was given: bar-time held divided by bar-time
    # evaluated, both on the primary stream. Bar-time, not wall-clock,
    # because Ripster can hold overnight and a wall-clock denominator would
    # charge a 10Min cell for 17 hours a night it was never asked about.
    held_bars = sum(t.get('bars_held', 0) for t in agg['trades'])
    tim = (held_bars / eval_bars * 100.0) if eval_bars else 0.0

    # Entries the gate refused, and entries that landed outside RTH anyway
    # (must be zero unless rth='off'). The second is the audit of the first.
    off_hours = sum(1 for t in agg['trades']
                    if not (RTH_START
                            <= datetime.fromisoformat(t['entry_time'])
                            .astimezone(ET).time() < RTH_END))

    row = {
        'name': d['name'], 'entry_tf': entry_tf, 'exit_tf': exit_tf,
        'mode': mode, 'rth': rth, 'primary_tf': eng.primary_tf,
        'macro_tf': eng.slow_tf or '-', 'is_control': d['is_control'],
        'entry_periods': '/'.join(str(p) for p in d['entry_periods']),
        'exit_periods': '/'.join(str(p) for p in d['exit_periods']),
        'entry_gate_hours': round(d['entry_horizon_min'][3] / 60.0, 1),
        'exit_gate_hours': round(d['exit_horizon_min'][3] / 60.0, 1),
        'n_trades': m['n_trades'], 'win_rate_pct': m['win_rate_pct'],
        'avg_pnl': m['expectancy'], 'total_pnl': m['total_pnl'],
        'avg_win': m['avg_win'], 'avg_loss': m['avg_loss'],
        'profit_factor': (m['profit_factor']
                          if m['profit_factor'] != float('inf') else None),
        'max_drawdown_pct': m['max_drawdown_pct'],
        'time_in_market_pct': round(tim, 1),
        'avg_hold_min': (round((m['avg_bars_held'] or 0) * p_min, 1)
                         if m['avg_bars_held'] is not None else None),
        'sharpe': m['sharpe'],
        'gross_pnl': m['gross_pnl'], 'slippage_cost': m['slippage_cost'],
        'breakeven_slip_bps': m['breakeven_slippage_bps'],
        'turnover_annual': m['turnover_annual'],
        'trading_days': m['trading_days'], 'evals': m['evals'],
        'n_long': sum(1 for t in agg['trades'] if t['direction'] == 'long'),
        'n_short': sum(1 for t in agg['trades'] if t['direction'] == 'short'),
        'entries_off_hours': off_hours,
        'exits_by_reason': m['exits_by_reason'],
    }
    return row


def _worker(args):
    (entry_tf, exit_tf, mode, symbols, paths, start_iso, cap, bud, slip,
     rth) = args
    try:
        return run_cell(entry_tf, exit_tf, mode, symbols, paths,
                        datetime.fromisoformat(start_iso), cap, bud, slip,
                        rth=rth)
    except Exception as e:
        import traceback
        return {'error': f'{e}', 'trace': traceback.format_exc(),
                'name': tfx.engine_name(entry_tf, exit_tf, mode),
                'entry_tf': entry_tf, 'exit_tf': exit_tf, 'mode': mode,
                'is_control': False}


# ─── TABLE ────────────────────────────────────────────────────────────────────

COLS = [
    ('entry', 7, lambda r: r['entry_tf']),
    ('exit', 7, lambda r: r['exit_tf']),
    ('gateH', 6, lambda r: f"{r['entry_gate_hours']}"),
    ('trades', 7, lambda r: r['n_trades']),
    ('L/S', 9, lambda r: f"{r['n_long']}/{r['n_short']}"),
    ('win%', 6, lambda r: r['win_rate_pct']),
    ('avgP/L', 9, lambda r: f"{r['avg_pnl']:+.2f}"),
    ('totP/L', 11, lambda r: f"{r['total_pnl']:+.2f}"),
    ('maxDD%', 7, lambda r: r['max_drawdown_pct']),
    ('inMkt%', 7, lambda r: r['time_in_market_pct']),
    ('holdMin', 8, lambda r: r['avg_hold_min']),
    ('BE slip', 8, lambda r: r['breakeven_slip_bps']),
    ('turn/yr', 8, lambda r: r['turnover_annual']),
]


def _fmt_row(r, control):
    cells = []
    for _, w, fn in COLS:
        try:
            v = fn(r)
        except Exception:
            v = '?'
        cells.append(f'{str(v):>{w}}')
    delta = ''
    if control and not r['is_control']:
        d = r['total_pnl'] - control['total_pnl']
        delta = f"  {d:+11.2f}"
    elif r['is_control']:
        delta = '  ' + 'CONTROL'.rjust(11)
    return ' '.join(cells) + delta


def _header():
    cells = [f'{n:>{w}}' for n, w, _ in COLS]
    return ' '.join(cells) + '  ' + 'vs control'.rjust(11)


def print_tables(rows, meta):
    control = next((r for r in rows
                    if r.get('is_control') and 'error' not in r), None)

    print()
    print('=' * 108)
    print('TIMEFRAME MATRIX - Ripster entry TF x exit TF x period mode')
    print('=' * 108)
    print(f"  symbols          {', '.join(meta['symbols'])}  "
          f"({len(meta['symbols'])})")
    print(f"  window           {meta['start']} -> {meta['end']}  "
          f"({control['trading_days'] if control else '?'} trading days "
          f"in the pooled equity curve)")
    print(f"  sizing           ${meta['budget']:,.0f} per position, "
          f"capital ${meta['capital']:,.0f}")
    print(f"  slippage assumed {meta['slippage_bps']} bps per side, "
          f"on the UNDERLYING only (no options spread modelled)")
    rth_blurb = {
        'entries': 'ENTRIES ONLY inside 09:30-16:00 ET (exits unrestricted)',
        'all': 'NO ACTION outside 09:30-16:00 ET',
        'off': 'OFF -- extended-hours entries INCLUDED. These numbers '
               'contain trades the live book could not take.',
    }[meta['rth']]
    print(f"  RTH gate         {rth_blurb}")
    off = sum(r.get('entries_off_hours', 0) for r in rows if 'error' not in r)
    print(f"  entries landing outside 09:30-16:00 ET across all cells: {off}"
          + ('' if meta['rth'] == 'off' else '  (must be 0)'))
    print()
    print('  SAMPLE SIZE: one basket, one date range, one market regime. '
          'These are')
    print('  observations, not findings. Nothing here has been tested '
          'out of sample.')
    print()

    for mode, blurb in (
        ('stock',
         'periods stay 5/12/34/50 -> smaller bars mean SHORTER horizons. '
         'A genuinely faster system.'),
        ('scaled',
         'periods x 10/tf_min -> every cloud spans the same wall clock. '
         'Same system, sampled more often.'),
    ):
        sub = [r for r in rows if r.get('mode') == mode]
        if not sub:
            continue
        print('-' * 108)
        print(f'  PERIOD MODE: {mode.upper()}   {blurb}')
        print(f'  gateH = wall-clock hours the ENTRY 34/50 trend gate spans. '
              f'The control is 8.3h.')
        print('-' * 108)
        print(_header())
        # control first, then the rest in matrix order
        ordered = ([r for r in sub if r.get('is_control')]
                   + [r for r in sub if not r.get('is_control')])
        if control and not any(r.get('is_control') for r in sub):
            print(_fmt_row(control, control) + '   (control, stock mode)')
        for r in ordered:
            if 'error' in r:
                print(f"{r['entry_tf']:>7} {r['exit_tf']:>7}   "
                      f"ERROR: {r['error']}")
                continue
            print(_fmt_row(r, control))
        print()

    print('-' * 108)
    print('  BE slip = breakeven slippage in bps (compute_metrics). If it is '
          'below the round-trip')
    print('  cost you actually pay, the cell does not exist. The live trade '
          'is OPTIONS; the spread')
    print('  there is far wider than on shares, and it is paid once per '
          'trade, so the cells with')
    print('  the biggest trade counts are the ones this column is aimed at.')
    print('  inMkt% = primary bars held / primary bars evaluated. holdMin = '
          'average hold in minutes,')
    print('  which is the only hold column comparable across bar sizes.')
    print('-' * 108)


# ─── SELF-TEST: does the control row actually reproduce the live engine? ──────

def selftest(symbols, start_dt, end, capital, budget, slippage_bps):
    """Two assertions, both on real bars. Neither is optional: a matrix whose
    control row is not the live system compares everything to nothing.

      1. engine_ripster_tf.context(df, BASE_PERIODS) == signal_engine
         .trend_context(df) on every decision-relevant key, over many windows.
         This is the check that the parameterised copy has not drifted.
      2. The control cell's trades are identical to RipsterEMACloudEngine's
         over the same bars with the same min_window -- same entries, same
         exits, same prices, same quantities. exit_reason is allowed to
         differ by the '|10Min' exit_tag this variant adds for reporting.
    """
    import signal_engine as se
    from engine_ripster import RipsterEMACloudEngine

    tf_needs = {tfx.BASE_TF: tfx.history_for(tfx.BASE_PERIODS)}
    paths = fetch_streams(symbols, tf_needs, start_dt, end)

    ok = True
    for s in symbols:
        bars = _load_bars(paths[(s, tfx.BASE_TF)])
        if not bars:
            print(f'  {s}: no bars, skipped')
            continue

        # -- 1. context vs trend_context -------------------------------------
        keys = ('trend', 'fresh_long', 'fresh_short', 'long_exit',
                'short_exit', 'result', 'price_vs_5_12', 'price_vs_34_50',
                'emas', 'closes', 'clouds')
        n_checked = 0
        step = max(1, (len(bars) - 300) // 400)
        for i in range(300, len(bars), step):
            df = se.bars_to_df(bars[max(0, i - 300):i])
            a = se.trend_context(df)
            b = tfx.context(df, tfx.BASE_PERIODS)
            for k in keys:
                if a[k] != b[k]:
                    print(f'  FAIL {s} bar {i} key {k}: {a[k]!r} != {b[k]!r}')
                    ok = False
            n_checked += 1
        print(f'  {s}: context == trend_context on {n_checked} windows '
              f'({"ok" if ok else "FAILED"})')

        # -- 2. control cell vs the live engine ------------------------------
        i0 = _first_index_at(bars, start_dt)
        live = backtester.run_symbol(RipsterEMACloudEngine(), s, bars, None,
                                     budget_dollars=budget,
                                     slippage_bps=slippage_bps,
                                     min_window=i0 + 1)
        ctrl = backtester.run_symbol(tfx.build(*tfx.CONTROL), s, bars, None,
                                     budget_dollars=budget,
                                     slippage_bps=slippage_bps,
                                     min_window=i0 + 1)
        cmp_keys = ('direction', 'entry_time', 'entry_price', 'exit_time',
                    'exit_price', 'qty', 'pnl_dollars', 'bars_held')
        if len(live['trades']) != len(ctrl['trades']):
            print(f'  FAIL {s}: live {len(live["trades"])} trades, '
                  f'control {len(ctrl["trades"])}')
            ok = False
        else:
            bad = 0
            for a, b in zip(live['trades'], ctrl['trades']):
                if any(a[k] != b[k] for k in cmp_keys):
                    bad += 1
            if bad:
                print(f'  FAIL {s}: {bad} trades differ')
                ok = False
            else:
                print(f'  {s}: control cell == ripster_ema_cloud on all '
                      f'{len(live["trades"])} trades (ok)')

    print()
    print('SELFTEST ' + ('PASSED' if ok else 'FAILED'))
    return ok


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Ripster entry-TF x exit-TF x period-mode matrix')
    ap.add_argument('--symbols', default='SPY',
                    help='comma-separated (default SPY, the owner\'s own '
                         'example)')
    ap.add_argument('--basket', default=None, help='basket name instead')
    ap.add_argument('--start', required=True, help='YYYY-MM-DD')
    ap.add_argument('--end', default=None, help='YYYY-MM-DD')
    ap.add_argument('--capital', type=float, default=100000.0)
    ap.add_argument('--alloc-dollars', type=float, default=10000.0,
                    help='pinned budget per position (default $10,000). A '
                         'flat dollar budget keeps per-trade P/L comparable '
                         'across cells, which percent-of-capital would not '
                         'once trade counts diverge.')
    ap.add_argument('--slippage-bps', type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument('--rth', choices=RTH_MODES, default='entries',
                    help="regular-hours gate on the deciding bar's close. "
                         "'entries' (default) blocks ENTER_* outside "
                         "09:30-16:00 ET; 'all' blocks every action; 'off' "
                         'is unfiltered and exists only to measure the '
                         'contamination. See the module docstring.')
    ap.add_argument('--tfs', default=','.join(tfx.MATRIX_TFS))
    ap.add_argument('--modes', default=','.join(tfx.PERIOD_MODES))
    ap.add_argument('--workers', type=int, default=None,
                    help='process pool over CELLS (default: cpu count)')
    ap.add_argument('--refetch', action='store_true',
                    help='ignore the bar cache')
    ap.add_argument('--selftest', action='store_true',
                    help='verify the control row reproduces the live engine, '
                         'then exit')
    ap.add_argument('--out', default=None, help='results CSV path')
    args = ap.parse_args()

    if args.basket:
        import baskets as bk
        symbols = bk.get_basket(args.basket)['symbols']
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(',')
                   if s.strip()]
    if not symbols:
        sys.exit('no symbols')

    start_dt = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=ET)
    base_start = datetime.strptime(args.start, '%Y-%m-%d')
    end = datetime.strptime(args.end, '%Y-%m-%d') if args.end else None

    if args.selftest:
        ok = selftest(symbols, start_dt, end, args.capital,
                      args.alloc_dollars, args.slippage_bps)
        sys.exit(0 if ok else 1)

    tfs = tuple(t.strip() for t in args.tfs.split(',') if t.strip())
    modes = tuple(m.strip() for m in args.modes.split(',') if m.strip())
    cells = tfx.matrix_configs(tfs, modes)

    # Every stream any cell consumes, at the deepest history any cell wants.
    # One fetch serves the whole matrix.
    tf_needs = {}
    for e, x, mode in cells:
        eng = tfx.build(e, x, mode)
        for role, tf in eng.timeframes.items():
            tf_needs[tf] = max(tf_needs.get(tf, 0), eng.history[role])
    print(f'streams needed: '
          + ', '.join(f'{tf} x{n}' for tf, n in sorted(tf_needs.items())))
    paths = fetch_streams(symbols, tf_needs, base_start, end,
                          force=args.refetch)

    work = [(e, x, m, symbols, paths, start_dt.isoformat(), args.capital,
             args.alloc_dollars, args.slippage_bps, args.rth)
            for e, x, m in cells]
    workers = args.workers or (os.cpu_count() or 1)
    workers = max(1, min(workers, len(work)))

    print(f'replaying {len(work)} cells on {workers} worker(s)...')
    rows = []
    if workers == 1:
        for i, w in enumerate(work, 1):
            print(f'  [{i}/{len(work)}] '
                  f'{tfx.engine_name(w[0], w[1], w[2])}')
            rows.append(_worker(w))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_worker, w): w for w in work}
            done = 0
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                print(f'  [{done}/{len(work)}] {r.get("name")}'
                      + (f'  ERROR {r["error"]}' if 'error' in r else
                         f'  {r["n_trades"]} trades  '
                         f'{r["total_pnl"]:+.2f}'))
                rows.append(r)

    order = {tfx.engine_name(e, x, m): i for i, (e, x, m) in enumerate(cells)}
    rows.sort(key=lambda r: order.get(r.get('name'), 999))

    print_tables(rows, {'symbols': symbols, 'start': args.start,
                        'end': args.end or 'now', 'capital': args.capital,
                        'budget': args.alloc_dollars,
                        'slippage_bps': args.slippage_bps,
                        'rth': args.rth})

    # The 10Min/scaled cell is arithmetically the control (scaling by 10/10
    # is the identity). If those two rows ever disagree, something in this
    # file is wrong and no other row should be believed.
    c = next((r for r in rows if r.get('is_control')), None)
    s10 = next((r for r in rows if r.get('name')
                == 'ripster_tf_e10m_x10m_scaled'), None)
    if c and s10:
        same = (c['n_trades'] == s10['n_trades']
                and abs(c['total_pnl'] - s10['total_pnl']) < 0.01)
        print(f"\nCONSISTENCY: 10Min/scaled vs control -> "
              f"{'identical, as it must be' if same else 'DIFFERENT -- BUG'}")

    out = args.out or os.path.join(
        cm.DATA_DIR,
        f'tf_matrix_rth-{args.rth}_{datetime.now():%Y%m%d_%H%M%S}.csv')
    flat = [{k: v for k, v in r.items() if k != 'exits_by_reason'}
            for r in rows if 'error' not in r]
    if flat:
        with open(out, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)
        print(f'\nmatrix written: {out}')


if __name__ == '__main__':
    main()
