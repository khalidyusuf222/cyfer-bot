"""
Discord trading bot — setup alerts, GBP position tracking, hard risk limits.

Strategy: cyfer.py, built from the Cyfer Academy guide. Trade with the
trend, at a level price has respected at least three times, on a candle
that shows rejection there, for at least twice the risk.

Two things this deliberately does NOT do:

  1. It never says "buy". It reports which conditions are present on the
     chart right now and shows its working, so you can disagree with it.
     The strategy has never been backtested; a command would be a lie
     dressed as confidence.

  2. It never lets you past your own risk limits. The session clock and the
     daily loss cap are hard gates, because those are the rules that keep
     accounts alive independently of whether the strategy works.

Everything you see is GBP. The pairs settle in their own quote currencies —
EUR/USD in dollars, USD/JPY in yen — so every figure is converted at the
live rate for that currency before display.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# MUST run before any local import that reads environment variables.
load_dotenv(Path(__file__).parent / ".env")

import ai
import ai_log
import calc
import execution
import fx
import memory
import watchlist
import graystone
import data as market
import metrics
import reconcile
import report
import review
import risk
import sessions
import cyfer
import tracker
from config import CONFIG, parameter_report

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cyferbot")

TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "60"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

conn = tracker.connect()

COLOURS = {"urgent": 0xE03131, "warn": 0xF08C00,
           "info": 0x1971C2, "good": 0x2F9E44, "setup": 0x0E7C86}

_alerted: set[str] = set()          # setups already announced this hour
# Kept separate from _alerted on purpose. They used to be one set, and a
# weak 2/6 alert at 14:05 used up the hour's slot — so the same setup
# improving to a tradeable 3/6 at 14:20 was silently never traded.
_traded: set[str] = set()
_recent_setups: dict = {}           # ticker -> (Setup, phase) for !b to attach


def embed(title: str, desc: str, kind: str = "info") -> discord.Embed:
    return discord.Embed(title=title, description=desc,
                         colour=COLOURS.get(kind, COLOURS["info"]))


# ===========================================================================
# Lifecycle
# ===========================================================================

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    ok, msg = await asyncio.to_thread(market.validate_credentials)
    state = sessions.current_state()

    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        import pairs
        watching = " · ".join(pairs.parse(t).display
                              for t in CONFIG.instruments.watchlist)
        s = CONFIG.sessions
        ai_line = ("🤖 AI review **ON** — it can block a trade, never open one"
                   if ai.enabled() else
                   "AI review off — trading on the strategy alone")
        if market.DATA_SOURCE_OVERRIDDEN:
            ai_line += f"\n\n⚠️ {market.DATA_SOURCE_OVERRIDDEN}"
        await channel.send(embed=embed(
            "Cyfer bot online",
            f"{msg}\n\n"
            f"Strategy: **Cyfer** — trend, level, trigger.\n"
            f"Watching **{watching}**\n"
            f"Account: £{CONFIG.display.account_gbp:,.2f} · "
            f"risking {CONFIG.risk.risk_per_trade_pct}% per trade "
            f"({execution.describe_mode(conn).splitlines()[0]})\n\n"
            f"🕒 {state.et_str} ET / {state.uk_str} UK — {state.reason}\n"
            f"Entries: {sessions.next_macro_countdown()}\n"
            f"Golden hours **{s.golden_start}–{s.golden_end} ET** "
            f"(13:00–17:00 UK) — London and New York both open.\n\n"
            f"Flat by **Fri {s.weekend_flatten} ET** · daily summary "
            f"**{s.eod_summary} ET**\n"
            f"Results are reconciled against the broker every scan.\n"
            f"{ai_line}\n\n"
            f"`!help` for commands · `!chart` for a live scan.",
            "good" if ok else "urgent",
        ))

    if not scan_loop.is_running():
        scan_loop.start()


# ===========================================================================
# The scan loop
# ===========================================================================

@tasks.loop(seconds=SCAN_SECONDS)
async def scan_loop():
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            return

        # Ask the broker what became of orders we placed, BEFORE anything
        # reads the risk ledger. A loss recorded here is what shuts the
        # gates further down this same tick.
        await _reconcile(channel)

        await _check_open_positions(channel)

        state = sessions.current_state()

        # Both of these are end-of-day duties and must run even though
        # entries are long since closed by the time they fire.
        await _end_of_day(channel, state)

        if not state.can_enter:
            return

        verdict = risk.check(conn)
        if not verdict.allowed:
            return

        # The whole watchlist, not just two. Four pairs across a 13-hour
        # window is a very different number of looks from two shares across
        # a 6-hour one, and the point of paper trading is to accumulate
        # enough closed trades for the review to say anything at all.
        for ticker in CONFIG.instruments.watchlist:
            await _scan_ticker(ticker, channel, state)

    except market.MarketError as e:
        log.warning("Market data: %s", e)
    except Exception:  # noqa: BLE001
        log.exception("scan_loop failed")


async def _scan_ticker(ticker: str, channel, state) -> None:
    """
    One instrument, one pass of the Cyfer strategy.

    [BOOK p45] The higher timeframe leads — trend and levels come from it,
    because longer timeframes filter out the noise. The lower timeframe
    supplies only the trigger candle and the current price.
    """
    y = CONFIG.cyfer

    bars_htf, bars_ltf = await asyncio.gather(
        asyncio.to_thread(market.get_bars, ticker, y.htf, 200),
        asyncio.to_thread(market.get_bars, ticker, y.ltf, 60),
    )

    # [JG] The 8/20/50 stack, kept as a confirmation condition only.
    ema_dir = graystone.ema_stack_direction(bars_htf)

    setup = cyfer.scan(ticker, bars_htf, bars_ltf, ema_direction=ema_dir)

    if setup is None or setup.score < CONFIG.strategy.min_alert_score:
        return

    key = f"{ticker}:{setup.direction}:{state.now_et.strftime('%Y-%m-%d-%H')}"
    first_alert = key not in _alerted
    _alerted.add(key)

    _recent_setups[ticker.upper()] = (setup, state.phase)

    will_trade = (execution.auto_enabled(conn)
                  and setup.score >= CONFIG.strategy.min_auto_score
                  and setup.tradeable
                  and key not in _traded)

    # One position per pair. The backtest applies the same rule, so what it
    # reports is what this loop would actually do.
    if (will_trade and CONFIG.strategy.one_position_per_pair
            and tracker.find_open(conn, ticker) is not None):
        will_trade = False
        log.info("%s: setup qualifies but a position is already open", ticker)

    if will_trade:
        _traded.add(key)
        await _auto_execute(setup, state, channel)
    elif not first_alert:
        return                      # already told Bob about this one

    await channel.send(
        content="@here" if setup.score >= CONFIG.strategy.min_auto_score
        else None,
        embed=embed(f"Setup — {ticker}",
                    cyfer.format_signal(
                        setup,
                        f"🕒 {state.et_str} ET / {state.uk_str} UK — "
                        f"{state.reason}"),
                    "setup"),
    )


def _free_margin_gbp():
    """
    OANDA's free margin right now, in GBP, or None if it can't be read.

    Margin is the deposit the broker holds while a trade is open. Free
    margin is what is left to open the next one with. None makes the sizer
    fall back to the account size, which is right when nothing is open.
    """
    if execution.broker_name() != "oanda":
        return None
    try:
        import oanda
        return float(oanda.account_summary()["margin_available"])
    except Exception as e:  # noqa: BLE001 - sizing still works without it
        log.warning("Couldn't read free margin (%s); sizing off the account "
                    "size instead", e)
        return None


async def _auto_execute(setup, state, channel) -> None:
    """
    Place an order for a setup that met the configured score threshold.

    Rewritten for forex on 2026-09-15. Two things changed that matter:

      1. SHORTS ARE REAL ORDERS NOW. Selling EUR/USD is buying dollars —
         no borrow, no locate, no special permission. The share version
         hardcoded "stop must be below entry" and would have silently
         refused every bearish setup the strategy produced.

      2. SIZE NO LONGER ROUNDS TO ZERO. A unit is one euro, not one $760
         share, so a GBP 10 risk always has a size that fits it. The whole
         "one whole share risks more than your limit" refusal is gone,
         because the condition that caused it cannot arise.
    """
    import pairs

    if execution.is_live() and not execution.live_armed(conn):
        await channel.send(embed=embed(
            "Auto-execute skipped",
            "Live mode is set but not armed. Nothing was sent.\n"
            "`!arm` explains how to arm it.", "warn"))
        return

    pair = pairs.parse(setup.ticker)
    d = pair.displayed_decimals
    long = setup.direction == "bullish"
    side = "buy" if long else "sell"

    # A setup can reach here with no entry when the price feed was rejected
    # as unreliable. Sizing would raise on it; refusing explicitly says why.
    if setup.entry <= 0 or setup.stop <= 0:
        await channel.send(embed=embed(
            f"Auto-execute refused — {pair.display}",
            "No trustworthy entry price. The two timeframes disagreed by "
            "more than the allowed tolerance, so no order was priced.\n\n"
            "*This is the guard that was missing when trade #1 was built "
            "on a bad print.*", "warn"))
        return

    # The 59-second trade, checked in the direction the setup actually is.
    if (long and setup.stop >= setup.entry) or \
       (not long and setup.stop <= setup.entry):
        wrong = "below" if long else "above"
        await channel.send(embed=embed(
            f"Auto-execute refused — {pair.display}",
            f"Stop `{setup.stop:.{d}f}` is not {wrong} entry "
            f"`{setup.entry:.{d}f}` for a {'long' if long else 'short'}. "
            f"That order would close itself the moment it filled.", "warn"))
        return

    try:
        # setup.target is passed in deliberately. Without it, size_trade
        # invents its own 2:1 target and the reward figure shown to Bob is
        # the minimum the book allows rather than the one the order is
        # actually carrying — understating a 3.3:1 trade as a 2:1 one.
        #
        # Free margin comes from OANDA, so a second trade is sized to what
        # the first one left over instead of being sent at full size and
        # rejected by the broker.
        free_margin = await asyncio.to_thread(_free_margin_gbp)
        sized = await asyncio.to_thread(
            risk.size_trade, setup.entry, setup.stop, setup.ticker,
            None, None, False, setup.target, free_margin)
    except ValueError as e:
        await channel.send(embed=embed("Auto-execute refused", str(e), "warn"))
        return

    qty = float(sized.units)

    if qty < 1:
        await channel.send(embed=embed(
            f"Auto-execute refused — {pair.display}",
            f"Sizing resolved to zero units.\n\n"
            + "\n".join(f"• {w}" for w in sized.warnings), "warn"))
        return

    problems = execution.preflight(conn, state, setup.entry, setup.stop,
                                   qty, setup.target,
                                   symbol=setup.ticker, side=side)
    if problems:
        await channel.send(embed=embed(
            f"Auto-execute refused — {pair.display}",
            "The setup fired but the order was blocked:\n\n"
            + "\n".join(f"• {p}" for p in problems)
            + "\n\n*Refusing is the system working, not failing.*",
            "warn"))
        return

    # --- the AI second opinion -------------------------------------------
    #
    # Deliberately LAST. Every deterministic gate — session clock, daily
    # loss cap, trades per day, consecutive losses, level validation, the
    # per-trade risk ceiling — has already passed by the time we get here.
    # The model is therefore incapable of unlocking any of them; the only
    # thing left for it to do is stop something that was otherwise going
    # to happen.
    #
    # It is also asked AFTER sizing, so the size it is shown is the size
    # that will be sent. It is told that number is fixed, and there is no
    # code path by which its answer could change it.
    verdict_id = None
    if ai.enabled():
        spread = None
        if market.get_spread_pips:
            try:
                spread = await asyncio.to_thread(
                    market.get_spread_pips, setup.ticker)
            except Exception:  # noqa: BLE001 — a missing spread is not fatal
                spread = None

        losses = await asyncio.to_thread(
            ai_log.similar_losses, conn,
            "long" if long else "short",
            CONFIG.ai.history_max_examples,
            CONFIG.ai.history_min_trades)

        review = await asyncio.to_thread(
            ai.review_trade, setup, sized, state, spread, losses)

        would_have = (f"{side} {sized.units:,} units {pair.symbol} @ "
                      f"{setup.entry:.{d}f}, stop {setup.stop:.{d}f}, "
                      f"target {setup.target:.{d}f}, "
                      f"risk £{sized.risk_gbp:,.2f}")
        verdict_id = await asyncio.to_thread(
            ai_log.record_verdict, conn, setup.ticker,
            "long" if long else "short", review, would_have)

        if review.blocked:
            await channel.send(embed=embed(
                f"AI blocked this trade — {pair.display}",
                f"{review.verdict.summary()}\n\n"
                f"**Would have been:** {would_have}\n\n"
                f"*{review.reason}*\n\n"
                f"Logged either way. `!ai` shows whether these blocks have "
                f"been saving money or costing it.",
                "warn"))
            return

        if review.consulted:
            log.info("AI allowed %s: %s", setup.ticker,
                     review.verdict.rationale)
        else:
            log.warning("AI gave no verdict (%s) — %s",
                        review.verdict.failure, review.reason)

    try:
        order = await asyncio.to_thread(
            execution.place_bracket, setup.ticker, qty,
            setup.entry, setup.stop, setup.target, side)
    except execution.ExecutionError as e:
        await channel.send(embed=embed("Order failed", str(e), "urgent"))
        return

    # The trade id is what makes this reconcilable. Without it stored, the
    # bot can never ask the broker how the trade ended — which is exactly
    # how auto trades used to close invisibly and leave the risk gates
    # reporting that they were protecting an account they had never seen a
    # loss on.
    #
    # qty is stored SIGNED. Negative is short, matching OANDA's own units
    # convention, and it is what makes the tracker's profit arithmetic come
    # out with the right sign in both directions.
    signed_qty = qty if long else -qty

    pos = await asyncio.to_thread(
        tracker.open_position, conn, setup.ticker, signed_qty,
        order.entry or setup.entry, setup.stop, setup.target,
        broker_order_id=order.order_id, broker_mode=order.mode,
        context={
            "phase": state.phase,
            "direction": setup.direction,
            "side": side,
            "score": setup.score,
            "total": setup.total,
            "conditions_met": setup.conditions_met,
            "conditions_missing": setup.conditions_missing,
            "requested_entry": setup.entry,
            "stop_distance": round(abs(setup.entry - setup.stop), 6),
            "stop_pips": round(sized.stop_pips, 1),
            "risk_gbp": round(sized.risk_gbp, 2),
            "golden_hours": bool(getattr(state, "is_golden", False)),
        })
    risk.record_trade_opened(conn)
    if verdict_id is not None:
        await asyncio.to_thread(ai_log.attach_position, conn, verdict_id,
                                pos.id)
    memory.record_context(conn, pos.id, "cyfer-auto", setup.direction,
                          setup.score, setup.total, setup.conditions_met,
                          setup.conditions_missing, state.phase)

    tag = "🔴 LIVE" if order.mode == "live" else "PRACTICE"
    arrow = "🟩 LONG" if long else "🟥 SHORT"
    await channel.send(content="@here", embed=embed(
        f"{tag} order placed — {pair.display}",
        f"{arrow}  **{sized.units:,} units**\n"
        f"Entry `{order.entry or setup.entry:.{d}f}` · "
        f"Stop `{setup.stop:.{d}f}` ({sized.stop_pips:.1f} pips) · "
        f"Target `{setup.target:.{d}f}`\n"
        f"Risk **£{sized.risk_gbp:,.2f}** to make "
        f"**£{sized.reward_gbp:,.2f}**\n\n"
        + ("".join(f"⚠️ {w}\n\n" for w in sized.warnings
                   if w.startswith("Cut from")))
        + f"Stop and target are held by OANDA, not by me — they survive this "
        f"bot going down.\n\n"
        f"Trade `{str(order.order_id)[:8]}` · status `{order.status}`\n"
        f"`!halt` closes everything.",
        "urgent" if order.mode == "live" else "good"))


async def _reconcile(channel) -> None:
    """
    Ask the broker what happened to the orders we placed, and record it.

    This is the loop that used to be missing. Without it the bot placed
    trades, the broker closed them, and the tracker never found out — so realised
    P&L stayed at zero and both the daily loss cap and the consecutive-loss
    lockout sat there reporting themselves as active while never once seeing
    a loss to act on.
    """
    if not tracker.broker_managed_open(conn):
        return

    try:
        changes = await asyncio.to_thread(reconcile.run, conn)
    except Exception:  # noqa: BLE001
        log.exception("reconcile failed")
        return

    for change in changes:
        if change.kind == "closed":
            # OANDA reports the realised figure in the account currency
            # already, including the spread. pnl_gbp converts from whatever
            # currency the change says it is in, rather than assuming USD.
            risk.record_trade_closed(conn, change.pnl_gbp)
        elif change.kind == "abandoned":
            # The entry never filled, so it shouldn't consume a trade slot.
            risk.record_trade_cancelled(conn)
        elif change.kind == "entry_synced":
            # The fill came back at a different price from the one we asked
            # for. If it moved far enough that the stop is now on the wrong
            # side, the bracket is broken and will close itself instantly.
            # This is exactly what happened to trade #1 and nothing noticed.
            pos = tracker.get_position(conn, change.position_id)
            broken = pos.stop_price is not None and (
                pos.stop_price >= pos.entry_price if not pos.is_short
                else pos.stop_price <= pos.entry_price)
            if broken:
                wrong = "above" if not pos.is_short else "below"
                await channel.send(content="@here", embed=embed(
                    f"⚠️ Broken stop — {pos.ticker}",
                    f"Filled at **`{pos.entry_price:g}`** but the stop sits "
                    f"at **`{pos.stop_price:g}`** — *{wrong}* the entry on a "
                    f"{pos.direction}.\n\n"
                    f"A stop on the wrong side of the entry triggers "
                    f"immediately. This trade will close itself for roughly "
                    f"the spread.\n\n"
                    f"Cause is a fill far from the requested price. The "
                    f"feed-disagreement guard should prevent it; if you're "
                    f"seeing this, the price moved after the check.",
                    "urgent"))

        severity = "info"
        if change.kind == "closed":
            severity = "good" if change.pnl_gbp >= 0 else "urgent"

        await channel.send(embed=embed(
            "Trade closed" if change.kind == "closed" else "Order update",
            reconcile.describe(change), severity))

        if change.kind == "closed":
            await _postmortem(channel, change)

    # A close may have shut a gate. Say so once, here, rather than letting
    # the next silent skip look like a malfunction.
    if any(c.kind == "closed" for c in changes):
        verdict = risk.check(conn)
        if not verdict.allowed:
            await channel.send(content="@here", embed=embed(
                "Trading stopped for today", verdict.reason
                + "\n\nThis is the risk limit doing its job. It resets "
                  "tomorrow.", "warn"))


async def _postmortem(channel, change) -> None:
    """
    Ask for one takeaway on a trade that just closed, and store it.

    Runs AFTER the ledger has been updated and the close has been posted,
    and swallows everything. A failed post-mortem is a missing journal
    line; it must never be able to interfere with recording the trade.

    Note what is NOT here: the takeaway does not feed back into anything
    until 20 trades have closed. Below that, ai_log.similar_losses returns
    nothing and the prompt says so outright. The lessons still get written
    down from trade one — they just do not get to influence anything while
    the sample is too small to mean much.
    """
    if not (ai.enabled() and CONFIG.ai.postmortem_enabled):
        return

    try:
        pos = await asyncio.to_thread(tracker.get_position, conn,
                                      change.position_id)

        context = None
        if pos.context_json:
            try:
                context = json.loads(pos.context_json)
            except (ValueError, TypeError):
                context = None

        takeaway = await asyncio.to_thread(
            ai.postmortem, pos, change.pnl_gbp,
            change.exit_reason or "", context)

        if not takeaway.ok:
            log.warning("post-mortem unavailable for #%s: %s",
                        change.position_id, takeaway.failure)
            return

        await asyncio.to_thread(
            ai_log.record_postmortem, conn, pos.id, pos.ticker, takeaway,
            change.pnl_gbp, change.exit_reason or "")

        # "No lesson" is recorded but not announced. Posting "nothing to
        # learn from that one" after every trade would train Bob to ignore
        # the ones that do say something.
        if takeaway.has_lesson:
            await channel.send(embed=embed(
                f"Post-mortem — {pos.ticker}",
                f"{takeaway.text}\n\n"
                f"*One trade is a very small sample. Stored in the journal; "
                f"`!lessons` shows what has come up more than once.*",
                "info"))

    except Exception:  # noqa: BLE001 — a journal entry is never load-bearing
        log.exception("post-mortem failed for #%s", change.position_id)


async def _end_of_day(channel, state) -> None:
    """
    The daily roll and the Friday flatten.

    Forex has no bell, so there is nothing to be flat for four nights a
    week — a stop sits at the broker all night and the market never gaps
    past it because it never stops trading. The one night that is not true
    is Friday, and that is the only night this closes positions.

    The daily summary still fires every day, after the 17:00 New York roll,
    because that is when OANDA's trading day ends and the day's numbers
    stop moving.
    """
    s = CONFIG.sessions
    now_et = state.now_et
    day = now_et.strftime("%Y-%m-%d")
    hhmm = now_et.strftime("%H:%M")

    # --- the weekend flatten ---------------------------------------------
    if (s.flatten_before_weekend
            and now_et.weekday() == s.week_close_day
            and s.weekend_flatten <= hhmm < s.week_close
            and tracker.open_positions(conn)
            and tracker.claim_once(conn, f"weekend_flatten:{day}")):
        await _flatten_for_the_weekend(channel)

    # --- summary ----------------------------------------------------------
    if hhmm >= s.eod_summary and tracker.claim_once(conn, f"eod_summary:{day}"):
        prices = {}
        opens = tracker.open_positions(conn)
        if opens:
            try:
                prices = await asyncio.to_thread(
                    market.get_prices, [p.ticker for p in opens])
            except market.MarketError:
                pass
        body = await asyncio.to_thread(report.format_eod, conn, prices)
        await channel.send(embed=embed(
            f"End of day — {now_et.strftime('%A %d %B')}", body, "info"))

    # --- weekly review ----------------------------------------------------
    # The slow loop. Reads the week's ledger, scores it, clusters the losses,
    # proposes nothing it can't evidence. Fires once a week after the close.
    if (now_et.weekday() == CONFIG.review.review_weekday
            and hhmm >= s.eod_summary
            and tracker.claim_once(conn, f"weekly_review:{day}")):
        await _post_review(channel)


async def _flatten_for_the_weekend(channel) -> None:
    """
    Close everything before the Friday 17:00 close.

    This is the one night the gap risk is real. Forex shuts for roughly 48
    hours and reopens wherever the weekend's news has put it — an election,
    a central bank, a war. Price can arrive Sunday evening a long way from
    where it left off, with nothing traded in between and a stop that never
    had the chance to fire.

    The share bot did this every night. That was necessary then and is not
    now: a stop held by OANDA sits there through Tuesday night perfectly
    well, and closing a working trade at 16:30 every day for no reason
    would have thrown away every winner that needed longer than a session.
    """
    positions = tracker.open_positions(conn)
    if not positions:
        return

    try:
        await asyncio.to_thread(execution.cancel_all)
        await asyncio.to_thread(execution.close_all_positions)
    except execution.ExecutionError as e:
        await channel.send(content="@here", embed=embed(
            "Weekend close FAILED",
            f"{e}\n\n**Close your positions manually at OANDA now.** "
            f"Anything left open is exposed to the weekend gap, which a "
            f"stop cannot protect against.", "urgent"))
        return

    try:
        prices = await asyncio.to_thread(
            market.get_prices, [p.ticker for p in positions])
    except market.MarketError:
        prices = {}

    lines = []
    for pos in positions:
        price = prices.get(pos.ticker) or pos.entry_price
        closed = tracker.close_position_by_id(conn, pos.id, price, "[weekend]")
        pnl_gbp = fx.pnl_to_gbp(closed.realised(), closed.ticker)
        risk.record_trade_closed(conn, pnl_gbp)
        mark = "🟢" if pnl_gbp >= 0 else "🔴"
        lines.append(f"{mark} **{pos.ticker}** {pos.qty:+,.0f} units closed "
                     f"at `{price:g}` · **£{pnl_gbp:,.2f}**")

    await channel.send(embed=embed(
        "Flat for the weekend",
        "\n".join(lines)
        + "\n\n*Closed before the Friday 17:00 New York close. The market "
          "is shut for about 48 hours and reopens wherever the weekend's "
          "news puts it — a stop cannot protect against a gap.*",
        "info"))


async def _check_open_positions(channel) -> None:
    positions = tracker.open_positions(conn)
    if not positions:
        return
    if not await asyncio.to_thread(market.market_is_open):
        return

    prices = await asyncio.to_thread(
        market.get_prices, [p.ticker for p in positions])

    for pos in positions:
        price = prices.get(pos.ticker)
        if price is None:
            continue
        tracker.update_peak(conn, pos.id, price)
        fresh = tracker.get_position(conn, pos.id)

        # [BOOK p47] Once the trade is far enough in profit, move the stop
        # to the entry price. From then on it cannot lose. The book gives
        # the idea; the trigger point is a choice in config.
        #
        # This now moves the stop AT THE BROKER as well. On Alpaca it could
        # only ever move the bot's own record, which meant the message
        # "this trade can no longer lose money" was not true — the real
        # stop was still sitting at the original level. OANDA lets the
        # actual order be modified, so the claim and the reality match.
        at_breakeven = (fresh.stop_price is not None and (
            fresh.stop_price < fresh.entry_price if not fresh.is_short
            else fresh.stop_price > fresh.entry_price))

        if CONFIG.cyfer.breakeven_enabled and at_breakeven:
            new_stop = cyfer.breakeven_stop(
                fresh.entry_price, fresh.stop_price, price,
                "bearish" if fresh.is_short else "bullish")
            if new_stop is not None:
                moved_at_broker = False
                if fresh.broker_order_id:
                    moved_at_broker = await asyncio.to_thread(
                        execution.modify_stop, fresh.broker_order_id,
                        new_stop, fresh.ticker)

                tracker.set_stop(conn, fresh.ticker, new_stop)
                fresh = tracker.get_position(conn, pos.id)

                if moved_at_broker:
                    footer = ("The stop was moved **at OANDA**, not just "
                              "here — it holds even if this bot stops "
                              "running.")
                elif fresh.broker_order_id:
                    footer = ("⚠️ The broker **refused** the change, so the "
                              "original stop is still the live one. This "
                              "trade can still lose. Move it by hand at "
                              "OANDA if you want it protected.")
                else:
                    footer = ("This is a hand-logged position, so only my "
                              "own record moved. Change the real stop at "
                              "your broker yourself.")

                await channel.send(embed=embed(
                    f"Stop moved to break-even — {fresh.ticker}",
                    f"Price reached `{price:g}`, "
                    f"{CONFIG.cyfer.breakeven_at_r:.0f}R "
                    f"{'below' if fresh.is_short else 'above'} entry "
                    f"`{fresh.entry_price:g}`.\n\n"
                    f"Stop moved to the entry price.\n\n"
                    f"*{footer}*",
                    "good" if moved_at_broker or not fresh.broker_order_id
                    else "warn"))

        for alert in tracker.evaluate(fresh, price):
            if tracker.already_fired(conn, pos.id, alert.kind):
                continue
            tracker.mark_fired(conn, pos.id, alert.kind)
            msg = _gbp_alert(alert, fresh, price)
            await channel.send(
                content="@here" if alert.severity == "urgent" else None,
                embed=embed(alert.ticker, msg, alert.severity))


def _gbp_alert(alert, pos, price: float) -> str:
    """Re-express a tracker alert in GBP."""
    pnl_gbp = fx.pnl_to_gbp(pos.unrealised(price), pos.ticker)
    pct = pos.unrealised_pct(price)
    body = alert.message.split("\n\n")[0]
    tail = "\n\n".join(alert.message.split("\n\n")[1:])
    return (f"{body}\n\n"
            f"Position: **£{pnl_gbp:,.2f}** ({pct:+.2f}%)\n\n{tail}")


@scan_loop.before_loop
async def before_scan():
    await bot.wait_until_ready()


# ===========================================================================
# Commands — positions
# ===========================================================================

@bot.event
async def on_command_error(ctx, error):
    """
    Never fail silently.

    A command that raises inside discord.py posts nothing at all — you type
    something and get no reply, with no way to tell a broken command from a
    mistyped one. That cost a round trip finding a threading bug that would
    otherwise have surfaced tomorrow, mid-trade, as auto-execution quietly
    doing nothing.
    """
    if isinstance(error, commands.CommandNotFound):
        return

    original = getattr(error, "original", error)
    log.exception("command '%s' failed", ctx.invoked_with, exc_info=original)

    await ctx.send(embed=embed(
        f"`!{ctx.invoked_with}` failed",
        f"```{type(original).__name__}: {str(original)[:400]}```\n"
        f"This is a bug, not something you did wrong. The full trace is in "
        f"`journalctl -u cyferbot -n 50 --no-pager`.",
        "urgent"))


@bot.command(name="bought", aliases=["b", "buy"])
async def cmd_bought(ctx, ticker: str = None, qty: str = None,
                     price: str = None, *rest):
    """!bought EURUSD 2500 1.10000 stop=1.09500 target=1.11000

    A NEGATIVE qty is a short: `!b EURUSD -2500 1.10000 stop=1.10500`.
    """
    # Price is optional — leave it off and the live price is used.
    if ticker and qty and not price:
        try:
            price = str(await asyncio.to_thread(market.get_price, ticker))
        except market.MarketError as e:
            await ctx.send(embed=embed("Couldn't get a price", str(e), "warn"))
            return

    if not all([ticker, qty, price]):
        await ctx.send(embed=embed(
            "Log a trade",
            "`!bought EURUSD 2500 1.10000 stop=1.09500 target=1.11000`\n\n"
            "Units, then the pair's own price. A **negative** quantity is a "
            "short:\n`!b EURUSD -2500 1.10000 stop=1.10500`\n\n"
            "Profit and loss come back in GBP whatever the pair settles "
            "in.\n\n"
            "Not sure what size? `!size EURUSD 1.10000 1.09500` works it "
            "out from your risk limit first.", "info"))
        return

    state = sessions.current_state()
    verdict = risk.check(conn)

    try:
        kw = {}
        for p in rest:
            for k in ("stop", "target", "trail"):
                if p.lower().startswith(f"{k}="):
                    kw[k] = float(p.split("=", 1)[1].rstrip("%"))

        pos = await asyncio.to_thread(
            tracker.open_position, conn, ticker, float(qty), float(price),
            kw.get("stop"), kw.get("target"), kw.get("trail"))
    except (ValueError, KeyError) as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return

    risk.record_trade_opened(conn)

    # Attach the setup that fired for this ticker, if there was one.
    cached = _recent_setups.get(pos.ticker)
    if cached:
        setup, phase = cached
        memory.record_context(
            conn, pos.id, "cyfer", setup.direction, setup.score,
            setup.total, setup.conditions_met, setup.conditions_missing,
            phase)
    else:
        memory.record_context(conn, pos.id, "manual")

    cost_gbp = fx.pnl_to_gbp(pos.cost_basis, pos.ticker)
    lines = [f"**{pos.qty:g} {pos.ticker}** @ ${pos.entry_price:,.2f}",
             f"Cost: **£{cost_gbp:,.2f}**"]

    if pos.stop_price:
        risk_gbp = abs(fx.pnl_to_gbp((pos.stop_price - pos.entry_price) * pos.qty, pos.ticker))
        risk_pct_acct = risk_gbp / CONFIG.display.account_gbp * 100
        lines.append(f"Stop ${pos.stop_price:,.2f} — risking **£{risk_gbp:,.2f}** "
                     f"({risk_pct_acct:.2f}% of account)")
        if risk_pct_acct > CONFIG.risk.risk_per_trade_pct * 1.5:
            lines.append(f"\n⚠️ That's above your "
                         f"{CONFIG.risk.risk_per_trade_pct}% limit.")

    if pos.target_price:
        rew_gbp = abs(fx.pnl_to_gbp((pos.target_price - pos.entry_price) * pos.qty, pos.ticker))
        lines.append(f"Target ${pos.target_price:,.2f} — **£{rew_gbp:,.2f}**")

    if pos.stop_price and pos.target_price:
        rr = ((pos.target_price - pos.entry_price) /
              (pos.entry_price - pos.stop_price))
        lines.append(f"Risk/reward **1 : {rr:.2f}**")

    if not pos.stop_price and not pos.trail_pct:
        lines.append(f"\n⚠️ **No stop.** Maximum loss is £{cost_gbp:,.2f} — "
                     f"all of it. `!stop {pos.ticker} <price>`")

    if not state.can_enter:
        lines.append(f"\n🕒 Logged, but note: {state.reason}")
    if not verdict.allowed:
        lines.append(f"\n{verdict.reason}")

    lines.append(f"\n*{fx.rate_note()}*")
    await ctx.send(embed=embed(f"Logged: {pos.ticker}", "\n".join(lines), "good"))


@bot.command(name="sold", aliases=["s", "sell"])
async def cmd_sold(ctx, ticker: str = None, price: str = None):
    if not ticker:
        await ctx.send(embed=embed(
            "Log a close", "`!sold EURUSD 1.10520`", "info"))
        return
    try:
        exit_price = (float(price) if price
                      else await asyncio.to_thread(market.get_price, ticker))
        pos = await asyncio.to_thread(tracker.close_position, conn,
                                      ticker, exit_price)
    except (KeyError, ValueError, market.MarketError) as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return

    pnl_gbp = fx.pnl_to_gbp(pos.realised(), pos.ticker)
    pct = pos.return_pct or 0.0          # signed by direction
    won = pnl_gbp >= 0

    risk.record_trade_closed(conn, pnl_gbp)
    verdict = risk.check(conn)

    body = (f"**{pos.direction.upper()} {abs(pos.qty):,.0f} "
            f"{pos.ticker}**\n"
            f"`{pos.entry_price:g}` → `{pos.exit_price:g}`\n"
            f"**{'Profit' if won else 'Loss'}: £{abs(pnl_gbp):,.2f}** ({pct:+.2f}%)")

    body += ("\n\nBanked. Write down what you did right while it's fresh."
             if won else
             "\n\nTaken as planned. A loss you sized for is the cost of "
             "doing business.")

    if not verdict.allowed:
        body += f"\n\n{verdict.reason}"

    await ctx.send(embed=embed(f"Closed: {pos.ticker}", body,
                               "good" if won else "warn"))


@bot.command(name="size", aliases=["z", "sz"])
async def cmd_size(ctx, ticker: str = None, entry: str = None,
                   stop: str = None):
    """!size EURUSD 1.10000 1.09500 — how many units your risk limit allows."""
    if not all([ticker, entry, stop]):
        await ctx.send(embed=embed(
            "Work out position size",
            "`!size EURUSD 1.10000 1.09500`\n\n"
            "Pair, entry price, stop price. I'll tell you how many units "
            f"keep the loss inside your "
            f"{CONFIG.risk.risk_per_trade_pct}% limit.\n\n"
            "Put the stop **above** the entry and it sizes a short — the "
            "direction is read from your levels, not asked for.", "info"))
        return

    try:
        t = await asyncio.to_thread(risk.size_trade, float(entry),
                                    float(stop), ticker)
    except ValueError as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return

    import pairs
    pair = pairs.parse(ticker)
    d = pair.displayed_decimals
    signed = t.units if t.direction == "long" else -t.units

    await ctx.send(embed=embed(
        f"Size — {pair.display}",
        risk.format_sizing(t) +
        f"\n\nIf you take it: `!b {pair.symbol} {signed} "
        f"{t.entry:.{d}f} stop={t.stop:.{d}f} target={t.target:.{d}f}`",
        "info"))


@bot.command(name="calc", aliases=["c", "whatif"])
async def cmd_calc(ctx, stake: str = None, ticker: str = None,
                   stop: str = None, target: str = None):
    """
    !c 100 EURUSD 1.0950 — what does this trade make or lose?

    The share version asked "what does £100 buy". That question does not
    have a forex answer: you do not put £100 into EUR/USD, you risk £10 on
    a stop 50 pips away and the position size follows from those two
    numbers. So on forex the first argument is what you are willing to
    LOSE, not what you are putting in — and the command says so rather
    than quietly reinterpreting it.
    """
    forex = execution.broker_name() == "oanda"

    if not stake:
        await ctx.send(embed=embed(
            "What could I make or lose?",
            ("`!c 10 EURUSD 1.09500` — risk £10 with a stop at 1.09500\n"
             "`!c 10 EURUSD 1.09500 1.11000` — with your own target\n\n"
             "The first number is what you'd **lose** if the stop is hit, "
             "not what you're putting in. Leave the target off and I use "
             f"{CONFIG.cyfer.min_risk_reward:g}:1.\n\n"
             "*On forex, size follows from the risk and the stop. There is "
             "no 'how much does £100 buy'.*")
            if forex else
            ("`!c 100 SPY` — £100 into SPY at the live price\n"
             "`!c 100 SPY 508` — with your own stop\n"
             "`!c 100 SPY 508 530` — with your own stop and target"),
            "info"))
        return

    try:
        stake_gbp = float(stake.lstrip("£$"))
    except ValueError:
        await ctx.send(embed=embed("That didn't work",
                                   f"'{stake}' isn't a number.", "warn"))
        return

    ticker = (ticker or CONFIG.instruments.primary).upper()

    try:
        entry = await asyncio.to_thread(market.get_price, ticker)
    except market.MarketError as e:
        await ctx.send(embed=embed("Couldn't get a price", str(e), "warn"))
        return

    if forex:
        import pairs
        pair = pairs.parse(ticker)
        if not stop:
            await ctx.send(embed=embed(
                "I need a stop",
                f"Without one there is no risk to size against, and the "
                f"size is the whole answer.\n\n"
                f"`!c {stake_gbp:g} {pair.symbol} <stop price>` — "
                f"{pair.display} is at `{entry:g}` now.", "warn"))
            return
        try:
            t = await asyncio.to_thread(
                risk.size_trade, entry, float(stop), ticker,
                account_gbp=stake_gbp * 100 / CONFIG.risk.risk_per_trade_pct,
                target=float(target) if target else None)
        except ValueError as e:
            await ctx.send(embed=embed("That didn't work", str(e), "warn"))
            return
        await ctx.send(embed=embed(
            f"Risking £{stake_gbp:,.2f} on {pair.display}",
            risk.format_sizing(t)
            + "\n\n*This sizes to the risk you named, ignoring your "
              "configured account limit — it answers 'what if', not "
              "'should I'.*",
            "info"))
        return

    stop_usd = float(stop) if stop else entry * 0.99
    target_usd = float(target) if target else None

    try:
        o = await asyncio.to_thread(
            calc.outcomes, stake_gbp, entry, stop_usd, target_usd,
            CONFIG.cyfer.min_risk_reward)
    except ValueError as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return

    await ctx.send(embed=embed(f"£{stake_gbp:,.0f} in {ticker}",
                               calc.format_outcomes(o, ticker), "info"))


@bot.command(name="daily", aliases=["d"])
async def cmd_daily(ctx, ticker: str = None):
    """!d SPY — check the Graystone daily setup right now. Shares only."""
    # Graystone's daily method is a SHARES method, and the S&P 500 list it
    # scans does not exist on a forex feed. Rather than fail with a
    # confusing "unknown instrument", say what is actually going on.
    if execution.broker_name() == "oanda":
        await ctx.send(embed=embed(
            "Shares only",
            "Graystone's daily rules run on the S&P 500, and this bot is "
            "pointed at OANDA, which trades currencies.\n\n"
            "To use it, set `BROKER=alpaca` and `DATA_SOURCE=yahoo` in "
            "`.env` and restart — that switches the whole bot back to "
            "shares, including the strategy.\n\n"
            "*The forex scan is `!chart`.*", "info"))
        return

    ticker = (ticker or CONFIG.instruments.primary).upper()
    try:
        bars = await asyncio.to_thread(market.get_bars, ticker, "1Day", 120)
    except market.MarketError as e:
        await ctx.send(embed=embed("Couldn't get data", str(e), "warn"))
        return

    sig = graystone.scan(ticker, bars)
    if sig is None:
        await ctx.send(embed=embed(
            f"{ticker} — no daily setup",
            "Graystone's conditions aren't met on the latest daily candle.\n"
            "Either the 8/20/50 EMAs aren't stacked, the candle body isn't "
            "in the right 30%, or it never reached the 8 EMA.\n\n"
            "*No setup is the normal state. Most days there isn't one.*",
            "info"))
        return

    shares = risk_gbp = reward_gbp = None
    try:
        t = await asyncio.to_thread(risk.size_trade, sig.entry, sig.stop,
                                    ticker)
        shares, risk_gbp, reward_gbp = t.shares, t.risk_gbp, t.reward_gbp
    except ValueError:
        pass

    await ctx.send(embed=embed(
        f"Daily setup — {ticker}",
        graystone.format_signal(sig, shares, risk_gbp, reward_gbp), "setup"))


@bot.command(name="scan", aliases=["sc"])
async def cmd_scan(ctx, limit: str = None):
    """!scan — Graystone's daily rules across the S&P 500. Shares only."""
    # Graystone's daily method is a SHARES method, and the S&P 500 list it
    # scans does not exist on a forex feed. Rather than fail with a
    # confusing "unknown instrument", say what is actually going on.
    if execution.broker_name() == "oanda":
        await ctx.send(embed=embed(
            "Shares only",
            "Graystone's daily rules run on the S&P 500, and this bot is "
            "pointed at OANDA, which trades currencies.\n\n"
            "To use it, set `BROKER=alpaca` and `DATA_SOURCE=yahoo` in "
            "`.env` and restart — that switches the whole bot back to "
            "shares, including the strategy.\n\n"
            "*The forex scan is `!chart`.*", "info"))
        return

    tickers = watchlist.all_tickers()

    try:
        max_show = int(limit) if limit else 12
    except ValueError:
        max_show = 12

    notice = await ctx.send(embed=embed(
        "Scanning…",
        f"Checking **{len(tickers)}** tickers against Graystone's daily rules.\n"
        f"Takes 30–60 seconds — it fetches in batches.",
        "info"))

    try:
        bars_by_ticker = await asyncio.to_thread(
            market.get_bars_multi, tickers, "1Day", 220)
    except market.MarketError as e:
        await notice.edit(embed=embed("Scan failed", str(e), "warn"))
        return

    hits = []
    for ticker, bars in bars_by_ticker.items():
        try:
            sig = graystone.scan(ticker, bars)
        except Exception:  # noqa: BLE001
            continue
        if sig:
            hits.append(sig)

    missing = len(tickers) - len(bars_by_ticker)

    if not hits:
        await notice.edit(embed=embed(
            "No setups today",
            f"Scanned **{len(bars_by_ticker)}** tickers. None meet all of "
            f"Graystone's conditions on the latest daily candle.\n\n"
            f"*This is the normal result. His rules are strict — EMAs stacked, "
            f"body in the right 30%, and a touch of the 8 EMA. Most days most "
            f"stocks fail at least one.*"
            + (f"\n\n{missing} tickers returned no data and were skipped."
               if missing else ""),
            "info"))
        return

    # Strongest trend first — [JG] "the more that they fan out the higher
    # probability that the trend is strengthening"
    hits.sort(key=lambda x: x.fan_width_pct, reverse=True)

    lines = []
    for sig in hits[:max_show]:
        arrow = "▲" if sig.direction == "bullish" else "▼"
        try:
            t = await asyncio.to_thread(risk.size_trade, sig.entry,
                                        sig.stop, sig.ticker) \
                if sig.direction == "bullish" else None
            size_txt = (f" · {t.shares:g} sh, risk £{t.risk_gbp:,.2f}"
                        if t and t.shares else "")
        except ValueError:
            size_txt = ""

        lines.append(
            f"{arrow} **{sig.ticker}** — entry ${sig.entry:,.2f}, "
            f"stop ${sig.stop:,.2f}, target ${sig.target:,.2f}\n"
            f"    fan {sig.fan_width_pct:.1f}%{size_txt}"
        )

    body = "\n".join(lines)
    more = (f"\n\n*…and {len(hits) - max_show} more. "
            f"`!scan {len(hits)}` to see all.*" if len(hits) > max_show else "")

    await notice.edit(embed=embed(
        f"{len(hits)} daily setups",
        f"Scanned {len(bars_by_ticker)} tickers, sorted by trend strength.\n\n"
        + body + more +
        "\n\n`!d TICKER` for the full reasoning on any one of them."
        "\n\n*Conditions met on the daily candle — not a prediction, and "
        "this hasn't been backtested either.*",
        "setup"))


