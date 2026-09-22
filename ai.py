"""
The AI layer — a second opinion, not a decision maker.

WHAT THIS IS ALLOWED TO DO
--------------------------
Exactly one thing: block a trade the strategy already found and already
sized. It can say no. It cannot say yes to something nobody proposed, it
cannot change a position size, move a stop, widen a target, or unlock a
gate that risk.py has shut.

That boundary is not a style preference. Position sizing and the risk
gates are the only parts of this system with any evidence behind them —
they are arithmetic, they are tested, and they behave the same way every
time. The strategy is unbacktested and the model is unbacktestABLE, so
letting either one near the sizing maths would mean the whole system was
resting on nothing.

WHY NOT THE OFFICIAL SDK
------------------------
Bob's spec asked for `from groq import Groq`. This uses plain `requests`
instead, for three reasons: requests is already a dependency and already
how oanda.py talks to its API, so there is nothing new to install on the
VPS and nothing new to break; Groq's endpoint is OpenAI-compatible, so
the same twenty lines reach Google Gemini by changing one variable; and
the SDK pins its own httpx version, which is how a working bot becomes a
broken bot on a `pip install`.

WHY THERE IS A PROVIDER SWITCH
------------------------------
Groq retired llama-3.3-70b-versatile on 16 August 2026, which is the
model this feature was originally specced against. That happened before a
line of it was written. It will happen again. Changing providers is a
one-line edit in .env, not a rewrite.

NOTHING IN HERE CAN RAISE
-------------------------
Every public function catches everything and returns a fallback. An AI
outage must never be able to crash the bot, and must never be able to
place a trade either.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from config import CONFIG

log = logging.getLogger("cyferbot.ai")


# ===========================================================================
# Providers
# ===========================================================================

# Both speak the OpenAI chat-completions dialect, which is why one client
# reaches either. `strict` says whether the provider enforces a JSON schema
# rather than merely promising valid JSON.
PROVIDERS: dict[str, dict] = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
        "strict_schema": True,
        "signup": "https://console.groq.com — API Keys — Create API Key",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/"
               "chat/completions",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-3.5-flash",
        "strict_schema": False,
        "signup": "https://aistudio.google.com/apikey",
    },
}


class AIUnavailable(RuntimeError):
    """Raised internally only. Never escapes a public function."""


def provider_name() -> str:
    return os.environ.get("AI_PROVIDER", "groq").strip().lower()


def model_name() -> str:
    p = PROVIDERS.get(provider_name(), {})
    return os.environ.get("AI_MODEL", "").strip() or p.get("default_model", "")


def enabled() -> bool:
    """
    Off unless explicitly switched on.

    The bot ran for weeks without this. It must still run without it, and
    the default must be the configuration that has actually been observed
    working.
    """
    return os.environ.get("AI_ENABLED", "off").strip().lower() in (
        "on", "true", "yes", "1")


def _key() -> str:
    p = PROVIDERS.get(provider_name())
    if not p:
        raise AIUnavailable(
            f"AI_PROVIDER is '{provider_name()}' — expected one of "
            f"{', '.join(PROVIDERS)}.")
    key = os.environ.get(p["key_env"], "").strip()
    if not key:
        raise AIUnavailable(f"{p['key_env']} is not set in .env.")
    return key


def configured() -> tuple[bool, str]:
    """(ready, explanation) — for !ai and the startup banner."""
    if not enabled():
        return False, "AI is off (`AI_ENABLED=off`). The bot trades on the strategy alone."
    try:
        _key()
    except AIUnavailable as e:
        return False, str(e)
    return True, f"{provider_name()} · `{model_name()}`"


# ===========================================================================
# What comes back
# ===========================================================================

VALID_ACTIONS = ("BUY", "SELL", "HOLD")


@dataclass(frozen=True)
class Verdict:
    """
    One answer from the model, or the fallback that stands in for one.

    `ok` is the field that matters. False means no usable answer was
    obtained — timeout, bad JSON, missing key, wrong type, API down,
    anything — and `action` is then the fallback, not the model's opinion.
    Callers must branch on `ok` before treating `action` as a judgement.
    """
    action: str = "HOLD"
    confidence: float = 0.0
    rationale: str = ""
    ok: bool = False
    failure: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    attempts: int = 0
    raw: str = ""

    @property
    def is_hold(self) -> bool:
        return self.action == "HOLD"

    def summary(self) -> str:
        if not self.ok:
            return f"⚪ no answer — {self.failure}"
        mark = {"BUY": "🟩", "SELL": "🟥", "HOLD": "⬜"}.get(self.action, "⬜")
        return (f"{mark} **{self.action}** ({self.confidence:.0%}) — "
                f"{self.rationale}")


def _fallback(why: str, started: float, attempts: int = 0) -> Verdict:
    """
    The only thing returned when the model cannot be reached or believed.

    HOLD at zero confidence, every time, with the reason recorded. What a
    caller DOES with a fallback is the caller's decision — see
    CONFIG.ai.fail_mode, because "the API timed out" and "the model thinks
    this is a bad trade" are not the same event and should not always
    produce the same behaviour.
    """
    return Verdict(action="HOLD", confidence=0.0, ok=False, failure=why,
                   rationale=f"No AI verdict: {why}",
                   provider=provider_name(), model=model_name(),
                   latency_ms=int((time.time() - started) * 1000),
                   attempts=attempts)


# ===========================================================================
# The schema
# ===========================================================================

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(VALID_ACTIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["action", "confidence", "rationale"],
    "additionalProperties": False,
}


def _response_format(schema_name: str, schema: dict) -> dict:
    """
    Strict schema enforcement where the provider supports it.

    `{"type": "json_object"}` only promises the reply parses as JSON — a
    model can return {"decision": "buy"} and satisfy it completely. Strict
    mode makes the wrong shape impossible at the API rather than something
    we detect afterwards, which removes a whole class of fallback.
    """
    if PROVIDERS.get(provider_name(), {}).get("strict_schema"):
        return {"type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True,
                                "schema": schema}}
    return {"type": "json_object"}


# ===========================================================================
# The call
# ===========================================================================

def _post(messages: list[dict], response_format: dict,
          timeout: float, max_tokens: int) -> dict:
    p = PROVIDERS[provider_name()]
    body = {
        "model": model_name(),
        "messages": messages,
        "temperature": CONFIG.ai.temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
    }
    r = requests.post(
        p["url"], json=body, timeout=timeout,
        headers={"Authorization": f"Bearer {_key()}",
                 "Content-Type": "application/json"})

    if r.status_code == 429:
        raise AIUnavailable("rate limited (free tier)")
    if r.status_code in (401, 403):
        raise AIUnavailable(f"{p['key_env']} rejected — check the key")
    if r.status_code == 404:
        raise AIUnavailable(
            f"model '{model_name()}' not found — it may have been retired")
    if not r.ok:
        raise AIUnavailable(f"HTTP {r.status_code}")

    return r.json()


def _content(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise AIUnavailable(f"unexpected response shape ({e})") from e


def _parse_decision(text: str) -> tuple[str, float, str]:
    """
    Validate hard. A model that returns the wrong shape gets no benefit of
    the doubt — a half-understood verdict is worse than none, because it
    looks like a judgement.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise AIUnavailable(f"reply was not valid JSON ({e})") from e

    if not isinstance(data, dict):
        raise AIUnavailable("reply was not a JSON object")

    action = data.get("action")
    if not isinstance(action, str) or action.upper() not in VALID_ACTIONS:
        raise AIUnavailable(f"action was {action!r}, expected one of "
                            f"{'/'.join(VALID_ACTIONS)}")

    raw_conf = data.get("confidence")
    if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)):
        raise AIUnavailable(f"confidence was {raw_conf!r}, expected a number")
    # Clamp rather than reject: a model that says 1.4 still means "very".
    confidence = max(0.0, min(1.0, float(raw_conf)))

    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise AIUnavailable("rationale was empty")

    return action.upper(), confidence, rationale.strip()[:400]


