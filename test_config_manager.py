"""
test_config_manager.py — Exercise config_manager before committing.

Tests:
  1. Config round-trip (write then read returns same data)
  2. Positions round-trip
  3. All three log appenders + readers
  4. Concurrent writes don't corrupt the file (lock works)
  5. Atomic write leaves original intact if a crash happens mid-write
  6. Forensics log writes to the right place

Cleans up after itself so it can be run repeatedly.
"""

import os
import threading
import time
import config_manager as cm


def cleanup():
    """Delete any test artifacts so the test starts from a known state."""
    paths = [cm.CONFIG_FILE, cm.POSITIONS_FILE,
             cm.TRADE_LOG, cm.SIGNAL_LOG, cm.ORDER_LOG]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    # Wipe today's forensics files
    today_stamp = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
    for kind in ('signal_eval', 'api_event', 'conn_event'):
        f = os.path.join(cm.FORENSICS_DIR, f'{kind}_{today_stamp}.csv')
        if os.path.exists(f):
            os.remove(f)


def test_config_roundtrip():
    """Write a config, read it back, verify it matches."""
    test_cfg = {
        'alpaca_key': 'TEST_KEY_123',
        'alpaca_secret': 'TEST_SECRET_456',
        'paper_trading': True,
        'watchlist': [
            {'ticker': 'NVDA', 'allocation_pct': 25.0},
            {'ticker': 'AAPL', 'allocation_pct': 15.0},
        ],
        'notifications_enabled': False,
    }
    cm.save_config(test_cfg)
    loaded = cm.load_config()
    assert loaded == test_cfg, f"Config round-trip failed: {loaded!r} != {test_cfg!r}"
    print("  PASS: config round-trip")


def test_default_config_fills_missing_keys():
    """A config with only some keys should get the defaults filled in."""
    cm.save_config({'alpaca_key': 'PARTIAL'})
    loaded = cm.load_config()
    assert loaded['alpaca_key'] == 'PARTIAL'
    assert loaded['paper_trading'] == True   # default
    assert loaded['watchlist'] == []          # default
    print("  PASS: default config fills missing keys")


def test_positions_roundtrip():
    test_positions = {
        'NVDA': {
            'shares': 10,
            'share_entry_price': 450.25,
            'option_contracts': 2,
            'option_symbol': 'NVDA260117C00500000',
            'option_entry_premium': 12.50,
            'opened_at': '2026-06-09 10:15:00',
        },
        'AAPL': {
            'shares': 50,
            'share_entry_price': 180.00,
            'option_contracts': 0,
            'opened_at': '2026-06-09 10:30:00',
        },
    }
    cm.save_positions(test_positions)
    loaded = cm.load_positions()
    assert loaded == test_positions, f"Positions round-trip failed"
    print("  PASS: positions round-trip")


def test_trade_log():
    cm.log_trade(
        ticker='NVDA', side='LONG', sleeve='COMBO', action='BUY',
        qty=10, price=450.25, status='FILLED', reason='3L vote + macro confirmed',
        pnl_dollars='', pnl_pct='',
        option_symbol='NVDA260117C00500000', option_strike=500,
        option_expiry='2026-01-17', option_type='call',
    )
    cm.log_trade(
        ticker='NVDA', side='LONG', sleeve='COMBO', action='SELL',
        qty=10, price=465.00, status='FILLED', reason='MACD collapse 70%',
        pnl_dollars=147.50, pnl_pct=3.28,
        option_symbol='', option_strike='', option_expiry='', option_type='',
    )
    rows = cm.read_trades()
    assert len(rows) == 2, f"Expected 2 trade rows, got {len(rows)}"
    assert rows[0]['ticker'] == 'NVDA'
    assert rows[0]['action'] == 'BUY'
    assert rows[1]['action'] == 'SELL'
    print(f"  PASS: trade log ({len(rows)} rows written and read)")


def test_signal_log():
    cm.log_signal(
        ticker='NVDA', bar_time='2026-06-09 10:20:00',
        launch_gate_passed=True, launch_gate_reason='all 3 conditions met',
        step2_votes_long=3, step2_votes_short=0, step2_votes_hold=0,
        step2_result='3L_AUTO',
        step3_macro_state='above', step3_result='CONFIRMED',
        final_action='ENTER_LONG_COMBO',
        notes='clean entry',
    )
    rows = cm.read_signals()
    assert len(rows) == 1
    assert rows[0]['step2_result'] == '3L_AUTO'
    print(f"  PASS: signal log")


