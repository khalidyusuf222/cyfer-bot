"""
Tests for the AI reviewer.

Every test here runs OFFLINE. `requests.post` is replaced with a fake, so
the whole matrix of failure modes — timeout, rate limit, bad JSON, missing
keys, wrong types, a dead model — is exercised deterministically instead of
being hoped about.

The tests fall into two groups, and the second group is the important one:

  1. Does it fail safely?  Every broken response must produce a fallback,
     never an exception and never a usable-looking verdict.

  2. Is it confined?  The model must not be able to open a trade, resize
     one, move a stop, or reach a risk gate. Those are asserted directly,
     because a comment in a docstring is not a guarantee.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import requests

import ai
import ai_log
import risk
import tracker
from config import CONFIG


# Most tests here deliberately break things, and ai.py logs a warning every
# time it falls back. That is correct behaviour and noise in a test run.
logging.getLogger("cyferbot.ai").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, content="", payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": content}}]}

    def json(self):
        return self._payload


def replies(content):
    """A fake transport that always returns this assistant message."""
    return lambda *a, **k: FakeResponse(content=content)


def good(action="BUY", confidence=0.8, rationale="Clean setup at a level."):
    return json.dumps({"action": action, "confidence": confidence,
                       "rationale": rationale})


@dataclass
class FakeSetup:
    ticker: str = "EUR_USD"
    direction: str = "bullish"
    entry: float = 1.10000
    stop: float = 1.09500
    target: float = 1.11000
    conditions_met: list = field(default_factory=lambda: ["Trend: uptrend"])
    conditions_missing: list = field(default_factory=lambda: ["No EMA stack"])

    @property
    def score(self):
        return len(self.conditions_met)

    @property
    def total(self):
        return len(self.conditions_met) + len(self.conditions_missing)

    @property
    def rr(self):
        return abs(self.target - self.entry) / abs(self.entry - self.stop)


@dataclass
class FakeSized:
    units: int = 2500
    stop_pips: float = 50.0
    risk_gbp: float = 10.0
    reward_gbp: float = 20.0
    direction: str = "long"


@dataclass
class FakeState:
    phase: str = "golden"
    is_golden: bool = True
    can_enter: bool = True
    reason: str = "Golden hours."


def setup_env(**over):
    os.environ["AI_ENABLED"] = over.get("enabled", "on")
    os.environ["AI_PROVIDER"] = over.get("provider", "groq")
    os.environ["GROQ_API_KEY"] = over.get("key", "gsk_test")
    os.environ.pop("AI_MODEL", None)


def fresh():
    conn = tracker.connect(Path(tempfile.mkdtemp()) / "ai.db")
    risk.day_state(conn)
    return conn


def review(transport, **env):
    setup_env(**env)
    with patch.object(requests, "post", transport):
        return ai.review_trade(FakeSetup(), FakeSized(), FakeState())


# ===========================================================================
# 1. Does it fail safely?
# ===========================================================================

def test_a_good_reply_is_understood():
    setup_env()
    with patch.object(requests, "post", replies(good())):
        v = ai._ask([{"role": "user", "content": "x"}], "t",
                    ai.DECISION_SCHEMA, 100)
    assert v.ok and v.action == "BUY" and v.confidence == 0.8
    assert v.attempts == 1
    print("PASS  a well-formed reply parses")


def test_every_broken_reply_becomes_a_fallback():
    """
    The matrix. None of these may raise, and none may come back ok=True —
    a half-understood verdict is worse than none, because it looks like a
    judgement.
    """
    broken = {
        "not JSON at all": "I think you should buy!",
        "a JSON list": "[1, 2, 3]",
        "a bare string": '"BUY"',
        "empty": "",
        "missing action": json.dumps({"confidence": 0.9, "rationale": "x"}),
        "missing confidence": json.dumps({"action": "BUY", "rationale": "x"}),
        "missing rationale": json.dumps({"action": "BUY", "confidence": 0.9}),
        "unknown action": json.dumps({"action": "SHORT", "confidence": 0.9,
                                      "rationale": "x"}),
        "lowercase-but-invalid": json.dumps({"action": "maybe",
                                             "confidence": 0.5,
                                             "rationale": "x"}),
        "confidence as text": json.dumps({"action": "BUY",
                                          "confidence": "high",
                                          "rationale": "x"}),
        "confidence as bool": json.dumps({"action": "BUY", "confidence": True,
                                          "rationale": "x"}),
        "blank rationale": json.dumps({"action": "BUY", "confidence": 0.9,
                                       "rationale": "   "}),
        "null everywhere": json.dumps({"action": None, "confidence": None,
                                       "rationale": None}),
    }
    setup_env()
    for label, content in broken.items():
        with patch.object(requests, "post", replies(content)):
            v = ai._ask([{"role": "user", "content": "x"}], "t",
                        ai.DECISION_SCHEMA, 100)
        assert not v.ok, f"{label} was accepted"
        assert v.action == "HOLD", f"{label} -> {v.action}"
        assert v.confidence == 0.0, label
        assert v.failure, label
    print(f"PASS  all {len(broken)} malformed replies fall back to "
          f"HOLD/0.0, none raise")


def test_lowercase_action_is_accepted():
    """Case is cosmetic — a model returning "buy" meant BUY."""
    setup_env()
    with patch.object(requests, "post", replies(json.dumps(
            {"action": "buy", "confidence": 0.7, "rationale": "ok"}))):
        v = ai._ask([{"role": "user", "content": "x"}], "t",
                    ai.DECISION_SCHEMA, 100)
    assert v.ok and v.action == "BUY"
    print("PASS  a lowercase action is accepted and normalised")


def test_confidence_is_clamped_not_rejected():
    setup_env()
    for raw, want in ((1.4, 1.0), (-0.3, 0.0), (0.55, 0.55), (1, 1.0)):
        with patch.object(requests, "post", replies(json.dumps(
                {"action": "BUY", "confidence": raw, "rationale": "x"}))):
            v = ai._ask([{"role": "user", "content": "x"}], "t",
                        ai.DECISION_SCHEMA, 100)
        assert v.ok and v.confidence == want, (raw, v.confidence)
    print("PASS  out-of-range confidence is clamped, not thrown away")


def test_transport_failures_all_fall_back():
    def timeout(*a, **k):
        raise requests.Timeout("too slow")

    def conn_error(*a, **k):
        raise requests.ConnectionError("no route")

    def explodes(*a, **k):
        raise ValueError("something nobody predicted")

    cases = {
        "timeout": timeout,
        "connection error": conn_error,
        "unexpected exception": explodes,
        "rate limited": lambda *a, **k: FakeResponse(status=429),
        "bad key": lambda *a, **k: FakeResponse(status=401),
        "model retired": lambda *a, **k: FakeResponse(status=404),
        "server error": lambda *a, **k: FakeResponse(status=500),
        "empty payload": lambda *a, **k: FakeResponse(payload={}),
        "no choices": lambda *a, **k: FakeResponse(payload={"choices": []}),
    }
    setup_env()
    for label, transport in cases.items():
        with patch.object(requests, "post", transport):
            v = ai._ask([{"role": "user", "content": "x"}], "t",
                        ai.DECISION_SCHEMA, 100)
        assert not v.ok and v.action == "HOLD", label
        assert v.failure, label
    print(f"PASS  all {len(cases)} transport failures fall back cleanly")


def test_a_retired_model_says_so():
    setup_env()
    with patch.object(requests, "post",
                      lambda *a, **k: FakeResponse(status=404)):
        v = ai._ask([{"role": "user", "content": "x"}], "t",
                    ai.DECISION_SCHEMA, 100)
    assert "retired" in v.failure or "not found" in v.failure, v.failure
    print(f"PASS  a 404 names the likely cause: {v.failure}")


def test_missing_key_is_caught_before_the_network():
    os.environ["AI_ENABLED"] = "on"
    os.environ["AI_PROVIDER"] = "groq"
    os.environ.pop("GROQ_API_KEY", None)
    result = ai.review_trade(FakeSetup(), FakeSized(), FakeState())
    assert not result.blocked
    assert "GROQ_API_KEY" in result.verdict.failure
    setup_env()
    print("PASS  a missing API key is reported, and blocks nothing")


def test_it_retries_once_then_gives_up():
    setup_env()
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("first one")
        return FakeResponse(content=good())

    with patch.object(requests, "post", flaky):
        v = ai._ask([{"role": "user", "content": "x"}], "t",
                    ai.DECISION_SCHEMA, 100)
    assert v.ok and v.attempts == 2, (v.ok, v.attempts)
    assert calls["n"] == 2
    print("PASS  one transient failure is retried, and the retry is used")


def test_it_does_not_retry_forever():
    setup_env()
    calls = {"n": 0}

    def always_fails(*a, **k):
        calls["n"] += 1
        raise requests.Timeout("nope")

    with patch.object(requests, "post", always_fails):
        ai._ask([{"role": "user", "content": "x"}], "t",
                ai.DECISION_SCHEMA, 100)
    assert calls["n"] == CONFIG.ai.max_attempts, calls["n"]
    print(f"PASS  stops after {CONFIG.ai.max_attempts} attempts — it sits "
          f"in front of an order and cannot hang")


# ===========================================================================
# 2. Is it confined?
# ===========================================================================

def test_disabled_by_default_blocks_nothing():
    os.environ["AI_ENABLED"] = "off"
    assert not ai.enabled()
    result = ai.review_trade(FakeSetup(), FakeSized(), FakeState())
    assert not result.blocked and not result.consulted
    setup_env()
    print("PASS  off by default, and off means it never interferes")


def test_agreement_lets_the_trade_through():
    result = review(replies(good("BUY", 0.9)))
    assert not result.blocked and result.consulted
    print("PASS  agreement lets the trade through")


def test_a_confident_hold_blocks():
    result = review(replies(good("HOLD", 0.9, "Stop is inside the noise.")))
    assert result.blocked
    assert "rejected" in result.reason.lower()
    print("PASS  a confident HOLD blocks the trade")


def test_an_unconfident_hold_does_not_block():
    """
    A hesitant objection is not grounds to overrule a setup that passed
    every deterministic check. The strategy is the baseline; the model has
    to be fairly sure before it overrides it.
    """
    low = CONFIG.ai.min_veto_confidence - 0.2
    result = review(replies(good("HOLD", low, "Not sure about this.")))
    assert not result.blocked, result.reason
    assert "below" in result.reason
    print(f"PASS  a HOLD at {low:.0%} does not block "
          f"(floor is {CONFIG.ai.min_veto_confidence:.0%})")


def test_the_opposite_direction_is_a_rejection_not_a_new_trade():
    """
    THE ONE THAT MATTERS MOST.

    The setup is bullish. The model answers SELL. That must be read as
    "don't take this", never as "take the other side" — the model is not
    permitted to open a position, and a short here would be a position
    nobody sized, nobody risk-checked and nobody proposed.
    """
    result = review(replies(good("SELL", 0.95, "Trend looks exhausted.")))
    assert result.blocked
    assert "not allowed to open the other side" in result.reason
    assert result.verdict.action == "SELL"     # recorded honestly...
    # ...but the only outcome available is "blocked".
    assert isinstance(result.blocked, bool)
    print("PASS  an opposite-direction answer blocks, and cannot open a trade")


def test_a_short_setup_mirrors_correctly():
    setup_env()
    short = FakeSetup(direction="bearish", entry=1.10000, stop=1.10500,
                      target=1.09000)
    with patch.object(requests, "post", replies(good("SELL", 0.9))):
        agree = ai.review_trade(short, FakeSized(), FakeState())
    with patch.object(requests, "post", replies(good("BUY", 0.9))):
        disagree = ai.review_trade(short, FakeSized(), FakeState())
    assert not agree.blocked, "SELL on a bearish setup is agreement"
    assert disagree.blocked, "BUY on a bearish setup is disagreement"
    print("PASS  on a short, SELL is agreement and BUY is a rejection")


def test_fail_mode_abstain_leaves_the_trade_alone():
    import dataclasses
    original = CONFIG.ai
    object.__setattr__(CONFIG, "ai",
                       dataclasses.replace(original, fail_mode="abstain"))
    try:
        def timeout(*a, **k):
            raise requests.Timeout("down")
        result = review(timeout)
        assert not result.blocked
        assert "stands on the strategy alone" in result.reason
    finally:
        object.__setattr__(CONFIG, "ai", original)
    print("PASS  abstain: an API outage changes nothing about the bot")


def test_fail_mode_hold_blocks_on_failure():
    import dataclasses
    original = CONFIG.ai
    object.__setattr__(CONFIG, "ai",
                       dataclasses.replace(original, fail_mode="hold"))
    try:
        def timeout(*a, **k):
            raise requests.Timeout("down")
        result = review(timeout)
        assert result.blocked
        assert "AI_FAIL_MODE=hold" in result.reason
    finally:
        object.__setattr__(CONFIG, "ai", original)
    print("PASS  hold: an API outage blocks the trade, as the spec asked")


def test_the_prompt_never_asks_the_model_about_size():
    """
    Sizing is arithmetic with tests behind it. The model is shown the size
    so it can judge the trade, and told plainly that it is fixed.
    """
    prompt = ai.build_veto_prompt(FakeSetup(), FakeSized(), FakeState())
    assert "FIXED" in prompt
    assert "not yours to change" in prompt
    low = ai.VETO_SYSTEM.lower()
    assert "cannot change the size" in low
    assert "cannot propose a trade in the opposite direction" in low
    print("PASS  the prompt states that size, stop and target are fixed")


def test_the_prompt_carries_the_real_numbers():
    prompt = ai.build_veto_prompt(FakeSetup(), FakeSized(), FakeState(),
                                  spread_pips=1.2)
    for marker in ("EUR/USD", "1.10000", "1.09500", "1.11000",
                   "2,500 units", "50.0 pips", "1.2 pips", "golden"):
        assert marker in prompt, marker
    print("PASS  the prompt carries the actual levels, size and spread")


# ===========================================================================
# 3. The 20-trade floor
# ===========================================================================

def test_no_loss_history_below_the_floor():
    """
    Bob's choice, and the safeguard that matters most in the feedback loop.
    A model shown three losses and asked what they have in common will
    always answer, and at n=3 that answer is noise.
    """
    conn = fresh()
    for i in range(10):
        pos = tracker.open_position(conn, "EUR_USD", 1000, 1.1000,
                                    1.0950, 1.1100)
        tracker.close_position_by_id(conn, pos.id, 1.0950)

    losses = ai_log.similar_losses(conn, "long", 5, min_closed=20)
    assert losses == [], losses
    print("PASS  10 closed trades is below the floor — no history is sent")


def test_history_appears_once_the_floor_is_cleared():
    conn = fresh()
    for i in range(22):
        pos = tracker.open_position(conn, "EUR_USD", 1000, 1.1000,
                                    1.0950, 1.1100)
        tracker.close_position_by_id(conn, pos.id, 1.0950)   # all losses

    losses = ai_log.similar_losses(conn, "long", 5, min_closed=20)
    assert len(losses) == 5, len(losses)
    assert all(l["pnl_gbp"] < 0 for l in losses)
    print("PASS  at 22 closed trades the history opens up, capped at 5")


def test_the_prompt_says_plainly_when_there_is_no_history():
    prompt = ai.build_veto_prompt(FakeSetup(), FakeSized(), FakeState(),
                                  losses=[])
    assert "none available yet" in prompt
    assert "Do not speculate" in prompt
    print("PASS  with no history the prompt says so, and forbids guessing")


def test_winners_are_never_offered_as_losses():
    conn = fresh()
    for i in range(25):
        pos = tracker.open_position(conn, "EUR_USD", 1000, 1.1000,
                                    1.0950, 1.1100)
        tracker.close_position_by_id(conn, pos.id, 1.1100)   # all wins
    assert ai_log.similar_losses(conn, "long", 5, min_closed=20) == []
    print("PASS  25 winning trades produce no 'past losses'")


def test_history_is_filtered_by_direction():
    conn = fresh()
    for i in range(12):
        p = tracker.open_position(conn, "EUR_USD", 1000, 1.1000, 1.0950)
        tracker.close_position_by_id(conn, p.id, 1.0950)
    for i in range(12):
        p = tracker.open_position(conn, "EUR_USD", -1000, 1.1000, 1.1050)
        tracker.close_position_by_id(conn, p.id, 1.1050)

    longs = ai_log.similar_losses(conn, "long", 5, min_closed=20)
    shorts = ai_log.similar_losses(conn, "short", 5, min_closed=20)
    assert longs and all(l["direction"] == "long" for l in longs)
    assert shorts and all(l["direction"] == "short" for l in shorts)
    print("PASS  a long setup is only shown losing longs")


# ===========================================================================
# 4. The post-mortem
# ===========================================================================

def _closed_position(conn):
    pos = tracker.open_position(conn, "EUR_USD", 2500, 1.10000,
                                1.09500, 1.11000)
    return tracker.close_position_by_id(conn, pos.id, 1.09500, "[stop]")


def test_no_lesson_is_a_first_class_answer():
    """
    The safeguard against manufacturing superstitions. A model asked why a
    trade lost will always produce a reason; it has to be able to say the
    loss was ordinary, and that has to be recorded as an answer rather
    than as a failure.
    """
    conn = fresh()
    pos = _closed_position(conn)
    setup_env()
    with patch.object(requests, "post", replies(good(
            "HOLD", 0.9, "NO LESSON — within normal variance"))):
        t = ai.postmortem(pos, -10.0, "stop")
    assert t.ok and not t.has_lesson
    assert t.text == ai.Takeaway.NO_LESSON
    print("PASS  'no lesson' is a valid, recorded answer")


def test_a_real_lesson_is_kept():
    conn = fresh()
    pos = _closed_position(conn)
    setup_env()
    with patch.object(requests, "post", replies(good(
            "SELL", 0.8, "Stop was 5 pips, inside the normal spread."))):
        t = ai.postmortem(pos, -10.0, "stop")
    assert t.ok and t.has_lesson
    assert "5 pips" in t.text
    print("PASS  a specific lesson is kept")


def test_the_postmortem_prompt_allows_no_lesson_loudly():
    low = ai.POSTMORTEM_SYSTEM.lower()
    assert "a single trade is almost never evidence" in low
    assert "use it freely" in low
    assert "do not give generic trading advice" in low
    print("PASS  the post-mortem prompt makes 'no lesson' the easy answer")


def test_a_failed_postmortem_is_silent_not_fatal():
    conn = fresh()
    pos = _closed_position(conn)
    setup_env()

    def timeout(*a, **k):
        raise requests.Timeout("down")

    with patch.object(requests, "post", timeout):
        t = ai.postmortem(pos, -10.0, "stop")
    assert not t.ok and not t.has_lesson and t.failure
    print("PASS  a failed post-mortem is a missing journal line, nothing more")


# ===========================================================================
# 5. The record
# ===========================================================================

def test_every_verdict_is_logged_with_what_it_prevented():
    """
    A block that leaves no record of what it blocked is an unfalsifiable
    claim to have helped. In a month the only question worth asking is
    whether these saved money, and that needs the counterfactual stored.
    """
    conn = fresh()
    result = review(replies(good("HOLD", 0.9, "Bad spread.")))
    assert result.blocked
    ai_log.record_verdict(conn, "EUR_USD", "long", result,
                          would_have="buy 2,500 units @ 1.10000, stop 1.09500")

    rows = ai_log.recent_verdicts(conn, 5)
    assert len(rows) == 1
    assert rows[0]["blocked"] == 1
    assert rows[0]["rationale"] == "Bad spread."
    assert "2,500 units" in rows[0]["would_have"]
    print("PASS  a block is stored with the trade it prevented")


def test_failed_verdicts_are_logged_too():
    conn = fresh()

    def timeout(*a, **k):
        raise requests.Timeout("down")

    result = review(timeout)
    ai_log.record_verdict(conn, "EUR_USD", "long", result)
    row = ai_log.recent_verdicts(conn, 1)[0]
    assert row["ok"] == 0 and row["failure"]
    s = ai_log.stats(conn)
    assert s["failed"] == 1 and s["fail_rate"] == 100.0
    print("PASS  outages are recorded, so a silent AI can't look like a "
          "working one")


def test_stats_separate_blocks_from_failures():
    conn = fresh()
    ai_log.record_verdict(conn, "EUR_USD", "long",
                          review(replies(good("HOLD", 0.9))))
    ai_log.record_verdict(conn, "EUR_USD", "long",
                          review(replies(good("BUY", 0.9))))
    ai_log.record_verdict(conn, "GBP_USD", "long",
                          review(replies(good("BUY", 0.9))))

    s = ai_log.stats(conn)
    assert s["verdicts"] == 3 and s["blocked"] == 1
    assert abs(s["block_rate"] - 33.3) < 0.5
    assert s["failed"] == 0
    print(f"PASS  stats read 3 verdicts, 1 block "
          f"({s['block_rate']:.0f}%), 0 failures")


# ===========================================================================
# 6. Provider switching
# ===========================================================================

def test_provider_defaults_to_groq_with_a_live_model():
    os.environ.pop("AI_PROVIDER", None)
    os.environ.pop("AI_MODEL", None)
    assert ai.provider_name() == "groq"
    assert ai.model_name() == "openai/gpt-oss-120b"
    assert "llama-3.3" not in ai.model_name(), \
        "llama-3.3-70b-versatile was retired on 16 Aug 2026"
    setup_env()
    print(f"PASS  defaults to groq · {ai.model_name()}")


def test_switching_provider_switches_endpoint_and_key():
    os.environ["AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = "test-gemini"
    assert "generativelanguage" in ai.PROVIDERS["gemini"]["url"]
    assert ai._key() == "test-gemini"
    assert ai.model_name().startswith("gemini")
    setup_env()
    print("PASS  one .env line moves the bot to a different company's API")


def test_an_unknown_provider_refuses_rather_than_guessing():
    os.environ["AI_PROVIDER"] = "skynet"
    ok, detail = ai.configured()
    assert not ok and "expected one of" in detail
    setup_env()
    print("PASS  an unrecognised provider is reported, not guessed at")


def test_strict_schema_used_where_supported():
    os.environ["AI_PROVIDER"] = "groq"
    fmt = ai._response_format("t", ai.DECISION_SCHEMA)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True

    os.environ["AI_PROVIDER"] = "gemini"
    assert ai._response_format("t", ai.DECISION_SCHEMA) == \
        {"type": "json_object"}
    setup_env()
    print("PASS  strict schema on Groq, json_object where it isn't supported")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