def _ask(messages: list[dict], schema_name: str, schema: dict,
         max_tokens: int) -> Verdict:
    """
    One decision, with a bounded number of attempts and a hard time budget.

    This sits inside a 60-second scan loop and in front of an order, so it
    is not allowed to hang. Two attempts of `timeout_seconds` each is the
    worst case, and the budget is checked between them so a slow first
    attempt does not buy a second one.
    """
    started = time.time()
    cfg = CONFIG.ai
    last = "unknown"

    for attempt in range(1, cfg.max_attempts + 1):
        if time.time() - started > cfg.total_budget_seconds:
            return _fallback(f"out of time after {attempt - 1} attempt(s)",
                             started, attempt - 1)
        try:
            payload = _post(messages, _response_format(schema_name, schema),
                            cfg.timeout_seconds, max_tokens)
            text = _content(payload)
            action, confidence, rationale = _parse_decision(text)
            return Verdict(
                action=action, confidence=confidence, rationale=rationale,
                ok=True, provider=provider_name(), model=model_name(),
                latency_ms=int((time.time() - started) * 1000),
                attempts=attempt, raw=text[:2000])

        except AIUnavailable as e:
            last = str(e)
        except requests.Timeout:
            last = f"timed out after {cfg.timeout_seconds:g}s"
        except requests.RequestException as e:
            last = f"network error ({type(e).__name__})"
        except Exception as e:  # noqa: BLE001 — nothing may escape
            last = f"unexpected error ({type(e).__name__}: {e})"

        log.warning("AI attempt %d/%d failed: %s",
                    attempt, cfg.max_attempts, last)

    return _fallback(last, started, cfg.max_attempts)