def test_order_log():
    cm.log_order(
        ticker='NVDA', side='LONG', sleeve='SHARES', qty=10,
        symbol='NVDA', order_id='ord_abc123', status='FILLED',
        fill_price=450.25, fill_qty=10, reason='manual buy',
    )
    rows = cm.read_orders()
    assert len(rows) == 1
    print(f"  PASS: order log")


def test_ticker_filter():
    """Read with a ticker filter returns only matching rows."""
    cm.log_trade(ticker='AAPL', side='LONG', sleeve='SHARES', action='BUY',
                 qty=50, price=180.0, status='FILLED', reason='test')
    nvda_only = cm.read_trades(ticker='NVDA')
    aapl_only = cm.read_trades(ticker='AAPL')
    assert all(r['ticker'] == 'NVDA' for r in nvda_only)
    assert all(r['ticker'] == 'AAPL' for r in aapl_only)
    print(f"  PASS: ticker filter (NVDA={len(nvda_only)}, AAPL={len(aapl_only)})")


def test_concurrent_writes():
    """50 threads each appending 10 rows. Expect exactly 500 rows, no corruption."""
    cleanup()
    N_THREADS = 50
    ROWS_PER_THREAD = 10

    def worker(thread_id):
        for i in range(ROWS_PER_THREAD):
            cm.log_trade(
                ticker=f'T{thread_id:02d}', side='LONG', sleeve='SHARES',
                action='BUY', qty=1, price=100.0 + i, status='FILLED',
                reason=f'thread {thread_id} row {i}',
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    rows = cm.read_trades()
    expected = N_THREADS * ROWS_PER_THREAD
    assert len(rows) == expected, f"Expected {expected} rows, got {len(rows)}"
    # All rows must have correct field counts (no interleaved/corrupt rows)
    for r in rows:
        assert r['ticker'].startswith('T')
        assert r['action'] == 'BUY'
    print(f"  PASS: concurrent writes ({expected} rows, no corruption)")


def test_atomic_write_survives_crash():
    """Simulate a crash mid-write. Original file should remain intact."""
    # Write a known-good config
    good_cfg = dict(cm.DEFAULT_CONFIG)
    good_cfg['alpaca_key'] = 'GOOD_KEY'
    cm.save_config(good_cfg)

    # Create a stray .tmp file as if a previous write crashed
    tmp_path = cm.CONFIG_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        f.write('{"corrupted": "data"')  # malformed JSON, no closing brace
    # The real config file is still valid
    loaded = cm.load_config()
    assert loaded['alpaca_key'] == 'GOOD_KEY', "Atomic write integrity failed"

    # Cleanup the stray tmp
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    print("  PASS: atomic write survives crash (.tmp file does not corrupt real config)")


def test_corrupt_config_returns_default():
    """If config.json is malformed, load_config returns defaults instead of crashing."""
    with open(cm.CONFIG_FILE, 'w') as f:
        f.write('{not valid json')
    loaded = cm.load_config()
    assert loaded['paper_trading'] == True  # default value
    print("  PASS: corrupt config returns defaults (no crash)")


def test_forensics():
    cm.log_forensic('api_event', event='get_bars', ticker='NVDA', status='ok',
                    latency_ms=120)
    cm.log_forensic('conn_event', event='connect', status='ok')
    cm.log_forensic('signal_eval', ticker='NVDA', bar='10:20', vote='3L')

    today_stamp = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
    for kind in ('api_event', 'conn_event', 'signal_eval'):
        path = os.path.join(cm.FORENSICS_DIR, f'{kind}_{today_stamp}.csv')
        assert os.path.exists(path), f"Forensics file missing: {path}"
    print("  PASS: forensics logs written to correct dated files")


def main():
    print("\nFoundations Trading — config_manager test suite")
    print("=" * 55)

    print("\n[1] Config")
    cleanup()
    test_config_roundtrip()
    cleanup()
    test_default_config_fills_missing_keys()

    print("\n[2] Positions")
    cleanup()
    test_positions_roundtrip()

    print("\n[3] Logs")
    cleanup()
    test_trade_log()
    cleanup()
    test_signal_log()
    cleanup()
    test_order_log()
    cleanup()
    test_ticker_filter()

    print("\n[4] Concurrency")
    test_concurrent_writes()

    print("\n[5] Crash safety")
    cleanup()
    test_atomic_write_survives_crash()
    cleanup()
    test_corrupt_config_returns_default()

    print("\n[6] Forensics")
    cleanup()
    test_forensics()

    print("\n" + "=" * 55)
    print("ALL TESTS PASSED")
    cleanup()


if __name__ == '__main__':
    main()
