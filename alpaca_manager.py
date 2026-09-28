"""
alpaca_manager.py — Foundations Trading

The ONLY module that talks to Alpaca. Everything upward receives plain
Python dicts and lists, never raw SDK objects.

Account tier assumption: Algo Trader Plus (10,000 req/min) with live
market data (SIP feed for stocks, indicative feed for options Greeks).

Owns:
  - Connection lifecycle (connect / disconnect / is_connected)
  - Account + market clock queries
  - Stock bars, quotes, snapshots
  - Options chains WITH GREEKS, options quotes, GEX computation
  - Order placement (shares + options) and position queries
  - A token-bucket rate limiter as a guardrail

Does NOT own:
  - Any trading decision. No stops, no gates, no sizing. Pure broker I/O.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from config_manager import log_forensic

# alpaca-py imports are deferred into connect() so this module can be
# imported (e.g. by tests) on a machine without the SDK installed.


# ─── RATE LIMITER ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket. Default sized for Algo Trader Plus (10,000/min) with
    headroom — we cap ourselves at 9,000 so bursts never brush the real limit."""

    def __init__(self, max_per_minute=9000):
        self.capacity = max_per_minute
        self.tokens = float(max_per_minute)
        self.refill_rate = max_per_minute / 60.0   # tokens per second
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def wait(self):
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.last_refill) * self.refill_rate)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.01)


# ─── MANAGER ───────────────────────────────────────────────────────────────────

