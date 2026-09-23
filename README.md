# cyfer-bot

A forex trading bot for Discord, built on the strategy in the Cyfer Academy
trading guide: trade with the trend, at a level price has respected three or
more times, on a rejection candle, with at least twice the risk to the next
level. It only trades when all of that is there.

It watches **EUR/USD, GBP/USD, USD/JPY and AUD/USD** on an OANDA account,
posts setups to a Discord channel, and — if you switch it on — places the
trades itself with the stop and target attached at the broker.

---

## What it is not

**It isn't proven.** The guide defines the parts; the way they're combined
here is a choice, marked `[CHOICE]` in the code everywhere it appears.
`!backtest` shows how it would have done on past prices, which is evidence,
not a promise. Nothing the bot reports should be read as proof the strategy
works until there are enough closed trades to say so — the review command
refuses to draw conclusions below 20.

**It runs on a demo account by default.** Live trading needs a different
token, a config change, a restart *and* a typed confirmation phrase.

---

## Where the rules come from

Every number in `config.py` is tagged with its source:

| Tag | Means |
| :-- | :-- |
| `[BOOK pNN]` | Stated in the Cyfer Academy guide, at that page |
| `[CHOICE]` | The guide describes it but never gives a number, so one had to be picked |
| `[JG]` | From Jason Graystone — used only for the 8/20/50 EMA confirmation |
| `[BOB]` | A deliberate override of the source |

`!params` in Discord lists them all.

---

## What's fixed and what isn't

**Always pure arithmetic, never decided by AI:** position size, the stop,
the target, the daily loss cap, the trade limit, the consecutive-loss
lockout, and the session clock.

**Optional AI reviewer:** a second opinion that can *block* a trade the
strategy found. It cannot open one, resize one, or move a stop. Off by
default. See `AI-SETUP.md`.

---

## Files

| File | Job |
| :-- | :-- |
| `bot.py` | Discord commands and the scan loop |
| `cyfer.py` | The strategy |
| `pairs.py` | Pips, units, position sizing |
| `oanda.py` | The broker |
| `risk.py` | Sizing and every risk limit |
| `sessions.py` | The 24/5 forex clock and the golden hours |
| `reconcile.py` | Asks the broker how each trade actually ended |
| `review.py`, `metrics.py` | The weekly review, with error bars |
| `ai.py`, `ai_log.py` | The optional AI reviewer |
| `backtest.py` | Replays the strategy over past OANDA prices — `!backtest` |
| `update.sh` | Pull new code and restart safely |

Settings and secrets live in `.env`, which is **never** committed —
`.gitignore` makes sure of it. `.env.example` shows every setting.

---

## Updating

See `UPDATING.md`. Short version: upload changed files here, then type
`!update yes` in Discord.

---

## Backtest

`!backtest` in Discord (or `!backtest 26` for 26 weeks) replays the
strategy over OANDA's own price history, five minutes at a time, using the
live bot's own code for every decision. It runs as a separate program, so
it can't slow the live bot down, and it never touches the trade database.

`!backtest 12 compare` replays the rules from before 23 Sep 2026 next to
the current ones (and two variations) on the same prices, scored in R: each
trade's result divided by what it risked. `!backtest 12 older` runs the 12
weeks before the most recent 12, to check that a rule which won once wins
again on weeks it wasn't chosen on.

Its most important test runs it on pure random prices, where no strategy
can have an edge: there, it must lose. It does — which is how you know it
isn't secretly peeking at future candles.

## Tests

```
for t in test_*.py; do python3 "$t"; done
```

All offline — no network, no broker, no API keys.
