"""
Every tunable number, in one place.

Tags:
  [BOOK pNN]  the Cyfer Academy guide states this, at that page.
  [JG]        Jason Graystone's videos.
  [CHOICE]    nobody stated it; it had to be decided for the code to run.
  [BOB]       Bob overrode the source deliberately.

Change one at a time. If results swing wildly on a small change, the edge
was in the parameter, not the method.
"""



from __future__ import annotations

from pathlib import Path as _EnvPath
from dotenv import load_dotenv as _load_env
_load_env(_EnvPath(__file__).parent / ".env")


import os
from dataclasses import dataclass, field

# ===========================================================================
# The six undefined parameters
# ===========================================================================

@dataclass(frozen=True)
class StrategyParams:
    """Shared chart plumbing, used by strategy.py regardless of strategy."""

    # [CHOICE] A swing high needs this many lower highs either side.
    # Every structural read downstream depends on it: trend, levels, BOS.
    swing_strength: int = 3

    # [BOOK p42] A break of structure is price moving beyond the level, and
    # the book draws it as a decisive move rather than a touch. Kept strict:
    # the candle BODY must close beyond, a wick through is not a break.
    bos_requires_close: bool = True

    # ---------------------------------------------------------------------
    # [BOB 2026-09-14] How aligned a setup must be to act. Six conditions.
    # Lowered for PAPER testing to generate trade history. A lower bar means
    # acting on partial evidence — more trades, weaker signals behind each.
    # BEFORE GOING LIVE: raise these.
    # ---------------------------------------------------------------------
    min_alert_score: int = 2
    min_auto_score: int = 3

    # [CHOICE 2026-09-23] At most one open position per pair. Without it,
    # a trending hour-after-hour market opens a fresh long every hour —
    # three EUR/USD longs is three times the per-trade risk riding on one
    # idea, which the per-trade limit quietly assumes can't happen. False
    # restores stacking.
    one_position_per_pair: bool = True

    # ---------------------------------------------------------------------
    # [FIX 2026-09-14] Bad-print guard. A 1m bar closed 0.52% away from the
    # real market on a thin feed; the bracket was priced off it, the limit
    # filled at the true price, and the stop landed above the entry. The
    # trade closed itself in 59 seconds.
    #
    # The test is whether the last bar is an outlier among ITS OWN
    # neighbours — a bad print spikes alone, a real move carries them.
    # ---------------------------------------------------------------------
    max_entry_deviation_pct: float = 0.25
    max_feed_offset_pct: float = 0.60
    entry_neighbour_bars: int = 5


# ===========================================================================
# The Cyfer strategy  [BOOK]
# ===========================================================================

