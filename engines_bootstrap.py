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
from engine_ou import FAMILY as OU_FAMILY, OUReversionEngine

registry = EngineRegistry()
registry.register(RipsterEMACloudEngine())
# The OU family: one engine per bar size. A half-life of 2-12 BARS is a
# different amount of real time at each, so each variant sees a different
# slice of the universe. Assignment is deterministic (assign.py) and never
# looks at P&L.
for _cls in OU_FAMILY:
    registry.register(_cls())
registry.register(OUReversionEngine())      # legacy alias == ou_reversion_1d

# Global default: every ticker runs the faithful Ripster engine until told
# otherwise. Overrides, when you want them:
#
#   resolver.set_basket('swing_sleeve', 'ripster_swing')
#   resolver.assign('AAPL', 'swing_sleeve')
#   resolver.set_ticker('NVDA', 'gex_scalper')
#
# The two engines hold OPPOSITE theses and must never both be mounted on the
# same ticker. Use characterize() to sort names into a trending sleeve and a
# reverting sleeve, read the table yourself, and assign. Never let a loop rank
# engines per ticker and mount the winner: that is one overfit per name.
#
resolver = EngineResolver(registry, default_name='ripster_ema_cloud')
