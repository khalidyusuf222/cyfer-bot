"""
Tests for the Cyfer strategy.

Every rule the book states with a number is tested against that number.
Every rule it describes without one is tested against the [CHOICE] made in
config — so if a threshold changes, the test says so rather than quietly
passing.

No network, no database, no clock.
"""

import cyfer
import strategy
from config import CONFIG
from strategy import Bar


def bar(o, h, l, c, i=0):
    return Bar(ts=f"t{i}", open=o, high=h, low=l, close=c)


def series(closes, spread=0.4):
    """Plain bars around a list of closes."""
    return [bar(c - spread / 2, c + spread, c - spread, c, i)
            for i, c in enumerate(closes)]


def zigzag(points, per_leg=6):
    """Bars that walk between turning points, so swings are detectable."""
    out, idx = [], 0
    for a, b in zip(points, points[1:]):
        for k in range(per_leg):
            price = a + (b - a) * (k + 1) / per_leg
            out.append(bar(price - 0.2, price + 0.3, price - 0.3, price, idx))
            idx += 1
    return out


# ---------------------------------------------------------------------------
# Candlesticks  [p34-37]
# ---------------------------------------------------------------------------

def test_bullish_and_bearish():
    """[p34-35] Close above open is bullish; below is bearish."""
    assert cyfer.is_bullish(bar(100, 105, 99, 104))
    assert cyfer.is_bearish(bar(104, 105, 99, 100))
    assert not cyfer.is_bullish(bar(104, 105, 99, 100))
    print("PASS  bullish/bearish read from open vs close")


def test_doji_is_almost_no_body():
    """[p36] Open and close virtually equal — little or no body."""
    tight = bar(100.0, 103.0, 97.0, 100.1)      # body 0.1 of range 6 = 1.7%
    fat = bar(100.0, 103.0, 97.0, 102.5)        # body 2.5 of range 6 = 42%
    assert cyfer.is_doji(tight)
    assert not cyfer.is_doji(fat)
    print(f"PASS  doji at <={CONFIG.cyfer.doji_max_body_pct}% body "
          f"({cyfer.body_pct(tight):.1f}% vs {cyfer.body_pct(fat):.1f}%)")


def test_engulfing_needs_to_pass_the_previous_open():
    """
    [p36] The book is specific: the engulfing candle closes beyond the
    previous candle's OPEN, not merely beyond its close.
    """
    prev = bar(100, 101, 98, 99)                 # small bearish
    full = bar(98.5, 102, 98.4, 100.5)           # closes above prev OPEN (100)
    short = bar(98.5, 100, 98.4, 99.5)           # only above prev close (99)

    assert cyfer.engulfing(prev, full) == "bullish"
    assert cyfer.engulfing(prev, short) is None
    print("PASS  engulfing must clear the previous open, not just its close")


def test_engulfing_body_must_be_larger():
    prev = bar(100, 106, 94, 94)                 # large bearish body
    small = bar(95, 96, 94.5, 95.5)              # tiny bullish
    assert cyfer.engulfing(prev, small) is None
    print("PASS  a smaller candle cannot engulf a larger one")


def test_bearish_engulfing():
    prev = bar(99, 101, 98.5, 100)               # small bullish
    cur = bar(100.5, 101, 97, 98.5)              # closes below prev open (99)
    assert cyfer.engulfing(prev, cur) == "bearish"
    print("PASS  bearish engulfing detected")


def test_wick_rejection_direction():
    """
    [p37] A long lower wick means lower prices were rejected — buyers
    stepped in — so it reads bullish. A long upper wick is the mirror.
    """
    long_lower = bar(100, 100.5, 94, 100.2)
    long_upper = bar(100, 106, 99.5, 99.8)
    assert cyfer.wick_rejection(long_lower) == "bullish"
    assert cyfer.wick_rejection(long_upper) == "bearish"
    print("PASS  lower wick reads bullish, upper wick bearish")


