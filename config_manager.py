"""
config_manager.py — Foundations Trading

Owns:
  - The data directory at %LOCALAPPDATA%\\FoundationsTrading\\
  - Atomic JSON reads/writes with a process-wide lock
  - The config file (API keys, paper/live flag, watchlist, allocations)
  - The positions file (current open positions, by ticker)
  - The three operational logs: trades, signals, orders
  - The forensics log infrastructure (signal_eval, api_event, conn_event)

Does NOT own:
  - Engine rules (stops, profit-take thresholds, gate constants, DTE window).
    Those live as module constants in signal_engine.py and trade_router.py
    so nothing automated can overwrite them at runtime.
"""

import os
import sys
import json
import csv
import threading
from datetime import datetime


# ─── DATA DIRECTORY ────────────────────────────────────────────────────────────

def _resolve_data_dir():
    """Locate %LOCALAPPDATA%\\FoundationsTrading\\, fall back to home dir."""
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA')
        if base:
            return os.path.join(base, 'FoundationsTrading')
        return os.path.join(os.path.expanduser('~'), 'FoundationsTrading')
    return os.path.join(os.path.expanduser('~'), '.foundations_trading')


DATA_DIR = _resolve_data_dir()
FORENSICS_DIR = os.path.join(DATA_DIR, 'forensics')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FORENSICS_DIR, exist_ok=True)


# ─── FILE PATHS ────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
POSITIONS_FILE = os.path.join(DATA_DIR, 'positions.json')

TRADE_LOG = os.path.join(DATA_DIR, 'trade_log.csv')
SIGNAL_LOG = os.path.join(DATA_DIR, 'signal_log.csv')
ORDER_LOG = os.path.join(DATA_DIR, 'order_log.csv')


# ─── LOG SCHEMAS ───────────────────────────────────────────────────────────────

TRADE_LOG_HEADER = [
    'timestamp', 'ticker', 'side', 'sleeve', 'action',
    'qty', 'price', 'status', 'reason', 'pnl_dollars', 'pnl_pct',
    'option_symbol', 'option_strike', 'option_expiry', 'option_type',
]

SIGNAL_LOG_HEADER = [
    'timestamp', 'ticker', 'bar_time',
    'gate_passed', 'gate_reason',
    'trend',           # 34/50 verdict: up / down / chop (Rule 1)
    'trigger',         # 5/12 cross: fresh_long / fresh_short / none (Rule 2)
    'price_vs_5_12',   # close vs the 5/12 cloud: above / below / inside
    'price_vs_34_50',  # close vs the 34/50 cloud: above / below / inside
    'exit_kind',       # structural / ride_end / '' (Rule 3)
    'final_action', 'notes',
]

ORDER_LOG_HEADER = [
    'timestamp', 'ticker', 'side', 'sleeve', 'qty', 'symbol',
    'order_id', 'status', 'fill_price', 'fill_qty', 'reason',
]


# ─── LOCKING ───────────────────────────────────────────────────────────────────

# One lock guards every on-disk JSON write in this process.
# CSV log appends use the same lock because two threads appending to the
# same file at the same time can interleave rows.
_WRITE_LOCK = threading.RLock()


# ─── ATOMIC JSON I/O ───────────────────────────────────────────────────────────

