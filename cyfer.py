"""
The Cyfer strategy — trend, level, trigger.

SOURCE
------
Built from "The Ultimate Beginner's Guide to Trading" (Cyfer Academy,
Vol. I, 2026). Page references below are to that document.

It is a forex primer. These are the parts that are instrument-agnostic —
chart structure and risk — reimplemented for equities.

WHAT THE BOOK GIVES, AND WHAT IT DOESN'T
----------------------------------------
It defines the COMPONENTS precisely: what an uptrend is, what makes a
level a level, what an engulfing candle looks like, what a break of
structure means, what risk-reward is acceptable.

It does NOT give an entry algorithm. Nowhere does it say "when A and B and
C, buy." So the sequencing below — trend, then level, then trigger — is an
assembly of its parts, marked [CHOICE], not a rule lifted from the text.
The distinction matters: if this loses money, the assembly is as likely to
be at fault as the components.

THE ONE-LINE VERSION
--------------------
Trade with the trend, only at a level price has respected before, only on
a candle that shows rejection there, and only when the reward is at least
twice the risk.

Every function is pure and testable. Nothing here predicts anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

import strategy
from config import CONFIG
from strategy import Bar, Swing

Direction = Literal["bullish", "bearish"]
TrendKind = Literal["uptrend", "downtrend", "consolidation", "undetermined"]


# ===========================================================================
# Candlesticks  [BOOK p34-37]
# ===========================================================================

def body(bar: Bar) -> float:
    """Distance from open to close. [p34] The body is open-to-close."""
    return abs(bar.close - bar.open)


def candle_range(bar: Bar) -> float:
    return bar.high - bar.low


def body_pct(bar: Bar) -> float:
    """Body as a share of the whole candle. Small body = indecision."""
    rng = candle_range(bar)
    return (body(bar) / rng * 100) if rng > 0 else 0.0


def is_bullish(bar: Bar) -> bool:
    """[p34] Close above open — buyers were in control."""
    return bar.close > bar.open


def is_bearish(bar: Bar) -> bool:
    """[p35] Close below open — sellers were in control."""
    return bar.close < bar.open


def is_doji(bar: Bar, max_body_pct: float | None = None) -> bool:
    """
    [p36] Open and close virtually equal, leaving little or no body.
    Signals indecision — neither side gained control.

    The book says "virtually equal" without a number, so the threshold is
    a [CHOICE] in config.
    """
    limit = (max_body_pct if max_body_pct is not None
             else CONFIG.cyfer.doji_max_body_pct)
    return body_pct(bar) <= limit


def upper_wick(bar: Bar) -> float:
    return bar.high - max(bar.open, bar.close)


def lower_wick(bar: Bar) -> float:
    return min(bar.open, bar.close) - bar.low


def wick_rejection(bar: Bar,
                   min_ratio: float | None = None) -> Optional[Direction]:
    """
    [p37] A long wick means the market rejected those prices.

    A long upper wick: price pushed up and was sold back down — rejection
    of higher prices, so bearish. A long lower wick is the mirror: buyers
    stepped in, so bullish.

    Returns the direction the rejection implies, or None.
    """
    ratio = (min_ratio if min_ratio is not None
             else CONFIG.cyfer.wick_rejection_ratio)
    b = body(bar)
    if b <= 0:
        b = candle_range(bar) * 0.01 or 1e-9

    if lower_wick(bar) >= b * ratio and lower_wick(bar) > upper_wick(bar):
        return "bullish"
    if upper_wick(bar) >= b * ratio and upper_wick(bar) > lower_wick(bar):
        return "bearish"
    return None


def engulfing(prev: Bar, cur: Bar) -> Optional[Direction]:
    """
    [p36] Two candles. The first is smaller and runs with the prevailing
    trend. The second is larger, runs the opposite way, and its body
    engulfs the first's.

    Bullish engulfing: the second closes above the first's OPEN — the book
    is specific about that, not merely above the first's close.

    Returns the direction of the engulfing candle, or None.
    """
    if body(cur) <= body(prev):
        return None

    if is_bearish(prev) and is_bullish(cur):
        if cur.close > prev.open and cur.open <= prev.close:
            return "bullish"

    if is_bullish(prev) and is_bearish(cur):
        if cur.close < prev.open and cur.open >= prev.close:
            return "bearish"

    return None


# ===========================================================================
# Trend structure  [BOOK p39-41]
# ===========================================================================

@dataclass(frozen=True)
class TrendState:
    kind: TrendKind
    basis: str
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)

    @property
    def direction(self) -> Optional[Direction]:
        if self.kind == "uptrend":
            return "bullish"
        if self.kind == "downtrend":
            return "bearish"
        return None

    @property
    def is_trending(self) -> bool:
        return self.kind in ("uptrend", "downtrend")


def trend_state(bars: Sequence[Bar],
                swings: Sequence[Swing] | None = None) -> TrendState:
    """
    [p39] An uptrend is higher highs and higher lows.
    [p40] A downtrend is lower highs and lower lows.
    [p41] Neither, oscillating in a band, is consolidation.

    How many swings must agree is a [CHOICE] — the book shows pictures
    with five or six, and says nothing about a minimum.
    """
    sw = list(swings) if swings is not None else strategy.find_swings(bars)
    need = CONFIG.cyfer.trend_swings_required

    highs = [s.price for s in sw if s.kind == "high"][-need:]
    lows = [s.price for s in sw if s.kind == "low"][-need:]

    if len(highs) < need or len(lows) < need:
        return TrendState("undetermined",
                          f"only {len(highs)} highs / {len(lows)} lows, "
                          f"need {need} of each", highs, lows)

    rising_highs = all(b > a for a, b in zip(highs, highs[1:]))
    rising_lows = all(b > a for a, b in zip(lows, lows[1:]))
    falling_highs = all(b < a for a, b in zip(highs, highs[1:]))
    falling_lows = all(b < a for a, b in zip(lows, lows[1:]))

    if rising_highs and rising_lows:
        return TrendState("uptrend",
                          f"{len(highs)} higher highs and higher lows",
                          highs, lows)
    if falling_highs and falling_lows:
        return TrendState("downtrend",
                          f"{len(lows)} lower highs and lower lows",
                          highs, lows)

    return TrendState("consolidation",
                      "highs and lows are not stepping consistently either "
                      "way — price is ranging", highs, lows)


# ===========================================================================
# Support and resistance  [BOOK p44-45]
# ===========================================================================

@dataclass(frozen=True)
class Level:
    price: float
    kind: Literal["support", "resistance"]
    touches: int
    timeframe: str = ""

    def distance_pct(self, price: float) -> float:
        return abs(price - self.price) / self.price * 100 if self.price else 0.0


def find_levels(bars: Sequence[Bar],
                timeframe: str = "",
                swings: Sequence[Swing] | None = None) -> list[Level]:
    """
    [p45] A level is where price has rejected or reversed a MINIMUM OF
    3 TIMES. That number is the book's, not a choice — it is the one
    quantitative rule it gives for levels, and it is what stops every
    wiggle counting as support.

    Swings within a tolerance of each other are clustered; a cluster with
    enough members becomes a level at the average of its prices.
    """
    sw = list(swings) if swings is not None else strategy.find_swings(bars)
    tol = CONFIG.cyfer.level_tolerance_pct
    need = CONFIG.cyfer.level_min_touches

    out: list[Level] = []
    for kind, swing_kind in (("resistance", "high"), ("support", "low")):
        prices = sorted(s.price for s in sw if s.kind == swing_kind)
        used = [False] * len(prices)

        for i, anchor in enumerate(prices):
            if used[i]:
                continue
            cluster = [p for j, p in enumerate(prices)
                       if not used[j] and abs(p - anchor) / anchor * 100 <= tol]
            if len(cluster) >= need:
                for j, p in enumerate(prices):
                    if p in cluster and not used[j]:
                        used[j] = True
                out.append(Level(sum(cluster) / len(cluster), kind,
                                 len(cluster), timeframe))

    return sorted(out, key=lambda l: l.price)


def nearest_level(levels: Sequence[Level], price: float,
                  kind: str) -> Optional[Level]:
    candidates = [l for l in levels if l.kind == kind]
    return min(candidates, key=lambda l: abs(l.price - price)) \
        if candidates else None


def holding(level: Level, price: float) -> bool:
    """
    Is price on the side of this level that makes it a level?

    [BOOK p44] Support is a floor price bounces up from, so price must be
    at or above it. Resistance is a ceiling, so price must be at or below
    it. A close more than level_break_pct through means it has broken.
    """
    # getattr so that a cyfer.py uploaded without its matching config.py
    # still runs, rather than raising inside the scan loop — where the
    # error is caught and logged, and the bot goes on looking healthy while
    # never finding a setup again.
    pct = getattr(CONFIG.cyfer, "level_break_pct", 0.05)
    tol = level.price * pct / 100
    if level.kind == "support":
        return price >= level.price - tol
    return price <= level.price + tol


def at_level(levels: Sequence[Level], price: float,
             kind: str) -> Optional[Level]:
    """
    Is price sitting at a level of this kind, AND holding it?

    Near is not enough. The nearest support 10 pips above price is not
    support — price has already fallen through it.
    """
    lvl = nearest_level(levels, price, kind)
    if lvl is None:
        return None
    if lvl.distance_pct(price) > CONFIG.cyfer.at_level_pct:
        return None
    return lvl if holding(lvl, price) else None


# ===========================================================================
# Double top / double bottom  [BOOK p46]
# ===========================================================================

def double_top(swings: Sequence[Swing]) -> Optional[tuple[Swing, Swing]]:
    """
    [p46] Two peaks at roughly the same level — a reversal pattern.

    Head and shoulders, triangles, wedges and flags are also named on that
    page but drawn rather than defined, so they are not implemented. A
    picture is not a specification.
    """
    highs = [s for s in swings if s.kind == "high"]
    if len(highs) < 2:
        return None
    a, b = highs[-2], highs[-1]
    if abs(a.price - b.price) / a.price * 100 <= CONFIG.cyfer.level_tolerance_pct:
        return (a, b)
    return None


def double_bottom(swings: Sequence[Swing]) -> Optional[tuple[Swing, Swing]]:
    lows = [s for s in swings if s.kind == "low"]
    if len(lows) < 2:
        return None
    a, b = lows[-2], lows[-1]
    if abs(a.price - b.price) / a.price * 100 <= CONFIG.cyfer.level_tolerance_pct:
        return (a, b)
    return None


# ===========================================================================
# The signal
# ===========================================================================

@dataclass
class Signal:
    ticker: str
    direction: Direction
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    level: Optional[Level] = None
    trend: Optional[TrendState] = None
    conditions_met: list[str] = field(default_factory=list)
    conditions_missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # The two parts of the book's core setup that aren't the level itself.
    # Set by scan(); a hand-built Signal has neither.
    trend_ok: bool = False
    trigger_ok: bool = False

    @property
    def score(self) -> int:
        return len(self.conditions_met)

    @property
    def total(self) -> int:
        return len(self.conditions_met) + len(self.conditions_missing)

    @property
    def is_complete(self) -> bool:
        return not self.conditions_missing

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def rr(self) -> float:
        r = self.risk_per_share
        return (self.reward_per_share / r) if r > 0 else 0.0

    @property
    def core_missing(self) -> list[str]:
        """Which of the book's three (trend, level, trigger) aren't here."""
        out = []
        if not self.trend_ok:
            out.append("trend")
        if self.level is None:
            out.append("level")
        if not self.trigger_ok:
            out.append("trigger candle")
        return out

    @property
    def tradeable(self) -> bool:
        """
        May an order be placed on this? Priced, at least 2:1 [p48], and
        (with require_core on) the book's trend, level and trigger all
        present [p39-48]. The score only decides what gets posted.
        """
        priced = (self.entry > 0 and self.stop > 0 and self.target > 0
                  and self.rr >= CONFIG.cyfer.min_risk_reward)
        if not priced:
            return False
        return not (CONFIG.cyfer.require_core and self.core_missing)


def scan(ticker: str,
         bars_htf: Sequence[Bar],
         bars_ltf: Sequence[Bar],
         ema_direction: Optional[Direction] = None) -> Optional[Signal]:
    """
    Run the strategy over one instrument.

    bars_htf  the higher timeframe — trend and levels come from here.
              [p45] Higher timeframes take precedence; they cut the noise.
    bars_ltf  the lower timeframe — the entry trigger and price come from here.
    ema_direction  optional Graystone 8/20/50 stack direction, used as a
              confirmation condition only.

    Returns a Signal describing which conditions hold. Returns None only
    when there aren't enough bars to say anything at all.
    """
    c = CONFIG.cyfer

    if len(bars_htf) < c.min_htf_bars or len(bars_ltf) < 3:
        return None

    swings_htf = strategy.find_swings(bars_htf)
    trend = trend_state(bars_htf, swings_htf)
    price = bars_ltf[-1].close
    levels = find_levels(bars_htf, timeframe="HTF", swings=swings_htf)

    # --- which way is this setup facing? ---------------------------------
    #
    # With a trend, the trend decides  [BOOK p39-41: trade with it].
    #
    # Without one, there used to be a silent default to "bullish", and every
    # later check was then measured against a direction nothing on the
    # chart had chosen. A ranging market at resistance would be reported as
    # a bullish setup missing its support. Now the direction comes from the
    # level price is actually holding, and if it has to fall back, the
    # alert says so rather than pretending it read something.
    if trend.is_trending:
        direction = trend.direction
    else:
        on_support = at_level(levels, price, "support")
        on_resist = at_level(levels, price, "resistance")
        if on_resist and not on_support:
            direction = "bearish"
            why = f"price is holding resistance `{fmt(on_resist.price, ticker)}`"
        elif on_support and not on_resist:
            direction = "bullish"
            why = f"price is holding support `{fmt(on_support.price, ticker)}`"
        elif on_support and on_resist:
            nearer = min((on_support, on_resist),
                         key=lambda l: l.distance_pct(price))
            direction = "bullish" if nearer.kind == "support" else "bearish"
            why = f"price is nearest {nearer.kind} `{fmt(nearer.price, ticker)}`"
        elif ema_direction:
            direction = ema_direction
            why = "no level is holding, so the EMA stack was used"
        else:
            direction = "bullish"
            why = "no level is holding and no EMA stack was given — a guess"

    sig = Signal(ticker=ticker, direction=direction,     # type: ignore[arg-type]
                 trend=trend)
    if not trend.is_trending:
        sig.notes.append(f"No trend to follow, so this is read as "
                         f"{direction} because {why}.")

    # --- 1. trend  [p39-41] ------------------------------------------------
    sig.trend_ok = trend.is_trending
    if trend.is_trending:
        sig.conditions_met.append(
            f"Trend: **{trend.kind}** ({trend.basis})")
    else:
        sig.conditions_missing.append(
            f"No clean trend — {trend.kind} ({trend.basis}). "
            f"The book's setups need a trend to run with.")

    # --- 2. at a level, and holding it  [p44-45] --------------------------
    wanted_kind = "support" if sig.direction == "bullish" else "resistance"
    near = nearest_level(levels, price, wanted_kind)
    level = None

    if near is None:
        sig.conditions_missing.append(
            f"No {wanted_kind} with {c.level_min_touches}+ rejections")
    elif near.distance_pct(price) > c.at_level_pct:
        sig.conditions_missing.append(
            f"Not at {wanted_kind} — nearest is "
            f"`{fmt(near.price, ticker)}`, "
            f"{near.distance_pct(price):.2f}% away "
            f"(need within {c.at_level_pct}%)")
    elif not holding(near, price):
        side = "below" if wanted_kind == "support" else "above"
        sig.conditions_missing.append(
            f"Price `{fmt(price, ticker)}` is {side} {wanted_kind} "
            f"`{fmt(near.price, ticker)}` — the level has broken, so it "
            f"isn't doing a {wanted_kind}'s job any more")
    else:
        level = near
        sig.level = level
        sig.conditions_met.append(
            f"At {level.kind} `{fmt(level.price, ticker)}` and holding "
            f"({level.touches} rejections, needs {c.level_min_touches})")

    # --- 3. the trigger candle  [p36-37] ----------------------------------
    prev, cur = bars_ltf[-2], bars_ltf[-1]
    eng = engulfing(prev, cur)
    rej = wick_rejection(cur)

    if eng == sig.direction:
        sig.trigger_ok = True
        sig.conditions_met.append(
            f"{eng.title()} engulfing candle — body engulfs the previous")
    elif rej == sig.direction:
        sig.trigger_ok = True
        sig.conditions_met.append(
            f"Wick rejection {rej} — price pushed through and was pushed back")
    elif is_doji(cur):
        sig.conditions_missing.append(
            "Doji — indecision, neither side in control. Not a trigger.")
    else:
        sig.conditions_missing.append(
            f"No {sig.direction} trigger candle (engulfing or wick rejection)")

    # --- 4. structure hasn't broken against us  [p42-43] ------------------
    breaks = strategy.find_structure_breaks(bars_htf, swings_htf)
    against = [b for b in breaks[-c.bos_lookback:]
               if b.direction != sig.direction]
    if against:
        last = against[-1]
        sig.conditions_missing.append(
            f"Break of structure {last.direction} at "
            f"`{fmt(last.level, ticker)}` — "
            f"the trend may already have turned")
    else:
        sig.conditions_met.append("No break of structure against the trend")

    # --- 5. EMA stack agrees  [Graystone, kept at Bob's request] ----------
    if ema_direction is None:
        sig.notes.append("EMA stack not supplied — condition skipped.")
    elif ema_direction == sig.direction:
        sig.conditions_met.append(
            f"8/20/50 EMA stack agrees ({ema_direction})")
    else:
        sig.conditions_missing.append(
            f"EMA stack says {ema_direction}, against this "
            f"{sig.direction} setup")

    # --- 6. is the entry price trustworthy?  [FIX 2026-09-14] -------------
    #
    # Carried over from an earlier version of the bot because the failure was in
    # the plumbing, not the strategy. A single bad print on a thin feed put
    # the whole bracket 0.52% off the real market; the limit filled at the
    # true price and the stop landed above the entry.
    #
    # A bad print is an outlier among its OWN neighbours. A real move
    # carries them with it.
    s_cfg = CONFIG.strategy
    window = bars_ltf[-(s_cfg.entry_neighbour_bars + 1):-1]
    if window:
        neighbours = sorted(b.close for b in window)
        local = neighbours[len(neighbours) // 2]
        drift = abs(price - local) / local * 100 if local else 0.0
        if drift > s_cfg.max_entry_deviation_pct:
            sig.conditions_missing.append(
                f"Bad print suspected: `{fmt(price, ticker)}` is "
                f"{drift:.2f}% away from the last {len(window)} bars "
                f"(~`{fmt(local, ticker)}`). "
                f"Refusing to price a trade off a single outlying tick.")
            sig.entry = sig.stop = sig.target = 0.0
            return sig

    # --- 7. levels: entry, stop, target  [p44, p48] -----------------------
    if level:
        buffer = level.price * c.stop_buffer_pct / 100
        sig.entry = price

        bullish = sig.direction == "bullish"
        # [p44] stops just beyond the level
        sig.stop = level.price - buffer if bullish else level.price + buffer
        risk = abs(price - sig.stop)
        measured = (price + risk * c.min_risk_reward if bullish
                    else price - risk * c.min_risk_reward)

        in_the_way = None
        if c.target_at_next_level:
            # [p51] the target is the next level; [p45] of either kind,
            # because a broken support is the next resistance
            if bullish:
                ahead = [l for l in levels if l.price > max(price, level.price)]
                in_the_way = min(ahead, key=lambda l: l.price, default=None)
            else:
                ahead = [l for l in levels if l.price < min(price, level.price)]
                in_the_way = max(ahead, key=lambda l: l.price, default=None)
            sig.target = in_the_way.price if in_the_way else measured
        elif bullish:
            opposing = nearest_level(
                [l for l in levels if l.price > price], price, "resistance")
            sig.target = max(opposing.price, measured) if opposing else measured
        else:
            opposing = nearest_level(
                [l for l in levels if l.price < price], price, "support")
            sig.target = min(opposing.price, measured) if opposing else measured

        to = (f" to the next level `{fmt(in_the_way.price, ticker)}`"
              if in_the_way else "")
        if sig.rr >= c.min_risk_reward:
            sig.conditions_met.append(
                f"Reward-to-risk {sig.rr:.1f}:1{to} (minimum "
                f"{c.min_risk_reward:.0f}:1)")
        else:
            sig.conditions_missing.append(
                f"Reward-to-risk only {sig.rr:.1f}:1{to}, below the "
                f"{c.min_risk_reward:.0f}:1 minimum the book sets")
    else:
        sig.conditions_missing.append(
            "No level to place a stop against, so no trade can be priced")

    return sig


# ===========================================================================
# Break-even  [BOOK p47]
# ===========================================================================

def breakeven_stop(entry: float, stop: float, current: float,
                   direction: Direction = "bullish") -> Optional[float]:
    """
    [p47] Once a trade has moved in your favour, move the stop to the entry
    price. From then on the trade cannot lose.

    The book gives the idea but not the trigger point, so how far into
    profit before moving is a [CHOICE].

    Returns the new stop price, or None if it isn't time yet.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    trigger_r = CONFIG.cyfer.breakeven_at_r
    if direction == "bullish":
        if current >= entry + risk * trigger_r and stop < entry:
            return entry
    else:
        if current <= entry - risk * trigger_r and stop > entry:
            return entry
    return None


