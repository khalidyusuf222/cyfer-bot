# How this bot got here

Everything that matters from the chat that built cyfer-bot: what was asked
for, what was decided and why, what broke, and what's still open. Read it
alongside CLAUDE.md. All dates are 2026. Where a date is only approximate,
it says so.

The code has its own notes too. Search for `[FIX date]`, `[BOB date]` and
`[CHOICE]` in `config.py`, `cyfer.py`, `bot.py` and `oanda.py`.

---

## Timeline

**1 Sep: the idea.** The owner wanted AI help with day trading: a
strategy, risk checks, suggestions, and when to act. It had to run in the
cloud, not on his PC. The first plan was crypto on Coinbase (including
meme-coin pump alerts), plus forex and futures.

**8 Sep: first bot, "TJR".**

- The direction changed to a funded (prop-firm) account on the stock
  market. No crypto, no Coinbase.
- Strategy: TJR's "Path To Profitability" YouTube series (Smart Money
  Concepts: fair value gaps, break of structure, liquidity sweeps).
- Data came from Alpaca's paper-trading API.
- The first Discord bot, "Bigger Boss", ran on a Vultr VPS as the systemd
  service `tjrbot`, watching SPY and QQQ. It later added Jason Graystone's
  8/20/50 EMA method, and the watchlist grew to the S&P 500.
- The owner described himself as new to trading, and still wants terms
  explained.

**10 Sep: 20% risk.** The owner set risk to 20% a trade (£200 of £1,000),
with a 40% daily cap, to chase a target of £1,000 a week. He also wanted
fully automatic trading, not a confirm-first flow.

**14 Sep: "trade #1", the 59-second trade.**

- What went wrong: a 1-minute bar on a thin feed closed 0.52% away from
  the real market, and the order's stop and target were priced off it. The
  order filled at the true price, which left the stop on the wrong side of
  the entry. The trade closed itself in 59 seconds.
- Three guards came out of it:
  1. the bad-print check in `cyfer.py`
  2. the pre-flight level check (`oanda.validate_levels`)
  3. `reconcile.py`, which asks the broker how each trade ended. Before
     that, trades closed at the broker without the bot knowing, so the
     loss limits never saw a loss.
- Score thresholds were lowered for paper testing: alert at 2/6, trade at
  3/6. They should go back up before real money.

**15 Sep: forex, and the Cyfer PDF.**

- The owner said to follow the Cyfer Academy trading guide ("99% from the
  PDF"), keep a little Graystone, and remove everything TJR.
- The bot moved to forex on an OANDA practice account in GBP. Shares can't
  be split, so a £10 risk on a $760 share can't be sized. Currency units
  divide down to 1.
- Other changes that day:
  - short trades became real orders
  - the session clock was rewritten for the 24/5 forex week
  - everything open is closed at 16:30 New York time on Friday
- Risk went back to 1%, the top of the book's range (p53).
- The spreads the backtest uses were read off the owner's OANDA screen.

**15 Sep: the AI reviewer.**

- The owner asked for a free AI API with memory and a "post-mortem" that
  learns from losses. Groq was chosen because it's free.
- The owner's answers to the design questions:
  - He picked a hard floor: no loss history is shown to the model until
    there are 20 closed trades.
  - When asked what role the AI should play, he said to just find one and
    carry on. So it was built as veto-only. It can block a trade and
    nothing else.
- If the API fails, the trade goes ahead unreviewed ("abstain"). The model
  `llama-3.3-70b-versatile` was retired on 16 Aug, so it uses
  `openai/gpt-oss-120b`. It's off unless `AI_ENABLED` is set (see
  AI-SETUP.md).

**About 16 to 21 Sep: updates and the rename.**

- Updating by dragging files in WinSCP kept going wrong: folders got
  renamed `_1`/`_2`, and systemctl's pager caught him out. The owner asked
  to give Claude access to the server. That was declined for security, and
  the GitHub flow was built instead: this public repo, `update.sh`,
  `!update`, `!update yes` and `!version`.
- The owner wanted every trace of TJR gone, so everything was renamed to
  cyfer-bot (service `cyferbot`, folder `/root/cyfer-bot`).
- `migrate.sh` moved the old database aside as `positions-old-shares.db`.
  It still held an old SPY trade, and auto-trading was switched on in it.
- OANDA once rejected the token. The owner made a new one himself.

**22 Sep: first live alert, and a strategy flaw.**

- An EUR/USD 2/6 alert counted a price 10 pips *below* support as "at
  support". The stop landed 7 pips from the entry, which inflated
  reward-to-risk to 6:1 and marked two conditions as met for the wrong
  reason.
