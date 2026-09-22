"""
Alpaca execution — kept for the shares bot.

Not used while BROKER=oanda. Left in place so switching back to US
equities is a config change rather than a rebuild.

Original notes follow.

Order execution — placing real orders through Alpaca.

Design principles, in order of importance:

1. THE STOP GOES IN WITH THE ENTRY.
   Every order is a bracket: entry, stop-loss and take-profit submitted
   together as one instruction. The stop then lives at Alpaca, not in this
   process. If the VPS dies, the network drops, or the bot crashes mid-trade,
   the stop is still there. A bot-managed stop is a stop that stops existing
   the moment the bot does.

2. EVERY EXISTING GATE STILL APPLIES.
   Session clock, daily loss cap, trades-per-day, consecutive losses. An
   order that fails any of them is refused before it reaches the API.

3. PAPER UNLESS DELIBERATELY TOLD OTHERWISE.
   Live trading requires TRADING_MODE=live in .env AND a typed confirmation
   phrase. Two separate acts, because one flag is too easy to flip by accident.

4. NOTHING IS SILENT.
   Every submission, rejection and fill is logged and posted to Discord.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

PAPER_URL = "https://paper-api.alpaca.markets/v2"
LIVE_URL = "https://api.alpaca.markets/v2"

# The phrase a user must type to arm live trading. Deliberately awkward.
LIVE_CONFIRM_PHRASE = "I ACCEPT REAL MONEY LOSSES"


class ExecutionError(RuntimeError):
    pass


class OrderNotFound(ExecutionError):
    """Alpaca has never heard of this order id."""


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: float
    entry: float
    stop: float
    target: float
    status: str
    mode: str
    raw: dict


# ===========================================================================
# Mode
# ===========================================================================

def trading_mode() -> str:
    """'paper' or 'live'. Anything unrecognised is treated as paper."""
    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    return "live" if mode == "live" else "paper"


def is_live() -> bool:
    return trading_mode() == "live"


def _base_url() -> str:
    return LIVE_URL if is_live() else PAPER_URL


def _headers() -> dict:
    if is_live():
        key = os.environ.get("ALPACA_LIVE_KEY")
        secret = os.environ.get("ALPACA_LIVE_SECRET")
        if not key or not secret:
            raise ExecutionError(
                "TRADING_MODE is 'live' but ALPACA_LIVE_KEY / "
                "ALPACA_LIVE_SECRET are not set. Refusing to trade."
            )
    else:
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        if not key or not secret:
            raise ExecutionError("Alpaca paper keys are not set.")

    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
        "content-type": "application/json",
    }


# ===========================================================================
# Pre-flight checks
# ===========================================================================

def preflight(conn, session_state, entry: float, stop: float,
              qty: float, target: float) -> list[str]:
    """
    Every reason this order must NOT be sent. Empty list means clear.

    Checked here rather than at the call site so there is exactly one place
    that decides whether money moves.
    """
    import risk as risk_mod

    problems: list[str] = []

    # --- the gates that already exist -------------------------------------
    if not session_state.can_enter:
        problems.append(f"Session: {session_state.reason}")

    verdict = risk_mod.check(conn)
    if not verdict.allowed:
        problems.append(f"Risk: {verdict.reason}")

    # --- sanity on the order itself ---------------------------------------
    if qty <= 0:
        problems.append("Quantity is zero — nothing to send.")

    if entry <= 0:
        problems.append("Entry price is not positive.")

    if stop >= entry:
        problems.append(
            f"Stop ({stop:.2f}) is not below entry ({entry:.2f}). "
            f"That order would close itself immediately."
        )

    if target <= entry:
        problems.append(
            f"Target ({target:.2f}) is not above entry ({entry:.2f})."
        )

    # --- bracket orders need whole shares ---------------------------------
    if qty != int(qty):
        problems.append(
            f"Alpaca will not attach a bracket to a fractional order "
            f"({qty} shares). Without a bracket the stop would live only "
            f"inside this bot, and vanish if it crashes. Refusing. "
            f"Trade whole shares, or pick an instrument where one share "
            f"fits your risk budget."
        )

    # --- the loss this order can cause ------------------------------------
    import config
    account = config.CONFIG.display.account_gbp
    import fx
    max_loss_gbp = fx.to_gbp((entry - stop) * qty)
    cap = account * config.CONFIG.risk.risk_per_trade_pct / 100

    if max_loss_gbp > cap * 1.25:
        problems.append(
            f"This order risks £{max_loss_gbp:,.2f}, above your "
            f"£{cap:,.2f} per-trade limit."
        )

    return problems


def live_armed(conn) -> bool:
    """Live mode also needs the typed confirmation, stored in settings."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'live_armed'"
    ).fetchone()
    return bool(row and row[0] == "yes")


def arm_live(conn, phrase: str) -> bool:
    """Returns True only if the exact phrase was given."""
    if phrase.strip() != LIVE_CONFIRM_PHRASE:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('live_armed', 'yes')"
    )
    conn.commit()
    return True


