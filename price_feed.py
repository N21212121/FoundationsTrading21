"""
price_feed.py — Foundations Trading

The live price layer under the screener. A CONSUMER of the board: it does not
score anything, does not decide anything, and holds no opinion about a setup.
It answers one question, fast and often: has price crossed a level since we
last looked?

WHY THIS IS SEPARATE FROM THE SCAN
  A screener pass is expensive. It pulls daily, intraday and hourly bars for
  every watched ticker, rebuilds the level set, recomputes the clouds and
  re-reads the journal. That is a once-a-minute job and it should stay one.

  But a level is crossed in a second, not a minute, and a board showing a
  price that is fifty seconds old is lying about the only number on it that
  moves. So the readings that are expensive keep their slow clock, and the
  one reading that is cheap gets a fast one. The levels this module tests
  against are whatever the last scan computed; it never recomputes them.

ONE REQUEST, NOT FIFTY
  The obvious way to keep fifty tickers fresh is to spool them — a few names
  every tick, round-robin, so no single pass has to wait for all of them.
  That reasoning is right when each name costs a request. Here it does not.

  alpaca_manager.get_quotes_multi() batches by MULTI_CHUNK = 200, so every
  ticker the screener can hold fits in ONE http request. Spooling fifty names
  across a five-second window would make five times the requests, add up to
  five seconds of jitter to when any given name was last seen, and leave the
  board permanently inconsistent — half the rows from this second, half from
  three seconds ago, with no way to tell which. A single batched call gives
  every row the same timestamp and the whole board moves together.

  The cost is nothing. Fifty names every five seconds is 12 requests a
  minute against a limiter sized at 9,000.

WHAT A STANDBY ALERT IS
  Not a signal, and deliberately not one. The scan already raises alerts when
  a ticker ESCALATES into at_level or triggered, on cloud and level state it
  has actually measured. A standby is weaker and earlier than that: price
  just moved through a level it was on one side of, and the next scan has not
  run yet. It means LOOK NOW, not ACT.

  The distinction matters because the crossing is all this module knows. It
  cannot see whether the cloud agrees, whether the bar closed through or just
  wicked through, or whether the level ever mattered. A 10-min bar that pokes
  a level and closes back inside is not a break, and only the scan can tell
  you that. So standby alerts carry their own kind and are never promoted.

WHY ONLY THE NEAR LEVELS
  Each row carries near_levels: the six closest levels the scan found. Those
  are what get tested. Every level in the set would mean testing psych
  levels the price is nowhere near — on a $60 name those sit every $5 — and
  a cross of something two ATR away is not news. Six is what the row already
  carries, which also means this module adds no memory of its own.
"""

import threading
import time
from collections import deque
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import config_manager as cm

ET = ZoneInfo('America/New_York')

# Every five seconds. Fast enough that a level cross is news while it is still
# happening, slow enough that the board is not redrawn under the reader's
# cursor. Not a tunable: see the module docstring on what the cost is.
TICK_SECONDS = 5

# The session the feed runs in. SIP carries 04:00-20:00 ET and the pre-market
# extremes (PMH/PML) are levels the owner watches, so the feed starts when the
# tape does rather than at the opening bell.
FEED_START = dtime(4, 0)
FEED_END = dtime(20, 0)

# A ticker that crossed a level and crossed it back should not alert twice in
# a minute. Long enough to stop a price sitting exactly on a level from
# ringing every tick; short enough that a genuine re-test still reports.
CROSS_COOLDOWN_SECONDS = 300

# How many standby records to keep per ticker for the detail panel.
HISTORY_PER_TICKER = 40


def _now_et():
    return datetime.now(ET)


def in_session(now_et=None):
    """True when the tape is live. Weekends are excluded; holidays are not,
    because this module has no calendar and a quiet holiday poll is cheaper
    than a wrong one."""
    n = now_et or _now_et()
    if n.weekday() >= 5:
        return False
    return FEED_START <= n.time() < FEED_END


def crossings(prev_price, new_price, levels):
    """Every level strictly between two prices, with the direction taken.

    Uses a half-open test so a price resting exactly ON a level reports once,
    on the tick it arrives, and not again while it sits there. Returns a list
    of {'name', 'kind', 'level_price', 'way'} where way is 'up' or 'down'.
    """
    if prev_price is None or new_price is None or prev_price == new_price:
        return []
    lo, hi = sorted((prev_price, new_price))
    way = 'up' if new_price > prev_price else 'down'
    out = []
    for lv in levels or []:
        p = lv.get('price')
        if p is None:
            continue
        # Half-open on the low side: arriving exactly at a level counts as
        # reaching it, leaving it again does not count as crossing it.
        if lo < p <= hi:
            out.append({'name': lv.get('name'), 'kind': lv.get('kind'),
                        'level_price': p, 'way': way})
    return out