# ===========================================================================
# The veto
# ===========================================================================

VETO_SYSTEM = """You are a risk reviewer for a forex trading bot. You are \
NOT the trader.

A deterministic strategy has already found this setup, checked it against \
support and resistance levels, and calculated the position size from a \
fixed percentage of the account. All of that is settled and is not yours \
to change.

Your only job is to answer one question: is there an obvious reason NOT to \
take this trade?

Rules:
- Reply HOLD to reject the trade. Reply with the setup's own direction \
(BUY for a long, SELL for a short) to let it proceed.
- You cannot change the size, the stop, or the target. Do not suggest \
changes to them; they are fixed and your reply will not alter them.
- You cannot propose a trade in the opposite direction. If you disagree \
with the direction, reply HOLD.
- Default to letting it through. The strategy has a rationale; you are \
looking for a clear defect in this specific setup, not deciding whether \
you would have taken it.
- "confidence" is how sure you are of YOUR OWN answer, 0 to 1.
- Keep "rationale" under 200 characters and concrete. Name the specific \
thing, not a general principle.

Reply with JSON only."""


def _history_block(losses: list) -> str:
    """
    Past losing trades, or an explicit statement that there aren't enough.

    The floor is deliberate. Below it the model is told plainly that no
    history is available, because a model handed two losing trades and
    asked what they have in common will always answer, and that answer
    will be noise wearing the costume of a lesson.
    """
    floor = CONFIG.ai.history_min_trades
    if not losses:
        return (f"PAST LOSSES: none available yet. Fewer than {floor} closed "
                f"trades exist, so there is no loss history to learn from. "
                f"Do not speculate about past mistakes — you have not been "
                f"shown any.")

    lines = [f"PAST LOSSES on similar setups ({len(losses)} shown):"]
    for i, l in enumerate(losses, 1):
        lines.append(
            f"  {i}. {l.get('ticker', '?')} {l.get('direction', '?')} — "
            f"lost £{abs(float(l.get('pnl_gbp', 0))):,.2f}. "
            f"Conditions met: {l.get('conditions', 'unrecorded')}. "
            f"Takeaway recorded: {l.get('takeaway') or 'none'}")
    lines.append(
        "These are a small sample and may be coincidence. Treat them as "
        "weak evidence, and only mention one if this setup is clearly the "
        "same shape.")
    return "\n".join(lines)


