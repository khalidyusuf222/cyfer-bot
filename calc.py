"""
"If I put in £X, what could I make or lose?"

Pure arithmetic on numbers you supply. No forecasting — it doesn't know
whether the trade will win, only what each outcome is worth.
"""

from __future__ import annotations

from dataclasses import dataclass

import fx


@dataclass(frozen=True)
class Outcome:
    stake_gbp: float
    entry_usd: float
    stop_usd: float
    target_usd: float
    shares: float
    exposure_gbp: float
    loss_gbp: float          # if the stop is hit
    profit_gbp: float        # if the target is hit
    breakeven_pct: float     # move needed just to cover nothing
    stop_move_pct: float
    target_move_pct: float
    rr: float
    warnings: list[str]


def outcomes(stake_gbp: float,
             entry_usd: float,
             stop_usd: float,
             target_usd: float | None = None,
             r_multiple: float = 2.0) -> Outcome:
    """
    Given how much you're putting in and your levels, work out both outcomes.

    stake_gbp   — what you're actually putting into this position
    entry_usd   — price you'd get in at
    stop_usd    — where you'd get out if wrong
    target_usd  — where you'd take profit (defaults to r_multiple × risk)
    """
    warnings: list[str] = []

    if stake_gbp <= 0:
        raise ValueError("Stake must be more than zero.")
    if entry_usd <= 0:
        raise ValueError("Entry price must be more than zero.")

    long_trade = stop_usd < entry_usd
    risk_per_share = abs(entry_usd - stop_usd)

    if risk_per_share == 0:
        raise ValueError("Your stop is the same as your entry — no risk defined.")

    if target_usd is None:
        target_usd = (entry_usd + risk_per_share * r_multiple if long_trade
                      else entry_usd - risk_per_share * r_multiple)

    reward_per_share = abs(target_usd - entry_usd)

    stake_usd = fx.to_usd(stake_gbp)
    shares = round(stake_usd / entry_usd, 3)

    loss_gbp = fx.to_gbp(shares * risk_per_share)
    profit_gbp = fx.to_gbp(shares * reward_per_share)
    exposure_gbp = fx.to_gbp(shares * entry_usd)

    stop_move_pct = risk_per_share / entry_usd * 100
    target_move_pct = reward_per_share / entry_usd * 100
    rr = reward_per_share / risk_per_share

    # --- sanity checks ------------------------------------------------------
    if shares * entry_usd < 1.0:
        warnings.append(
            "That's below the $1 minimum order size. You'd need a larger stake."
        )

    if rr < 1:
        warnings.append(
            f"You're risking £{loss_gbp:,.2f} to make £{profit_gbp:,.2f}. "
            f"Below 1:1 you need to win more often than you lose just to break even."
        )

    if stop_move_pct < 0.1:
        warnings.append(
            f"Your stop is only {stop_move_pct:.2f}% away. Ordinary noise will "
            f"take you out before the idea has a chance."
        )

    if stop_move_pct > 5:
        warnings.append(
            f"Your stop is {stop_move_pct:.1f}% away — that's a wide one. "
            f"Correct if the setup calls for it, but size accordingly."
        )

    return Outcome(
        stake_gbp=stake_gbp,
        entry_usd=entry_usd,
        stop_usd=stop_usd,
        target_usd=target_usd,
        shares=shares,
        exposure_gbp=exposure_gbp,
        loss_gbp=loss_gbp,
        profit_gbp=profit_gbp,
        breakeven_pct=stop_move_pct,
        stop_move_pct=stop_move_pct,
        target_move_pct=target_move_pct,
        rr=rr,
        warnings=warnings,
    )


def format_outcomes(o: Outcome, ticker: str = "") -> str:
    """The Discord message. Deliberately short — this gets read mid-session."""
    name = f" — {ticker.upper()}" if ticker else ""

    lines = [
        f"**£{o.stake_gbp:,.2f}{name}** buys **{o.shares:g} shares** "
        f"at ${o.entry_usd:,.2f}",
        "",
        f"🔴 Stop hit  ${o.stop_usd:,.2f}  →  **−£{o.loss_gbp:,.2f}**  "
        f"({o.stop_move_pct:.2f}% move)",
        f"🟢 Target hit ${o.target_usd:,.2f}  →  **+£{o.profit_gbp:,.2f}**  "
        f"({o.target_move_pct:.2f}% move)",
        "",
        f"Risk/reward **1 : {o.rr:.2f}**",
    ]

    for w in o.warnings:
        lines.append(f"\n⚠️ {w}")

    lines.append(
        "\n*Both outcomes, not a forecast. Nothing here says which one happens.*"
    )
    return "\n".join(lines)
