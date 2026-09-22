"""Tests for the rules engine. Run: python3 test_tracker.py"""

import tempfile
from pathlib import Path

import tracker


def fresh_db():
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    return tracker.connect(tmp)


def kinds(alerts):
    return {a.kind for a in alerts}


def test_stop_triggers_at_and_below():
    conn = fresh_db()
    p = tracker.open_position(conn, "AAPL", 10, 100.0, stop_price=95.0)
    assert kinds(tracker.evaluate(p, 96.0)) == set(), "should not fire above stop"
    assert "stop_hit" in kinds(tracker.evaluate(p, 95.0)), "must fire exactly at stop"
    assert "stop_hit" in kinds(tracker.evaluate(p, 90.0)), "must fire below stop"
    print("PASS  stop loss boundary")


def test_target_triggers_at_and_above():
    conn = fresh_db()
    p = tracker.open_position(conn, "AAPL", 10, 100.0,
                              stop_price=95.0, target_price=110.0)
    assert kinds(tracker.evaluate(p, 109.99)) == set()
    assert "target_hit" in kinds(tracker.evaluate(p, 110.0))
    assert "target_hit" in kinds(tracker.evaluate(p, 120.0))
    print("PASS  target boundary")


def test_trailing_stop_follows_peak_and_never_retreats():
    conn = fresh_db()
    p = tracker.open_position(conn, "TSLA", 5, 200.0, trail_pct=10.0)

    # Rises to 250 -> trail level becomes 225
    tracker.update_peak(conn, p.id, 250.0)
    p = tracker.get_position(conn, p.id)
    assert p.peak_price == 250.0

    assert kinds(tracker.evaluate(p, 230.0)) == set(), "230 is above 225 trail"
    assert "trail_hit" in kinds(tracker.evaluate(p, 225.0)), "must fire at trail"

    # A lower price must NOT lower the peak
    tracker.update_peak(conn, p.id, 210.0)
    p = tracker.get_position(conn, p.id)
    assert p.peak_price == 250.0, "peak must never move down"
    print("PASS  trailing stop ratchets up only")


def test_trail_does_not_fire_before_profit():
    """A position that never rose above entry shouldn't trigger a trail exit."""
    conn = fresh_db()
    p = tracker.open_position(conn, "NVDA", 2, 500.0, trail_pct=5.0)
    assert "trail_hit" not in kinds(tracker.evaluate(p, 400.0))
    print("PASS  trail inactive below entry")


def test_unprotected_position_warns():
    conn = fresh_db()
    p = tracker.open_position(conn, "GME", 100, 20.0)
    assert "risk_warning" in kinds(tracker.evaluate(p, 20.0))
    print("PASS  unprotected position warns")


def test_deep_loss_escalates():
    conn = fresh_db()
    p = tracker.open_position(conn, "GME", 100, 20.0)
    assert "deep_loss" in kinds(tracker.evaluate(p, 17.0)), "-15% should escalate"
    print("PASS  deep loss escalation")


def test_invalid_levels_rejected():
    """
    A negative qty is no longer invalid — it means SHORT. What is still
    invalid is a level on the wrong side of the entry, and that check now
    has to know which way the trade is facing.
    """
    conn = fresh_db()
    for bad in (
        # long: stop above, target below
        lambda: tracker.open_position(conn, "AAPL", 10, 100.0, stop_price=105.0),
        lambda: tracker.open_position(conn, "AAPL", 10, 100.0, target_price=95.0),
        # short: stop below, target above — the mirror image
        lambda: tracker.open_position(conn, "AAPL", -10, 100.0, stop_price=95.0),
        lambda: tracker.open_position(conn, "AAPL", -10, 100.0, target_price=105.0),
        lambda: tracker.open_position(conn, "AAPL", 0, 100.0),
        lambda: tracker.open_position(conn, "AAPL", 10, 0),
    ):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("invalid input was accepted")
    print("PASS  invalid levels rejected, in both directions")


def test_alert_dedup():
    conn = fresh_db()
    p = tracker.open_position(conn, "AAPL", 10, 100.0, stop_price=95.0)
    assert not tracker.already_fired(conn, p.id, "stop_hit")
    tracker.mark_fired(conn, p.id, "stop_hit")
    assert tracker.already_fired(conn, p.id, "stop_hit")
    print("PASS  alert de-duplication")


def test_pnl_and_summary():
    """
    portfolio_summary reports in POUNDS now, converted per position.

    It used to return raw quote-currency numbers and let the caller convert
    the total, which was fine while every instrument settled in dollars and
    silently wrong the moment a yen pair joined them.
    """
    import fx
    rate, _ = fx.rate_to_gbp("USD")

    conn = fresh_db()
    tracker.open_position(conn, "AAPL", 10, 100.0, stop_price=95.0)
    tracker.close_position(conn, "AAPL", 110.0)          # +$100
    tracker.open_position(conn, "MSFT", 5, 200.0, stop_price=190.0)
    tracker.close_position(conn, "MSFT", 190.0)          # -$50
    tracker.open_position(conn, "NVDA", 2, 500.0, stop_price=480.0)

    s = tracker.portfolio_summary(conn, {"NVDA": 550.0})
    assert s["currency"] == "GBP"
    assert abs(s["realised"] - 50.0 * rate) < 1e-6, s["realised"]
    assert abs(s["unrealised"] - 100.0 * rate) < 1e-6, s["unrealised"]
    assert abs(s["total"] - 150.0 * rate) < 1e-6
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 50.0
    print(f"PASS  P&L and portfolio summary, in GBP at {rate:.4f}")