def test_balanced_candle_is_not_a_rejection():
    """
    Neither wick long enough relative to the body. The first version of
    this test used open=100 high=102 low=98 close=101, which actually HAS
    a lower wick twice the body — the code was right and the test was
    wrong. Kept as a reminder to check the arithmetic, not the intuition.
    """
    balanced = bar(100, 102, 99, 101.5)   # body 1.5, wicks 0.5 and 1.0
    assert cyfer.wick_rejection(balanced) is None, (
        f"body {cyfer.body(balanced)}, upper {cyfer.upper_wick(balanced)}, "
        f"lower {cyfer.lower_wick(balanced)}")
    print("PASS  a candle with modest wicks is not a rejection")


# ---------------------------------------------------------------------------
# Trend  [p39-41]
# ---------------------------------------------------------------------------

def test_uptrend_is_higher_highs_and_higher_lows():
    """[p39]"""
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    t = cyfer.trend_state(bars)
    assert t.kind == "uptrend", (t.kind, t.basis)
    assert t.direction == "bullish"
    print(f"PASS  uptrend detected — {t.basis}")


def test_downtrend_is_lower_highs_and_lower_lows():
    """[p40]"""
    bars = zigzag([200, 190, 195, 182, 187, 174, 179, 166])
    t = cyfer.trend_state(bars)
    assert t.kind == "downtrend", (t.kind, t.basis)
    assert t.direction == "bearish"
    print(f"PASS  downtrend detected — {t.basis}")


def test_range_is_consolidation_not_a_trend():
    """[p41] Oscillating in a band is consolidation, and must not trade."""
    bars = zigzag([100, 110, 100, 110, 100, 110, 100, 110])
    t = cyfer.trend_state(bars)
    assert t.kind == "consolidation", (t.kind, t.basis)
    assert t.direction is None
    assert not t.is_trending
    print("PASS  a range reads as consolidation, not a trend")


def test_too_few_swings_is_undetermined_not_a_guess():
    t = cyfer.trend_state(series([100, 101, 102]))
    assert t.kind == "undetermined"
    print("PASS  too little structure returns undetermined, not a guess")


# ---------------------------------------------------------------------------
# Levels  [p44-45]
# ---------------------------------------------------------------------------

def test_level_needs_three_rejections():
    """
    [p45] The book's one hard number for levels: a minimum of 3 rejections.
    Two touches must NOT qualify.

    Note the zigzags start HIGH. The first point of a series is never a
    swing — there are no bars before it to compare against — so starting
    low would silently give one fewer low than intended. The first draft of
    this test did exactly that and passed on empty lists.
    """
    assert CONFIG.cyfer.level_min_touches == 3, "the book says three"

    twice = zigzag([120, 100, 120, 100, 120])           # 2 interior lows
    thrice = zigzag([120, 100, 120, 100, 120, 100, 120])  # 3 interior lows

    two_lvls = [l for l in cyfer.find_levels(twice) if l.kind == "support"]
    three_lvls = [l for l in cyfer.find_levels(thrice) if l.kind == "support"]

    assert two_lvls == [], f"2 touches must not make a level: {two_lvls}"
    assert len(three_lvls) == 1, three_lvls
    assert three_lvls[0].touches >= 3
    print(f"PASS  2 rejections is not a level, "
          f"{CONFIG.cyfer.level_min_touches} is")


def test_level_price_is_the_cluster_average():
    bars = zigzag([120, 100, 120, 100.1, 120, 99.9, 120, 105])
    supports = [l for l in cyfer.find_levels(bars) if l.kind == "support"]
    assert supports, "expected a support level"
    lvl = supports[0]
    assert 99.0 <= lvl.price <= 101.5, lvl.price
    print(f"PASS  level sits at the cluster average (${lvl.price:.2f})")


def test_at_level_respects_the_tolerance():
    lvl = cyfer.Level(100.0, "support", 3)
    inside = 100.0 * (1 + CONFIG.cyfer.at_level_pct / 100 * 0.5)
    outside = 100.0 * (1 + CONFIG.cyfer.at_level_pct / 100 * 3)
    assert cyfer.at_level([lvl], inside, "support") is not None
    assert cyfer.at_level([lvl], outside, "support") is None
    print(f"PASS  'at a level' means within {CONFIG.cyfer.at_level_pct}%")


