"""
Storage for what the AI said, and what it cost.

Separate from ai.py on purpose: ai.py does network and no database, this
does database and no network. That split is what lets every test in
test_ai.py run without touching either.

The table this writes is the only way the veto feature can ever be
evaluated. A verdict that blocks a trade and leaves no record of what it
blocked is an unfalsifiable claim to have helped.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ===========================================================================
# Writing
# ===========================================================================

def record_verdict(conn: sqlite3.Connection, ticker: str, direction: str,
                   result, would_have: str = "",
                   position_id: Optional[int] = None) -> int:
    """
    Store one VetoResult. Returns the row id.

    `would_have` describes the trade that was on the table when the model
    was asked — size, entry, stop, target. Without it, a blocked trade is
    a decision nobody can grade later.
    """
    v = result.verdict
    cur = conn.execute(
        """INSERT INTO ai_verdicts
           (ts, ticker, direction, action, confidence, rationale, blocked,
            ok, failure, provider, model, latency_ms, position_id,
            would_have)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), ticker.upper(), direction, v.action, float(v.confidence),
         v.rationale[:500], 1 if result.blocked else 0, 1 if v.ok else 0,
         v.failure[:300], v.provider, v.model, int(v.latency_ms),
         position_id, would_have[:500]))
    conn.commit()
    return cur.lastrowid


def record_postmortem(conn: sqlite3.Connection, position_id: int,
                      ticker: str, takeaway, pnl_gbp: float,
                      exit_reason: str = "") -> None:
    model = ""
    if takeaway.verdict is not None:
        model = takeaway.verdict.model
    conn.execute(
        """INSERT OR REPLACE INTO ai_postmortems
           (position_id, ts, ticker, takeaway, has_lesson, pnl_gbp,
            exit_reason, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (position_id, _now(), ticker.upper(), takeaway.text[:500],
         1 if takeaway.has_lesson else 0, float(pnl_gbp),
         exit_reason[:100], model))
    conn.commit()


def attach_position(conn: sqlite3.Connection, verdict_id: int,
                    position_id: int) -> None:
    """Link a verdict to the trade it let through, once that trade exists."""
    conn.execute("UPDATE ai_verdicts SET position_id = ? WHERE id = ?",
                 (position_id, verdict_id))
    conn.commit()


# ===========================================================================
# Reading
# ===========================================================================

def recent_verdicts(conn: sqlite3.Connection, limit: int = 10) -> list:
    return list(conn.execute(
        "SELECT * FROM ai_verdicts ORDER BY id DESC LIMIT ?", (limit,)))


def similar_losses(conn: sqlite3.Connection, direction: str,
                   limit: int = 5, min_closed: int = 20) -> list:
    """
    Recent losing trades in the same direction, WITH their takeaways.

    Returns an empty list until `min_closed` trades have actually closed.
    That floor is the whole safeguard: a model shown three losses and
    asked what they have in common will always find something, and at
    that sample size the something is noise. Callers pass the empty list
    straight through to the prompt, which then states plainly that no
    history exists rather than leaving the model to guess.
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE closed_at IS NOT NULL "
        "AND exit_price IS NOT NULL").fetchone()[0]
    if total < min_closed:
        return []

    rows = conn.execute(
        """SELECT p.*, m.takeaway AS takeaway
           FROM positions p
           LEFT JOIN ai_postmortems m ON m.position_id = p.id
           WHERE p.closed_at IS NOT NULL AND p.exit_price IS NOT NULL
           ORDER BY p.id DESC LIMIT 200""").fetchall()

    import tracker
    out = []
    for row in rows:
        pos = tracker._row_to_position(row)
        if pos.direction != direction:
            continue
        pnl = pos.realised_gbp()
        if pnl >= 0:
            continue

        conditions = "unrecorded"
        if pos.context_json:
            try:
                ctx = json.loads(pos.context_json)
                conditions = ", ".join(ctx.get("conditions_met", [])) \
                    or "unrecorded"
            except (ValueError, TypeError):
                pass

        out.append({
            "ticker": pos.ticker,
            "direction": pos.direction,
            "pnl_gbp": pnl,
            "conditions": conditions[:300],
            "takeaway": row["takeaway"] if "takeaway" in row.keys() else None,
        })
        if len(out) >= limit:
            break
    return out


def stats(conn: sqlite3.Connection) -> dict:
    """
    How the AI has actually behaved. Facts, not a verdict on the feature.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(blocked) AS blocked,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
                  AVG(latency_ms) AS avg_latency
           FROM ai_verdicts""").fetchone()

    lessons = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(has_lesson) AS with_lesson
           FROM ai_postmortems""").fetchone()

    total = row["total"] or 0
    return {
        "verdicts": total,
        "blocked": row["blocked"] or 0,
        "failed": row["failed"] or 0,
        "block_rate": ((row["blocked"] or 0) / total * 100) if total else 0.0,
        "fail_rate": ((row["failed"] or 0) / total * 100) if total else 0.0,
        "avg_latency_ms": int(row["avg_latency"] or 0),
        "postmortems": lessons["total"] or 0,
        "with_lesson": lessons["with_lesson"] or 0,
    }
