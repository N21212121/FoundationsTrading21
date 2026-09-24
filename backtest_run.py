"""
backtest_run.py - Foundations Trading

The runner. Wires the three pieces together, exactly the shape promised:
first argument a basket, second an engine.

  python backtest_run.py --basket semis_intraday --start 2026-05-01
  python backtest_run.py --symbols NVDA,AMD --engine ripster_ema_cloud \
      --start 2026-05-01 --end 2026-06-30 --alloc-pct 1 --capital 80000

Fetches history through the same alpaca_manager the live loop uses, replays
it through the registered engine, prints the report, writes trades to CSV
in the data dir. Touches nothing in the live system.
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta

import config_manager as cm
from alpaca_manager import AlpacaManager
from engines_bootstrap import registry, resolver
import backtester
import baskets as bk

# Calendar padding before --start, so the engine's warmup window is full on
# day one. This MUST be derived from the engine's own timeframe and history,
# not hardcoded: a fixed 16 days is ~429 ten-minute bars (fine for Ripster's
# 300) but only ~11 DAILY bars, which starves a daily engine that wants 1,200
# and leaves it permanently in warmup, silently returning zero trades.
WARMUP_PAD_DAYS = 16        # legacy default; used only when no engine is known
BIG_LIMIT = 1_000_000       # get_bars_multi trims to limit; don't trim history

# Sessions that one bar of each timeframe spans. Intraday bars are a fraction
# of a 390-minute regular session; daily and slower are whole sessions.
def _sessions_per_bar(tf, default=10 / 390):
    """Sessions each bar spans, derived from minutes rather than enumerated.

    Intraday bars are a fraction of a 390-minute session; daily and slower
    are whole sessions."""
    import halflife as hlf
    m = hlf.tf_minutes(tf)
    if m is None:
        return default
    if m >= 1440:
        return {1440: 1.0, 10080: 5.0, 43200: 21.0}.get(m, m / 1440)
    return m / 390


class _SessionsPerBar(dict):
    """Dict-shaped for existing call sites; computes anything it is asked for."""
    def __missing__(self, tf):
        return _sessions_per_bar(tf)

    def get(self, tf, default=None):
        v = _sessions_per_bar(tf, default)
        return v if v is not None else default


_SESSIONS_PER_BAR = _SessionsPerBar()
_CAL_PER_SESSION = 1.45     # 7/5 weekends, plus slack for holidays
_PAD_FLOOR_DAYS = 10


def warmup_pad_days(timeframe, bars):
    """Calendar days of history needed to deliver `bars` closed bars of
    `timeframe`. Fails LOUD on an unknown timeframe rather than guessing."""
    spb = _SESSIONS_PER_BAR[timeframe]
    sessions = bars * spb
    return int(sessions * _CAL_PER_SESSION) + _PAD_FLOOR_DAYS


def warmup_start(base_start, timeframe, bars):
    """The date to fetch from, so `base_start` opens with a full window."""
    return base_start - timedelta(days=warmup_pad_days(timeframe, bars))


def _parse_date(s):
    return datetime.strptime(s, '%Y-%m-%d')


def _fetch(alpaca, symbols, tf, start, end, progress=None):
    got = alpaca.get_bars_multi(symbols, timeframe=tf, limit=BIG_LIMIT,
                                start=start, progress=progress)
    if end is None:
        return got
    out = {}
    for sym, bars in got.items():
        out[sym] = [b for b in bars
                    if datetime.fromisoformat(b['time']).date() <= end.date()]
    return out


def main():
    ap = argparse.ArgumentParser(description='Foundations Trading backtester')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--basket', help='basket name from baskets.json')
    src.add_argument('--symbols', help='comma-separated ad-hoc list')
    ap.add_argument('--engine', default=None,
                    help='registered engine name (default: basket engine, '
                         'else global default)')
    ap.add_argument('--start', required=True, help='YYYY-MM-DD')
    ap.add_argument('--end', default=None, help='YYYY-MM-DD (default: today)')
    ap.add_argument('--capital', type=float, default=100000.0)
    ap.add_argument('--alloc-pct', type=float, default=1.0,
                    help='budget per position as %% of capital')
    ap.add_argument('--alloc-dollars', type=float, default=None,
                    help='budget per position in dollars (overrides '
                         '--alloc-pct)')
    ap.add_argument('--slippage-bps', type=float, default=2.0)
    ap.add_argument('--out', default=None,
                    help='trades CSV path (default: data dir)')
    args = ap.parse_args()

    # ── universe: basket first, ad-hoc second ──
    basket_engine = None
    if args.basket:
        b = bk.get_basket(args.basket)
        symbols = b['symbols']
        basket_engine = b.get('engine')
        label = f'basket {args.basket}'
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(',')
                   if s.strip()]
        label = 'ad-hoc symbols'
    if not symbols:
        sys.exit('no symbols to test')

    # ── engine: CLI flag > basket's engine > global default ──
    name = args.engine or basket_engine or resolver.default_name
    try:
        engine = registry.get(name)
    except KeyError as e:
        sys.exit(f'{e}; registered: {registry.names()}')

    # ── history ──
    cfg = cm.load_config()
    alpaca = AlpacaManager()
    r = alpaca.connect(cfg.get('alpaca_key', ''), cfg.get('alpaca_secret', ''),
                       paper=cfg.get('paper_trading', True))
    if r.get('status') != 'ok':
        sys.exit(f"alpaca: {r.get('message')}")

    base_start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else None

    tf_primary = engine.timeframes['primary']
    n_primary = engine.history.get('primary', 300)
    start = warmup_start(base_start, tf_primary, n_primary)
    print(f'fetching {tf_primary} history for {len(symbols)} symbol(s) '
          f'from {start:%Y-%m-%d} ({n_primary} bars of warmup)...')
    bars = _fetch(alpaca, symbols, tf_primary, start, end)

    macro = {}
    if 'macro' in engine.timeframes:
        tf_macro = engine.timeframes['macro']
        n_macro = engine.history.get('macro', 200)
        m_start = warmup_start(base_start, tf_macro, n_macro)
        print(f'fetching {tf_macro} history from {m_start:%Y-%m-%d}...')
        macro = _fetch(alpaca, symbols, tf_macro, m_start, end)

    missing = [s for s in symbols if not bars.get(s)]
    if missing:
        print(f'WARNING: no primary bars for {missing}; skipped')

    # ── replay ──
    print(f'replaying through engine {engine.name}...')
    result = backtester.run_basket(engine, bars, macro,
                                   capital=args.capital,
                                   alloc_pct=args.alloc_pct,
                                   alloc_dollars=args.alloc_dollars,
                                   slippage_bps=args.slippage_bps)

    sizing = (f'${args.alloc_dollars:,.0f}/name'
              if args.alloc_dollars is not None
              else f'{args.alloc_pct}% of ${args.capital:,.0f}')
    title = (f'{label} | engine {engine.name} | {args.start} -> '
             f'{args.end or "now"} | alloc {sizing} | '
             f'slip {args.slippage_bps}bps')
    print()
    print(backtester.format_report(result, title))

    # ── trades CSV ──
    out = args.out or os.path.join(
        cm.DATA_DIR,
        f'backtest_{(args.basket or "adhoc")}_{engine.name}_'
        f'{datetime.now():%Y%m%d_%H%M%S}.csv')
    trades = result['trades']
    if trades:
        with open(out, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(sorted(trades, key=lambda t: t['entry_time']))
        print(f'\ntrades written: {out}')
    else:
        print('\nno trades generated; nothing written')


if __name__ == '__main__':
    main()