@bot.command(name="learn", aliases=["l", "why"])
async def cmd_learn(ctx):
    """!l — what the logged trades actually show, once there are enough."""
    closed = tracker.closed_positions(conn)
    result = await asyncio.to_thread(memory.analyse, conn, closed)
    await ctx.send(embed=embed(
        "What the data says",
        memory.format_analysis(result),
        "good" if result["enough_data"] else "info"))


@bot.command(name="mode", aliases=["m"])
async def cmd_mode(ctx):
    """!m — paper or live, armed or not, auto on or off."""
    await ctx.send(embed=embed(
        "Trading mode",
        execution.describe_mode(conn) +
        "\n\n`!auto on` / `!auto off` — toggle auto-execute\n"
        "`!halt` — cancel everything and switch auto off",
        "urgent" if (execution.is_live() and execution.live_armed(conn))
        else "info"))


@bot.command(name="auto")
async def cmd_auto(ctx, setting: str = None):
    """!auto on — let the bot place orders itself."""
    if setting is None:
        await ctx.send(embed=embed("Auto-execute",
                                   execution.describe_mode(conn), "info"))
        return

    on = setting.strip().lower() in ("on", "yes", "true", "1")
    execution.set_auto(conn, on)

    if not on:
        await ctx.send(embed=embed("Auto-execute OFF",
                                   "Back to alerts only. Existing orders are "
                                   "untouched — `!halt` cancels those.", "good"))
        return

    live = execution.is_live() and execution.live_armed(conn)
    await ctx.send(embed=embed(
        "Auto-execute ON",
        f"{execution.describe_mode(conn)}\n\n"
        f"Orders fire only on **6/6 conditions**, inside the session window, "
        f"and only if every risk gate passes.\n\n"
        + ("🔴 **This spends real money.** `!halt` stops everything."
           if live else
           "Paper money — nothing real at risk."),
        "urgent" if live else "good"))


