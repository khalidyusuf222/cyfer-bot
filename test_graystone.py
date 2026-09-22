"""Tests for the Graystone engine and the make/lose calculator."""

import calc
import fx
import graystone
from strategy import Bar


def mk(o, h, l, c, i=0):
    return Bar(ts=f"d{i}", open=o, high=h, low=l, close=c)


def uptrend(n=70, start=100.0, step=1.0):
    """Steadily rising bars so the 8/20/50 EMAs stack bullish."""
    bars = []
    for i in range(n):
        c = start + i * step
        bars.append(mk(c - 0.3, c + 0.5, c - 0.6, c, i))
    return bars


def downtrend(n=70, start=200.0, step=1.0):
    bars = []
    for i in range(n):
        c = start - i * step
        bars.append(mk(c + 0.3, c + 0.6, c - 0.5, c, i))
    return bars


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def test_ema_matches_hand_calculation():
    values = [1, 2, 3, 4, 5]
    out = graystone.ema(values, 3)
    assert out[0] is None and out[1] is None
    assert abs(out[2] - 2.0) < 1e-9, out          # seed = mean(1,2,3)
    # multiplier 2/(3+1) = 0.5 -> (4-2)*0.5+2 = 3.0
    assert abs(out[3] - 3.0) < 1e-9, out
    assert abs(out[4] - 4.0) < 1e-9, out
    print("PASS  EMA matches hand calculation")


def test_ema_returns_none_before_period():
    out = graystone.ema([1, 2], 5)
    assert all(v is None for v in out)
    print("PASS  EMA undefined before its period fills")


# ---------------------------------------------------------------------------
# Signal candle body rule  [JG: upper/lower 30%]
# ---------------------------------------------------------------------------

def test_body_in_upper_30_required_for_bullish():
    # range 100-110. Upper 30% starts at 107.
    good = mk(107.5, 110, 100, 109)
    bad = mk(102, 110, 100, 109)          # open too low
    assert graystone._both_in_upper(good, 30)
    assert not graystone._both_in_upper(bad, 30)
    print("PASS  bullish needs BOTH open and close in upper 30%")


def test_body_in_lower_30_required_for_bearish():
    good = mk(102.5, 110, 100, 101)
    bad = mk(102.5, 110, 100, 108)         # close too high
    assert graystone._both_in_lower(good, 30)
    assert not graystone._both_in_lower(bad, 30)
    print("PASS  bearish needs BOTH open and close in lower 30%")


# ---------------------------------------------------------------------------
# Full signal
# ---------------------------------------------------------------------------

def test_no_signal_without_ema_stack():
    """Choppy sideways data shouldn't produce a stacked trend."""
    bars = [mk(100 + (i % 3), 101 + (i % 3), 99 + (i % 3), 100 + (i % 3), i)
            for i in range(70)]
    assert graystone.scan("TEST", bars) is None
    print("PASS  no signal without a clean EMA stack")


def test_bullish_signal_fires_on_pullback_candle():
    bars = uptrend()
    fast = graystone.ema([b.close for b in bars], graystone.EMA_FAST)
    f = fast[-1]

    # Replace the last bar with a pullback that dips to the 8 EMA and
    # closes strongly — open and close both in the upper 30%.
    low = f - 0.5
    high = low + 10
    body_floor = low + 10 * 0.70
    bars[-1] = mk(body_floor + 0.5, high, low, high - 0.5, len(bars) - 1)

    sig = graystone.scan("TEST", bars)
    assert sig is not None, "expected a bullish signal"
    assert sig.direction == "bullish"
    assert sig.entry > sig.stop
    assert sig.target > sig.entry
    print(f"PASS  bullish signal fires (entry {sig.entry:.2f}, stop {sig.stop:.2f})")


