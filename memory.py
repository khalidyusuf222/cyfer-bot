"""
Trade memory — the honest version.

The idea comes from Miles Deutscher's bot video: a bot should remember its
trades and learn from them. He's right, and the missing piece was real:
until now the database recorded WHAT you traded but not WHY, so there was
no way to ever find out which conditions were doing the work.

This module fixes that. Every trade is stored with the conditions that were
present when it fired, so after enough trades you can ask real questions:
did setups with SMT divergence beat those without? Did 6-of-6 beat 5-of-6?

Where this deliberately differs from the video:

  The video suggests the bot write plain-English "lessons" after each trade.
  This one computes statistics instead, and refuses to report any bucket
  with too few trades in it.

  The reason is that a lesson written after three trades is pattern-matching
  on noise. "I do better on Tuesdays" from a dozen trades is not a finding,
  it's a coin landing heads twice. Confident narration over a small sample
  is worse than no memory at all, because you act on it.

So: it counts, it reports, and it tells you plainly when there isn't enough
data yet to say anything.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Below this many closed trades, report nothing but the count.
MIN_TRADES_OVERALL = 20

# Below this many trades in a single bucket, don't report that bucket.
MIN_TRADES_PER_BUCKET = 5


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_context (
    position_id    INTEGER PRIMARY KEY,
    strategy       TEXT    NOT NULL,
    direction      TEXT    NOT NULL DEFAULT '',
    score          INTEGER NOT NULL DEFAULT 0,
    total          INTEGER NOT NULL DEFAULT 0,
    conditions_met TEXT    NOT NULL DEFAULT '[]',
    conditions_missing TEXT NOT NULL DEFAULT '[]',
    session_phase  TEXT    NOT NULL DEFAULT '',
    weekday        TEXT    NOT NULL DEFAULT '',
    recorded_at    TEXT    NOT NULL
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ===========================================================================
# Recording
# ===========================================================================

def record_context(conn: sqlite3.Connection,
                   position_id: int,
                   strategy: str,
                   direction: str = "",
                   score: int = 0,
                   total: int = 0,
                   conditions_met: Optional[list[str]] = None,
                   conditions_missing: Optional[list[str]] = None,
                   session_phase: str = "") -> None:
    """Attach the setup context to a position when it's opened."""
    ensure(conn)
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT OR REPLACE INTO trade_context
           (position_id, strategy, direction, score, total, conditions_met,
            conditions_missing, session_phase, weekday, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (position_id, strategy, direction, score, total,
         json.dumps(_labels(conditions_met or [])),
         json.dumps(_labels(conditions_missing or [])),
         session_phase, now.strftime("%A"), now.isoformat(timespec="seconds")),
    )
    conn.commit()


def _labels(conditions: list[str]) -> list[str]:
    """
    Reduce a full condition sentence to a short stable label, so the same
    condition groups together across trades regardless of the prices in it.
    """
    out = []
    for c in conditions:
        low = c.lower()
        if "bias" in low:
            out.append("4h_bias")
        elif "liquidity draw" in low:
            out.append("liquidity_draw")
        elif "swept" in low or "sweep" in low:
            out.append("liquidity_sweep")
        elif "discount" in low or "premium" in low or "equilibrium" in low:
            out.append("equilibrium")
        elif "break of structure" in low or "bos" in low:
            out.append("break_of_structure")
        elif "inverse fvg" in low or "inverse fair value" in low:
            out.append("inverse_fvg")
        elif "smt" in low:
            out.append("smt_divergence")
        elif "ema" in low:
            out.append("ema_stack")
        elif "upper 30" in low or "lower 30" in low:
            out.append("body_30pct")
        else:
            out.append(c[:40].strip().lower().replace(" ", "_"))
    return sorted(set(out))