@bot.command(name="arm")
async def cmd_arm(ctx, *, phrase: str = None):
    """Arm live trading. Deliberately awkward."""
    if not execution.is_live():
        await ctx.send(embed=embed(
            "Not in live mode",
            "`TRADING_MODE` is `paper`, so there's nothing to arm.\n\n"
            "To go live you'd change it in `.env`, add live API keys, "
            "restart, then arm. Four separate acts, on purpose.", "info"))
        return

    if phrase is None:
        await ctx.send(embed=embed(
            "Arm live trading",
            f"This lets the bot spend **real money** with no further "
            f"confirmation.\n\nType exactly:\n"
            f"```\n!arm {execution.LIVE_CONFIRM_PHRASE}\n```\n"
            f"`!disarm` revokes it instantly.", "urgent"))
        return

    if execution.arm_live(conn, phrase):
        await ctx.send(embed=embed(
            "🔴 LIVE TRADING ARMED",
            "Orders will now spend real money.\n\n"
            "`!disarm` · `!halt` · `!auto off`", "urgent"))
    else:
        await ctx.send(embed=embed(
            "Not armed",
            "That phrase didn't match exactly. Nothing changed.", "warn"))


@bot.command(name="disarm")
async def cmd_disarm(ctx):
    execution.disarm_live(conn)
    await ctx.send(embed=embed("Disarmed",
                               "Live orders blocked. Open orders untouched — "
                               "`!halt` cancels those.", "good"))