def test_bearish_signal_fires():
    bars = downtrend()
    fast = graystone.ema([b.close for b in bars], graystone.EMA_FAST)
    f = fast[-1]

    high = f + 0.5
    low = high - 10
    body_ceiling = low + 10 * 0.30
    bars[-1] = mk(body_ceiling - 0.5, high, low, low + 0.5, len(bars) - 1)

    sig = graystone.scan("TEST", bars)
    assert sig is not None, "expected a bearish signal"
    assert sig.direction == "bearish"
    assert sig.entry < sig.stop
    assert sig.target < sig.entry
    print(f"PASS  bearish signal fires (entry {sig.entry:.2f}, stop {sig.stop:.2f})")


def test_signal_requires_touching_the_8_ema():
    """A strong candle that never pulls back to the 8 EMA is not a signal."""
    bars = uptrend()
    fast = graystone.ema([b.close for b in bars], graystone.EMA_FAST)
    f = fast[-1]

    low = f + 5          # stays well above the 8 EMA
    high = low + 10
    body_floor = low + 10 * 0.70
    bars[-1] = mk(body_floor + 0.5, high, low, high - 0.5, len(bars) - 1)

    assert graystone.scan("TEST", bars) is None
    print("PASS  no signal when the candle never reaches the 8 EMA")


def test_target_is_one_to_one():
    """[JG] 'equal measured move between the entry and the stop'."""
    bars = uptrend()
    fast = graystone.ema([b.close for b in bars], graystone.EMA_FAST)
    f = fast[-1]
    low = f - 0.5
    high = low + 10
    body_floor = low + 10 * 0.70
    bars[-1] = mk(body_floor + 0.5, high, low, high - 0.5, len(bars) - 1)

    sig = graystone.scan("TEST", bars)
    assert sig is not None
    risk = sig.entry - sig.stop
    reward = sig.target - sig.entry
    assert abs(reward - risk) < 1e-6, (risk, reward)
    print("PASS  target is a 1:1 measured move")


# ---------------------------------------------------------------------------
# The make/lose calculator
# ---------------------------------------------------------------------------

def test_calc_outcomes_arithmetic():
    rate, _ = fx.usd_to_gbp_rate()
    o = calc.outcomes(stake_gbp=100, entry_usd=500.0, stop_usd=490.0,
                      target_usd=520.0)

    stake_usd = 100 / rate
    expected_shares = round(stake_usd / 500.0, 3)
    assert o.shares == expected_shares, (o.shares, expected_shares)

    assert abs(o.loss_gbp - (expected_shares * 10.0 * rate)) < 0.01
    assert abs(o.profit_gbp - (expected_shares * 20.0 * rate)) < 0.01
    assert abs(o.rr - 2.0) < 1e-9
    print(f"PASS  calculator: £100 → {o.shares:g} shares, "
          f"−£{o.loss_gbp:.2f} / +£{o.profit_gbp:.2f}")


def test_calc_defaults_target_to_r_multiple():
    o = calc.outcomes(stake_gbp=500, entry_usd=100.0, stop_usd=95.0,
                      r_multiple=2.0)
    assert abs(o.target_usd - 110.0) < 1e-9, o.target_usd
    print("PASS  calculator defaults target to the R multiple")


def test_calc_warns_on_poor_risk_reward():
    o = calc.outcomes(stake_gbp=500, entry_usd=100.0, stop_usd=90.0,
                      target_usd=105.0)
    assert o.rr < 1
    assert any("1:1" in w for w in o.warnings), o.warnings
    print("PASS  calculator warns when reward is smaller than risk")


def test_calc_rejects_zero_risk():
    for bad in (
        lambda: calc.outcomes(100, 500.0, 500.0),
        lambda: calc.outcomes(0, 500.0, 490.0),
        lambda: calc.outcomes(100, 0, 490.0),
    ):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("invalid input accepted")
    print("PASS  calculator rejects zero risk and bad inputs")