def test_double_bottom_detected():
    """[p46]"""
    bars = zigzag([120, 100, 115, 100.1, 118, 110])
    sw = strategy.find_swings(bars)
    assert cyfer.double_bottom(sw) is not None
    print("PASS  double bottom detected")


# ---------------------------------------------------------------------------
# Risk  [p47-48]
# ---------------------------------------------------------------------------

def test_minimum_reward_to_risk_is_two():
    """[p48] 'a minimum 1:2 ratio'."""
    assert CONFIG.cyfer.min_risk_reward == 2.0
    good = cyfer.Signal("X", "bullish", entry=100, stop=99, target=102)
    poor = cyfer.Signal("X", "bullish", entry=100, stop=99, target=100.5)
    assert good.rr == 2.0 and good.tradeable
    assert poor.rr == 0.5 and not poor.tradeable
    print("PASS  2:1 minimum enforced — 0.5:1 is refused")


def test_breakeven_moves_the_stop_to_entry():
    """[p47] Once in profit, move the stop to entry. It can no longer lose."""
    entry, stop = 100.0, 98.0                  # 1R = 2.00
    trigger = CONFIG.cyfer.breakeven_at_r

    assert cyfer.breakeven_stop(entry, stop, 100.5, "bullish") is None
    moved = cyfer.breakeven_stop(entry, stop, 100 + 2 * trigger, "bullish")
    assert moved == entry, moved
    print(f"PASS  stop moves to entry at {trigger:.0f}R in profit")


def test_breakeven_never_moves_backwards():
    """A stop already at or above entry must not be moved down."""
    assert cyfer.breakeven_stop(100.0, 100.0, 120.0, "bullish") is None
    print("PASS  break-even never moves a stop backwards")


def test_breakeven_handles_shorts():
    moved = cyfer.breakeven_stop(100.0, 102.0, 98.0, "bearish")
    assert moved == 100.0
    print("PASS  break-even works for a short as well")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def test_scan_returns_none_without_enough_bars():
    assert cyfer.scan("X", series([100] * 5), series([100] * 5)) is None
    print("PASS  scan refuses to run on too little data")


def test_consolidation_is_reported_as_a_missing_condition():
    bars = zigzag([100, 110, 100, 110, 100, 110, 100, 110])
    sig = cyfer.scan("X", bars, series([105, 105.2, 105.1]))
    assert sig is not None
    assert any("No clean trend" in c for c in sig.conditions_missing)
    print("PASS  a ranging market fails the trend condition")


def test_uptrend_scores_the_trend_condition():
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    sig = cyfer.scan("X", bars, series([133, 133.5, 134]))
    assert sig is not None
    assert sig.direction == "bullish"
    assert any("uptrend" in c for c in sig.conditions_met), sig.conditions_met
    print(f"PASS  uptrend scores ({sig.score}/{sig.total} conditions)")


def test_bad_print_is_refused():
    """
    Carried over from an earlier version of the bot. The failure was in the
    plumbing, not the strategy: one outlying tick priced a whole bracket
    and put the stop on the wrong side of the fill.
    """
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    ltf = series([133, 133, 133, 133, 133]) + series([140])   # lone spike
    sig = cyfer.scan("X", bars, ltf)
    assert sig is not None
    assert sig.entry == 0.0
    assert any("Bad print suspected" in c for c in sig.conditions_missing)
    print("PASS  a lone outlying tick still refuses to price a trade")


def test_normal_movement_is_not_a_bad_print():
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    ltf = series([133.0, 133.1, 133.2, 133.3, 133.4, 133.5])
    sig = cyfer.scan("X", bars, ltf)
    assert sig is not None
    assert not any("Bad print" in c for c in sig.conditions_missing)
    print("PASS  ordinary drift is not mistaken for a bad print")


