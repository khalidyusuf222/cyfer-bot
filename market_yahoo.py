"""
Market data from Yahoo Finance — no account, no API key, no ID.

Drop-in replacement for market.py. Same function names, same return types,
so nothing else in the codebase changes.

The trade-off, stated honestly:
  + No signup of any kind. Install the package and it works.
  + Same OHLC bars the strategy needs, on every timeframe it uses.
  - Unofficial. yfinance reads Yahoo's public endpoints, so Yahoo can
    change them and break it without warning.
  - Rate limited. Fine for two tickers on a 60-second loop; don't scan fifty.
  - No account balance, because there's no account.

If it ever breaks, switch DATA_SOURCE back to alpaca in .env.
"""

from __future__ import annotations

import time
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_price_cache: dict[str, tuple[float, float]] = {}
CACHE_TTL = 20.0


class MarketError(RuntimeError):
    pass


def _yf():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise MarketError(
            "yfinance isn't installed. Run: pip install yfinance"
        ) from e


# Alpaca timeframe names -> yfinance intervals, so config.py needs no changes.
_INTERVAL = {
    "1Min": "1m",
    "5Min": "5m",
    "15Min": "15m",
    "1Hour": "1h",
    "4Hour": "1h",     # resampled below — Yahoo has no native 4h
    "1Day": "1d",
}

# How much history to request for each interval. Yahoo caps intraday history:
# 1m is only available for the last 7 days, 5m for 60 days.
_PERIOD = {
    "1m": "5d",
    "5m": "30d",
    "15m": "60d",
    "1h": "180d",
    "1d": "2y",
}


def get_bars(ticker: str, timeframe: str = "5Min", limit: int = 100) -> list:
    """Historical bars, oldest first, as strategy.Bar objects."""
    from strategy import Bar

    yf = _yf()
    interval = _INTERVAL.get(timeframe)
    if interval is None:
        raise MarketError(f"Unsupported timeframe: {timeframe}")

    try:
        df = yf.Ticker(ticker.upper()).history(
            period=_PERIOD[interval], interval=interval, auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        raise MarketError(f"Yahoo error fetching {ticker}: {e}") from e

    if df is None or df.empty:
        raise MarketError(f"No data returned for {ticker}.")

    # Yahoo has no 4-hour bars, so build them from hourly.
    if timeframe == "4Hour":
        df = df.resample("4h").agg({
            "Open": "first", "High": "max",
            "Low": "min", "Close": "last", "Volume": "sum",
        }).dropna()

    df = df.tail(limit)

    return [Bar(ts=str(idx), open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]),
                volume=float(r.get("Volume", 0) or 0))
            for idx, r in df.iterrows()]


def get_price(ticker: str, use_cache: bool = True) -> float:
    """Most recent price."""
    ticker = ticker.upper().strip()
    now = time.time()

    if use_cache and ticker in _price_cache:
        price, fetched = _price_cache[ticker]
        if now - fetched < CACHE_TTL:
            return price

    yf = _yf()
    try:
        t = yf.Ticker(ticker)
        info = getattr(t, "fast_info", None)
        price = None
        if info is not None:
            price = (info.get("last_price") if hasattr(info, "get")
                     else getattr(info, "last_price", None))
        if not price:
            hist = t.history(period="1d", interval="1m")
            if hist is None or hist.empty:
                raise MarketError(f"No recent price for {ticker}.")
            price = float(hist["Close"].iloc[-1])
    except MarketError:
        raise
    except Exception as e:  # noqa: BLE001
        raise MarketError(f"Yahoo error fetching {ticker}: {e}") from e

    price = float(price)
    _price_cache[ticker] = (price, now)
    return price


def get_prices(tickers: list[str]) -> dict[str, float]:
    """Batch fetch. Yahoo handles several symbols in one download."""
    tickers = sorted({t.upper().strip() for t in tickers})
    if not tickers:
        return {}

    yf = _yf()
    out: dict[str, float] = {}
    try:
        data = yf.download(" ".join(tickers), period="1d", interval="1m",
                           progress=False, group_by="ticker", auto_adjust=False)
        now = time.time()
        for t in tickers:
            try:
                series = data[t]["Close"] if len(tickers) > 1 else data["Close"]
                series = series.dropna()
                if len(series):
                    out[t] = float(series.iloc[-1])
                    _price_cache[t] = (out[t], now)
            except (KeyError, IndexError):
                continue
    except Exception:  # noqa: BLE001
        # Fall back to one at a time rather than failing the whole scan.
        for t in tickers:
            try:
                out[t] = get_price(t)
            except MarketError:
                continue

    return out


def get_bars_multi(tickers: list, timeframe: str = "1Day",
                   days_back: int = 220) -> dict:
    """Daily bars for many symbols. Yahoo handles batches natively."""
    from strategy import Bar

    yf = _yf()
    out: dict = {}
    interval = _INTERVAL.get(timeframe, "1d")

    for batch in [tickers[i:i + 100] for i in range(0, len(tickers), 100)]:
        try:
            data = yf.download(" ".join(batch), period=f"{days_back}d",
                               interval=interval, progress=False,
                               group_by="ticker", auto_adjust=False,
                               threads=True)
        except Exception:  # noqa: BLE001
            continue

        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                df = df.dropna()
                if df.empty:
                    continue
                out[t] = [
                    Bar(ts=str(idx), open=float(r["Open"]), high=float(r["High"]),
                        low=float(r["Low"]), close=float(r["Close"]),
                        volume=float(r.get("Volume", 0) or 0))
                    for idx, r in df.iterrows()
                ]
            except (KeyError, IndexError, TypeError):
                continue

    return out


def previous_day_range(ticker: str) -> tuple:
    """(high, low) of the previous trading day."""
    daily = get_bars(ticker, "1Day", limit=3)
    if len(daily) < 2:
        return (None, None)
    prev = daily[-2]
    return (prev.high, prev.low)


def session_range(ticker: str) -> tuple:
    """(high, low) so far in today's session."""
    bars = get_bars(ticker, "5Min", limit=78)   # ~1 session of 5m bars
    if not bars:
        return (None, None)
    return (max(b.high for b in bars), min(b.low for b in bars))


def market_is_open() -> bool:
    """
    US equities regular hours, computed rather than asked.

    No broker API here, so this doesn't know about market holidays. On a
    holiday it will think the market is open and simply find no fresh bars,
    which is harmless — no setup can fire without data.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 0)


def account_summary() -> dict:
    """No account exists with Yahoo. Zeros, so callers don't crash."""
    return {
        "equity": 0.0,
        "cash": 0.0,
        "buying_power": 0.0,
        "daytrade_count": 0,
        "pattern_day_trader": False,
        "source": "yahoo",
    }


def validate_credentials() -> tuple[bool, str]:
    """Nothing to validate — just confirm data actually comes back."""
    try:
        bars = get_bars("SPY", "1Day", limit=2)
    except MarketError as e:
        return False, f"Yahoo Finance unreachable: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"Unexpected error reaching Yahoo Finance: {e}"

    if not bars:
        return False, "Yahoo Finance returned no data for SPY."
    return True, (f"Connected to Yahoo Finance — no account needed. "
                  f"SPY last close ${bars[-1].close:,.2f}.")
