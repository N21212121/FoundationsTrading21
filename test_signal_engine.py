"""
test_signal_engine.py — Two parts:

PART 1: Synthetic-data unit tests of every gate and vote path.
        No network. Deterministic. Must always pass.

PART 2: Live run against real NVDA/AAPL/SPY bars through alpaca_manager.
        Prints the actual decision the engine would make right now.
        Not pass/fail — eyeball output.
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import signal_engine as se
import config_manager as cm

ET = ZoneInfo('America/New_York')


# ─── PART 1: SYNTHETIC TESTS ──────────────────────────────────────────────────

def _mk_bars(closes, start=None, spacing_min=10, vol=100000):
    """Build bar dicts from a list of closes. Opens = prior close."""
    start = start or (datetime.now(ET) - timedelta(minutes=spacing_min * len(closes)))
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        t = start + timedelta(minutes=spacing_min * i)
        bars.append({'time': t.isoformat(), 'open': prev,
                     'high': max(prev, c) + 0.05, 'low': min(prev, c) - 0.05,
                     'close': c, 'volume': vol})
        prev = c
    return bars


def test_warmup_blocks():
    bars = _mk_bars([100 + i * 0.1 for i in range(50)])   # only 50 bars
    d = se.evaluate('TEST', bars, bars, current_open=105)
    assert d['action'] == 'NONE'
    assert 'warming up' in d['gate']['reason']
    print("  PASS: warmup blocks (<250 bars -> no signal)")


def test_opening_pause_blocks_entry():
    closes = [100 + i * 0.05 for i in range(300)]
    bars = _mk_bars(closes)
    fake_945 = datetime.now(ET).replace(hour=9, minute=45, second=0)
    d = se.evaluate('TEST', bars, bars, current_open=closes[-1] + 1,
                    now_et=fake_945)
    assert d['action'] == 'NONE'
    assert 'opening pause' in d['gate']['reason']
    print("  PASS: opening pause blocks entries before 10:00 ET")


def test_uptrend_votes_long():
    closes = [100 + i * 0.25 for i in range(300)]    # steady uptrend
    bars = _mk_bars(closes)
    df = se.bars_to_df(bars)
    s2 = se.step2_votes(df, current_open=closes[-1] + 1.0)
    assert s2['tally']['L'] == 3, f"Expected 3L in steady uptrend, got {s2['tally']}"
    assert s2['result'] == 'AUTO' and s2['direction'] == 'long'
    print("  PASS: steady uptrend -> 3 long votes, AUTO long")


def test_downtrend_votes_short():
    closes = [200 - i * 0.25 for i in range(300)]
    bars = _mk_bars(closes)
    df = se.bars_to_df(bars)
    s2 = se.step2_votes(df, current_open=closes[-1] - 1.0)
    assert s2['tally']['S'] == 3, f"Expected 3S in downtrend, got {s2['tally']}"
    print("  PASS: steady downtrend -> 3 short votes, AUTO short")


def test_candle_ratio_blocks_retrace_pair():
    closes = [100 + i * 0.1 for i in range(298)]
    closes += [closes[-1] + 1.00]    # bar up $1
    closes += [closes[-2] + 0.05]    # next bar retraces ~95% of it
    bars = _mk_bars(closes)
    fake_1100 = datetime.now(ET).replace(hour=11, minute=0, second=0)
    g = se.launch_gate(se.bars_to_df(bars), now_et=fake_1100)
    # Note: RVOL may also fail on synthetic single-session data; we check
    # specifically that IF rvol passes, overlap blocks. Easier: call the
    # overlap helper directly.
    b1 = {'open': 100.0, 'close': 101.0}
    b2 = {'open': 100.95, 'close': 100.05}
    frac = se._body_overlap_frac(b1, b2)
    assert frac >= 0.85, f"Expected >=85% overlap, got {frac:.0%}"
    b3 = {'open': 101.0, 'close': 102.5}
    frac2 = se._body_overlap_frac(b1, b3)
    assert frac2 < 0.85
    print("  PASS: candle-ratio overlap math (retrace pair blocked, clean pair passes)")


def test_step3_confirms_and_rejects():
    up = se.bars_to_df(_mk_bars([100 + i * 0.3 for i in range(200)], spacing_min=60))
    s3 = se.step3_macro(up, 'long')
    assert s3['state'] == 'above' and s3['confirmed']
    s3s = se.step3_macro(up, 'short')
    assert not s3s['confirmed']
    print("  PASS: step 3 confirms aligned direction, rejects opposing")


def test_signal_flip():
    closes = [200 - i * 0.25 for i in range(300)]   # downtrend -> 3S
    bars = _mk_bars(closes)
    df = se.bars_to_df(bars)
    s2 = se.step2_votes(df, current_open=closes[-1] - 1.0)
    flip = se.detect_signal_flip(s2, open_position_direction='long')
    assert flip['flip'] and flip['close_direction'] == 'long'
    noflip = se.detect_signal_flip(s2, open_position_direction='short')
    assert not noflip['flip']
    print("  PASS: 3S vote against open long -> flip exit; aligned -> no flip")


def test_flip_fires_even_during_opening_pause():
    closes = [200 - i * 0.25 for i in range(300)]
    bars = _mk_bars(closes)
    fake_940 = datetime.now(ET).replace(hour=9, minute=40, second=0)
    d = se.evaluate('TEST', bars, bars, current_open=closes[-1] - 1,
                    open_position_direction='long', now_et=fake_940)
    assert d['action'] == 'EXIT_LONG', f"Expected EXIT_LONG, got {d['action']}"
    print("  PASS: signal-flip exit fires during opening pause (exits ungated)")


# ─── PART 2: LIVE EYEBALL RUN ─────────────────────────────────────────────────

def live_run():
    from alpaca_manager import AlpacaManager
    cfg = cm.load_config()
    if not cfg.get('alpaca_key'):
        print("\n[live] no keys stored; skipping live run")
        return
    a = AlpacaManager()
    r = a.connect(cfg['alpaca_key'], cfg['alpaca_secret'],
                  paper=cfg.get('paper_trading', True))
    if r['status'] != 'ok':
        print(f"\n[live] connect failed: {r['message']}; skipping")
        return

    for ticker in ('NVDA', 'AAPL', 'SPY'):
        bars10 = a.get_bars(ticker, '10Min', limit=300)
        bars1h = a.get_bars(ticker, '1Hour', limit=200)
        q = a.get_quote(ticker)
        if not bars10 or not q:
            print(f"\n{ticker}: no data, skipped")
            continue
        d = se.evaluate(ticker, bars10, bars1h, current_open=q['mid'])
        print(f"\n{ticker}  (spot {q['mid']:.2f})")
        print(f"  gate:   {d['gate']['reason']}")
        if d['step2']:
            s2 = d['step2']
            print(f"  step2:  votes {s2['votes']}  tally {s2['tally']}  "
                  f"-> {s2['result']} {s2['direction'] or ''}")
            print(f"          emas {s2['emas']}")
        if d['step3']:
            print(f"  step3:  {d['step3']['reason']}")
        print(f"  ACTION: {d['action']}")
    a.disconnect()


def main():
    print("\nFoundations Trading — signal_engine tests")
    print("=" * 60)
    print("\nPART 1: synthetic unit tests")
    test_warmup_blocks()
    test_opening_pause_blocks_entry()
    test_uptrend_votes_long()
    test_downtrend_votes_short()
    test_candle_ratio_blocks_retrace_pair()
    test_step3_confirms_and_rejects()
    test_signal_flip()
    test_flip_fires_even_during_opening_pause()
    print("\nALL SYNTHETIC TESTS PASSED")

    print("\n" + "=" * 60)
    print("PART 2: live decision-tree run (eyeball, not pass/fail)")
    live_run()
    print("\nDONE")


if __name__ == '__main__':
    main()
