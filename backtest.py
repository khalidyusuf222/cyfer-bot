"""
Backtest — what this bot would have done on past prices.

    venv/bin/python3 backtest.py          # the last 12 weeks
    venv/bin/python3 backtest.py 26       # the last 26 weeks

Or in Discord:  !backtest   /   !backtest 26

HOW IT WORKS
------------
It fetches OANDA's own candle history — the same feed the bot trades on —
then walks forward through it five minutes at a time. At every step it does
exactly what the live bot would do at that moment, using the live bot's own
code, not a copy of it:

    sessions.current_state    may a trade be opened right now?
    the risk gates            trades today, daily loss cap, losses in a row
    cyfer.scan                the strategy itself, on the last 200 1-hour
                              and last 60 5-minute candles — no more, so it
                              can never see a candle that hadn't closed yet
    risk.size_trade           position size: the risk setting, cut to fit the UK leverage limit
    oanda.validate_levels     the same pre-flight the real order goes through
    cyfer.breakeven_stop      the stop-to-entry move at 1R

It never places an order, never touches the trade database, and runs as a
separate program, so it can't disturb the live bot while it works.

WHAT IT CAN'T KNOW
------------------
Real fills. The spread is taken from the prices on Bob's own OANDA screen
and charged on both entry and exit, but there is no extra slippage, no
financing, and no requotes. When a single 5-minute candle touches both the
stop and the target, it assumes the stop — the pessimistic reading, because
a candle doesn't say which came first.

And it is the past. It says what these rules would have done over these
weeks. It says nothing about next week.

WHY IT'S A FAIR TEST
--------------------
None of the strategy's numbers were tuned on this data. They came from the
Cyfer guide, or were chosen before any price history was looked at. That
matters: a strategy fitted to the data it's tested on always looks good.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import cyfer
import graystone
import pairs
import sessions
from config import CONFIG
from strategy import Bar

# Taken from Bob's OANDA instrument panel, 2026-09-15. Spread is a real cost
# on every trade, and it hurts tight stops most.  [BOOK p8]
SPREAD_PIPS = {"EUR_USD": 0.8, "GBP_USD": 1.3, "USD_JPY": 1.5, "AUD_USD": 1.3}
DEFAULT_SPREAD_PIPS = 1.5

HTF_MINUTES, LTF_MINUTES = 60, 5
HTF_WINDOW, LTF_WINDOW = 200, 60          # exactly what the live scan asks for

DEFAULT_WEEKS, MAX_WEEKS = 12, 52


# ===========================================================================
# Time
# ===========================================================================

def parse_ts(s: str) -> datetime:
    """OANDA sends RFC3339 with nanoseconds, which datetime won't read."""
    s = s.strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest, ""
        for sep in ("+", "-"):
            if sep in rest:
                frac, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ===========================================================================
# What happened
# ===========================================================================

@dataclass
class Trade:
    pair: str
    long: bool
    decided: datetime
    opened: datetime
    entry: float                 # the fill, spread included
    stop: float
    target: float
    first_stop: float
    units: int
    risk_gbp: float
    score: int
    total: int
    rate: float                  # quote currency -> GBP
    closed: Optional[datetime] = None
    exit: Optional[float] = None
    reason: str = ""             # target | stop | breakeven | weekend | open
    moved_to_breakeven: bool = False

    @property
    def signed_units(self) -> int:
        return self.units if self.long else -self.units

    @property
    def pnl_gbp(self) -> float:
        if self.exit is None:
            return 0.0
        return (self.exit - self.entry) * self.signed_units * self.rate

    @property
    def is_open(self) -> bool:
        return self.reason in ("", "open")


@dataclass
class Result:
    trades: list[Trade]
    start: Optional[datetime]
    end: Optional[datetime]
    pairs: list[str]
    scans: int = 0
    alerts: int = 0
    skipped_holding: int = 0
    skipped_refused: int = 0
    capped: int = 0                  # cut down to fit leverage / free margin
    lowest_equity: Optional[float] = None
    lowest_equity_at: Optional[datetime] = None
    wiped_out_at: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if not t.is_open]


# ===========================================================================
# The replay
# ===========================================================================

