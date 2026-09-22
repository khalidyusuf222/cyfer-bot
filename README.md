# cyfer-bot

A forex trading bot for Discord, built on the strategy in the Cyfer Academy
trading guide: trade with the trend, at a level price has respected three or
more times, on a rejection candle, for at least twice the risk.

It watches **EUR/USD, GBP/USD, USD/JPY and AUD/USD** on an OANDA account,
posts setups to a Discord channel, and — if you switch it on — places the
trades itself with the stop and target attached at the broker.

---

## What it is not

**It has never been backtested.** The guide defines the parts; the way
they're combined here is a choice, marked `[CHOICE]` in the code everywhere
it appears. Nothing the bot reports should be read as proof the strategy
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
| `update.sh` | Pull new code and restart safely |

Settings and secrets live in `.env`, which is **never** committed —
`.gitignore` makes sure of it. `.env.example` shows every setting.

---

## Updating

See `UPDATING.md`. Short version: upload changed files here, then type
`!update yes` in Discord.

---

## Tests

```
for t in test_*.py; do python3 "$t"; done
```

All offline — no network, no broker, no API keys.
