"""
Data source switch.

Set DATA_SOURCE in .env:

  oanda   — forex candles and prices from the same account that places the
            orders. Default, and the right answer when BROKER=oanda: the
            prices you analyse are then the prices you trade at.
  yahoo   — no account, no API key. Shares and indices only.
  alpaca  — US shares, needs free paper-trading API keys.

Everything else in the codebase imports from here, so switching sources is
a one-line change in .env and a restart.

A MISMATCH IS WORTH AVOIDING. Analysing Yahoo's EUR/USD and trading OANDA's
means the entry you calculated and the entry you get are from two different
feeds. The default below follows BROKER for exactly that reason.
"""



from __future__ import annotations

from pathlib import Path as _EnvPath
from dotenv import load_dotenv as _load_env
_load_env(_EnvPath(__file__).parent / ".env")


import os

_BROKER = os.environ.get("BROKER", "oanda").strip().lower()
_DEFAULT = "oanda" if _BROKER == "oanda" else "yahoo"
_SOURCE = os.environ.get("DATA_SOURCE", _DEFAULT).strip().lower()

# A forex broker with a shares data source is never a real configuration:
# neither Alpaca nor Yahoo carries EUR_USD in the form the strategy asks
# for, so every scan would fail quietly and the bot would sit there looking
# healthy while never finding a setup.
#
# It is also the EXACT state an old .env produces. The shares-era file has
# DATA_SOURCE=alpaca in it, and moving to a new folder copies it across.
# So rather than trust the setting, the broker wins, and it says so.
DATA_SOURCE_OVERRIDDEN = ""
if _BROKER == "oanda" and _SOURCE != "oanda":
    DATA_SOURCE_OVERRIDDEN = (
        f"DATA_SOURCE={_SOURCE} ignored — BROKER is oanda, and {_SOURCE} "
        f"can't supply forex prices. Using OANDA's own. Remove the "
        f"DATA_SOURCE line from .env to stop this warning.")
    import sys as _sys
    print(f"WARNING: {DATA_SOURCE_OVERRIDDEN}", file=_sys.stderr)
    _SOURCE = "oanda"

if _SOURCE == "oanda":
    import oanda as _backend
elif _SOURCE == "alpaca":
    import market as _backend
elif _SOURCE == "yahoo":
    import market_yahoo as _backend
else:
    raise RuntimeError(
        f"DATA_SOURCE is '{_SOURCE}' — expected 'oanda', 'yahoo' or 'alpaca'."
    )

SOURCE_NAME = _SOURCE

MarketError = _backend.MarketError
get_price = _backend.get_price
get_prices = _backend.get_prices
get_bars = _backend.get_bars
get_bars_multi = _backend.get_bars_multi
previous_day_range = _backend.previous_day_range
session_range = _backend.session_range
market_is_open = _backend.market_is_open
account_summary = _backend.account_summary
validate_credentials = _backend.validate_credentials

# Forex-only. Absent on the share backends, so callers check first.
get_spread_pips = getattr(_backend, "get_spread_pips", None)


def describe() -> str:
    if _SOURCE == "oanda":
        return _backend.describe()
    if _SOURCE == "yahoo":
        return ("Yahoo Finance — free, no account. Unofficial, so it can "
                "break if Yahoo changes things.")
    return "Alpaca — official API, also reports your paper account balance."