@bot.command(name="halt", aliases=["stop_all", "panic"])
async def cmd_halt(ctx):
    """!halt — the kill switch."""
    execution.set_auto(conn, False)
    execution.disarm_live(conn)

    lines = ["Auto-execute **OFF**", "Live trading **disarmed**"]

    try:
        n = await asyncio.to_thread(execution.cancel_all)
        lines.append(f"Cancelled **{n}** open order(s)")
    except execution.ExecutionError as e:
        lines.append(f"⚠️ Couldn't cancel orders: {e}")

    lines.append("\n*Positions were left open — closing them is a decision, "
                 "not an emergency. `!flat` closes everything if you want that.*")

    await ctx.send(embed=embed("HALTED", "\n".join(lines), "urgent"))


@bot.command(name="flat")
async def cmd_flat(ctx, confirm: str = None):
    """!flat yes — close every open position at market."""
    if confirm != "yes":
        await ctx.send(embed=embed(
            "Close everything?",
            "This closes every open position at market price, taking "
            "whatever price is available.\n\nType `!flat yes` to do it.",
            "warn"))
        return
    if not tracker.open_positions(conn):
        try:
            n = await asyncio.to_thread(execution.close_all_positions)
            await ctx.send(embed=embed(
                "Flat", f"Closed **{n}** position(s) at the broker. "
                        f"Nothing was being tracked here.", "good"))
        except execution.ExecutionError as e:
            await ctx.send(embed=embed("Couldn't close", str(e), "urgent"))
        return

    # Same path as the automatic close, so the tracker and the risk ledger
    # get updated rather than left believing the positions are still open.
    await _flatten_for_the_weekend(ctx.channel)


