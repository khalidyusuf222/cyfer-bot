# Adding the AI reviewer — step by step

This bolts a language model onto the bot as a **second opinion**. It is
**off by default**, and the bot works exactly as it did without it.

About 10 minutes.

---

## What it can and cannot do

Read this bit properly, because it is the whole design.

**It CAN:** block a trade the strategy already found.

**It CANNOT:** open a trade, change a position size, move a stop, move a
target, or reopen a risk gate that has closed.

That is not caution for its own sake. Your position sizing and your risk
limits are the only parts of this system with any evidence behind them —
they are arithmetic, they have tests, and they behave identically every
time. The strategy has never been backtested, and a language model cannot
be backtested at all. Letting either one near the sizing maths would leave
the whole thing resting on nothing.

The AI is called **last**, after every deterministic check has already
passed. By the time it is asked, the only thing left for it to do is stop
something.

---

## Step 1 — Get a free Groq key (3 min)

**Groq** is a company that runs other people's open models on their own
hardware, very fast, with a free tier that needs no card.

1. Go to **https://console.groq.com** and sign up with Google or email.
2. Click **API Keys** in the left-hand sidebar.
3. Click **Create API Key**. Name it something like `tradebot`.
4. **Copy it straight away** — it is shown once. It starts with `gsk_`.
5. Paste it into a text file on your own PC.

> **Never send this key to anyone, including me.** If it leaks, go back to
> that page, delete it, and make a new one. Same rule as your OANDA token.

---

## Step 2 — Put it in `.env` (3 min)

1. Open **WinSCP**, connect to the VPS, go to `/root/cyfer-bot/`.
2. Right-click **`.env`** → **Edit**.
3. Add these lines at the bottom:

   ```
   AI_ENABLED=on
   AI_PROVIDER=groq
   GROQ_API_KEY=gsk_your_key_here
   AI_FAIL_MODE=abstain
   ```

4. Save and close.

---

## Step 3 — Restart (1 min)

The AI code is already part of the bot — there's nothing new to upload.
It just needs the setting you changed to take effect.

1. In the VPS console:

   ```
   systemctl restart cyferbot
   ```

2. In Discord, type:

   ```
   !ai
   ```

   It should say **AI review: ON**, name the provider and model, and
   confirm it can only block trades.

**Nothing to install.** It uses `requests`, which is already there.

---

## What the settings mean

| Setting | What it does |
| :-- | :-- |
| `AI_ENABLED` | `on` or `off`. Off is the default and the bot runs as it always has. |
| `AI_PROVIDER` | `groq` or `gemini`. One line moves you to a different company. |
| `AI_MODEL` | Leave blank for the provider's default. Only set it if a model gets retired. |
| `AI_FAIL_MODE` | What happens when the API is down — see below. |

### `AI_FAIL_MODE` is the one worth understanding

Your original spec said "if the API fails, default to HOLD". That is
correct **when the AI is the decision maker** — if it can't think, don't
trade.

But here it is only a veto, so HOLD-on-failure would mean **a Groq outage
silently stops your bot trading at all**, despite the bot having run
perfectly well without any AI for weeks. So there are two options:

- **`abstain`** (default) — an API failure leaves the trade alone. It goes
  ahead on the strategy, exactly as before. An outage changes nothing.
- **`hold`** — an API failure blocks the trade. This is your original spec.
  Choose it if you ever give the AI more power than a veto.

Either way the failure is logged, so a silently broken AI can never look
like a working one.

---

## The feedback loop, and the 20-trade floor

You chose the hard floor, which I think was right. Here is what it does.

Every closed trade gets a **post-mortem**: the model compares the result to
the reasoning at entry and writes one takeaway to the database. That starts
from trade one.

But those takeaways **do not feed back into any trading decision until 20
trades have closed.** Below that, the model is told outright that no
history exists and is forbidden from speculating about past mistakes.