def get_context(conn: sqlite3.Connection, position_id: int) -> Optional[dict]:
    ensure(conn)
    row = conn.execute(
        "SELECT * FROM trade_context WHERE position_id = ?", (position_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["conditions_met"] = json.loads(d["conditions_met"])
    d["conditions_missing"] = json.loads(d["conditions_missing"])
    return d


# ===========================================================================
# Analysis
# ===========================================================================

@dataclass
class Bucket:
    label: str
    wins: int
    losses: int
    total_pnl: float

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.n * 100 if self.n else None

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.n if self.n else 0.0

    @property
    def reportable(self) -> bool:
        return self.n >= MIN_TRADES_PER_BUCKET


def analyse(conn: sqlite3.Connection, closed_positions: list) -> dict:
    """
    Break closed trades down by the conditions that were present.

    `closed_positions` are tracker.Position objects that have been closed.
    Returns buckets plus an honest verdict on whether there's enough data.
    """
    ensure(conn)

    by_condition: dict[str, Bucket] = {}
    by_score: dict[str, Bucket] = {}
    by_strategy: dict[str, Bucket] = {}
    unlabelled = 0

    for pos in closed_positions:
        pnl = pos.realised()
        won = pnl > 0

        ctx = get_context(conn, pos.id)
        if ctx is None:
            unlabelled += 1
            continue

        def add(store: dict, key: str) -> None:
            b = store.setdefault(key, Bucket(key, 0, 0, 0.0))
            if won:
                b.wins += 1
            else:
                b.losses += 1
            b.total_pnl += pnl

        for cond in ctx["conditions_met"]:
            add(by_condition, cond)

        if ctx["total"]:
            add(by_score, f"{ctx['score']}/{ctx['total']} conditions")

        add(by_strategy, ctx["strategy"])

    total_closed = len(closed_positions)

    return {
        "total_closed": total_closed,
        "labelled": total_closed - unlabelled,
        "unlabelled": unlabelled,
        "enough_data": total_closed >= MIN_TRADES_OVERALL,
        "needed": max(0, MIN_TRADES_OVERALL - total_closed),
        "by_condition": by_condition,
        "by_score": by_score,
        "by_strategy": by_strategy,
    }


def format_analysis(result: dict, currency: str = "£") -> str:
    """The !learn message. Says nothing it can't support."""
    n = result["total_closed"]

    if not result["enough_data"]:
        return (
            f"**{n} closed trade{'s' if n != 1 else ''} so far.**\n\n"
            f"I need **{result['needed']} more** before breaking results down "
            f"by condition.\n\n"
            f"Not being awkward about it — with a handful of trades any "
            f"pattern I found would be noise. Three wins in a row tells you "
            f"nothing about what caused them, and a 'lesson' drawn from that "
            f"is worse than no lesson, because you'd act on it.\n\n"
            f"Keep logging. `!st` shows your running totals in the meantime."
        )

    lines = [f"**{n} closed trades.** What the data actually says:", ""]

    def render(store: dict, heading: str) -> None:
        reportable = {k: b for k, b in store.items() if b.reportable}
        if not reportable:
            return
        lines.append(f"**{heading}**")
        for key, b in sorted(reportable.items(),
                             key=lambda kv: kv[1].avg_pnl, reverse=True):
            name = key.replace("_", " ")
            lines.append(
                f"`{name}` — {b.wins}W/{b.losses}L "
                f"({b.win_rate:.0f}%), avg {currency}{b.avg_pnl:+,.2f}"
            )
        lines.append("")

    render(result["by_strategy"], "By strategy")
    render(result["by_score"], "By how many conditions aligned")
    render(result["by_condition"], "By individual condition")

    skipped = [k for k, b in result["by_condition"].items() if not b.reportable]
    if skipped:
        lines.append(
            f"*Not shown — fewer than {MIN_TRADES_PER_BUCKET} trades each: "
            f"{', '.join(sorted(k.replace('_', ' ') for k in skipped))}.*"
        )

    if result["unlabelled"]:
        lines.append(
            f"\n*{result['unlabelled']} trades were logged manually with no "
            f"setup attached, so they're counted in the total but not broken down.*"
        )

    lines.append(
        "\n*These are counts, not conclusions. Even at 20 trades the error "
        "bars are wide — treat a difference as a hint worth testing, not a "
        "finding.*"
    )

    return "\n".join(lines)
