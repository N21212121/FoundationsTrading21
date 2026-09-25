"""
engine_contract.py - Foundations Trading

The boundary between "what to trade" (strategy engines) and "how to trade it"
(app.py orchestration + trade_router + alpaca_manager). Engines compute however
they like; they emit ONE decision in a FIXED vocabulary. The router understands
that vocabulary and nothing else.

Change an engine's internals freely. Never change this contract without changing
the consumers in lockstep. That discipline is the point: the plumbing you paid
for (options router, reconciler, naked-short guards, order log, INVERT mirror)
never moves again.
"""
from abc import ABC, abstractmethod

# The only actions downstream code understands. Note app.py's INVERT_SIGNALS
# mirror does string surgery on these names (LONG<->SHORT); any new action MUST
# keep LONG/SHORT spellable that way.
ACTIONS = {
    'NONE',
    'ENTER_LONG', 'ENTER_SHORT',
    'EXIT_LONG', 'EXIT_SHORT',
    'EXIT_LONG_THEN_ENTER_SHORT', 'EXIT_SHORT_THEN_ENTER_LONG',
}

# Keys every decision must carry. 'gate', 'step2', 'exit_kind', 'volume_ok'
# are consumed by app.py's logging/entry paths; engines that don't compute
# them must still emit them (None / True defaults) so the log stays uniform.
_REQUIRED_KEYS = ('ticker', 'action', 'gate', 'step2', 'exit_kind',
                  'volume_ok')

# How the router must fill an entry. The engine names the SHAPE of the trade;
# trade_router owns the mechanics. A reversion engine that gets filled with
# calls will be destroyed by theta and IV crush long before its edge shows up,
# so this is not a preference, it is part of the strategy.
EXECUTIONS = {
    'options_combo',   # long = calls + shares, short = puts only (Ripster)
    'shares_only',     # shares both ways; no options leg, ever (OU)
}

# What an engine's assumptions require of a name. Returned by characterize().
VERDICTS = {'trending', 'reverting', 'random_walk', 'unknown'}

# OPTIONAL decision key. `exit_kind` tells the ROUTER how to treat the exit
# (structural = stop, blocks same-day rebuy). `exit_tag` tells YOU which of an
# engine's several structural stops actually fired. Group reports by exit_tag;
# never branch execution on it.


def validate_decision(decision):
    """Fail loud on a malformed decision. Returns it unchanged if OK.
    Given the naked-short history, a decision that fails the contract stops
    the line here rather than leaking a bad action into execution."""
    if not isinstance(decision, dict):
        raise ValueError(f'decision must be a dict, got {type(decision)}')
    for k in _REQUIRED_KEYS:
        if k not in decision:
            raise ValueError(f'decision missing required key: {k!r}')
    if decision['action'] not in ACTIONS:
        raise ValueError(f'unknown action: {decision["action"]!r}')
    return decision


class StrategyEngine(ABC):
    """Base class for a plug-and-play strategy.

    Contract:
      name         Stable id. Registry key; appears in logs.
      timeframes   role -> bar spec, e.g. {'primary': '10Min', 'macro': '1Hour'}.
                   Tells the data layer what streams this engine consumes.
                   'primary' is mandatory: it is the stream whose bar close
                   drives evaluation and dedupe.
      history      role -> bar count the engine wants delivered. The batched
                   fetch sizes requests from the max across a basket.
      execution    One of EXECUTIONS. The trade SHAPE this strategy needs.
      evaluate     Bars in, one validated decision out. PURE. No broker calls,
                   no I/O, no threads, NO PER-TICKER STATE on the instance.
      characterize Bars in, one verdict out. Do this engine's assumptions
                   hold on this name at all? MEASURES; never selects.

    Statelessness matters for the 1,000-name sweep: one shared instance
    evaluates every name with no contention. Per-ticker state (held direction,
    no-rebuy flags) stays in app.py where it lives now. Engines decide; they
    do not remember.

    `position` is how a stateless engine gets the little history it needs
    (when the trade opened) WITHOUT holding state. The caller owns the fact;
    the engine is handed a copy each call. A time stop is impossible without
    it, and a mean-reversion engine without a time stop has no stop at all:
    price stops are backwards for reversion, since a position moving against
    you has MORE expected edge, not less.
    """

    name: str = 'base'
    timeframes: dict = {'primary': '10Min'}
    history: dict = {'primary': 300}
    execution: str = 'options_combo'

    @abstractmethod
    def evaluate(self, ticker, bars, position_direction=None, now_et=None,
                 position=None):
        """
        bars               dict keyed by the roles in self.timeframes, each a
                           list of bar dicts (alpaca_manager format), oldest
                           first, closed bars only. Missing roles arrive as
                           None; engines must fail closed on thin data.
        position_direction 'long' | 'short' | None (currently held).
        now_et             injected clock for backtest determinism;
                           None = live wall clock.
        position           dict or None. When a position is open, carries at
                           least {'opened_at': <ISO or 'YYYY-MM-DD HH:MM:SS'
                           in ET>}. Engines that need no history ignore it.

        Returns a decision dict that passes validate_decision().
        """
        raise NotImplementedError

    def characterize(self, ticker, bars):
        """Do this engine's assumptions hold on this name?

        Returns {'verdict': one of VERDICTS, 'reason': str, ...}. Extra keys
        are engine-specific diagnostics.

        This exists because a backtest measures P&L, and P&L arrives one
        TRADE at a time; twenty trades on a thin edge tell you nothing. A
        characterizer measures the autocorrelation of returns, and that
        arrives one BAR at a time. Three orders of magnitude more samples,
        same fetch. So characterize first, backtest the survivors.

        MEASURES, NEVER SELECTS. Do not build a loop that ranks engines per
        ticker and mounts the winner: that is one overfit per name, across
        the whole universe. Two buckets and an abstention, read by a human.
        """
        return {'verdict': 'unknown', 'reason': 'engine has no characterizer'}
