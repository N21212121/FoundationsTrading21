"""
engine_registry.py - Foundations Trading

Two jobs.

  Registry   name -> engine instance. One place that knows every engine.

  Resolver   ticker -> engine, layered lookup, first hit wins:
                 1. per-ticker override
                 2. per-basket default
                 3. global default
             "Global" is just "no override set." Per-ticker is one line when
             you want it; you never re-architect to move one symbol.

fetch_plan() is what makes mixed baskets cheap: it groups symbols by the
(timeframe, role) streams their engines actually consume, so the batched data
pull requests each stream only for the names that need it.
"""


class EngineRegistry:
    def __init__(self):
        self._engines = {}

    def register(self, engine):
        if engine.name in self._engines:
            raise ValueError(f'engine already registered: {engine.name!r}')
        self._engines[engine.name] = engine
        return engine

    def get(self, name):
        if name not in self._engines:
            raise KeyError(f'no engine registered as {name!r}')
        return self._engines[name]

    def names(self):
        return sorted(self._engines)


class EngineResolver:
    """Decides which engine runs for a given ticker.

    The common case, one strategy for the whole basket, is default_name and
    nothing else. by_ticker / by_basket exist for the day NVDA runs one engine
    and the ETF sleeve runs another, with no code change.
    """

    def __init__(self, registry, default_name):
        self.registry = registry
        self.registry.get(default_name)      # validate now, fail loud
        self.default_name = default_name
        self.by_ticker = {}          # ticker -> engine name
        self.by_basket = {}          # basket name -> engine name
        self.ticker_basket = {}      # ticker -> basket name (optional)

    def set_ticker(self, ticker, engine_name):
        self.registry.get(engine_name)
        self.by_ticker[ticker.upper()] = engine_name

    def clear_ticker(self, ticker):
        """Remove a per-ticker override; the name falls back through
        basket/global resolution."""
        self.by_ticker.pop(ticker.upper(), None)

    def set_basket(self, basket, engine_name):
        self.registry.get(engine_name)
        self.by_basket[basket] = engine_name

    def assign(self, ticker, basket):
        self.ticker_basket[ticker.upper()] = basket

    def resolve(self, ticker):
        t = ticker.upper()
        name = self.by_ticker.get(t)
        if name is None:
            basket = self.ticker_basket.get(t)
            name = self.by_basket.get(basket) if basket else None
        if name is None:
            name = self.default_name
        return self.registry.get(name)

    def fetch_plan(self, tickers):
        """Group a basket by the data streams its engines consume.

        Returns {(timeframe, role): {'symbols': [...], 'limit': int}}.

        Keyed by (timeframe, role) rather than role alone so two engines are
        free to call different timeframes by the same role name ('primary' =
        10Min for intraday, 1Day for swing) without colliding. limit is the
        max any engine in the group wants, so one batched request serves all.
        """
        plan = {}
        for t in tickers:
            eng = self.resolve(t)
            for role, tf in eng.timeframes.items():
                key = (tf, role)
                slot = plan.setdefault(key, {'symbols': [], 'limit': 0})
                slot['symbols'].append(t)
                slot['limit'] = max(slot['limit'],
                                    eng.history.get(role, 0))
        return plan