- The fix:
  - support has to be *held* (`level_break_pct`)
  - the level check now depends on which side the trade is on
  - with no trend, the direction comes from the level price is holding
- This went out as update 1, commit `4f0a465`.

**23 Sep: the £1,000-a-week question, the backtest, 10% risk.**

- The owner asked for a weekly profit forecast and restated the £1,000 a
  week goal. The explanation he got:
  - at 1% risk, even a perfect week (30 winning trades) makes about £600
  - £1,000 a week needs 7 to 13% risk a trade
  - at 20% risk, five losses in a row wipes the account, about a 37%
    chance over 30 trades
  - doubling £1,000 every week would be £1 million in 10 weeks
  - the realistic route is to prove the strategy on demo, then trade a
    bigger or funded account
- `backtest.py` and `!backtest` were built. Building them turned up two
  bugs in the live bot, and both were fixed:
  1. A weak 2/6 alert used up the hour's slot, so a later 3/6 setup in the
     same hour was never traded. `_alerted` and `_traded` are now separate.
  2. A trending market could open a new position in the same pair every
     hour. There's now one open position per pair.
- The owner then set risk to **10%** ("no question"). Other changes that
  came with it:
  - The daily cap went to 20%, because the old 4% would stop the bot after
    one loss.
  - UK accounts are capped at 30:1 leverage (20:1 on AUD/USD), which makes
    most 10% trades impossible. So the sizer now cuts them to fit, using
    90% of the limit and OANDA's free margin, instead of skipping them.
- The plan from here: connect GitHub through Claude's GitHub app, and
  make bot changes in Claude Code sessions. The owner still types
  `!update` himself.

**23 Sep (afternoon): first backtest run failed.** OANDA's practice server answered the
first download (100 days of hourly candles in one request) with a 504
"timed out" HTML page, and the bot pasted that page into Discord. The fix:

- downloads are now smaller (4 days of 5-minute candles, 30 days of hourly)
- a failed download is retried
- a window that still fails is split in half
- OANDA errors show as one plain sentence

Reads are retried. Orders never are, because sending one twice would open
two trades.

---

## Standing decisions

| Decision | Why |
| :-- | :-- |
| OANDA **practice** account only | Live needs a new token, a config change and a typed phrase (`!arm`). |
| Risk 10% a trade, daily cap 20% | The owner's call on 23 Sep. The book says 0.5 to 1%. |
| Trades too big for UK leverage are cut, not skipped | The owner wants the trades to happen. OANDA would reject them at full size anyway. |
| One position per pair | Stops one idea from stacking up hour after hour. |
| Alert from 2/6, trade from 3/6 | Lowered for paper testing. Raise them before live. |
| AI is veto-only and off by default | It must never open, resize or loosen anything. |
| Updates only via GitHub and `!update` | The owner keeps control of the server. Claude never gets server access. |
| Sizing uses a fixed £1,000, not the live balance | Simple and predictable. At 10% it means risk stays at £100 even after losses. |

---

## Rules that came from mistakes

- Never trust a single price print. Compare it with its neighbours.
- Check the stop is on the correct side of the entry before sending, for
  both directions.
- Always reconcile with the broker. The bot's own records aren't the
  truth.
- Tests run with `python3 test_x.py`, not unittest. unittest finds 0 tests
  and prints OK.
- A backtest must lose on random prices. If it doesn't, it's peeking at
  future candles.
- Don't use HTML tags like `<kbd>` in replies to the owner. They show up
  as gibberish in his app.
- If a key or token ever shows up in a screenshot or a chat, tell the owner
  to reset it straight away.

---

## Still open

1. `!update` from Discord doesn't run the check that the bot came back up,
   or the rollback if it didn't. The service restart kills `update.sh`.
   Fix it with `systemd-run` (details in CLAUDE.md).
2. Delete `migrate.sh`. The owner can then delete the old
   `/root/tjr-discord-bot` folder on the server.
3. The first `!backtest` result hasn't been seen yet.
4. Before any live trading: raise the score thresholds and review the risk
   setting.
5. Turning on the AI reviewer needs a Groq key, which the owner puts into
   `.env` on the server himself. See AI-SETUP.md.

---

## Not in this repo

- **The Cyfer Academy guide.** It's the owner's copy and it's copyrighted.
  The code cites its pages (`[BOOK pNN]`). If a rule is unclear, ask the
  owner rather than guessing.
- **Anything personal:** the server's address, the OANDA account number,
  emails, keys. They stay out.
- **The step-by-step setup guide.** The owner has it as a page in his
  Claude account ("Cyfer Bot Setup").
