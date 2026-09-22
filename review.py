"""
The weekly review — the slow loop.

ARCHITECTURE (from Bob's spec, derived from the Lewis Jackson breakdown)
------------------------------------------------------------------------
Fast loop: scan_loop, every 60s, executes a static ruleset and never
rewrites itself.

Slow loop: this. Weekly. Reads the ledger, scores it against numeric
benchmarks, clusters the losses to find the dominant failure mode, and
hands the result to a human. It proposes; it never writes.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not name a "primary failure mode" below the sample floor. The
spec asks for findings like "60% of losses hit stop within 3 candles" —
that sentence is worth something from 40 trades and worth nothing from 4,
and the difference is invisible once it is written down as a conclusion.
So below the floor the report prints every number it has and states
plainly that it is not diagnosing yet.

That refusal is the feature. An optimiser that tunes parameters on six
trades doesn't improve a strategy, it fits it to noise and then reports
the fit as progress.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import fx
import metrics
import tracker
from config import CONFIG


# ===========================================================================
# Clustering — where the losses come from
# ===========================================================================

@dataclass
class Cluster:
    label: str
    count: int
    total: int
    evidence: list[str] = field(default_factory=list)

    @property
    def share_pct(self) -> float:
        return (self.count / self.total * 100) if self.total else 0.0

    @property
    def dominant(self) -> bool:
        return self.share_pct >= CONFIG.review.cluster_threshold_pct


def _exit_reason(pos) -> str:
    note = pos.note or ""
    if "[stop]" in note:
        return "stopped out"
    if "[target]" in note:
        return "target hit"
    if "[eod]" in note:
        return "closed at the bell"
    if "[never filled]" in note:
        return "never filled"
    return "closed manually"


def cluster_losses(positions: Sequence) -> list[Cluster]:
    """
    Group losing trades by what they have in common.

    Four axes, because a loss can be caused at any of them: the exit
    (stop too tight), the entry (wrong signal), the clock (wrong session
    phase), or friction (slippage eating the edge).
    """
    losses = [p for p in metrics.settled(positions)
              if metrics._pnl_gbp(p) < 0]
    total = len(losses)
    if not total:
        return []

    out: list[Cluster] = []

    # --- by exit reason ---------------------------------------------------
    for reason, n in Counter(_exit_reason(p) for p in losses).most_common():
        ev = [f"#{p.id} {p.ticker}" for p in losses
              if _exit_reason(p) == reason][:4]
        out.append(Cluster(f"exit: {reason}", n, total, ev))

    # --- stopped out almost immediately -----------------------------------
    fast = [p for p in losses
            if (p.duration_seconds or 1e9) <= CONFIG.review.fast_stopout_seconds]
    if fast:
        mins = CONFIG.review.fast_stopout_seconds // 60
        out.append(Cluster(
            f"stopped within {mins} min of entry", len(fast), total,
            [f"#{p.id} {p.ticker} after {int(p.duration_seconds)}s"
             for p in fast[:4]]))

    # --- by session phase, if the context was recorded --------------------
    phases = Counter()
    for p in losses:
        ctx = p.context_json or ""
        for phase in ("macro", "late", "extended", "pm"):
            if f'"phase": "{phase}"' in ctx:
                phases[phase] += 1
                break
    for phase, n in phases.most_common():
        out.append(Cluster(f"session phase: {phase}", n, total, []))

    # --- by ticker --------------------------------------------------------
    for ticker, n in Counter(p.ticker for p in losses).most_common():
        if n > 1:
            out.append(Cluster(f"instrument: {ticker}", n, total, []))

    return sorted(out, key=lambda c: c.count, reverse=True)


# ===========================================================================
# Coherence — do the benchmarks and the live config agree?
# ===========================================================================

def coherence_warnings() -> list[str]:
    """
    Contradictions between the review targets and the running config.

    Worth checking before any trade happens, because a benchmark that the
    configuration makes unreachable will fail every week forever and the
    weekly report will keep proposing fixes for a problem that isn't in
    the strategy at all.
    """
    out = []
    rv, rk = CONFIG.review, CONFIG.risk

    # One losing trade costs risk_per_trade_pct of the account. If that is
    # bigger than the drawdown ceiling, the FIRST loss breaches it.
    if rk.risk_per_trade_pct > rv.max_drawdown_pct:
        ratio = rk.risk_per_trade_pct / rv.max_drawdown_pct
        out.append(
            f"**Drawdown target is unreachable.** Risk per trade is "
            f"{rk.risk_per_trade_pct:.0f}% but the drawdown ceiling is "
            f"{rv.max_drawdown_pct:.0f}%. A single losing trade is a "
            f"{rk.risk_per_trade_pct:.0f}% drawdown — {ratio:.0f}x the "
            f"limit. This benchmark will fail on the first loss and every "
            f"week after, whatever the strategy does. Either risk "
            f"≤{rv.max_drawdown_pct:.0f}% per trade, or raise the ceiling "
            f"to match the risk you chose."
        )

    # A 2:1 payoff needs a 33% win rate to break even. Demanding 52% on top
    # of 2:1 is asking for a profit factor above 2, not 1.5.
    r = CONFIG.cyfer.min_risk_reward
    if r > 0:
        breakeven = 100 / (1 + r)
        implied_pf = (rv.min_win_rate_pct / 100 * r) / \
                     ((1 - rv.min_win_rate_pct / 100) or 1e-9)
        if implied_pf > rv.min_profit_factor * 1.25:
            out.append(
                f"**Win-rate and profit-factor targets disagree.** At your "
                f"{r:.0f}:1 payoff, a {rv.min_win_rate_pct:.0f}% win rate "
                f"already implies a profit factor of {implied_pf:.1f} — well "
                f"above the {rv.min_profit_factor:.1f} you set. The win-rate "
                f"target is the binding one; profit factor will pass "
                f"automatically and tells you nothing. Break-even at this "
                f"payoff is {breakeven:.0f}%."
            )

    if rk.max_trades_per_day * 5 < rv.min_trades_to_diagnose:
        weeks = rv.min_trades_to_diagnose / (rk.max_trades_per_day * 5)
        out.append(
            f"**The weekly cadence can't fill a weekly sample.** At "
            f"{rk.max_trades_per_day} trades/day the ceiling is "
            f"{rk.max_trades_per_day * 5} per week, and the diagnosis floor "
            f"is {rv.min_trades_to_diagnose}. Even at maximum activity a "
            f"review can only diagnose every ~{weeks:.0f} weeks. In practice "
            f"most days produce no setup at all, so expect longer."
        )

    return out


# ===========================================================================
# The report
# ===========================================================================

def _window(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat(timespec="seconds")


def build(conn, days: int = 7, now: Optional[datetime] = None) -> str:
    """
    The full review, in the exact Markdown structure the spec requires.

    Returns a string. Writes nothing, changes nothing, proposes nothing it
    cannot support with the sample it has.
    """
    now = now or datetime.now(timezone.utc)
    cycle = tracker.closed_since(conn, _window(days))
    lifetime = tracker.closed_positions(conn)

    cycle_m = metrics.all_metrics(cycle)
    base_m = {m.name: m for m in metrics.all_metrics(lifetime)}
    settled = metrics.settled(cycle)
    floor = CONFIG.review.min_trades_to_diagnose

    L: list[str] = []
    L.append(f"# Strategy Review — {days}-day cycle")
    L.append(f"*Generated {now.strftime('%Y-%m-%d %H:%M')} UTC · "
             f"paper mode · read-only*")
    L.append("")

    # --- 1. performance ---------------------------------------------------
    L.append("## 1. Cycle Performance Summary")
    L.append("")
    L.append("| Metric | Previous Baseline | Past 7 Days | Target Benchmark "
             "| Status |")
    L.append("| :--- | :--- | :--- | :--- | :--- |")
    for m in cycle_m:
        L.append(metrics.format_row(m, base_m.get(m.name)))
    L.append("")

    if not settled:
        L.append("**No closed trades in this cycle.** Every figure above is "
                 "undefined — not zero, undefined. There is nothing to review "
                 "yet.")
        L.append("")
    elif len(settled) < floor:
        L.append(f"⚠️ **{len(settled)} closed trade"
                 f"{'s' if len(settled) != 1 else ''} — below the "
                 f"{floor}-trade floor.** Every status above reads "
                 f"UNRELIABLE regardless of which side of the target it "
                 f"landed on. The numbers are real; the conclusions you "
                 f"could draw from them are not.")
        L.append("")

    # --- 2. diagnostics ---------------------------------------------------
    L.append("## 2. Trade Log Diagnostics")
    L.append("")
    clusters = cluster_losses(cycle)

    if not settled:
        L.append("- **Primary Failure Mode:** none — no trades to analyse.")
        L.append("- **Key Data Evidence:** none.")
    elif len(settled) < floor:
        L.append(f"- **Primary Failure Mode:** *withheld.* Naming one from "
                 f"{len(settled)} trades would be pattern-matching on noise.")
        L.append("- **Key Data Evidence:** raw counts only, below.")
        L.append("")
        for c in clusters[:6]:
            L.append(f"  - {c.label}: {c.count}/{c.total} losses "
                     f"({c.share_pct:.0f}%)")
    else:
        top = clusters[0] if clusters else None
        if top and top.dominant:
            L.append(f"- **Primary Failure Mode:** {top.label} — "
                     f"{top.count} of {top.total} losing trades "
                     f"({top.share_pct:.0f}%).")
        elif top:
            L.append(f"- **Primary Failure Mode:** no single dominant cause. "
                     f"Largest group is {top.label} at {top.share_pct:.0f}%, "
                     f"below the {CONFIG.review.cluster_threshold_pct:.0f}% "
                     f"threshold for naming one.")
        else:
            L.append("- **Primary Failure Mode:** none — no losing trades.")

        L.append("- **Key Data Evidence:**")
        for c in clusters[:6]:
            ev = f" — {', '.join(c.evidence)}" if c.evidence else ""
            L.append(f"  - {c.label}: {c.count}/{c.total} "
                     f"({c.share_pct:.0f}%){ev}")
    L.append("")

    # --- friction ---------------------------------------------------------
    slip = metrics.slippage(cycle)
    if slip["entry_samples"]:
        L.append("**Execution friction**")
        if slip["entry_avg_usd"] is not None:
            L.append(f"- Average entry slippage: "
                     f"{slip['entry_avg_usd']:+.5f}/unit "
                     f"({slip['entry_samples']} fills)")
        if slip["exit_avg_usd"] is not None:
            L.append(f"- Average exit slippage: "
                     f"{slip['exit_avg_usd']:+.5f}/unit")
        L.append(f"- Total slippage cost this cycle: "
                 f"**£{slip['total_cost_gbp']:,.2f}**")
        L.append("")

    # --- 3. hypothesis ----------------------------------------------------
    L.append("## 3. Optimization Hypothesis")
    L.append("")
    if len(settled) < floor:
        need = floor - len(settled)
        L.append(f"- **Target Variable:** *none proposed this cycle.*")
        L.append(f"- **Reason:** {len(settled)}/{floor} trades. "
                 f"{need} more needed before a single-variable change can be "
                 f"tied to evidence rather than to chance.")
        L.append(f"- **What would change this:** trades, or a backtest. At "
                 f"{CONFIG.risk.max_trades_per_day} trades/day and most days "
                 f"producing no setup, {need} more trades is a matter of "
                 f"weeks. A backtest over historical data would supply "
                 f"hundreds immediately.")
    else:
        top = clusters[0] if clusters else None
        L.append("- **Target Variable:** `[to be chosen from the evidence "
                 "above]`")
        L.append(f"- **Current Value:** see `!params`")
        L.append("- **Proposed Value:** `[one change only]`")
        L.append(f"- **Hypothesis:** the dominant failure mode is "
                 f"{top.label if top else 'undetermined'}; a single "
                 f"adjustment targeting it should improve the binding "
                 f"metric without breaching the drawdown ceiling.")
        L.append("- **Reversion Criteria:** if the next cycle's profit "
                 "factor falls below this cycle's, revert to the previous "
                 "value and record the result as evidence against the "
                 "hypothesis.")
    L.append("")

    # --- 4. diff ----------------------------------------------------------
    L.append("## 4. Proposed Configuration Diff")
    L.append("")
    L.append("```yaml")
    if len(settled) < floor:
        L.append("# No change proposed — insufficient evidence.")
        L.append("strategy_parameters: {}   # unchanged")
    else:
        L.append("# Only the single modified field should differ")
        L.append("strategy_parameters:")
        L.append("  [target_variable]: [proposed_value]  "
                 "# Changed from [current_value]")
    L.append("```")
    L.append("")

    # --- 5. coherence -----------------------------------------------------
    warnings = coherence_warnings()
    if warnings:
        L.append("## 5. Benchmark Coherence")
        L.append("")
        L.append("*Problems in the targets themselves, not in the trading. "
                 "These would fail every cycle regardless of performance.*")
        L.append("")
        for w in warnings:
            L.append(f"- {w}")
        L.append("")

    L.append("---")
    L.append("*Read-only. No configuration was changed. "
             "Nothing here executes.*")

    return "\n".join(L)


DISCORD_LIMIT = 3900          # embed descriptions cap at 4096


def chunks(report: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """
    Split the report for Discord, preferring section boundaries.

    A report cut mid-table is unreadable, so splits happen at "## " headings
    where possible and only fall back to line boundaries when a single
    section is itself too long.
    """
    if len(report) <= limit:
        return [report]

    out, current = [], ""
    for block in report.split("\n## "):
        piece = block if not out and not current else "## " + block
        if len(current) + len(piece) + 1 > limit:
            if current:
                out.append(current.rstrip())
                current = ""
            while len(piece) > limit:
                cut = piece.rfind("\n", 0, limit)
                cut = cut if cut > 0 else limit
                out.append(piece[:cut].rstrip())
                piece = piece[cut:].lstrip("\n")
        current += ("\n" if current else "") + piece
    if current.strip():
        out.append(current.rstrip())
    return out


def summary_line(conn, days: int = 7) -> str:
    """One line for Discord, since the full report exceeds the embed limit."""
    cycle = tracker.closed_since(conn, _window(days))
    settled = metrics.settled(cycle)
    floor = CONFIG.review.min_trades_to_diagnose
    if not settled:
        return f"No closed trades in the last {days} days — nothing to review."
    pnl = sum(metrics._pnl_gbp(p) for p in settled)
    return (f"{len(settled)} trades, £{pnl:,.2f}. "
            f"{'Diagnosing.' if len(settled) >= floor else f'Below the {floor}-trade floor — reporting only.'}")