def _exit_on(tr: Trade, bar: Bar, spread: float) -> Optional[tuple[float, str]]:
    """
    Did this candle take the trade out, and at what price?

    Mid-price candles. A long sells at the BID (mid minus half the spread),
    a short buys back at the ASK. A candle that opens beyond a level fills
    at the open — a gap is worse than the level, never better. A candle
    that touches both levels is read as a stop.
    """
    half = spread / 2
    if tr.long:
        o, h, lo = bar.open - half, bar.high - half, bar.low - half
        if o <= tr.stop:
            return o, "stop"
        if o >= tr.target:
            return o, "target"
        if lo <= tr.stop:
            return tr.stop, "stop"
        if h >= tr.target:
            return tr.target, "target"
    else:
        o, h, lo = bar.open + half, bar.high + half, bar.low + half
        if o >= tr.stop:
            return o, "stop"
        if o <= tr.target:
            return o, "target"
        if h >= tr.stop:
            return tr.stop, "stop"
        if lo <= tr.target:
            return tr.target, "target"
    return None


class _Day:
    """The live bot's daily risk ledger, in memory. Resets each UTC day."""

    def __init__(self):
        self.day: Optional[date] = None
        self.trades = self.consec = 0
        self.pnl = 0.0
        self.locked = False

    def roll(self, t: datetime) -> None:
        d = t.astimezone(timezone.utc).date()
        if d != self.day:
            self.day, self.trades, self.consec = d, 0, 0
            self.pnl, self.locked = 0.0, False

    def allowed(self) -> bool:
        r = CONFIG.risk
        limit = CONFIG.display.account_gbp * r.max_daily_loss_pct / 100
        return (not self.locked and self.trades < r.max_trades_per_day
                and self.pnl > -limit and self.consec < r.max_consecutive_losses)

    def closed(self, t: datetime, pnl: float) -> None:
        # Same rule as risk.record_trade_closed: booked on the day it CLOSES,
        # a break-even resets the losing streak, and either limit locks the
        # rest of that day.
        self.roll(t)
        self.consec = 0 if pnl >= 0 else self.consec + 1
        self.pnl += pnl
        r = CONFIG.risk
        limit = CONFIG.display.account_gbp * r.max_daily_loss_pct / 100
        if self.consec >= r.max_consecutive_losses or self.pnl <= -limit:
            self.locked = True


def _account_now(open_trades, pending, P, realised: float) -> tuple[float, float]:
    """
    (equity, free margin) in GBP, the way OANDA works them out.

    Equity is the account plus closed profit plus open profit, marked at
    each pair's last close. Margin is the deposit held against each open
    or about-to-fill position: its size in pounds divided by the leverage
    limit for that pair. Free margin is equity minus margin, and it is
    what the next trade has to fit inside.
    """
    import risk
    equity = CONFIG.display.account_gbp + realised
    used = 0.0
    for tr in open_trades:
        p = P[tr.pair]
        last = p["last"]
        mark = last.close if last is not None else tr.entry
        equity += (mark - tr.entry) * tr.signed_units * tr.rate
        used += tr.units * mark * tr.rate / risk.max_leverage(p["pair"])
    for pend in pending:
        p = P[pend["pair"]]
        used += pend["units"] * pend["price"] * p["rate"] / \
            risk.max_leverage(p["pair"])
    return equity, equity - used


