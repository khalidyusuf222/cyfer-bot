"""
Position tracker + rules engine.

This module does NOT predict anything. It stores the positions you tell it
about, and it tells you when a level YOU set has been reached.
"""

from __future__ import annotations

import json as _json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "positions.db"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Position:
    """
    One position.

    qty IS SIGNED. Positive is long, negative is short — the same
    convention OANDA uses for units, so nothing has to be translated at
    the boundary.

    This matters more than it looks. Every profit calculation below is
    (exit - entry) * qty, and with a signed qty that one expression is
    correct in both directions with no branch. Before the move to forex
    this class was long-only, which would have reported the profit on
    every short with the sign reversed.
    """
    id: int
    ticker: str
    qty: float
    entry_price: float
    stop_price: Optional[float]
    target_price: Optional[float]
    trail_pct: Optional[float]
    peak_price: float
    opened_at: str
    closed_at: Optional[str]
    exit_price: Optional[float]
    note: str

    # Set when the bot placed this order itself through Alpaca. Without it
    # there is no way to ask the broker what became of the trade, which is
    # how auto-executed trades used to close at Alpaca without the bot ever
    # finding out.
    broker_order_id: Optional[str] = None
    broker_mode: Optional[str] = None        # 'paper' | 'live'
    requested_entry: Optional[float] = None  # what we asked for, pre-fill
    context_json: Optional[str] = None       # indicator values at entry
    realised_gbp_broker: Optional[float] = None   # the broker's own figure

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def direction(self) -> str:
        return "short" if self.qty < 0 else "long"

    @property
    def entry_slippage(self) -> Optional[float]:
        """How much worse than asked the entry filled, per unit.

        Positive always means worse. On a long that is paying more than
        we asked; on a short it is selling for less. This is the cost the
        ledger used to hide.
        """
        if self.requested_entry is None:
            return None
        raw = self.entry_price - self.requested_entry
        return -raw if self.is_short else raw

    @property
    def exit_slippage(self) -> Optional[float]:
        """How far past the intended exit level we actually filled.

        Positive means worse than intended in both directions: through the
        stop on a loss, short of the target on a win.
        """
        if self.exit_price is None:
            return None
        if self.is_short:
            if self.stop_price is not None and self.exit_price >= self.stop_price:
                return self.exit_price - self.stop_price
            if self.target_price is not None and self.exit_price <= self.target_price:
                return self.target_price - self.exit_price
            return None
        if self.stop_price is not None and self.exit_price <= self.stop_price:
            return self.stop_price - self.exit_price
        if self.target_price is not None and self.exit_price >= self.target_price:
            return self.exit_price - self.target_price
        return None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.closed_at is None:
            return None
        from datetime import datetime as _dt
        try:
            a = _dt.fromisoformat(self.opened_at)
            b = _dt.fromisoformat(self.closed_at)
        except ValueError:
            return None
        return (b - a).total_seconds()

    @property
    def return_pct(self) -> Optional[float]:
        """Trade return as a percentage, signed by direction."""
        if self.exit_price is None or self.entry_price == 0:
            return None
        raw = (self.exit_price - self.entry_price) / self.entry_price * 100
        return -raw if self.is_short else raw

    @property
    def is_broker_managed(self) -> bool:
        return bool(self.broker_order_id)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def cost_basis(self) -> float:
        """Notional size of the position. Unsigned — it is a magnitude."""
        return abs(self.qty) * self.entry_price

    def unrealised(self, price: float) -> float:
        return (price - self.entry_price) * self.qty

    def unrealised_pct(self, price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price * 100

    def realised(self) -> float:
        """Derived from the prices, in the QUOTE currency. May flatter."""
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    def realised_gbp(self) -> float:
        """
        What this trade made or lost, in pounds.

        The broker's own figure wins when there is one: it includes the
        spread, which the derived number silently omits. Everything that
        reports a result should use this, so the ledger, the daily summary
        and the weekly review can never disagree about the same trade.
        """
        if self.realised_gbp_broker is not None:
            return self.realised_gbp_broker
        import fx
        return fx.pnl_to_gbp(self.realised(), self.ticker)

    @property
    def pnl_is_from_broker(self) -> bool:
        return self.realised_gbp_broker is not None


@dataclass
class Alert:
    """Something factual that happened. Never a prediction."""
    ticker: str
    kind: str          # stop_hit | target_hit | trail_hit | risk_warning
    message: str
    price: float
    severity: str      # info | warn | urgent


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    entry_price   REAL    NOT NULL,
    stop_price    REAL,
    target_price  REAL,
    trail_pct     REAL,
    peak_price    REAL    NOT NULL,
    opened_at     TEXT    NOT NULL,
    closed_at     TEXT,
    exit_price    REAL,
    note          TEXT    NOT NULL DEFAULT '',
    broker_order_id TEXT,
    broker_mode     TEXT,
    requested_entry REAL,
    context_json    TEXT
);

