"""
backtester.py - Foundations Trading

Event-driven replay. Feeds historical bars through the REAL engine, one bar
close at a time, exactly the way the live loop does. No vectorized shortcut,
no reimplemented rules. The engine under test is the engine that trades.

FIDELITY RULES (each one mirrors a specific live behavior):

  Window      The engine sees the trailing N primary bars per evaluation,
              N = engine.history['primary'], same as the live cache hands it.
  Clock       now_et is injected as the bar's CLOSE time in ET. Decisions on
              closed bars only, like the wall-aligned live loop.
  Fills       Decisions happen at a bar close; orders fill at the NEXT bar's
              open, +/- slippage. The engine never trades its own signal bar.
  One position per ticker, no pyramiding (mirrors _do_entry's engine guard).
  Volume gate blocks SHORT entries only (live: shorts are puts-only and
              volume gates options; longs fall back to shares).
  Structural stop -> no re-entry same day, either direction (mirrors
              mark_stop_out marking every sleeve the exit sold, and
              _do_entry refusing on any marked sleeve).
  End of data Open positions are force-closed at the last close and tagged,
              so nothing silently escapes the P/L.

WHAT THIS DOES NOT MODEL, ON PURPOSE:
  The options overlay. Live longs are calls+shares, shorts are puts. This
  simulates the directional signal on the UNDERLYING (long/short shares of
  equal notional). It measures whether the engine's entries and exits have
  edge. It does not measure theta, IV, or spread costs on the options leg.
  Read results as signal quality, not as dollar-accurate live P/L.

Sizing mirrors the session-pinned budget: every position gets a pinned
dollar budget. No compounding inside a run; that matches _budget_for
pinning to day-start cash and keeps per-trade returns comparable across
the run. The budget can be set three ways, resolved per symbol:
  budget_by_symbol[sym]  (portfolio modes: even split, Sharpe weights)
  > alloc_dollars        (flat $ per name)
  > capital * alloc_pct  (flat % of capital per name, the original)
Even split (capital/N per name) is the one sizing where simultaneous full
deployment across the basket cannot exceed capital, so its pooled equity
curve is internally consistent without a shared cash constraint.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

ET = ZoneInfo('America/New_York')

# The SAME object as halflife's, not a copy. dict(...) would flatten it back
# into a plain dict and lose the self-extending lookup, reintroducing the
# KeyError this was meant to remove.
import halflife as _hlf
TF_MINUTES = _hlf.TF_MINUTES


def _t(bar):
    return datetime.fromisoformat(bar['time'])


# ─── SINGLE-SYMBOL REPLAY ─────────────────────────────────────────────────────

PROGRESS_EVERY_BARS = 25


def run_symbol(engine, symbol, bars_primary, bars_macro=None,
               budget_dollars=1000.0, slippage_bps=2.0, min_window=None,
               progress=None, compound=False):
    """Replay one symbol through the engine.

    compound=False: every entry sizes off the pinned budget (original).
    compound=True: PER-SLEEVE compounding — the sleeve starts at
    budget_dollars and each realized P/L (net of slippage) rolls into the
    next entry's sizing. A sleeve that bleeds below one share stops
    entering: busted is busted. NOTE the deliberate deviation from live:
    _budget_for compounds the SHARED pot at day-start, so live lets
    winners fund losers' budgets; sleeve-level is the version that stays
    per-symbol parallel. State it when reading results, don't discover it.

    Returns {'trades': [...], 'daily_marks': {date_iso: unrealized $ at day
    close}, 'evals': int}. Trades carry entry/exit time+price, direction,
    pnl_dollars, ret_pct, exit_reason, bars_held.

    progress(done_bars, total_bars, 'bars'), if given, fires every
    PROGRESS_EVERY_BARS. A single-symbol backtest otherwise reports NOTHING
    until it finishes, because symbol-level progress has nothing to count.
    Only the serial path uses this: a closure cannot be pickled to a pool.
    """
    tf = engine.timeframes['primary']
    tf_min = TF_MINUTES[tf]
    window = engine.history.get('primary', 300)
    macro_tf_min = None
    if bars_macro and 'macro' in engine.timeframes:
        macro_tf_min = TF_MINUTES[engine.timeframes['macro']]

    slip = slippage_bps / 10000.0
    sleeve = float(budget_dollars)     # per-sleeve equity (compound mode)
    trades = []
    daily_marks = {}
    held = None                # None | 'long' | 'short'
    entry_px = entry_t = None
    entry_bar_i = None
    qty = 0
    blocked_day = None         # date with a structural stop: no re-entry
    macro_ptr = 0              # advancing pointer into bars_macro
    evals = 0

    # FIDELITY: live evaluates EVERY bar close and lets the engine gate its
    # own warmup (it returns NONE cheaply on thin windows). Starting the
    # replay at full window depth would skip bars the live loop evaluates,
    # so we start at the second bar and let the engine decide. min_window
    # exists only to skip known-dead evals for speed; it must never exceed
    # the engine's own warmup or the replay diverges from live.
    start_i = max(1, (min_window or 2) - 1)
    n = len(bars_primary)
    n_evals = max(1, n - start_i)

    def _fill(i, side_is_buy):
        """Next-bar-open fill with slippage. Returns (price, time) or None
        if there is no next bar."""
        if i + 1 >= n:
            return None
        px = bars_primary[i + 1]['open']
        px *= (1 + slip) if side_is_buy else (1 - slip)
        return px, _t(bars_primary[i + 1])

    def _close_trade(i, reason, force_px=None, force_t=None):
        nonlocal held, entry_px, entry_t, qty, entry_bar_i, sleeve
        if force_px is not None:
            xpx, xt = force_px, force_t
        else:
            f = _fill(i, side_is_buy=(held == 'short'))
            if f is None:      # no next bar: close at last close
                xpx = bars_primary[i]['close'] * \
                    ((1 + slip) if held == 'short' else (1 - slip))
                xt = _t(bars_primary[i])
                reason = reason + '+end_of_data'
            else:
                xpx, xt = f
        sign = 1 if held == 'long' else -1
        pnl = (xpx - entry_px) * qty * sign
        sleeve += pnl
        trades.append({
            'symbol': symbol, 'direction': held,
            'entry_time': entry_t.isoformat(), 'entry_price': round(entry_px, 4),
            'exit_time': xt.isoformat(), 'exit_price': round(xpx, 4),
            'qty': qty, 'pnl_dollars': round(pnl, 2),
            'notional': round(entry_px * qty, 2),
            'ret_pct': round(pnl / (entry_px * qty) * 100, 4) if qty else 0.0,
            'exit_reason': reason, 'bars_held': i - entry_bar_i,
            'exit_date': xt.astimezone(ET).date().isoformat(),
        })
        held = None
        entry_px = entry_t = None
        entry_bar_i = None
        qty = 0

    for i in range(start_i, n):
        if progress and (i - start_i) % PROGRESS_EVERY_BARS == 0:
            progress(i - start_i, n_evals, 'bars')
        bar = bars_primary[i]
        bar_close = _t(bar) + timedelta(minutes=tf_min)
        now_et = bar_close.astimezone(ET)

        w0 = max(0, i - window + 1)
        win_primary = bars_primary[w0:i + 1]

        win_macro = None
        if macro_tf_min is not None:
            # closed macro bars only: bar start + width <= now
            while (macro_ptr < len(bars_macro)
                   and _t(bars_macro[macro_ptr])
                   + timedelta(minutes=macro_tf_min) <= bar_close):
                macro_ptr += 1
            m0 = max(0, macro_ptr - engine.history.get('macro', 200))
            win_macro = bars_macro[m0:macro_ptr] or None

        # Hand the engine the entry timestamp when a position is open. A
        # stateless engine cannot implement a time stop without it, and the
        # OU engine's ONLY unconditional stop is a time stop.
        pos_ctx = ({'opened_at': entry_t.isoformat()}
                   if (held and entry_t is not None) else None)
        d = engine.evaluate(symbol, {'primary': win_primary,
                                     'macro': win_macro},
                            position_direction=held, now_et=now_et,
                            position=pos_ctx)
        evals += 1
        a = d['action']
        today = now_et.date()

        # ── exits first, never gated (mirrors _evaluate_ticker) ──
        if held and (a.startswith('EXIT_LONG') or a.startswith('EXIT_SHORT')):
            ekind = d.get('exit_kind') or 'ride_end'
            # Keep the engine's own explanation in exits_by_reason so a
            # backtest report distinguishes a time stop from a circuit
            # breaker from a profit exit. Both are 'structural' to the
            # router; they are very different to you.
            tag = d.get('exit_tag')
            _close_trade(i, f'{ekind}|{tag}' if tag else ekind)
            if ekind == 'structural':
                blocked_day = today      # stop-out: no same-day re-entry

        # ── entries (mirrors the live guards) ──
        if held is None and (a.endswith('ENTER_LONG')
                             or a.endswith('ENTER_SHORT')):
            direction = 'long' if a.endswith('ENTER_LONG') else 'short'
            if blocked_day == today:
                pass                     # same-day rebuy blocked (stop-out)
            elif direction == 'short' and not d.get('volume_ok', True):
                pass                     # shorts need options; volume gate
            else:
                f = _fill(i, side_is_buy=(direction == 'long'))
                if f is not None:
                    px, t = f
                    base = sleeve if compound else budget_dollars
                    q = int(base // px) if base > 0 else 0
                    if q >= 1:
                        held = direction
                        entry_px, entry_t = px, t
                        entry_bar_i = i
                        qty = q

        # ── daily unrealized mark at each session's last bar ──
        is_last_of_day = (i + 1 >= n
                          or _t(bars_primary[i + 1]).astimezone(ET).date()
                          != today)
        if is_last_of_day:
            mark = 0.0
            if held:
                sign = 1 if held == 'long' else -1
                mark = (bar['close'] - entry_px) * qty * sign
            daily_marks[today.isoformat()] = round(mark, 2)

    if held:
        last = bars_primary[-1]
        xpx = last['close'] * ((1 + slip) if held == 'short' else (1 - slip))
        _close_trade(n - 1, 'end_of_data', force_px=xpx, force_t=_t(last))

    if progress:
        progress(n_evals, n_evals, 'bars')
    return {'trades': trades, 'daily_marks': daily_marks, 'evals': evals}


# ─── PORTFOLIO AGGREGATION + METRICS ──────────────────────────────────────────

def _resolve_budget(sym, capital, alloc_pct, alloc_dollars, budget_by_symbol):
    """One symbol's pinned budget. Precedence: per-symbol dict, then flat
    dollars, then percent of capital."""
    if budget_by_symbol and sym in budget_by_symbol:
        return float(budget_by_symbol[sym])
    if alloc_dollars is not None:
        return float(alloc_dollars)
    return capital * alloc_pct / 100.0


def run_basket(engine, bars_by_symbol, macro_by_symbol=None,
               capital=100000.0, alloc_pct=1.0, slippage_bps=2.0,
               alloc_dollars=None, budget_by_symbol=None, compound=False):
    """Replay every symbol independently on a pinned per-position budget,
    then aggregate. Budget precedence per symbol: budget_by_symbol >
    alloc_dollars > alloc_pct of capital. Independent budgets mirror the
    live session-pinned sizing; the sim does not model a shared cash
    constraint, so at very high total allocation, live would refuse
    entries the sim takes. Even split (capital/N) is the sizing where
    that cannot happen."""
    macro_by_symbol = macro_by_symbol or {}
    all_trades = []
    marks_by_day = {}
    total_evals = 0

    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        budget = _resolve_budget(sym, capital, alloc_pct, alloc_dollars,
                                 budget_by_symbol)
        r = run_symbol(engine, sym, bars, macro_by_symbol.get(sym),
                       budget_dollars=budget, slippage_bps=slippage_bps,
                       compound=compound)
        all_trades.extend(r['trades'])
        total_evals += r['evals']
        for day, m in r['daily_marks'].items():
            marks_by_day[day] = marks_by_day.get(day, 0.0) + m

    equity = _equity_curve(all_trades, marks_by_day, capital)
    # slippage_bps MUST flow through: fills above were simulated at this
    # rate, so metrics computed at the default 0 would report gross == net
    # and understate breakeven slippage by exactly what you paid. The
    # parallel path already passes it; the two reports must agree.
    m = compute_metrics(all_trades, equity, capital,
                        slippage_bps=slippage_bps)
    m['evals'] = total_evals
    m['symbols'] = len(bars_by_symbol)
    return {'trades': all_trades, 'equity_curve': equity, 'metrics': m}


# ─── PARALLEL REPLAY ──────────────────────────────────────────────────────────
#
# The replay is embarrassingly parallel: each symbol is independent, no shared
# state. It is also CPU-bound pandas/loop work, so the GIL makes THREADS
# pointless here. Use PROCESSES. Fetch stays serial and shared upstream (I/O,
# rate-limited); only the compute fans out.
#
# The worker is a module-level function so it pickles cleanly across the
# process boundary. It receives the engine NAME, not the engine object, and
# rebuilds it from the registry inside the worker. This is only safe because
# engines are stateless by contract - the same reason the live sweep can share
# one instance across 1,000 names. It imports the registry lazily so a worker
# never drags the Flask app or a broker connection across the fork.

def _replay_one(args):
    """Worker body. args = (engine_name, symbol, bars, macro, budget,
    slip, compound). Returns (symbol, result_dict) or (symbol, {'error':
    str}). Never raises out: one bad symbol must not kill the pool."""
    engine_name, symbol, bars, macro, budget, slip, compound = args
    try:
        from engines_bootstrap import registry
        engine = registry.get(engine_name)
        r = run_symbol(engine, symbol, bars, macro,
                       budget_dollars=budget, slippage_bps=slip,
                       compound=compound)
        return symbol, r
    except Exception as e:
        return symbol, {'error': str(e)}


def run_basket_parallel(engine_name, bars_by_symbol, macro_by_symbol=None,
                        capital=100000.0, alloc_pct=1.0, slippage_bps=2.0,
                        max_workers=None, progress=None,
                        alloc_dollars=None, budget_by_symbol=None,
                        compound=False):
    """Same contract as run_basket, but replays symbols across a process pool.

    Takes engine_name (str), not an engine object, because the object has to
    survive pickling to the workers; the name does, and each worker rebuilds
    from the registry. The aggregation math is identical to run_basket, so
    results match the serial path exactly (verified in tests).

    Budget precedence per symbol: budget_by_symbol > alloc_dollars >
    alloc_pct of capital. A symbol with a zero budget is legal but wasted
    compute (every entry sizes to zero shares); callers running weighted
    portfolios should drop zero-weight names from bars_by_symbol instead.

    max_workers defaults to os.cpu_count(). progress, if given, is called
    progress(done, total) after each symbol completes - the job runner uses
    it to write partial status so a crash mid-run doesn't lose everything.

    CANCEL PROTOCOL: progress may return the string 'stop'. backtester does
    not know about jobs or Flask state (importing app would be circular), so
    the caller's callback is the only channel. On 'stop' the dispatch loop
    stops handing out work and the pool is shut down WITHOUT waiting on
    in-flight futures - a stop that still blocked on a 400-name batch would
    not be a stop. Whatever completed before the signal is returned as a
    partial result; the JOB RUNNER decides to discard it. A PAUSE is the
    caller's problem: the callback simply blocks (sleeps) until resumed, so
    from here a paused job is indistinguishable from a slow one. That keeps
    the freeze at the dispatch boundary, which is the only place a process
    pool can honestly freeze.
    """
    macro_by_symbol = macro_by_symbol or {}
    def _b(sym):
        return _resolve_budget(sym, capital, alloc_pct, alloc_dollars,
                               budget_by_symbol)
    # engine_name may be a single name (one engine for the whole basket) or a
    # {symbol: engine_name} map. The map is how a resolver-assigned universe
    # runs: NVDA on ou_reversion_30m, AAPL on ou_reversion_1d, in one sweep.
    # The work tuple always carried the engine per row; only the caller had to
    # catch up.
    if isinstance(engine_name, dict):
        work = [(engine_name[sym], sym, bars, macro_by_symbol.get(sym),
                 _b(sym), slippage_bps, compound)
                for sym, bars in bars_by_symbol.items()
                if bars and sym in engine_name]
    else:
        work = [(engine_name, sym, bars, macro_by_symbol.get(sym),
                 _b(sym), slippage_bps, compound)
                for sym, bars in bars_by_symbol.items() if bars]
    total = len(work)

    all_trades = []
    marks_by_day = {}
    total_evals = 0
    errors = {}
    per_symbol = {}          # symbol -> its own metrics, for the screener
    done = 0

    workers = max_workers or os.cpu_count() or 1
    # Small universes: the process spin-up cost outweighs the win. Fall back
    # to the serial path under a threshold so a 3-name backtest stays snappy.
    if total <= 2 or workers <= 1:
        pairs = _serial_pairs(work, progress)
    else:
        pairs = _pool_pairs(work, workers, progress)

    stopped = False
    for item in pairs:
        # A pairs generator yields (symbol, result) normally, or the bare
        # sentinel 'STOP' once, last, when the callback asked to cancel.
        if item == 'STOP':
            stopped = True
            break
        sym, r = item
        if 'error' in r:
            errors[sym] = r['error']
            continue
        all_trades.extend(r['trades'])
        total_evals += r['evals']
        for day, mk in r['daily_marks'].items():
            marks_by_day[day] = marks_by_day.get(day, 0.0) + mk
        # per-symbol metrics for ranking: build a tiny equity curve per name
        s_eq = _equity_curve(r['trades'], r['daily_marks'], capital)
        per_symbol[sym] = compute_metrics(r['trades'], s_eq, capital,
                                          slippage_bps=slippage_bps)
        done += 1

    equity = _equity_curve(all_trades, marks_by_day, capital)
    m = compute_metrics(all_trades, equity, capital, slippage_bps=slippage_bps)
    m['evals'] = total_evals
    m['symbols'] = len(bars_by_symbol)
    m['errors'] = errors
    return {'trades': all_trades, 'equity_curve': equity, 'metrics': m,
            'per_symbol': per_symbol, 'stopped': stopped}


def _serial_pairs(work, progress):
    """Yield (symbol, result) in order.

    Bar-level progress ONLY when there is exactly one symbol. With two symbols
    the per-symbol bar counter restarts at zero, so a shared bar gauge would
    run BACKWARDS halfway through -- worse than no gauge at all. With more
    than one symbol we report symbols, which is monotone.

    A one-symbol backtest has exactly one symbol-level event and it lands
    after the run is over; from the UI that is indistinguishable from a hang.
    That is the case this exists for."""
    from engines_bootstrap import registry
    solo = len(work) == 1
    done = 0
    for engine_name, symbol, bars, macro, budget, slip, compound in work:
        try:
            engine = registry.get(engine_name)
            r = run_symbol(engine, symbol, bars, macro,
                           budget_dollars=budget, slippage_bps=slip,
                           compound=compound,
                           progress=(progress if solo else None))
            yield symbol, r
        except Exception as e:
            yield symbol, {'error': str(e)}
        done += 1
        if progress and not solo:
            # progress may block here (pause) or return 'stop' (cancel).
            if progress(done, len(work), 'symbols') == 'stop':
                yield 'STOP'
                return


def _pool_pairs(work, workers, progress):
    """Yield (symbol, result) as workers finish. Isolated so the serial
    fallback path above stays free of pool machinery."""
    total = len(work)
    done = 0
    ex = ProcessPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(_replay_one, w): w[1] for w in work}
        for fut in as_completed(futs):
            done += 1
            yield fut.result()
            # After yielding the completed result, poll the callback. It may
            # block (pause) or return 'stop'. On stop, abandon the rest:
            # cancel_futures kills what has not started, and we do NOT wait
            # on the handful already running - they die with the pool.
            if progress and progress(done, total) == 'stop':
                yield 'STOP'
                return
    finally:
        # cancel_futures is 3.9+; the app targets 3.13. On the stop path the
        # pending futures are dropped instead of drained.
        ex.shutdown(wait=False, cancel_futures=True)


def _equity_curve(trades, marks_by_day, capital):
    """[(date_iso, equity)] = capital + realized-through-day + that day's
    open-position marks."""
    realized_by_day = {}
    for t in trades:
        d = t['exit_date']
        realized_by_day[d] = realized_by_day.get(d, 0.0) + t['pnl_dollars']
    days = sorted(set(marks_by_day) | set(realized_by_day))
    curve, cum = [], 0.0
    for d in days:
        cum += realized_by_day.get(d, 0.0)
        curve.append((d, round(capital + cum + marks_by_day.get(d, 0.0), 2)))
    return curve


def compute_metrics(trades, equity_curve, capital, slippage_bps=0.0,
                    trading_days_per_year=252):
    n = len(trades)
    wins = [t for t in trades if t['pnl_dollars'] > 0]
    losses = [t for t in trades if t['pnl_dollars'] <= 0]
    gross_win = sum(t['pnl_dollars'] for t in wins)
    gross_loss = -sum(t['pnl_dollars'] for t in losses)
    total_pnl = gross_win - gross_loss

    # daily returns off the equity curve
    rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        rets.append((equity_curve[i][1] - prev) / prev if prev else 0.0)

    def _mean(x):
        return sum(x) / len(x) if x else 0.0

    def _std(x):
        if len(x) < 2:
            return 0.0
        mu = _mean(x)
        return (sum((v - mu) ** 2 for v in x) / (len(x) - 1)) ** 0.5

    mu, sd = _mean(rets), _std(rets)
    downside = [r for r in rets if r < 0]
    dsd = _std(downside) if len(downside) > 1 else 0.0
    ann = 252 ** 0.5
    sharpe = (mu / sd * ann) if sd > 0 else 0.0
    sortino = (mu / dsd * ann) if dsd > 0 else 0.0

    peak, max_dd = -float('inf'), 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    by_reason = {}
    for t in trades:
        r = t['exit_reason']
        s = by_reason.setdefault(r, {'n': 0, 'pnl': 0.0})
        s['n'] += 1
        s['pnl'] = round(s['pnl'] + t['pnl_dollars'], 2)

    m = {
        'n_trades': n,
        'win_rate_pct': round(len(wins) / n * 100, 2) if n else 0.0,
        'avg_win': round(_mean([t['pnl_dollars'] for t in wins]), 2),
        'avg_loss': round(_mean([t['pnl_dollars'] for t in losses]), 2),
        'profit_factor': round(gross_win / gross_loss, 3)
                         if gross_loss > 0 else float('inf'),
        'expectancy': round(total_pnl / n, 2) if n else 0.0,
        'total_pnl': round(total_pnl, 2),
        'return_on_capital_pct': round(total_pnl / capital * 100, 3),
        'max_drawdown_pct': round(max_dd * 100, 3),
        'sharpe': round(sharpe, 3),
        'sortino': round(sortino, 3),
        'trading_days': len(equity_curve),
        'exits_by_reason': by_reason,
    }

    # ── COST ECONOMICS ────────────────────────────────────────────────────────
    # The number that decides whether a fast sleeve exists at all.
    #
    # P&L is linear in slippage: every round trip pays it twice, on notional.
    #     pnl(s) = pnl(0) - 2 * s * total_notional
    # So gross = pnl(s) + 2*s*N, and the slippage at which the strategy breaks
    # even is  s* = gross / (2N).
    #
    # A 30-minute OU engine holding ~13 bars turns over ~13x more often than a
    # daily one holding ~13 bars, while harvesting a residual of a fraction of
    # a percent. Turnover multiplies spread. An engine can pass every
    # statistical test in this system and still lose money net of costs, and
    # THIS is where you see it: if breakeven_slippage_bps is below the spread
    # you actually pay, the sleeve does not exist.
    notional = sum(abs(t.get('notional') or 0.0) for t in trades)
    s = (slippage_bps or 0.0) / 10000.0
    m['total_notional'] = round(notional, 2)
    m['slippage_cost'] = round(2.0 * s * notional, 2)
    m['gross_pnl'] = round(m['total_pnl'] + m['slippage_cost'], 2)
    m['breakeven_slippage_bps'] = (round(m['gross_pnl'] / (2.0 * notional)
                                         * 10000.0, 2) if notional > 0 else None)
    m['slippage_bps_assumed'] = slippage_bps
    # Turnover: notional traded per dollar of capital, annualised.
    days = max(1, m['trading_days'])
    m['turnover_annual'] = (round(notional / capital
                                  * (trading_days_per_year / days), 2)
                            if capital > 0 else None)
    m['avg_bars_held'] = (round(sum(t.get('bars_held', 0) for t in trades)
                                / len(trades), 1) if trades else None)
    return m


def format_report(result, title=''):
    m = result['metrics']
    lines = []
    if title:
        lines.append(title)
        lines.append('=' * len(title))
    pf = m['profit_factor']
    lines += [
        f"symbols {m.get('symbols', '?')}   evals {m.get('evals', '?')}   "
        f"trading days {m['trading_days']}",
        f"trades {m['n_trades']}   win rate {m['win_rate_pct']}%   "
        f"profit factor {pf if pf != float('inf') else 'inf'}",
        f"avg win ${m['avg_win']}   avg loss ${m['avg_loss']}   "
        f"expectancy ${m['expectancy']}/trade",
        f"total P/L ${m['total_pnl']}   "
        f"return on capital {m['return_on_capital_pct']}%",
        f"max drawdown {m['max_drawdown_pct']}%   "
        f"sharpe {m['sharpe']}   sortino {m['sortino']}",
        'exits: ' + ', '.join(f"{k} n={v['n']} ${v['pnl']}"
                              for k, v in m['exits_by_reason'].items()),
        '',
        f"slippage assumed {m.get('slippage_bps_assumed')}bps   "
        f"cost ${m.get('slippage_cost')}   gross P/L ${m.get('gross_pnl')}",
        f"BREAKEVEN SLIPPAGE {m.get('breakeven_slippage_bps')} bps   "
        f"turnover {m.get('turnover_annual')}x/yr   "
        f"avg hold {m.get('avg_bars_held')} bars",
        'If breakeven slippage is below the spread you actually pay, this',
        'sleeve does not exist. Fast engines die here, not in the statistics.',
        '',
        'NOTE: underlying-only simulation. Live longs add a call sleeve and',
        'live shorts trade puts; options costs/leverage are not modeled.',
        'Read this as signal quality, not dollar-accurate live P/L.',
    ]
    return '\n'.join(lines)
