"""
End-to-end dry run, no network.

Builds a synthetic EUR/USD chart that the strategy should read as a valid
setup, then walks it through every stage the live bot would: scan, size,
preflight, record, reconcile, report. Nothing is sent anywhere.

The point is not that the strategy is good. It is that the pipeline is
wired together and the arithmetic comes out the same at every stage.

    python3 dryrun_forex.py
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("BROKER", "oanda")
os.environ.setdefault("OANDA_TOKEN", "dryrun")
os.environ.setdefault("OANDA_ACCOUNT", "101-004-0000000-001")

import ai
import ai_log
import cyfer
import execution
import fx
import graystone
import pairs
import reconcile
import report
import risk
import sessions
import tracker
from config import CONFIG
from strategy import Bar

PAIR = "EUR_USD"
p = pairs.parse(PAIR)


def bar(t, o, h, l, c):
    return Bar(ts=t.isoformat(), open=o, high=h, low=l, close=c, volume=1000)


def leg(bars, t, start, end, n=6):
    """
    Walk price from `start` to `end` over n bars, so find_swings can see a
    pivot at each turn. Swing detection needs 3 clear bars either side, so
    the legs have to be long enough to give it them.
    """
    step = (end - start) / n
    price = start
    for i in range(n):
        o = price
        price += step
        hi = max(o, price) + 0.0004
        lo = min(o, price) - 0.0004
        bars.append(bar(t, o, hi, lo, price))
        t += timedelta(hours=1)
    return t


def build_uptrend_into_support():
    """
    An uptrend stepping up off a support zone it has already rejected
    three times, pulling back into that zone on a bullish engulfing candle.

    This is the textbook shape, built deliberately. It is NOT evidence the
    strategy works — it is a chart constructed to satisfy the conditions,
    so that the machinery downstream gets exercised.
    """
    t = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)
    bars = []

    # Padding, so the first pivot has bars on its left.
    t = leg(bars, t, 1.1060, 1.1040, 5)

    # Three rejections of the same zone, each followed by a higher high.
    # The lows rise slightly — that is what makes it a trend rather than a
    # flat range — but they stay inside the clustering tolerance, so they
    # read as one level.
    t = leg(bars, t, 1.1040, 1.0995)      # low 1
    t = leg(bars, t, 1.0995, 1.1090)      # high 1
    t = leg(bars, t, 1.1090, 1.1005)      # low 2  (higher)
    t = leg(bars, t, 1.1005, 1.1110)      # high 2 (higher)
    t = leg(bars, t, 1.1110, 1.1015)      # low 3  (higher)
    t = leg(bars, t, 1.1015, 1.1130)      # high 3 (higher)

    # The current pullback, still in progress — deliberately not long
    # enough to confirm a fourth swing low, because in real time it
    # wouldn't be.
    t = leg(bars, t, 1.1130, 1.1004, 5)

    return bars, t


def build_trigger_candle(last_close):
    """
    The 5-minute chart under that pullback, ending on a bullish engulfing
    candle: a red bar, then a green one whose body swallows it whole.
    """
    t = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    bars, price = [], last_close + 0.0016

    for i in range(38):
        o = price
        price -= 0.00004
        bars.append(bar(t, o, o + 0.0002, price - 0.0002, price))
        t += timedelta(minutes=5)

    # The setup candle: down, then a bigger up that engulfs it.
    red_open, red_close = price, price - 0.0006
    bars.append(bar(t, red_open, red_open + 0.0001,
                    red_close - 0.0002, red_close))
    t += timedelta(minutes=5)
    bars.append(bar(t, red_close - 0.0001, red_open + 0.0009,
                    red_close - 0.0003, red_open + 0.0008))

    return bars


def main():
    print("=" * 70)
    print("DRY RUN — forex pipeline, no network, nothing sent")
    print("=" * 70)

    # --- 1. the clock ----------------------------------------------------
    when = datetime(2026, 9, 14, 9, 30,
                    tzinfo=sessions.ET)          # Monday, golden hours
    state = sessions.current_state(when)
    print(f"\n1. CLOCK   {state.et_str} ET / {state.uk_str} UK")
    print(f"           open: {state.centres_str}"
          f"{'  (golden hours)' if state.is_golden else ''}")
    print(f"           can enter: {state.can_enter} — {state.reason}")
    assert state.can_enter

    # --- 2. the scan -----------------------------------------------------
    bars_htf, t = build_uptrend_into_support()
    bars_ltf = build_trigger_candle(bars_htf[-1].close)
    ema_dir = graystone.ema_stack_direction(bars_htf)
    sig = cyfer.scan(PAIR, bars_htf, bars_ltf, ema_direction=ema_dir)

    print(f"\n2. SCAN    {len(bars_htf)} HTF bars, {len(bars_ltf)} LTF bars")
    if sig is None:
        print("           no setup — the pipeline below is untested on this "
              "chart, which is a fair outcome for synthetic data")
        return
    print(f"           {sig.direction} {sig.score}/{sig.total}")
    print(f"           met:     {', '.join(sig.conditions_met) or '-'}")
    print(f"           missing: {', '.join(sig.conditions_missing) or '-'}")
    print(f"           entry {sig.entry:.5f}  stop {sig.stop:.5f}  "
          f"target {sig.target:.5f}")

    if sig.entry <= 0:
        print("           nothing priced — either no level to stop against, "
              "or the feed guard rejected the price. Stopping here.")
        return

    # --- 3. sizing -------------------------------------------------------
    sized = risk.size_trade(sig.entry, sig.stop, PAIR,
                            target=sig.target)
    print(f"\n3. SIZE    {sized.units:,} units ({sized.direction})")
    print(f"           stop {sized.stop_pips:.1f} pips · "
          f"risk £{sized.risk_gbp:.2f} · reward £{sized.reward_gbp:.2f}")
    print(f"           controlling £{sized.exposure_gbp:,.0f} "
          f"({sized.leverage:.1f}x account)")
    budget = CONFIG.display.account_gbp * CONFIG.risk.risk_per_trade_pct / 100
    print(f"           budget was £{budget:.2f} — "
          f"{'within' if sized.risk_gbp <= budget else 'OVER'}")
    assert sized.risk_gbp <= budget + 0.01, "sizing breached the risk budget"

    # --- 4. preflight ----------------------------------------------------
    conn = tracker.connect(Path(tempfile.mkdtemp()) / "dryrun.db")
    risk.day_state(conn)
    side = "buy" if sig.direction == "bullish" else "sell"
    problems = execution.preflight(conn, state, sig.entry, sig.stop,
                                   sized.units, sig.target,
                                   symbol=PAIR, side=side)
    print(f"\n4. GATES   {len(problems)} objection(s)")
    for prob in problems:
        print(f"           • {prob}")
    if problems:
        print("           refused — which is the gate working")
        return

    # --- 4b. the AI reviewer ---------------------------------------------
    #
    # Exercised with a fake transport, so this shows the wiring without a
    # network call or an API key. Note where it sits: AFTER every gate
    # above has already passed. It cannot open a trade, only stop one.
    import json as _json
    import os as _os
    from unittest.mock import patch as _patch
    import requests as _requests

    _os.environ["AI_ENABLED"] = "on"
    _os.environ["GROQ_API_KEY"] = "dryrun"

    class _Reply:
        def __init__(self, content):
            self.status_code, self.ok = 200, True
            self._c = content

        def json(self):
            return {"choices": [{"message": {"content": self._c}}]}

    def _fake(verdict):
        return lambda *a, **k: _Reply(_json.dumps(verdict))

    print("\n4b. AI      (fake model — no network, no key)")

    scenarios = [
        ("agrees", {"action": "BUY", "confidence": 0.85,
                    "rationale": "Trend, level and trigger all line up."}),
        ("objects, confident", {"action": "HOLD", "confidence": 0.9,
                                "rationale": "Spread is a third of the stop."}),
        ("objects, unsure", {"action": "HOLD", "confidence": 0.3,
                             "rationale": "Bit uncertain here."}),
        ("wants the other side", {"action": "SELL", "confidence": 0.95,
                                  "rationale": "Trend looks exhausted."}),
    ]
    for label, verdict in scenarios:
        with _patch.object(_requests, "post", _fake(verdict)):
            r = ai.review_trade(sig, sized, state, spread_pips=1.1)
        mark = "BLOCKED" if r.blocked else "allowed"
        print(f"           {label:22} -> {mark:8} {r.reason}")

    def _down(*a, **k):
        raise _requests.Timeout("api down")

    with _patch.object(_requests, "post", _down):
        r = ai.review_trade(sig, sized, state)
    print(f"           {'API down':22} -> "
          f"{'BLOCKED' if r.blocked else 'allowed':8} {r.reason}")
    assert not r.blocked, "an outage must not halt a bot that ran without AI"

    # Carry on with the agreeing verdict.
    with _patch.object(_requests, "post", _fake(scenarios[0][1])):
        review_result = ai.review_trade(sig, sized, state, spread_pips=1.1)
    assert not review_result.blocked

    # --- 5. record (as if the order filled) ------------------------------
    signed = sized.units if side == "buy" else -sized.units
    pos = tracker.open_position(conn, PAIR, signed, sig.entry,
                                sig.stop, sig.target,
                                broker_order_id="dryrun-1",
                                broker_mode="practice")
    risk.record_trade_opened(conn)
    ai_log.record_verdict(conn, PAIR, side, review_result,
                          would_have=f"{side} {sized.units:,} units",
                          position_id=pos.id)
    print(f"\n5. RECORD  position #{pos.id} · {pos.direction} "
          f"{pos.qty:+,.0f} units")

    # --- 6. reconcile a target fill --------------------------------------
    import oanda
    payload = {"trade": {
        "id": "dryrun-1", "state": "CLOSED", "instrument": PAIR,
        "initialUnits": str(signed), "price": f"{sig.entry:.5f}",
        "averageClosePrice": f"{sig.target:.5f}",
        # Slightly less than the arithmetic reward: that difference is the
        # spread, and the whole reason the broker's number is preferred.
        "realizedPL": f"{sized.reward_gbp - 0.35:.2f}"}}
    outcome = reconcile.classify_trade(oanda.parse_trade(payload),
                                       sig.stop, sig.target, "GBP")
    change = reconcile.apply_outcome(conn, pos, outcome)
    risk.record_trade_closed(conn, change.pnl_gbp)
    print(f"\n6. CLOSE   {reconcile.describe(change)}")
    print(f"           arithmetic said £{sized.reward_gbp:.2f}, broker paid "
          f"£{change.pnl:.2f} — the gap is the spread")
    fresh = tracker.get_position(conn, pos.id)
    print(f"           ledger stores £{fresh.realised_gbp():.2f} "
          f"(from broker: {fresh.pnl_is_from_broker}) — the derived figure "
          f"would have been £{fx.pnl_to_gbp(fresh.realised(), PAIR):.2f}")

    # --- 7. the ledger ---------------------------------------------------
    verdict = risk.check(conn)
    print(f"\n7. LEDGER  trades today {verdict.trades_today}"
          f"/{CONFIG.risk.max_trades_per_day} · "
          f"P&L £{verdict.pnl_today_gbp:.2f}")
    print(f"           next trade allowed: {verdict.allowed}")

    # --- 7b. the post-mortem ---------------------------------------------
    with _patch.object(_requests, "post", _fake(
            {"action": "HOLD", "confidence": 0.8,
             "rationale": "NO LESSON — within normal variance"})):
        fresh = tracker.get_position(conn, pos.id)
        takeaway = ai.postmortem(fresh, change.pnl_gbp, change.exit_reason)
    ai_log.record_postmortem(conn, pos.id, PAIR, takeaway, change.pnl_gbp,
                             change.exit_reason)
    print(f"\n7b. LESSON {takeaway.text}")
    print(f"           has_lesson={takeaway.has_lesson} — recorded either "
          f"way, announced only when there is something to say")

    hist = ai_log.similar_losses(conn, "long", 5,
                                 CONFIG.ai.history_min_trades)
    print(f"           loss history offered to future prompts: "
          f"{len(hist)} (floor is {CONFIG.ai.history_min_trades} closed "
          f"trades, ledger has 1)")
    assert hist == [], "history must stay shut below the floor"

    print("\n8. SUMMARY")
    for line in report.format_eod(conn, {}).splitlines():
        print(f"           {line}")

    print("\n" + "=" * 70)
    print("Pipeline intact. Nothing was sent to any broker.")
    print("=" * 70)


if __name__ == "__main__":
    main()
