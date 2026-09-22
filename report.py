"""
P&L reporting — what actually happened, in GBP.

Every number here comes from closed positions with a real exit price
recorded by reconcile.py. Nothing is estimated and nothing is projected.
An open position contributes to "unrealised" and nowhere else, because an
unrealised gain is a price, not a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import fx
import tracker
from config import CONFIG


def _price(value: float, ticker: str = "") -> str:
    """
    Print a price the way the instrument is quoted.

    "$1.10 → $1.11" was the whole of a EUR/USD trade rounded away — the
    entire move lives below the second decimal.
    """
    try:
        import pairs
        return f"{value:.{pairs.parse(ticker).displayed_decimals}f}"
    except Exception:  # noqa: BLE001
        return f"${value:,.2f}"

try:
    from sessions import ET, UK
except Exception:  # pragma: no cover - sessions imports config at module load
    from zoneinfo import ZoneInfo
    ET = ZoneInfo(CONFIG.sessions.timezone)
    UK = ZoneInfo("Europe/London")


# ===========================================================================
# Windows
# ===========================================================================

def day_start_utc(now: datetime | None = None) -> str:
    """Midnight ET today, as an ISO UTC string — the start of the trading day."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def days_ago_utc(days: int, now: datetime | None = None) -> str:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    start = (now_et - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


EPOCH = "1970-01-01T00:00:00+00:00"

PERIODS = {
    "today": (day_start_utc, "Today"),
    "week": (lambda: days_ago_utc(7), "Last 7 days"),
    "month": (lambda: days_ago_utc(30), "Last 30 days"),
    "all": (lambda: EPOCH, "All time"),
}


# ===========================================================================
# Aggregation
# ===========================================================================

@dataclass
class Tally:
    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    realised_gbp: float = 0.0
    best_gbp: float = 0.0
    worst_gbp: float = 0.0
    gross_win_gbp: float = 0.0
    gross_loss_gbp: float = 0.0
    lines: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.wins + self.losses
        return (self.wins / decided * 100) if decided else None

    @property
    def avg_win_gbp(self) -> float:
        return self.gross_win_gbp / self.wins if self.wins else 0.0

    @property
    def avg_loss_gbp(self) -> float:
        return self.gross_loss_gbp / self.losses if self.losses else 0.0

    @property
    def expectancy_gbp(self) -> Optional[float]:
        """Average result per trade. The only number that compounds."""
        return self.realised_gbp / self.trades if self.trades else None


def tally(conn, since_iso: str, label: str = "") -> Tally:
    out = Tally(label=label)
    for pos in tracker.closed_since(conn, since_iso):
        if pos.exit_price is None:
            continue
        pnl = pos.realised_gbp()
        out.trades += 1
        out.realised_gbp += pnl

        if pnl > 0:
            out.wins += 1
            out.gross_win_gbp += pnl
        elif pnl < 0:
            out.losses += 1
            out.gross_loss_gbp += abs(pnl)
        else:
            out.scratches += 1

        out.best_gbp = max(out.best_gbp, pnl)
        out.worst_gbp = min(out.worst_gbp, pnl)

        reason = ""
        if "[stop]" in pos.note:
            reason = " · stopped out"
        elif "[target]" in pos.note:
            reason = " · target hit"
        elif "[eod]" in pos.note:
            reason = " · closed at the bell"
        elif "[never filled]" in pos.note:
            reason = " · never filled"

        mark = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        out.lines.append(
            f"{mark} **{pos.ticker}** {pos.qty:+,.0f} @ "
            f"`{_price(pos.entry_price, pos.ticker)}` → "
            f"`{_price(pos.exit_price, pos.ticker)}` · "
            f"**£{pnl:,.2f}**{reason}")
    return out


# ===========================================================================
# Formatting
# ===========================================================================

def format_tally(t: Tally, account_gbp: float | None = None) -> str:
    if t.trades == 0:
        return (f"**{t.label}** — no closed trades.\n\n"
                f"*No trades is a result, not a failure. The strategy "
                f"produces setups on a minority of days.*")

    account = account_gbp if account_gbp is not None \
        else CONFIG.display.account_gbp
    pct = (t.realised_gbp / account * 100) if account else 0.0
    sign = "+" if t.realised_gbp >= 0 else ""

    body = [
        f"**{sign}£{t.realised_gbp:,.2f}**  ({sign}{pct:.2f}% of "
        f"£{account:,.0f})",
        "",
        f"Trades: **{t.trades}** · {t.wins}W / {t.losses}L"
        + (f" / {t.scratches} flat" if t.scratches else ""),
    ]

    if t.win_rate is not None:
        body.append(f"Win rate: **{t.win_rate:.0f}%**")
    if t.wins:
        body.append(f"Average win: £{t.avg_win_gbp:,.2f}")
    if t.losses:
        body.append(f"Average loss: £{t.avg_loss_gbp:,.2f}")
    if t.expectancy_gbp is not None:
        body.append(f"Per trade: **£{t.expectancy_gbp:,.2f}**")

    body += ["", "**Trades**"] + t.lines

    # The honest caveats, attached to the numbers rather than buried.
    notes = []
    if t.trades < 20:
        notes.append(
            f"⚠️ {t.trades} trade{'s' if t.trades != 1 else ''} is far too "
            f"few to mean anything. A win rate needs 30+ trades before it "
            f"stops being noise.")
    if t.losses and t.avg_loss_gbp > t.avg_win_gbp and t.wins:
        notes.append(
            "⚠️ Your average loss is bigger than your average win. That "
            "needs a high win rate just to break even.")
    if notes:
        body += [""] + notes

    return "\n".join(body)


def format_eod(conn, prices: dict[str, float] | None = None,
               now: datetime | None = None) -> str:
    """The end-of-day summary posted automatically after the close."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    t = tally(conn, day_start_utc(now_et), now_et.strftime("%A %d %B"))

    parts = [format_tally(t)]

    still_open = tracker.open_positions(conn)
    if still_open:
        lines = []
        for p in still_open:
            price = (prices or {}).get(p.ticker)
            if price is None:
                lines.append(f"• **{p.ticker}** {p.qty:g} @ "
                             f"`{_price(p.entry_price, p.ticker)}` "
                             f"— no price")
            else:
                lines.append(f"• **{p.ticker}** {p.qty:g} @ "
                             f"`{_price(p.entry_price, p.ticker)}` · "
                             f"£{fx.pnl_to_gbp(p.unrealised(price), p.ticker):,.2f} unrealised")
        parts += ["", "⚠️ **Still open overnight**"] + lines + [
            "", "*A stop cannot protect these while the market is shut — "
            "price can gap straight past it overnight.*"]

    # Running totals, so a good day isn't read in isolation.
    all_time = tally(conn, EPOCH, "All time")
    if all_time.trades > t.trades:
        sign = "+" if all_time.realised_gbp >= 0 else ""
        parts += ["", f"*All time: {sign}£{all_time.realised_gbp:,.2f} over "
                      f"{all_time.trades} trades.*"]

    parts.append(f"\n*{fx.rate_note()}*")
    return "\n".join(parts)
