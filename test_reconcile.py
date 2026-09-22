"""
Tests for fill reconciliation.

The bug being fixed here was invisible: the bot placed trades, Alpaca closed
them, and nobody recorded the result — so the loss cap and the consecutive-loss
lockout reported themselves as active while never seeing a single loss.

These tests all run offline. classify() is pure, and run() takes an injected
fetch function, so the whole path is exercised without touching Alpaca.
"""

import tempfile
from pathlib import Path

import reconcile
import risk
import tracker


def fresh():
    conn = tracker.connect(Path(tempfile.mkdtemp()) / "rec.db")
    risk.day_state(conn)
    return conn


def order(status="filled", filled_qty="2", filled_avg="100.00", legs=None):
    return {
        "id": "order-1",
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg,
        "legs": legs if legs is not None else [],
    }


def leg(kind, status="new", filled_qty="0", filled_avg=None):
    return {"id": f"leg-{kind}", "type": kind, "status": status,
            "filled_qty": filled_qty, "filled_avg_price": filled_avg}


# ---------------------------------------------------------------------------
# classify — the pure decision
# ---------------------------------------------------------------------------

def test_entry_not_filled_is_pending():
    for status in ("new", "accepted", "pending_new", "held"):
        out = reconcile.classify(order(status=status, filled_qty="0",
                                       filled_avg=None))
        assert out.kind == "pending", (status, out)
    print("PASS  unfilled entry reads as pending, not closed")


def test_cancelled_entry_is_abandoned():
    for status in ("canceled", "expired", "rejected"):
        out = reconcile.classify(order(status=status, filled_qty="0",
                                       filled_avg=None))
        assert out.kind == "abandoned", (status, out)
    print("PASS  entry cancelled without filling reads as abandoned")


def test_filled_entry_live_legs_is_working():
    out = reconcile.classify(order(legs=[leg("limit"), leg("stop")]))
    assert out.kind == "working"
    assert out.entry_price == 100.00
    assert out.entry_qty == 2.0
    print("PASS  filled entry with live legs is still working")


def test_stop_leg_filled_closes_the_trade():
    out = reconcile.classify(order(legs=[
        leg("limit", status="canceled"),
        leg("stop", status="filled", filled_qty="2", filled_avg="99.00"),
    ]), stop_price=99.0, target_price=102.0)
    assert out.kind == "closed"
    assert out.exit_price == 99.00
    assert out.exit_reason == "stop"
    print("PASS  stop fill closes the trade at the real stop price")


def test_target_leg_filled_closes_the_trade():
    out = reconcile.classify(order(legs=[
        leg("limit", status="filled", filled_qty="2", filled_avg="102.00"),
        leg("stop", status="canceled"),
    ]), stop_price=99.0, target_price=102.0)
    assert out.kind == "closed"
    assert out.exit_price == 102.00
    assert out.exit_reason == "target"
    print("PASS  target fill closes the trade at the real target price")


def test_real_fill_price_beats_the_price_we_asked_for():
    """We submit a limit; the fill can differ. P&L must use the fill."""
    out = reconcile.classify(order(filled_avg="100.37", legs=[
        leg("stop", status="filled", filled_qty="2", filled_avg="98.88"),
    ]))
    assert out.entry_price == 100.37
    assert out.exit_price == 98.88
    print("PASS  reports the actual fill prices, not the requested ones")


def test_leg_filled_without_price_stays_working():
    """Don't invent an exit price — wait for the next pass."""
    out = reconcile.classify(order(legs=[
        leg("stop", status="filled", filled_qty="2", filled_avg=None),
    ]))
    assert out.kind == "working"
    print("PASS  a fill with no price yet does not close the position")


def test_unknown_leg_type_falls_back_to_nearest_level():
    out = reconcile.classify(order(legs=[
        leg("mystery", status="filled", filled_qty="2", filled_avg="99.10"),
    ]), stop_price=99.0, target_price=102.0)
    assert out.exit_reason == "stop", out
    print("PASS  unrecognised leg type resolved by nearest recorded level")


def test_partially_filled_entry_counts_as_open():
    out = reconcile.classify(order(status="partially_filled", filled_qty="1",
                                   filled_avg="100.00",
                                   legs=[leg("stop"), leg("limit")]))
    assert out.kind == "working"
    assert out.entry_qty == 1.0
    print("PASS  partially filled entry is treated as an open position")


# ---------------------------------------------------------------------------
# apply_outcome — writing it down
# ---------------------------------------------------------------------------