def test_ema_disagreement_is_a_missing_condition():
    """[JG] The 8/20/50 stack, kept as confirmation only."""
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    agree = cyfer.scan("X", bars, series([133, 133.5, 134]),
                       ema_direction="bullish")
    clash = cyfer.scan("X", bars, series([133, 133.5, 134]),
                       ema_direction="bearish")
    assert any("EMA stack agrees" in c for c in agree.conditions_met)
    assert any("EMA stack says" in c for c in clash.conditions_missing)
    print("PASS  EMA stack counts for when it agrees, against when it doesn't")


def test_signal_never_claims_to_predict():
    bars = zigzag([100, 110, 105, 118, 112, 126, 120, 134])
    sig = cyfer.scan("X", bars, series([133, 133.5, 134]))
    text = cyfer.format_signal(sig)
    assert "not a prediction" in text
    assert "backtested" in text
    print("PASS  the alert says plainly that it isn't a prediction")


def test_scored_conditions_are_consistent():
    """score + missing must always equal total — no silent shrinking."""
    for points in ([100, 110, 105, 118, 112, 126, 120, 134],
                   [200, 190, 195, 182, 187, 174, 179, 166],
                   [100, 110, 100, 110, 100, 110, 100, 110]):
        bars = zigzag(points)
        sig = cyfer.scan("X", bars, series([points[-1]] * 4))
        assert sig.score + len(sig.conditions_missing) == sig.total
        assert sig.total >= 4, sig.total
    print("PASS  every condition lands in exactly one list")


# ---------------------------------------------------------------------------
# Holding a level  [p44]  — the live EUR/USD alert, 2026-09-22
# ---------------------------------------------------------------------------
#
# The alert read support 1.14527 as "at support" with price at 1.14426 —
# ten pips BELOW it. The stop, placed just under the level, then sat 7.1 pips
# from entry, reward-to-risk read 6.0:1, and two conditions ticked green for
# the wrong reason. These tests use those exact prices.

LIVE_SUPPORT = 1.14527
LIVE_PRICE = 1.14426


def fx_range(low=1.14557, high=1.14900, legs=8, per_leg=6, wick=0.0003):
    """A ranging EUR/USD chart whose swing lows cluster at `low - wick`."""
    points = [high if i % 2 == 0 else low for i in range(legs + 1)]
    out, idx = [], 0
    for a, b in zip(points, points[1:]):
        for k in range(per_leg):
            p = a + (b - a) * (k + 1) / per_leg
            out.append(bar(p - wick * 0.6, p + wick, p - wick, p, idx))
            idx += 1
    return out


def fx_ltf(end, n=8, step=0.00002):
    closes = [end + step * (n - 1 - i) for i in range(n)]
    return [bar(c + 0.00002, c + 0.00006, c - 0.00006, c, i)
            for i, c in enumerate(closes)]


def test_price_below_support_is_not_holding_it():
    lvl = cyfer.Level(LIVE_SUPPORT, "support", 5)
    assert not cyfer.holding(lvl, LIVE_PRICE)
    assert cyfer.at_level([lvl], LIVE_PRICE, "support") is None
    print(f"PASS  price {LIVE_PRICE} is not 'at' support {LIVE_SUPPORT} "
          f"— it's under it")


def test_price_above_support_still_counts():
    """The fix must not break the case the strategy is actually for."""
    lvl = cyfer.Level(LIVE_SUPPORT, "support", 5)
    above = LIVE_SUPPORT + 0.00040
    assert cyfer.holding(lvl, above)
    assert cyfer.at_level([lvl], above, "support") is lvl
    print("PASS  price just above support still reads as holding it")


def test_a_tiny_poke_through_is_tolerated():
    """A close a hair through the level hasn't broken it yet."""
    lvl = cyfer.Level(LIVE_SUPPORT, "support", 5)
    poke = LIVE_SUPPORT * (1 - CONFIG.cyfer.level_break_pct / 100 * 0.5)
    assert cyfer.holding(lvl, poke)
    print(f"PASS  a close within {CONFIG.cyfer.level_break_pct}% through "
          f"the level is still holding")