def build_veto_prompt(setup, sized, state, spread_pips=None,
                      losses: Optional[list] = None) -> str:
    """The user-side message. Pure string building — no network, testable."""
    import pairs
    pair = pairs.parse(setup.ticker)
    d = pair.displayed_decimals
    side = "BUY (long)" if setup.direction == "bullish" else "SELL (short)"

    met = "\n".join(f"  PASSED: {c}" for c in setup.conditions_met) or \
        "  (none)"
    missing = "\n".join(f"  FAILED: {c}" for c in setup.conditions_missing) \
        or "  (none)"

    parts = [
        f"PAIR: {pair.display}",
        f"PROPOSED: {side}",
        f"ENTRY {setup.entry:.{d}f}  STOP {setup.stop:.{d}f}  "
        f"TARGET {setup.target:.{d}f}",
        f"Stop is {sized.stop_pips:.1f} pips away. "
        f"Reward-to-risk {setup.rr:.1f}:1.",
        f"Size {sized.units:,} units, risking £{sized.risk_gbp:,.2f} "
        f"(FIXED — not yours to change).",
        "",
        f"STRATEGY SCORE: {setup.score} of {setup.total} conditions",
        met,
        missing,
        "",
        f"SESSION: {state.phase}"
        + (" — London/New York overlap, the highest-volume window"
           if getattr(state, "is_golden", False) else ""),
    ]
    if spread_pips is not None:
        parts.append(f"CURRENT SPREAD: {spread_pips:.1f} pips "
                     f"({spread_pips / max(sized.stop_pips, 0.1) * 100:.0f}% "
                     f"of the stop distance)")
    parts += ["", _history_block(losses or [])]
    parts += ["", "Is there an obvious reason not to take this trade?"]
    return "\n".join(parts)


@dataclass(frozen=True)
class VetoResult:
    """What the veto layer decided, and why."""
    blocked: bool
    verdict: Verdict
    reason: str = ""

    @property
    def consulted(self) -> bool:
        return self.verdict.ok


def review_trade(setup, sized, state, spread_pips=None,
                 losses: Optional[list] = None) -> VetoResult:
    """
    Ask for a second opinion on a trade the strategy has already approved.

    Returns whether to block. This function CANNOT approve a trade that
    was not already approved, cannot alter a size, and cannot reach any
    of the risk gates — it is called after all of them have passed, and
    its only possible effect is to stop something happening.
    """
    if not enabled():
        return VetoResult(False, Verdict(failure="AI disabled"),
                          "AI is off")

    cfg = CONFIG.ai
    try:
        prompt = build_veto_prompt(setup, sized, state, spread_pips, losses)
    except Exception as e:  # noqa: BLE001
        log.exception("could not build the AI prompt")
        return VetoResult(False, _fallback(f"prompt build failed ({e})",
                                           time.time()),
                          "Prompt could not be built — trade left alone")

    verdict = _ask(
        [{"role": "system", "content": VETO_SYSTEM},
         {"role": "user", "content": prompt}],
        "trade_review", DECISION_SCHEMA, cfg.max_tokens_decision)

    # --- no usable answer ------------------------------------------------
    #
    # An API outage and a considered rejection are different events. Bob's
    # original spec said fall back to HOLD, which is right when the model
    # IS the decision maker — there, failing closed means not trading.
    # Here the model is only a veto, so failing closed would mean a Groq
    # outage silently halts a bot that ran fine without any AI for weeks.
    # Default is therefore to abstain, and fail_mode makes it a choice.
    if not verdict.ok:
        if cfg.fail_mode == "hold":
            return VetoResult(True, verdict,
                              f"No AI verdict ({verdict.failure}) and "
                              f"AI_FAIL_MODE=hold, so the trade is blocked.")
        return VetoResult(False, verdict,
                          f"No AI verdict ({verdict.failure}) — the trade "
                          f"stands on the strategy alone.")

    wanted = "BUY" if setup.direction == "bullish" else "SELL"

    # --- it agrees --------------------------------------------------------
    if verdict.action == wanted:
        return VetoResult(False, verdict, "AI agrees.")

    # --- it disagrees -----------------------------------------------------
    #
    # Both a HOLD and an opposite-direction answer are treated the same:
    # as a rejection of THIS trade. An opposite answer is never read as a
    # proposal to trade the other way — that would be the model creating a
    # position, which it is not permitted to do.
    if verdict.confidence < cfg.min_veto_confidence:
        return VetoResult(
            False, verdict,
            f"AI said {verdict.action} but only {verdict.confidence:.0%} "
            f"confident, below the {cfg.min_veto_confidence:.0%} needed to "
            f"block. Trade proceeds.")

    flipped = (verdict.action != "HOLD")
    return VetoResult(
        True, verdict,
        (f"AI wanted the opposite direction ({verdict.action}), which is "
         f"read as a rejection — it is not allowed to open the other side."
         if flipped else "AI rejected this setup.")
    )


