"""
Tests for the execution engine.

These test the SAFETY logic, not the network calls. What matters is that
bad orders are refused before they reach the API — the network part is the
broker's problem, the refusing is mine.

Rewritten 2026-09-15 when execution.py became a router over two brokers.
The old file tested Alpaca's share rules only, and every test in it that
said "stop must be below entry" would have rejected every short setup the
strategy produces.
"""

import os
import tempfile
from pathlib import Path

import execution
import risk
import tracker


class FakeSession:
    def __init__(self, can_enter=True, reason="Golden hours."):
        self.can_enter = can_enter
        self.reason = reason


def fresh():
    conn = tracker.connect(Path(tempfile.mkdtemp()) / "exec.db")
    risk.day_state(conn)
    return conn


def use_oanda():
    os.environ["BROKER"] = "oanda"


def use_alpaca():
    os.environ["BROKER"] = "alpaca"


# ---------------------------------------------------------------------------
# Which broker
# ---------------------------------------------------------------------------

def test_broker_defaults_to_oanda():
    os.environ.pop("BROKER", None)
    assert execution.broker_name() == "oanda"
    print("PASS  BROKER defaults to oanda")


def test_unknown_broker_refuses_rather_than_guessing():
    os.environ["BROKER"] = "interactive-brokers"
    try:
        execution._broker()
    except execution.ExecutionError as e:
        assert "expected 'oanda' or 'alpaca'" in str(e)
        use_oanda()
        print("PASS  an unrecognised BROKER refuses instead of falling back")
        return
    use_oanda()
    raise AssertionError("unknown broker silently resolved to something")


# ---------------------------------------------------------------------------
# Mode defaults
# ---------------------------------------------------------------------------

def test_defaults_to_practice():
    use_oanda()
    for value in (None, "", "PRACTICE", "nonsense", "Live-ish", "paper"):
        if value is None:
            os.environ.pop("OANDA_ENV", None)
        else:
            os.environ["OANDA_ENV"] = value
        assert execution.trading_mode() == "practice", value
        assert not execution.is_live(), value
    print("PASS  anything but exactly 'live' means practice")


def test_live_requires_the_exact_word():
    use_oanda()
    os.environ["OANDA_ENV"] = "live"
    assert execution.is_live()
    os.environ["OANDA_ENV"] = "practice"
    assert not execution.is_live()
    print("PASS  live mode requires OANDA_ENV=live exactly")


def test_live_uses_different_credentials():
    """
    The practice token must not be able to reach the live account. Separate
    variable names are the whole mechanism, so this asserts they are.
    """
    import oanda
    use_oanda()
    os.environ["OANDA_TOKEN"] = "practice-token"
    os.environ.pop("OANDA_LIVE_TOKEN", None)

    os.environ["OANDA_ENV"] = "live"
    try:
        oanda._token()
    except oanda.OandaError as e:
        assert "OANDA_LIVE_TOKEN" in str(e)
        assert "Refusing to trade" in str(e)
        os.environ["OANDA_ENV"] = "practice"
        assert oanda._token() == "practice-token"
        print("PASS  live needs its own token; the practice one won't do")
        return
    os.environ["OANDA_ENV"] = "practice"
    raise AssertionError("live mode accepted the practice token")


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------

def test_live_not_armed_by_default():
    conn = fresh()
    assert not execution.live_armed(conn)
    print("PASS  live is not armed by default")


def test_arming_requires_exact_phrase():
    conn = fresh()
    for wrong in ("yes", "i accept real money losses",
                  "I ACCEPT REAL MONEY LOSS", "I ACCEPT REAL MONEY LOSSES!"):
        assert not execution.arm_live(conn, wrong), wrong
        assert not execution.live_armed(conn)

    assert execution.arm_live(conn, execution.LIVE_CONFIRM_PHRASE)
    assert execution.live_armed(conn)
    print("PASS  arming needs the exact phrase, case and all")


def test_disarm_works():
    conn = fresh()
    execution.arm_live(conn, execution.LIVE_CONFIRM_PHRASE)
    execution.disarm_live(conn)
    assert not execution.live_armed(conn)
    print("PASS  disarming revokes it")


def test_auto_off_by_default():
    conn = fresh()
    assert not execution.auto_enabled(conn)
    execution.set_auto(conn, True)
    assert execution.auto_enabled(conn)
    execution.set_auto(conn, False)
    assert not execution.auto_enabled(conn)
    print("PASS  auto-execute defaults off and toggles")


# ---------------------------------------------------------------------------
# Preflight — the part that actually protects the account
# ---------------------------------------------------------------------------

# £1,000 account at 1% = £10 of risk. 2,500 units of EUR/USD with a 50-pip
# stop risks $12.50, about £9.88 — just inside it.
LONG = dict(entry=1.1000, stop=1.0950, qty=2500, target=1.1100,
            symbol="EUR_USD", side="buy")
SHORT = dict(entry=1.1000, stop=1.1050, qty=2500, target=1.0900,
             symbol="EUR_USD", side="sell")


def test_preflight_clear_on_a_valid_long():
    use_oanda()
    conn = fresh()
    assert execution.preflight(conn, FakeSession(), **LONG) == []
    print("PASS  a valid long passes preflight")


def test_preflight_clear_on_a_valid_short():
    """The case the old share-only preflight would have refused outright."""
    use_oanda()
    conn = fresh()
    problems = execution.preflight(conn, FakeSession(), **SHORT)
    assert problems == [], problems
    print("PASS  a valid SHORT passes preflight — stop above, target below")


