"""
Tests for backtest.py.

The strategy has its own tests (test_cyfer.py). What these check is the
REPLAY — that given a signal, the backtest does what the live bot would:
fills at the next candle with the spread paid, exits where the broker would
exit, moves the stop at 1R, respects the session clock and every risk gate,
and never shows the strategy a candle that hadn't closed yet.

cyfer.scan is replaced by a stand-in that fires exactly when told to, so
every outcome here is known in advance and checked to the penny.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import backtest as bt
import cyfer
import fx
from config import CONFIG
from strategy import Bar

UTC = timezone.utc
PIP = 0.0001
EURUSD_SPREAD = bt.SPREAD_PIPS["EUR_USD"] * PIP
USD_GBP = fx.rate_to_gbp("USD")[0]

# Tuesday 15 Sep 2026, 09:00 New York (13:00 UTC) — inside the entry window
T0 = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)

# The replay tests below are checked to the penny at 1% risk and a 4% daily
# cap, whatever the live setting is — they test the engine, not the setting.
# The live setting (10%, and the leverage cut it triggers) is tested at the
# bottom with with_risk().
LIVE_RISK = CONFIG.risk
object.__setattr__(CONFIG, "risk", dataclasses.replace(
    LIVE_RISK, risk_per_trade_pct=1.0, max_daily_loss_pct=4.0))


# ---------------------------------------------------------------------------
# Building prices
# ---------------------------------------------------------------------------

def _stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def candles(start, n, minutes, price_at):
    """n candles from `start`, each opening at price_at(k), closing at k+1."""
    out = []
    for k in range(n):
        o, c = price_at(k), price_at(k + 1)
        out.append(Bar(ts=_stamp(start + timedelta(minutes=minutes * k)),
                       open=o, high=max(o, c), low=min(o, c), close=c))
    return out


def history(ltf_price, days_after=1):
    """
    Enough hourly history for the 200-candle window, then 5-minute candles
    from a day before T0. `ltf_price(k)` gives the mid at 5-minute step k,
    where step k=0 is T0 - 1 day.
    """
    h_start = T0 - timedelta(days=20)
    htf = candles(h_start, (20 + days_after) * 24, 60, lambda k: 1.1000)
    l_start = T0 - timedelta(days=1)
    ltf = candles(l_start, (1 + days_after) * 288, 5, ltf_price)
    return {"EUR_USD": {"htf": htf, "ltf": ltf}}


def steps_to(t):
    """The 5-minute step index whose candle CLOSES at t."""
    return int((t - (T0 - timedelta(days=1))).total_seconds() // 300) - 1


def signal(direction, entry, stop_pips, rr=2.0, score=3):
    sgn = 1 if direction == "bullish" else -1
    s = cyfer.Signal(ticker="EUR_USD", direction=direction)
    s.entry = entry
    s.stop = entry - sgn * stop_pips * PIP
    s.target = entry + sgn * stop_pips * PIP * rr
    s.conditions_met = [f"c{i}" for i in range(score)]
    s.conditions_missing = [f"m{i}" for i in range(6 - score)]
    return s


def firing(times=None, direction="bullish", stop_pips=20, rr=2.0,
           every=False):
    """A stand-in for cyfer.scan: fires at the given close times only."""
    calls = []

    def fake(ticker, htf, ltf, ema_direction=None):
        now = bt.parse_ts(ltf[-1].ts) + timedelta(minutes=5)
        calls.append((now, htf, ltf))
        if every or (times and now in times):
            return signal(direction, ltf[-1].close, stop_pips, rr)
        return None
    return fake, calls


def run_with(fake, hist, start=T0, **cfg):
    original = CONFIG.strategy
    if cfg:
        object.__setattr__(CONFIG, "strategy",
                           dataclasses.replace(original, **cfg))
    try:
        with patch("cyfer.scan", fake):
            return bt.run(hist, start=start)
    finally:
        object.__setattr__(CONFIG, "strategy", original)


def units_for(stop_pips):
    """What risk.size_trade gives for a £10 risk on that stop."""
    import risk
    return risk.size_trade(1.1, 1.1 - stop_pips * PIP, "EUR_USD").units


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_reads_oandas_nanosecond_timestamps():
    t = bt.parse_ts("2026-09-15T13:05:00.000000000Z")
    assert t == datetime(2026, 9, 15, 13, 5, tzinfo=UTC)
    assert bt.parse_ts("2026-09-15T13:05:00Z") == t
    print("PASS  reads OANDA's nanosecond timestamps")


# ---------------------------------------------------------------------------
# One trade, three endings — each checked to the penny
# ---------------------------------------------------------------------------

def test_a_winner_pays_the_target_less_the_spread():
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000 if k <= k0 + 1 else
                   1.1000 + (k - k0 - 1) * PIP)      # 1 pip a candle, up
    fake, _ = firing({T0})
    res = run_with(fake, hist)
    assert len(res.trades) == 1, len(res.trades)
    tr = res.trades[0]
    assert tr.reason == "target", tr.reason
    assert abs(tr.entry - (1.1000 + EURUSD_SPREAD / 2)) < 1e-9   # paid the ask
    expected = (tr.target - tr.entry) * units_for(20) * USD_GBP
    assert abs(tr.pnl_gbp - expected) < 0.01, (tr.pnl_gbp, expected)
    assert 19.0 < tr.pnl_gbp < 20.5, tr.pnl_gbp
    print(f"PASS  a winner: filled at the ask, target hit, "
          f"+£{tr.pnl_gbp:.2f} (2R less the spread)")


def test_a_loser_costs_the_risk_plus_the_spread():
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000 if k <= k0 + 1 else
                   1.1000 - (k - k0 - 1) * PIP)      # 1 pip a candle, down
    fake, _ = firing({T0})
    res = run_with(fake, hist)
    tr = res.trades[0]
    assert tr.reason == "stop", tr.reason
    assert abs(tr.exit - tr.stop) < 1e-9
    assert -10.6 < tr.pnl_gbp < -9.9, tr.pnl_gbp
    print(f"PASS  a loser: stopped at the stop, £{tr.pnl_gbp:.2f} "
          f"(1R plus the spread)")


def test_breakeven_turns_a_loser_into_a_scratch():
    """[BOOK p47] Up 1R, stop to entry — then the reversal costs nothing."""
    k0 = steps_to(T0)

    def price(k):
        j = k - k0 - 1
        if j <= 0:
            return 1.1000
        return 1.1000 + (j if j <= 25 else 50 - j) * PIP   # up 25, then down

    fake, _ = firing({T0})
    res = run_with(fake, history(price))
    tr = res.trades[0]
    assert tr.moved_to_breakeven
    assert tr.reason == "breakeven", tr.reason
    assert abs(tr.pnl_gbp) < 0.01, tr.pnl_gbp
    print("PASS  up 1R then reversed: stop moved to entry, closed at £0.00")


def test_a_short_mirrors_it():
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000 if k <= k0 + 1 else
                   1.1000 - (k - k0 - 1) * PIP)
    fake, _ = firing({T0}, direction="bearish")
    res = run_with(fake, hist)
    tr = res.trades[0]
    assert not tr.long and tr.reason == "target", (tr.long, tr.reason)
    assert abs(tr.entry - (1.1000 - EURUSD_SPREAD / 2)) < 1e-9   # sold the bid
    assert tr.pnl_gbp > 19.0, tr.pnl_gbp
    print(f"PASS  a short: sold at the bid, bought back at target, "
          f"+£{tr.pnl_gbp:.2f}")


# ---------------------------------------------------------------------------
# Exits candle by candle
# ---------------------------------------------------------------------------

def _tr(long=True):
    return bt.Trade(pair="EUR_USD", long=long, decided=T0, opened=T0,
                    entry=1.1000, stop=1.0980 if long else 1.1020,
                    target=1.1040 if long else 1.0960,
                    first_stop=1.0980 if long else 1.1020, units=1000,
                    risk_gbp=10, score=3, total=6, rate=1.0)


def test_a_candle_touching_both_levels_counts_as_a_stop():
    """A candle doesn't say which came first. Assume the worse."""
    both = Bar(ts="x", open=1.1000, high=1.1050, low=1.0970, close=1.1000)
    assert bt._exit_on(_tr(), both, 0.0) == (1.0980, "stop")
    print("PASS  a candle that hits both levels is read as a stop")


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    gap = Bar(ts="x", open=1.0970, high=1.0975, low=1.0965, close=1.0970)
    price, why = bt._exit_on(_tr(), gap, 0.0)
    assert why == "stop" and price == 1.0970, (price, why)
    print("PASS  a gap through the stop fills at the gap, not the stop")