# ===========================================================================
# The post-mortem
# ===========================================================================

# The "no lesson" escape hatch is the most important line in this prompt.
#
# Asked why a trade lost, a language model will always produce a reason —
# that is what the shape of the question invites. With one trade as
# evidence, that reason is very often manufactured. Left unchecked, those
# manufactured reasons get written to the database and fed into later
# prompts, and the result is a machine that invents superstitions and then
# trades on them. Giving the model an explicit, legitimate way to say
# "nothing to learn here" is what keeps that from being the default path.
POSTMORTEM_SYSTEM = """You are reviewing one completed forex trade for a \
trading journal.

Compare what happened to the reasoning at entry, and produce ONE concrete \
takeaway.

Critical rule: a single trade is almost never evidence of anything. Losing \
trades are a normal, expected part of a strategy with a 50% win rate — a \
loss is not proof of a mistake. If nothing specific went wrong, you MUST \
reply with the action "HOLD" and the exact rationale "NO LESSON — within \
normal variance". Use it freely. It is the correct answer most of the time.

Only give a real takeaway when something specific and checkable happened, \
for example: the stop was inside the normal noise range for that pair, the \
entry was taken against the higher-timeframe trend, the spread was a large \
fraction of the stop distance, or the trade was opened in a session with \
no volume.

Do not give generic trading advice. Do not say "manage risk carefully" or \
"wait for confirmation". Name the specific thing about THIS trade or say \
there is no lesson.

Use "action": "BUY" if the takeaway is that the setup was sound, "SELL" if \
the takeaway is that it should not have been taken, "HOLD" if there is no \
lesson. Keep "rationale" under 200 characters.

Reply with JSON only."""


@dataclass(frozen=True)
class Takeaway:
    """One lesson, or an honest absence of one."""
    text: str = ""
    has_lesson: bool = False
    ok: bool = False
    failure: str = ""
    verdict: Optional[Verdict] = None

    NO_LESSON = "NO LESSON — within normal variance"