def test_preflight_blocks_outside_session():
    use_oanda()
    conn = fresh()
    problems = execution.preflight(
        conn, FakeSession(False, "Asian hours."), **LONG)
    assert any("Session" in p for p in problems), problems
    print("PASS  blocked outside the entry window")


def test_preflight_blocks_when_risk_gate_shut():
    from config import CONFIG
    use_oanda()
    conn = fresh()
    for _ in range(CONFIG.risk.max_consecutive_losses):
        risk.record_trade_opened(conn)
        risk.record_trade_closed(conn, -50.0)

    problems = execution.preflight(conn, FakeSession(), **LONG)
    assert any("Risk" in p for p in problems), problems
    print(f"PASS  blocked after {CONFIG.risk.max_consecutive_losses} "
          f"consecutive losses")


def test_preflight_blocks_inverted_long():
    """The 59-second trade. Stop above a long entry closes it instantly."""
    use_oanda()
    conn = fresh()
    bad = dict(LONG, stop=1.1050)
    problems = execution.preflight(conn, FakeSession(), **bad)
    assert any("closes the moment it opens" in p for p in problems), problems
    print("PASS  blocked when a long's stop sits above its entry")


def test_preflight_blocks_inverted_short():
    use_oanda()
    conn = fresh()
    bad = dict(SHORT, stop=1.0950)
    problems = execution.preflight(conn, FakeSession(), **bad)
    assert any("not above entry" in p for p in problems), problems
    print("PASS  blocked when a short's stop sits below its entry")


def test_preflight_blocks_target_on_the_wrong_side():
    use_oanda()
    conn = fresh()
    problems = execution.preflight(conn, FakeSession(),
                                   **dict(LONG, target=1.0990))
    assert any("Target" in p for p in problems), problems

    problems = execution.preflight(conn, FakeSession(),
                                   **dict(SHORT, target=1.1010))
    assert any("Target" in p for p in problems), problems
    print("PASS  blocked when the target is on the wrong side, either way")


def test_preflight_blocks_oversized_risk():
    use_oanda()
    conn = fresh()
    # 100,000 units with a 50-pip stop risks $500 — about £395 on a
    # £1,000 account with a £10 limit.
    problems = execution.preflight(conn, FakeSession(),
                                   **dict(LONG, qty=100_000))
    assert any("above your" in p for p in problems), problems
    print("PASS  blocked when the order exceeds the per-trade risk limit")


def test_preflight_blocks_zero_units():
    use_oanda()
    conn = fresh()
    problems = execution.preflight(conn, FakeSession(), **dict(LONG, qty=0))
    assert any("Zero units" in p for p in problems), problems
    print("PASS  blocked on zero units")


def test_yen_risk_is_not_converted_at_the_dollar_rate():
    """
    USD/JPY risk is denominated in YEN. Converting it at the USD rate
    overstates the loss by about 150x, which would refuse every legitimate
    yen trade as 'above your limit'. This is the bug that check exists for.
    """
    use_oanda()
    yen = execution._risk_in_account_ccy(150.00, 149.55, 3000, "USD_JPY")
    usd = execution._risk_in_account_ccy(1.1000, 1.0950, 2500, "EUR_USD")
    # Both are deliberately sized near £10. If the yen one were converted
    # at the dollar rate it would come out in the hundreds.
    assert 5 < yen < 20, yen
    assert 5 < usd < 20, usd
    print(f"PASS  yen risk converts in yen (£{yen:.2f}), not dollars")


# ---------------------------------------------------------------------------
# The share path, kept working
# ---------------------------------------------------------------------------

def test_alpaca_path_still_refuses_fractional_shares():
    """
    broker_alpaca.py is preserved so switching back to shares is a config
    change. The rule that mattered most there — no fractional order, because
    a fraction cannot carry a broker-side stop — must still hold.
    """
    use_alpaca()
    conn = fresh()
    problems = execution.preflight(conn, FakeSession(), entry=100.0,
                                   stop=99.0, qty=0.93, target=102.0,
                                   symbol="SPY")
    assert any("fractional" in p for p in problems), problems
    assert any("vanish if it crashes" in p for p in problems), problems
    use_oanda()
    print("PASS  the Alpaca path still refuses fractional orders")


# ---------------------------------------------------------------------------
# Saying what's at stake
# ---------------------------------------------------------------------------

def test_describe_mode_is_unambiguous():
    use_oanda()
    conn = fresh()
    os.environ["OANDA_ENV"] = "practice"
    text = execution.describe_mode(conn)
    assert "PRACTICE" in text and "OANDA" in text

    os.environ["OANDA_ENV"] = "live"
    assert "NOT armed" in execution.describe_mode(conn)

    execution.arm_live(conn, execution.LIVE_CONFIRM_PHRASE)
    assert "REAL MONEY" in execution.describe_mode(conn)

    os.environ["OANDA_ENV"] = "practice"
    print("PASS  mode description always states what's at stake")


if __name__ == "__main__":
    os.environ.setdefault("OANDA_TOKEN", "test")
    os.environ.setdefault("OANDA_ACCOUNT", "001-004-000000-001")
    os.environ.setdefault("ALPACA_API_KEY", "test")
    os.environ.setdefault("ALPACA_API_SECRET", "test")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