def disarm_live(conn) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('live_armed', 'no')"
    )
    conn.commit()


def auto_enabled(conn) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'auto_trade'"
    ).fetchone()
    return bool(row and row[0] == "on")


def set_auto(conn, on: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_trade', ?)",
        ("on" if on else "off",),
    )
    conn.commit()


# ===========================================================================
# Placing the order
# ===========================================================================

def place_bracket(symbol: str, qty: float, entry: float, stop: float,
                  target: float, side: str = "buy") -> OrderResult:
    """
    Submit entry, stop and target as one bracket order.

    The stop and target are held by Alpaca from the moment the entry fills.
    That is the entire point — this bot going down must not leave a naked
    position.
    """
    if qty != int(qty):
        raise ExecutionError(
            "Bracket orders require whole shares. Refusing to place a "
            "fractional order without a broker-side stop."
        )

    payload = {
        "symbol": symbol.upper(),
        "qty": str(int(qty)),
        "side": side,
        "type": "limit",
        "limit_price": f"{entry:.2f}",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": f"{stop:.2f}"},
        "take_profit": {"limit_price": f"{target:.2f}"},
    }

    try:
        r = requests.post(f"{_base_url()}/orders", headers=_headers(),
                          json=payload, timeout=20)
    except requests.RequestException as e:
        raise ExecutionError(f"Network error submitting order: {e}") from e

    if r.status_code == 403:
        raise ExecutionError(
            f"Alpaca refused the order (403). Usually insufficient buying "
            f"power. Response: {r.text[:200]}"
        )
    if not r.ok:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:300]}")

    data = r.json()
    return OrderResult(
        order_id=data.get("id", ""),
        symbol=symbol.upper(),
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        target=target,
        status=data.get("status", "unknown"),
        mode=trading_mode(),
        raw=data,
    )


def cancel_all() -> int:
    """Kill switch. Cancels every open order. Returns how many."""
    try:
        r = requests.delete(f"{_base_url()}/orders", headers=_headers(),
                            timeout=20)
    except requests.RequestException as e:
        raise ExecutionError(f"Network error cancelling orders: {e}") from e

    if not r.ok and r.status_code != 207:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:200]}")

    try:
        return len(r.json())
    except Exception:  # noqa: BLE001
        return 0


def close_all_positions() -> int:
    """Flatten everything. The other half of the kill switch."""
    try:
        r = requests.delete(f"{_base_url()}/positions", headers=_headers(),
                            params={"cancel_orders": "true"}, timeout=30)
    except requests.RequestException as e:
        raise ExecutionError(f"Network error closing positions: {e}") from e

    if not r.ok and r.status_code != 207:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:200]}")

    try:
        return len(r.json())
    except Exception:  # noqa: BLE001
        return 0


def get_order(order_id: str, nested: bool = True) -> dict:
    """
    Fetch one order from Alpaca, with its bracket legs attached.

    nested=True returns the stop-loss and take-profit children inside a
    "legs" array on the parent. That is the only way to find out which side
    of the bracket actually filled, which is the only way to know whether a
    trade won or lost.
    """
    try:
        r = requests.get(f"{_base_url()}/orders/{order_id}",
                         headers=_headers(),
                         params={"nested": "true" if nested else "false"},
                         timeout=15)
    except requests.RequestException as e:
        raise ExecutionError(f"Network error fetching order: {e}") from e

    if r.status_code == 404:
        raise OrderNotFound(f"Alpaca has no order {order_id}.")
    if not r.ok:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:200]}")
    return r.json()


def broker_positions() -> dict[str, float]:
    """Symbol -> signed quantity, as Alpaca currently holds them."""
    try:
        r = requests.get(f"{_base_url()}/positions", headers=_headers(),
                         timeout=15)
    except requests.RequestException as e:
        raise ExecutionError(f"Network error fetching positions: {e}") from e
    if not r.ok:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:200]}")
    return {p["symbol"].upper(): float(p["qty"]) for p in r.json()}


def open_orders() -> list:
    r = requests.get(f"{_base_url()}/orders", headers=_headers(),
                     params={"status": "open"}, timeout=15)
    if not r.ok:
        raise ExecutionError(f"Alpaca error {r.status_code}: {r.text[:200]}")
    return r.json()


def describe_mode(conn) -> str:
    """Shown wherever mode matters, so it is never ambiguous."""
    mode = trading_mode()
    auto = "ON" if auto_enabled(conn) else "OFF"

    if mode == "paper":
        return (f"**PAPER** — simulated money, real prices.\n"
                f"Auto-execute: **{auto}**")

    armed = live_armed(conn)
    if not armed:
        return (f"⚠️ **LIVE mode set, but NOT armed.**\n"
                f"No orders will be sent until you arm it.\n"
                f"Auto-execute: **{auto}**")

    return (f"🔴 **LIVE — REAL MONEY.**\n"
            f"Auto-execute: **{auto}**\n"
            f"Every order placed spends actual funds.")
