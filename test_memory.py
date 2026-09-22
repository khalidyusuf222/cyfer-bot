"""Tests for trade memory. The key property: it refuses to conclude too early."""

import tempfile
from pathlib import Path

import memory
import tracker


def fresh():
    return tracker.connect(Path(tempfile.mkdtemp()) / "mem.db")


def make_trade(conn, ticker, entry, exit_price, conditions, score=5, total=6,
               strategy="cyfer"):
    pos = tracker.open_position(conn, ticker, 10, entry, stop_price=entry * 0.98)
    memory.record_context(conn, pos.id, strategy, "bullish", score, total,
                          conditions_met=conditions)
    tracker.close_position(conn, ticker, exit_price)
    return pos.id


def test_context_is_stored_and_retrieved():
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 1, 500.0, stop_price=490.0)
    memory.record_context(conn, pos.id, "cyfer", "bullish", 5, 6,
                          conditions_met=["4H bias bullish (last break at 512)",
                                          "SMT divergence vs partner index"])
    ctx = memory.get_context(conn, pos.id)
    assert ctx is not None
    assert ctx["strategy"] == "cyfer"
    assert "4h_bias" in ctx["conditions_met"]
    assert "smt_divergence" in ctx["conditions_met"]
    print("PASS  setup context stored and read back")


def test_condition_labels_are_stable_across_prices():
    """Same condition at different prices must group into one bucket."""
    a = memory._labels(["Liquidity swept: prev day low at $506.00"])
    b = memory._labels(["Liquidity swept: session low at $488.20"])
    assert a == b == ["liquidity_sweep"], (a, b)
    print("PASS  condition labels stable regardless of price")


def test_refuses_to_conclude_below_threshold():
    conn = fresh()
    for i in range(5):
        make_trade(conn, f"AA{i}", 100.0, 110.0, ["SMT divergence"])
    result = memory.analyse(conn, tracker.closed_positions(conn))
    assert not result["enough_data"]
    assert result["needed"] == memory.MIN_TRADES_OVERALL - 5
    text = memory.format_analysis(result)
    assert "more" in text and "noise" in text
    assert "%" not in text.split("noise")[0], "must not report win rates yet"
    print(f"PASS  refuses to conclude at 5 trades (needs "
          f"{result['needed']} more)")


def test_reports_once_threshold_is_met():
    conn = fresh()
    # 12 winners with SMT, 12 losers without — a difference big enough to show
    for i in range(12):
        make_trade(conn, f"W{i}", 100.0, 110.0, ["SMT divergence", "4H bias"])
    for i in range(12):
        make_trade(conn, f"L{i}", 100.0, 95.0, ["4H bias"])

    result = memory.analyse(conn, tracker.closed_positions(conn))
    assert result["enough_data"]
    assert result["total_closed"] == 24

    smt = result["by_condition"]["smt_divergence"]
    bias = result["by_condition"]["4h_bias"]
    assert smt.wins == 12 and smt.losses == 0
    assert bias.n == 24
    assert smt.avg_pnl > bias.avg_pnl

    text = memory.format_analysis(result)
    assert "smt divergence" in text
    assert "12W/0L" in text
    print(f"PASS  reports at 24 trades — SMT {smt.wins}W/{smt.losses}L "
          f"avg £{smt.avg_pnl:+.2f}")


def test_small_buckets_are_withheld():
    conn = fresh()
    for i in range(20):
        make_trade(conn, f"A{i}", 100.0, 110.0, ["4H bias"])
    # only 2 trades carry this condition — below MIN_TRADES_PER_BUCKET
    for i in range(2):
        make_trade(conn, f"R{i}", 100.0, 130.0, ["4H bias", "SMT divergence"])

    result = memory.analyse(conn, tracker.closed_positions(conn))
    assert result["by_condition"]["smt_divergence"].n == 2
    assert not result["by_condition"]["smt_divergence"].reportable

    text = memory.format_analysis(result)
    assert "Not shown" in text and "smt divergence" in text.split("Not shown")[1]
    print("PASS  buckets under 5 trades are withheld, not reported")


def test_unlabelled_trades_counted_but_not_analysed():
    conn = fresh()
    for i in range(20):
        make_trade(conn, f"A{i}", 100.0, 110.0, ["4H bias"])
    # manual trade with no setup attached
    tracker.open_position(conn, "MANUAL", 1, 50.0, stop_price=49.0)
    tracker.close_position(conn, "MANUAL", 55.0)

    result = memory.analyse(conn, tracker.closed_positions(conn))
    assert result["unlabelled"] == 1
    assert result["labelled"] == 20
    assert "logged manually" in memory.format_analysis(result)
    print("PASS  manual trades counted in total, excluded from breakdown")


def test_by_score_bucketing():
    conn = fresh()
    for i in range(10):
        make_trade(conn, f"S6{i}", 100.0, 112.0, ["4H bias"], score=6, total=6)
    for i in range(10):
        make_trade(conn, f"S4{i}", 100.0, 97.0, ["4H bias"], score=4, total=6)

    result = memory.analyse(conn, tracker.closed_positions(conn))
    six = result["by_score"]["6/6 conditions"]
    four = result["by_score"]["4/6 conditions"]
    assert six.wins == 10 and four.losses == 10
    assert six.avg_pnl > four.avg_pnl
    print("PASS  results split by how many conditions aligned")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
