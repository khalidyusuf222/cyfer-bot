"""Tests for the P&L report and the end-of-day summary."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import fx
import report
import tracker


def fresh():
    return tracker.connect(Path(tempfile.mkdtemp()) / "rep.db")


def closed_trade(conn, ticker, qty, entry, exit_price, note=""):
    pos = tracker.open_position(conn, ticker, qty, entry, entry - 5,
                                entry + 10)
    return tracker.close_position_by_id(conn, pos.id, exit_price, note)


def test_empty_tally_says_so_without_pretending():
    conn = fresh()
    t = report.tally(conn, report.EPOCH, "All time")
    assert t.trades == 0
    text = report.format_tally(t)
    assert "no closed trades" in text
    assert "not a failure" in text
    print("PASS  no trades reports as no trades")


def test_wins_and_losses_counted_in_gbp():
    conn = fresh()
    closed_trade(conn, "SPY", 10, 100.0, 110.0)      # +100 USD
    closed_trade(conn, "QQQ", 10, 100.0, 95.0)       # -50 USD

    t = report.tally(conn, report.EPOCH, "All time")
    rate, _ = fx.usd_to_gbp_rate()

    assert t.trades == 2
    assert t.wins == 1 and t.losses == 1
    assert abs(t.realised_gbp - 50 * rate) < 0.01
    assert abs(t.win_rate - 50.0) < 1e-9
    print(f"PASS  2 trades tally to £{t.realised_gbp:.2f} at {t.win_rate:.0f}% "
          f"win rate")


def test_open_positions_are_excluded():
    """An unrealised gain is a price, not a result."""
    conn = fresh()
    closed_trade(conn, "SPY", 10, 100.0, 110.0)
    tracker.open_position(conn, "NVDA", 5, 200.0, 195.0, 210.0)

    t = report.tally(conn, report.EPOCH)
    assert t.trades == 1, "an open position must not appear in realised P&L"
    print("PASS  open positions are not counted as profit")


def test_averages_and_expectancy():
    conn = fresh()
    closed_trade(conn, "A", 1, 100.0, 120.0)         # +20
    closed_trade(conn, "B", 1, 100.0, 120.0)         # +20
    closed_trade(conn, "C", 1, 100.0, 90.0)          # -10

    t = report.tally(conn, report.EPOCH)
    rate, _ = fx.usd_to_gbp_rate()
    assert abs(t.avg_win_gbp - 20 * rate) < 0.01
    assert abs(t.avg_loss_gbp - 10 * rate) < 0.01
    assert abs(t.expectancy_gbp - 30 / 3 * rate) < 0.01
    print(f"PASS  expectancy £{t.expectancy_gbp:.2f} per trade")


def test_exit_reason_is_shown():
    conn = fresh()
    closed_trade(conn, "SPY", 1, 100.0, 99.0, "[stop]")
    closed_trade(conn, "QQQ", 1, 100.0, 110.0, "[target]")
    closed_trade(conn, "IWM", 1, 100.0, 101.0, "[eod]")

    t = report.tally(conn, report.EPOCH)
    joined = "\n".join(t.lines)
    assert "stopped out" in joined
    assert "target hit" in joined
    assert "closed at the bell" in joined
    print("PASS  each trade line says how it ended")


def test_small_sample_warning_is_attached():
    conn = fresh()
    closed_trade(conn, "SPY", 1, 100.0, 110.0)
    text = report.format_tally(report.tally(conn, report.EPOCH, "Today"))
    assert "far too few" in text
    print("PASS  a tiny sample is labelled as meaningless, next to the number")


def test_no_small_sample_warning_once_there_is_a_sample():
    conn = fresh()
    for i in range(25):
        closed_trade(conn, f"T{i}", 1, 100.0, 101.0)
    text = report.format_tally(report.tally(conn, report.EPOCH, "All time"))
    assert "far too few" not in text
    print("PASS  the warning goes away once 20+ trades exist")


def test_warns_when_average_loss_exceeds_average_win():
    conn = fresh()
    closed_trade(conn, "A", 1, 100.0, 102.0)         # +2
    closed_trade(conn, "B", 1, 100.0, 90.0)          # -10
    text = report.format_tally(report.tally(conn, report.EPOCH))
    assert "average loss is bigger" in text
    print("PASS  flags losses being bigger than wins")


def test_period_windows_filter_by_date():
    conn = fresh()
    old = closed_trade(conn, "OLD", 1, 100.0, 110.0)
    conn.execute("UPDATE positions SET closed_at = ? WHERE id = ?",
                 ("2020-01-01T12:00:00+00:00", old.id))
    conn.commit()
    closed_trade(conn, "NEW", 1, 100.0, 110.0)

    today = report.tally(conn, report.day_start_utc())
    everything = report.tally(conn, report.EPOCH)
    assert today.trades == 1 and everything.trades == 2
    print("PASS  'today' excludes older trades, 'all' includes them")


def test_eod_summary_flags_positions_left_open():
    conn = fresh()
    closed_trade(conn, "SPY", 1, 100.0, 110.0)
    tracker.open_position(conn, "NVDA", 5, 200.0, 195.0, 210.0)

    text = report.format_eod(conn, {"NVDA": 205.0})
    assert "Still open overnight" in text
    assert "gap straight past it" in text
    print("PASS  the summary warns about anything left open overnight")


def test_eod_summary_clean_when_flat():
    conn = fresh()
    closed_trade(conn, "SPY", 1, 100.0, 110.0)
    text = report.format_eod(conn, {})
    assert "Still open overnight" not in text
    print("PASS  no overnight warning when the account is flat")


def test_all_time_line_appears_once_history_exists():
    conn = fresh()
    old = closed_trade(conn, "OLD", 1, 100.0, 110.0)
    conn.execute("UPDATE positions SET closed_at = ? WHERE id = ?",
                 ("2020-01-01T12:00:00+00:00", old.id))
    conn.commit()
    closed_trade(conn, "NEW", 1, 100.0, 105.0)

    text = report.format_eod(conn, {})
    assert "All time:" in text
    print("PASS  a good day is shown against the running total")


def test_claim_once_is_once():
    conn = fresh()
    assert tracker.claim_once(conn, "eod_summary:2026-09-11")
    assert not tracker.claim_once(conn, "eod_summary:2026-09-11")
    assert tracker.claim_once(conn, "eod_summary:2026-09-12")
    print("PASS  once-a-day jobs fire once, and survive a restart")


def test_every_report_uses_the_brokers_figure_not_the_derived_one():
    """
    The bug this pins: the risk ledger was fed OANDA's realised P&L while
    the daily summary recomputed its own from the entry and exit prices.
    They differ by the spread, so the same trade had two different answers
    depending on which part of the bot you asked — and the flattering one
    was the one Bob saw.

    Everything that reports a result must go through realised_gbp().
    """
    import metrics

    conn = fresh()
    pos = tracker.open_position(conn, "EUR_USD", 4110, 1.10128,
                                1.09820, 1.11140,
                                broker_order_id="x", broker_mode="practice")
    tracker.close_position_by_id(conn, pos.id, 1.11140, "[target]")

    derived = fx.pnl_to_gbp((1.11140 - 1.10128) * 4110, "EUR_USD")
    broker = derived - 0.35            # the spread
    tracker.set_realised_gbp(conn, pos.id, broker)

    fresh_pos = tracker.get_position(conn, pos.id)
    assert fresh_pos.pnl_is_from_broker
    assert abs(fresh_pos.realised_gbp() - broker) < 1e-9

    # The three places a result is reported must all agree.
    t = report.tally(conn, report.EPOCH, "All time")
    assert abs(t.realised_gbp - broker) < 1e-9, t.realised_gbp

    summary = tracker.portfolio_summary(conn, {})
    assert abs(summary["realised"] - broker) < 1e-9, summary["realised"]

    assert abs(metrics._pnl_gbp(fresh_pos) - broker) < 1e-9

    # And none of them may be the derived number.
    assert abs(t.realised_gbp - derived) > 0.3
    print(f"PASS  report, summary and metrics all say £{broker:.2f} "
          f"(broker), not £{derived:.2f} (derived)")


def test_a_hand_logged_trade_still_falls_back_to_the_prices():
    """
    No broker figure exists for a trade Bob logged himself, so the derived
    number is the only one there is. It must still work.
    """
    conn = fresh()
    pos = tracker.open_position(conn, "EUR_USD", 2500, 1.1000,
                                1.0950, 1.1100)
    closed = tracker.close_position_by_id(conn, pos.id, 1.1100)
    assert not closed.pnl_is_from_broker
    expected = fx.pnl_to_gbp(0.0100 * 2500, "EUR_USD")
    assert abs(closed.realised_gbp() - expected) < 1e-9
    print(f"PASS  a hand-logged trade falls back to the prices "
          f"(£{expected:.2f})")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