class AlpacaManager:

    def __init__(self):
        self._trading = None       # TradingClient
        self._stock_data = None    # StockHistoricalDataClient
        self._option_data = None   # OptionHistoricalDataClient
        self.connected = False
        self.paper = True
        self._key = self._secret = None
        self.limiter = RateLimiter()
        self._lock = threading.RLock()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, key, secret, paper=True):
        """Connect all three clients. Returns {'status': 'ok'|'error', 'message': str}."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.historical.option import OptionHistoricalDataClient
        except ImportError as e:
            return {'status': 'error',
                    'message': f'alpaca-py not installed: {e}. Run: pip install alpaca-py'}

        if not key or not secret:
            return {'status': 'error', 'message': 'Missing API key or secret.'}

        try:
            with self._lock:
                self._trading = TradingClient(key, secret, paper=paper)
                self._key, self._secret = key, secret
                self._stock_data = StockHistoricalDataClient(key, secret)
                self._option_data = OptionHistoricalDataClient(key, secret)
                # Prove the credentials work with one cheap call.
                acct = self._trading.get_account()
                self.connected = True
                self.paper = paper
            log_forensic('conn_event', event='connect', status='ok',
                         paper=paper, account_status=str(acct.status))
            return {'status': 'ok',
                    'message': f"Connected ({'paper' if paper else 'LIVE'}). "
                               f"Account status: {acct.status}"}
        except Exception as e:
            self.connected = False
            log_forensic('conn_event', event='connect', status='error', error=str(e))
            return {'status': 'error', 'message': f'Connection failed: {e}'}

    def disconnect(self):
        with self._lock:
            self._trading = None
            self._stock_data = None
            self._option_data = None
            self._key = self._secret = None
            self.connected = False
        log_forensic('conn_event', event='disconnect', status='ok')

    def is_connected(self):
        return self.connected

    def _require(self):
        if not self.connected:
            raise RuntimeError('Not connected to Alpaca.')

    # ── Account / clock ───────────────────────────────────────────────────────

    def get_account(self):
        """{'equity', 'cash', 'buying_power', 'status', 'options_level'}"""
        self._require()
        self.limiter.wait()
        a = self._trading.get_account()
        return {
            'equity': float(a.equity),
            'cash': float(a.cash),
            'buying_power': float(a.buying_power),
            'status': str(a.status),
            'options_level': getattr(a, 'options_trading_level', None),
        }

    def get_clock(self):
        """{'is_open', 'next_open', 'next_close'}"""
        self._require()
        self.limiter.wait()
        c = self._trading.get_clock()
        return {
            'is_open': bool(c.is_open),
            'next_open': str(c.next_open),
            'next_close': str(c.next_close),
        }

    def is_market_open(self):
        return self.get_clock()['is_open']

    # ── Stock data ────────────────────────────────────────────────────────────

    _TF_MAP = None

    def _timeframe(self, tf_str):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        m = {
            '1Min':  TimeFrame(1, TimeFrameUnit.Minute),
            '2Min':  TimeFrame(2, TimeFrameUnit.Minute),
            '5Min':  TimeFrame(5, TimeFrameUnit.Minute),
            '6Min':  TimeFrame(6, TimeFrameUnit.Minute),
            '10Min': TimeFrame(10, TimeFrameUnit.Minute),
            '15Min': TimeFrame(15, TimeFrameUnit.Minute),
            '30Min': TimeFrame(30, TimeFrameUnit.Minute),
            '1Hour': TimeFrame(1, TimeFrameUnit.Hour),
            '2Hour': TimeFrame(2, TimeFrameUnit.Hour),
            '4Hour': TimeFrame(4, TimeFrameUnit.Hour),
            '1Day':  TimeFrame(1, TimeFrameUnit.Day),
            '1Week': TimeFrame(1, TimeFrameUnit.Week),
            '1Month': TimeFrame(1, TimeFrameUnit.Month),
        }
        if tf_str in m:
            return m[tf_str]
        # Fall through to a parsed TimeFrame so an unlisted-but-valid
        # timeframe ('3Min', '45Min') works instead of raising.
        import re as _re
        hit = _re.match(r'^(\d+)(Min|Hour|Day|Week|Month)$', str(tf_str).strip(),
                        _re.I)
        if hit:
            unit = {'min': TimeFrameUnit.Minute, 'hour': TimeFrameUnit.Hour,
                    'day': TimeFrameUnit.Day, 'week': TimeFrameUnit.Week,
                    'month': TimeFrameUnit.Month}[hit.group(2).lower()]
            return TimeFrame(int(hit.group(1)), unit)
        raise ValueError(f'Unsupported timeframe: {tf_str}')

    def get_bars(self, ticker, timeframe='10Min', limit=500):
        """Returns a list of dicts, oldest first:
        {'time': iso str, 'open', 'high', 'low', 'close', 'volume'}"""
        self._require()
        from alpaca.data.requests import StockBarsRequest

        # Lookback window generous enough to cover `limit` bars incl. weekends.
        import halflife as _hlf
        minutes_per_bar = _hlf.tf_minutes(timeframe, default=10)
        # Market hours ≈ 390 min/day; pad x3 for weekends/holidays.
        days_back = max(2, int(limit * minutes_per_bar / 390 * 3) + 2)
        start = datetime.now() - timedelta(days=days_back)

        self.limiter.wait()
        t0 = time.monotonic()
        req = StockBarsRequest(symbol_or_symbols=ticker,
                               timeframe=self._timeframe(timeframe),
                               start=start, limit=None, feed='sip')
        try:
            resp = self._stock_data.get_stock_bars(req)
            bars = resp.data.get(ticker, [])
        except Exception as e:
            log_forensic('api_event', event='get_bars', ticker=ticker,
                         timeframe=timeframe, status='error', error=str(e))
            return []
        out = [{
            'time': b.timestamp.isoformat(),
            'open': float(b.open), 'high': float(b.high),
            'low': float(b.low), 'close': float(b.close),
            'volume': float(b.volume),
        } for b in bars]
        out = out[-limit:]
        log_forensic('api_event', event='get_bars', ticker=ticker,
                     timeframe=timeframe, status='ok', n_bars=len(out),
                     latency_ms=int((time.monotonic() - t0) * 1000))
        return out

    # How many symbols to pack into one multi-symbol data request. Conservative:
    # keeps URLs short and each response a sane size. 1,000 names = 5 requests.
    MULTI_CHUNK = 200

    def get_bars_multi(self, symbols, timeframe='10Min', limit=300, start=None,
                       progress=None):
        """Batched bars for many symbols. Returns {symbol: [bar dicts]},
        each list oldest first, same dict shape as get_bars. Symbols with no
        data are simply absent from the result.

        One API request per MULTI_CHUNK symbols instead of one per symbol —
        this is what makes 1,000-name sweeps fit the clock and the rate limit.
        `start` overrides the lookback window (used by the incremental bar
        cache to request only bars newer than what it holds).

        `progress`, if given, is called progress(symbols_done, symbols_total)
        after each chunk. A 1,000-name fetch is 5 chunks and can take a
        minute; without this the UI sits blank through the slowest phase of
        a screen."""
        self._require()
        from alpaca.data.requests import StockBarsRequest

        symbols = [s.upper() for s in symbols]
        if not symbols:
            return {}

        if start is None:
            import halflife as _hlf
            minutes_per_bar = _hlf.tf_minutes(timeframe, default=10)
            days_back = max(2, int(limit * minutes_per_bar / 390 * 3) + 2)
            start = datetime.now() - timedelta(days=days_back)

        out = {}
        t0 = time.monotonic()
        n_chunks = (len(symbols) + self.MULTI_CHUNK - 1) // self.MULTI_CHUNK
        for i in range(0, len(symbols), self.MULTI_CHUNK):
            chunk = symbols[i:i + self.MULTI_CHUNK]
            ci = i // self.MULTI_CHUNK + 1
            self.limiter.wait()
            c0 = time.monotonic()
            # Announce BEFORE the call. A hung request is otherwise invisible:
            # the UI shows a fetch phase at 0% and the console shows nothing,
            # so there is no way to tell a slow API from a dead thread.
            print(f'[BARS] {timeframe} chunk {ci}/{n_chunks} '
                  f'({len(chunk)} symbols) start={start} ...', flush=True)
            try:
                req = StockBarsRequest(symbol_or_symbols=chunk,
                                       timeframe=self._timeframe(timeframe),
                                       start=start, limit=None, feed='sip')
                resp = self._stock_data.get_stock_bars(req)
            except Exception as e:
                print(f'[BARS] {timeframe} chunk {ci}/{n_chunks} FAILED '
                      f'after {time.monotonic() - c0:.1f}s: {e}', flush=True)
                log_forensic('api_event', event='get_bars_multi',
                             timeframe=timeframe, status='error',
                             n_symbols=len(chunk), error=str(e))
                # Advance progress even on failure. Otherwise a failing chunk
                # freezes the bar at the previous count and looks like a hang.
                if progress:
                    progress(min(i + self.MULTI_CHUNK, len(symbols)),
                             len(symbols))
                continue
            print(f'[BARS] {timeframe} chunk {ci}/{n_chunks} ok '
                  f'({time.monotonic() - c0:.1f}s)', flush=True)
            for sym in chunk:
                bars = resp.data.get(sym, [])
                if not bars:
                    continue
                rows = [{
                    'time': b.timestamp.isoformat(),
                    'open': float(b.open), 'high': float(b.high),
                    'low': float(b.low), 'close': float(b.close),
                    'volume': float(b.volume),
                } for b in bars]
                out[sym] = rows[-limit:]
            if progress:
                progress(min(i + self.MULTI_CHUNK, len(symbols)), len(symbols))
        log_forensic('api_event', event='get_bars_multi', timeframe=timeframe,
                     status='ok', n_symbols=len(out),
                     latency_ms=int((time.monotonic() - t0) * 1000))
        return out

    def get_quotes_multi(self, symbols):
        """Batched latest quotes. Returns {symbol: {'bid','ask','mid','time'}}.
        Symbols with a bad/absent quote are omitted. Same chunking economics
        as get_bars_multi: 1,000 names = 5 requests, not 1,000."""
        self._require()
        from alpaca.data.requests import StockLatestQuoteRequest
        symbols = [s.upper() for s in symbols]
        out = {}
        for i in range(0, len(symbols), self.MULTI_CHUNK):
            chunk = symbols[i:i + self.MULTI_CHUNK]
            self.limiter.wait()
            try:
                req = StockLatestQuoteRequest(symbol_or_symbols=chunk,
                                              feed='sip')
                quotes = self._stock_data.get_stock_latest_quote(req)
            except Exception as e:
                log_forensic('api_event', event='get_quotes_multi',
                             status='error', n_symbols=len(chunk),
                             error=str(e))
                continue
            for sym in chunk:
                q = quotes.get(sym)
                if q is None:
                    continue
                try:
                    bid, ask = float(q.bid_price), float(q.ask_price)
                except (TypeError, ValueError):
                    continue
                out[sym] = {'bid': bid, 'ask': ask,
                            'mid': round((bid + ask) / 2, 4),
                            'time': q.timestamp.isoformat()}
        return out

    def get_quote(self, ticker):
        """{'bid', 'ask', 'mid', 'time'} or None."""
        self._require()
        from alpaca.data.requests import StockLatestQuoteRequest
        self.limiter.wait()
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed='sip')
            q = self._stock_data.get_stock_latest_quote(req)[ticker]
            bid, ask = float(q.bid_price), float(q.ask_price)
            return {'bid': bid, 'ask': ask,
                    'mid': round((bid + ask) / 2, 4),
                    'time': q.timestamp.isoformat()}
        except Exception as e:
            log_forensic('api_event', event='get_quote', ticker=ticker,
                         status='error', error=str(e))
            return None

    # ── Options data ──────────────────────────────────────────────────────────

    def get_options_chain(self, ticker, current_price,
                          strike_range_pct=0.04, dte_min=4, dte_max=26):
        """Chain snapshot with Greeks. Returns a list of contract dicts:

        {'symbol', 'type': 'call'|'put', 'strike', 'expiry',
         'bid', 'ask', 'mid', 'spread_pct',
         'open_interest', 'iv',
         'delta', 'gamma', 'theta', 'vega', 'rho'}

        Filtered to strikes within strike_range_pct of current_price and
        expiries within [dte_min, dte_max] days.
        """
        self._require()
        from alpaca.data.requests import OptionChainRequest

        lo = current_price * (1 - strike_range_pct)
        hi = current_price * (1 + strike_range_pct)
        today = datetime.now().date()
        exp_gte = today + timedelta(days=dte_min)
        exp_lte = today + timedelta(days=dte_max)

        self.limiter.wait()
        t0 = time.monotonic()
        try:
            req = OptionChainRequest(
                underlying_symbol=ticker,
                strike_price_gte=round(lo, 2),
                strike_price_lte=round(hi, 2),
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                feed='indicative',          # indicative feed carries Greeks
            )
            chain = self._option_data.get_option_chain(req)
        except Exception as e:
            log_forensic('api_event', event='get_options_chain', ticker=ticker,
                         status='error', error=str(e))
            return []

        out = []
        for symbol, snap in chain.items():
            try:
                q = snap.latest_quote
                if q is None:
                    continue
                bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
                if bid <= 0 or ask <= 0:
                    continue
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid * 100 if mid > 0 else 999

                g = snap.greeks
                contract = {
                    'symbol': symbol,
                    'type': 'call' if 'C' in symbol[-9:] else 'put',
                    'strike': _occ_strike(symbol),
                    'expiry': _occ_expiry(symbol),
                    'bid': round(bid, 4), 'ask': round(ask, 4),
                    'mid': round(mid, 4),
                    'spread_pct': round(spread_pct, 2),
                    'open_interest': _safe_float(getattr(snap, 'open_interest', None)),
                    'iv': _safe_float(getattr(snap, 'implied_volatility', None)),
                    'delta': _safe_float(getattr(g, 'delta', None)) if g else None,
                    'gamma': _safe_float(getattr(g, 'gamma', None)) if g else None,
                    'theta': _safe_float(getattr(g, 'theta', None)) if g else None,
                    'vega':  _safe_float(getattr(g, 'vega', None)) if g else None,
                    'rho':   _safe_float(getattr(g, 'rho', None)) if g else None,
                }
                out.append(contract)
            except Exception:
                continue

        out.sort(key=lambda c: (c['expiry'], c['strike']))
        log_forensic('api_event', event='get_options_chain', ticker=ticker,
                     status='ok', n_contracts=len(out),
                     latency_ms=int((time.monotonic() - t0) * 1000))
        return out

    def get_options_quote(self, option_symbol):
        """{'bid', 'ask', 'mid'} or None."""
        self._require()
        from alpaca.data.requests import OptionLatestQuoteRequest
        self.limiter.wait()
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol,
                                           feed='indicative')
            q = self._option_data.get_option_latest_quote(req)[option_symbol]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid <= 0 or ask <= 0:
                return None
            return {'bid': round(bid, 4), 'ask': round(ask, 4),
                    'mid': round((bid + ask) / 2, 4)}
        except Exception as e:
            log_forensic('api_event', event='get_options_quote',
                         symbol=option_symbol, status='error', error=str(e))
            return None

    def get_gex(self, ticker, current_price, strike_range_pct=0.10,
                dte_min=0, dte_max=45):
        """Approximate gamma exposure from the chain.

        GEX = Σ gamma × open_interest × 100 × spot   (calls +, puts −)

        Wider strike range and DTE window than trading selection, because
        GEX is a market-structure read, not a contract pick. Returns
        {'gex_total', 'gex_calls', 'gex_puts', 'n_contracts'} or None.
        """
        chain = self.get_options_chain(ticker, current_price,
                                       strike_range_pct=strike_range_pct,
                                       dte_min=dte_min, dte_max=dte_max)
        if not chain:
            return None
        gex_calls = 0.0
        gex_puts = 0.0
        n = 0
        for c in chain:
            if c['gamma'] is None or not c['open_interest']:
                continue
            contrib = c['gamma'] * c['open_interest'] * 100 * current_price
            if c['type'] == 'call':
                gex_calls += contrib
            else:
                gex_puts -= contrib
            n += 1
        return {
            'gex_total': round(gex_calls + gex_puts, 0),
            'gex_calls': round(gex_calls, 0),
            'gex_puts': round(gex_puts, 0),
            'n_contracts': n,
        }

    # ── Orders / positions ────────────────────────────────────────────────────

    def place_share_order(self, ticker, side, qty, limit_price=None):
        """Shares. side: 'buy'|'sell'. limit_price=None means a MARKET order.

        A limit is DAY like everything else here, so an unfilled one dies at
        the bell rather than resting overnight against a position the engine
        thinks it knows the size of.

        Returns {'order_id', 'status', 'symbol', 'qty'} or {'status':'error',...}."""
        self._require()
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        self.limiter.wait()
        try:
            _side = OrderSide.BUY if side == 'buy' else OrderSide.SELL
            if limit_price is not None:
                req = LimitOrderRequest(
                    symbol=ticker, qty=qty, side=_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(float(limit_price), 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=ticker, qty=qty, side=_side,
                    time_in_force=TimeInForce.DAY,
                )
            o = self._trading.submit_order(req)
            log_forensic('api_event', event='place_share_order', ticker=ticker,
                         side=side, qty=qty, status='ok', order_id=str(o.id))
            return {'order_id': str(o.id), 'status': str(o.status),
                    'symbol': ticker, 'qty': qty}
        except Exception as e:
            log_forensic('api_event', event='place_share_order', ticker=ticker,
                         side=side, qty=qty, status='error', error=str(e))
            return {'status': 'error', 'message': str(e)}

    def place_option_order(self, option_symbol, side, contracts,
                           limit_price=None):
        """Option contracts. side: 'buy'|'sell'. limit_price=None is MARKET.

        Position intent is ALWAYS explicit and one-directional:
          buy  -> BUY_TO_OPEN   (establish/add a long option position)
          sell -> SELL_TO_CLOSE (reduce an existing long; can NEVER open
                                 a short). A sell with nothing to close is
                                 rejected by the broker rather than opening
                                 a naked short. This makes short options
                                 structurally impossible for this system.
        """
        self._require()
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, PositionIntent
        self.limiter.wait()
        try:
            intent = (PositionIntent.BUY_TO_OPEN if side == 'buy'
                      else PositionIntent.SELL_TO_CLOSE)
            _side = OrderSide.BUY if side == 'buy' else OrderSide.SELL
            if limit_price is not None:
                req = LimitOrderRequest(
                    symbol=option_symbol, qty=contracts, side=_side,
                    time_in_force=TimeInForce.DAY,
                    position_intent=intent,
                    limit_price=round(float(limit_price), 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=option_symbol, qty=contracts, side=_side,
                    time_in_force=TimeInForce.DAY,
                    position_intent=intent,
                )
            o = self._trading.submit_order(req)
            log_forensic('api_event', event='place_option_order',
                         symbol=option_symbol, side=side, qty=contracts,
                         status='ok', order_id=str(o.id))
            return {'order_id': str(o.id), 'status': str(o.status),
                    'symbol': option_symbol, 'qty': contracts}
        except Exception as e:
            log_forensic('api_event', event='place_option_order',
                         symbol=option_symbol, side=side, qty=contracts,
                         status='error', error=str(e))
            return {'status': 'error', 'message': str(e)}

    def get_order(self, order_id):
        """Fetch one order's current state."""
        self._require()
        self.limiter.wait()
        try:
            o = self._trading.get_order_by_id(order_id)
            return {
                'order_id': str(o.id), 'symbol': o.symbol,
                'status': str(o.status), 'side': str(o.side),
                'qty': _safe_float(o.qty),
                'filled_qty': _safe_float(o.filled_qty),
                'filled_avg_price': _safe_float(o.filled_avg_price),
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def get_positions(self):
        """All open broker positions (shares and options)."""
        self._require()
        self.limiter.wait()
        try:
            positions = self._trading.get_all_positions()
        except Exception as e:
            log_forensic('api_event', event='get_positions', status='error',
                         error=str(e))
            return []
        out = []
        for p in positions:
            out.append({
                'symbol': p.symbol,
                'asset_class': str(p.asset_class),
                'qty': _safe_float(p.qty),
                'avg_entry_price': _safe_float(p.avg_entry_price),
                'current_price': _safe_float(p.current_price),
                'market_value': _safe_float(p.market_value),
                'unrealized_pl': _safe_float(p.unrealized_pl),
                'unrealized_plpc': _safe_float(p.unrealized_plpc),
            })
        return out


# ─── OCC SYMBOL HELPERS ────────────────────────────────────────────────────────

    # ── Account activities (fills) ────────────────────────────────────────────

    TRADING_BASE_PAPER = 'https://paper-api.alpaca.markets'
    TRADING_BASE_LIVE = 'https://api.alpaca.markets'

    def get_fill_activities(self, after=None, until=None, page_limit=50):
        """Every FILL and PARTIAL_FILL on the account, newest first.

        Returns a list of normalized dicts:
          {activity_id, order_id, symbol, side, qty, price, transaction_time,
           type, cum_qty, leaves_qty}

        WHY THIS TALKS REST INSTEAD OF THE SDK
          alpaca-py has moved the activities call and its request model across
          versions. The /v2/account/activities/FILL endpoint has not changed.
          We try the SDK first so we inherit its auth and retry behaviour, and
          fall back to the documented REST route when the SDK on this machine
          does not expose it. Both paths return the same normalized shape.

        after/until are ISO timestamps. Paginates until exhausted or
        page_limit pages, whichever comes first (100 records per page, so the
        default ceiling is 5,000 fills).
        """
        self._require()

        rows = self._fills_via_sdk(after, until, page_limit)
        if rows is not None:
            return rows
        return self._fills_via_rest(after, until, page_limit)

    def _fills_via_sdk(self, after, until, page_limit):
        """Returns None (not []) when the SDK can't do it, so the caller
        knows to fall back rather than believing the account has no fills."""
        try:
            from alpaca.trading.requests import GetAccountActivitiesRequest
        except Exception:
            return None
        if not hasattr(self._trading, 'get_account_activities'):
            return None

        out, token = [], None
        try:
            for _ in range(page_limit):
                kw = {'activity_types': ['FILL'], 'page_size': 100}
                if after:
                    kw['after'] = after
                if until:
                    kw['until'] = until
                if token:
                    kw['page_token'] = token
                try:
                    req = GetAccountActivitiesRequest(**kw)
                except TypeError:
                    kw.pop('activity_types', None)
                    kw['activity_types'] = 'FILL'
                    req = GetAccountActivitiesRequest(**kw)

                self.limiter.wait()
                page = self._trading.get_account_activities(req)
                page = list(page or [])
                if not page:
                    break
                for a in page:
                    row = self._norm_activity(a)
                    if row:
                        out.append(row)
                token = getattr(page[-1], 'id', None)
                if not token or len(page) < 100:
                    break
            return out
        except Exception as e:
            log_forensic('api_event', event='activities_sdk', status='error',
                         error=str(e))
            return None

    @staticmethod
    def _rfc3339(v):
        """Normalize a timestamp to what the activities endpoint accepts.

        The docs allow exactly two shapes: YYYY-MM-DD and YYYY-MM-DDTHH:MM:SSZ.
        A naive isoformat() emits microseconds and no zone, which the endpoint
        rejects with 422. Fractions of a second are not accepted anywhere in
        this API, so they are dropped rather than rounded.
        """
        if v in (None, ''):
            return None
        if isinstance(v, datetime):
            dt = v
        else:
            txt = str(v).strip()
            if len(txt) == 10 and txt[4] == '-':      # already YYYY-MM-DD
                return txt
            try:
                dt = datetime.fromisoformat(txt.replace('Z', '+00:00'))
            except ValueError:
                return txt                            # hand it over untouched
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _fills_via_rest(self, after, until, page_limit):
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request

        if not self._key or not self._secret:
            raise RuntimeError('No stored credentials for the activities call.')

        base = (self.TRADING_BASE_PAPER if self.paper
                else self.TRADING_BASE_LIVE)
        after = self._rfc3339(after)
        until = self._rfc3339(until)

        def fetch(path, extra, token):
            q = dict(extra)
            q['page_size'] = 100
            if after:
                q['after'] = after
            if until:
                q['until'] = until
            if token:
                q['page_token'] = token
            url = f'{base}{path}?' + urllib.parse.urlencode(q)
            req = urllib.request.Request(url, headers={
                'APCA-API-KEY-ID': self._key,
                'APCA-API-SECRET-KEY': self._secret,
                'accept': 'application/json'})
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return _json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                # Alpaca explains itself in the response body. Reading it is
                # the difference between "422 Unprocessable Entity" and an
                # error that names the offending parameter.
                try:
                    detail = e.read().decode('utf-8', 'replace')[:400]
                except Exception:
                    detail = ''
                raise RuntimeError(
                    f'Alpaca {e.code} on {path}: {detail or e.reason}') from None

        # Two request shapes, because Alpaca exposes both and the path form
        # has been the less reliable of the two.
        shapes = [('/v2/account/activities/FILL', {}),
                  ('/v2/account/activities', {'activity_types': 'FILL'})]

        last_err = None
        for path, extra in shapes:
            out, token = [], None
            try:
                for _ in range(page_limit):
                    page = fetch(path, extra, token)
                    if not page:
                        break
                    for a in page:
                        row = self._norm_activity(a)
                        if row:
                            out.append(row)
                    token = (page[-1] or {}).get('id')
                    if not token or len(page) < 100:
                        break
                return out
            except RuntimeError as e:
                last_err = e
                log_forensic('api_event', event='activities_rest',
                             status='error', error=str(e), shape=path)
                continue

        raise last_err if last_err else RuntimeError(
            'Alpaca activities request failed with no error recorded.')

    def get_nontrade_activities(self, after=None, until=None, page_limit=50):
        """Every NON-trade activity: fees, dividends, interest, transfers,
        option assignments and exercises.

        Same endpoint as the fills, different shape. A non-trade activity has
        `date` and `net_amount` rather than `transaction_time` and price/qty,
        and `net_amount` is already signed, so a fee arrives negative.

        No activity_type filter is sent. Alpaca's type codes change as they
        add products, and enumerating them here would silently drop whatever
        is new; taking everything and discarding the fills is stable.
        """
        self._require()
        rows = self._nontrade_via_rest(after, until, page_limit)
        return rows

    def _nontrade_via_rest(self, after, until, page_limit):
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request

        if not self._key or not self._secret:
            raise RuntimeError('No stored credentials for the activities call.')

        base = (self.TRADING_BASE_PAPER if self.paper
                else self.TRADING_BASE_LIVE)
        after = self._rfc3339(after)
        until = self._rfc3339(until)

        out, token = [], None
        for _ in range(page_limit):
            q = {'page_size': 100}
            if after:
                q['after'] = after
            if until:
                q['until'] = until
            if token:
                q['page_token'] = token
            url = f'{base}/v2/account/activities?' + urllib.parse.urlencode(q)
            req = urllib.request.Request(url, headers={
                'APCA-API-KEY-ID': self._key,
                'APCA-API-SECRET-KEY': self._secret,
                'accept': 'application/json'})
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    page = _json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode('utf-8', 'replace')[:400]
                except Exception:
                    detail = ''
                log_forensic('api_event', event='nontrade_rest',
                             status='error', error=f'{e.code} {detail}')
                raise RuntimeError(
                    f'Alpaca {e.code} on activities: {detail or e.reason}') from None
            if not page:
                break
            for a in page:
                row = self._norm_nontrade(a)
                if row:
                    out.append(row)
            token = (page[-1] or {}).get('id')
            if not token or len(page) < 100:
                break
        return out

    @staticmethod
    def _norm_nontrade(a):
        """One non-trade activity -> our shape. Fills are dropped."""
        def g(k):
            return a.get(k) if isinstance(a, dict) else getattr(a, k, None)

        kind = str(g('activity_type') or '').upper()
        if kind in ('FILL', 'PARTIAL_FILL', ''):
            return None

        amt = _safe_float(g('net_amount'))
        if amt is None:
            return None

        when = g('date') or g('transaction_time') or ''
        if hasattr(when, 'isoformat'):
            when = when.isoformat()
        when = str(when)[:10]

        return {
            'activity_id': str(g('id') or ''),
            'activity_type': kind,
            'date': when,
            'net_amount': amt,
            'description': str(g('description') or ''),
            'symbol': str(g('symbol') or ''),
            'qty': _safe_float(g('qty')),
            'status': str(g('status') or ''),
        }

    @staticmethod
    def _norm_activity(a):
        """One activity -> our shape. Accepts an SDK object or a dict."""
        def g(k):
            if isinstance(a, dict):
                return a.get(k)
            return getattr(a, k, None)

        symbol = g('symbol')
        side = str(g('side') or '').lower()
        if '.' in side:                      # OrderSide.BUY -> buy
            side = side.split('.')[-1]
        if side not in ('buy', 'sell') or not symbol:
            return None

        qty = _safe_float(g('qty'))
        price = _safe_float(g('price'))
        if not qty or price is None:
            return None

        t = g('transaction_time')
        if hasattr(t, 'isoformat'):
            t = t.isoformat()

        return {
            'activity_id': str(g('id') or ''),
            'order_id': str(g('order_id') or ''),
            'symbol': str(symbol).strip(),
            'side': side,
            'qty': abs(qty),
            'price': price,
            'transaction_time': str(t or ''),
            'type': str(g('type') or ''),
            'cum_qty': _safe_float(g('cum_qty')),
            'leaves_qty': _safe_float(g('leaves_qty')),
        }


def _occ_strike(symbol):
    """Strike from an OCC symbol: last 8 chars are strike × 1000."""
    try:
        return int(symbol[-8:]) / 1000.0
    except (ValueError, IndexError):
        return None


def _occ_expiry(symbol):
    """Expiry (YYYY-MM-DD) from an OCC symbol: 6 digits before the C/P flag."""
    try:
        d = symbol[-15:-9]
        return f'20{d[0:2]}-{d[2:4]}-{d[4:6]}'
    except IndexError:
        return None


def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