def test_the_spread_decides_whether_a_level_was_touched():
    """A long sells at the bid. A mid that just reaches the target hasn't."""
    just = Bar(ts="x", open=1.1030, high=1.1040, low=1.1030, close=1.1035)
    assert bt._exit_on(_tr(), just, 0.0) == (1.1040, "target")
    assert bt._exit_on(_tr(), just, EURUSD_SPREAD) is None
    print("PASS  with the spread, a mid touching the target isn't a fill")


# ---------------------------------------------------------------------------
# It can't see the future
# ---------------------------------------------------------------------------

def test_the_strategy_never_sees_an_unclosed_candle():
    """
    The single most important property of a backtest. Every candle handed
    to the strategy must have CLOSED by the moment it's deciding — the
    live bot drops OANDA's still-forming candle, and so must this.
    """
    fake, calls = firing()
    run_with(fake, history(lambda k: 1.1000))
    assert calls, "the strategy was never consulted"
    for now, htf, ltf in calls:
        assert bt.parse_ts(ltf[-1].ts) + timedelta(minutes=5) == now
        assert bt.parse_ts(htf[-1].ts) + timedelta(minutes=60) <= now
        assert len(htf) == bt.HTF_WINDOW and len(ltf) == bt.LTF_WINDOW
    print(f"PASS  {len(calls):,} decisions checked — never a candle from "
          f"the future, always the live bot's 200/60 windows")