@dataclass(frozen=True)
class CyferParams:
    """
    Parameters for the strategy in cyfer.py.

    Where the book gives a number, it is used and cited. Where it describes
    something without quantifying it — "virtually equal", "a long wick",
    "just beyond" — a threshold had to be invented, and is marked [CHOICE].
    """

    # [BOOK p45] A level needs a minimum of three rejections. This is the
    # one hard number the book gives for levels, and it is what stops every
    # small wiggle counting as support.
    level_min_touches: int = 3

    # [CHOICE] How close two swings must be to count as the same level.
    level_tolerance_pct: float = 0.25

    # [CHOICE] How close price must be to a level to count as "at" it.
    at_level_pct: float = 0.30

    # [BOOK p44] Support is a floor that price bounces UP from; resistance
    # is a ceiling it turns DOWN from. So price has to be on the right side
    # of the level for it to be doing that job. A close this far through
    # it means the level has broken, whatever the distance check says.
    #
    # [CHOICE] How far through is "broken". Kept well inside the stop
    # buffer, so a setup that passes can never put its stop closer to the
    # entry than (stop_buffer_pct - level_break_pct).
    #
    # [FIX 2026-09-22] Added after a live EUR/USD alert read price 10 pips
    # BELOW support as "at support". The stop then sat 7 pips from entry,
    # which inflated reward-to-risk to 6:1 and ticked two conditions green
    # for the wrong reason.
    level_break_pct: float = 0.05

    # [CHOICE] The book shows trends as five or six steps but sets no
    # minimum. Three of each is the fewest that can establish a pattern.
    trend_swings_required: int = 3

    # [BOOK p48] "a minimum 1:2 ratio" — risk one to make two.
    min_risk_reward: float = 2.0

    # [BOOK p44] Stops go "just beyond" the level. How far beyond is a
    # [CHOICE]: too tight and noise takes you out, too wide and the
    # reward-to-risk collapses.
    stop_buffer_pct: float = 0.15

    # [BOOK p36] A doji has "little or no body". Quantified here as the
    # body being at most this share of the candle's full range.
    doji_max_body_pct: float = 10.0

    # [BOOK p37] A "long" wick, quantified as this multiple of the body.
    wick_rejection_ratio: float = 2.0

    # [BOOK p47] Move the stop to entry once the trade is in profit. The
    # book gives the idea, not the trigger point; 1R is the [CHOICE].
    breakeven_at_r: float = 1.0
    breakeven_enabled: bool = True

    # [CHOICE] How many recent structure breaks to check for a break
    # against the trend direction.
    bos_lookback: int = 3

    # [CHOICE] Minimum higher-timeframe bars before the scan will run.
    min_htf_bars: int = 40

    # [BOOK p45] Higher timeframes take precedence — they filter the noise.
    # Trend and levels come from the first; the trigger from the second.
    htf: str = "1Hour"
    ltf: str = "5Min"


# ===========================================================================
# Risk — [BOOK] where the guide gives a number, [CHOICE] where it doesn't
# ===========================================================================

@dataclass(frozen=True)
class RiskParams:
    # ---------------------------------------------------------------------
    # [BOOK p53] "Most professional traders risk 0.5-1% per trade. The
    # reason is mathematical - even a string of 10 consecutive losses at 1%
    # leaves you with 90% of your capital."
    #
    # [BOB 2026-09-23] Set to 10% on Bob's direct instruction, ten times
    # the top of the book's range. At 10%, ten consecutive losses leave
    # GBP 349 of GBP 1,000 (compounding) or nothing at all (fixed size,
    # which is what this bot does - it sizes off account_gbp, not the live
    # balance). History: 20% (old strategy) -> 1% (book) -> 10% (Bob).
    #
    # On most Cyfer setups the full 10% is not physically possible: the
    # position would need more leverage than a UK retail account is
    # allowed (see max_leverage_* below). Those trades are cut down to the
    # largest size the limit allows rather than skipped, so the real risk
    # per trade will usually land between about 3% and 10%.
    # ---------------------------------------------------------------------
    risk_per_trade_pct: float = 10.0

    # [CHOICE] The book sets no daily cap. Was 4% at 1% risk. At 10% that
    # would lock the day after a single loss, so it is now two full losses.
    max_daily_loss_pct: float = 20.0

    # [OANDA UK retail] Maximum leverage. 30:1 (3.33% margin) on pairs of
    # USD, EUR, JPY, GBP, CAD and CHF - so EUR/USD, GBP/USD, USD/JPY.
    # 20:1 (5% margin) on everything else, which includes AUD/USD.
    # Source: OANDA Europe Markets, "Margin Rates and Leverage Ratios for
    # Retail Clients".
    max_leverage_major: float = 30.0
    max_leverage_other: float = 20.0
    leverage_major_ccys: tuple = ("USD", "EUR", "JPY", "GBP", "CAD", "CHF")

    # [CHOICE] Use at most this share of the limit, so the spread or a
    # small move between sizing and filling can't tip an order over it and
    # get it rejected for insufficient margin.
    leverage_headroom_pct: float = 90.0

    # [CHOICE] Kept at 6 for paper testing, to build trade history faster.
    max_trades_per_day: int = 6

    # [CHOICE] Halve size when the stop is unusually wide.
    oversized_stop_multiple: float = 1.8

    # [BOOK p62-65] Sit out high-impact news rather than trading into it.
    halve_on_news: bool = True

    # [BOOK p66] Overtrading and revenge trading are two of the four traps
    # the book names. A hard stop after consecutive losses is the only part
    # of that a program can enforce.
    max_consecutive_losses: int = 3


