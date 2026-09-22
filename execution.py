"""
Execution — which broker, and the gates every order passes through.

This is a router. The settings that decide whether trading is armed live
in the database and are broker-agnostic; the order functions delegate to
whichever broker BROKER names in .env.

  oanda   — forex, v20 REST, practice by default. The current setup.
  alpaca  — US shares. Kept working in broker_alpaca.py.

DESIGN PRINCIPLES, IN ORDER OF IMPORTANCE
-----------------------------------------
1. THE STOP GOES ON WITH THE ENTRY.
   Never as a follow-up request. On OANDA that is stopLossOnFill; on
   Alpaca it is a bracket. Either way the stop lives at the broker from
   the instant the position exists, and survives this bot dying.

2. EVERY GATE STILL APPLIES.
   Session clock, daily loss cap, trades per day, consecutive losses. An
   order failing any of them is refused before it reaches the network.

3. PRACTICE UNLESS DELIBERATELY TOLD OTHERWISE.
   Live needs a different host, different credentials, a config change,
   a restart AND a typed confirmation. Five separate acts.

4. NOTHING IS SILENT.
   Every submission, refusal and fill is logged and posted to Discord.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


class ExecutionError(RuntimeError):
    pass


class OrderNotFound(ExecutionError):
    """The broker has never heard of this order or trade."""


# The phrase required to arm live trading. Deliberately awkward to type.
LIVE_CONFIRM_PHRASE = "I ACCEPT REAL MONEY LOSSES"


def broker_name() -> str:
    return os.environ.get("BROKER", "oanda").strip().lower()


def _broker():
    name = broker_name()
    if name == "oanda":
        import oanda
        return oanda
    if name == "alpaca":
        import broker_alpaca
        return broker_alpaca
    raise ExecutionError(
        f"BROKER is '{name}' — expected 'oanda' or 'alpaca'.")


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

def is_live() -> bool:
    """Whether the configured broker is pointed at real money."""
    b = _broker()
    if broker_name() == "oanda":
        return b.is_live()
    return b.is_live()


def trading_mode() -> str:
    return "live" if is_live() else "practice"


# ===========================================================================
# Arming — stored in the database, so it survives a restart
# ===========================================================================

def live_armed(conn) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'live_armed'").fetchone()
    return bool(row and row[0] == "yes")


def arm_live(conn, phrase: str) -> bool:
    """True only if the exact phrase was typed, case and all."""
    if phrase.strip() != LIVE_CONFIRM_PHRASE:
        return False
    conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                 "VALUES ('live_armed', 'yes')")
    conn.commit()
    return True


def disarm_live(conn) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                 "VALUES ('live_armed', 'no')")
    conn.commit()


def auto_enabled(conn) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'auto_trade'").fetchone()
    return bool(row and row[0] == "on")


def set_auto(conn, on: bool) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES "
                 "('auto_trade', ?)", ("on" if on else "off",))
    conn.commit()


# ===========================================================================
# Pre-flight
# ===========================================================================

def preflight(conn, session_state, entry: float, stop: float,
              qty: float, target: float, symbol: str | None = None,
              side: str = "buy") -> list[str]:
    """
    Every reason this order must NOT be sent. Empty list means clear.

    Decided in one place so there is exactly one gate that money passes
    through, whichever broker is behind it.

    `side` matters now. The share version assumed every trade was a long
    and hardcoded "stop must be below entry", which would have refused
    every short the strategy produces.
    """
    import risk as risk_mod
    from config import CONFIG

    problems: list[str] = []
    symbol = symbol or CONFIG.instruments.primary
    long = side.lower() in ("buy", "long")

    if not session_state.can_enter:
        problems.append(f"Session: {session_state.reason}")

    verdict = risk_mod.check(conn)
    if not verdict.allowed:
        problems.append(f"Risk: {verdict.reason}")

    # --- the levels themselves ------------------------------------------
    if broker_name() == "oanda":
        import oanda
        signed = int(qty) if long else -abs(int(qty))
        problems += oanda.validate_levels(signed, entry, stop, target)
    else:
        if qty <= 0:
            problems.append("Quantity is zero — nothing to send.")
        if long and stop >= entry:
            problems.append(
                f"Stop ({stop:.2f}) is not below entry ({entry:.2f}). "
                f"That order would close itself immediately.")
        if long and target <= entry:
            problems.append(
                f"Target ({target:.2f}) is not above entry ({entry:.2f}).")
        if qty != int(qty):
            problems.append(
                f"A bracket cannot be attached to a fractional order "
                f"({qty} shares), so the stop would live only inside this "
                f"bot and vanish if it crashes. Refusing.")

    # --- the loss this order can cause ----------------------------------
    risk_gbp = _risk_in_account_ccy(entry, stop, qty, symbol)
    cap = CONFIG.display.account_gbp * CONFIG.risk.risk_per_trade_pct / 100

    if risk_gbp > cap * 1.25:
        problems.append(
            f"This order risks £{risk_gbp:,.2f}, above your "
            f"£{cap:,.2f} per-trade limit.")

    return problems


def _risk_in_account_ccy(entry: float, stop: float, qty: float,
                         symbol: str | None = None) -> float:
    """
    What being stopped out costs, in the account's currency.

    The currency is decided by the pair, not assumed. On USD/JPY the risk
    is denominated in yen, and converting it at the dollar rate would
    understate the loss by a factor of roughly 150.
    """
    import fx
    distance = abs(entry - stop) * abs(qty)

    if broker_name() == "oanda":
        import pairs
        from config import CONFIG
        pair = pairs.parse(symbol or CONFIG.instruments.primary)
        return fx.convert(distance, pair.quote)

    return fx.to_gbp(distance)


# ===========================================================================
# Placing
# ===========================================================================

def place_bracket(symbol: str, qty: float, entry: float, stop: float,
                  target: float, side: str = "buy") -> OrderResult:
    """
    Submit the entry with its stop and target attached, as one instruction.

    Name kept from the Alpaca days because every caller uses it. On OANDA
    the equivalent is a market order carrying stopLossOnFill and
    takeProfitOnFill — same guarantee, different wording.
    """
    b = _broker()

    if broker_name() == "oanda":
        units = int(qty) if side == "buy" else -abs(int(qty))
        try:
            res = b.place_order(symbol, units, stop, target,
                                entry_hint=entry)
        except b.OandaError as e:
            raise ExecutionError(str(e)) from e

        return OrderResult(
            order_id=res.trade_id, symbol=res.instrument, side=side,
            qty=abs(res.units), entry=res.price, stop=res.stop,
            target=res.target, status="filled", mode=res.environment,
            raw=res.raw)

    return b.place_bracket(symbol, qty, entry, stop, target, side)


def get_order(order_id: str, nested: bool = True):
    """Ask the broker what became of a trade. Shape differs per broker."""
    b = _broker()
    if broker_name() == "oanda":
        try:
            return b.get_trade(order_id)
        except b.TradeNotFound as e:
            raise OrderNotFound(str(e)) from e
        except b.OandaError as e:
            raise ExecutionError(str(e)) from e
    try:
        return b.get_order(order_id, nested)
    except b.OrderNotFound as e:
        raise OrderNotFound(str(e)) from e


def cancel_all() -> int:
    b = _broker()
    if broker_name() == "oanda":
        return 0        # market orders fill or fail; nothing rests
    return b.cancel_all()


def close_all_positions() -> int:
    """Flatten everything. The other half of the kill switch."""
    b = _broker()
    if broker_name() == "oanda":
        return b.close_all()
    return b.close_all_positions()


def open_orders() -> list:
    b = _broker()
    if broker_name() == "oanda":
        return b.open_trades()
    return b.open_orders()


def modify_stop(order_id: str, new_stop: float, symbol: str) -> bool:
    """
    Move the stop AT THE BROKER, for the break-even move.

    The shares bot could only move its own record of the stop. OANDA lets
    the real one move, which is what [BOOK p47] actually describes.
    """
    if broker_name() != "oanda":
        return False
    b = _broker()
    try:
        b.modify_stop(order_id, new_stop, symbol)
        return True
    except b.OandaError:
        return False


def describe_mode(conn) -> str:
    """Shown wherever mode matters, so it is never ambiguous."""
    auto = "ON" if auto_enabled(conn) else "OFF"
    name = broker_name().upper()

    if not is_live():
        return (f"**PRACTICE** on {name} — simulated money, real prices.\n"
                f"Auto-execute: **{auto}**")

    if not live_armed(conn):
        return (f"⚠️ **LIVE mode set on {name}, but NOT armed.**\n"
                f"No orders will be sent until you arm it.\n"
                f"Auto-execute: **{auto}**")

    return (f"🔴 **LIVE on {name} — REAL MONEY.**\n"
            f"Auto-execute: **{auto}**\n"
            f"Every order placed spends actual funds.")
