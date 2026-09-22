"""
OANDA v20 — market data and order execution.

WHY OANDA AND NOT METATRADER 5
------------------------------
The book teaches MT5 (p27-33, p54-57). MT5's Python bridge only runs on
Windows and needs the terminal open. The bot lives on a headless Linux VPS,
so MT5 is not an option here.

OANDA exposes a plain REST API that needs nothing installed, offers free
practice accounts, and is FCA-regulated for UK retail. Everything the book
describes doing by hand in MT5 — set the lot size, attach a stop and a
target, press buy — is one HTTP request here.

PRACTICE BY DEFAULT
-------------------
api-fxpractice.oanda.com is a separate host with separate credentials from
the live one. Reaching real money needs a different token, a different
account number, a config change and a restart. It cannot happen by
accident.

A NOTE ON WHAT IS AND ISN'T TESTED
----------------------------------
The parsing, sizing and safety logic below is covered by tests that run
offline. The HTTP calls themselves are not — they cannot be, without
credentials and a live endpoint. The first real order will be the first
time those paths execute. That is what a practice account is for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import pairs

PRACTICE_HOST = "https://api-fxpractice.oanda.com"
LIVE_HOST = "https://api-fxtrade.oanda.com"


class MarketError(RuntimeError):
    pass


class OandaError(MarketError):
    pass


class TradeNotFound(OandaError):
    """OANDA has never heard of this trade id."""


# ===========================================================================
# Connection
# ===========================================================================

def is_live() -> bool:
    """Anything but exactly 'live' means practice."""
    return os.environ.get("OANDA_ENV", "practice").strip().lower() == "live"


def _host() -> str:
    return LIVE_HOST if is_live() else PRACTICE_HOST


def _token() -> str:
    key = "OANDA_LIVE_TOKEN" if is_live() else "OANDA_TOKEN"
    token = os.environ.get(key, "").strip()
    if not token:
        raise OandaError(
            f"{key} is not set in .env. Refusing to trade without it.")
    return token


def _account() -> str:
    key = "OANDA_LIVE_ACCOUNT" if is_live() else "OANDA_ACCOUNT"
    acct = os.environ.get(key, "").strip()
    if not acct:
        raise OandaError(f"{key} is not set in .env.")
    return acct


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept-Datetime-Format": "RFC3339",
    }


def _get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    try:
        r = requests.get(f"{_host()}{path}", headers=_headers(),
                         params=params or {}, timeout=timeout)
    except requests.RequestException as e:
        raise MarketError(f"Network error reaching OANDA: {e}") from e

    if r.status_code in (401, 403):
        raise OandaError("OANDA rejected the token. Check OANDA_TOKEN and "
                         "that OANDA_ENV matches which token you generated.")
    if r.status_code == 404:
        raise TradeNotFound(f"OANDA returned 404 for {path}")
    if not r.ok:
        raise OandaError(f"OANDA error {r.status_code}: {r.text[:300]}")
    return r.json()


def _post(path: str, body: dict, timeout: int = 20) -> dict:
    try:
        r = requests.post(f"{_host()}{path}", headers=_headers(),
                          json=body, timeout=timeout)
    except requests.RequestException as e:
        raise OandaError(f"Network error sending order: {e}") from e
    if r.status_code in (401, 403):
        raise OandaError("OANDA rejected the token.")
    if not r.ok:
        raise OandaError(f"OANDA error {r.status_code}: {r.text[:400]}")
    return r.json()


def _put(path: str, body: dict, timeout: int = 20) -> dict:
    try:
        r = requests.put(f"{_host()}{path}", headers=_headers(),
                         json=body, timeout=timeout)
    except requests.RequestException as e:
        raise OandaError(f"Network error: {e}") from e
    if not r.ok and r.status_code != 404:
        raise OandaError(f"OANDA error {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


# ===========================================================================
# Timeframes
# ===========================================================================

# Our config speaks "5Min"/"1Hour"; OANDA speaks M5/H1.
GRANULARITY = {
    "1min": "M1", "2min": "M2", "4min": "M4", "5min": "M5",
    "10min": "M10", "15min": "M15", "30min": "M30",
    "1hour": "H1", "2hour": "H2", "3hour": "H3", "4hour": "H4",
    "6hour": "H6", "8hour": "H8", "12hour": "H12",
    "1day": "D", "1week": "W", "1month": "M",
}

_MINUTES = {
    "M1": 1, "M2": 2, "M4": 4, "M5": 5, "M10": 10, "M15": 15, "M30": 30,
    "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480,
    "H12": 720, "D": 1440, "W": 10080, "M": 43200,
}


def granularity(timeframe: str) -> str:
    """'5Min' -> 'M5'. Raises rather than guessing."""
    g = GRANULARITY.get(timeframe.strip().lower())
    if g is None:
        raise MarketError(
            f"Unsupported timeframe '{timeframe}'. "
            f"Try one of: {', '.join(sorted(GRANULARITY))}")
    return g


def granularity_minutes(g: str) -> int:
    return _MINUTES.get(g, 60)


# ===========================================================================
# Candles
# ===========================================================================

def _to_bar(candle: dict):
    """One OANDA candle -> our Bar. Mid prices; OANDA sends them as strings."""
    from strategy import Bar
    m = candle.get("mid") or {}
    return Bar(ts=candle.get("time", ""),
               open=float(m.get("o", 0)), high=float(m.get("h", 0)),
               low=float(m.get("l", 0)), close=float(m.get("c", 0)),
               volume=int(candle.get("volume", 0)))


def parse_candles(payload: dict, include_incomplete: bool = False) -> list:
    """
    Pull bars out of a candles response, oldest first.

    The newest candle is usually still forming. An incomplete candle has a
    close that changes every tick, so including it means the strategy reads
    a different chart every minute. Dropped by default.
    """
    out = []
    for c in payload.get("candles", []):
        if not include_incomplete and not c.get("complete", False):
            continue
        out.append(_to_bar(c))
    return out


def get_bars(ticker: str, timeframe: str = "5Min", limit: int = 200,
             start: str | None = None) -> list:
    """
    Historical candles, oldest first.

    OANDA caps `count` at 5000 and will not accept `count` and `from`
    together, so `start` switches to a from/to window.
    """
    pair = pairs.parse(ticker)
    g = granularity(timeframe)

    params = {"granularity": g, "price": "M"}
    if start:
        params["from"] = start
        params["to"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    else:
        params["count"] = min(max(int(limit) + 1, 2), 5000)

    payload = _get(f"/v3/instruments/{pair.symbol}/candles", params)
    bars = parse_candles(payload)
    return bars[-limit:] if len(bars) > limit else bars


def get_bars_multi(tickers: list, timeframe: str = "1Day",
                   days_back: int = 220) -> dict:
    """One request per pair. The watchlist is four pairs, not 503 shares."""
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    out = {}
    for t in tickers:
        try:
            out[pairs.parse(t).symbol] = get_bars(t, timeframe, 5000, start)
        except MarketError:
            continue
    return out


# ===========================================================================
# Live pricing
# ===========================================================================

def parse_prices(payload: dict) -> dict:
    """
    Response -> {symbol: mid price}.

    [BOOK p12] Every quote has two prices. The bid is what you can sell at,
    the ask what you must pay to buy. The mid sits between them; the gap is
    the spread, and it is a real cost (p13).
    """
    out = {}
    for p in payload.get("prices", []):
        sym = p.get("instrument")
        bids, asks = p.get("bids") or [], p.get("asks") or []
        if not sym or not bids or not asks:
            continue
        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
        out[sym] = (bid + ask) / 2
    return out


def parse_spreads(payload: dict) -> dict:
    """{symbol: spread in pips} — the cost of opening a trade right now."""
    out = {}
    for p in payload.get("prices", []):
        sym = p.get("instrument")
        bids, asks = p.get("bids") or [], p.get("asks") or []
        if not sym or not bids or not asks:
            continue
        pair = pairs.parse(sym)
        out[sym] = (float(asks[0]["price"]) - float(bids[0]["price"])) / pair.pip
    return out


def get_prices(tickers: list) -> dict:
    syms = ",".join(pairs.parse(t).symbol for t in tickers)
    payload = _get(f"/v3/accounts/{_account()}/pricing",
                   {"instruments": syms})
    return parse_prices(payload)


def get_price(ticker: str) -> float:
    prices = get_prices([ticker])
    sym = pairs.parse(ticker).symbol
    if sym not in prices:
        raise MarketError(f"No price returned for {sym}.")
    return prices[sym]


def get_spread_pips(ticker: str) -> Optional[float]:
    sym = pairs.parse(ticker).symbol
    payload = _get(f"/v3/accounts/{_account()}/pricing",
                   {"instruments": sym})
    return parse_spreads(payload).get(sym)


def market_is_open() -> bool:
    """
    [BOOK p3] Forex runs 24 hours, 5 days a week — it closes only at the
    weekend. OANDA reports tradeability per instrument, which also covers
    holidays and any halt.
    """
    try:
        payload = _get(f"/v3/accounts/{_account()}/pricing",
                       {"instruments": pairs.DEFAULT_PAIRS[0]})
        prices = payload.get("prices") or []
        return bool(prices and prices[0].get("tradeable", False))
    except (MarketError, OandaError):
        return False


def previous_day_range(ticker: str) -> tuple:
    """(high, low) of the previous complete daily candle."""
    daily = get_bars(ticker, "1Day", limit=5)
    if len(daily) < 2:
        return (None, None)
    prev = daily[-2]
    return (prev.high, prev.low)


def session_range(ticker: str) -> tuple:
    """(high, low) of the last 24 hours."""
    bars = get_bars(ticker, "1Hour", limit=24)
    if not bars:
        return (None, None)
    return (max(b.high for b in bars), min(b.low for b in bars))


# ===========================================================================
# Account
# ===========================================================================

def account_summary() -> dict:
    """
    [BOOK p19-20] Balance is cash. Equity is balance plus or minus open
    positions. Margin used is locked away; free margin is what's left to
    open with and to absorb losses before a margin call.
    """
    payload = _get(f"/v3/accounts/{_account()}/summary")
    a = payload.get("account", {})
    return {
        "currency": a.get("currency", "?"),
        "balance": float(a.get("balance", 0)),
        "equity": float(a.get("NAV", 0)),
        "margin_used": float(a.get("marginUsed", 0)),
        "margin_available": float(a.get("marginAvailable", 0)),
        "unrealised": float(a.get("unrealizedPL", 0)),
        "open_trades": int(a.get("openTradeCount", 0)),
        "environment": "LIVE" if is_live() else "practice",
    }


def validate_credentials() -> tuple:
    try:
        a = account_summary()
    except OandaError as e:
        return (False, f"❌ {e}")
    except MarketError as e:
        return (False, f"❌ Couldn't reach OANDA: {e}")

    tag = "🔴 LIVE" if is_live() else "practice"
    return (True,
            f"Connected to OANDA {tag} account — "
            f"{a['currency']} {a['balance']:,.2f} balance, "
            f"{a['open_trades']} open trade(s).")


# ===========================================================================
# Orders
# ===========================================================================

@dataclass(frozen=True)
class TradeResult:
    trade_id: str
    instrument: str
    units: int
    price: float
    stop: float
    target: float
    environment: str
    raw: dict


def build_order(pair: pairs.Pair, units: int, stop: float,
                target: float) -> dict:
    """
    The order payload, built separately so it can be checked without
    sending anything.

    [BOOK p47] The stop and the target go on WITH the order, not after it.
    stopLossOnFill and takeProfitOnFill attach the moment the trade opens,
    so they exist at the broker even if this bot dies a second later.

    Units are signed: positive is a buy, negative is a sell. [BOOK p10]
    Buying EUR/USD means buying the base and selling the quote.
    """
    if units == 0:
        raise OandaError("Refusing to send an order for zero units.")

    return {
        "order": {
            "type": "MARKET",
            "instrument": pair.symbol,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{pair.round_price(stop):.{pair.displayed_decimals}f}",
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": f"{pair.round_price(target):.{pair.displayed_decimals}f}",
                "timeInForce": "GTC",
            },
        }
    }


def validate_levels(units: int, entry: float, stop: float,
                    target: float) -> list[str]:
    """
    Every reason this order must not be sent. Empty list means clear.

    This is the check that was missing when a stop landed above a long
    entry and closed the trade in 59 seconds.
    """
    problems = []
    long = units > 0

    if units == 0:
        problems.append("Zero units — nothing to send.")
    if entry <= 0:
        problems.append("Entry price is not positive.")

    if long:
        if stop >= entry:
            problems.append(
                f"Stop {stop} is not below entry {entry}. A long with a stop "
                f"above it closes the moment it opens.")
        if target <= entry:
            problems.append(f"Target {target} is not above entry {entry}.")
    else:
        if stop <= entry:
            problems.append(
                f"Stop {stop} is not above entry {entry} for a short.")
        if target >= entry:
            problems.append(
                f"Target {target} is not below entry {entry} for a short.")

    return problems


def place_order(ticker: str, units: int, stop: float, target: float,
                entry_hint: float | None = None) -> TradeResult:
    """
    Send a market order with its stop and target attached.

    entry_hint is the price the strategy priced the trade from. It is used
    only to validate that the stop and target sit on the right sides — the
    fill price comes back from OANDA.
    """
    pair = pairs.parse(ticker)
    ref = entry_hint if entry_hint is not None else get_price(ticker)

    problems = validate_levels(units, ref, stop, target)
    if problems:
        raise OandaError("Refusing to send this order:\n" +
                         "\n".join(f"• {p}" for p in problems))

    payload = _post(f"/v3/accounts/{_account()}/orders",
                    build_order(pair, units, stop, target))

    fill = payload.get("orderFillTransaction")
    if not fill:
        reason = (payload.get("orderRejectTransaction", {}).get("reason")
                  or payload.get("orderCancelTransaction", {}).get("reason")
                  or "no fill transaction returned")
        raise OandaError(f"Order did not fill: {reason}")

    opened = fill.get("tradeOpened") or {}
    return TradeResult(
        trade_id=str(opened.get("tradeID", "")),
        instrument=pair.symbol,
        units=int(float(opened.get("units", units))),
        price=float(fill.get("price", ref)),
        stop=stop,
        target=target,
        environment="live" if is_live() else "practice",
        raw=payload,
    )


# ===========================================================================
# Reading trades back
# ===========================================================================

@dataclass(frozen=True)
class TradeState:
    trade_id: str
    state: str                 # OPEN | CLOSED | unknown
    instrument: str
    units: int
    open_price: float
    close_price: Optional[float]
    realised_pl: float         # in the ACCOUNT currency, from OANDA
    unrealised_pl: float
    close_time: Optional[str]

    @property
    def is_closed(self) -> bool:
        return self.state.upper() == "CLOSED"


def parse_trade(payload: dict) -> TradeState:
    """
    Turn a /trades/{id} response into a TradeState.

    OANDA reports realizedPL directly in the account currency, which is a
    great deal simpler than reconstructing profit from bracket legs. No
    guessing which side filled.
    """
    t = payload.get("trade") or payload
    close_price = t.get("averageClosePrice")
    return TradeState(
        trade_id=str(t.get("id", "")),
        state=str(t.get("state", "unknown")),
        instrument=t.get("instrument", ""),
        units=int(float(t.get("initialUnits", 0))),
        open_price=float(t.get("price", 0)),
        close_price=float(close_price) if close_price else None,
        realised_pl=float(t.get("realizedPL", 0) or 0),
        unrealised_pl=float(t.get("unrealizedPL", 0) or 0),
        close_time=t.get("closeTime"),
    )


def get_trade(trade_id: str) -> TradeState:
    payload = _get(f"/v3/accounts/{_account()}/trades/{trade_id}")
    return parse_trade(payload)


def open_trades() -> list[TradeState]:
    payload = _get(f"/v3/accounts/{_account()}/openTrades")
    return [parse_trade({"trade": t}) for t in payload.get("trades", [])]


def close_trade(trade_id: str) -> dict:
    return _put(f"/v3/accounts/{_account()}/trades/{trade_id}/close",
                {"units": "ALL"})


def close_all() -> int:
    """Flatten everything. Half of the kill switch."""
    n = 0
    for t in open_trades():
        try:
            close_trade(t.trade_id)
            n += 1
        except OandaError:
            continue
    return n


def modify_stop(trade_id: str, new_stop: float, instrument: str) -> dict:
    """
    [BOOK p47] Move the stop — used for the break-even move.

    Unlike the share bot, this one can actually move the stop AT THE BROKER
    rather than only in its own records.
    """
    pair = pairs.parse(instrument)
    return _put(
        f"/v3/accounts/{_account()}/trades/{trade_id}/orders",
        {"stopLoss": {
            "price": f"{pair.round_price(new_stop):.{pair.displayed_decimals}f}",
            "timeInForce": "GTC"}})


def describe() -> str:
    tag = "🔴 LIVE — real money" if is_live() else "practice — simulated money"
    return (f"OANDA v20 ({tag}). Forex, 24/5. Stops and targets are attached "
            f"to the order and held by the broker.")