# ===========================================================================
# Sessions — the forex week  [BOOK p3-p5]
# ===========================================================================

@dataclass(frozen=True)
class SessionParams:
    """
    Forex does not open and close each day. It runs continuously from
    Sunday evening to Friday evening, New York time, and the "sessions"
    are the four financial centres whose working hours overlap the clock.

    [BOOK p3] Sydney, Tokyo, London and New York. [BOOK p3] The London/New
    York overlap is named the "golden hours" — both of the largest centres
    are open at once, volume is highest and spreads are tightest.

    Everything below is in NEW YORK time, because the forex week itself is
    defined by New York's 17:00 — that is when the trading day rolls over
    and when the week opens and shuts.

    The US equity clock that used to live here (09:30 open, 16:00 bell,
    15:30 last entry) was removed on 2026-09-15 with the move to forex.
    None of it transfers: there is no bell, no gap risk at 16:00, and no
    reason to be flat overnight when the market never stops.
    """
    timezone: str = "America/New_York"

    # --- the week ---------------------------------------------------------
    # Monday=0 ... Sunday=6, matching datetime.weekday().
    week_open_day: int = 6          # Sunday
    week_open: str = "17:00"        # 22:00 UK, year round
    week_close_day: int = 4         # Friday
    week_close: str = "17:00"

    # --- the four centres, in New York time  [BOOK p3] --------------------
    sydney_open: str = "17:00"
    sydney_close: str = "02:00"
    tokyo_open: str = "19:00"
    tokyo_close: str = "04:00"
    london_open: str = "03:00"
    london_close: str = "12:00"
    newyork_open: str = "08:00"
    newyork_close: str = "17:00"

    # --- the golden hours  [BOOK p3] --------------------------------------
    # London and New York both open. 13:00-17:00 London time.
    golden_start: str = "08:00"
    golden_end: str = "12:00"

    # [CHOICE] Entries are allowed across London and New York, not only in
    # the four-hour overlap. The book says the overlap is BEST, not that
    # the rest is untradeable, and four hours a day on a 1-hour chart is
    # too few looks to ever learn anything from the results. Set this True
    # to tighten to the overlap alone.
    golden_hours_only: bool = False

    entry_window_start: str = "03:00"   # London opens
    entry_window_end: str = "16:00"     # an hour before the day rolls over

    # [CHOICE] No entries in the first stretch after the Sunday open. The
    # book does not say this; it is the same judgement as the old equity
    # warm-up. Sunday spreads are at their widest of the week, and [BOOK p8]
    # is explicit that the spread is a real cost you pay on entry.
    sunday_warmup_minutes: int = 120

    # --- the weekend ------------------------------------------------------
    # [CHOICE] Flat before the close on Friday. Unlike the nightly flatten
    # this one is genuinely necessary: the market is shut for 48 hours and
    # reopens wherever the weekend's news puts it. A stop cannot fire
    # through a gap.
    flatten_before_weekend: bool = True
    weekend_flatten: str = "16:30"      # Friday, New York time

    # Daily summary after the 17:00 roll, so the day's numbers are final.
    eod_summary: str = "17:05"

    # Kept so older callers that still ask for these names keep working.
    @property
    def last_entry(self) -> str:
        return self.entry_window_end

    @property
    def eod_flatten(self) -> str:
        return self.weekend_flatten

    @property
    def market_close(self) -> str:
        return self.week_close


# ===========================================================================
# Instruments
# ===========================================================================

