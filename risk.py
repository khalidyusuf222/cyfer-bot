"""
Position sizing and risk limits, in GBP.

The book gives a risk-per-trade range and the reasoning behind it. It gives no
daily loss cap, no trade limit and no consecutive-loss rule — those are
imposed here, because their absence is what actually ends accounts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import fx
from config import CONFIG

DB_PATH = Path(__file__).parent / "positions.db"


@dataclass(frozen=True)
class SizedTrade:
    """
    One sized position.

    Units, not shares. A unit is one of the base currency, and unlike a
    share it divides all the way down to 1 — which is the entire reason
    the bot moved to forex. The old version of this class counted shares
    and could not express a GBP 10 risk on a USD 760 instrument without
    either rounding to nothing or breaching the risk limit.
    """
    pair: str
    units: int
    direction: str            # long | short
    entry: float
    stop: float
    target: float
    stop_pips: float
    risk_gbp: float
    reward_gbp: float
    exposure_gbp: float
    leverage: float
    r_multiple: float
    halved: bool
    halve_reason: str
    warnings: list[str]
    capped: bool = False       # cut down to fit the leverage limit / free margin

    @property
    def tradeable(self) -> bool:
        return self.units > 0

    # --- names the rest of the codebase still uses ------------------------
    @property
    def shares(self) -> float:
        return float(self.units)

    @property
    def entry_usd(self) -> float:
        return self.entry

    @property
    def stop_usd(self) -> float:
        return self.stop

    @property
    def target_usd(self) -> float:
        return self.target


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reason: str
    trades_today: int
    pnl_today_gbp: float
    consecutive_losses: int


# ===========================================================================
# Daily state
# ===========================================================================

def _today() -> str:
    return date.today().isoformat()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_state (
        day               TEXT PRIMARY KEY,
        trades_taken      INTEGER NOT NULL DEFAULT 0,
        realised_gbp      REAL    NOT NULL DEFAULT 0,
        consecutive_losses INTEGER NOT NULL DEFAULT 0,
        locked_out        INTEGER NOT NULL DEFAULT 0,
        lock_reason       TEXT    NOT NULL DEFAULT ''
    );
    """)
    conn.commit()


def day_state(conn: sqlite3.Connection) -> sqlite3.Row:
    _ensure(conn)
    row = conn.execute("SELECT * FROM daily_state WHERE day = ?", (_today(),)).fetchone()
    if row is None:
        conn.execute("INSERT INTO daily_state (day) VALUES (?)", (_today(),))
        conn.commit()
        row = conn.execute("SELECT * FROM daily_state WHERE day = ?", (_today(),)).fetchone()
    return row


def record_trade_opened(conn: sqlite3.Connection) -> None:
    day_state(conn)
    conn.execute("UPDATE daily_state SET trades_taken = trades_taken + 1 WHERE day = ?",
                 (_today(),))
    conn.commit()


def record_trade_cancelled(conn: sqlite3.Connection) -> None:
    """
    Give back a trade slot when an entry order never filled.

    record_trade_opened() runs at submission, before we know whether the
    limit gets hit. An order that expires unfilled cost nothing and risked
    nothing, so it shouldn't burn one of the day's two trades.
    """
    row = day_state(conn)
    if row["trades_taken"] <= 0:
        return
    conn.execute(
        "UPDATE daily_state SET trades_taken = trades_taken - 1 WHERE day = ?",
        (_today(),),
    )
    conn.commit()


def record_trade_closed(conn: sqlite3.Connection, pnl_gbp: float) -> None:
    row = day_state(conn)
    consec = 0 if pnl_gbp >= 0 else row["consecutive_losses"] + 1
    conn.execute(
        "UPDATE daily_state SET realised_gbp = realised_gbp + ?, "
        "consecutive_losses = ? WHERE day = ?",
        (pnl_gbp, consec, _today()),
    )
    conn.commit()

    r = CONFIG.risk
    account = CONFIG.display.account_gbp
    row = day_state(conn)

    if consec >= r.max_consecutive_losses:
        _lock(conn, f"{consec} losses in a row. Stopped for the day.")
    elif row["realised_gbp"] <= -(account * r.max_daily_loss_pct / 100):
        _lock(conn, f"Daily loss limit hit ({r.max_daily_loss_pct}% of account).")


def _lock(conn: sqlite3.Connection, reason: str) -> None:
    conn.execute("UPDATE daily_state SET locked_out = 1, lock_reason = ? WHERE day = ?",
                 (reason, _today()))
    conn.commit()


def reset_day(conn: sqlite3.Connection) -> None:
    """Manual override. Deliberately requires a typed confirmation in the bot."""
    conn.execute("DELETE FROM daily_state WHERE day = ?", (_today(),))
    conn.commit()