class PriceSpool:
    """Live prices for the watched tickers, plus the crosses between ticks.

    Pure apart from the clock: feed it quotes and levels, it tells you what
    crossed. The thread that fetches quotes lives below and is injected with
    its dependencies the way screener.scan takes a bar_getter, so this stays
    testable without Alpaca.
    """

    def __init__(self, cooldown_seconds=CROSS_COOLDOWN_SECONDS):
        self._lock = threading.Lock()
        self._prices = {}        # ticker -> {'price','prev','at','bid','ask'}
        self._levels = {}        # ticker -> [level dicts]
        self._last_cross = {}    # (ticker, name, way) -> monotonic seconds
        self._cooldown = cooldown_seconds
        self._history = {}       # ticker -> deque of standby records
        self.ticks = 0
        self.last_tick_at = None
        self.last_error = None

    # ─── what the scan tells us ──────────────────────────────────────────────

    def set_levels(self, rows):
        """Adopt the level sets from a completed scan.

        Called with the board plus the bench, because a ticker that fell off
        the board still has a price worth watching — that is precisely the
        one about to cross back into relevance.
        """
        levels, seen = {}, set()
        for r in rows or []:
            t = r.get('ticker')
            if not t:
                continue
            seen.add(t)
            levels.setdefault(t, r.get('near_levels') or [])
        with self._lock:
            self._levels = levels
            # Drop prices for tickers no longer watched, so an unwatched name
            # cannot come back with a stale previous price and fire a cross
            # across the gap.
            for t in list(self._prices):
                if t not in seen:
                    self._prices.pop(t, None)

    def tickers(self):
        with self._lock:
            return sorted(self._levels)

    # ─── what the quotes tell us ─────────────────────────────────────────────

    def ingest(self, quotes, now=None):
        """Apply a batch of quotes. Returns the standby records it produced.

        quotes is get_quotes_multi's shape: {sym: {'bid','ask','mid','time'}}.
        """
        mono = time.monotonic()
        stamp = (now or datetime.now()).isoformat(timespec='seconds')
        out = []
        with self._lock:
            for sym, q in (quotes or {}).items():
                mid = q.get('mid')
                if mid is None:
                    continue
                cur = self._prices.get(sym) or {}
                prev = cur.get('price')
                self._prices[sym] = {'price': mid, 'prev': prev,
                                     'bid': q.get('bid'), 'ask': q.get('ask'),
                                     'at': stamp}
                for c in crossings(prev, mid, self._levels.get(sym)):
                    key = (sym, c['name'], c['way'])
                    last = self._last_cross.get(key)
                    if last is not None and mono - last < self._cooldown:
                        continue
                    self._last_cross[key] = mono
                    rec = {
                        'kind': 'standby',
                        'ticker': sym,
                        'price': mid,
                        'prev_price': prev,
                        'level': c['name'],
                        'level_kind': c['kind'],
                        'level_price': c['level_price'],
                        'way': c['way'],
                        'at': stamp,
                    }
                    out.append(rec)
                    h = self._history.setdefault(sym, deque(
                        maxlen=HISTORY_PER_TICKER))
                    h.append(rec)
            self.ticks += 1
            self.last_tick_at = stamp
        return out

    # ─── readers ─────────────────────────────────────────────────────────────

    def prices(self):
        with self._lock:
            return {t: dict(v) for t, v in self._prices.items()}

    def price_of(self, ticker):
        with self._lock:
            v = self._prices.get((ticker or '').upper())
            return dict(v) if v else None

    def history(self, ticker, limit=HISTORY_PER_TICKER):
        with self._lock:
            return list(self._history.get(
                (ticker or '').upper(), []))[-limit:]

    def state(self):
        with self._lock:
            return {'tickers': len(self._levels), 'priced': len(self._prices),
                    'ticks': self.ticks, 'last_tick_at': self.last_tick_at,
                    'tick_seconds': TICK_SECONDS,
                    'in_session': in_session(),
                    'last_error': self.last_error}


# ─── THE THREAD ────────────────────────────────────────────────────────────────

_spool = PriceSpool()
_thread = None
_stop = threading.Event()


def spool():
    return _spool


def _tick(alpaca, emit):
    syms = _spool.tickers()
    if not syms:
        return
    quotes = alpaca.get_quotes_multi(syms)
    for rec in _spool.ingest(quotes):
        emit(rec)


def loop(alpaca, emit):
    """Daemon target. Never raises: a bad tick leaves the last prices intact.

    Outside the session it idles rather than exiting, so the feed comes back
    by itself the next morning without anything having to restart it.
    """
    while not _stop.is_set():
        try:
            if in_session():
                _tick(alpaca, emit)
                _spool.last_error = None
        except Exception as e:
            _spool.last_error = f'{type(e).__name__}: {e}'
            cm.log_forensic('api_event', event='price_feed',
                            status='error', error=str(e))
        _stop.wait(TICK_SECONDS if in_session() else 60)


def start(alpaca, emit):
    """Start the feed once. emit(record) receives each standby alert."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=loop, args=(alpaca, emit),
                               daemon=True, name='price_feed')
    _thread.start()
    return _thread


def stop():
    _stop.set()
