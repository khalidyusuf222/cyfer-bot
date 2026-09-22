"""
Tests for the forex session clock.

The clock is the one part of the system that cannot be wrong on a
judgement call — it either knows when the market is open or it does not.
Everything downstream trusts it, so the edges are what get tested here:
the Sunday open, the Friday close, and midnight-crossing sessions.

Dates used below are real. 2026-09-13 is a Sunday, 2026-09-18 a Friday.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sessions
from config import CONFIG

ET = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


SUNDAY = (2026, 9, 13)
MONDAY = (2026, 9, 14)
FRIDAY = (2026, 9, 18)
SATURDAY = (2026, 9, 19)


# ---------------------------------------------------------------------------
# The week
# ---------------------------------------------------------------------------

def test_the_week_opens_sunday_evening_not_monday_morning():
    """
    Forex opens Sunday 17:00 New York. A weekday test would have called
    Sunday evening 'closed' and thrown away a fifth of the week.
    """
    assert not sessions.market_is_open(et(*SUNDAY, 16, 59))
    assert sessions.market_is_open(et(*SUNDAY, 17, 0))
    assert sessions.market_is_open(et(*SUNDAY, 23, 0))
    print("PASS  the week opens Sunday 17:00 ET, not Monday")


def test_the_week_closes_friday_evening():
    assert sessions.market_is_open(et(*FRIDAY, 16, 59))
    assert not sessions.market_is_open(et(*FRIDAY, 17, 0))
    assert not sessions.market_is_open(et(*SATURDAY, 12, 0))
    print("PASS  the week closes Friday 17:00 ET and stays shut Saturday")


def test_midweek_nights_are_open():
    """
    The share bot closed every night. This one must not: a Tuesday 3am
    position is perfectly normal and the market never stopped.
    """
    for hour in (0, 3, 6, 22):
        assert sessions.market_is_open(et(*MONDAY, hour)), hour
    print("PASS  the market stays open through midweek nights")


def test_weekend_state_says_when_it_reopens():
    s = sessions.current_state(et(*SATURDAY, 12))
    assert s.phase == "weekend" and not s.can_enter
    assert "Sunday" in s.reason
    print("PASS  the weekend state says when it reopens")


# ---------------------------------------------------------------------------
# The four centres  [BOOK p3]
# ---------------------------------------------------------------------------

def test_sessions_that_cross_midnight():
    """
    Sydney runs 17:00-02:00 and Tokyo 19:00-04:00 New York time. A naive
    start <= t < end test reports both as closed for their whole length.
    """
    assert "Sydney" in sessions.open_centres(et(*MONDAY, 1))
    assert "Tokyo" in sessions.open_centres(et(*MONDAY, 1))
    assert "Sydney" not in sessions.open_centres(et(*MONDAY, 10))
    print("PASS  Sydney and Tokyo are read correctly across midnight")


def test_golden_hours_are_london_and_new_york_together():
    centres = sessions.open_centres(et(*MONDAY, 9))
    assert "London" in centres and "New York" in centres
    s = sessions.current_state(et(*MONDAY, 9))
    assert s.is_golden and s.phase == "golden" and s.can_enter
    print(f"PASS  09:00 ET is the overlap: {s.centres_str}")


def test_asia_is_watched_but_not_traded():
    s = sessions.current_state(et(*MONDAY, 1))
    assert not s.can_enter and s.phase == "asia"
    assert "spread" in s.reason
    print("PASS  Asian hours are watched, not traded")


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def test_sunday_warmup_blocks_the_first_stretch():
    opened = et(*SUNDAY, 17, 0)
    warm = opened + timedelta(minutes=CONFIG.sessions.sunday_warmup_minutes - 1)
    after = opened + timedelta(minutes=CONFIG.sessions.sunday_warmup_minutes + 1)

    assert sessions.current_state(warm).phase == "warmup"
    assert not sessions.current_state(warm).can_enter
    assert sessions.current_state(after).phase != "warmup"
    print(f"PASS  no entries for the first "
          f"{CONFIG.sessions.sunday_warmup_minutes} minutes of the week")


def test_friday_runout_refuses_new_positions():
    s = sessions.current_state(et(*FRIDAY, 16, 15))
    assert not s.can_enter and s.phase == "runout"
    assert "weekend" in s.reason
    print("PASS  no new positions into the weekend")


def test_friday_still_trades_during_the_day():
    """The run-out is the last hour, not the whole day."""
    s = sessions.current_state(et(*FRIDAY, 9, 0))
    assert s.can_enter, s.reason
    print("PASS  Friday still trades up to the run-out")


def test_entries_span_london_through_new_york():
    open_hours = [h for h in range(24)
                  if sessions.current_state(et(*MONDAY, h)).can_enter]
    assert min(open_hours) == 3, open_hours
    assert max(open_hours) == 15, open_hours
    assert len(open_hours) == 13, open_hours
    print(f"PASS  entries run {min(open_hours):02d}:00-{max(open_hours) + 1:02d}"
          f":00 ET — {len(open_hours)} hours a day")


def test_golden_hours_only_narrows_it_to_four():
    """
    The tightening switch has to actually tighten. Four hours is what the
    book calls best; it is off by default because four hours a day is too
    few looks to learn anything from.
    """
    import dataclasses
    original = CONFIG.sessions
    object.__setattr__(CONFIG, "sessions",
                       dataclasses.replace(original, golden_hours_only=True))
    try:
        hours = [h for h in range(24)
                 if sessions.current_state(et(*MONDAY, h)).can_enter]
        assert hours == [8, 9, 10, 11], hours
    finally:
        object.__setattr__(CONFIG, "sessions", original)
    print("PASS  golden_hours_only narrows entries to the 4-hour overlap")


# ---------------------------------------------------------------------------
# Countdowns
# ---------------------------------------------------------------------------

def test_countdown_from_the_weekend_lands_on_a_tradeable_moment():
    """
    The countdown must not point at a time entries are still refused. From
    Saturday, 'next open' is Monday 03:00 — not Sunday 17:00, when the
    market reopens but the warm-up is still running.
    """
    target = sessions._next_entry_time(et(*SATURDAY, 12))
    assert sessions.current_state(target).can_enter, target
    assert target.weekday() == 0, target      # Monday
    assert target.hour == 3, target
    print(f"PASS  next entry from Saturday is "
          f"{target.strftime('%a %H:%M')} ET, and it is genuinely open")


def test_countdown_while_open_reports_time_left():
    text = sessions.next_open_countdown(et(*MONDAY, 9, 30))
    assert "OPEN NOW" in text and "golden" in text
    print(f"PASS  open countdown: {text}")


def test_week_open_and_close_helpers_agree_with_market_is_open():
    now = et(*MONDAY, 10)
    assert sessions.week_open_before(now) == et(*SUNDAY, 17)
    assert sessions.week_close_after(now) == et(*FRIDAY, 17)
    print("PASS  week open/close helpers land on the right edges")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def test_summary_shows_both_timezones_and_the_golden_hours():
    text = sessions.session_summary()
    for marker in ("Sydney", "Tokyo", "London", "New York",
                   "GOLDEN HOURS", "ENTRIES", "Week opens", "Flat by"):
        assert marker in text, marker
    print("PASS  the session table covers all four centres and the week")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