def run(history: dict, spreads: Optional[dict] = None,
        start: Optional[datetime] = None,
        progress=None) -> Result:
    """
    Replay the strategy over `history` = {pair: {"htf": [...], "ltf": [...]}},
    each a list of completed Bars, oldest first.

    `start` limits which moments are traded; candles before it are only used
    as history for the first scans.
    """
    import risk
    import oanda
    import fx

    y, s_cfg, r_cfg = CONFIG.cyfer, CONFIG.strategy, CONFIG.risk
    spreads = {**SPREAD_PIPS, **(spreads or {})}
    watch = [p for p in CONFIG.instruments.watchlist if p in history] \
        or sorted(history)

    P: dict[str, dict] = {}
    for sym in watch:
        pair = pairs.parse(sym)
        htf, ltf = history[sym]["htf"], history[sym]["ltf"]
        lc = [parse_ts(b.ts) + timedelta(minutes=LTF_MINUTES) for b in ltf]
        P[sym] = {
            "pair": pair, "htf": htf, "ltf": ltf,
            "hc": [parse_ts(b.ts) + timedelta(minutes=HTF_MINUTES) for b in htf],
            "at": {t: i for i, t in enumerate(lc)},
            "hp": 0,
            "last": None,
            "spread": spreads.get(sym, DEFAULT_SPREAD_PIPS) * pair.pip,
            "rate": fx.rate_to_gbp(pair.quote)[0],
        }

    times = sorted({t for p in P.values() for t in p["at"]})
    res = Result(trades=[], start=None, end=None, pairs=list(watch))
    if not times:
        res.notes.append("No price history came back.")
        return res

    day = _Day()
    realised = 0.0
    open_trades: list[Trade] = []
    pending: list[dict] = []
    traded_keys: set = set()
    alert_keys: set = set()
    flattened: set = set()

    for step, t in enumerate(times):
        if progress and step % 2000 == 0:
            progress(step, len(times))
        day.roll(t)

        # --- 1. fill orders decided on the previous candle ----------------
        for pend in list(pending):
            p = P[pend["pair"]]
            i = p["at"].get(t)
            if i is None:
                continue
            bar = p["ltf"][i]
            half = p["spread"] / 2
            fill = bar.open + half if pend["long"] else bar.open - half
            tr = Trade(pair=pend["pair"], long=pend["long"],
                       decided=pend["decided"], opened=t - timedelta(minutes=LTF_MINUTES),
                       entry=fill, stop=pend["stop"], target=pend["target"],
                       first_stop=pend["stop"], units=pend["units"],
                       risk_gbp=pend["risk_gbp"], score=pend["score"],
                       total=pend["total"], rate=p["rate"])
            open_trades.append(tr)
            res.trades.append(tr)
            pending.remove(pend)

        # --- 2. did this candle close anything? ---------------------------
        for tr in list(open_trades):
            p = P[tr.pair]
            i = p["at"].get(t)
            if i is None:
                continue
            bar = p["ltf"][i]
            hit = _exit_on(tr, bar, p["spread"])
            if hit:
                tr.exit, why = hit
                tr.closed = t
                tr.reason = ("breakeven" if why == "stop" and tr.moved_to_breakeven
                             else why)
                open_trades.remove(tr)
                day.closed(t, tr.pnl_gbp)
                realised += tr.pnl_gbp
                continue
            # [BOOK p47] stop to entry once 1R in profit — checked on the
            # candle close, as the live bot checks the current price
            if y.breakeven_enabled and not tr.moved_to_breakeven:
                new = cyfer.breakeven_stop(tr.entry, tr.stop, bar.close,
                                           "bullish" if tr.long else "bearish")
                if new is not None:
                    tr.stop, tr.moved_to_breakeven = new, True

        for sym, p in P.items():
            i = p["at"].get(t)
            if i is not None:
                p["last"] = p["ltf"][i]

        equity, free_margin = _account_now(open_trades, pending, P, realised)
        if res.lowest_equity is None or equity < res.lowest_equity:
            res.lowest_equity, res.lowest_equity_at = equity, t
        if equity <= 0 and res.wiped_out_at is None:
            res.wiped_out_at = t
        if res.wiped_out_at is not None:
            continue                    # nothing left to trade with

        # --- 3. flat before the weekend -----------------------------------
        s = CONFIG.sessions
        et = t.astimezone(sessions.ET)
        if (s.flatten_before_weekend and et.weekday() == s.week_close_day
                and s.weekend_flatten <= et.strftime("%H:%M") < s.week_close
                and et.date() not in flattened):
            flattened.add(et.date())
            for tr in list(open_trades):
                last = P[tr.pair]["last"]
                if last is None:
                    continue
                half = P[tr.pair]["spread"] / 2
                tr.exit = last.close - half if tr.long else last.close + half
                tr.closed, tr.reason = t, "weekend"
                open_trades.remove(tr)
                day.closed(t, tr.pnl_gbp)
                realised += tr.pnl_gbp
            pending.clear()

        # --- 4. decide ----------------------------------------------------
        if start is not None and t < start:
            for p in P.values():                  # keep pointers moving
                while p["hp"] < len(p["hc"]) and p["hc"][p["hp"]] <= t:
                    p["hp"] += 1
            continue

        state = sessions.current_state(t)
        if not state.can_enter:
            for p in P.values():
                while p["hp"] < len(p["hc"]) and p["hc"][p["hp"]] <= t:
                    p["hp"] += 1
            continue

        if res.start is None:
            res.start = t
        res.end = t

        for sym in watch:
            p = P[sym]
            while p["hp"] < len(p["hc"]) and p["hc"][p["hp"]] <= t:
                p["hp"] += 1
            i = p["at"].get(t)
            if i is None or p["hp"] < HTF_WINDOW or i + 1 < LTF_WINDOW:
                continue
            if not day.allowed():
                break                       # the live loop returns here too

            htf = p["htf"][p["hp"] - HTF_WINDOW:p["hp"]]
            ltf = p["ltf"][i - LTF_WINDOW + 1:i + 1]
            sig = cyfer.scan(sym, htf, ltf,
                             ema_direction=graystone.ema_stack_direction(htf))
            res.scans += 1
            if sig is None or sig.score < s_cfg.min_alert_score:
                continue

            key = (sym, sig.direction, et.strftime("%Y-%m-%d-%H"))
            alert_keys.add(key)
            if sig.score < s_cfg.min_auto_score or not sig.tradeable:
                continue
            if key in traded_keys:
                continue

            if getattr(s_cfg, "one_position_per_pair", True) and (
                    any(tr.pair == sym for tr in open_trades)
                    or any(pd["pair"] == sym for pd in pending)):
                res.skipped_holding += 1
                continue

            long = sig.direction == "bullish"
            if (long and sig.stop >= sig.entry) or \
               (not long and sig.stop <= sig.entry):
                res.skipped_refused += 1
                continue
            try:
                sized = risk.size_trade(sig.entry, sig.stop, sym,
                                        target=sig.target,
                                        margin_available_gbp=free_margin)
            except ValueError:
                res.skipped_refused += 1
                continue
            if sized.units < 1:
                res.skipped_refused += 1
                continue

            signed = sized.units if long else -sized.units
            problems = oanda.validate_levels(signed, sig.entry, sig.stop,
                                             sig.target)
            cap = CONFIG.display.account_gbp * r_cfg.risk_per_trade_pct / 100
            if problems or sized.risk_gbp > cap * 1.25:
                res.skipped_refused += 1
                continue

            traded_keys.add(key)
            day.trades += 1
            if sized.capped:
                res.capped += 1
            pending.append({
                "pair": sym, "long": long, "decided": t,
                "stop": sig.stop, "target": sig.target, "price": sig.entry,
                "units": sized.units, "risk_gbp": sized.risk_gbp,
                "score": sig.score, "total": sig.total,
            })
            # the next pair in this same candle sees this one's margin
            free_margin -= (sized.units * sig.entry * P[sym]["rate"]
                            / risk.max_leverage(P[sym]["pair"]))

    for tr in open_trades:
        last = P[tr.pair]["last"]
        if last is not None:
            half = P[tr.pair]["spread"] / 2
            tr.exit = last.close - half if tr.long else last.close + half
            tr.closed = times[-1]
        tr.reason = "open"

    res.alerts = len(alert_keys)
    return res


