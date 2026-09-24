"""
engine_ripster.py - Foundations Trading

Engine #1. A thin adapter over the existing, faithful signal_engine module.

The Ripster logic is UNCHANGED and stays in signal_engine.py. This class only
maps the shared contract onto signal_engine.evaluate(). History depths mirror
what app.py fetched before the refactor (300 x 10-min, 200 x 1-hour) so the
engine sees exactly the data it saw yesterday.
"""
import signal_engine as se

from engine_contract import StrategyEngine, validate_decision


class RipsterEMACloudEngine(StrategyEngine):
    name = 'ripster_ema_cloud'

    # 'macro' is consumed only when se.REQUIRE_1H is on, but declaring it means
    # flipping that flag needs no loop change. The /api/bars chart also draws
    # the 1H cloud, so the stream is not wasted.
    timeframes = {'primary': '10Min', 'macro': '1Hour'}
    history = {'primary': 300, 'macro': 200}
    execution = 'options_combo'   # long = calls + shares, short = puts only

    def evaluate(self, ticker, bars, position_direction=None, now_et=None,
                 position=None):
        decision = se.evaluate(
            ticker=ticker,
            bars10=bars.get('primary'),
            bars1h=bars.get('macro'),
            current_open=None,               # ignored by the engine, by design
            open_position_direction=position_direction,
            now_et=now_et,
        )
        # `position` is unused: Ripster's exits are structural (a close through
        # a cloud), never time-based, so it needs no entry timestamp.
        return validate_decision(decision)

    def characterize(self, ticker, bars):
        """Ripster's premise is that moves PERSIST. Same statistic as the OU
        engine, opposite verdict. MEASURES. Never selects."""
        import halflife as hlf
        primary = bars.get('primary') if isinstance(bars, dict) else bars
        if not primary:
            return {'verdict': 'unknown', 'reason': 'no bars'}
        cls = hlf.classify(primary)
        cls['tradeable'] = cls['verdict'] == 'trending'
        return cls
