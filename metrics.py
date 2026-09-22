"""
Performance metrics — Sharpe, profit factor, drawdown, win rate.

WHY EVERY METRIC CARRIES AN ERROR BAR
-------------------------------------
These numbers are the input to decisions about real money, and all four of
them are unstable on small samples. A win rate measured over 10 trades has
roughly a +/-31 percentage point confidence interval. A Sharpe ratio over
5 days is barely distinguishable from noise.

A review that prints "Win Rate 60% — PASS" from 10 trades is not reporting
a fact, it is laundering randomness into a decision. So every metric here
returns its sample size and, where the maths allows, its standard error —
and format_row() marks anything below the sample floor as UNRELIABLE no
matter which side of the target it landed on.

Pure functions over lists of Position objects. No database, no network,
no clock, so the tests can hand-check every formula.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import fx
from config import CONFIG


# ===========================================================================
# One metric, with its uncertainty
# ===========================================================================

@dataclass(frozen=True)
class Metric:
    name: str
    value: Optional[float]
    target: Optional[float]
    direction: str              # ">=" or "<="
    unit: str = ""
    sample: int = 0
    stderr: Optional[float] = None
    note: str = ""

    @property
    def reliable(self) -> bool:
        return self.sample >= CONFIG.review.min_trades_to_diagnose

    @property
    def passes(self) -> Optional[bool]:
        if self.value is None or self.target is None:
            return None
        return (self.value >= self.target if self.direction == ">="
                else self.value <= self.target)

    @property
    def confidence_interval(self) -> Optional[tuple[float, float]]:
        """
        95% interval. None when the maths doesn't support one.

        Percentages are clamped to 0-100. The normal approximation happily
        reports a win rate interval reaching 103% on a small sample, which
        reads as a bug rather than as the warning it actually is.
        """
        if self.value is None or self.stderr is None:
            return None
        lo = self.value - 1.96 * self.stderr
        hi = self.value + 1.96 * self.stderr
        if self.unit == "%" and self.name == "Win Rate":
            lo, hi = max(0.0, lo), min(100.0, hi)
        return (lo, hi)

    @property
    def status(self) -> str:
        if self.value is None:
            return "NO DATA"
        if not self.reliable:
            return "UNRELIABLE"
        return "PASS" if self.passes else "FAIL"

    def fmt(self) -> str:
        if self.value is None:
            return "—"
        return f"{self.value:,.2f}{self.unit}"

    def fmt_target(self) -> str:
        if self.target is None:
            return "—"
        return f"{self.direction} {self.target:,.2f}{self.unit}"


# ===========================================================================
# Building blocks
# ===========================================================================

def _pnl_gbp(pos) -> float:
    """
    One trade's result in pounds.

    Prefers the broker's own realised figure where the position carries
    one, because the derived (exit - entry) * qty number leaves out the
    spread and so reports a better trade than actually happened. Every
    metric in this file is built on top of this one function, so getting
    it right here is what keeps the review honest.
    """
    if hasattr(pos, "realised_gbp"):
        return pos.realised_gbp()
    return fx.to_gbp(pos.realised())


def settled(positions: Sequence) -> list:
    """Closed trades with a real exit price, oldest first."""
    out = [p for p in positions
           if p.exit_price is not None and p.closed_at is not None]
    return sorted(out, key=lambda p: p.closed_at)


def win_rate(positions: Sequence) -> Metric:
    """
    Wins / (wins + losses). Scratches excluded — a flat trade is neither.

    Standard error uses the binomial formula sqrt(p(1-p)/n), which is why
    small samples produce such wide intervals.
    """
    trades = settled(positions)
    wins = sum(1 for p in trades if _pnl_gbp(p) > 0)
    losses = sum(1 for p in trades if _pnl_gbp(p) < 0)
    decided = wins + losses

    if decided == 0:
        return Metric("Win Rate", None, CONFIG.review.min_win_rate_pct,
                      ">=", "%", 0, None, "No decided trades yet.")

    p = wins / decided
    se = math.sqrt(p * (1 - p) / decided) * 100
    return Metric("Win Rate", p * 100, CONFIG.review.min_win_rate_pct,
                  ">=", "%", decided, se,
                  f"{wins}W / {losses}L")


def profit_factor(positions: Sequence) -> Metric:
    """
    Gross profit / gross loss. Above 1.0 means the winners outweigh the
    losers; below means they don't, whatever the win rate says.
    """
    trades = settled(positions)
    gross_win = sum(v for v in (_pnl_gbp(p) for p in trades) if v > 0)
    gross_loss = -sum(v for v in (_pnl_gbp(p) for p in trades) if v < 0)

    if not trades:
        return Metric("Profit Factor", None, CONFIG.review.min_profit_factor,
                      ">=", "", 0, None, "No closed trades.")
    if gross_loss == 0:
        return Metric("Profit Factor", None, CONFIG.review.min_profit_factor,
                      ">=", "", len(trades), None,
                      "No losing trades yet — undefined, not infinite.")

    return Metric("Profit Factor", gross_win / gross_loss,
                  CONFIG.review.min_profit_factor, ">=", "",
                  len(trades), None,
                  f"£{gross_win:,.2f} won / £{gross_loss:,.2f} lost")


def equity_curve(positions: Sequence, starting_gbp: float | None = None
                 ) -> list[float]:
    """Running account balance after each closed trade, in GBP."""
    start = (starting_gbp if starting_gbp is not None
             else CONFIG.display.account_gbp)
    curve = [start]
    for p in settled(positions):
        curve.append(curve[-1] + _pnl_gbp(p))
    return curve


def max_drawdown(positions: Sequence,
                 starting_gbp: float | None = None) -> Metric:
    """
    Largest peak-to-trough fall on the equity curve, as a percentage of the
    peak. Measured trade by trade, so it is the worst run the account
    actually lived through — not the difference between best and worst days.
    """
    trades = settled(positions)
    curve = equity_curve(trades, starting_gbp)

    peak = curve[0]
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)

    if not trades:
        return Metric("Max Drawdown", None, CONFIG.review.max_drawdown_pct,
                      "<=", "%", 0, None, "No closed trades.")

    return Metric("Max Drawdown", worst, CONFIG.review.max_drawdown_pct,
                  "<=", "%", len(trades), None,
                  f"peak £{max(curve):,.2f} → trough £{min(curve):,.2f}")


def daily_returns(positions: Sequence,
                  starting_gbp: float | None = None) -> list[float]:
    """Daily percentage returns, one per day that had a closed trade."""
    start = (starting_gbp if starting_gbp is not None
             else CONFIG.display.account_gbp)
    by_day: dict[str, float] = defaultdict(float)
    for p in settled(positions):
        by_day[p.closed_at[:10]] += _pnl_gbp(p)

    balance = start
    returns = []
    for day in sorted(by_day):
        if balance <= 0:
            break
        returns.append(by_day[day] / balance * 100)
        balance += by_day[day]
    return returns


def sharpe(positions: Sequence, starting_gbp: float | None = None) -> Metric:
    """
    Annualised Sharpe from daily returns, with its standard error.

    SE(Sharpe) ~= sqrt((1 + S^2/2) / n) on the per-period figure. Over a
    week that n is about 5, which is why the interval below is usually
    wider than the whole range of plausible answers. Reported anyway,
    because the width IS the finding.

    Risk-free rate is taken as zero — over a day it rounds to nothing and
    pretending otherwise adds false precision.
    """
    trades = settled(positions)
    returns = daily_returns(trades, starting_gbp)
    n = len(returns)
    target = CONFIG.review.min_sharpe

    if n < 2:
        return Metric("Sharpe Ratio", None, target, ">=", "", len(trades),
                      None, f"Needs 2+ trading days, has {n}.")

    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)

    if sd == 0:
        return Metric("Sharpe Ratio", None, target, ">=", "", len(trades),
                      None, "Zero variance — undefined.")

    per_day = mean / sd
    annual = per_day * math.sqrt(CONFIG.review.trading_days_per_year)
    se = math.sqrt((1 + per_day ** 2 / 2) / n) * math.sqrt(
        CONFIG.review.trading_days_per_year)

    return Metric("Sharpe Ratio", annual, target, ">=", "", len(trades), se,
                  f"from {n} trading day{'s' if n != 1 else ''}")


def net_pnl(positions: Sequence) -> Metric:
    trades = settled(positions)
    total = sum(_pnl_gbp(p) for p in trades)
    return Metric("Net P&L", total if trades else None, 0.0, ">=", "",
                  len(trades), None,
                  f"{len(trades)} closed trade{'s' if len(trades) != 1 else ''}")


def expectancy(positions: Sequence) -> Metric:
    """Average result per trade — the only figure that compounds."""
    trades = settled(positions)
    if not trades:
        return Metric("Per Trade", None, 0.0, ">=", "", 0, None, "")
    values = [_pnl_gbp(p) for p in trades]
    mean = sum(values) / len(values)
    if len(values) > 1:
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        se = math.sqrt(var / len(values))
    else:
        se = None
    return Metric("Per Trade", mean, 0.0, ">=", "", len(trades), se, "")


def slippage(positions: Sequence) -> dict:
    """
    What the gap between intended and actual prices cost.

    Jackson lists slippage as a friction point the reviewer should diagnose.
    It was previously computed during reconciliation and discarded.
    """
    trades = settled(positions)
    entry = [p.entry_slippage for p in trades if p.entry_slippage is not None]
    exit_ = [p.exit_slippage for p in trades if p.exit_slippage is not None]

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    # Converted per trade, by that instrument's own quote currency. A
    # slippage figure in yen added to one in dollars is not a number.
    total_gbp = sum(
        fx.pnl_to_gbp(
            (abs(p.entry_slippage or 0) + abs(p.exit_slippage or 0))
            * abs(p.qty), p.ticker)
        for p in trades)

    return {
        "entry_avg_usd": avg(entry),
        "exit_avg_usd": avg(exit_),
        "entry_samples": len(entry),
        "exit_samples": len(exit_),
        "total_cost_gbp": total_gbp if trades else 0.0,
    }


# ===========================================================================
# The set
# ===========================================================================

def all_metrics(positions: Sequence,
                starting_gbp: float | None = None) -> list[Metric]:
    """The five in Bob's benchmark table, in his order."""
    return [
        win_rate(positions),
        profit_factor(positions),
        max_drawdown(positions, starting_gbp),
        sharpe(positions, starting_gbp),
        net_pnl(positions),
    ]


def format_row(m: Metric, baseline: Optional[Metric] = None) -> str:
    """One Markdown table row, matching the required output structure."""
    prev = baseline.fmt() if baseline and baseline.value is not None else "—"
    ci = ""
    if m.confidence_interval and m.reliable:
        lo, hi = m.confidence_interval
        ci = f" ±{1.96 * m.stderr:,.1f}"
    return (f"| {m.name} | {prev} | {m.fmt()}{ci} | {m.fmt_target()} "
            f"| {m.status} |")