# ===========================================================================
# The numbers
# ===========================================================================

def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a win rate. Honest at small samples, unlike ±."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def summarise(res: Result) -> dict:
    closed = res.closed
    pnls = [t.pnl_gbp for t in closed]
    wins = [x for x in pnls if x > 0.005]
    losses = [x for x in pnls if x < -0.005]
    scratch = len(pnls) - len(wins) - len(losses)
    decided = len(wins) + len(losses)

    span_days = ((res.end - res.start).total_seconds() / 86400
                 if res.start and res.end else 0)
    span_weeks = max(span_days / 7, 1 / 7)

    equity, peak, dd = 0.0, 0.0, 0.0
    streak = worst_streak = 0
    for t in sorted(closed, key=lambda t: t.closed):
        equity += t.pnl_gbp
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        if t.pnl_gbp < -0.005:
            streak += 1
            worst_streak = max(worst_streak, streak)
        elif t.pnl_gbp > 0.005:
            streak = 0

    weekly: dict[tuple, float] = {}
    if res.start and res.end:
        d = res.start.date()
        while d <= res.end.date():
            weekly.setdefault(tuple(d.isocalendar()[:2]), 0.0)
            d += timedelta(days=1)
    for t in closed:
        k = tuple(t.closed.date().isocalendar()[:2])
        weekly[k] = weekly.get(k, 0.0) + t.pnl_gbp

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    be = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) else None
    lo, hi = _wilson(len(wins), decided)

    per_pair = {}
    for sym in res.pairs:
        mine = [t for t in closed if t.pair == sym]
        per_pair[sym] = {
            "trades": len(mine),
            "wins": sum(1 for t in mine if t.pnl_gbp > 0.005),
            "losses": sum(1 for t in mine if t.pnl_gbp < -0.005),
            "pnl": sum(t.pnl_gbp for t in mine),
        }

    reasons = {}
    for t in closed:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1

    return {
        "weeks": span_weeks, "start": res.start, "end": res.end,
        "trades": len(closed), "open": len(res.trades) - len(closed),
        "wins": len(wins), "losses": len(losses), "scratch": scratch,
        "win_rate": len(wins) / decided if decided else None,
        "win_lo": lo, "win_hi": hi, "breakeven_rate": be,
        "pnl": sum(pnls), "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "max_drawdown": dd, "worst_streak": worst_streak,
        "per_week": len(closed) / span_weeks,
        "weekly": [weekly[k] for k in sorted(weekly)],
        "per_pair": per_pair, "reasons": reasons,
        "scans": res.scans, "alerts": res.alerts,
        "skipped_holding": res.skipped_holding,
        "skipped_refused": res.skipped_refused,
        "capped": res.capped,
        "avg_risk": (sum(t.risk_gbp for t in closed) / len(closed)
                     if closed else 0.0),
        "lowest_equity": res.lowest_equity,
        "lowest_equity_at": res.lowest_equity_at,
        "wiped_out_at": res.wiped_out_at,
        "notes": res.notes,
    }


