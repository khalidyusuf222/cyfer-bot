"""
Chart primitives — swings, trend structure, break of structure.

These are the generic building blocks used by the Cyfer strategy: what a
bar is, where the swing highs and lows are, and when a trend's structure
breaks. Nothing instrument-specific and nothing strategy-specific.

Nothing here predicts price. Every function answers a question of the form
"is this pattern present in these bars right now" — a fact about the past,
not a claim about the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from config import CONFIG

Direction = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class StructureBreak:
    index: int
    direction: Direction
    level: float              # the swing that was broken
    close: float              # the close that broke it


def find_swings(bars: Sequence[Bar], strength: int | None = None) -> list[Swing]:
    """
    A swing high is a bar whose high exceeds the highs of `strength` bars on
    both sides. Swing low is the mirror.

    [BOOK p39-42] The guide draws swings as the turning points of a trend
    but never says how many bars make one. `strength` is therefore a
    [CHOICE], and the single most consequential one in this codebase — it
    feeds the trend read, the support and resistance levels, and the break
    of structure check.
    """
    n = strength if strength is not None else CONFIG.strategy.swing_strength
    out: list[Swing] = []
    if len(bars) < 2 * n + 1:
        return out

    for i in range(n, len(bars) - n):
        window = bars[i - n: i + n + 1]
        centre = bars[i]

        if all(centre.high >= b.high for b in window) and \
           any(centre.high > b.high for b in window if b is not centre):
            out.append(Swing(i, centre.high, "high"))

        if all(centre.low <= b.low for b in window) and \
           any(centre.low < b.low for b in window if b is not centre):
            out.append(Swing(i, centre.low, "low"))

    return out


# ===========================================================================
# Break of structure  [BOOK p42-43]
# ===========================================================================

def find_structure_breaks(bars: Sequence[Bar],
                          swings: Sequence[Swing] | None = None) -> list[StructureBreak]:
    """
    [BOOK p42] A break of structure is price moving decisively beyond the
    last swing, and the guide draws it as a close through the level rather
    than a touch. Kept strict: the candle BODY must close beyond. A wick
    through the level is NOT a break.
    """
    sw = list(swings) if swings is not None else find_swings(bars)
    out: list[StructureBreak] = []

    # The latest swing high and low before each bar, found by walking both
    # lists once. It used to rebuild them for every bar, which made this
    # over half the backtest's running time. Same answers, tested against
    # the old version in test_cyfer.py.
    highs = sorted((s for s in sw if s.kind == "high"), key=lambda s: s.index)
    lows = sorted((s for s in sw if s.kind == "low"), key=lambda s: s.index)
    hi = lo = 0
    last_high = last_low = None

    for i, bar in enumerate(bars):
        while hi < len(highs) and highs[hi].index < i:
            last_high, hi = highs[hi], hi + 1
        while lo < len(lows) and lows[lo].index < i:
            last_low, lo = lows[lo], lo + 1

        if last_high is not None:
            level = last_high.price
            if bar.close > level:                       # body close, not wick
                out.append(StructureBreak(i, "bullish", level, bar.close))

        if last_low is not None:
            level = last_low.price
            if bar.close < level:
                out.append(StructureBreak(i, "bearish", level, bar.close))

    return _dedupe_breaks(out)


def _dedupe_breaks(breaks: list[StructureBreak]) -> list[StructureBreak]:
    """Keep only the first break of each level in each direction."""
    seen: set[tuple[float, str]] = set()
    out = []
    for b in breaks:
        key = (round(b.level, 6), b.direction)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out
