"""
test_alpaca_manager.py — Live read-only test of alpaca_manager.

Requires: alpaca-py installed, keys stored via setup_keys.py.
Places NO orders. Read-only API calls.

Run while the market is open for full results. Outside market hours,
quotes and chains may be thin or stale — the test notes this rather
than failing.
"""

import sys
import config_manager as cm
from alpaca_manager import AlpacaManager

TICKER = 'NVDA'    # liquid, heavily optioned — good test subject


def main():
    print("\nFoundations Trading — alpaca_manager live test (read-only)")
    print("=" * 60)

    cfg = cm.load_config()
    if not cfg.get('alpaca_key'):
        print("No keys found. Run setup_keys.py first.")
        sys.exit(1)

    a = AlpacaManager()

    # 1. Connect
    print("\n[1] Connect")
    r = a.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                  paper=cfg.get('paper_trading', True))
    print(f"  {r['status']}: {r['message']}")
    if r['status'] != 'ok':
        sys.exit(1)

    # 2. Account
    print("\n[2] Account")
    acct = a.get_account()
    print(f"  equity: ${acct['equity']:,.2f}")
    print(f"  cash: ${acct['cash']:,.2f}")
    print(f"  buying power: ${acct['buying_power']:,.2f}")
    print(f"  status: {acct['status']}")
    print(f"  options level: {acct['options_level']}")

    # 3. Clock
    print("\n[3] Market clock")
    clk = a.get_clock()
    print(f"  open now: {clk['is_open']}")
    print(f"  next open: {clk['next_open']}")
    print(f"  next close: {clk['next_close']}")

    # 4. Bars
    print(f"\n[4] Bars — {TICKER}")
    for tf in ('10Min', '1Hour', '1Day'):
        bars = a.get_bars(TICKER, timeframe=tf, limit=50)
        if bars:
            print(f"  {tf}: {len(bars)} bars, "
                  f"latest close ${bars[-1]['close']:.2f} at {bars[-1]['time']}")
        else:
            print(f"  {tf}: NO BARS — problem if market has been open recently")

    # 5. Quote
    print(f"\n[5] Quote — {TICKER}")
    q = a.get_quote(TICKER)
    if q:
        print(f"  bid ${q['bid']:.2f} / ask ${q['ask']:.2f} / mid ${q['mid']:.2f}")
        spot = q['mid']
    else:
        print("  No quote (market closed?). Using last bar close as spot.")
        bars = a.get_bars(TICKER, '1Day', limit=1)
        spot = bars[-1]['close'] if bars else None
        if spot is None:
            print("  Can't establish a spot price. Stopping.")
            sys.exit(1)

    # 6. Options chain with Greeks
    print(f"\n[6] Options chain — {TICKER} (strikes ±4% of ${spot:.2f}, DTE 4–26)")
    chain = a.get_options_chain(TICKER, spot)
    print(f"  contracts returned: {len(chain)}")
    if chain:
        with_greeks = [c for c in chain if c['delta'] is not None]
        print(f"  contracts with Greeks: {len(with_greeks)}")
        c = with_greeks[0] if with_greeks else chain[0]
        print(f"  sample: {c['symbol']}")
        print(f"    {c['type']} ${c['strike']} exp {c['expiry']}")
        print(f"    bid {c['bid']} / ask {c['ask']} / mid {c['mid']} "
              f"(spread {c['spread_pct']}%)")
        print(f"    OI {c['open_interest']}  IV {c['iv']}")
        print(f"    delta {c['delta']}  gamma {c['gamma']}  theta {c['theta']}  "
              f"vega {c['vega']}")
    else:
        print("  Empty chain — expected only if market data is unavailable.")

    # 7. Options quote round-trip
    if chain:
        print(f"\n[7] Single option quote")
        oq = a.get_options_quote(chain[0]['symbol'])
        if oq:
            print(f"  {chain[0]['symbol']}: bid {oq['bid']} / ask {oq['ask']} "
                  f"/ mid {oq['mid']}")
        else:
            print("  No quote returned (may be stale outside market hours).")

    # 8. GEX
    print(f"\n[8] GEX — {TICKER}")
    gex = a.get_gex(TICKER, spot)
    if gex:
        print(f"  total: {gex['gex_total']:,.0f}")
        print(f"  calls: {gex['gex_calls']:,.0f}   puts: {gex['gex_puts']:,.0f}")
        print(f"  contracts in computation: {gex['n_contracts']}")
    else:
        print("  GEX unavailable (empty chain).")

    # 9. Broker positions
    print("\n[9] Open broker positions")
    pos = a.get_positions()
    if pos:
        for p in pos:
            print(f"  {p['symbol']}: {p['qty']} @ {p['avg_entry_price']} "
                  f"(P/L {p['unrealized_pl']})")
    else:
        print("  none (clean paper account)")

    a.disconnect()
    print("\n" + "=" * 60)
    print("DONE — all read-only calls exercised. No orders placed.")


if __name__ == '__main__':
    main()