def verdict(s: dict) -> str:
    n = s["trades"]
    if n == 0:
        return ("**It never traded.** Nothing in these weeks cleared the "
                "bar. The rules may be too strict for these markets, or it "
                "was a quiet stretch — but either way, a strategy that "
                "doesn't trade can't make anything.")
    if n < CONFIG.review.min_trades_to_diagnose:
        return (f"**Too few trades to judge — {n}.** The review needs at "
                f"least {CONFIG.review.min_trades_to_diagnose} before the "
                f"win rate means much. Try a longer test: `!backtest 26`.")
    be, lo, hi = s["breakeven_rate"], s["win_lo"], s["win_hi"]
    if be is None:
        return "Not enough wins and losses to compare against break-even."
    if lo > be:
        return (f"**Better than break-even on this sample.** Even the low "
                f"end of the likely win rate ({lo:.0%}) beats the "
                f"{be:.0%} it needs. That's evidence of an edge over these "
                f"weeks — not a promise about the next ones.")
    if hi < be:
        return (f"**Worse than break-even on this sample.** Even the high "
                f"end of the likely win rate ({hi:.0%}) is below the "
                f"{be:.0%} it needs to pay for its losses. On these weeks, "
                f"these rules lose money.")
    return (f"**Can't tell it apart from break-even.** It needs to win "
            f"{be:.0%} of the time; the true win rate is likely somewhere "
            f"between {lo:.0%} and {hi:.0%}. That range straddles the line, "
            f"so these weeks don't show whether the strategy works.")