# ===========================================================================
# The gate
# ===========================================================================

def check(conn: sqlite3.Connection) -> RiskVerdict:
    """Can a new trade be opened right now, on risk grounds alone?"""
    r = CONFIG.risk
    row = day_state(conn)
    account = CONFIG.display.account_gbp

    trades = row["trades_taken"]
    pnl = row["realised_gbp"]
    consec = row["consecutive_losses"]

    if row["locked_out"]:
        return RiskVerdict(False, f"🔒 {row['lock_reason']}", trades, pnl, consec)

    if trades >= r.max_trades_per_day:
        return RiskVerdict(False,
                           f"🔒 {trades}/{r.max_trades_per_day} trades taken today.",
                           trades, pnl, consec)

    limit = -(account * r.max_daily_loss_pct / 100)
    if pnl <= limit:
        return RiskVerdict(False,
                           f"🔒 Down £{abs(pnl):,.2f} today — daily limit is "
                           f"£{abs(limit):,.2f}.",
                           trades, pnl, consec)

    if consec >= r.max_consecutive_losses:
        return RiskVerdict(False, f"🔒 {consec} consecutive losses.",
                           trades, pnl, consec)

    return RiskVerdict(True,
                       f"{trades}/{r.max_trades_per_day} trades used · "
                       f"P&L today £{pnl:,.2f}",
                       trades, pnl, consec)


# ===========================================================================
# Sizing
# ===========================================================================

def max_leverage(pair) -> float:
    """
    The most leverage a UK retail account may use on this pair.

    [OANDA UK retail] 30:1 when both currencies are USD, EUR, JPY, GBP,
    CAD or CHF; 20:1 otherwise - which puts AUD/USD at 20:1.
    """
    r = CONFIG.risk
    majors = r.leverage_major_ccys
    if pair.base in majors and pair.quote in majors:
        return r.max_leverage_major
    return r.max_leverage_other