def test_summary_does_not_add_yen_to_dollars():
    """
    The bug this guards against: 1,500 yen of profit and $12 of profit are
    not 1,512 of anything. Converting each before adding is the only way
    the total means something.
    """
    import fx
    conn = fresh_db()
    # +1,500 JPY on a USD/JPY short  (sold at 150.00, bought back at 149.85)
    tracker.open_position(conn, "USD_JPY", -10_000, 150.00, stop_price=150.50)
    tracker.close_position(conn, "USD_JPY", 149.85)
    # +$12.50 on a EUR/USD long
    tracker.open_position(conn, "EUR_USD", 2500, 1.1000, stop_price=1.0950)
    tracker.close_position(conn, "EUR_USD", 1.1050)

    s = tracker.portfolio_summary(conn, {})
    expected = fx.pnl_to_gbp(1500.0, "USD_JPY") + fx.pnl_to_gbp(12.5, "EUR_USD")
    assert abs(s["realised"] - expected) < 0.01, (s["realised"], expected)
    # The naive sum would be 1512.5 "units" — off by two orders of magnitude.
    assert s["realised"] < 100, s["realised"]
    assert s["wins"] == 2 and s["losses"] == 0
    print(f"PASS  yen and dollar profits converted before adding "
          f"(£{s['realised']:.2f}, not 1512.50)")


def test_short_positions_profit_in_the_right_direction():
    """
    A short that falls is a WIN. With a signed qty that falls out of the
    same arithmetic the longs use, which is why qty is signed.
    """
    conn = fresh_db()
    winner = tracker.open_position(conn, "EUR_USD", -2500, 1.1000,
                                   stop_price=1.1050, target_price=1.0900)
    tracker.close_position(conn, "EUR_USD", 1.0900)
    loser = tracker.open_position(conn, "GBP_USD", -2000, 1.2500,
                                  stop_price=1.2550, target_price=1.2400)
    tracker.close_position(conn, "GBP_USD", 1.2550)

    closed = {p.ticker: p for p in tracker.closed_positions(conn)}
    assert closed["EUR_USD"].direction == "short"
    assert closed["EUR_USD"].realised() > 0, closed["EUR_USD"].realised()
    assert closed["GBP_USD"].realised() < 0, closed["GBP_USD"].realised()

    s = tracker.portfolio_summary(conn, {})
    assert s["wins"] == 1 and s["losses"] == 1, s
    print("PASS  a short that falls is a win, one that rises is a loss")


def test_moving_stop_clears_stale_alert():
    conn = fresh_db()
    p = tracker.open_position(conn, "AAPL", 10, 100.0, stop_price=95.0)
    tracker.mark_fired(conn, p.id, "stop_hit")
    tracker.set_stop(conn, "AAPL", 90.0)
    assert not tracker.already_fired(conn, p.id, "stop_hit"), \
        "moving the stop should re-arm the alert"
    print("PASS  moving stop re-arms alert")


def test_connection_works_from_a_worker_thread():
    """
    Regression: the bot hands database work to worker threads via
    asyncio.to_thread. Python's default sqlite3 guard refuses that, and
    discord.py swallows the exception — so every affected command replied
    with absolute silence and nothing in the channel said why.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    conn = tracker.connect(Path(tempfile.mkdtemp()) / "threaded.db")
    tracker.open_position(conn, "SPY", 1, 100.0, 99.0, 102.0)

    async def main():
        return await asyncio.to_thread(tracker.open_positions, conn)

    positions = asyncio.run(main())
    assert len(positions) == 1 and positions[0].ticker == "SPY"
    print("PASS  the database is usable from a worker thread")


def test_short_stop_and_target_fire_the_right_way_round():
    """
    A short's stop is ABOVE the entry and fires when price RISES. Before
    the direction fix, the comparison was the long one in both cases: a
    short's stop would have stayed silent no matter how far it went wrong,
    and its target would have fired the instant the trade went against it.
    """
    conn = fresh_db()
    pos = tracker.open_position(conn, "EUR_USD", -2500, 1.1000,
                                stop_price=1.1050, target_price=1.0900)

    kinds = lambda price: {a.kind for a in tracker.evaluate(pos, price)}

    assert kinds(1.1050) == {"stop_hit"}, kinds(1.1050)
    assert kinds(1.1080) == {"stop_hit"}
    assert kinds(1.0900) == {"target_hit"}, kinds(1.0900)
    assert kinds(1.0850) == {"target_hit"}
    assert kinds(1.1000) == set(), kinds(1.1000)
    print("PASS  a short's stop fires upward, its target downward")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print("\nAll tests passed.")
