"""
Jason Graystone's daily-timeframe method, from "Trading for Beginners" Parts 1 & 2.

This is a SEPARATE strategy from Cyfer's, and it is a SHARES method — it
runs on the S&P 500 and needs BROKER=alpaca. On the forex setup its only
use is the 8/20/50 EMA stack, which cyfer.py borrows as one confirmation
condition among five. The two must not otherwise be mixed:

  Cyfer      forex, 1-hour trend and levels, 5-minute trigger
  Graystone  shares, daily bars, one check per day, fully mechanical

Almost everything Graystone specifies is a number. Where he leaves a gap
it's marked [CHOICE] as usual.

Direct quotes are marked [JG].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from strategy import Bar

Direction = Literal["bullish", "bearish"]


# ===========================================================================
# Parameters
# ===========================================================================

# [JG] "the eight has to be before the 20 and the 20 has to be above the 50
#      in a bullish Trend"
EMA_FAST = 8
EMA_MID = 20
EMA_SLOW = 50

# [JG] "open and the close to be in the lower 30 of the entire candle body"
BODY_ZONE_PCT = 30.0

# [JG] "two Pips below the low of that signal candle". Pips are a forex unit;
# for shares this is expressed as a percentage of price.
# [CHOICE] 0.02% — roughly 10 cents on a $500 instrument.
ENTRY_BUFFER_PCT = 0.02

# [JG] "one to one" — "equal measured move between the entry and the stop"
TAKE_PROFIT_R = 1.0

# [JG] Part 1: "1% of account per trade maximum"
RISK_PER_TRADE_PCT = 1.0

# [JG] "when the exponential moving averages are crossing over like this...
#      we don't want to look at a setup at all"
# Checking stack ORDER alone isn't enough: EMAs that crossed yesterday are
# technically in order but are exactly what he says to avoid. So the stack
# must have HELD for this many consecutive bars.
# [CHOICE] 3 — he says "crossing over", not how long to wait after.
MIN_STACK_BARS = 3

# [JG] "the more that they fan out the higher probability that the trend is
#      strengthening" — and implicitly, a barely-separated stack is a
#      crossover zone. Minimum separation between fast and slow EMA.
# [CHOICE] 0.5% — he gives no number.
MIN_FAN_PCT = 0.5

# [JG] Part 1: "when it's choppy and indecisive and not sure then we stay
#      out"; "when we're in those types of environments then we don't
#      really have an edge". He never quantifies choppy, so this uses the
#      proportion of recent bars where the stack held.
# [CHOICE] 70% of the last 20 bars must hold the same stack direction.
CHOP_LOOKBACK = 20
MIN_TREND_CONSISTENCY_PCT = 70.0


@dataclass(frozen=True)
class GraystoneSignal:
    ticker: str
    direction: Direction
    entry: float
    stop: float
    target: float
    ema_fast: float
    ema_mid: float
    ema_slow: float
    fan_width_pct: float
    body_position_pct: float
    touched_ema: bool
    reasons: list[str]

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)


# ===========================================================================
# Indicators
# ===========================================================================

def ema(values: Sequence[float], period: int) -> list[float]:
    """
    Exponential moving average. Returns a list the same length as `values`;
    entries before the period is filled are None.
    """
    if not values or period <= 0:
        return []

    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out  # type: ignore[return-value]

    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed

    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev

    return out  # type: ignore[return-value]


def body_position_pct(bar: Bar) -> Optional[float]:
    """
    Where the open and close sit within the candle's full range, as a
    percentage from the low.

    [JG] A bearish signal needs open AND close in the lower 30%.
         A bullish signal needs both in the upper 30%.

    Returns the higher of the two positions for a bullish read, so the caller
    checks: bullish if >= 70, bearish if <= 30.
    """
    rng = bar.high - bar.low
    if rng <= 0:
        return None
    open_pos = (bar.open - bar.low) / rng * 100
    close_pos = (bar.close - bar.low) / rng * 100
    return min(open_pos, close_pos) if open_pos >= 50 and close_pos >= 50 \
        else max(open_pos, close_pos)


def _both_in_upper(bar: Bar, zone: float) -> bool:
    rng = bar.high - bar.low
    if rng <= 0:
        return False
    threshold = bar.low + rng * (1 - zone / 100)
    return bar.open >= threshold and bar.close >= threshold


def _both_in_lower(bar: Bar, zone: float) -> bool:
    rng = bar.high - bar.low
    if rng <= 0:
        return False
    threshold = bar.low + rng * (zone / 100)
    return bar.open <= threshold and bar.close <= threshold


def _stack_held(fast, mid, slow, i: int, direction: Direction,
                bars: int) -> bool:
    """
    [JG] "when the exponential moving averages are crossing over... we don't
    want to look at a setup at all."

    Stack order on one bar isn't enough — EMAs that crossed yesterday are in
    order but are exactly the crossover zone he rejects. This requires the
    ordering to have held for `bars` consecutive bars.
    """
    for k in range(i - bars + 1, i + 1):
        if k < 0:
            return False
        a, b, c = fast[k], mid[k], slow[k]
        if a is None or b is None or c is None:
            return False
        if direction == "bullish" and not (a > b > c):
            return False
        if direction == "bearish" and not (a < b < c):
            return False
    return True


def _trend_consistency(fast, mid, slow, i: int, direction: Direction) -> float:
    """
    [JG] "when it's choppy and indecisive... we stay out."

    He never quantifies chop, so this measures what fraction of the recent
    window held the same stack direction. A market flipping back and forth
    scores low and is rejected.
    """
    start = max(0, i - CHOP_LOOKBACK + 1)
    checked = held = 0
    for k in range(start, i + 1):
        a, b, c = fast[k], mid[k], slow[k]
        if a is None or b is None or c is None:
            continue
        checked += 1
        if direction == "bullish" and a > b > c:
            held += 1
        elif direction == "bearish" and a < b < c:
            held += 1
    return (held / checked * 100) if checked else 0.0


# ===========================================================================
# The signal
# ===========================================================================

def scan(ticker: str, daily_bars: Sequence[Bar]) -> Optional[GraystoneSignal]:
    """
    Check the most recently closed daily candle against Graystone's rules.

    Returns a signal only when every condition is met — unlike the Cyfer
    scan, which reports partial alignment. Graystone's rules are strict
    enough that partial matches aren't meaningful.
    """
    if len(daily_bars) < EMA_SLOW + 2:
        return None

    closes = [b.close for b in daily_bars]
    fast = ema(closes, EMA_FAST)
    mid = ema(closes, EMA_MID)
    slow = ema(closes, EMA_SLOW)

    i = len(daily_bars) - 1
    signal_bar = daily_bars[i]
    f, m, s = fast[i], mid[i], slow[i]

    if f is None or m is None or s is None:
        return None

    reasons: list[str] = []

    # --- Trend: EMA stack ---------------------------------------------------
    # [JG] 8 above 20 above 50 for bullish; reversed for bearish.
    bullish_stack = f > m > s
    bearish_stack = f < m < s

    if not (bullish_stack or bearish_stack):
        return None

    direction: Direction = "bullish" if bullish_stack else "bearish"

    # [JG] Reject anything near a crossover. The stack must have HELD.
    if not _stack_held(fast, mid, slow, i, direction, MIN_STACK_BARS):
        return None

    # [JG] Reject choppy conditions — "we stay out".
    consistency = _trend_consistency(fast, mid, slow, i, direction)
    if consistency < MIN_TREND_CONSISTENCY_PCT:
        return None

    fan_width = abs(f - s) / s * 100 if s else 0.0

    # A barely-separated stack IS a crossover zone.
    if fan_width < MIN_FAN_PCT:
        return None

    reasons.append(
        f"EMAs stacked {direction} — "
        f"8:{f:.2f} {'>' if bullish_stack else '<'} "
        f"20:{m:.2f} {'>' if bullish_stack else '<'} 50:{s:.2f}"
    )
    reasons.append(
        f"Stack held {MIN_STACK_BARS}+ bars, {consistency:.0f}% of the last "
        f"{CHOP_LOOKBACK} — not a crossover, not chop"
    )
    reasons.append(f"Fan width {fan_width:.2f}% — wider means stronger trend")

    # --- Signal candle: body in the right 30% ------------------------------
    rng = signal_bar.high - signal_bar.low
    if rng <= 0:
        return None

    if direction == "bullish":
        if not _both_in_upper(signal_bar, BODY_ZONE_PCT):
            return None
        reasons.append("Open and close both in the upper 30% of the candle")
    else:
        if not _both_in_lower(signal_bar, BODY_ZONE_PCT):
            return None
        reasons.append("Open and close both in the lower 30% of the candle")

    # --- Candle must interact with the 8 EMA -------------------------------
    # [JG] bullish: candle touches or dips below the 8 EMA (the pullback)
    #      bearish: candle touches or rises above it
    if direction == "bullish":
        touched = signal_bar.low <= f
        if not touched:
            return None
        reasons.append(f"Candle pulled back to the 8 EMA ({f:.2f})")
    else:
        touched = signal_bar.high >= f
        if not touched:
            return None
        reasons.append(f"Candle rallied into the 8 EMA ({f:.2f})")

    # --- Entry, stop, target -----------------------------------------------
    # [JG] Entry is a stop order beyond the signal candle; stop is the
    #      opposite side; target is a 1:1 measured move.
    buffer = signal_bar.close * ENTRY_BUFFER_PCT / 100

    if direction == "bullish":
        entry = signal_bar.high + buffer
        stop = signal_bar.low - buffer
        target = entry + (entry - stop) * TAKE_PROFIT_R
    else:
        entry = signal_bar.low - buffer
        stop = signal_bar.high + buffer
        target = entry - (stop - entry) * TAKE_PROFIT_R

    reasons.append(
        f"Entry {entry:.2f} · stop {stop:.2f} · target {target:.2f} "
        f"({TAKE_PROFIT_R:g}:1)"
    )

    return GraystoneSignal(
        ticker=ticker,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        ema_fast=f,
        ema_mid=m,
        ema_slow=s,
        fan_width_pct=fan_width,
        body_position_pct=(signal_bar.close - signal_bar.low) / rng * 100,
        touched_ema=touched,
        reasons=reasons,
    )


def format_signal(sig: GraystoneSignal, shares: float | None = None,
                  risk_gbp: float | None = None,
                  reward_gbp: float | None = None) -> str:
    """Discord message. Short, because this fires once a day and gets read properly."""
    arrow = "▲" if sig.direction == "bullish" else "▼"
    lines = [
        f"**{arrow} {sig.ticker} — {sig.direction.upper()} (Graystone, daily)**",
        "",
        f"**Entry** ${sig.entry:,.2f}  ·  **Stop** ${sig.stop:,.2f}  ·  "
        f"**Target** ${sig.target:,.2f}",
    ]

    if shares is not None and risk_gbp is not None:
        lines += [
            "",
            f"**{shares:g} shares** — risk **£{risk_gbp:,.2f}**, "
            f"reward **£{reward_gbp:,.2f}**",
        ]

    lines += [""]
    for r in sig.reasons:
        lines.append(f"✅ {r}")

    lines += [
        "",
        "*Daily timeframe — this candle has closed, so there's no rush. "
        "Place the order tonight; it triggers by itself if price moves there.*",
    ]

    # [JG] "I don't like holding trades over the weekend... I'd still
    #      prefer not to"
    from datetime import datetime
    if datetime.now().weekday() == 4:      # Friday
        lines.append(
            "\n⚠️ *It's Friday. Graystone: \"I don't like holding trades "
            "over the weekend.\" Entering now means carrying it through "
            "two days of closed markets and a Monday gap.*"
        )

    return "\n".join(lines)


def ema_stack_direction(bars) -> "str | None":
    """
    [JG] Which way the 8/20/50 stack points, if it is stacked at all.

    Used by the Cyfer strategy as a confirmation condition only — it never
    generates a signal on its own there.
    """
    closes = [b.close for b in bars]
    if len(closes) < EMA_SLOW + 1:
        return None
    fast, mid, slow = (ema(closes, EMA_FAST), ema(closes, EMA_MID),
                       ema(closes, EMA_SLOW))
    f, m, s = fast[-1], mid[-1], slow[-1]
    if None in (f, m, s):
        return None
    if f > m > s:
        return "bullish"
    if f < m < s:
        return "bearish"
    return None
