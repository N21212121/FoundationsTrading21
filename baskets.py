"""
baskets.py - Foundations Trading

The basket NOUN. First-class data object, stored in baskets.json next to the
rest of the config. Three consumers: the live loop (deploy to watchlist), the
backtester (borrows the universe), and the future Setup docs (references
engines by name).

No builder UI yet. Until that tab exists, baskets.json is hand-editable:

{
  "semis_intraday": {
    "symbols": ["NVDA", "AMD", "AVGO"],
    "engine": "ripster_ema_cloud",
    "default_allocation_pct": 1.0,
    "notes": "Ripster news-play semis sleeve"
  }
}

This module is deliberately dumb: load, get, list, save. Validation of engine
names happens at the consumer (resolver / backtester), which knows the
registry. The noun does not know about the verbs.
"""
import os

import config_manager as cm

BASKETS_FILE = os.path.join(cm.DATA_DIR, 'baskets.json')


def load_baskets():
    """{name: basket dict}. Missing or corrupt file -> empty dict (a corrupt
    baskets file must not kill the app; it just means no baskets). Reads
    through config_manager so all JSON I/O goes one door."""
    data = cm._read_json(BASKETS_FILE, {})
    return data if isinstance(data, dict) else {}


def get_basket(name):
    """One basket, normalized: symbols uppercased and deduped, order kept.
    Raises KeyError listing what exists, so a typo'd basket name fails loud
    with the fix in the message."""
    baskets = load_baskets()
    if name not in baskets:
        raise KeyError(f'no basket {name!r}; available: '
                       f'{sorted(baskets) or "(none)"}')
    b = dict(baskets[name])
    seen, syms = set(), []
    for s in b.get('symbols', []):
        u = s.upper()
        if u not in seen:
            seen.add(u)
            syms.append(u)
    b['symbols'] = syms
    return b


def list_baskets():
    return sorted(load_baskets())


def save_baskets(baskets):
    """Atomic write through config_manager, so basket writes take the same
    process-wide _WRITE_LOCK as config and positions. No separate write
    implementation to drift, and no basket write can interleave with a
    config or positions write."""
    cm._atomic_write_json(BASKETS_FILE, baskets)
