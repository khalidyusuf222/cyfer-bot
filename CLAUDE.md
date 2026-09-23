# Notes for Claude

Read this before changing anything, then read **HISTORY.md**. It has the
whole story of how the bot got here: every decision, what broke and why.

This repo is **public**. Nothing personal goes in it (see "Never commit"
below).

## What this is

cyfer-bot is a Discord bot that trades forex on an **OANDA practice (demo)
account**. It watches EUR/USD, GBP/USD, USD/JPY and AUD/USD, posts setups
to Discord, and can place the trades itself with the stop and target held
at OANDA.

- Strategy: the Cyfer Academy trading guide (the "book"), plus Jason
  Graystone's 8/20/50 EMA stack as a confirmation only. Rules are cited in
  code as `[BOOK pNN]`, `[JG]`, `[CHOICE]` or `[BOB date]` (the owner's own
  override).
- History: this started as a share-trading bot on a different strategy
  ("TJR", Alpaca). All of that was removed at the owner's request. Don't
  bring the name or the approach back. `broker_alpaca.py` and
  `market_yahoo.py` are the old code paths. They're only reachable through
  `BROKER` / `DATA_SOURCE` in `.env`, and the server runs `BROKER=oanda`.

## How code gets to the server

1. Commit and push to `main` on GitHub.
2. The owner types `!update` in Discord to see what changed, then
   `!update yes` to apply it.
3. `update.sh` fetches, resets to `origin/main`, refuses anything that
   doesn't parse, and restarts the systemd service `cyferbot`
   (folder `/root/cyfer-bot`, virtualenv `venv`). Logs: `journalctl -u cyferbot`.
   `!update yes` starts it with `systemd-run` as its own unit
   (`cyferbot-update`), so it survives the restart. If the bot doesn't stay
   up for 20 seconds, it puts the old code back. It writes progress to
   `update.status` and output to `update.log`, and the bot that's running
   afterwards posts the result (`updater.py`).
4. `!version` shows the running commit.

Always tell the owner in plain words what you're about to push, and wait
for a yes, before pushing. Once they say yes, push straight to `main`, not
to a side branch or a pull request (the owner's instruction, 23 Sep). The
server only pulls from `main`. The owner never gives Claude server access,
and Claude shouldn't ask for it.

## Tests

Tests are plain functions, **not** pytest or unittest. Run every file:

    for t in test_*.py; do python3 "$t" || echo "FAILED: $t"; done

Each file prints "All N tests passed." (`python -m unittest` finds 0 tests
and says OK, which proves nothing.) All tests run offline. Currently
there are about 310 across 15 files. Run them all before every push.

## Current settings (config.py)

- **Risk 10% per trade** (`risk_per_trade_pct = 10.0`). This was the owner's
  instruction on 2026-09-23. The book says 0.5 to 1%. Say so honestly when
  it matters, but it's the owner's call.
- Daily loss cap 20% (two full losses). Stops after 3 losses in a row.
  At most 6 trades a day. One open position per pair.
- UK leverage limit: 30:1 on pairs of USD/EUR/JPY/GBP/CAD/CHF, 20:1
  otherwise (AUD/USD). The sizer uses 90% of that. Trades that are too big
  are **cut to fit, not skipped** (`risk.size_trade`, `capped`). The live
  bot reads free margin from OANDA, so a second open trade only gets what's
  left. In the first backtest every trade was cut, and the real risk
  averaged £14.54, not £100. Raising the percentage does nothing at £1,000:
  the leverage cap binds first (about £35 to £95 a trade, depending on the
  stop). The owner wants £1,000 a week; the honest answer is in HISTORY.md
  (23 Sep, night).
- Account size for sizing is fixed at `display.account_gbp` (£1,000), not
  the live balance.
- Alerts from score 2/6, auto-trades from 3/6, and only when the book's
  trend, level and trigger candle are all there (`cyfer.require_core`).
- Target is the next level of either kind. If that's under 2:1, no trade
  (`cyfer.target_at_next_level`). Both switches came in on 23 Sep, after the
  first backtest found no edge. `!backtest 12 compare` replays old and new
  rules side by side.
- No new trades after 12:00 New York time on Friday
  (`sessions.friday_last_entry`).
- AI reviewer (Groq, `openai/gpt-oss-120b`): **veto only**, off unless
  `AI_ENABLED` is set in `.env`. It can block a trade and nothing else.
  It must never be able to open, resize or loosen anything.

## Backtest

`!backtest [weeks] [compare] [older]` (default 12, max 52, compare max 16)
runs `backtest.py` as a separate process on the server, using OANDA's own
candles. `compare` replays `backtest.VARIANTS` on the same prices using
`config.override`; `older` tests the stretch before the recent one. It
calls the live code (`cyfer.scan`, `sessions`, `risk.size_trade`,
`oanda.validate_levels`).

- No look-ahead. It enters at the next candle's open plus half the spread.
- A candle touching both the stop and the target counts as a stop.
- It tracks equity and margin like OANDA does.
- It must lose on random prices (`test_no_edge_on_pure_coin_flip_prices`).
  If that test ever finds an edge, the engine is peeking.

Claude's cloud sandbox can't reach market data, so backtests only run on
the server.

## Talking to OANDA

- `oanda._get(..., retries=N)` retries network errors and 5xx responses.
  The live scan uses `retries=0`, so a slow OANDA can't hold a scan up.
  The backtest uses retries.
- **Never retry an order** (`_post`). Sending it twice opens two trades.
- `OandaServerError` means OANDA's side failed. `_explain()` turns OANDA's
  HTML error pages into one sentence. Never paste raw HTML into Discord.

## Never commit

- `.env`, tokens, API keys, `positions.db` (all in `.gitignore`).
- The server's IP address, the OANDA account number, email addresses, or
  any other personal detail.
- Never ask the owner to paste a token, key, password or 2FA code into a
  chat or screenshot one. They create keys and type them into `.env` on
  the server themselves.

## To do

1. Delete `migrate.sh` (a one-off from the rename). Remind the owner that the
   old folder on the server can be removed.
2. Read `!backtest 12 compare` and `!backtest 12 compare older` with the
   owner. Keep only rule changes that win on both stretches.
3. Add an economic calendar so the bot sits out high-impact news
   (book p62-65). There's none yet.

## How the owner likes answers

- Numbered, step-by-step instructions, with exact button names on GitHub
  and in Discord.
- Explain trading and tech terms the first time they come up.
- Plain words. **No HTML tags like `<kbd>`**, which show up as gibberish in
  the owner's app. Markdown and backticks are fine.
- The owner works from an iPhone and a Windows PC with the Brave browser.