def build_postmortem_prompt(pos, pnl_gbp: float, exit_reason: str,
                            entry_context: Optional[dict] = None) -> str:
    import pairs
    try:
        d = pairs.parse(pos.ticker).displayed_decimals
    except Exception:  # noqa: BLE001
        d = 5

    ctx = entry_context or {}
    won = pnl_gbp >= 0

    lines = [
        f"PAIR: {pos.ticker}",
        f"DIRECTION: {pos.direction}",
        f"ENTRY {pos.entry_price:.{d}f} -> EXIT {pos.exit_price:.{d}f}",
        f"STOP was {pos.stop_price:.{d}f}, TARGET was "
        f"{pos.target_price:.{d}f}" if pos.stop_price and pos.target_price
        else "Stop/target not recorded",
        f"RESULT: {'WON' if won else 'LOST'} £{abs(pnl_gbp):,.2f}",
        f"HOW IT ENDED: {exit_reason or 'unknown'}",
    ]

    if pos.duration_seconds is not None:
        mins = pos.duration_seconds / 60
        lines.append(f"HELD FOR: {mins:.0f} minutes"
                     + ("  <- very short; consider whether the stop was "
                        "inside normal noise" if mins < 5 else ""))

    if ctx:
        lines += [
            "",
            "AT ENTRY, THE STRATEGY SAID:",
            f"  Score: {ctx.get('score', '?')} of {ctx.get('total', '?')}",
            f"  Passed: {', '.join(ctx.get('conditions_met', [])) or 'none'}",
            f"  Failed: {', '.join(ctx.get('conditions_missing', [])) or 'none'}",
            f"  Session: {ctx.get('phase', 'unknown')}"
            + ("  (golden hours)" if ctx.get("golden_hours") else ""),
            f"  Stop distance: {ctx.get('stop_pips', '?')} pips",
        ]

    if pos.entry_slippage:
        lines.append(f"  Entry filled {pos.entry_slippage:+.5f} worse than "
                     f"requested")

    lines += ["", "What is the one concrete takeaway, if there is one?"]
    return "\n".join(lines)


def postmortem(pos, pnl_gbp: float, exit_reason: str = "",
               entry_context: Optional[dict] = None) -> Takeaway:
    """
    One short lesson from a closed trade, or an explicit "no lesson".

    Never raises. A failed post-mortem is a missing journal entry, which
    costs nothing; it must never interfere with recording the trade or
    updating the risk ledger.
    """
    if not enabled() or not CONFIG.ai.postmortem_enabled:
        return Takeaway(failure="post-mortem disabled")

    try:
        prompt = build_postmortem_prompt(pos, pnl_gbp, exit_reason,
                                         entry_context)
    except Exception as e:  # noqa: BLE001
        log.exception("could not build the post-mortem prompt")
        return Takeaway(failure=f"prompt build failed ({e})")

    verdict = _ask(
        [{"role": "system", "content": POSTMORTEM_SYSTEM},
         {"role": "user", "content": prompt}],
        "post_mortem", DECISION_SCHEMA, CONFIG.ai.max_tokens_postmortem)

    if not verdict.ok:
        return Takeaway(failure=verdict.failure, verdict=verdict)

    text = verdict.rationale.strip()
    no_lesson = (verdict.action == "HOLD"
                 or text.upper().startswith("NO LESSON"))

    return Takeaway(
        text=Takeaway.NO_LESSON if no_lesson else text,
        has_lesson=not no_lesson,
        ok=True,
        verdict=verdict)


# ===========================================================================
# Describing itself
# ===========================================================================

def describe() -> str:
    ready, detail = configured()
    cfg = CONFIG.ai

    if not ready:
        return (f"**AI review: OFF**\n{detail}\n\n"
                f"*The bot trades on the strategy alone — which is how it "
                f"has run all along.*")

    fail = ("blocks the trade" if cfg.fail_mode == "hold"
            else "lets the trade through")
    return (
        f"**AI review: ON**\n"
        f"Provider **{provider_name()}** · model `{model_name()}`\n\n"
        f"It can only **block** a trade the strategy already found and "
        f"sized. It cannot open one, resize one, move a stop, or reopen a "
        f"gate that risk limits have closed.\n\n"
        f"Blocks only at **{cfg.min_veto_confidence:.0%}+** confidence · "
        f"on failure it **{fail}**\n"
        f"Loss history is withheld below **{cfg.history_min_trades}** "
        f"closed trades."
    )