@dataclass(frozen=True)
class Instruments:
    """
    Currency pairs. The full list lives in pairs.DEFAULT_PAIRS; this holds
    the two the intraday scanner runs on every cycle.

    [BOOK p8] EUR/USD carries the narrowest spread of any pair, which at a
    1% risk budget matters more than choice does.
    [BOOK p9] The dollar is on one side of roughly 85% of all trades, so
    both defaults are dollar pairs.

    SPY and QQQ were here until 2026-09-15. They were shares, indivisible,
    and that is what made correct position sizing impossible.
    """
    primary: str = "EUR_USD"
    secondary: str = "GBP_USD"

    @property
    def watchlist(self) -> tuple:
        from pairs import DEFAULT_PAIRS
        return DEFAULT_PAIRS

    # [BOOK p45] The higher timeframe leads; the lower one triggers.
    bias_timeframe: str = "4Hour"
    confirm_timeframe: str = "1Hour"
    entry_timeframe: str = "5Min"


# ===========================================================================
# Display
# ===========================================================================

@dataclass(frozen=True)
class DisplayParams:
    # Everything Bob sees is GBP. The instruments are USD-denominated —
    # SPY and QQQ trade in dollars and there is no GBP version of them —
    # so every figure is converted at the live rate before display.
    currency: str = "GBP"
    symbol: str = "£"
    fx_pair: str = "USD/GBP"
    fx_cache_minutes: int = 60
    # Used only if the FX API is unreachable. Bot warns loudly when it falls back.
    fx_fallback_rate: float = float(os.environ.get("FX_FALLBACK_USD_GBP", "0.79"))

    # Account size in GBP, for position sizing.
    account_gbp: float = float(os.environ.get("ACCOUNT_GBP", "1000"))


# ===========================================================================
# Review benchmarks — [BOB'S SPEC, from the Lewis Jackson breakdown]
# ===========================================================================

@dataclass(frozen=True)
class ReviewParams:
    """
    The numeric targets the weekly review scores against.

    These are Bob's figures, not measured ones. Nothing here has been
    verified against this strategy — they are the bar it is being asked to
    clear, which is a different thing from a bar it has cleared.
    """
    min_sharpe: float = 1.2
    max_drawdown_pct: float = 5.0
    min_win_rate_pct: float = 52.0
    min_profit_factor: float = 1.5

    # How many closed trades before the review will DIAGNOSE rather than
    # merely report. Below this it prints the numbers and refuses to draw a
    # conclusion, because a pattern in six trades is noise with a haircut.
    #
    # 20 matches the floor already used in memory.py. Even 20 is generous:
    # a win rate measured over 20 trades carries roughly +/-11 percentage
    # points of error at 95% confidence.
    min_trades_to_diagnose: int = 20

    # A failure mode must appear in at least this share of losing trades
    # before it is named as "the primary" one.
    cluster_threshold_pct: float = 50.0

    # Trading days per year, for annualising the Sharpe ratio.
    trading_days_per_year: int = 252

    # Weekday the automatic review posts on. 0=Monday .. 4=Friday.
    # Friday after the close, so the cycle covers a whole trading week.
    review_weekday: int = 4

    # A "fast" stop-out: hit the stop within this many seconds of entry.
    # Used to distinguish stops that were too tight from entries that were
    # simply wrong.
    fast_stopout_seconds: int = 300


# ===========================================================================
# The AI reviewer  [BOB 2026-09-15]
# ===========================================================================