def test_fills_at_the_next_candle_not_the_signal_candle():
    """
    The signal candle closes at 1.1000; the next one GAPS open at 1.1005.
    A backtest that filled at the signal close would get a price nobody
    could actually have traded at. It must pay the gap.
    """
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000)
    nxt = hist["EUR_USD"]["ltf"][k0 + 1]
    hist["EUR_USD"]["ltf"][k0 + 1] = Bar(ts=nxt.ts, open=1.1005, high=1.1005,
                                         low=1.1005, close=1.1005)
    fake, _ = firing({T0})
    res = run_with(fake, hist)
    tr = res.trades[0]
    assert tr.decided == T0
    assert tr.opened == T0                     # the next candle opens at T0
    assert abs(tr.entry - (1.1005 + EURUSD_SPREAD / 2)) < 1e-9, tr.entry
    print("PASS  a gap on the next candle is paid — it fills at the next "
          "open, never the close it decided on")


# ---------------------------------------------------------------------------
# The live bot's gates
# ---------------------------------------------------------------------------

def test_only_scans_inside_the_entry_window():
    import sessions
    fake, calls = firing()
    start = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    run_with(fake, history(lambda k: 1.1000), start=start)
    assert calls
    for now, _, _ in calls:
        assert sessions.current_state(now).can_enter, now
    print(f"PASS  all {len(calls):,} scans fell inside the entry window")


def test_three_losses_in_a_row_stops_it_for_the_day():
    """The same lockout as the live bot, in the same order."""
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000 if k <= k0 + 1 else
                   1.1000 - (k - k0 - 1) * PIP)      # falls all day
    fake, _ = firing(every=True, stop_pips=10)
    res = run_with(fake, hist)
    same_day = [t for t in res.trades if t.decided.date() == T0.date()]
    assert len(same_day) == CONFIG.risk.max_consecutive_losses, len(same_day)
    assert all(t.reason == "stop" for t in same_day)
    print(f"PASS  a falling day: {len(same_day)} losses, then it stopped")


def test_one_position_per_pair_and_one_trade_per_hour():
    fake, _ = firing(every=True)
    flat = history(lambda k: 1.1000)               # never hits either level
    res = run_with(fake, flat)
    today = [t for t in res.trades if t.decided.date() == T0.date()]
    assert len(today) == 1, len(today)
    assert res.skipped_holding > 0

    res = run_with(fake, flat, one_position_per_pair=False)
    today = [t for t in res.trades if t.decided.date() == T0.date()]
    hours = {t.decided.strftime("%H") for t in today}
    assert len(hours) == len(today), "two trades in one hour"
    assert len(today) == CONFIG.risk.max_trades_per_day, len(today)
    print(f"PASS  one open per pair (1 trade); with stacking allowed, "
          f"one per hour up to the {CONFIG.risk.max_trades_per_day}/day cap")


