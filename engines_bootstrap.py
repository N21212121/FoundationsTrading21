"""
engines_bootstrap.py - Foundations Trading

Build the registry and resolver once at import. app.py imports `resolver` and
calls resolver.resolve(ticker) per name; the decision goes to the UNCHANGED
trade_router path.

To add engine #2: write engine_<name>.py implementing StrategyEngine, register
it here, optionally point tickers or a basket at it. Nothing else changes.
"""
from engine_registry import EngineRegistry, EngineResolver
from engine_ripster import RipsterEMACloudEngine

registry = EngineRegistry()
registry.register(RipsterEMACloudEngine())

# THE OU REVERSION FAMILY WAS DELETED 2026-10-01 at Nate's instruction -- a
# deliberate restart before a new strategy is written, not a bug fix. Six
# variants (one per bar size), engine_ou.py, and assign.py went with it.
# What that removed, so nobody looks for it: the half-life estimator's only
# consumer, the /api/assign routes, the Engine Assignment card, and the
# screener's OU pipeline gate. halflife.py itself STAYS -- its TF_MINUTES and
# tf_minutes are bar-width arithmetic the whole app depends on, and only
# estimate/classify/theta_line are now unused.

# Global default: every ticker runs the faithful Ripster engine until told
# otherwise. Overrides, when you want them:
#
#   resolver.set_basket('swing_sleeve', 'ripster_swing')
#   resolver.assign('AAPL', 'swing_sleeve')
#   resolver.set_ticker('NVDA', 'gex_scalper')
#
# ONE engine is registered today. The invariant still stands for whatever
# comes next: never let a loop rank engines per ticker and mount the winner,
# because that is one overfit per name. Measure, assign by arithmetic, freeze.
#
resolver = EngineResolver(registry, default_name='ripster_ema_cloud')