def size_trade(entry: float,
               stop: float,
               symbol: str | None = None,
               account_gbp: float | None = None,
               median_stop_pct: float | None = None,
               news_day: bool = False,
               target: float | None = None,
               margin_available_gbp: float | None = None) -> SizedTrade:
    """
    Size a position so that being stopped out costs exactly the configured
    percentage of the account, in GBP.

    The arithmetic lives in pairs.size_position; this wraps it with the
    risk rules the book does not give — halving on an oversized stop or a
    news day — and converts everything into pounds for display.

    Direction is read from the levels, not passed in: a stop below entry
    is a long, a stop above it is a short. The previous version of this
    function raised an error on stop > entry, which silently made every
    short setup unsizeable.
    """
    import pairs

    r, s = CONFIG.risk, CONFIG.cyfer
    account = account_gbp if account_gbp is not None else CONFIG.display.account_gbp
    symbol = symbol or CONFIG.instruments.primary
    pair = pairs.parse(symbol)
    warnings: list[str] = []

    if entry <= 0:
        raise ValueError("Entry price must be positive.")
    if stop == entry:
        raise ValueError("Stop cannot equal entry — that is a zero-risk trade.")

    direction = "long" if stop < entry else "short"
    stop_distance = abs(entry - stop)
    stop_pct = stop_distance / entry * 100

    # --- the two reasons to take half size --------------------------------
    halved, halve_reason = False, ""
    if median_stop_pct and stop_pct > median_stop_pct * r.oversized_stop_multiple:
        halved = True
        halve_reason = (f"stop is {stop_pct:.3f}% vs usual {median_stop_pct:.3f}% "
                        f"— size halved")
    elif news_day and r.halve_on_news:
        halved = True
        halve_reason = "high-impact news day — size halved  [BOOK p62-65]"

    risk_budget_gbp = account * r.risk_per_trade_pct / 100
    effective_risk_gbp = risk_budget_gbp / 2 if halved else risk_budget_gbp

    # --- the target  [BOOK p48] -------------------------------------------
    if target is None:
        target = (entry + stop_distance * s.min_risk_reward if direction == "long"
                  else entry - stop_distance * s.min_risk_reward)

    # --- the conversion ---------------------------------------------------
    quote_rate, rate_is_live = fx.rate_to_gbp(pair.quote)
    if not rate_is_live:
        warnings.append(
            f"The {pair.quote}/GBP rate is a hardcoded fallback — the FX feed "
            f"is unreachable. Position size is only as accurate as that rate.")

    sized = pairs.size_position(
        pair=pair,
        entry=entry,
        stop=stop,
        risk_account_ccy=effective_risk_gbp,
        quote_to_account_rate=quote_rate,
        target=target,
    )
    warnings.extend(sized.notes)

    units = sized.units
    per_unit_gbp = entry * quote_rate

    # --- leverage ---------------------------------------------------------
    # Forex positions are always larger than the account - that is what
    # leverage is, and it is not a fault. But the broker caps HOW much
    # larger: margin (a deposit the broker holds while the trade is open)
    # must cover exposure / limit, and it has to come out of free margin,
    # which another open trade may already be using.
    #
    # When the risk budget asks for more than that, the position is cut to
    # the largest size that fits instead of being skipped. The order would
    # be rejected by the broker at full size anyway; cutting it means the
    # trade still happens, at less risk than asked for, and says so.
    limit = max_leverage(pair)
    usable = limit * r.leverage_headroom_pct / 100
    free = account if margin_available_gbp is None else max(0.0, margin_available_gbp)
    max_exposure = free * usable
    capped = False

    if units > 0 and per_unit_gbp > 0 and units * per_unit_gbp > max_exposure:
        full_units = units
        full_lev = full_units * per_unit_gbp / account if account else 0.0
        units = int(max_exposure // per_unit_gbp)
        capped = True
        if units <= 0:
            warnings.append(
                f"No free margin left for {pair.display} - another open "
                f"trade is using it. UK accounts are capped at "
                f"{limit:.0f}x leverage on this pair. Nothing to trade.")
        else:
            cut_risk = units * stop_distance * quote_rate
            why = (f"{pair.display} is capped at {limit:.0f}x for UK accounts"
                   + (f" and there's £{free:,.0f} of free margin"
                      if margin_available_gbp is not None else ""))
            warnings.append(
                f"Cut from {full_units:,} to {units:,} units. Risking the full "
                f"£{effective_risk_gbp:,.2f} would need {full_lev:.0f}x "
                f"leverage; {why}. Risking "
                f"£{cut_risk:,.2f} ({cut_risk / account * 100:.1f}%) instead.")

    exposure_gbp = units * per_unit_gbp
    leverage = exposure_gbp / account if account else 0.0

    if leverage > 10 and not capped:
        warnings.append(
            f"Position is {leverage:.0f}x the account. Within broker limits, "
            f"but £{exposure_gbp:,.0f} of currency is being moved by "
            f"£{account:,.0f} of yours. The stop is what keeps that "
            f"survivable - nothing else does.")

    if units <= 0 and not capped:
        warnings.append("Sizing resolved to zero units. Nothing to trade.")

    actual_risk_gbp = units * stop_distance * quote_rate
    reward_gbp = units * abs(target - entry) * quote_rate

    if sized.stop_pips < 5:
        warnings.append(
            f"Stop is {sized.stop_pips:.1f} pips away. [BOOK p8] The spread "
            f"is a cost you pay on every trade, and on a stop this tight the "
            f"spread alone can take you out.")

    return SizedTrade(
        pair=pair.symbol,
        units=units,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        stop_pips=sized.stop_pips,
        risk_gbp=actual_risk_gbp,
        reward_gbp=reward_gbp,
        exposure_gbp=exposure_gbp,
        leverage=leverage,
        r_multiple=s.min_risk_reward,
        halved=halved,
        halve_reason=halve_reason,
        warnings=warnings,
        capped=capped,
    )


def _lot_note(units: int) -> str:
    """
    [BOOK p14] 100,000 units is a standard lot, 10,000 a mini, 1,000 a
    micro. Shown because that is the language every forex source uses;
    orders are placed in units regardless.
    """
    if units >= 100_000:
        return f"{units / 100_000:.2f} standard lots"
    if units >= 10_000:
        return f"{units / 10_000:.2f} mini lots"
    if units >= 1_000:
        return f"{units / 1_000:.2f} micro lots"
    return f"under a micro lot"


def format_sizing(t: SizedTrade) -> str:
    import pairs
    pair = pairs.parse(t.pair)
    d = pair.displayed_decimals
    arrow = "🟩 LONG" if t.direction == "long" else "🟥 SHORT"

    lot_note = f"  ({_lot_note(t.units)})" if t.units else ""

    lines = [
        f"{arrow}  **{t.units:,} units** of {pair.display}{lot_note}",
        f"Entry `{t.entry:.{d}f}` · Stop `{t.stop:.{d}f}` "
        f"({t.stop_pips:.1f} pips) · Target `{t.target:.{d}f}`",
        "",
        f"Risk: **£{t.risk_gbp:,.2f}**",
        f"Reward at {t.r_multiple:g}R: **£{t.reward_gbp:,.2f}**",
        f"Controlling: £{t.exposure_gbp:,.0f}  ({t.leverage:.1f}x account)",
    ]
    if t.halved:
        lines.append(f"\n⚠️ {t.halve_reason}")
    for w in t.warnings:
        lines.append(f"\n⚠️ {w}")
    lines.append(f"\n*{fx.rate_note(pair.quote)}*")
    return "\n".join(lines)