def test_a_weak_alert_no_longer_uses_up_the_hour():
    """
    The live bug this backtest found. A 2/6 alert at 09:00 must not stop a
    tradeable 3/6 at 09:20 in the same hour from trading.
    """
    t1, t2 = T0, T0 + timedelta(minutes=20)

    def fake(ticker, htf, ltf, ema_direction=None):
        now = bt.parse_ts(ltf[-1].ts) + timedelta(minutes=5)
        if now == t1:
            return signal("bullish", ltf[-1].close, 20, score=2)
        if now == t2:
            return signal("bullish", ltf[-1].close, 20, score=3)
        return None

    res = run_with(fake, history(lambda k: 1.1000))
    assert len(res.trades) == 1, len(res.trades)
    assert res.trades[0].decided == t2
    print("PASS  a 2/6 alert early in the hour doesn't block a 3/6 later")


def test_flat_before_the_weekend():
    fri = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)        # Fri 09:00 ET
    global T0
    saved, T0 = T0, fri
    try:
        fake, _ = firing({fri})
        res = run_with(fake, history(lambda k: 1.1000, days_after=1),
                       start=fri)
    finally:
        T0 = saved
    tr = res.trades[0]
    assert tr.reason == "weekend", tr.reason
    et = tr.closed.astimezone(__import__("sessions").ET)
    assert et.weekday() == 4 and et.strftime("%H:%M") >= "16:30"
    print(f"PASS  an open trade is closed Friday {et:%H:%M} New York, "
          f"before the weekend gap")


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------

def _closed(pnl, reason, when):
    t = _tr()
    t.closed, t.reason = when, reason
    t.exit = t.entry + pnl / (t.units * t.rate)
    return t


def test_summary_counts_and_the_break_even_line():
    day = T0
    trades = ([_closed(20, "target", day + timedelta(hours=i)) for i in range(4)]
              + [_closed(-10, "stop", day + timedelta(hours=10 + i)) for i in range(6)])
    res = bt.Result(trades=trades, start=day, end=day + timedelta(days=7),
                    pairs=["EUR_USD"])
    s = bt.summarise(res)
    assert s["trades"] == 10 and s["wins"] == 4 and s["losses"] == 6
    assert abs(s["pnl"] - 20.0) < 1e-6, s["pnl"]
    assert abs(s["win_rate"] - 0.4) < 1e-9
    assert abs(s["breakeven_rate"] - 1 / 3) < 1e-6
    assert s["win_lo"] < 0.4 < s["win_hi"]
    assert s["worst_streak"] == 6
    print(f"PASS  4W/6L: +£20, 40% win rate (likely {s['win_lo']:.0%}–"
          f"{s['win_hi']:.0%}), needs 33% to break even")


def test_the_verdict_refuses_to_judge_a_small_sample():
    res = bt.Result(trades=[_closed(20, "target", T0)], start=T0,
                    end=T0 + timedelta(days=7), pairs=["EUR_USD"])
    assert "Too few trades" in bt.verdict(bt.summarise(res))
    empty = bt.Result(trades=[], start=T0, end=T0 + timedelta(days=7),
                      pairs=["EUR_USD"])
    assert "never traded" in bt.verdict(bt.summarise(empty))
    print("PASS  a handful of trades is called too few, not a result")


def test_the_report_reads_cleanly():
    k0 = steps_to(T0)
    hist = history(lambda k: 1.1000 if k <= k0 + 1 else
                   1.1000 + (k - k0 - 1) * PIP)
    fake, _ = firing({T0})
    text = bt.report(run_with(fake, hist), 12)
    for bit in ("Backtest", "Trades:", "Result:", "By pair",
                "How they ended", "Against £1,000 a week", "not a forecast"):
        assert bit in text, bit
    assert "<" not in text, "no HTML — it shows as junk in Bob's app"
    print("PASS  the report has every section and no HTML")


# ---------------------------------------------------------------------------
# The honesty check
# ---------------------------------------------------------------------------