def report(res: Result, requested_weeks: Optional[int] = None) -> str:
    s = summarise(res)
    acct = CONFIG.display.account_gbp
    wk = requested_weeks or round(s["weeks"])

    def gbp(x: float) -> str:
        return f"{'+' if x >= 0 else '−'}£{abs(x):,.2f}"

    if s["start"] is None:
        return ("**Backtest — no data**\n\nOANDA didn't return any usable "
                "candles for the period. " + " ".join(s["notes"]))

    lines = [
        f"**Backtest — last {wk} weeks, {len(res.pairs)} pairs**",
        f"OANDA prices, {s['start']:%d %b} → {s['end']:%d %b %Y} · "
        f"£{acct:,.0f} account, {CONFIG.risk.risk_per_trade_pct:g}% risk "
        f"setting",
        "",
        f"Trades: **{s['trades']}** ({s['per_week']:.1f} a week) · "
        f"{s['wins']} won / {s['losses']} lost / {s['scratch']} break-even",
    ]
    if s["win_rate"] is not None:
        lines.append(f"Win rate: **{s['win_rate']:.0%}** "
                     f"(likely somewhere {s['win_lo']:.0%}–{s['win_hi']:.0%})")
    lines += [
        f"Result: **{gbp(s['pnl'])}** "
        f"({s['pnl'] / acct * 100:+.1f}% of the account)",
    ]
    if s["weekly"]:
        lines.append(f"Average week {gbp(s['pnl'] / max(s['weeks'], 1e-9))} · "
                     f"best {gbp(max(s['weekly']))} · "
                     f"worst {gbp(min(s['weekly']))}")
    if s["trades"]:
        pf = s["profit_factor"]
        lines += [
            f"Average win £{s['avg_win']:,.2f} · average loss "
            f"£{s['avg_loss']:,.2f}"
            + (f" · profit factor {pf:.2f}" if pf is not None else ""),
            f"Worst dip from a high: {gbp(s['max_drawdown'])} "
            f"(−{abs(s['max_drawdown']) / acct * 100:.1f}%) · longest losing run "
            f"{s['worst_streak']}",
        ]
        if s["breakeven_rate"] is not None:
            lines.append(f"Win rate needed to break even with these wins "
                         f"and losses: **{s['breakeven_rate']:.0%}**")
        risk_line = (f"Real risk per trade: **£{s['avg_risk']:,.2f}** "
                     f"({s['avg_risk'] / acct * 100:.1f}% on average)")
        if s["capped"]:
            risk_line += (f" · {s['capped']} trades cut down to fit the "
                          f"UK leverage limit")
        lines.append(risk_line)
    if s["lowest_equity"] is not None:
        low = s["lowest_equity"]
        lines.append(f"Lowest the account went: **£{low:,.2f}**"
                     + (f" ({s['lowest_equity_at']:%d %b})"
                        if s["lowest_equity_at"] else ""))
    if s["wiped_out_at"] is not None:
        lines.append(f"**The account was wiped out on "
                     f"{s['wiped_out_at']:%d %b}.** Nothing after that "
                     f"date was traded.")

    if s["trades"]:
        lines += ["", "**By pair**"]
        for sym, pp in s["per_pair"].items():
            lines.append(f"{pairs.parse(sym).display}: {pp['trades']} trades, "
                         f"{pp['wins']}W/{pp['losses']}L, {gbp(pp['pnl'])}")
        r = s["reasons"]
        lines += ["", "**How they ended:** "
                  + " · ".join(f"{r.get(k, 0)} {k}" for k in
                               ("target", "stop", "breakeven", "weekend"))]
    if s["open"]:
        lines.append(f"*{s['open']} still open at the end — not counted.*")

    lines += ["", verdict(s)]

    if s["weekly"]:
        best = max(s["weekly"])
        if s["wins"]:
            per_win = f"the average win was £{s['avg_win']:,.0f}"
        else:
            per_win = (f"at {CONFIG.risk.risk_per_trade_pct:g}% risk a win "
                       f"would be worth up to £"
                       f"{acct * CONFIG.risk.risk_per_trade_pct / 100 * CONFIG.cyfer.min_risk_reward:,.0f}")
        lines += ["", f"**Against £1,000 a week:** the best week here was "
                  f"{gbp(best)}, and {per_win}."]

    lines += [
        "",
        f"*{s['scans']:,} scans · {s['alerts']} alerts you'd have been sent · "
        f"{s['skipped_holding']} setups skipped because that pair was "
        f"already open · {s['skipped_refused']} refused at pre-flight.*",
        "",
        "*Past prices, not a forecast. The spread is charged both ways, but "
        "fills are otherwise ideal, and a candle touching both levels "
        "counts as a stop. None of the rules were tuned on this data.*",
    ]
    return "\n".join(lines)