@bot.command(name="stop", aliases=["sl"])
async def cmd_stop(ctx, ticker: str = None, price: str = None):
    if not ticker or not price:
        await ctx.send(embed=embed(
            "Set a stop", "`!stop EURUSD 1.09500`", "info"))
        return
    try:
        pos = await asyncio.to_thread(tracker.set_stop, conn, ticker, float(price))
    except (KeyError, ValueError) as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return
    risk_gbp = abs(fx.pnl_to_gbp((pos.stop_price - pos.entry_price) * pos.qty, pos.ticker))
    await ctx.send(embed=embed(
        f"Stop set: {pos.ticker}",
        f"${pos.stop_price:,.2f} — maximum loss now **£{risk_gbp:,.2f}**.",
        "good"))


@bot.command(name="target", aliases=["tp"])
async def cmd_target(ctx, ticker: str = None, price: str = None):
    if not ticker or not price:
        await ctx.send(embed=embed(
            "Set a target", "`!target EURUSD 1.11000`", "info"))
        return
    try:
        pos = await asyncio.to_thread(tracker.set_target, conn, ticker, float(price))
    except (KeyError, ValueError) as e:
        await ctx.send(embed=embed("That didn't work", str(e), "warn"))
        return
    rew = abs(fx.pnl_to_gbp((pos.target_price - pos.entry_price) * pos.qty, pos.ticker))
    await ctx.send(embed=embed(f"Target set: {pos.ticker}",
                               f"${pos.target_price:,.2f} — **£{rew:,.2f}** if hit.",
                               "good"))


