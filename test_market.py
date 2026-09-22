"""
Tests for the bar-fetch window.

THE BUG THESE EXIST TO PREVENT
------------------------------
get_bars() sent Alpaca a `limit` but no `start` date. `limit` is a maximum,
not a request for history, so without a start date the API returned roughly
the current day only.

A request for 60 four-hour bars came back with 2. The scanner needs 7
before it will do anything, so it returned None on every single scan for two
trading days — with no alert, no error and no log line. Indistinguishable
from a dead bot.

previous_day_range() had it too: it asked for 3 daily bars, got 1, and
returned (None, None), so previous-day highs and lows silently never entered
the liquidity levels.

No network in here. The lookback arithmetic is pure, which is the whole
reason it can be tested at all.
"""

import market
from config import CONFIG


# ---------------------------------------------------------------------------
# Timeframe parsing
# ---------------------------------------------------------------------------

def test_timeframe_minutes():
    cases = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60,
             "4Hour": 240, "1Day": 390}
    for tf, expected in cases.items():
        assert market._timeframe_minutes(tf) == expected, tf
    print("PASS  timeframes parse to the right number of minutes")


def test_timeframe_parsing_is_case_insensitive():
    assert market._timeframe_minutes("4hour") == 240
    assert market._timeframe_minutes("5MIN") == 5
    print("PASS  timeframe parsing ignores case")


# ---------------------------------------------------------------------------
# The lookback window — the actual fix
# ---------------------------------------------------------------------------

def test_four_hour_lookback_covers_the_scanner_requirement():
    """
    The exact case that broke. The scanner needs 2*swing_strength+1 four-hour
    bars; the window must comfortably exceed that.
    """
    needed = 2 * CONFIG.strategy.swing_strength + 1
    days = market._lookback_days("4Hour", 60)

    # ~1.6 four-hour bars per trading day, ~5 trading days per 7 calendar
    bars_expected = days * (5 / 7) * (390 / 240)
    assert bars_expected > needed * 3, (days, bars_expected, needed)
    print(f"PASS  4H window of {days} days yields ~{bars_expected:.0f} bars "
          f"vs the {needed} required")


def test_longer_timeframes_reach_further_back():
    windows = [market._lookback_days(tf, 60)
               for tf in ("1Min", "5Min", "15Min", "1Hour", "4Hour", "1Day")]
    assert windows == sorted(windows), windows
    assert windows[-1] > windows[0] * 5
    print(f"PASS  lookback grows with timeframe: {windows}")


def test_lookback_allows_for_weekends_and_holidays():
    """A window of exactly N trading days lands short once a weekend lands in it."""
    # 1Day bars: 20 bars needs 20 trading days, which is 28 calendar days
    days = market._lookback_days("1Day", 20)
    assert days >= 28, days
    print(f"PASS  20 daily bars asks for {days} calendar days, not 20")


def test_lookback_has_a_floor():
    """Even a tiny request must span a weekend, or Monday returns nothing."""
    assert market._lookback_days("1Min", 1) >= 5
    print("PASS  minimum lookback is at least a long weekend")


def test_lookback_has_a_ceiling():
    """Don't ask for a decade of minute bars by accident."""
    assert market._lookback_days("1Day", 100000) <= 1500
    print("PASS  lookback is capped at a sane maximum")


def _code_only(fn) -> str:
    """
    Source with comments and docstrings stripped.

    Checking raw source is a trap: the comment explaining a fixed bug
    usually quotes the broken code, so a naive search finds the old value
    in the very comment that documents its removal. This test failed that
    way on its first run.
    """
    import inspect
    import io
    import tokenize

    src = inspect.getsource(fn)
    out = []
    prev_type = tokenize.INDENT
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        # A string on its own line is a docstring, not a value.
        if tok.type == tokenize.STRING and prev_type in (
                tokenize.INDENT, tokenize.NEWLINE, tokenize.NL):
            prev_type = tok.type
            continue
        out.append(tok.string)
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_type = tok.type
    # Whitespace-free, so assertions match the code as written rather than
    # as the tokeniser happens to space it.
    return "".join(out).replace(" ", "")


def test_previous_day_needs_more_than_three_bars():
    """
    limit=3 on daily bars returned today only, so prev day was always None.
    The source must now request enough to survive a long weekend.
    """
    code = _code_only(market.previous_day_range)
    assert "limit=3" not in code, "previous_day_range still asks for 3 bars"
    assert "limit=10" in code, code
    print("PASS  previous_day_range requests enough bars to find yesterday")


def test_session_range_pins_itself_to_today():
    """
    session_range means TODAY. If it inherited the widened default window it
    would report a multi-day range as 'the session', putting every liquidity
    level in the wrong place.
    """
    code = _code_only(market.session_range)
    assert "start=today_et" in code, "session_range must pin start to today"
    assert "America/New_York" in code, "session day must be in market time"
    print("PASS  session_range is pinned to today in market time")


def test_get_bars_sends_a_start_date():
    """The one-line root cause: no start parameter in the request."""
    code = _code_only(market.get_bars)
    assert '"start":start' in code, "get_bars must send a start date"
    assert "_lookback_days" in code, "get_bars must compute a default window"
    print("PASS  get_bars always sends a start date")


def test_get_bars_keeps_the_newest_bars():
    """A wider window can over-return; the strategy wants the recent end."""
    code = _code_only(market.get_bars)
    assert "bars[-limit:]" in code, "must trim to the most recent bars"
    print("PASS  over-returned bars are trimmed from the old end, not the new")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