def test_no_edge_on_pure_coin_flip_prices():
    """
    The test that matters most. On a random walk with no trend at all, no
    strategy can have an edge, so after the spread the average trade MUST
    come out negative. A backtest that finds profit in pure randomness is
    peeking at the future somewhere — and would lie about real results too.

    Runs the REAL strategy, not a stand-in. Fixed seed, so it's repeatable.
    Measured at -0.13R a trade when written; the bar here is deliberately
    loose (below +0.15R) so a future strategy change can't trip it by
    chance, while a genuine look-ahead leak — which shows up as a large
    edge — still would.
    """
    import random
    import statistics
    import sessions

    rng = random.Random(1000)
    specs = {"EUR_USD": (1.15, 0.00025), "GBP_USD": (1.34, 0.00030),
             "USD_JPY": (150.0, 0.030), "AUD_USD": (0.66, 0.00020)}
    end = datetime(2026, 9, 19, tzinfo=UTC)
    start = end - timedelta(weeks=8)

    def walk(p0, vol):
        m5, t, p = [], start - timedelta(days=16), p0
        while t < end:
            if sessions.market_is_open(t.astimezone(sessions.ET)):
                o = p
                p = p + rng.gauss(0, vol)
                m5.append(Bar(ts=_stamp(t), open=o,
                              high=max(o, p) + abs(rng.gauss(0, vol / 2)),
                              low=min(o, p) - abs(rng.gauss(0, vol / 2)),
                              close=p))
            t += timedelta(minutes=5)
        buckets = {}
        for b in m5:
            buckets.setdefault(bt.parse_ts(b.ts).replace(minute=0), []).append(b)
        h1 = [Bar(ts=_stamp(k), open=v[0].open, high=max(x.high for x in v),
                  low=min(x.low for x in v), close=v[-1].close)
              for k, v in sorted(buckets.items())]
        return {"htf": h1, "ltf": m5}

    res = bt.run({s: walk(*v) for s, v in specs.items()}, start=start)
    r = [t.pnl_gbp / t.risk_gbp for t in res.closed if t.risk_gbp]
    assert len(r) > 50, len(r)
    mean = statistics.mean(r)
    assert mean < 0.15, f"found an edge in random prices: {mean:+.3f}R"
    print(f"PASS  {len(r)} trades on coin-flip prices average {mean:+.3f}R "
          f"— no edge in randomness, so no peeking")


# ---------------------------------------------------------------------------
# The 10% setting and the UK leverage limit
# ---------------------------------------------------------------------------

class with_risk:
    """Temporarily swap the risk settings."""
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.saved = CONFIG.risk
        object.__setattr__(CONFIG, "risk",
                           dataclasses.replace(self.saved, **self.kw))

    def __exit__(self, *exc):
        object.__setattr__(CONFIG, "risk", self.saved)


def test_live_setting_is_ten_percent_with_a_two_loss_day():
    assert LIVE_RISK.risk_per_trade_pct == 10.0, LIVE_RISK.risk_per_trade_pct
    assert LIVE_RISK.max_daily_loss_pct == 20.0, LIVE_RISK.max_daily_loss_pct
    print("PASS  live risk is 10% a trade, day stops after two full losses")


def test_one_percent_is_never_cut():
    import risk
    t = risk.size_trade(1.1, 1.1 - 20 * PIP, "EUR_USD")
    assert not t.capped and t.leverage < 10, (t.capped, t.leverage)
    assert 9.9 < t.risk_gbp < 10.1, t.risk_gbp
    print(f"PASS  1% on a 20-pip stop: £{t.risk_gbp:.2f}, "
          f"{t.leverage:.1f}x, not cut")


def test_ten_percent_is_cut_to_the_uk_limit_not_skipped():
    import risk
    with with_risk(risk_per_trade_pct=10.0, max_daily_loss_pct=20.0):
        t = risk.size_trade(1.1, 1.1 - 20 * PIP, "EUR_USD")
    # £100 on 20 pips would be 55x; 30:1 with 90% headroom is 27x
    assert t.capped and t.units > 0, (t.capped, t.units)
    assert 26.9 < t.leverage <= 27.0, t.leverage
    assert 48 < t.risk_gbp < 50, t.risk_gbp
    assert any(w.startswith("Cut from") for w in t.warnings), t.warnings
    print(f"PASS  10% on a 20-pip stop is cut to {t.leverage:.1f}x, "
          f"risking £{t.risk_gbp:.2f} instead of being skipped")


def test_aud_usd_gets_the_lower_twenty_to_one_limit():
    import risk, pairs
    assert risk.max_leverage(pairs.parse("AUD_USD")) == 20.0
    assert risk.max_leverage(pairs.parse("EUR_USD")) == 30.0
    assert risk.max_leverage(pairs.parse("USD_JPY")) == 30.0
    with with_risk(risk_per_trade_pct=10.0):
        t = risk.size_trade(0.66, 0.66 - 20 * PIP, "AUD_USD")
    assert t.capped and t.leverage <= 18.0 + 1e-9, t.leverage
    print(f"PASS  AUD/USD capped at {t.leverage:.1f}x (20:1 less headroom)")


