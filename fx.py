"""
Converting any currency into GBP.

Bob's account is in pounds. The pairs are not: EUR/USD settles in dollars,
USD/JPY in yen, AUD/USD in dollars again. Position sizing and every figure
on screen has to cross that gap, so this module turns one unit of any
quote currency into pounds.

FX moves far too slowly intraday to matter for sizing, so each rate is
cached for an hour. If both providers are unreachable the bot uses a
hardcoded fallback and says so, loudly, rather than quietly showing
numbers that are wrong.
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from config import CONFIG

# currency -> (rate_to_gbp, fetched_at, is_live)
_cache: dict[str, tuple[float, float, bool]] = {}

PRIMARY = "https://api.frankfurter.app/latest"
BACKUP = "https://open.er-api.com/v6/latest/"

# Only used when both providers are unreachable. Rough mid-2026 levels —
# good enough to keep the bot running and reporting, not good enough to
# size a real position on, which is why the warning is shouted.
FALLBACK_TO_GBP = {
    "GBP": 1.0,
    "USD": CONFIG.display.fx_fallback_rate,   # from .env, default 0.79
    "EUR": 0.855,
    "JPY": 0.0052,
    "AUD": 0.515,
    "NZD": 0.475,
    "CAD": 0.575,
    "CHF": 0.905,
}


def rate_to_gbp(ccy: str) -> tuple[float, bool]:
    """
    What one unit of `ccy` is worth in GBP, and whether that came from a
    live feed. is_live=False means the fallback table is in use.
    """
    ccy = ccy.strip().upper()
    if ccy == "GBP":
        return 1.0, True

    ttl = CONFIG.display.fx_cache_minutes * 60
    hit = _cache.get(ccy)
    if hit and (time.time() - hit[1]) < ttl:
        return hit[0], hit[2]

    for fetch in (_try_frankfurter, _try_erapi):
        r = fetch(ccy)
        if r:
            _cache[ccy] = (r, time.time(), True)
            return r, True

    fallback = FALLBACK_TO_GBP.get(ccy)
    if fallback is None:
        raise RuntimeError(
            f"No live rate for {ccy}->GBP and no fallback configured. "
            f"Refusing to guess at a conversion that decides position size.")
    _cache[ccy] = (fallback, time.time(), False)
    return fallback, False


def _try_frankfurter(ccy: str) -> Optional[float]:
    try:
        r = requests.get(PRIMARY, params={"from": ccy, "to": "GBP"}, timeout=8)
        if r.ok:
            return float(r.json()["rates"]["GBP"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _try_erapi(ccy: str) -> Optional[float]:
    try:
        r = requests.get(BACKUP + ccy, timeout=8)
        if r.ok:
            return float(r.json()["rates"]["GBP"])
    except Exception:  # noqa: BLE001
        pass
    return None


def convert(amount: float, ccy: str) -> float:
    """Turn an amount of `ccy` into GBP."""
    rate, _ = rate_to_gbp(ccy)
    return amount * rate


def from_gbp(amount_gbp: float, ccy: str) -> float:
    """The other direction: GBP into `ccy`."""
    rate, _ = rate_to_gbp(ccy)
    return amount_gbp / rate if rate else 0.0


# ---------------------------------------------------------------------------
# Per-instrument conversion
# ---------------------------------------------------------------------------

def quote_currency(ticker: str) -> str:
    """
    The currency a position in `ticker` makes and loses money in.

    EUR/USD pays out in dollars, USD/JPY in yen. Getting this wrong is not
    a rounding error: converting a yen profit at the dollar rate overstates
    it by a factor of about 150. A ticker that is not a currency pair —
    SPY, say — is assumed to be dollar-denominated, which it is.
    """
    try:
        import pairs
        return pairs.parse(ticker).quote
    except Exception:  # noqa: BLE001
        return "USD"


def pnl_to_gbp(amount: float, ticker: str) -> float:
    """Convert a profit or loss on `ticker` into pounds."""
    return convert(amount, quote_currency(ticker))


# ---------------------------------------------------------------------------
# USD shortcuts, kept because most of the codebase still calls these
# ---------------------------------------------------------------------------

def usd_to_gbp_rate() -> tuple[float, bool]:
    return rate_to_gbp("USD")


def to_gbp(usd: float) -> float:
    return convert(usd, "USD")


def to_usd(gbp: float) -> float:
    return from_gbp(gbp, "USD")


def gbp(amount_usd: float, decimals: int = 2) -> str:
    """Format a USD figure as a GBP string."""
    return f"£{to_gbp(amount_usd):,.{decimals}f}"


def gbp_direct(amount_gbp: float, decimals: int = 2) -> str:
    """Format an amount that is already GBP."""
    return f"£{amount_gbp:,.{decimals}f}"


def rate_note(ccy: str = "USD") -> str:
    ccy = ccy.strip().upper()
    if ccy == "GBP":
        return "Account currency"
    rate, live = rate_to_gbp(ccy)
    if live:
        return f"{ccy}/GBP {rate:.4f}"
    return (f"⚠️ {ccy}/GBP {rate:.4f} — **fallback rate**, FX feed "
            f"unreachable. Figures may be off.")
