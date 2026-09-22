"""
Fill reconciliation — asking the broker what actually happened to our orders.

THE BUG THIS EXISTS TO FIX
--------------------------
The bot places an order and writes the position into its own database. The
broker then fills the entry and, some time later, fills either the stop or
the take-profit and the trade is over.

Nothing told the bot.

So the tracker went on believing the position was open forever. Realised P&L
stayed at zero, which meant the daily loss cap could never trip and the
consecutive-loss lockout could never fire, because neither had ever seen a
loss. Both gates were reported as protecting the account while doing nothing
at all. That is a worse failure than having no gate, because it is invisible.

This module closes the loop: every scan, ask the broker about each order the
bot placed, and when the trade has ended, record the real exit price and the
real profit or loss.

TWO BROKER SHAPES
-----------------
Alpaca returns a parent order with nested bracket legs, and the profit has
to be reconstructed from whichever leg filled. OANDA returns a trade object
with realizedPL already computed in the account currency, which is far less
to get wrong. classify() handles Alpaca's JSON; classify_trade() handles
OANDA's TradeState. classify_any() picks.

DESIGN NOTE
-----------
Both classifiers are pure functions with no network in them, so every branch
below is covered by tests that never touch the internet. The network lives
in run(), which is a thin loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# Alpaca order states in which nothing was, or ever will be, filled.
DEAD_STATES = {"canceled", "expired", "rejected", "suspended", "replaced",
               "stopped", "done_for_day"}

# States meaning the order is still working at the exchange.
LIVE_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding",
               "partially_filled", "calculated", "pending_replace",
               "pending_cancel", "held"}

STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}
TARGET_TYPES = {"limit"}


@dataclass(frozen=True)
class Outcome:
    """What Alpaca says became of one bracket order."""
    kind: str                      # pending | working | closed | abandoned
    entry_price: Optional[float] = None    # real average fill, not our limit
    entry_qty: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None      # stop | target | other
    detail: str = ""
    # OANDA hands back the realised profit directly; Alpaca does not, and
    # it stays None there so the tracker computes it from the prices.
    realised: Optional[float] = None
    currency: Optional[str] = None

    @property
    def is_closed(self) -> bool:
        return self.kind == "closed"


def _f(value) -> Optional[float]:
    """Alpaca sends numbers as strings, and nulls as None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _leg_reason(leg: dict, stop_price: Optional[float],
                target_price: Optional[float]) -> str:
    """Which side of the bracket was this leg?"""
    kind = (leg.get("type") or "").lower()
    if kind in STOP_TYPES:
        return "stop"
    if kind in TARGET_TYPES:
        return "target"

    # Fall back to whichever recorded level the fill sits nearer to. Only
    # reached if Alpaca changes its type names.
    price = _f(leg.get("filled_avg_price"))
    if price is not None and stop_price is not None and target_price is not None:
        return "stop" if abs(price - stop_price) <= abs(price - target_price) \
            else "target"
    return "other"


def classify(order: dict,
             stop_price: Optional[float] = None,
             target_price: Optional[float] = None) -> Outcome:
    """
    Turn one Alpaca order (fetched with nested=true) into an Outcome.

    Pure. No network, no database, no clock.
    """
    status = (order.get("status") or "").lower()
    filled_qty = _f(order.get("filled_qty")) or 0.0
    filled_avg = _f(order.get("filled_avg_price"))

    # --- the entry never happened ------------------------------------------
    if status in DEAD_STATES and filled_qty <= 0:
        return Outcome(
            kind="abandoned",
            detail=f"Entry order {status} without filling — no position was "
                   f"ever opened.",
        )

    # --- the entry hasn't happened yet -------------------------------------
    if filled_qty <= 0:
        return Outcome(
            kind="pending",
            detail=f"Entry order is {status or 'unknown'} — not filled yet.",
        )

    # --- the entry filled; did either bracket leg? -------------------------
    legs = order.get("legs") or []
    for leg in legs:
        leg_status = (leg.get("status") or "").lower()
        leg_filled = _f(leg.get("filled_qty")) or 0.0
        if leg_status == "filled" or leg_filled > 0:
            exit_price = _f(leg.get("filled_avg_price"))
            reason = _leg_reason(leg, stop_price, target_price)
            if exit_price is None:
                # Filled but no price yet: treat as still open rather than
                # inventing a number. It will resolve on the next pass.
                return Outcome(kind="working", entry_price=filled_avg,
                               entry_qty=filled_qty,
                               detail="Exit leg filled, awaiting fill price.")
            return Outcome(
                kind="closed",
                entry_price=filled_avg,
                entry_qty=leg_filled or filled_qty,
                exit_price=exit_price,
                exit_reason=reason,
                detail=f"{reason.title()} filled at {exit_price:.2f}.",
            )

    # Entry filled, both legs still live: the trade is running.
    return Outcome(
        kind="working",
        entry_price=filled_avg,
        entry_qty=filled_qty,
        detail=f"Position open — entry filled at "
               f"{filled_avg:.2f}." if filled_avg else "Position open.",
    )