def test_a_second_trade_only_gets_the_margin_that_is_left():
    import risk
    with with_risk(risk_per_trade_pct=10.0):
        some = risk.size_trade(1.1, 1.1 - 20 * PIP, "EUR_USD",
                               margin_available_gbp=100.0)
        none = risk.size_trade(1.1, 1.1 - 20 * PIP, "EUR_USD",
                               margin_available_gbp=0.0)
    assert some.capped and some.exposure_gbp <= 100 * 27 + 1, some.exposure_gbp
    assert none.units == 0 and any("No free margin" in w
                                   for w in none.warnings), none.warnings
    print(f"PASS  £100 free margin → £{some.exposure_gbp:,.0f} position; "
          f"none free → no trade")


def test_backtest_at_ten_percent_reports_the_real_risk():
    times = {T0 + timedelta(minutes=5)}
    fake, _ = firing(times, stop_pips=20)
    start = steps_to(T0 + timedelta(minutes=5))
    price = lambda k: 1.1 + max(0, k - start) * 2 * PIP   # climbs to target
    with with_risk(risk_per_trade_pct=10.0, max_daily_loss_pct=20.0):
        res = run_with(fake, history(price))
        text = bt.report(res, 1)
    assert len(res.closed) == 1 and res.capped == 1, (len(res.closed), res.capped)
    tr = res.closed[0]
    assert 45 < tr.risk_gbp < 50, tr.risk_gbp
    assert "cut down to fit the UK leverage limit" in text, text
    assert "Real risk per trade" in text and "Lowest the account went" in text
    assert "<" not in text
    print(f"PASS  at 10% the replay risks £{tr.risk_gbp:.2f} (cut), "
          f"made £{tr.pnl_gbp:+.2f}, and the report says so")


# ---------------------------------------------------------------------------
# Downloading the history when OANDA struggles
# ---------------------------------------------------------------------------

def _hourly_payload(a, b):
    out, t = [], a
    while t < b:
        out.append({"time": _stamp(t), "complete": True,
                    "mid": {"o": "1.1", "h": "1.1", "l": "1.1", "c": "1.1"},
                    "volume": 1})
        t += timedelta(hours=1)
    return {"candles": out}


def test_a_window_oanda_chokes_on_is_split_and_still_arrives_whole():
    import oanda
    asked = []

    def fake_get(path, params=None, timeout=20, retries=0):
        a, b = bt.parse_ts(params["from"]), bt.parse_ts(params["to"])
        asked.append(b - a)
        if b - a > timedelta(days=10):
            raise oanda.OandaServerError("OANDA error 504: OANDA's server "
                                         "timed out.")
        return _hourly_payload(a, b)

    a = datetime(2026, 8, 1, tzinfo=UTC)
    b = a + timedelta(days=30)
    with patch("oanda._get", fake_get):
        bars = bt._windowed("EUR_USD", "H1", a, b, timedelta(days=30))
    assert len(bars) == 30 * 24, len(bars)
    assert len({x.ts for x in bars}) == len(bars)
    assert max(asked) == timedelta(days=30) and min(asked) <= timedelta(days=7.5)
    print(f"PASS  a 30-day window OANDA refused was split into smaller ones "
          f"— all {len(bars)} hourly candles arrived, none twice")


def test_a_bad_token_stops_the_download_without_splitting():
    import oanda
    asked = []

    def fake_get(path, params=None, timeout=20, retries=0):
        asked.append(1)
        raise oanda.OandaError("OANDA rejected the token.")

    a = datetime(2026, 8, 1, tzinfo=UTC)
    with patch("oanda._get", fake_get):
        try:
            bt._windowed("EUR_USD", "H1", a, a + timedelta(days=30),
                         timedelta(days=30))
        except oanda.OandaError as e:
            assert "token" in str(e)
        else:
            raise AssertionError("no error")
    assert len(asked) == 1, len(asked)
    print("PASS  a bad token fails once instead of being split and retried")


def test_a_failed_download_prints_a_plain_message():
    import io
    import contextlib
    import oanda

    def broken(weeks, log=print):
        raise oanda.OandaServerError(
            "OANDA error 504: " + oanda._explain(type("R", (), {
                "status_code": 504, "text": "<!DOCTYPE html><html></html>",
                "json": lambda self: {}})()))

    err = io.StringIO()
    with patch("backtest.fetch", broken), contextlib.redirect_stderr(err):
        code = bt.main(["backtest.py", "12"])
    text = err.getvalue()
    assert code == 1 and "<" not in text and "OANDA's side" in text, text
    print("PASS  a 504 comes out as a sentence, not an HTML page")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
