"""
bar_cache.py - Foundations Trading

Rolling in-memory bar cache for the sweep.

Problem it solves: the engine needs ~300 closed 10-min bars of context per
name, but between sweeps only ONE bar is new. Re-downloading full history for
1,000 names every 10 minutes is ~300k bars of JSON per sweep. This cache pays
that cost once (cold fill), then refreshes incrementally: each sweep requests
only bars newer than the oldest cached tail across the basket.

Correctness properties:
  - Merge is by timestamp: a re-delivered bar REPLACES its cached twin, so a
    late volume revision from the feed self-heals on the next sweep.
  - The refresh window starts one bar BEFORE the newest cached bar, so the
    final bar of the previous sweep (which the feed may have revised) is
    always re-fetched.
  - Cold symbols (new to the basket, or evicted) get a full-history fetch.
    A symbol that returns nothing stays cold and retries next sweep.
  - clear() on reconnect: a data gap must trigger a full refill, never a
    silent hole in the middle of an EMA window.

Memory: 1,000 symbols x 300 bars x ~6 floats is a few tens of MB. Fine.

NOT thread-safe by itself; the bar loop is the only caller. If a second
consumer ever appears, add a lock here rather than in the callers.
"""
from datetime import datetime, timedelta


class BarCache:

    # Warm symbols whose newest bar lags the basket's newest by more than
    # this many bar widths are refreshed in their own batch (see refresh()).
    STALE_GAP_BARS = 3

    def __init__(self, timeframe, limit):
        self.timeframe = timeframe
        self.limit = int(limit)
        self._bars = {}      # symbol -> list of bar dicts, oldest first

    def clear(self):
        self._bars = {}

    # -- internals -------------------------------------------------------------

    def _merge(self, symbol, new_bars):
        by_t = {b['time']: b for b in self._bars.get(symbol, [])}
        for b in new_bars:
            by_t[b['time']] = b               # replace-on-collision: feed wins
        merged = [by_t[t] for t in sorted(by_t)]
        self._bars[symbol] = merged[-self.limit:]

    def _tf_minutes(self):
        # Parsed, not looked up. A private copy of the timeframe menu here is
        # how adding '6Min' elsewhere turned into a KeyError in this file.
        import halflife as hlf
        m = hlf.tf_minutes(self.timeframe)
        if m is None:
            raise ValueError(f'Unsupported timeframe: {self.timeframe}')
        return m

    # -- the one public call ---------------------------------------------------

    def refresh(self, alpaca, symbols):
        """Bring the cache current for `symbols` and return
        {symbol: [bars]} (oldest first, up to self.limit each).

        Cold symbols get a full batched fetch; warm symbols share one
        incremental batched fetch from just before the oldest warm tail."""
        symbols = [s.upper() for s in symbols]
        cold = [s for s in symbols if not self._bars.get(s)]
        warm = [s for s in symbols if self._bars.get(s)]

        if cold:
            got = alpaca.get_bars_multi(cold, timeframe=self.timeframe,
                                        limit=self.limit)
            for sym, bars in got.items():
                self._merge(sym, bars)

        if warm:
            # Two batches, not one. A single fetch window keyed to the global
            # minimum tail meant one halted/thin symbol dragged the ENTIRE
            # basket's refresh back to its stale tail, turning every sweep
            # into a near-full refetch. Fresh symbols (tails within
            # STALE_GAP_BARS of the newest) share a tight window; stragglers
            # share their own. Blast radius of one dead tape: one extra
            # request, not 300k bars. Each group's start is still its own
            # min tail minus one bar width, so the revision-safe re-fetch of
            # the previous final bar is preserved per group.
            width = timedelta(minutes=self._tf_minutes())
            tails = {s: datetime.fromisoformat(self._bars[s][-1]['time'])
                     for s in warm}
            newest = max(tails.values())
            cutoff = newest - self.STALE_GAP_BARS * width
            fresh = [s for s in warm if tails[s] >= cutoff]
            stale = [s for s in warm if tails[s] < cutoff]
            for group in (fresh, stale):
                if not group:
                    continue
                start = min(tails[s] for s in group) - width
                got = alpaca.get_bars_multi(group, timeframe=self.timeframe,
                                            limit=self.limit,
                                            start=start.replace(tzinfo=None))
                for sym, bars in got.items():
                    self._merge(sym, bars)

        return {s: self._bars[s] for s in symbols if self._bars.get(s)}
