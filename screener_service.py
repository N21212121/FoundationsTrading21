"""
screener_service.py — Foundations Trading

Runs the screener on a loop and holds its latest result.

SEPARATE THREAD, SEPARATE FAILURE DOMAIN
  This does NOT hook into bar_loop. The trading loop places orders; a screen
  is a read-only convenience. Wiring them together would mean a screener bug
  could stall or crash the path that manages live positions. It runs on its
  own daemon thread and every iteration is wrapped, so the worst case is a
  stale board.

BATCHED FETCHES
  Bars come from get_bars_multi, one request per timeframe for the whole
  watchlist, not one per ticker. Forty tickers on a serial fetch would blow
  the rate limit and take longer than the bar it is trying to beat.

ALERTS ARE A QUEUE, NOT A PUSH
  The service appends to a bounded deque; the UI polls and drains. That keeps
  the service ignorant of how alerts get displayed, and means an alert raised
  while the window is closed is still there when it opens.
"""

import threading
import time
from collections import deque
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

import config_manager as cm
import screener as SC
import watchlist as WL

ET = ZoneInfo('America/New_York')


def _bar_time(b):
    t = b.get('time') or b.get('t') or b.get('timestamp')
    if hasattr(t, 'isoformat'):
        return t
    try:
        return datetime.fromisoformat(str(t).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def drop_forming(bars, tf, now_et=None):
    """Remove the in-progress bar.

    Alpaca returns the partial current-period bar as soon as it has trades.
    A bar is closed only once its start plus its width has passed. app.py
    does this centrally for the engine feed; the screener runs on its own
    thread with its own fetches, so it has to do the same or it scores
    candles that are still forming.
    """
    if not bars:
        return bars
    import halflife as hlf
    width = hlf.TF_MINUTES.get(tf)
    if not width:
        return bars
    now_et = now_et or datetime.now(ET)
    last = _bar_time(bars[-1])
    if last is None:
        return bars
    if last.tzinfo is None:
        last = last.replace(tzinfo=ET)
    if last.astimezone(ET) + timedelta(minutes=width) > now_et:
        return bars[:-1]
    return bars


# Scan every minute, but DECIDE on closed 6-minute candles.
#
# Those are two different clocks on purpose. Polling at 60s means price and
# level proximity stay current; dropping the forming bar before scoring means
# the cloud state, the 34/50 gate and the 5/12 cross are all read off a
# candle that has actually closed. Without the drop, a bar with 60 seconds of
# data gets evaluated as finished and a "fresh cross" can appear and vanish
# within the same candle.
INTERVAL_SECONDS = 60
INTRA_TF = '6Min'
DAILY_BARS = 120          # enough for prior month + ATR(14)
INTRA_BARS = 260          # 6-min bars: ~4.3 sessions, enough for EMA50 + room
HOURLY_BARS = 200         # enough for the 1H 34/50

_state = {
    'running': False,
    'last_scan': None,
    'last_error': None,
    'result': None,
    'scans': 0,
}
_alerts = deque(maxlen=200)
# Drained alerts are gone from the queue but the chart still needs to know
# WHEN a ticker fired, so a separate bounded history is kept per ticker.
_alert_history = {}
_gate = SC.AlertGate(cooldown_minutes=30)
_lock = threading.Lock()
def _load_weights():
    try:
        cfg = cm.load_config()
        saved = cfg.get('screener_weights') or {}
        if saved:
            return SC.normalize_weights(saved)
    except Exception:
        pass
    return dict(SC.DEFAULT_WEIGHTS)


_settings = {
    'slots': SC.DEFAULT_SLOTS,
    'min_state': 'approaching',
    'enabled': True,
    'alert_on': 'at_level',      # at_level | triggered
    'weights': _load_weights(),
}


def set_weights(w):
    """Normalize, store, and persist. Weights live in config.json rather than
    memory so a tuned board survives a restart -- otherwise every launch
    silently reverts to my guesses."""
    norm = SC.normalize_weights(w or {})
    with _lock:
        _settings['weights'] = norm
    try:
        cfg = cm.load_config()
        cfg['screener_weights'] = norm
        cm.save_config(cfg)
    except Exception as e:
        cm.log_forensic('api_event', event='screener_weights',
                        status='error', error=str(e))
    return norm


def settings():
    with _lock:
        return dict(_settings)


def configure(**kw):
    with _lock:
        for k in ('slots', 'min_state', 'enabled', 'alert_on'):
            if k in kw and kw[k] is not None:
                _settings[k] = kw[k]
        return dict(_settings)


def state():
    with _lock:
        return {**_state, 'settings': dict(_settings),
                'pending_alerts': len(_alerts)}


def drain_alerts(limit=50):
    """Take alerts off the queue. The UI calls this; they are gone after."""
    out = []
    with _lock:
        while _alerts and len(out) < limit:
            out.append(_alerts.popleft())
    return out


def peek_alerts():
    with _lock:
        return list(_alerts)


def scan_once(alpaca, entries=None, slots=None, min_state=None,
              raise_errors=False):
    """One pass. Returns the scan result, or None when it cannot run."""
    entries = entries if entries is not None else WL.today()
    if not entries:
        return {'board': [], 'watched': 0, 'scored': 0, 'bench': [],
                'errors': [], 'slots': slots or _settings['slots'],
                'at': datetime.now().isoformat(timespec='seconds'),
                'note': 'watchlist is empty for today'}

    if not alpaca.is_connected():
        if raise_errors:
            raise RuntimeError('Not connected to Alpaca.')
        return None

    tickers = sorted({e['ticker'] for e in entries})
    daily = alpaca.get_bars_multi(tickers, '1Day', limit=DAILY_BARS)
    intra = alpaca.get_bars_multi(tickers, INTRA_TF, limit=INTRA_BARS)
    # The hour is needed for the 1H 34/50 vote. Batched like the others, so
    # the whole watchlist still costs three requests per pass, not 3N.
    hourly = alpaca.get_bars_multi(tickers, '1Hour', limit=HOURLY_BARS)

    now_et = datetime.now(ET)

    def getter(t):
        return (drop_forming(daily.get(t, []), '1Day', now_et),
                drop_forming(intra.get(t, []), INTRA_TF, now_et),
                drop_forming(hourly.get(t, []), '1Hour', now_et))

    with _lock:
        s = slots if slots is not None else _settings['slots']
        ms = min_state if min_state is not None else _settings['min_state']
        w = dict(_settings['weights'])
    import plays as PL
    from datetime import date as _date
    pbt = PL.by_ticker(for_date=_date.today().isoformat())
    return SC.scan(entries, getter, slots=s, min_state=ms, weights=w,
                   plays_by_ticker=pbt)


def _raise_alerts(result):
    """Queue anything that escalated into an alert-worthy state."""
    if not result:
        return 0
    with _lock:
        threshold = _settings['alert_on']
    want = SC.AlertGate.RANK.get(threshold, 2)
    n = 0
    for row in result.get('board', []):
        if SC.AlertGate.RANK.get(row['state'], 0) < want:
            continue
        if not _gate.should_alert(row):
            continue
        cl = row.get('closest') or {}
        rec = {
            'ticker': row['ticker'],
            'state': row['state'],
            'direction': row['direction'],
            'aligned': row['aligned'],
            'price': row['price'],
            'level': cl.get('name'),
            'level_price': cl.get('price'),
            'distance_atr': cl.get('distance_atr'),
            'trend': row['trend'],
            'trigger': row['trigger'],
            'note': row['note'],
            'at': datetime.now().isoformat(timespec='seconds'),
        }
        with _lock:
            _alerts.append(rec)
            hist = _alert_history.setdefault(row['ticker'], deque(maxlen=40))
            hist.append(rec)
        n += 1
    return n


def alert_history(ticker, limit=40):
    with _lock:
        return list(_alert_history.get(ticker.upper(), []))[-limit:]


def loop(alpaca):
    """Daemon target. Never raises; a bad pass leaves the last board intact."""
    with _lock:
        _state['running'] = True
    print(f'[SCR] screener started ({INTERVAL_SECONDS}s cadence)')
    while True:
        try:
            time.sleep(INTERVAL_SECONDS)
            with _lock:
                enabled = _settings['enabled']
            if not enabled:
                continue
            result = scan_once(alpaca)
            if result is None:
                continue
            raised = _raise_alerts(result)
            with _lock:
                _state['result'] = result
                _state['last_scan'] = result['at']
                _state['last_error'] = None
                _state['scans'] += 1
            if raised:
                cm.log_forensic('api_event', event='screener_alert',
                                status='ok', n_alerts=raised)
        except Exception as e:
            with _lock:
                _state['last_error'] = f'{type(e).__name__}: {e}'
            try:
                cm.log_forensic('api_event', event='screener_loop',
                                status='error', error=str(e))
            except Exception:
                pass
            time.sleep(5)


def start(alpaca):
    t = threading.Thread(target=loop, args=(alpaca,), daemon=True)
    t.start()
    return t


def reset_gate():
    _gate.reset()