**Why.** A model asked why a trade lost will always produce a reason —
that is what the question invites. With one trade as evidence, that reason
is very often invented. Store enough invented reasons and feed them back
in, and you have built a machine that manufactures superstitions and then
trades on them. Twenty is the same floor `metrics.py` already uses before
it will diagnose anything, for the same reason.

The post-mortem prompt also gives the model an explicit way out. It is told
that a losing trade in a 50%-win-rate strategy is **normal and not evidence
of a mistake**, and that it should reply `NO LESSON — within normal
variance` whenever nothing specific went wrong. That answer is recorded as
a real answer, not a failure. Expect most trades to produce it. That is the
honest result, not a broken one.

---

## Commands

| Command | What it shows |
| :-- | :-- |
| `!ai` | Provider, model, how many verdicts, how many blocks, how many failures, average latency, and the last five verdicts |
| `!lessons` | The post-mortem journal — only the ones that found something specific |

---

## How to tell whether it is helping

Every verdict is stored **with the trade it would have taken** — size,
entry, stop, target. That is deliberate: a block that leaves no record of
what it blocked is an unfalsifiable claim to have helped, and after a month
of those you would have no way to know.

So in a few weeks the question "did the AI save me money or cost me money?"
has an actual answer. Ask me then and I will write the query.

My honest guess: with the numbers you have now, it will be too early to
tell. Blocks are rare events and you need a lot of them before the
difference between "helped" and "got lucky" is visible.

---

## Things I did differently from your spec, and why

**1. No `groq` Python library.** Your spec asked for
`from groq import Groq`. This uses `requests` instead — already installed,
already how `oanda.py` talks to its API, nothing new to break on the VPS.
The Groq SDK pins its own HTTP library versions, which is a good way to
turn a working bot into a broken one on a `pip install`. The API is the
same either way.

**2. `llama-3.3-70b-versatile` is gone.** Groq retired it on **16 August
2026**, which was before a line of this got written. The default is now
`openai/gpt-oss-120b` — an OpenAI open-weights model that Groq hosts, and
the migration path Groq themselves recommend.

**3. Strict JSON schema instead of `json_object`.** Your spec asked for
`response_format={"type": "json_object"}`. That only guarantees the reply
is *valid JSON* — a model could return `{"decision": "buy"}` and pass.
`gpt-oss-120b` supports strict schema mode, which enforces your exact three
fields at the API. Wrong shapes become impossible rather than something we
catch afterwards.

**4. A provider switch.** Groq killed a model out from under this project
before it existed. It will happen again. Moving to Google Gemini is one
line in `.env` rather than a rewrite.

---

## What I'd still say plainly

You now have **two unvalidated layers stacked on each other**: a strategy
that has never been backtested, and a model whose decisions cannot be
reproduced. If results come out badly, you will not easily be able to tell
which one was at fault.

That is the real cost of this feature, and it is worth naming because it is
not visible in any of the code.

The veto design is what keeps it manageable. Because the AI can only
subtract, you can always answer "what would the bot have done without it?"
— that is exactly what the `would_have` column is for. If you had let the
model drive the decision instead, that question would have no answer.

If you want my recommendation: leave `AI_ENABLED=off` for your first
twenty or thirty trades, so you get a clean baseline of what the strategy
alone does. Then switch it on and compare. Without a baseline, you are
measuring against nothing.

---

## If something goes wrong

**`!ai` says a key is missing**
The `.env` line has a typo or a stray space. Re-open it in WinSCP.

**`!ai` shows lots of failures**
Check the model name. If Groq has retired another model, the failure text
says "not found — it may have been retired". Look up the current list at
https://console.groq.com/docs/deprecations and set `AI_MODEL=` to a live
one.

**"rate limited (free tier)"**
Free tier is around 30 requests a minute, 1,000 a day. The bot should only
call the AI when a setup actually fires — a handful a day. If you are
hitting limits, something is calling it far more than it should and I want
to know.

**You want it gone**
Set `AI_ENABLED=off` and restart. Everything reverts to the deterministic
bot. Nothing else changes.