# ===========================================================================
# OANDA
# ===========================================================================

def _oanda_exit_reason(close_price: Optional[float],
                       stop_price: Optional[float],
                       target_price: Optional[float],
                       units: int) -> str:
    """
    Which of the two attached orders ended the trade.

    OANDA's trade object does not name it — you would have to walk
    closingTransactionIDs to find out. Comparing the close price against
    the two levels we asked for gives the same answer with no extra call,
    and it is right unless the two are equidistant, which cannot happen
    with a 2:1 reward-to-risk.
    """
    if close_price is None or stop_price is None or target_price is None:
        return "other"
    return ("stop" if abs(close_price - stop_price)
            <= abs(close_price - target_price) else "target")


def classify_trade(trade,
                   stop_price: Optional[float] = None,
                   target_price: Optional[float] = None,
                   account_currency: str = "GBP") -> Outcome:
    """
    Turn one OANDA TradeState into an Outcome.

    Pure. No network, no database, no clock.

    There is no "pending" case here that matters: the bot sends market
    orders with fill-or-kill, so by the time a trade id exists the entry
    has filled. A trade that was killed instead never produces an id, and
    place_order raises rather than returning one.
    """
    state = (getattr(trade, "state", "") or "").upper()
    units = int(getattr(trade, "units", 0) or 0)
    open_price = getattr(trade, "open_price", None)

    if state == "CLOSED":
        close_price = getattr(trade, "close_price", None)
        if close_price is None:
            return Outcome(
                kind="working", entry_price=open_price, entry_qty=abs(units),
                detail="Trade closed, awaiting the close price.")
        reason = _oanda_exit_reason(close_price, stop_price, target_price,
                                    units)
        realised = float(getattr(trade, "realised_pl", 0.0) or 0.0)
        return Outcome(
            kind="closed",
            entry_price=open_price,
            entry_qty=abs(units),
            exit_price=close_price,
            exit_reason=reason,
            realised=realised,
            currency=account_currency,
            detail=f"{reason.title()} filled at {close_price:g}.")

    if state == "OPEN":
        return Outcome(kind="working", entry_price=open_price,
                       entry_qty=abs(units),
                       detail=f"Position open — filled at {open_price:g}."
                       if open_price else "Position open.")

    return Outcome(kind="pending",
                   detail=f"Trade state is '{state or 'unknown'}'.")


def classify_any(payload,
                 stop_price: Optional[float] = None,
                 target_price: Optional[float] = None,
                 account_currency: str = "GBP") -> Outcome:
    """Route to the right classifier by what the broker handed back."""
    if isinstance(payload, dict):
        return classify(payload, stop_price, target_price)
    return classify_trade(payload, stop_price, target_price,
                          account_currency)


# ===========================================================================
# Applying outcomes
# ===========================================================================

@dataclass
class Change:
    """A tracked position whose state changed because the broker said so."""
    position_id: int
    ticker: str
    kind: str                      # closed | abandoned | entry_synced
    pnl: float = 0.0
    currency: str = "USD"          # what `pnl` is denominated in
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    entry_price: Optional[float] = None
    detail: str = ""

    @property
    def pnl_usd(self) -> float:
        """Kept for callers written when everything was dollars."""
        return self.pnl

    @property
    def pnl_gbp(self) -> float:
        import fx
        return fx.convert(self.pnl, self.currency)


