"""
Market data via Alpaca's free data API.

Uses the paper-trading credentials. Data is delayed on the free tier
(15 minutes for US equities via the IEX feed) — that is fine for
position tracking and stop alerts, and you should know it rather than
assume you are seeing the live tape.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

DATA_URL = "https://data.alpaca.markets/v2"
TRADE_URL = "https://paper-api.alpaca.markets/v2"

_CACHE: dict[str, tuple[float, float]] = {}   # ticker -> (price, fetched_at)
CACHE_TTL = 20.0                              # seconds


class MarketError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        raise MarketError(
            "ALPACA_API_KEY / ALPACA_API_SECRET are not set. "
            "Add them to your .env file."
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }


def get_price(ticker: str, use_cache: bool = True) -> float:
    """Latest trade price for one ticker."""
    ticker = ticker.upper().strip()
    now = time.time()

    if use_cache and ticker in _CACHE:
        price, fetched = _CACHE[ticker]
        if now - fetched < CACHE_TTL:
            return price

    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{ticker}/trades/latest",
            headers=_headers(),
            params={"feed": "iex"},
            timeout=10,
        )
    except requests.RequestException as e:
        raise MarketError(f"Network error fetching {ticker}: {e}") from e

    if r.status_code == 404:
        raise MarketError(f"'{ticker}' isn't a ticker Alpaca recognises.")
    if r.status_code in (401, 403):
        raise MarketError("Alpaca rejected your API keys. Check your .env file.")
    if r.status_code == 429:
        raise MarketError("Hit Alpaca's rate limit. Backing off.")
    if not r.ok:
        raise MarketError(f"Alpaca error {r.status_code}: {r.text[:200]}")

    data = r.json()
    price = data.get("trade", {}).get("p")
    if price is None:
        raise MarketError(f"No recent trade data for {ticker}.")

    _CACHE[ticker] = (float(price), now)
    return float(price)


def get_prices(tickers: list[str]) -> dict[str, float]:
    """Batch fetch. One request for many tickers — far kinder to rate limits."""
    tickers = sorted({t.upper().strip() for t in tickers})
    if not tickers:
        return {}

    try:
        r = requests.get(
            f"{DATA_URL}/stocks/trades/latest",
            headers=_headers(),
            params={"symbols": ",".join(tickers), "feed": "iex"},
            timeout=15,
        )
    except requests.RequestException as e:
        raise MarketError(f"Network error fetching prices: {e}") from e

    if not r.ok:
        raise MarketError(f"Alpaca error {r.status_code}: {r.text[:200]}")

    out: dict[str, float] = {}
    now = time.time()
    for sym, trade in r.json().get("trades", {}).items():
        if trade and trade.get("p") is not None:
            out[sym] = float(trade["p"])
            _CACHE[sym] = (out[sym], now)
    return out


# Minutes in a US regular trading session.
_SESSION_MINUTES = 390


def _timeframe_minutes(timeframe: str) -> int:
    """'5Min' -> 5, '4Hour' -> 240, '1Day' -> 390 (one session)."""
    tf = timeframe.strip().lower()
    digits = "".join(c for c in tf if c.isdigit())
    n = int(digits) if digits else 1
    if "day" in tf:
        return _SESSION_MINUTES * n
    if "hour" in tf:
        return 60 * n
    return n                                    # minutes


def _lookback_days(timeframe: str, limit: int) -> int:
    """
    How many CALENDAR days back to ask for, to actually receive `limit` bars.

    THE BUG THIS FIXES
    ------------------
    `limit` is a maximum, not a request for history. Sent without a `start`
    date, Alpaca returns roughly the current day only — so a request for 60
    four-hour bars came back with 2, the scanner needed 7, and it returned
    None every single minute for two days. Silently: no alert, no log, no
    way to tell it apart from a dead bot.

    Weekends are covered by the 7/5 factor and public holidays by the
    constant, both deliberately generous. Over-fetching costs one API call;
    under-fetching costs every signal.
    """
    per_bar = _timeframe_minutes(timeframe)
    bars_per_session = max(1.0, _SESSION_MINUTES / per_bar)
    trading_days = max(1.0, limit / bars_per_session)
    calendar_days = int(trading_days * 7 / 5) + 7
    return max(5, min(calendar_days, 1500))       # ~4 years, hard ceiling


def get_bars(ticker: str, timeframe: str = "5Min", limit: int = 100,
             start: str | None = None) -> list:
    """
    Historical bars for the strategy engine.

    timeframe: "1Min" | "5Min" | "15Min" | "1Hour" | "4Hour" | "1Day"
    start:     ISO date (YYYY-MM-DD). Computed from the timeframe when
               omitted — see _lookback_days for why that matters.

    Returns strategy.Bar objects, oldest first.
    """
    from datetime import datetime, timedelta, timezone as _tz
    from strategy import Bar

    if start is None:
        back = _lookback_days(timeframe, limit)
        start = (datetime.now(_tz.utc) - timedelta(days=back)).date().isoformat()

    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{ticker.upper()}/bars",
            headers=_headers(),
            params={"timeframe": timeframe, "limit": limit,
                    "start": start,
                    "feed": "iex", "adjustment": "raw"},
            timeout=15,
        )
    except requests.RequestException as e:
        raise MarketError(f"Network error fetching bars for {ticker}: {e}") from e

    if r.status_code in (401, 403):
        raise MarketError("Alpaca rejected your API keys.")
    if not r.ok:
        raise MarketError(f"Alpaca error {r.status_code}: {r.text[:200]}")

    raw = r.json().get("bars") or []
    bars = [Bar(ts=b["t"], open=b["o"], high=b["h"],
                low=b["l"], close=b["c"], volume=b.get("v", 0))
            for b in raw]
    # A wider start window can return more than asked for; the strategy
    # wants the most RECENT bars, not the oldest.
    return bars[-limit:] if len(bars) > limit else bars


def get_bars_multi(tickers: list, timeframe: str = "1Day",
                   days_back: int = 220) -> dict:
    """
    Daily bars for many symbols in a few requests instead of one per symbol.

    Returns {ticker: [Bar, ...]}. Symbols the feed doesn't recognise are
    simply absent from the result rather than raising.
    """
    from datetime import datetime, timedelta, timezone as _tz
    from strategy import Bar

    start = (datetime.now(_tz.utc) - timedelta(days=days_back)).date().isoformat()
    out: dict = {}

    for batch in [tickers[i:i + 100] for i in range(0, len(tickers), 100)]:
        page_token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": timeframe,
                "start": start,
                "feed": "iex",
                "adjustment": "raw",
                "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token

            try:
                r = requests.get(f"{DATA_URL}/stocks/bars", headers=_headers(),
                                 params=params, timeout=30)
            except requests.RequestException as e:
                raise MarketError(f"Network error fetching bars: {e}") from e

            if r.status_code == 429:
                raise MarketError("Hit Alpaca's rate limit mid-scan.")
            if not r.ok:
                raise MarketError(f"Alpaca error {r.status_code}: {r.text[:200]}")

            payload = r.json()
            for sym, rows in (payload.get("bars") or {}).items():
                out.setdefault(sym, []).extend(
                    Bar(ts=b["t"], open=b["o"], high=b["h"], low=b["l"],
                        close=b["c"], volume=b.get("v", 0))
                    for b in rows
                )

            page_token = payload.get("next_page_token")
            if not page_token:
                break

    return out


def previous_day_range(ticker: str) -> tuple:
    """(high, low) of the previous trading day."""
    # limit=3 with no start date returned today only -> (None, None), so
    # previous-day highs and lows never made it into the liquidity levels.
    daily = get_bars(ticker, "1Day", limit=10)
    if len(daily) < 2:
        return (None, None)
    prev = daily[-2]
    return (prev.high, prev.low)


def session_range(ticker: str) -> tuple:
    """
    (high, low) so far in TODAY's session.

    Pins start to today explicitly. Without that it would inherit the
    widened default lookback and report a multi-day range as "the session",
    which would put liquidity levels in the wrong place.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    bars = get_bars(ticker, "5Min", limit=200, start=today_et)
    if not bars:
        return (None, None)
    return (max(b.high for b in bars), min(b.low for b in bars))