def test_closing_records_realised_pnl():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="order-1", broker_mode="paper")
    out = reconcile.classify(order(legs=[
        leg("stop", status="filled", filled_qty="2", filled_avg="99.00"),
    ]), 99.0, 102.0)
    change = reconcile.apply_outcome(conn, pos, out)

    assert change.kind == "closed"
    assert change.pnl_usd == -2.0, change.pnl_usd     # (99-100) * 2
    assert not tracker.get_position(conn, pos.id).is_open
    assert tracker.broker_managed_open(conn) == []
    print("PASS  closing writes the exit price and stops polling the order")


def test_entry_price_is_synced_on_fill():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="order-1", broker_mode="paper")
    out = reconcile.classify(order(filled_avg="100.42",
                                   legs=[leg("stop"), leg("limit")]))
    change = reconcile.apply_outcome(conn, pos, out)
    assert change.kind == "entry_synced"
    assert tracker.get_position(conn, pos.id).entry_price == 100.42
    print("PASS  the recorded entry is corrected to the actual fill")


def test_pending_changes_nothing():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="order-1", broker_mode="paper")
    out = reconcile.classify(order(status="new", filled_qty="0",
                                   filled_avg=None))
    assert reconcile.apply_outcome(conn, pos, out) is None
    assert tracker.get_position(conn, pos.id).is_open
    print("PASS  a pending order leaves the position untouched")


def test_abandoned_closes_with_zero_pnl():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="order-1", broker_mode="paper")
    out = reconcile.classify(order(status="canceled", filled_qty="0",
                                   filled_avg=None))
    change = reconcile.apply_outcome(conn, pos, out)
    assert change.kind == "abandoned"
    assert change.pnl_usd == 0.0
    assert tracker.get_position(conn, pos.id).realised() == 0.0
    print("PASS  an entry that never filled closes flat, not as a loss")


# ---------------------------------------------------------------------------
# run — the loop, with the network injected
# ---------------------------------------------------------------------------

def test_run_only_touches_broker_managed_positions():
    conn = fresh()
    tracker.open_position(conn, "AAPL", 1, 50.0, 49.0, 52.0)      # manual !b
    tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                          broker_order_id="order-1", broker_mode="paper")

    asked = []

    def fake(order_id):
        asked.append(order_id)
        return order(legs=[leg("limit", status="filled", filled_qty="2",
                               filled_avg="102.00")])

    changes = reconcile.run(conn, fetch=fake)
    assert asked == ["order-1"], asked
    assert len(changes) == 1 and changes[0].ticker == "SPY"
    assert tracker.find_open(conn, "AAPL") is not None
    print("PASS  hand-logged positions are never reconciled against Alpaca")


def test_network_failure_leaves_position_open():
    """A transient error must not close a trade that is still running."""
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="order-1", broker_mode="paper")

    def boom(order_id):
        raise ConnectionError("network down")

    assert reconcile.run(conn, fetch=boom) == []
    assert tracker.get_position(conn, pos.id).is_open
    print("PASS  a network failure never closes an open position")


def test_missing_order_is_retired_not_counted_as_a_loss():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                                broker_order_id="gone", broker_mode="paper")

    import execution

    def missing(order_id):
        raise execution.OrderNotFound("no such order")

    changes = reconcile.run(conn, fetch=missing)
    assert len(changes) == 1 and changes[0].kind == "abandoned"
    assert tracker.get_position(conn, pos.id).realised() == 0.0
    assert tracker.broker_managed_open(conn) == []
    print("PASS  an order Alpaca has lost stops being polled forever")


def test_the_gates_actually_shut_after_reconciled_losses():
    """The whole point. Two reconciled losses must lock trading out."""
    from config import CONFIG
    conn = fresh()
    assert risk.check(conn).allowed

    for i in range(CONFIG.risk.max_consecutive_losses):
        pos = tracker.open_position(conn, "SPY", 200, 100.0, 99.0, 102.0,
                                    broker_order_id=f"o{i}",
                                    broker_mode="paper")
        risk.record_trade_opened(conn)

        def fake(order_id, _i=i):
            return order(legs=[leg("stop", status="filled", filled_qty="200",
                                   filled_avg="99.00")])

        for change in reconcile.run(conn, fetch=fake):
            import fx
            risk.record_trade_closed(conn, fx.to_gbp(change.pnl_usd))

    verdict = risk.check(conn)
    assert not verdict.allowed, (
        f"{CONFIG.risk.max_consecutive_losses} reconciled losses should have "
        f"shut the gate")
    print(f"PASS  gates shut after reconciled losses — {verdict.reason}")