@dataclass(frozen=True)
class AIParams:
    """
    Settings for ai.py.

    Every number here bounds what the model is allowed to do or how long
    it is allowed to take. None of them let it do more.
    """

    # [CHOICE] Near-zero, because this is a judgement call that should come
    # out the same way twice on the same setup. Not exactly zero: some
    # providers behave oddly at 0 and it buys nothing here.
    temperature: float = 0.1

    # --- time budget ------------------------------------------------------
    # This sits in front of an order inside a 60-second loop. It is not
    # allowed to hang.
    timeout_seconds: float = 8.0
    max_attempts: int = 2
    total_budget_seconds: float = 20.0

    max_tokens_decision: int = 300
    max_tokens_postmortem: int = 300

    # --- how much power it has -------------------------------------------
    # [CHOICE] A hesitant rejection is not a reason to overrule a strategy
    # that has passed every deterministic check. It has to be fairly sure
    # before it stops a trade.
    min_veto_confidence: float = 0.60

    # [CHOICE] "abstain" — an API failure leaves the trade alone, because
    # the bot ran without any AI for weeks and an outage should not change
    # how it behaves. "hold" — an API failure blocks the trade, which is
    # what Bob's original spec asked for and is the right answer if the AI
    # is ever promoted from reviewer to decision maker.
    fail_mode: str = os.environ.get("AI_FAIL_MODE", "abstain").strip().lower()

    # --- the feedback loop ------------------------------------------------
    # [BOB 2026-09-15] Chose the hard floor. Below this many closed trades
    # the model is shown no loss history at all and is told so explicitly.
    # Matches the floor metrics.py already uses before it will diagnose
    # anything, and for the same reason: a pattern drawn from six trades is
    # noise with a haircut.
    history_min_trades: int = 20
    history_max_examples: int = 5

    postmortem_enabled: bool = True


@dataclass(frozen=True)
class Config:
    strategy: StrategyParams = field(default_factory=StrategyParams)
    cyfer: CyferParams = field(default_factory=CyferParams)
    risk: RiskParams = field(default_factory=RiskParams)
    sessions: SessionParams = field(default_factory=SessionParams)
    instruments: Instruments = field(default_factory=Instruments)
    display: DisplayParams = field(default_factory=DisplayParams)
    review: ReviewParams = field(default_factory=ReviewParams)
    ai: AIParams = field(default_factory=AIParams)


CONFIG = Config()


def parameter_report() -> str:
    """Shown on !params — so the choices are never invisible."""
    y, r, s = CONFIG.cyfer, CONFIG.risk, CONFIG.sessions
    return (
        "**From the book, with page numbers:**\n"
        f"• Level needs `{y.level_min_touches}` rejections *(p45)*\n"
        f"• Minimum reward-to-risk `{y.min_risk_reward:.0f}:1` *(p48)*\n"
        f"• Risk per trade `{r.risk_per_trade_pct}%` *(p53: 0.5–1%)*\n"
        f"• Stop to break-even at `{y.breakeven_at_r:.0f}R` *(p47)*\n"
        f"• Higher timeframe `{y.htf}` leads, `{y.ltf}` triggers *(p45)*\n\n"
        "**Numbers the book describes but never quantifies — my choices:**\n"
        f"• Swing strength: `{CONFIG.strategy.swing_strength}` bars either side\n"
        f"• Two swings are one level within `{y.level_tolerance_pct}%`\n"
        f"• \"At\" a level means within `{y.at_level_pct}%`\n"
        f"• Stop sits `{y.stop_buffer_pct}%` beyond the level\n"
        f"• A doji body is under `{y.doji_max_body_pct}%` of its range\n"
        f"• A \"long\" wick is `{y.wick_rejection_ratio}x` the body\n"
        f"• A trend needs `{y.trend_swings_required}` swings each way\n\n"
        "**Gates nobody stated:**\n"
        f"• Max daily loss `{r.max_daily_loss_pct}%` · "
        f"max `{r.max_trades_per_day}` trades/day\n"
        f"• Stop after `{r.max_consecutive_losses}` losses in a row *(p66)*\n"
        f"• Entries `{s.entry_window_start}`-`{s.entry_window_end}` ET "
        f"(London through New York); golden hours "
        f"`{s.golden_start}`-`{s.golden_end}` *(p3)*\n"
        f"• Flat by `{s.weekend_flatten}` ET Friday - the weekend gap\n\n"
        "*Change any of these and the signals change. That is the point — "
        "the results are yours to prove, not the book's to claim.*"
    )
