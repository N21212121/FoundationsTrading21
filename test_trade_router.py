"""
test_trade_router.py — Synthetic tests of sizing, strike selection,
planning, and every monitor trigger. No orders placed. Part 2 does one
live read-only strike pick against the real NVDA chain.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import trade_router as tr
import config_manager as cm

ET = ZoneInfo('America/New_York')
AFTERNOON = datetime.now(ET).replace(hour=13, minute=0, second=0)
MORNING = datetime.now(ET).replace(hour=9, minute=45, second=0)


def _chain(spot=100.0):
    """Synthetic chain: calls and puts at several strikes/expiries."""
    today = datetime.now(ET).date()
    out = []
    for dte in (5, 8, 10, 12, 24):
        exp = today + timedelta(days=dte)
        for strike in (96, 98, 100, 102, 104):
            for typ in ('call', 'put'):
                mid = max(0.5, abs(spot - strike) * 0.3 + 2.0)
                out.append({'symbol': f'TEST{exp:%y%m%d}{typ[0].upper()}{strike:08d}',
                            'type': typ, 'strike': float(strike),
                            'expiry': exp.strftime('%Y-%m-%d'),
                            'bid': mid * 0.97, 'ask': mid * 1.03,
                            'mid': mid, 'spread_pct': 6.0,
                            'open_interest': 100, 'iv': 0.4,
                            'delta': 0.5, 'gamma': 0.02, 'theta': -0.1,
                            'vega': 0.08, 'rho': 0.01})
    return out


def test_fill_spill():
    s = tr.compute_fill_spill(1000, 2.0)     # $200/contract -> 5 contracts, $0 spill
    assert s['contracts'] == 5 and s['share_dollars'] == 0.0
    s = tr.compute_fill_spill(1000, 3.0)     # $300/contract -> 3 contracts, $100 spill
    assert s['contracts'] == 3 and s['share_dollars'] == 100.0
    s = tr.compute_fill_spill(150, 2.0)      # can't afford one -> all shares
    assert s['contracts'] == 0 and s['share_dollars'] == 150.0
    s = tr.compute_fill_spill(1000, 0)       # no usable mid -> all shares
    assert s['contracts'] == 0 and s['share_dollars'] == 1000.0
    print("  PASS: fill-and-spill sizing")


def test_pick_contract():
    chain = _chain()
    c = tr.pick_contract(chain, 'call', spot=100.0)
    assert c is not None and c['type'] == 'call'
    assert abs(c['strike'] - 100.0) < 2.1          # near the money
    dte = (datetime.strptime(c['expiry'], '%Y-%m-%d').date()
           - datetime.now(ET).date()).days
    assert 4 <= dte <= 26
    # wide-spread contracts get rejected
    wide = [dict(x, spread_pct=20.0) for x in chain]
    assert tr.pick_contract(wide, 'call', 100.0) is None
    print(f"  PASS: strike selection (picked {c['strike']} exp {c['expiry']}, dte {dte})")


def test_plan_entry_long_combo():
    # ATM call mid is 2.00 -> $200/contract. Budget 2150 -> 10 contracts
    # + $150 spill -> 1 share at spot 100.
    p = tr.plan_entry('long', budget_dollars=2150, spot=100.0, chain=_chain())
    kinds = [l['kind'] for l in p['legs']]
    assert kinds[0] == 'option', "options leg must execute first (Policy A)"
    assert 'shares' in kinds, f"expected share spill leg, got {p}"
    opt = p['legs'][0]
    sh = [l for l in p['legs'] if l['kind'] == 'shares'][0]
    assert opt['contracts'] == 10
    assert sh['qty'] == 1
    # And the exact-divide case: no spill -> options only, and that's CORRECT
    p2 = tr.plan_entry('long', budget_dollars=2000, spot=100.0, chain=_chain())
    kinds2 = [l['kind'] for l in p2['legs']]
    assert kinds2 == ['option'], "exact divide should produce options-only"
    print("  PASS: long combo plan (spill -> share leg; exact divide -> options only)")


def test_plan_entry_long_no_chain():
    p = tr.plan_entry('long', budget_dollars=2000, spot=100.0, chain=[])
    assert len(p['legs']) == 1 and p['legs'][0]['kind'] == 'shares'
    assert p['legs'][0]['qty'] == 20
    print("  PASS: long with empty chain -> 100% shares")


def test_plan_entry_short_puts_only():
    p = tr.plan_entry('short', budget_dollars=2000, spot=100.0, chain=_chain())
    assert all(l['kind'] == 'option' for l in p['legs'])
    assert p['legs'][0]['type'] == 'put'
    # short with no chain -> no trade (shares are long-only)
    p2 = tr.plan_entry('short', budget_dollars=2000, spot=100.0, chain=[])
    assert p2['legs'] == []
    print("  PASS: short -> puts only; no puts -> no trade")


def _pos(**kw):
    base = {'shares': 10, 'share_entry_price': 100.0,
            'option_contracts': 2, 'option_symbol': 'TESTOPT',
            'option_entry_premium': 5.0,
            'peak_premium': 0.0, 'peak_macd_spread': 0.0,
            'direction': 'long'}
    base.update(kw)
    return base


def test_share_stop():
    r = tr.check_position(_pos(), share_price=94.9, option_premium=5.0,
                          macd_spread_abs=1.0, now_et=AFTERNOON)
    assert r['exit'] and r['exit']['sleeve'] == 'shares' and r['exit']['is_stop']
    print("  PASS: share stop fires at -5.1%")


def test_option_stop():
    r = tr.check_position(_pos(), share_price=100.0, option_premium=4.6,
                          macd_spread_abs=1.0, now_et=AFTERNOON)
    assert r['exit'] and r['exit']['sleeve'] == 'options' and r['exit']['is_stop']
    print("  PASS: option stop fires at -8%")


def test_stops_suppressed_before_10():
    r = tr.check_position(_pos(), share_price=90.0, option_premium=4.0,
                          macd_spread_abs=1.0, now_et=MORNING)
    # -10% share loss, -20% option loss — both stops would fire, but it's 9:45
    assert r['exit'] is None or not r['exit']['is_stop']
    print("  PASS: stops suppressed before 10:00 ET")


def test_premium_trail():
    # Armed: peak 10.0 > entry 5.0, premium falls to 5.9 (<=60% of peak) -> fire
    pos = _pos(peak_premium=10.0)        # entry premium is 5.0 in _pos
    r = tr.check_position(pos, share_price=100.0, option_premium=5.9,
                          macd_spread_abs=1.0, now_et=AFTERNOON)
    assert r['exit'] and 'premium trail' in r['exit']['trigger']
    assert not r['exit']['is_stop']
    # Above the trail line -> no fire
    r2 = tr.check_position(_pos(peak_premium=10.0), 100.0, 6.1, 1.0,
                           now_et=AFTERNOON)
    assert r2['exit'] is None or 'premium trail' not in (r2['exit'] or {}).get('trigger', '')
    # NOT armed: peak 5.05 barely above... no — peak must EXCEED entry.
    # Entry 5.0, peak never above 5.0, premium collapses to 2.9 (would be
    # 60% of a 4.9 peak) -> trail must NOT fire. Option stop (-7% = 4.65)
    # is the exit path instead, and at 2.9 the stop fires.
    pos3 = _pos(peak_premium=4.9)        # peaked BELOW entry
    r3 = tr.check_position(pos3, share_price=100.0, option_premium=2.9,
                           macd_spread_abs=1.0, now_et=AFTERNOON)
    assert r3['exit'] is not None and r3['exit']['is_stop'], \
        "below-entry collapse must exit via STOP, not trail"
    assert 'option stop' in r3['exit']['trigger']
    # Same below-entry peak, morning (stops off), small dip: nothing fires
    r4 = tr.check_position(_pos(peak_premium=4.9), 100.0, 4.8, 1.0,
                           now_et=MORNING)
    assert r4['exit'] is None
    print("  PASS: premium trail armed only above entry; below-entry exits via stop")


def test_macd_collapse():
    pos = _pos(peak_macd_spread=2.0, option_entry_premium=5.0)
    r = tr.check_position(pos, share_price=100.0, option_premium=5.0,
                          macd_spread_abs=1.39, now_et=AFTERNOON)
    assert r['exit'] and r['exit']['sleeve'] == 'both'
    assert 'MACD collapse' in r['exit']['trigger']
    print("  PASS: MACD collapse at <=70% of peak exits both sleeves")


def test_peak_tracking_and_reset():
    pos = _pos()
    r = tr.check_position(pos, 100.0, option_premium=7.5, macd_spread_abs=1.5,
                          now_et=AFTERNOON)
    assert r['peak_premium'] == 7.5 and r['peak_macd_spread'] == 1.5
    pos['peak_premium'], pos['peak_macd_spread'] = 7.5, 1.5
    tr.reset_peaks_on_add(pos)
    assert pos['peak_premium'] == 0.0 and pos['peak_macd_spread'] == 0.0
    print("  PASS: peak tracking updates; reset on add")


def test_no_rebuy_flags():
    flags = {}
    tr.mark_stop_out(flags, 'NVDA', 'shares', now_et=AFTERNOON)
    assert tr.rebuy_blocked(flags, 'NVDA', 'shares', now_et=AFTERNOON)
    assert not tr.rebuy_blocked(flags, 'NVDA', 'options', now_et=AFTERNOON)
    tomorrow = AFTERNOON + timedelta(days=1)
    assert not tr.rebuy_blocked(flags, 'NVDA', 'shares', now_et=tomorrow)
    print("  PASS: stop-out blocks same-day rebuy per sleeve; clears next day")


def live_strike_pick():
    from alpaca_manager import AlpacaManager
    cfg = cm.load_config()
    if not cfg.get('alpaca_key'):
        print("\n[live] no keys; skipping")
        return
    a = AlpacaManager()
    if a.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                 paper=cfg.get('paper_trading', True))['status'] != 'ok':
        print("\n[live] connect failed; skipping")
        return
    q = a.get_quote('NVDA')
    if not q:
        print("\n[live] no quote; skipping")
        a.disconnect()
        return
    chain = a.get_options_chain('NVDA', q['mid'])
    call = tr.pick_contract(chain, 'call', q['mid'])
    put = tr.pick_contract(chain, 'put', q['mid'])
    print(f"\nNVDA spot {q['mid']:.2f}, chain {len(chain)} contracts")
    for label, c in (('CALL', call), ('PUT', put)):
        if c:
            print(f"  {label}: {c['symbol']}  ${c['strike']} exp {c['expiry']}  "
                  f"mid {c['mid']} spread {c['spread_pct']}%  delta {c['delta']}")
        else:
            print(f"  {label}: none qualified")
    plan = tr.plan_entry('long', budget_dollars=5000, spot=q['mid'], chain=chain)
    print(f"  $5000 long combo plan: {plan['reason']}")
    for leg in plan['legs']:
        print(f"    {leg}")
    a.disconnect()


def main():
    print("\nFoundations Trading — trade_router tests")
    print("=" * 60)
    test_fill_spill()
    test_pick_contract()
    test_plan_entry_long_combo()
    test_plan_entry_long_no_chain()
    test_plan_entry_short_puts_only()
    test_share_stop()
    test_option_stop()
    test_stops_suppressed_before_10()
    test_premium_trail()
    test_macd_collapse()
    test_peak_tracking_and_reset()
    test_no_rebuy_flags()
    print("\nALL SYNTHETIC TESTS PASSED")
    print("\n" + "=" * 60)
    print("LIVE: strike pick + plan against real NVDA chain (read-only)")
    live_strike_pick()
    print("\nDONE")


if __name__ == '__main__':
    main()