@bot.command(name="positions", aliases=["p", "pos"])
async def cmd_positions(ctx):
    positions = tracker.open_positions(conn)
    if not positions:
        await ctx.send(embed=embed("No open positions",
                                   "`!b EURUSD 2500 1.10000 stop=1.09500`",
                                   "info"))
        return
    try:
        prices = await asyncio.to_thread(
            market.get_prices, [p.ticker for p in positions])
    except market.MarketError as e:
        await ctx.send(embed=embed("Couldn't fetch prices", str(e), "warn"))
        return

    lines = []
    for p in positions:
        price = prices.get(p.ticker)
        if price is None:
            lines.append(f"**{p.ticker}** — no price")
            continue
        pnl_gbp = fx.pnl_to_gbp(p.unrealised(price), p.ticker)
        pct = p.unrealised_pct(price)
        mark = "🟢" if pnl_gbp >= 0 else "🔴"
        prot = (f"stop ${p.stop_price:,.2f}" if p.stop_price
                else f"trail {p.trail_pct:.0f}%" if p.trail_pct
                else "⚠️ **unprotected**")
        lines.append(f"{mark} **{p.ticker}** {p.qty:g} @ ${p.entry_price:,.2f}\n"
                     f"    now ${price:,.2f} · **£{pnl_gbp:,.2f}** "
                     f"({pct:+.2f}%) · {prot}")

    lines.append(f"\n*{fx.rate_note()}*")
    await ctx.send(embed=embed("Open positions", "\n".join(lines), "info"))


# ===========================================================================
# Commands — state and transparency
# ===========================================================================

@bot.command(name="session", aliases=["ss", "time"])
async def cmd_session(ctx):
    state = sessions.current_state()
    golden = "  ⭐ **golden hours**" if state.is_golden else ""
    body = (f"**{state.et_str} ET / {state.uk_str} UK**\n"
            f"Open now: **{state.centres_str}**{golden}\n"
            f"{state.reason}\n\n"
            f"{'✅ Entries permitted' if state.can_enter else '⛔ No entries'}\n"
            f"Next window: {sessions.next_macro_countdown()}\n"
            f"Week closes in: {sessions.time_until_week_close()}\n\n"
            f"{sessions.session_summary()}")
    await ctx.send(embed=embed("Session clock", body,
                               "good" if state.can_enter else "info"))


@bot.command(name="risk", aliases=["r"])
async def cmd_risk(ctx):
    v = risk.check(conn)
    r = CONFIG.risk
    body = (f"{'✅ Trading allowed' if v.allowed else v.reason}\n\n"
            f"Trades today: **{v.trades_today}/{r.max_trades_per_day}**\n"
            f"P&L today: **£{v.pnl_today_gbp:,.2f}**\n"
            f"Consecutive losses: **{v.consecutive_losses}/"
            f"{r.max_consecutive_losses}**\n\n"
            f"Account £{CONFIG.display.account_gbp:,.2f} · "
            f"{r.risk_per_trade_pct}% per trade "
            f"(£{CONFIG.display.account_gbp * r.risk_per_trade_pct / 100:,.2f})\n"
            f"Daily loss cap {r.max_daily_loss_pct}% "
            f"(£{CONFIG.display.account_gbp * r.max_daily_loss_pct / 100:,.2f})")
    await ctx.send(embed=embed("Risk status", body,
                               "good" if v.allowed else "urgent"))


async def _post_review(channel, days: int = 7) -> None:
    """Build the review and post it, split across embeds if it's long."""
    try:
        text = await asyncio.to_thread(review.build, conn, days)
    except Exception:  # noqa: BLE001
        log.exception("review failed")
        await channel.send(embed=embed(
            "Weekly review failed",
            "The report couldn't be built. Trading is unaffected — this is "
            "read-only analysis. Check `journalctl -u cyferbot -n 50 "
            "--no-pager`.", "warn"))
        return

    parts = review.chunks(text)
    for i, part in enumerate(parts, 1):
        title = ("Weekly Strategy Review" if i == 1
                 else f"Weekly Strategy Review ({i}/{len(parts)})")
        await channel.send(embed=embed(title, part, "info"))


@bot.command(name="review", aliases=["rv", "weekly"])
async def cmd_review(ctx, days: str = None):
    """!review [days] — the full strategy review, read-only."""
    try:
        n = max(1, min(365, int(days))) if days else 7
    except ValueError:
        await ctx.send(embed=embed(
            "How many days?", "`!review` for the last 7 days, or "
            "`!review 30` for a month.", "info"))
        return

    await _post_review(ctx.channel, n)


@bot.command(name="metrics", aliases=["mx"])
async def cmd_metrics(ctx, days: str = None):
    """!metrics [days] — just the benchmark table, no diagnosis."""
    try:
        n = max(1, min(365, int(days))) if days else 7
    except ValueError:
        n = 7

    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=n)).isoformat(timespec="seconds")
    trades = await asyncio.to_thread(tracker.closed_since, conn, cutoff)

    rows = ["| Metric | Value | Target | Status |", "| :-- | :-- | :-- | :-- |"]
    for m in metrics.all_metrics(trades):
        rows.append(f"| {m.name} | {m.fmt()} | {m.fmt_target()} | {m.status} |")

    body = "\n".join(rows)
    settled = metrics.settled(trades)
    floor = CONFIG.review.min_trades_to_diagnose
    if len(settled) < floor:
        body += (f"\n\n⚠️ {len(settled)}/{floor} trades. Every status above "
                 f"is UNRELIABLE — too few trades for any of these to mean "
                 f"anything yet.")
    body += "\n\n`!review` for the full diagnosis."

    await ctx.send(embed=embed(f"Metrics — last {n} days", body, "info"))


