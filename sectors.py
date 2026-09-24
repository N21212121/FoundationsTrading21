"""
sectors.py - Foundations Trading

Read-only access to the hardcoded stock universe in sectors.json.

The universe is a NOUN, like baskets, but a static one: hand-built from the
NASDAQ listing, ETFs/SPACs/rights/units/notes/preferreds stripped, grouped
sector -> subsector -> [tickers]. It is a candidate POOL, not a recommendation.
The screener judges which names the mounted engine actually trades well; this
file only says which names exist in a theme.

Shape:
  { "Technology": { "Semiconductors": ["NVDA", ...], "General": [...] },
    "Energy":     { "General": [...] }, ... }

Sector labels here are organizational convenience, not gospel. The source CSV
categorized loosely (airlines and Apple both landed in "Technology"). That is
fine: the backtest scores each ticker on its own merits regardless of bucket.
Reads through config_manager so all JSON I/O goes one door.
"""
import os

import config_manager as cm

SECTORS_FILE = os.path.join(cm.DATA_DIR, 'sectors.json')


def load_sectors():
    """{sector: {subsector: [tickers]}}. Missing/corrupt -> {}."""
    data = cm._read_json(SECTORS_FILE, {})
    return data if isinstance(data, dict) else {}


def list_sectors():
    """[(sector, ticker_count)] sorted by size descending."""
    s = load_sectors()
    out = [(sec, sum(len(v) for v in subs.values())) for sec, subs in s.items()]
    return sorted(out, key=lambda t: -t[1])


def subsectors(sector):
    """{subsector: ticker_count} for one sector, or {} if unknown."""
    return {sub: len(v) for sub, v in load_sectors().get(sector, {}).items()}


def tickers_for(sector=None, subsector=None):
    """Flat, de-duplicated ticker list.

    sector=None            -> the ENTIRE universe
    sector set             -> that whole sector
    sector + subsector set -> just that subsector

    Raises KeyError on an unknown sector/subsector so a typo fails loud
    instead of silently screening nothing."""
    s = load_sectors()
    if sector is None:
        seen, out = set(), []
        for subs in s.values():
            for syms in subs.values():
                for t in syms:
                    if t not in seen:
                        seen.add(t)
                        out.append(t)
        return out
    if sector not in s:
        raise KeyError(f'unknown sector {sector!r}; have: {list(s)}')
    subs = s[sector]
    if subsector is not None:
        if subsector not in subs:
            raise KeyError(f'unknown subsector {subsector!r} in {sector!r}; '
                           f'have: {list(subs)}')
        return list(dict.fromkeys(subs[subsector]))
    seen, out = set(), []
    for syms in subs.values():
        for t in syms:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def sector_of(ticker):
    """(sector, subsector) for a ticker, or (None, None) if not in the
    universe. Linear scan; fine for a one-off lookup, not a hot loop."""
    t = ticker.upper()
    for sec, subs in load_sectors().items():
        for sub, syms in subs.items():
            if t in syms:
                return sec, sub
    return None, None