def test_resistance_mirrors_it():
    lvl = cyfer.Level(1.15000, "resistance", 4)
    assert cyfer.holding(lvl, 1.14950)
    assert not cyfer.holding(lvl, 1.15100)
    print("PASS  price above resistance isn't holding it either")


def test_a_holding_level_can_never_give_a_squeezed_stop():
    """
    The guarantee the fix buys. Anything that passes the holding check
    puts price at most level_break_pct through the level, and the stop sits
    stop_buffer_pct beyond it — so the stop is always at least the
    difference away. On EUR/USD that's about 11 pips, not 7.
    """
    y = CONFIG.cyfer
    lvl = cyfer.Level(LIVE_SUPPORT, "support", 5)
    worst = LIVE_SUPPORT * (1 - y.level_break_pct / 100) + 1e-9
    assert cyfer.holding(lvl, worst)
    stop = LIVE_SUPPORT - LIVE_SUPPORT * y.stop_buffer_pct / 100
    pips = (worst - stop) / 0.0001
    floor = LIVE_SUPPORT * (y.stop_buffer_pct - y.level_break_pct) / 100 / 0.0001
    assert pips >= floor - 0.01, (pips, floor)
    assert pips > 10, pips
    print(f"PASS  worst-case stop on a holding level is {pips:.1f} pips "
          f"(was 7.1 on the live alert)")


def test_live_alert_no_longer_claims_support():
    """The exact alert, rebuilt. It must not tick 'at support' or R:R."""
    sig = cyfer.scan("EUR_USD", fx_range(), fx_ltf(LIVE_PRICE),
                     ema_direction="bearish")
    met = " ".join(sig.conditions_met)
    assert "At support" not in met, sig.conditions_met
    assert "Reward-to-risk" not in met, sig.conditions_met
    assert sig.entry == 0 or abs(sig.entry - sig.stop) / 0.0001 > 10, \
        (sig.entry, sig.stop)
    print(f"PASS  live alert rebuilt: no false 'at support', no squeezed "
          f"stop ({sig.score}/{sig.total}, read as {sig.direction})")


def test_broken_support_is_named_as_broken():
    """When the check fails, say WHY — not just 'not at support'."""
    sig = cyfer.scan("EUR_USD", fx_range(), fx_ltf(LIVE_PRICE))
    joined = " ".join(sig.conditions_missing)
    assert "has broken" in joined, sig.conditions_missing
    print("PASS  a broken support is reported as broken")


def test_no_trend_direction_is_explained_not_silent():
    """
    With no trend there used to be a silent default to bullish. Now the
    alert states where the direction came from — and when it's a guess,
    it says so.
    """
    sig = cyfer.scan("EUR_USD", fx_range(), fx_ltf(LIVE_PRICE))
    notes = " ".join(sig.notes)
    assert "No trend to follow" in notes, sig.notes
    assert "guess" in notes, sig.notes
    print("PASS  with no trend, the alert says how it chose a direction")


def test_no_trend_at_resistance_reads_bearish():
    """A range sitting under its ceiling is a bearish read, not a
    bullish setup that's missing its support."""
    bars = fx_range()
    top = max(b.high for b in bars)
    sig = cyfer.scan("EUR_USD", bars, fx_ltf(top - 0.00010))
    assert sig.direction == "bearish", (sig.direction, sig.notes)
    assert any("resistance" in n for n in sig.notes), sig.notes
    print("PASS  a range at its ceiling reads bearish, from the level")


def test_ema_wording_no_longer_invents_a_trend():
    sig = cyfer.scan("EUR_USD", fx_range(), fx_ltf(LIVE_PRICE),
                     ema_direction="bullish")
    text = " ".join(sig.conditions_missing + sig.conditions_met)
    assert "trend says" not in text, text
    print("PASS  the EMA line no longer claims a trend that isn't there")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