@bot.command(name="chart", aliases=["now", "check", "cy"])
async def cmd_chart(ctx, ticker: str = None):
    """
    !chart [EURUSD] — run the strategy right now and show the full
    breakdown, whatever the score. The answer to "why isn't it trading".
    """
    inst, y = CONFIG.instruments, CONFIG.cyfer
    tick = (ticker or inst.primary).upper()

    try:
        bars_htf, bars_ltf = await asyncio.gather(
            asyncio.to_thread(market.get_bars, tick, y.htf, 200),
            asyncio.to_thread(market.get_bars, tick, y.ltf, 60),
        )
    except market.MarketError as e:
        await ctx.send(embed=embed(
            f"{tick} — data feed failed",
            f"```{e}```\nThis is why nothing fires. The scan loop hits the "
            f"same error every minute and logs it silently.", "urgent"))
        return

    counts = (f"Bars received: {y.htf} **{len(bars_htf)}** · "
              f"{y.ltf} **{len(bars_ltf)}**")

    if len(bars_htf) < y.min_htf_bars or len(bars_ltf) < 3:
        await ctx.send(embed=embed(
            f"{tick} — not enough data to scan",
            f"{counts}\n\nNeeds at least **{y.min_htf_bars}** {y.htf} bars "
            f"and **3** {y.ltf} bars. The scanner returns nothing in this "
            f"state — silently.", "urgent"))
        return

    ema_dir = graystone.ema_stack_direction(bars_htf)
    sig = cyfer.scan(tick, bars_htf, bars_ltf, ema_direction=ema_dir)

    if sig is None:
        await ctx.send(embed=embed(f"{tick} — scanner returned nothing",
                                   counts, "urgent"))
        return

    s = CONFIG.strategy
    state = sessions.current_state()
    verdict = risk.check(conn)

    lines = [f"**{sig.score}/{sig.total} conditions aligned "
             f"{sig.direction}**", ""]
    for c in sig.conditions_met:
        lines.append(f"✅ {c}")
    for c in sig.conditions_missing:
        lines.append(f"⬜ {c}")

    # The levels the strategy can actually see, so you can check them
    # against your own chart.
    levels = cyfer.find_levels(bars_htf, timeframe=y.htf)
    if levels:
        price = bars_ltf[-1].close
        near = sorted(levels, key=lambda l: abs(l.price - price))[:4]
        lines += ["", f"**Levels on the {y.htf} chart** "
                      f"(price ${price:,.2f})"]
        for l in near:
            lines.append(f"• {l.kind} ${l.price:,.2f} — {l.touches} "
                         f"rejections, {l.distance_pct(price):.2f}% away")

    lines += ["", f"Alert at **{s.min_alert_score}/{sig.total}** · "
                  f"auto-trade at **{s.min_auto_score}/{sig.total}**"]

    if sig.score >= s.min_auto_score and sig.tradeable:
        lines.append("→ This **would** be traded right now.")
    elif sig.score >= s.min_auto_score:
        lines.append(f"→ Score is high enough but the trade isn't priceable "
                     f"(reward-to-risk {sig.rr:.1f}:1).")
    elif sig.score >= s.min_alert_score:
        lines.append("→ Would alert, but not trade.")
    else:
        lines.append(f"→ Below the alert threshold — silent. Needs "
                     f"{s.min_alert_score - sig.score} more condition(s).")

    lines += ["",
              f"🕒 {state.et_str} ET / {state.uk_str} UK",
              f"Session: {'✅ entries permitted' if state.can_enter else '⛔ ' + state.reason}",
              f"Risk gate: {'✅ open' if verdict.allowed else '⛔ ' + verdict.reason}",
              f"Auto-execute: {'ON' if execution.auto_enabled(conn) else 'OFF'}",
              "", f"*{counts}*"]

    await ctx.send(embed=embed(
        f"Live scan — {tick}", "\n".join(lines),
        "good" if (sig.score >= s.min_auto_score and sig.tradeable) else "info"))


@bot.command(name="pnl", aliases=["pl", "summary"])
async def cmd_pnl(ctx, period: str = None):
    """!pnl [today|week|month|all] — realised profit and loss in GBP."""
    key = (period or "today").lower().strip()
    aliases = {"t": "today", "d": "today", "day": "today", "w": "week",
               "m": "month", "a": "all", "alltime": "all", "total": "all"}
    key = aliases.get(key, key)

    if key not in report.PERIODS:
        await ctx.send(embed=embed(
            "Which period?",
            "`!pnl today` · `!pnl week` · `!pnl month` · `!pnl all`", "info"))
        return

    since_fn, label = report.PERIODS[key]
    t = await asyncio.to_thread(report.tally, conn, since_fn(), label)
    body = report.format_tally(t)

    opens = tracker.open_positions(conn)
    if opens:
        body += (f"\n\n*{len(opens)} position{'s' if len(opens) > 1 else ''} "
                 f"still open — not counted above. `!p` shows them.*")

    await ctx.send(embed=embed(f"P&L — {label}", body,
                               "good" if t.realised_gbp >= 0 else "warn"))


@bot.command(name="eod")
async def cmd_eod(ctx):
    """!eod — the end-of-day summary on demand, without waiting for 21:05."""
    opens = tracker.open_positions(conn)
    prices = {}
    if opens:
        try:
            prices = await asyncio.to_thread(
                market.get_prices, [p.ticker for p in opens])
        except market.MarketError:
            pass
    body = await asyncio.to_thread(report.format_eod, conn, prices)
    await ctx.send(embed=embed("End of day", body, "info"))


@bot.command(name="sync")
async def cmd_sync(ctx):
    """!sync — force a reconciliation against the broker right now."""
    managed = tracker.broker_managed_open(conn)
    if not managed:
        await ctx.send(embed=embed(
            "Nothing to sync",
            "No bot-placed positions are open. Trades you logged by hand "
            "with `!b` aren't tracked at the broker, so there's nothing to "
            "ask "
            "about — close those with `!s`.", "info"))
        return

    try:
        changes = await asyncio.to_thread(reconcile.run, conn)
    except Exception as e:  # noqa: BLE001
        await ctx.send(embed=embed("Sync failed", str(e), "warn"))
        return

    if not changes:
        await ctx.send(embed=embed(
            "Synced — no change",
            f"Checked {len(managed)} order"
            f"{'s' if len(managed) > 1 else ''} at the broker. All still "
            f"open.",
            "info"))
        return

    for change in changes:
        if change.kind == "closed":
            risk.record_trade_closed(conn, change.pnl_gbp)
        elif change.kind == "abandoned":
            risk.record_trade_cancelled(conn)

    await ctx.send(embed=embed(
        "Synced",
        "\n".join(reconcile.describe(c) for c in changes), "info"))


@bot.command(name="ai")
async def cmd_ai(ctx, switch: str = None):
    """!ai — what the AI reviewer is doing. !ai on/off to switch it."""
    if switch and switch.lower() in ("on", "off"):
        # Deliberately not persisted to the database like auto-execute is.
        # Turning the reviewer on needs an API key in .env and a restart
        # anyway, so a Discord toggle that survived a restart would create
        # two sources of truth for one setting.
        await ctx.send(embed=embed(
            "Set this in .env",
            f"Change `AI_ENABLED={switch.lower()}` in `.env` and restart "
            f"with `systemctl restart cyferbot`.\n\n"
            f"It lives there rather than here because it needs an API key "
            f"beside it, and one setting with two homes is how settings "
            f"get out of sync.", "info"))
        return

    body = [ai.describe()]

    st = await asyncio.to_thread(ai_log.stats, conn)
    if st["verdicts"]:
        body += [
            "",
            f"**{st['verdicts']}** verdicts · **{st['blocked']}** blocked "
            f"({st['block_rate']:.0f}%) · **{st['failed']}** had no answer "
            f"({st['fail_rate']:.0f}%)",
            f"Average response {st['avg_latency_ms']:,}ms",
        ]
        if st["postmortems"]:
            body.append(
                f"**{st['postmortems']}** post-mortems, "
                f"**{st['with_lesson']}** found a specific lesson "
                f"(the rest said the loss was within normal variance)")

        rows = await asyncio.to_thread(ai_log.recent_verdicts, conn, 5)
        body += ["", "**Recent**"]
        for r in rows:
            mark = "🛑" if r["blocked"] else ("✅" if r["ok"] else "⚪")
            body.append(f"{mark} `{r['ticker']}` {r['action']} "
                        f"({r['confidence']:.0%}) — "
                        f"{(r['rationale'] or r['failure'])[:90]}")
    else:
        body += ["", "*No verdicts recorded yet.*"]

    body.append(
        "\n*Blocks are logged with the trade they prevented, so this can "
        "be graded later rather than taken on faith.*")

    await ctx.send(embed=embed("AI reviewer", "\n".join(body), "info"))


@bot.command(name="lessons", aliases=["takeaways"])
async def cmd_lessons(ctx, limit: str = None):
    """!lessons — the post-mortem journal."""
    try:
        n = max(1, min(25, int(limit))) if limit else 15
    except ValueError:
        n = 15

    rows = await asyncio.to_thread(
        lambda: list(conn.execute(
            "SELECT * FROM ai_postmortems WHERE has_lesson = 1 "
            "ORDER BY position_id DESC LIMIT ?", (n,))))

    total = await asyncio.to_thread(
        lambda: conn.execute(
            "SELECT COUNT(*) FROM ai_postmortems").fetchone()[0])

    if not rows:
        await ctx.send(embed=embed(
            "No lessons yet",
            f"{total} post-mortem(s) recorded, none of which found a "
            f"specific lesson.\n\n"
            f"*That is the expected result early on, and it is the honest "
            f"one. A losing trade in a strategy with a 50% win rate is not "
            f"evidence of a mistake, and the model is explicitly allowed "
            f"to say so rather than inventing a reason.*", "info"))
        return

    lines = []
    for r in rows:
        mark = "🟢" if r["pnl_gbp"] >= 0 else "🔴"
        lines.append(f"{mark} **{r['ticker']}** (£{r['pnl_gbp']:,.2f}) — "
                     f"{r['takeaway']}")

    silent = total - len(rows)
    await ctx.send(embed=embed(
        "Post-mortem journal",
        "\n".join(lines)
        + (f"\n\n*{silent} other trade(s) produced no lesson — recorded "
           f"as within normal variance.*" if silent > 0 else "")
        + f"\n\n*Written one trade at a time. Anything appearing once is "
          f"an anecdote; `!review` is what looks for patterns across the "
          f"whole ledger with error bars attached.*",
        "info"))