CREATE TABLE IF NOT EXISTS fired_alerts (
    position_id INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    fired_at    TEXT    NOT NULL,
    PRIMARY KEY (position_id, kind)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every answer the AI reviewer gave, whether it blocked anything or not.
--
-- `blocked` and `would_have` together are the point of this table. Logging
-- only the trades it stopped would make the feature unfalsifiable: in a
-- month the question is whether the vetoes saved money or cost it, and
-- that cannot be answered without a record of what each one prevented.
CREATE TABLE IF NOT EXISTS ai_verdicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0,
    rationale   TEXT    NOT NULL DEFAULT '',
    blocked     INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    failure     TEXT    NOT NULL DEFAULT '',
    provider    TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    position_id INTEGER,
    would_have  TEXT    NOT NULL DEFAULT ''
);

-- One takeaway per closed trade. has_lesson = 0 means the model was given
-- the chance to say "nothing to learn here" and took it, which is the
-- correct answer for most single trades and is recorded as such rather
-- than being discarded.
CREATE TABLE IF NOT EXISTS ai_postmortems (
    position_id INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    takeaway    TEXT    NOT NULL DEFAULT '',
    has_lesson  INTEGER NOT NULL DEFAULT 0,
    pnl_gbp     REAL    NOT NULL DEFAULT 0,
    exit_reason TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False because the bot hands database work to worker
    # threads via asyncio.to_thread — the scan loop, reconciliation, and every
    # command that would otherwise block the event loop. Python's default
    # guard refuses that outright, which showed up as commands silently
    # returning nothing at all.
    #
    # Safe here: CPython's sqlite3 is built in serialized threading mode, so
    # the C layer locks the connection itself. The guard is conservative,
    # not load-bearing.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Wait rather than erroring if another thread holds a write lock.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does
# nothing to a table that already exists, so an existing positions.db needs
# these adding explicitly or every query blows up on the live VPS.
_ADDED_COLUMNS = {
    "broker_order_id": "TEXT",
    "broker_mode": "TEXT",
    # The price we ASKED for. reconcile() overwrites entry_price with the
    # price we actually got, so without this the difference between the two
    # — slippage — was computed and then thrown away. The review agent
    # needs it: slippage is one of the friction points it diagnoses.
    "requested_entry": "REAL",
    # Indicator values at the moment of entry, as JSON. The reviewer looks
    # for recurring trigger values in losing trades; it can't if we only
    # record that a condition was "met".
    "context_json": "TEXT",
    # What the BROKER says the trade actually made or lost, in GBP.
    #
    # Deriving it from (exit - entry) * qty ignores the spread and any
    # financing, so the derived figure always flatters the trade. Worse,
    # the risk ledger was already being fed the broker's real number while
    # the daily report showed the derived one — two different answers for
    # the same trade, which is how a ledger stops being believed.
    "realised_gbp_broker": "REAL",
}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an older positions.db up to the current schema. Idempotent."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    added = []
    for name, sql_type in _ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {sql_type}")
            added.append(name)
    if added:
        conn.commit()
    return added


_POSITION_FIELDS = {
    "realised_gbp_broker",
    "id", "ticker", "qty", "entry_price", "stop_price", "target_price",
    "trail_pct", "peak_price", "opened_at", "closed_at", "exit_price",
    "note", "broker_order_id", "broker_mode", "requested_entry",
    "context_json",
}


def _row_to_position(row: sqlite3.Row) -> Position:
    # Filtered rather than splatted whole, so a column added by a future
    # version doesn't crash an older build mid-session.
    data = {k: v for k, v in dict(row).items() if k in _POSITION_FIELDS}
    return Position(**data)


# --------------------------------------------------------------------------
# Position operations
# --------------------------------------------------------------------------

def open_position(
    conn: sqlite3.Connection,
    ticker: str,
    qty: float,
    entry_price: float,
    stop_price: Optional[float] = None,
    target_price: Optional[float] = None,
    trail_pct: Optional[float] = None,
    note: str = "",
    broker_order_id: Optional[str] = None,
    broker_mode: Optional[str] = None,
    context: Optional[dict] = None,
) -> Position:
    ticker = ticker.upper().strip()
    if qty == 0:
        raise ValueError("Quantity cannot be zero.")
    if entry_price <= 0:
        raise ValueError("Entry price must be greater than zero.")

    # A negative qty is a SHORT, and the levels flip with it. This used to
    # reject any qty below zero outright, which made every short setup the
    # strategy produced impossible to record.
    short = qty < 0

    if short:
        if stop_price is not None and stop_price <= entry_price:
            raise ValueError(
                f"Stop ({stop_price}) must be ABOVE your entry "
                f"({entry_price}) on a short. A stop below it would trigger "
                f"instantly."
            )
        if target_price is not None and target_price >= entry_price:
            raise ValueError(
                f"Target ({target_price}) must be BELOW your entry "
                f"({entry_price}) on a short."
            )
    else:
        if stop_price is not None and stop_price >= entry_price:
            raise ValueError(
                f"Stop ({stop_price}) must be BELOW your entry "
                f"({entry_price}). A stop above entry would trigger instantly."
            )
        if target_price is not None and target_price <= entry_price:
            raise ValueError(
                f"Target ({target_price}) must be ABOVE your entry "
                f"({entry_price})."
            )

    cur = conn.execute(
        """INSERT INTO positions
           (ticker, qty, entry_price, stop_price, target_price,
            trail_pct, peak_price, opened_at, note,
            broker_order_id, broker_mode, requested_entry, context_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, qty, entry_price, stop_price, target_price,
         trail_pct, entry_price, _now(), note,
         broker_order_id, broker_mode, entry_price,
         _json.dumps(context) if context else None),
    )
    conn.commit()
    return get_position(conn, cur.lastrowid)


def set_realised_gbp(conn: sqlite3.Connection, pos_id: int,
                     amount_gbp: float) -> Position:
    """Record what the broker says the trade actually made, in GBP."""
    conn.execute("UPDATE positions SET realised_gbp_broker = ? WHERE id = ?",
                 (amount_gbp, pos_id))
    conn.commit()
    return get_position(conn, pos_id)


def get_position(conn: sqlite3.Connection, pos_id: int) -> Position:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
    if row is None:
        raise KeyError(f"No position with id {pos_id}")
    return _row_to_position(row)


def find_open(conn: sqlite3.Connection, ticker: str) -> Optional[Position]:
    row = conn.execute(
        "SELECT * FROM positions WHERE ticker = ? AND closed_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (ticker.upper().strip(),),
    ).fetchone()
    return _row_to_position(row) if row else None


def open_positions(conn: sqlite3.Connection) -> list[Position]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY ticker"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def closed_positions(conn: sqlite3.Connection) -> list[Position]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def close_position(
    conn: sqlite3.Connection, ticker: str, exit_price: float
) -> Position:
    pos = find_open(conn, ticker)
    if pos is None:
        raise KeyError(f"You have no open position in {ticker.upper()}.")
    conn.execute(
        "UPDATE positions SET closed_at = ?, exit_price = ? WHERE id = ?",
        (_now(), exit_price, pos.id),
    )
    conn.execute("DELETE FROM fired_alerts WHERE position_id = ?", (pos.id,))
    conn.commit()
    return get_position(conn, pos.id)


def close_position_by_id(conn: sqlite3.Connection, pos_id: int,
                         exit_price: float, note: str = "") -> Position:
    """
    Close a specific position by its id.

    close_position() finds by ticker, which is ambiguous the moment two
    positions in the same symbol exist. Reconciliation always knows the exact
    row it is closing, so it uses this.
    """
    pos = get_position(conn, pos_id)
    if not pos.is_open:
        return pos
    suffix = f" {note}".rstrip() if note else ""
    conn.execute(
        "UPDATE positions SET closed_at = ?, exit_price = ?, "
        "note = TRIM(COALESCE(note,'') || ?) WHERE id = ?",
        (_now(), exit_price, suffix, pos_id),
    )
    conn.execute("DELETE FROM fired_alerts WHERE position_id = ?", (pos_id,))
    conn.commit()
    return get_position(conn, pos_id)


def claim_once(conn: sqlite3.Connection, key: str) -> bool:
    """
    Returns True the FIRST time a key is claimed and False every time after.

    Used for things that must happen once per day — the end-of-day flatten,
    the summary post — so a scan loop running every 60 seconds doesn't do
    them sixty times an hour. Survives a restart because it lives in the
    database, not in memory.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        (key, _now()),
    )
    conn.commit()
    return cur.rowcount > 0


def broker_managed_open(conn: sqlite3.Connection) -> list[Position]:
    """Open positions the bot placed itself, so Alpaca can be asked about them."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE closed_at IS NULL "
        "AND broker_order_id IS NOT NULL ORDER BY id"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def closed_since(conn: sqlite3.Connection, iso_utc: str) -> list[Position]:
    """Positions closed at or after an ISO-8601 UTC timestamp, oldest first."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE closed_at IS NOT NULL "
        "AND closed_at >= ? ORDER BY closed_at",
        (iso_utc,),
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def set_stop(conn: sqlite3.Connection, ticker: str, stop_price: float) -> Position:
    pos = find_open(conn, ticker)
    if pos is None:
        raise KeyError(f"You have no open position in {ticker.upper()}.")
    if stop_price >= pos.entry_price:
        raise ValueError(
            f"Stop ({stop_price}) must be below your entry ({pos.entry_price})."
        )
    conn.execute("UPDATE positions SET stop_price = ? WHERE id = ?",
                 (stop_price, pos.id))
    conn.execute("DELETE FROM fired_alerts WHERE position_id = ? AND kind = 'stop_hit'",
                 (pos.id,))
    conn.commit()
    return get_position(conn, pos.id)


def set_target(conn: sqlite3.Connection, ticker: str, target_price: float) -> Position:
    pos = find_open(conn, ticker)
    if pos is None:
        raise KeyError(f"You have no open position in {ticker.upper()}.")
    if target_price <= pos.entry_price:
        raise ValueError(
            f"Target ({target_price}) must be above your entry ({pos.entry_price})."
        )
    conn.execute("UPDATE positions SET target_price = ? WHERE id = ?",
                 (target_price, pos.id))
    conn.execute("DELETE FROM fired_alerts WHERE position_id = ? AND kind = 'target_hit'",
                 (pos.id,))
    conn.commit()
    return get_position(conn, pos.id)


def set_trail(conn: sqlite3.Connection, ticker: str, trail_pct: float) -> Position:
    pos = find_open(conn, ticker)
    if pos is None:
        raise KeyError(f"You have no open position in {ticker.upper()}.")
    if not (0 < trail_pct < 100):
        raise ValueError("Trailing stop must be a percentage between 0 and 100.")
    conn.execute("UPDATE positions SET trail_pct = ? WHERE id = ?",
                 (trail_pct, pos.id))
    conn.commit()
    return get_position(conn, pos.id)


def update_peak(conn: sqlite3.Connection, pos_id: int, price: float) -> None:
    conn.execute(
        "UPDATE positions SET peak_price = ? WHERE id = ? AND peak_price < ?",
        (price, pos_id, price),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Alert de-duplication
# --------------------------------------------------------------------------

def already_fired(conn: sqlite3.Connection, pos_id: int, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fired_alerts WHERE position_id = ? AND kind = ?",
        (pos_id, kind),
    ).fetchone()
    return row is not None


def mark_fired(conn: sqlite3.Connection, pos_id: int, kind: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fired_alerts (position_id, kind, fired_at) "
        "VALUES (?, ?, ?)",
        (pos_id, kind, _now()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# The rules engine
# --------------------------------------------------------------------------

def _quote_ccy(ticker: str) -> str:
    """The currency a position's P&L is denominated in, for display."""
    try:
        import fx
        return fx.quote_currency(ticker)
    except Exception:  # noqa: BLE001
        return "USD"


def evaluate(pos: Position, price: float) -> list[Alert]:
    """
    Compare a live price against the levels Bob set on this position.

    Returns factual alerts only. Nothing here forecasts anything; every
    alert corresponds to a threshold the user chose in advance.

    DIRECTION MATTERS HERE. On a short, the stop is ABOVE the entry and is
    hit when price RISES. Every comparison below used to be the long one
    only, which would have left a short's stop alert silent while the
    trade ran away.
    """
    alerts: list[Alert] = []
    pnl = pos.unrealised(price)
    pct = pos.unrealised_pct(price)
    short = pos.is_short

    def _hit_stop() -> bool:
        if pos.stop_price is None:
            return False
        return price >= pos.stop_price if short else price <= pos.stop_price

    def _hit_target() -> bool:
        if pos.target_price is None:
            return False
        return price <= pos.target_price if short else price >= pos.target_price

    # --- Stop loss ---------------------------------------------------------
    if _hit_stop():
        alerts.append(Alert(
            ticker=pos.ticker,
            kind="stop_hit",
            severity="urgent",
            price=price,
            message=(
                f"**STOP HIT — {pos.ticker} at `{price:g}`**\n"
                f"Your stop was `{pos.stop_price:g}` on a {pos.direction}. "
                f"Position is down **{abs(pnl):,.2f}** "
                f"{_quote_ccy(pos.ticker)} ({pct:+.2f}%).\n\n"
                f"You decided this level before you entered. "
                f"The plan says exit. Moving a stop down to avoid taking "
                f"the loss is the single most common way beginners turn a "
                f"small loss into an account-ending one."
            ),
        ))

    # --- Take profit -------------------------------------------------------
    if _hit_target():
        alerts.append(Alert(
            ticker=pos.ticker,
            kind="target_hit",
            severity="urgent",
            price=price,
            message=(
                f"**TARGET HIT — {pos.ticker} at `{price:g}`**\n"
                f"Your target was `{pos.target_price:g}` on a "
                f"{pos.direction}. Position is up **{pnl:,.2f}** "
                f"{_quote_ccy(pos.ticker)} ({pct:+.2f}%).\n\n"
                f"This is the level you picked. Taking it is not leaving "
                f"money on the table — it's the plan working."
            ),
        ))

    # --- Trailing stop -----------------------------------------------------
    # [CHOICE] Trails stay long-only. The book does not describe one, the
    # peak tracking in this table only ever moves up, and inventing a
    # mirrored version nobody asked for is how untested code gets written.
    if pos.trail_pct is not None and not short:
        trail_level = pos.peak_price * (1 - pos.trail_pct / 100)
        if price <= trail_level and pos.peak_price > pos.entry_price:
            alerts.append(Alert(
                ticker=pos.ticker,
                kind="trail_hit",
                severity="urgent",
                price=price,
                message=(
                    f"**TRAILING STOP HIT — {pos.ticker} at ${price:,.2f}**\n"
                    f"Peak was ${pos.peak_price:,.2f}; your {pos.trail_pct:.1f}% "
                    f"trail sits at ${trail_level:,.2f}.\n"
                    f"Position is {'up' if pnl >= 0 else 'down'} "
                    f"**${abs(pnl):,.2f}** ({pct:+.2f}%)."
                ),
            ))

    # --- Unprotected position ---------------------------------------------
    if pos.stop_price is None and pos.trail_pct is None:
        alerts.append(Alert(
            ticker=pos.ticker,
            kind="risk_warning",
            severity="warn",
            price=price,
            message=(
                f"**{pos.ticker} has no stop loss.**\n"
                f"Currently {pct:+.2f}% (${pnl:+,.2f}).\n"
                f"Your maximum loss on this position is currently "
                f"**everything you put in** (${pos.cost_basis:,.2f}).\n"
                f"Set one with `!stop {pos.ticker} <price>`."
            ),
        ))

    # --- Large drawdown on an unstopped position --------------------------
    if pct <= -10 and pos.stop_price is None:
        alerts.append(Alert(
            ticker=pos.ticker,
            kind="deep_loss",
            severity="urgent",
            price=price,
            message=(
                f"**{pos.ticker} is down {pct:.1f}%** (${pnl:,.2f}) "
                f"with no stop set.\n"
                f"A 10% loss needs an 11% gain to break even. "
                f"A 50% loss needs 100%. Decide your exit now, "
                f"while it's still a decision rather than a rescue."
            ),
        ))

    return alerts


def portfolio_summary(
    conn: sqlite3.Connection, prices: dict[str, float]
) -> dict:
    """
    Aggregate open + realised P&L, IN POUNDS. Facts only.

    Every figure is converted per position before being added up. Adding a
    yen profit to a dollar profit and calling the result a number was the
    bug waiting to happen here the moment the bot started trading more than
    one quote currency.
    """
    import fx

    opens = open_positions(conn)
    unrealised = 0.0
    exposure = 0.0
    unpriced: list[str] = []

    for p in opens:
        price = prices.get(p.ticker)
        if price is None:
            unpriced.append(p.ticker)
            continue
        unrealised += fx.pnl_to_gbp(p.unrealised(price), p.ticker)
        exposure += fx.pnl_to_gbp(abs(p.qty) * price, p.ticker)

    closed = closed_positions(conn)
    results = [(p, p.realised_gbp()) for p in closed]

    realised = sum(g for _, g in results)
    wins = [g for _, g in results if g > 0]
    losses = [g for _, g in results if g < 0]

    return {
        "open_count": len(opens),
        "exposure": exposure,
        "unrealised": unrealised,
        "realised": realised,
        "total": unrealised + realised,
        "currency": "GBP",
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / (len(wins) + len(losses)) * 100)
                    if (wins or losses) else None,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "unpriced": unpriced,
    }