def market_is_open() -> bool:
    """Ask Alpaca whether the US market is currently open."""
    try:
        r = requests.get(f"{TRADE_URL}/clock", headers=_headers(), timeout=10)
        if r.ok:
            return bool(r.json().get("is_open", False))
    except (requests.RequestException, MarketError):
        pass
    return False


def account_summary() -> dict:
    """Paper account balances, so /status can show real numbers."""
    r = requests.get(f"{TRADE_URL}/account", headers=_headers(), timeout=10)
    if not r.ok:
        raise MarketError(f"Alpaca error {r.status_code}: {r.text[:200]}")
    d = r.json()
    return {
        "equity": float(d.get("equity", 0)),
        "cash": float(d.get("cash", 0)),
        "buying_power": float(d.get("buying_power", 0)),
        "daytrade_count": int(d.get("daytrade_count", 0)),
        "pattern_day_trader": bool(d.get("pattern_day_trader", False)),
    }


def validate_credentials() -> tuple[bool, str]:
    """Called at startup so failures are obvious rather than mysterious."""
    try:
        acct = account_summary()
    except MarketError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"Unexpected error contacting Alpaca: {e}"
    return True, (
        f"Connected to Alpaca paper account — "
        f"equity ${acct['equity']:,.2f}, "
        f"{acct['daytrade_count']} day trades used this window."
    )
