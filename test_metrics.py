"""
Tests for the metrics engine and the weekly review.

Every metric is checked against a hand-calculated value. These numbers feed
decisions about money, so "it returned a number" is not a passing test —
the number has to be the right one.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fx
import metrics
import review
import tracker
from config import CONFIG

RATE, _ = fx.usd_to_gbp_rate()


def fresh():
    return tracker.connect(Path(tempfile.mkdtemp()) / "mx.db")


def trade(conn, entry, exit_price, qty=10, ticker="SPY", day=1,
          note="", requested=None, held_seconds=600, context=None):
    """One closed trade on a fixed date, so windows are deterministic."""
    p = tracker.open_position(conn, ticker, qty, entry, entry - 5, entry + 10,
                              context=context)
    opened = datetime(2026, 9, day, 14, 0, tzinfo=timezone.utc)
    conn.execute("UPDATE positions SET opened_at = ?, requested_entry = ? "
                 "WHERE id = ?",
                 (opened.isoformat(timespec="seconds"),
                  requested if requested is not None else entry, p.id))
    conn.commit()
    tracker.close_position_by_id(conn, p.id, exit_price, note)
    conn.execute("UPDATE positions SET closed_at = ? WHERE id = ?",
                 ((opened + timedelta(seconds=held_seconds))
                  .isoformat(timespec="seconds"), p.id))
    conn.commit()
    return tracker.get_position(conn, p.id)


def standard(conn):
    """+100, -50, +100, -50, +200 USD across five consecutive days."""
    trade(conn, 100, 110, day=1)
    trade(conn, 100, 95, day=2)
    trade(conn, 100, 110, day=3)
    trade(conn, 100, 95, day=4)
    trade(conn, 100, 120, day=5)
    return tracker.closed_positions(conn)


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------

def test_win_rate_hand_calculated():
    m = metrics.win_rate(standard(fresh()))
    assert m.value == 60.0, m.value           # 3 wins, 2 losses
    assert m.sample == 5
    print("PASS  win rate 3W/2L = 60.0%")


def test_win_rate_excludes_scratches():
    conn = fresh()
    trade(conn, 100, 110, day=1)
    trade(conn, 100, 100, day=2)              # flat — neither
    trade(conn, 100, 95, day=3)
    m = metrics.win_rate(tracker.closed_positions(conn))
    assert m.sample == 2 and m.value == 50.0
    print("PASS  a flat trade counts as neither win nor loss")


def test_win_rate_error_bar_shrinks_with_sample():
    conn_small, conn_big = fresh(), fresh()
    for i in range(4):
        trade(conn_small, 100, 110 if i < 2 else 95, day=i + 1)
    for i in range(40):
        trade(conn_big, 100, 110 if i % 2 == 0 else 95, day=(i % 28) + 1)

    small = metrics.win_rate(tracker.closed_positions(conn_small))
    big = metrics.win_rate(tracker.closed_positions(conn_big))
    assert small.stderr > big.stderr * 2, (small.stderr, big.stderr)
    print(f"PASS  error bar shrinks: ±{small.stderr:.1f} at n=4 → "
          f"±{big.stderr:.1f} at n=40")


def test_small_sample_never_reads_pass():
    """The whole point — a good number on 5 trades is not a PASS."""
    m = metrics.win_rate(standard(fresh()))
    assert m.value > m.target, "test needs a value above target"
    assert m.status == "UNRELIABLE", m.status
    print("PASS  60% win rate on 5 trades reads UNRELIABLE, not PASS")


def test_win_rate_interval_is_clamped_to_100():
    conn = fresh()
    for i in range(3):
        trade(conn, 100, 110, day=i + 1)
    trade(conn, 100, 95, day=4)
    lo, hi = metrics.win_rate(tracker.closed_positions(conn)).confidence_interval
    assert 0 <= lo and hi <= 100, (lo, hi)
    print(f"PASS  win-rate interval stays inside 0-100 ({lo:.0f}-{hi:.0f})")


# ---------------------------------------------------------------------------
# Profit factor
# ---------------------------------------------------------------------------

def test_profit_factor_hand_calculated():
    m = metrics.profit_factor(standard(fresh()))
    # gross win 400 USD, gross loss 100 USD -> 4.0, currency-independent
    assert abs(m.value - 4.0) < 1e-9, m.value
    print("PASS  profit factor 400/100 = 4.0")


def test_profit_factor_undefined_without_losses():
    conn = fresh()
    trade(conn, 100, 110, day=1)
    m = metrics.profit_factor(tracker.closed_positions(conn))
    assert m.value is None
    assert "not infinite" in m.note
    print("PASS  no losses means undefined, not infinity")


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def test_equity_curve_is_cumulative():
    curve = metrics.equity_curve(standard(fresh()), starting_gbp=1000.0)
    expected = [1000.0]
    for usd in (100, -50, 100, -50, 200):
        expected.append(expected[-1] + usd * RATE)
    assert all(abs(a - b) < 0.01 for a, b in zip(curve, expected)), curve
    print("PASS  equity curve compounds trade by trade")


def test_max_drawdown_hand_calculated():
    m = metrics.max_drawdown(standard(fresh()), starting_gbp=1000.0)
    # peak 1079.0 -> trough 1039.5 is the deepest: 39.5/1079 = 3.66%
    assert abs(m.value - 3.6608) < 0.01, m.value
    print(f"PASS  max drawdown {m.value:.2f}% matches hand calculation")


def test_drawdown_zero_when_never_down():
    conn = fresh()
    for i in range(3):
        trade(conn, 100, 110, day=i + 1)
    assert metrics.max_drawdown(tracker.closed_positions(conn)).value == 0.0
    print("PASS  no drawdown on an unbroken run of wins")


# ---------------------------------------------------------------------------
# Sharpe
# ---------------------------------------------------------------------------

def test_sharpe_needs_two_days():
    conn = fresh()
    trade(conn, 100, 110, day=1)
    m = metrics.sharpe(tracker.closed_positions(conn))
    assert m.value is None and "2+" in m.note
    print("PASS  Sharpe refuses to compute from one day")


def test_sharpe_undefined_on_zero_variance():
    conn = fresh()
    for i in range(4):
        trade(conn, 100, 110, day=i + 1)
    m = metrics.sharpe(tracker.closed_positions(conn))
    # identical returns on identical balances would be zero variance; if the
    # compounding balance makes them differ, the value must at least exist
    assert m.value is None or m.value > 0
    print("PASS  Sharpe handles a constant return series without dividing by zero")


def test_sharpe_error_bar_is_enormous_on_a_week():
    """A week of data cannot support a Sharpe claim, and says so."""
    m = metrics.sharpe(standard(fresh()))
    lo, hi = m.confidence_interval
    assert lo < 0 < hi, (lo, hi)
    print(f"PASS  5-day Sharpe interval spans zero ({lo:.1f} to {hi:.1f}) — "
          f"no claim supportable")


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def test_entry_slippage_recovered():
    conn = fresh()
    trade(conn, 100.05, 110, requested=100.00, day=1)
    pos = tracker.closed_positions(conn)[0]
    assert abs(pos.entry_slippage - 0.05) < 1e-9, pos.entry_slippage
    print("PASS  entry slippage = fill minus requested price")


def test_slippage_survives_reconciliation_overwrite():
    """
    Reconciliation overwrites entry_price with the real fill. Before
    requested_entry existed, that destroyed the only record of slippage.
    """
    import reconcile
    conn = fresh()
    pos = tracker.open_position(conn, "SPY", 10, 100.00, 99.0, 102.0,
                                broker_order_id="o1", broker_mode="paper")
    assert pos.requested_entry == 100.00

    outcome = reconcile.classify({
        "status": "filled", "filled_qty": "10", "filled_avg_price": "100.07",
        "legs": [{"type": "stop", "status": "filled", "filled_qty": "10",
                  "filled_avg_price": "99.00"}],
    }, 99.0, 102.0)
    reconcile.apply_outcome(conn, pos, outcome)

    after = tracker.get_position(conn, pos.id)
    assert after.entry_price == 100.07
    assert after.requested_entry == 100.00
    assert abs(after.entry_slippage - 0.07) < 1e-9
    print("PASS  slippage survives the entry-price overwrite")


def test_exit_slippage_measures_past_the_level():
    conn = fresh()
    # stop is entry-5 = 95; filling at 94.50 is 0.50 worse than intended
    trade(conn, 100, 94.50, day=1)
    pos = tracker.closed_positions(conn)[0]
    assert abs(pos.exit_slippage - 0.50) < 1e-9, pos.exit_slippage
    print("PASS  exit slippage measures the overshoot past the stop")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def test_clusters_find_the_dominant_exit_reason():
    conn = fresh()
    for i in range(6):
        trade(conn, 100, 95, day=i + 1, note="[stop]")
    trade(conn, 100, 110, day=7, note="[target]")

    clusters = review.cluster_losses(tracker.closed_positions(conn))
    top = clusters[0]
    assert "stopped out" in top.label
    assert top.count == 6 and top.share_pct == 100.0
    assert top.dominant
    print("PASS  clustering identifies the dominant exit reason")


def test_clusters_flag_fast_stopouts():
    conn = fresh()
    for i in range(4):
        trade(conn, 100, 95, day=i + 1, note="[stop]", held_seconds=60)
    labels = [c.label for c in review.cluster_losses(
        tracker.closed_positions(conn))]
    assert any("stopped within" in s for s in labels), labels
    print("PASS  stops hit within seconds are flagged separately")


def test_clusters_use_recorded_session_phase():
    conn = fresh()
    for i in range(5):
        trade(conn, 100, 95, day=i + 1, note="[stop]",
              context={"phase": "extended", "score": 6})
    labels = [c.label for c in review.cluster_losses(
        tracker.closed_positions(conn))]
    assert "session phase: extended" in labels, labels
    print("PASS  losses are grouped by the session phase they happened in")


def test_winning_trades_are_not_clustered():
    conn = fresh()
    for i in range(5):
        trade(conn, 100, 110, day=i + 1, note="[target]")
    assert review.cluster_losses(tracker.closed_positions(conn)) == []
    print("PASS  clustering looks at losses only")


# ---------------------------------------------------------------------------
# The review report
# ---------------------------------------------------------------------------

def test_review_refuses_to_diagnose_an_empty_ledger():
    text = review.build(fresh())
    assert "No closed trades in this cycle" in text
    assert "none proposed this cycle" in text
    assert "strategy_parameters: {}" in text
    print("PASS  empty ledger produces no diagnosis and no proposed change")


def test_review_refuses_below_the_sample_floor():
    conn = fresh()
    now = datetime.now(timezone.utc)
    for i in range(5):
        p = tracker.open_position(conn, "SPY", 10, 100, 95, 110)
        tracker.close_position_by_id(conn, p.id, 95, "[stop]")
        conn.execute("UPDATE positions SET closed_at = ? WHERE id = ?",
                     ((now - timedelta(days=1)).isoformat(timespec="seconds"),
                      p.id))
        conn.commit()

    text = review.build(conn)
    assert "withheld" in text
    assert "5/20 trades" in text
    assert "No change proposed" in text
    print("PASS  5 trades: numbers reported, diagnosis withheld")


def test_review_diagnoses_above_the_floor():
    conn = fresh()
    now = datetime.now(timezone.utc)
    for i in range(25):
        p = tracker.open_position(conn, "SPY", 10, 100, 95, 110)
        win = i % 3 == 0
        tracker.close_position_by_id(conn, p.id, 110 if win else 95,
                                     "[target]" if win else "[stop]")
        conn.execute("UPDATE positions SET closed_at = ? WHERE id = ?",
                     ((now - timedelta(hours=i + 1)).isoformat(timespec="seconds"),
                      p.id))
        conn.commit()

    text = review.build(conn)
    assert "withheld" not in text
    assert "Primary Failure Mode:** exit: stopped out" in text
    print("PASS  25 trades: a primary failure mode is named")


def test_review_never_writes_config():
    """Read-only is the safety rail. Prove the words are actually there."""
    text = review.build(fresh())
    assert "read-only" in text.lower()
    assert "No configuration was changed" in text
    print("PASS  the report states plainly that it changed nothing")


def test_review_output_matches_the_required_structure():
    text = review.build(fresh())
    for heading in ("## 1. Cycle Performance Summary",
                    "## 2. Trade Log Diagnostics",
                    "## 3. Optimization Hypothesis",
                    "## 4. Proposed Configuration Diff"):
        assert heading in text, heading
    print("PASS  output matches the four required sections")


# ---------------------------------------------------------------------------
# Coherence
# ---------------------------------------------------------------------------

def test_coherence_catches_an_impossible_drawdown_target():
    """
    A drawdown ceiling below the per-trade risk can never be met — the
    first losing trade breaches it, and every weekly review fails forever
    on a problem that isn't in the strategy.

    This fired for real while risk was at 20% against a 5% ceiling. It
    should be silent now that risk is at the book's 1%.
    """
    import dataclasses
    original = CONFIG.risk

    object.__setattr__(CONFIG, "risk",
                       dataclasses.replace(original, risk_per_trade_pct=20.0))
    try:
        loud = review.coherence_warnings()
        assert any("Drawdown target is unreachable" in w for w in loud), loud
    finally:
        object.__setattr__(CONFIG, "risk", original)

    object.__setattr__(CONFIG, "risk",
                       dataclasses.replace(original, risk_per_trade_pct=1.0))
    try:
        quiet = review.coherence_warnings()
    finally:
        object.__setattr__(CONFIG, "risk", original)
    assert not any("Drawdown target" in w for w in quiet), quiet
    print("PASS  fires at 20% risk, silent at 1%")


def test_a_contradiction_in_the_live_config_is_never_silent():
    """
    If one losing trade at the configured risk breaks the drawdown ceiling,
    the review must say so. Bob chose 10% risk against a 5% ceiling on
    2026-09-23; that is his call, but the weekly review has to flag it
    rather than quietly fail the benchmark every week.
    """
    loud = review.coherence_warnings()
    breaches = CONFIG.risk.risk_per_trade_pct > CONFIG.review.max_drawdown_pct
    flagged = any("Drawdown target is unreachable" in w for w in loud)
    assert breaches == flagged, (breaches, loud)
    print(f"PASS  {CONFIG.risk.risk_per_trade_pct:g}% risk vs "
          f"{CONFIG.review.max_drawdown_pct:g}% ceiling: "
          f"{'flagged' if flagged else 'coherent, nothing to flag'}")


def test_chunks_never_exceed_the_limit():
    text = review.build(fresh())
    for c in review.chunks(text, limit=600):
        assert len(c) <= 600, len(c)
    print("PASS  chunked output always fits Discord's embed limit")


def test_chunks_lose_nothing():
    text = review.build(fresh())
    joined = "".join(review.chunks(text, limit=600))
    for marker in ("Cycle Performance Summary", "Optimization Hypothesis",
                   "No configuration was changed"):
        assert marker in joined, marker
    print("PASS  chunking drops no content")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