def apply_outcome(conn, pos, outcome: Outcome) -> Optional[Change]:
    """
    Write an Outcome into the tracker. Returns a Change when something
    actually changed, or None when the position is unchanged.

    Does NOT touch the risk ledger — that is bot.py's job, so that the
    Discord message and the ledger update happen together or not at all.
    """
    import tracker

    if outcome.kind == "pending":
        return None

    # Sync the real fill price. We submit a limit at our chosen entry, but
    # the actual fill can differ, and every P&L number downstream is wrong
    # if we keep using the price we asked for instead of the one we got.
    entry_synced = False
    tick = 1e-5 if abs(pos.entry_price) < 50 else 0.005
    if (outcome.entry_price is not None
            and abs(outcome.entry_price - pos.entry_price) > tick):
        conn.execute("UPDATE positions SET entry_price = ? WHERE id = ?",
                     (outcome.entry_price, pos.id))
        conn.commit()
        pos = tracker.get_position(conn, pos.id)
        entry_synced = True

    if outcome.kind == "abandoned":
        tracker.close_position_by_id(conn, pos.id, pos.entry_price,
                                     "[never filled]")
        return Change(position_id=pos.id, ticker=pos.ticker, kind="abandoned",
                      pnl=0.0, detail=outcome.detail)

    if outcome.kind == "closed":
        closed = tracker.close_position_by_id(
            conn, pos.id, outcome.exit_price, f"[{outcome.exit_reason}]")

        # Prefer the broker's own realised figure where there is one.
        # OANDA computes it in the account currency including the spread
        # and any financing; recomputing it from two prices would quietly
        # report a slightly better trade than actually happened.
        if outcome.realised is not None:
            pnl, ccy = outcome.realised, (outcome.currency or "GBP")
            import fx
            tracker.set_realised_gbp(conn, pos.id, fx.convert(pnl, ccy))
        else:
            pnl, ccy = closed.realised(), "USD"

        return Change(position_id=pos.id, ticker=pos.ticker, kind="closed",
                      pnl=pnl, currency=ccy,
                      exit_price=outcome.exit_price,
                      exit_reason=outcome.exit_reason,
                      entry_price=closed.entry_price,
                      detail=outcome.detail)

    if entry_synced:
        return Change(position_id=pos.id, ticker=pos.ticker,
                      kind="entry_synced", entry_price=pos.entry_price,
                      detail=f"Filled at {pos.entry_price:g}.")

    return None


def run(conn, fetch: Optional[Callable[[str], dict]] = None) -> list[Change]:
    """
    Reconcile every broker-managed open position. Returns what changed.

    Network failures are swallowed per-position: one unreachable order must
    not stop the others being checked, and a transient error should not
    close a position that is still open.
    """
    import tracker
    import execution

    if fetch is None:
        fetch = execution.get_order

    # OANDA reports profit in the account's own currency. Ask once rather
    # than per position, and fall back to GBP — which is what the setup
    # guide tells Bob to open the account in — if the call fails.
    account_ccy = "USD"
    if execution.broker_name() == "oanda":
        account_ccy = "GBP"
        try:
            import data
            account_ccy = data.account_summary().get("currency") or "GBP"
        except Exception:  # noqa: BLE001
            pass

    changes: list[Change] = []
    for pos in tracker.broker_managed_open(conn):
        try:
            order = fetch(pos.broker_order_id)
        except execution.OrderNotFound:
            # The order genuinely does not exist. Mark it so it stops being
            # polled forever, but do not invent a P&L for it.
            tracker.close_position_by_id(conn, pos.id, pos.entry_price,
                                         "[order not found at broker]")
            changes.append(Change(
                position_id=pos.id, ticker=pos.ticker, kind="abandoned",
                detail="The broker has no record of this order."))
            continue
        except Exception:  # noqa: BLE001 — network, JSON, anything
            continue

        outcome = classify_any(order, pos.stop_price, pos.target_price,
                               account_ccy)
        change = apply_outcome(conn, pos, outcome)
        if change is not None:
            changes.append(change)

    return changes


def describe(change: Change, to_gbp: Optional[Callable] = None) -> str:
    """One-line human summary of a Change, in GBP."""
    import fx

    if change.kind == "abandoned":
        return f"**{change.ticker}** — {change.detail}"

    if change.kind == "entry_synced":
        return (f"**{change.ticker}** — entry filled at "
                f"`{change.entry_price:g}`.")

    # to_gbp is still accepted so older callers keep working, but the
    # currency now travels with the Change, so the default path converts
    # from whatever the broker actually reported in.
    pnl = to_gbp(change.pnl) if to_gbp else fx.convert(change.pnl,
                                                       change.currency)
    mark = "🟢" if pnl >= 0 else "🔴"
    word = {"stop": "Stopped out", "target": "Target hit"}.get(
        change.exit_reason, "Closed")
    return (f"{mark} **{change.ticker}** — {word} at "
            f"`{change.exit_price:g}` · **£{pnl:,.2f}**")
