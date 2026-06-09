"""
setup_keys.py — One-time Alpaca credential setup.

Prompts for keys in the terminal and stores them in config.json
(which lives in LocalAppData and is gitignored). Run again any time
you regenerate keys.
"""

import config_manager as cm


def main():
    print("Foundations Trading — Alpaca key setup")
    print("=" * 45)
    cfg = cm.load_config()

    current = cfg.get('alpaca_key', '')
    if current:
        print(f"A key is already stored (ends in ...{current[-4:]}).")
        if input("Replace it? (y/n): ").strip().lower() != 'y':
            print("Keeping existing keys. Nothing changed.")
            return

    key = input("Paste your Alpaca API key: ").strip()
    secret = input("Paste your Alpaca secret: ").strip()
    if not key or not secret:
        print("Empty key or secret. Nothing saved.")
        return

    cfg['alpaca_key'] = key
    cfg['alpaca_secret'] = secret
    cfg['paper_trading'] = True   # paper by default, always
    cm.save_config(cfg)
    print(f"\nSaved to {cm.CONFIG_FILE}")
    print("paper_trading = True (live requires explicit change later, in the UI)")


if __name__ == '__main__':
    main()