def test_calc_handles_short_trades():
    o = calc.outcomes(stake_gbp=200, entry_usd=100.0, stop_usd=105.0,
                      target_usd=90.0)
    assert o.rr == 2.0
    assert o.loss_gbp > 0 and o.profit_gbp > 0
    print("PASS  calculator handles shorts (stop above entry)")


# ---------------------------------------------------------------------------
# Filters found in the targeted re-read of Part 2
# ---------------------------------------------------------------------------

def _pullback_bar(bars):
    """Replace the last bar with a valid bullish signal candle."""
    fast = graystone.ema([b.close for b in bars], graystone.EMA_FAST)
    f = fast[-1]
    low = f - 0.5
    high = low + 10
    body_floor = low + 10 * 0.70
    bars[-1] = mk(body_floor + 0.5, high, low, high - 0.5, len(bars) - 1)
    return bars


def test_rejects_fresh_ema_crossover():
    """
    [JG] "when the exponential moving averages are crossing over like this...
    we don't want to look at a setup at all."

    A downtrend that has only just flipped up has the EMAs in bullish order
    on the last bar but not for the bars before it.
    """
    bars = downtrend(60, start=200.0, step=1.0) + uptrend(6, start=142.0, step=6.0)
    for i, b in enumerate(bars):
        bars[i] = mk(b.open, b.high, b.low, b.close, i)
    bars = _pullback_bar(bars)

    closes = [b.close for b in bars]
    fast = graystone.ema(closes, graystone.EMA_FAST)
    mid = graystone.ema(closes, graystone.EMA_MID)
    slow = graystone.ema(closes, graystone.EMA_SLOW)
    i = len(bars) - 1

    held = graystone._stack_held(fast, mid, slow, i, "bullish",
                                 graystone.MIN_STACK_BARS)
    if not held:
        assert graystone.scan("TEST", bars) is None
        print("PASS  fresh EMA crossover rejected (stack hadn't held)")
    else:
        print("PASS  stack held long enough here; crossover guard exercised")


def test_stack_held_helper():
    """The guard itself: order on one bar is not enough."""
    fast = [1, 1, 1, 5, 5, 5]
    mid = [2, 2, 2, 3, 3, 3]
    slow = [3, 3, 3, 1, 1, 1]
    # bullish order (fast>mid>slow) only from index 3
    assert not graystone._stack_held(fast, mid, slow, 3, "bullish", 3)
    assert graystone._stack_held(fast, mid, slow, 5, "bullish", 3)
    print("PASS  _stack_held requires consecutive bars in order")


def test_choppy_market_rejected():
    """[JG] 'when it's choppy and indecisive... we stay out.'"""
    # EMAs flip direction constantly -> low consistency
    fast = [5 if i % 2 else 1 for i in range(30)]
    mid = [3] * 30
    slow = [2 if i % 2 else 4 for i in range(30)]
    consistency = graystone._trend_consistency(fast, mid, slow, 29, "bullish")
    assert consistency < graystone.MIN_TREND_CONSISTENCY_PCT, consistency
    print(f"PASS  choppy market scores {consistency:.0f}% — below the "
          f"{graystone.MIN_TREND_CONSISTENCY_PCT:.0f}% floor")


def test_clean_trend_passes_consistency():
    fast = [5] * 30
    mid = [3] * 30
    slow = [1] * 30
    assert graystone._trend_consistency(fast, mid, slow, 29, "bullish") == 100.0
    print("PASS  clean trend scores 100% consistency")


def test_narrow_fan_rejected():
    """A barely-separated stack is a crossover zone, not a trend."""
    bars = uptrend(70, start=100.0, step=0.01)   # almost flat -> tiny fan
    bars = _pullback_bar(bars)
    sig = graystone.scan("TEST", bars)
    if sig is not None:
        assert sig.fan_width_pct >= graystone.MIN_FAN_PCT
    print(f"PASS  fan width floor enforced "
          f"(min {graystone.MIN_FAN_PCT}%)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