# ===========================================================================
# Getting the history
# ===========================================================================

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Small windows: OANDA's practice server has answered a single 100-day
# hourly request with a 504 (its gateway gave up waiting). Four days of
# 5-minute candles is at most 1,152; thirty days of hourly ones 720.
LTF_STEP, HTF_STEP = timedelta(days=4), timedelta(days=30)
FETCH_BUDGET_SECONDS = 8 * 60          # the bot kills the job at 15 minutes


def _window(sym: str, granularity: str, a: datetime, b: datetime,
            deadline: float, splits_left: int = 2) -> list:
    """
    One window of candles. If OANDA's server fails on it even after the
    retries, the window is split in half and each half asked for
    separately - a smaller question is often one it can answer.
    """
    import time
    import oanda
    if time.monotonic() > deadline:
        raise oanda.OandaError(
            "OANDA is answering too slowly right now to download the "
            "history. Try `!backtest` again later.")
    try:
        payload = oanda._get(f"/v3/instruments/{sym}/candles", {
            "granularity": granularity, "price": "M",
            "from": _iso(a), "to": _iso(b)}, timeout=30, retries=2)
    except (oanda.OandaServerError, oanda.MarketError) as e:
        if isinstance(e, oanda.OandaError) and \
                not isinstance(e, oanda.OandaServerError):
            raise                           # bad token etc. - won't fix itself
        if splits_left <= 0 or (b - a) < timedelta(hours=12):
            raise
        mid = a + (b - a) / 2
        return (_window(sym, granularity, a, mid, deadline, splits_left - 1)
                + _window(sym, granularity, mid, b, deadline, splits_left - 1))
    return oanda.parse_candles(payload)


def _windowed(sym: str, granularity: str, frm: datetime, to: datetime,
              step: timedelta, deadline: Optional[float] = None) -> list:
    """
    OANDA returns at most 5000 candles per request and won't take `count`
    with a from/to window, so long histories are fetched window by window.
    """
    import time
    if deadline is None:
        deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    out, seen = [], set()
    cur = frm
    while cur < to:
        nxt = min(cur + step, to)
        for b in _window(sym, granularity, cur, nxt, deadline):
            if b.ts not in seen:
                seen.add(b.ts)
                out.append(b)
        cur = nxt
    out.sort(key=lambda b: parse_ts(b.ts))
    return out


def fetch(weeks: int, now: Optional[datetime] = None, log=print) -> tuple:
    """History for every pair on the watchlist, plus the moment to start."""
    now = (now or datetime.now(timezone.utc)) - timedelta(seconds=10)
    start = now - timedelta(weeks=weeks)
    # 200 hourly candles is about 8 trading days; 16 calendar days covers
    # it across a weekend with room to spare. 60 five-minute candles is 5h.
    htf_from = start - timedelta(days=16)
    ltf_from = start - timedelta(days=2)

    import time
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    history = {}
    for sym in CONFIG.instruments.watchlist:
        log(f"fetching {sym} …")
        history[sym] = {
            "htf": _windowed(sym, "H1", htf_from, now, HTF_STEP, deadline),
            "ltf": _windowed(sym, "M5", ltf_from, now, LTF_STEP, deadline),
        }
        log(f"  {len(history[sym]['htf'])} hourly, "
            f"{len(history[sym]['ltf'])} five-minute candles")
    return history, start


def main(argv: list[str]) -> int:
    try:
        weeks = int(argv[1]) if len(argv) > 1 else DEFAULT_WEEKS
    except ValueError:
        print(f"Weeks must be a number, not '{argv[1]}'.", file=sys.stderr)
        return 2
    weeks = max(1, min(weeks, MAX_WEEKS))

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    try:
        history, start = fetch(weeks, log=log)
    except Exception as e:  # noqa: BLE001 — explain it, don't dump a trace
        print(f"Couldn't fetch the price history from OANDA.\n{e}",
              file=sys.stderr)
        return 1

    log("replaying …")
    res = run(history, start=start,
              progress=lambda i, n: log(f"  {i / max(n, 1):.0%}"))
    print(report(res, weeks))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