# ===========================================================================
# Presentation
# ===========================================================================

def fmt(price: float, ticker: str = "") -> str:
    """
    Print a price the way the instrument is actually quoted.

    "$1.10" told you nothing useful about EUR/USD — the whole trade lives
    in the fourth and fifth decimals. Yen pairs get three, everything else
    five, and a ticker that is not a pair falls back to dollars.
    """
    try:
        import pairs
        pair = pairs.parse(ticker)
        return f"{price:.{pair.displayed_decimals}f}"
    except Exception:  # noqa: BLE001
        return f"${price:,.2f}"


def pips(a: float, b: float, ticker: str = "") -> str:
    try:
        import pairs
        return f"{pairs.parse(ticker).pips_between(a, b):.1f} pips"
    except Exception:  # noqa: BLE001
        return f"{abs(a - b):.2f}"


def format_signal(sig: Signal, session_note: str = "") -> str:
    lines = [f"**{sig.ticker} — {sig.score}/{sig.total} conditions aligned "
             f"{sig.direction}**", ""]

    for c in sig.conditions_met:
        lines.append(f"✅ {c}")
    for c in sig.conditions_missing:
        lines.append(f"⬜ {c}")

    if sig.entry and sig.stop and sig.target:
        t = sig.ticker
        lines += [
            "",
            f"Entry ~`{fmt(sig.entry, t)}` · stop `{fmt(sig.stop, t)}` "
            f"({pips(sig.entry, sig.stop, t)} away) · "
            f"target `{fmt(sig.target, t)}`",
            f"Reward-to-risk **{sig.rr:.1f}:1**",
            f"Size it with `!size {t} {fmt(sig.entry, t)} "
            f"{fmt(sig.stop, t)}`",
        ]

    if (sig.entry and CONFIG.cyfer.require_core and sig.core_missing):
        lines += ["", f"Won't be auto-traded: no "
                      f"{' or '.join(sig.core_missing)}. The book's trade "
                      f"needs a trend, a level and a trigger candle."]

    if session_note:
        lines += ["", session_note]

    for n in sig.notes:
        lines.append(f"\n*{n}*")

    lines.append(
        "\n*Conditions present on the chart, not a prediction. The book "
        "defines the parts; the way they're combined here is a choice. "
        "`!backtest` shows how these rules did on past prices.*")

    return "\n".join(lines)
