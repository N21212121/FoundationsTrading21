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
from datetime import datetime, timedelta

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
            '5Min':  TimeFrame(5, TimeFrameUnit.Minute),
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
        if tf_str not in m:
            raise ValueError(f'Unsupported timeframe: {tf_str}')
        return m[tf_str]

    def get_bars(self, ticker, timeframe='10Min', limit=500):
        """Returns a list of dicts, oldest first:
        {'time': iso str, 'open', 'high', 'low', 'close', 'volume'}"""
        self._require()
        from alpaca.data.requests import StockBarsRequest

        # Lookback window generous enough to cover `limit` bars incl. weekends.
        minutes_per_bar = {'1Min': 1, '5Min': 5, '10Min': 10, '15Min': 15,
                           '30Min': 30, '1Hour': 60, '2Hour': 120,
                           '4Hour': 240, '1Day': 1440, '1Week': 10080,
                           '1Month': 43200}[timeframe]
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

    def place_share_order(self, ticker, side, qty):
        """Market order for shares. side: 'buy'|'sell'.
        Returns {'order_id', 'status', 'symbol', 'qty'} or {'status':'error',...}."""
        self._require()
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        self.limiter.wait()
        try:
            req = MarketOrderRequest(
                symbol=ticker, qty=qty,
                side=OrderSide.BUY if side == 'buy' else OrderSide.SELL,
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

    def place_option_order(self, option_symbol, side, contracts):
        """Market order for option contracts. side: 'buy'|'sell'.

        Position intent is ALWAYS explicit and one-directional:
          buy  -> BUY_TO_OPEN   (establish/add a long option position)
          sell -> SELL_TO_CLOSE (reduce an existing long; can NEVER open
                                 a short). A sell with nothing to close is
                                 rejected by the broker rather than opening
                                 a naked short. This makes short options
                                 structurally impossible for this system.
        """
        self._require()
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, PositionIntent
        self.limiter.wait()
        try:
            intent = (PositionIntent.BUY_TO_OPEN if side == 'buy'
                      else PositionIntent.SELL_TO_CLOSE)
            req = MarketOrderRequest(
                symbol=option_symbol, qty=contracts,
                side=OrderSide.BUY if side == 'buy' else OrderSide.SELL,
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