@bot.command(name="update", aliases=["pull"])
async def cmd_update(ctx, confirm: str = None):
    """
    !update — pull the latest code from GitHub and restart.

    The whole point of this command is that updating the bot used to mean
    downloading a file, opening WinSCP, dragging it across, unpacking it in
    a console and restarting the service. Eight manual steps, several of
    which could silently go wrong. This is one message, from a phone.
    """
    import subprocess

    here = Path(__file__).parent

    if not (here / ".git").exists():
        await ctx.send(embed=embed(
            "Not set up yet",
            "This folder isn't connected to GitHub, so there's nothing to "
            "pull from.\n\nThe one-time setup is in `UPDATING.md` — about "
            "ten minutes, and then updating is just this command.", "warn"))
        return

    if confirm != "yes":
        try:
            out = await asyncio.to_thread(
                subprocess.run,
                ["git", "fetch", "origin", "--quiet"], cwd=here,
                capture_output=True, text=True, timeout=60)
            diff = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "--oneline", "HEAD..@{u}"], cwd=here,
                capture_output=True, text=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            await ctx.send(embed=embed("Couldn't reach GitHub", str(e), "warn"))
            return

        pending = (diff.stdout or "").strip()
        if not pending:
            await ctx.send(embed=embed(
                "Already up to date", "Nothing new to pull.", "info"))
            return

        await ctx.send(embed=embed(
            "Ready to update",
            f"```\n{pending[:1500]}\n```\n"
            f"Type `!update yes` to pull this and restart.\n\n"
            f"*Your `.env` and trade history aren't touched — git doesn't "
            f"track them.*", "info"))
        return

    await ctx.send(embed=embed(
        "Updating…", "Pulling, checking the code parses, then restarting. "
        "Back in about 10 seconds.", "info"))

    try:
        result = await asyncio.to_thread(
            subprocess.run, ["bash", str(here / "update.sh")],
            capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        await ctx.send(embed=embed("Update failed", str(e), "urgent"))
        return

    tail = ((result.stdout or "") + (result.stderr or "")).strip()[-1500:]

    if result.returncode != 0:
        # update.sh refuses to restart on a syntax error, so the bot is
        # still running the OLD code here — which is the safe outcome.
        await ctx.send(embed=embed(
            "Update refused — still on the old code",
            f"```\n{tail}\n```\n"
            f"*Nothing was restarted, and the files were rolled back — the "
            f"bot is still running the previous version and will boot into "
            f"it cleanly after a reboot too.*", "urgent"))
        return

    # If the restart worked, this process is about to be replaced, so the
    # startup message in Discord is the real confirmation.
    await ctx.send(embed=embed(
        "Updated", f"```\n{tail}\n```\n"
        f"*Watch for the startup message — that's the new code booting.*",
        "good"))


@bot.command(name="version", aliases=["ver"])
async def cmd_version(ctx):
    """!version — exactly which code is running right now."""
    import subprocess

    here = Path(__file__).parent
    lines = []

    if (here / ".git").exists():
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "-1", "--format=%h — %s (%cr)"], cwd=here,
                capture_output=True, text=True, timeout=20)
            lines.append(f"**Code:** `{(r.stdout or '?').strip()}`")
            d = await asyncio.to_thread(
                subprocess.run, ["git", "status", "--porcelain"], cwd=here,
                capture_output=True, text=True, timeout=20)
            if (d.stdout or "").strip():
                lines.append("⚠️ Files have been edited directly on the VPS. "
                             "`!update` will discard those changes.")
        except Exception:  # noqa: BLE001
            lines.append("**Code:** couldn't read git")
    else:
        lines.append("**Code:** not connected to GitHub — see `UPDATING.md`")

    lines += [
        f"**Broker:** {execution.broker_name()} "
        f"({execution.trading_mode()})",
        f"**Data:** {market.SOURCE_NAME}"
        + (f"  ⚠️ *{market.DATA_SOURCE_OVERRIDDEN}*"
           if market.DATA_SOURCE_OVERRIDDEN else ""),
        f"**AI review:** {'on' if ai.enabled() else 'off'}",
        f"**Watching:** {', '.join(CONFIG.instruments.watchlist)}",
    ]
    await ctx.send(embed=embed("Version", "\n".join(lines), "info"))


_backtest_running = False


@bot.command(name="backtest", aliases=["bt"])
async def cmd_backtest(ctx, weeks: str = None):
    """
    !backtest [weeks] — replay the strategy over past OANDA prices.

    Runs backtest.py as a SEPARATE program rather than inside the bot. It
    runs the strategy tens of thousands of times, and doing that in here
    would compete with the live scan loop for the same processor. As its
    own process it can't slow the bot down, and it never touches the trade
    database — it only reads price history.
    """
    import sys
    import backtest as bt

    global _backtest_running
    if _backtest_running:
        await ctx.send(embed=embed(
            "Already running", "One backtest at a time — it'll post when "
            "it's done.", "info"))
        return

    try:
        n = int(weeks) if weeks else bt.DEFAULT_WEEKS
    except ValueError:
        await ctx.send(embed=embed(
            "That didn't work", f"`!backtest 12` — weeks as a number, not "
            f"'{weeks}'.", "warn"))
        return
    n = max(1, min(n, bt.MAX_WEEKS))

    _backtest_running = True
    try:
        await ctx.send(embed=embed(
            f"Backtest running — last {n} weeks",
            f"Fetching {n} weeks of OANDA prices for all four pairs, then "
            f"replaying the strategy five minutes at a time, exactly as the "
            f"live bot would have traded it.\n\n"
            f"Usually under two minutes. The live bot keeps running "
            f"normally while this works.", "info"))

        here = Path(__file__).parent
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(here / "backtest.py"), str(n),
            cwd=str(here),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            await ctx.send(embed=embed(
                "Backtest took too long",
                "Stopped after 15 minutes. Try fewer weeks: `!backtest 4`.",
                "warn"))
            return

        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not text:
            tail = (err or b"").decode("utf-8", "replace").strip()[-900:]
            await ctx.send(embed=embed(
                "Backtest failed",
                f"```\n{tail or 'no output'}\n```", "warn"))
            return

        parts = review.chunks(text)
        for i, part in enumerate(parts, 1):
            title = ("Backtest" if len(parts) == 1
                     else f"Backtest ({i}/{len(parts)})")
            await ctx.send(embed=embed(title, part, "info"))
    finally:
        _backtest_running = False


@bot.command(name="params")
async def cmd_params(ctx):
    await ctx.send(embed=embed("Parameters", parameter_report(), "info"))


@bot.command(name="status", aliases=["st"])
async def cmd_status(ctx):
    positions = tracker.open_positions(conn)
    prices = {}
    if positions:
        try:
            prices = await asyncio.to_thread(
                market.get_prices, [p.ticker for p in positions])
        except market.MarketError:
            pass

    s = tracker.portfolio_summary(conn, prices)
    v = risk.check(conn)
    state = sessions.current_state()

    lines = [
        f"🕒 {state.et_str} ET / {state.uk_str} UK — {state.phase}",
        "",
        f"Open: **{s['open_count']}** · "
        f"Unrealised **£{s['unrealised']:,.2f}**",
        f"Realised **£{s['realised']:,.2f}** · "
        f"Total **£{s['total']:,.2f}**",
    ]

    if s["win_rate"] is not None:
        lines += ["",
                  f"Closed: {s['wins']}W / {s['losses']}L "
                  f"(**{s['win_rate']:.0f}%**)",
                  f"Avg win £{s['avg_win']:,.2f} · "
                  f"Avg loss £{abs(s['avg_loss']):,.2f}"]
        if s["avg_loss"] and abs(s["avg_win"]) < abs(s["avg_loss"]):
            lines.append("\n⚠️ Average loss exceeds average win. You need a "
                         "high win rate just to break even — usually the sign "
                         "of cutting winners early and letting losers run.")

    lines += ["", f"Trades today {v.trades_today}/"
                  f"{CONFIG.risk.max_trades_per_day} · {v.reason}"]
    lines.append(f"\n*{fx.rate_note()}*")

    await ctx.send(embed=embed("Status", "\n".join(lines), "info"))


@bot.command(name="help", aliases=["h", "commands"])
async def cmd_help(ctx):
    await ctx.send(embed=embed(
        "Commands — short versions",
        "**While trading**\n"
        "`!z EURUSD 1.1000 1.0950` — how many units my limit allows\n"
        "`!b EURUSD 2500 1.1000 stop=1.0950` — log a trade by hand\n"
        "`!s EURUSD` — log a close at the live price\n"
        "`!p` — open positions, live P&L in GBP\n\n"
        "**Setting levels**\n"
        "`!sl EURUSD 1.0950` — stop loss\n"
        "`!tp EURUSD 1.1100` — target\n\n"
        "**Results**\n"
        "`!pnl` — today's profit or loss in GBP\n"
        "`!pnl week` · `!pnl month` · `!pnl all`\n"
        "`!eod` — full end-of-day summary now\n"
        "`!sync` — ask OANDA how the open trades ended\n"
        "`!mx` — benchmark table · `!review` — full weekly review\n"
        "`!backtest` — how it would have done over the last 12 weeks\n"
        "`!ai` — what the AI reviewer blocked · `!lessons` — the journal\n\n"
        "**Checking**\n"
        "`!chart` — live scan: why it is or isn't trading\n"
        "`!ss` — session clock · `!r` — risk status\n"
        "`!st` — scorecard\n"
        "`!l` — what your logged trades actually show\n"
        "`!m` — practice or live · `!auto on/off` · `!halt` — kill switch\n"
        "`!params` — every number, with its source\n"
        "`!update` — pull new code from GitHub · `!version` — what's running\n\n"
        "*Long names still work: `!bought`, `!positions`, `!session`.*\n\n"
        "**The strategy**\n"
        "Trend → level → trigger. Trade with the trend, at a level price "
        "has respected 3+ times, on a rejection candle, for 2:1 or "
        "better — long or short.\n\n"
        "**The clock**\n"
        "Forex runs 24/5. Entries 08:00–21:00 UK, best between 13:00 and "
        "17:00 UK when London and New York are both open. Flat by 21:30 "
        "UK on Friday. Daily summary 22:05 UK.",
        "info"))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing from .env")
    if not CHANNEL_ID:
        raise SystemExit("DISCORD_CHANNEL_ID missing from .env")
    bot.run(TOKEN)