def _atomic_write_json(path, data):
    """Write JSON to a temp file, then rename. Crash-safe.

    A crash mid-write leaves the original file intact because the rename
    is atomic on Windows (os.replace) and the temp file is the only
    casualty.
    """
    tmp = path + '.tmp'
    with _WRITE_LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def _read_json(path, default):
    """Read a JSON file, return default if missing or corrupt."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


# ─── CONFIG ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    'alpaca_key': '',
    'alpaca_secret': '',
    'paper_trading': True,        # paper-by-default. Live requires explicit flip.
    'watchlist': [],              # list of {'ticker': str, 'allocation_pct': float}
    'notifications_enabled': True,
}


def load_config():
    """Load the config file. Missing keys are filled from DEFAULT_CONFIG."""
    cfg = _read_json(CONFIG_FILE, dict(DEFAULT_CONFIG))
    for key, default in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = default
    return cfg


def save_config(cfg):
    """Save the config file atomically."""
    _atomic_write_json(CONFIG_FILE, cfg)


# ─── POSITIONS ─────────────────────────────────────────────────────────────────

def load_positions():
    """Load the positions file. Returns {} if missing."""
    return _read_json(POSITIONS_FILE, {})


def save_positions(positions):
    """Save the positions file atomically."""
    _atomic_write_json(POSITIONS_FILE, positions)


# ─── CSV LOG APPENDS ───────────────────────────────────────────────────────────

def _append_csv_row(path, header, row_dict):
    """Append one row to a CSV. Writes the header on first append."""
    write_header = not os.path.exists(path)
    with _WRITE_LOCK:
        with open(path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header,
                                    extrasaction='ignore')
            if write_header:
                writer.writeheader()
            row_dict.setdefault('timestamp',
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            writer.writerow(row_dict)


def log_trade(**fields):
    """Append a row to trade_log.csv. Pass fields as keyword args."""
    _append_csv_row(TRADE_LOG, TRADE_LOG_HEADER, fields)


def log_signal(**fields):
    """Append a row to signal_log.csv."""
    _append_csv_row(SIGNAL_LOG, SIGNAL_LOG_HEADER, fields)


def log_order(**fields):
    """Append a row to order_log.csv."""
    _append_csv_row(ORDER_LOG, ORDER_LOG_HEADER, fields)


# ─── CSV LOG READS ─────────────────────────────────────────────────────────────

def read_log(path, ticker=None, limit=None):
    """Read a CSV log. Optionally filter by ticker and/or take the last N rows."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    if ticker:
        t = ticker.upper()
        rows = [r for r in rows if (r.get('ticker') or '').upper() == t]
    if limit:
        rows = rows[-limit:]
    return rows


def read_trades(ticker=None, limit=None):
    return read_log(TRADE_LOG, ticker, limit)


def read_signals(ticker=None, limit=None):
    return read_log(SIGNAL_LOG, ticker, limit)


def read_orders(ticker=None, limit=None):
    return read_log(ORDER_LOG, ticker, limit)


# ─── FORENSICS LOGS (rotating, lower priority — stubs for now) ────────────────

def _forensics_path(kind, dated=True):
    """Path for a rotating forensics file. kind is 'signal_eval', 'api_event',
    or 'conn_event'. Files rotate daily by default."""
    stamp = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(FORENSICS_DIR, f'{kind}_{stamp}.csv')


def log_forensic(kind, **fields):
    """Append a row to the appropriate forensics file. kind is one of
    'signal_eval', 'api_event', 'conn_event'. Header is determined by
    the keys in the first row written that day."""
    path = _forensics_path(kind)
    write_header = not os.path.exists(path)
    header = ['timestamp'] + [k for k in fields.keys() if k != 'timestamp']
    with _WRITE_LOCK:
        with open(path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header, extrasaction='ignore')
            if write_header:
                writer.writeheader()
            fields.setdefault('timestamp',
                              datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            writer.writerow(fields)


# ─── DIAGNOSTICS ───────────────────────────────────────────────────────────────

def describe():
    """Print where everything lives. Run this to confirm setup."""
    print(f"DATA_DIR:       {DATA_DIR}")
    print(f"FORENSICS_DIR:  {FORENSICS_DIR}")
    print(f"CONFIG_FILE:    {CONFIG_FILE}")
    print(f"POSITIONS_FILE: {POSITIONS_FILE}")
    print(f"TRADE_LOG:      {TRADE_LOG}")
    print(f"SIGNAL_LOG:     {SIGNAL_LOG}")
    print(f"ORDER_LOG:      {ORDER_LOG}")


if __name__ == '__main__':
    describe()
