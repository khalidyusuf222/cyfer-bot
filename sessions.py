"""
The session clock, for a market that never closes during the week.

Forex opens Sunday 17:00 New York time and closes Friday 17:00. In between
it does not stop. There is no bell, no gap overnight, and nothing that
forces you flat at the end of a day.

What replaces market hours is the four financial centres  [BOOK p3]:

    Sydney     17:00 - 02:00 ET
    Tokyo      19:00 - 04:00 ET
    London     03:00 - 12:00 ET
    New York   08:00 - 17:00 ET

and the overlap the book calls the GOLDEN HOURS - 08:00 to 12:00 ET, when
London and New York are both open. Most of the day's range is made there.

Everything is computed in New York time because the forex week is defined
by New York's 17:00, and displayed in UK time as well because that is where
Bob is.

This module needs no judgement and cannot be wrong, which is why the rest
of the system sits on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from config import CONFIG

ET = ZoneInfo(CONFIG.sessions.timezone)
UK = ZoneInfo("Europe/London")


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _between(t: time, start: str, end: str) -> bool:
    """
    Is t inside a window? Handles windows that cross midnight, which
    Sydney (17:00-02:00) and Tokyo (19:00-04:00) both do.
    """
    a, b = _t(start), _t(end)
    if a <= b:
        return a <= t < b
    return t >= a or t < b


# ===========================================================================
# The week
# ===========================================================================

def week_open_before(now_et: datetime) -> datetime:
    """The most recent Sunday 17:00 ET at or before `now_et`."""
    s = CONFIG.sessions
    h, m = map(int, s.week_open.split(":"))
    candidate = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    while candidate.weekday() != s.week_open_day or candidate > now_et:
        candidate -= timedelta(days=1)
        candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
    return candidate


def week_close_after(now_et: datetime) -> datetime:
    """The next Friday 17:00 ET at or after `now_et`."""
    s = CONFIG.sessions
    h, m = map(int, s.week_close.split(":"))
    candidate = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    while candidate.weekday() != s.week_close_day or candidate < now_et:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
    return candidate


def market_is_open(now_et: datetime) -> bool:
    """
    Between Sunday 17:00 and Friday 17:00 ET.

    Written as two explicit edges rather than a weekday test, because the
    weekend starts and ends in the middle of a day in this timezone.
    """
    s = CONFIG.sessions
    wd, t = now_et.weekday(), now_et.time()

    if wd == s.week_close_day and t >= _t(s.week_close):
        return False                      # Friday evening
    if wd == 5:
        return False                      # Saturday
    if wd == s.week_open_day and t < _t(s.week_open):
        return False                      # Sunday, before the open
    return True


# ===========================================================================
# Where in the week are we
# ===========================================================================

@dataclass(frozen=True)
class SessionState:
    now_et: datetime
    now_uk: datetime
    phase: str          # weekend | warmup | asia | london | golden |
                        # newyork | runout
    can_enter: bool
    reason: str
    centres: tuple = ()   # which of the four are open right now
    is_golden: bool = False

    @property
    def et_str(self) -> str:
        return self.now_et.strftime("%H:%M")

    @property
    def uk_str(self) -> str:
        return self.now_uk.strftime("%H:%M")

    @property
    def centres_str(self) -> str:
        return ", ".join(self.centres) if self.centres else "none"


def open_centres(now_et: datetime) -> tuple:
    """Which financial centres are trading right now.  [BOOK p3]"""
    s = CONFIG.sessions
    t = now_et.time()
    out = []
    if _between(t, s.sydney_open, s.sydney_close):
        out.append("Sydney")
    if _between(t, s.tokyo_open, s.tokyo_close):
        out.append("Tokyo")
    if _between(t, s.london_open, s.london_close):
        out.append("London")
    if _between(t, s.newyork_open, s.newyork_close):
        out.append("New York")
    return tuple(out)


def current_state(now: datetime | None = None) -> SessionState:
    """Where in the trading week are we, and may a position be opened?"""
    s = CONFIG.sessions
    now_et = (now or datetime.now(ET)).astimezone(ET)
    now_uk = now_et.astimezone(UK)
    t = now_et.time()

    # --- shut ------------------------------------------------------------
    if not market_is_open(now_et):
        opens = next_week_open(now_et)
        return SessionState(
            now_et, now_uk, "weekend", False,
            f"Forex is shut for the weekend. It reopens Sunday "
            f"{s.week_open} ET / "
            f"{opens.astimezone(UK).strftime('%H:%M')} UK.")

    centres = open_centres(now_et)
    golden = _between(t, s.golden_start, s.golden_end)

    # --- Sunday warm-up ---------------------------------------------------
    opened = week_open_before(now_et)
    warmup_ends = opened + timedelta(minutes=s.sunday_warmup_minutes)
    if now_et < warmup_ends:
        mins = int((warmup_ends - now_et).total_seconds()) // 60
        return SessionState(
            now_et, now_uk, "warmup", False,
            f"First {s.sunday_warmup_minutes} minutes of the week - spreads "
            f"are at their widest and the Sunday open can gap. No entries "
            f"for another {mins}m.",
            centres, golden)

    # --- Friday run-out ---------------------------------------------------
    # [CHOICE 2026-09-23] friday_last_entry: a trade opened late on Friday
    # rarely has time to reach its target before the weekend close.
    last = getattr(s, "friday_last_entry", s.entry_window_end)
    if now_et.weekday() == s.week_close_day and t >= _t(last):
        return SessionState(
            now_et, now_uk, "runout", False,
            f"Friday past {last} ET - no new positions into the weekend, "
            f"because a trade opened now rarely has time to reach its "
            f"target. Everything open is closed at {s.weekend_flatten} ET.",
            centres, golden)

    # --- the golden hours  [BOOK p3] --------------------------------------
    if golden:
        return SessionState(
            now_et, now_uk, "golden", True,
            "London/New York overlap - the golden hours. Highest volume, "
            "tightest spreads. Entries permitted.",
            centres, True)

    if s.golden_hours_only:
        return SessionState(
            now_et, now_uk, "asia" if "Tokyo" in centres else "london", False,
            f"Outside the golden hours ({s.golden_start}-{s.golden_end} ET) "
            f"and golden_hours_only is on. No entries.",
            centres, False)

    # --- the rest of the London/New York stretch --------------------------
    if _between(t, s.entry_window_start, s.entry_window_end):
        phase = "newyork" if "New York" in centres else "london"
        return SessionState(
            now_et, now_uk, phase, True,
            f"{' + '.join(centres) or 'Between centres'} open. Entries "
            f"permitted (best window is {s.golden_start}-{s.golden_end} ET).",
            centres, False)

    # --- Asia -------------------------------------------------------------
    return SessionState(
        now_et, now_uk, "asia", False,
        f"Asian hours. The dollar and euro pairs barely move and the spread "
        f"is wider - [BOOK p8] the spread is a cost you pay whether the "
        f"market moves or not. No entries until London opens "
        f"({s.entry_window_start} ET).",
        centres, False)


# ===========================================================================
# Countdowns
# ===========================================================================

def next_week_open(now_et: datetime) -> datetime:
    """The next Sunday 17:00 ET after `now_et`."""
    s = CONFIG.sessions
    h, m = map(int, s.week_open.split(":"))
    candidate = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    while candidate.weekday() != s.week_open_day or candidate <= now_et:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
    return candidate


def _next_entry_time(now_et: datetime) -> datetime:
    """When entries are next permitted, from a moment when they are not."""
    s = CONFIG.sessions
    start = s.golden_start if s.golden_hours_only else s.entry_window_start
    h, m = map(int, start.split(":"))

    probe = now_et
    for _ in range(14 * 24 * 4):          # two weeks of quarter-hours, ample
        candidate = probe.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now_et and current_state(candidate).can_enter:
            return candidate
        probe += timedelta(days=1)
    return now_et + timedelta(days=1)     # unreachable in practice


def next_open_countdown(now: datetime | None = None) -> str:
    """Time until entries are next permitted."""
    s = CONFIG.sessions
    now_et = (now or datetime.now(ET)).astimezone(ET)
    here = current_state(now_et)

    if here.can_enter:
        h, m = map(int, s.entry_window_end.split(":"))
        last = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
        if last <= now_et:
            last += timedelta(days=1)
        mins = max(0, int((last - now_et).total_seconds()) // 60)
        tag = " (golden hours)" if here.is_golden else ""
        return (f"OPEN NOW{tag} - {mins // 60}h {mins % 60}m of today's "
                f"window left")

    target = _next_entry_time(now_et)
    delta = target - now_et
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    uk = target.astimezone(UK)
    return (f"{hours}h {rem // 60}m - opens {target.strftime('%a %H:%M')} ET "
            f"/ {uk.strftime('%H:%M')} UK")


# Kept under the old name so existing callers keep working.
next_macro_countdown = next_open_countdown


def time_until_week_close(now: datetime | None = None) -> str:
    now_et = (now or datetime.now(ET)).astimezone(ET)
    if not market_is_open(now_et):
        return "shut"
    delta = week_close_after(now_et) - now_et
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m"


# ===========================================================================
# Display
# ===========================================================================

def session_summary() -> str:
    """For !session - the whole forex day, both timezones."""
    s = CONFIG.sessions
    rows = [
        ("Sydney",        s.sydney_open,        s.sydney_close),
        ("Tokyo",         s.tokyo_open,         s.tokyo_close),
        ("London",        s.london_open,        s.london_close),
        ("New York",      s.newyork_open,       s.newyork_close),
        ("GOLDEN HOURS",  s.golden_start,       s.golden_end),
        ("ENTRIES",       s.entry_window_start, s.entry_window_end),
    ]

    def to_uk(hhmm: str) -> str:
        h, m = map(int, hhmm.split(":"))
        ref = datetime.now(ET).replace(hour=h, minute=m, second=0,
                                       microsecond=0)
        return ref.astimezone(UK).strftime("%H:%M")

    lines = ["```", f"{'':15}{'ET':>13}   {'UK':>13}"]
    for name, a, b in rows:
        lines.append(f"{name:15}{a + '-' + b:>13}   "
                     f"{to_uk(a) + '-' + to_uk(b):>13}")
    lines.append("")
    lines.append(f"Week opens   Sun {s.week_open} ET / {to_uk(s.week_open)} UK")
    lines.append(f"Week closes  Fri {s.week_close} ET / {to_uk(s.week_close)} UK")
    lines.append(f"Flat by      Fri {s.weekend_flatten} ET / "
                 f"{to_uk(s.weekend_flatten)} UK")
    lines.append("```")
    return "\n".join(lines)