def test_cancelled_entry_gives_back_the_trade_slot():
    conn = fresh()
    tracker.open_position(conn, "SPY", 2, 100.0, 99.0, 102.0,
                          broker_order_id="order-1", broker_mode="paper")
    risk.record_trade_opened(conn)
    assert risk.day_state(conn)["trades_taken"] == 1

    def fake(order_id):
        return order(status="canceled", filled_qty="0", filled_avg=None)

    for change in reconcile.run(conn, fetch=fake):
        if change.kind == "abandoned":
            risk.record_trade_cancelled(conn)

    assert risk.day_state(conn)["trades_taken"] == 0
    print("PASS  an unfilled entry doesn't burn one of the day's trades")


def test_trade_slot_never_goes_negative():
    conn = fresh()
    risk.record_trade_cancelled(conn)
    assert risk.day_state(conn)["trades_taken"] == 0
    print("PASS  the trade counter can't be driven below zero")


# ===========================================================================
# The OANDA path
# ===========================================================================

def _trade(state="CLOSED", units="2500", price="1.10000",
           close="1.11000", pl="19.80"):
    import oanda
    return oanda.parse_trade({"trade": {
        "id": "777", "state": state, "instrument": "EUR_USD",
        "initialUnits": units, "price": price,
        "averageClosePrice": close, "realizedPL": pl}})


def test_oanda_open_trade_is_working_not_closed():
    out = reconcile.classify_trade(_trade(state="OPEN", close=None, pl="0"))
    assert out.kind == "working", out
    assert out.entry_price == 1.10000
    print("PASS  an OPEN OANDA trade is working, not closed")


def test_oanda_closed_trade_uses_the_brokers_own_profit():
    """
    OANDA computes realizedPL in the account currency, spread included.
    Recomputing it from two prices would quietly report a better trade
    than actually happened — the spread would vanish from every result.
    """
    out = reconcile.classify_trade(_trade(pl="19.80"),
                                   stop_price=1.09500, target_price=1.11000,
                                   account_currency="GBP")
    assert out.kind == "closed"
    assert out.realised == 19.80 and out.currency == "GBP"
    assert out.exit_reason == "target"
    print("PASS  a closed OANDA trade carries the broker's own realised P&L")


def test_oanda_exit_reason_read_from_the_close_price():
    stopped = reconcile.classify_trade(
        _trade(close="1.09500", pl="-9.90"),
        stop_price=1.09500, target_price=1.11000)
    assert stopped.exit_reason == "stop", stopped
    print("PASS  the exit reason is read from which level the close sits on")


def test_oanda_short_is_classified_the_same_way():
    out = reconcile.classify_trade(
        _trade(units="-2500", price="1.10000", close="1.09000", pl="19.80"),
        stop_price=1.10500, target_price=1.09000)
    assert out.kind == "closed" and out.exit_reason == "target"
    assert out.entry_qty == 2500          # magnitude, not sign
    print("PASS  a short reconciles the same way as a long")


def test_oanda_closed_without_a_price_stays_open():
    """Never invent an exit price. It resolves on the next pass."""
    out = reconcile.classify_trade(_trade(close=None))
    assert out.kind == "working", out
    print("PASS  a close with no price yet does not close the position")


def test_broker_profit_is_preferred_over_recomputing_it():
    """
    End to end: apply_outcome must take OANDA's number, not the one the
    tracker would derive. They differ by the spread, and the spread is the
    difference between an honest ledger and a flattering one.
    """
    conn = fresh()
    pos = tracker.open_position(conn, "EUR_USD", 2500, 1.10000,
                                stop_price=1.09500, target_price=1.11000,
                                broker_order_id="777", broker_mode="practice")
    out = reconcile.classify_trade(_trade(pl="19.80"),
                                   stop_price=1.09500, target_price=1.11000,
                                   account_currency="GBP")
    change = reconcile.apply_outcome(conn, pos, out)

    derived = (1.11000 - 1.10000) * 2500          # $25.00, spread ignored
    assert change.pnl == 19.80, change.pnl
    assert change.pnl != derived
    assert change.currency == "GBP"
    assert change.pnl_gbp == 19.80                # already GBP, not re-converted
    print(f"PASS  £{change.pnl:.2f} from the broker, not "
          f"{derived:.2f} recomputed from the prices")


def test_classify_any_routes_by_shape():
    assert reconcile.classify_any(order(legs=[leg("limit"), leg("stop")])).kind \
        == "working"
    assert reconcile.classify_any(_trade(state="OPEN", close=None)).kind \
        == "working"
    print("PASS  classify_any routes a dict to Alpaca, a TradeState to OANDA")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
