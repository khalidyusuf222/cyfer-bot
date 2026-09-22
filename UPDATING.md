# Updating the bot

## Every update, once set up

1. Here on GitHub: **Add file → Upload files** → drag in the changed files →
   **Commit changes**
2. In Discord: `!update` to see what's about to change
3. In Discord: `!update yes` to pull it and restart

All three work from a phone. Your `.env` and your trade history are never
touched — git has been told to ignore both.

---

## The safety net

Before restarting, `!update` checks every file parses. If something's
broken it **refuses to restart and puts the previous files back**. The bot
keeps running on the old version, and will still boot cleanly after a
reboot. You're told exactly what broke.

---

## One-time setup

You only do this once. After it, every update is the three steps above.

### 1. Put the code here

On this repo's page: **uploading an existing file** → drag in every file
from the unzipped folder, including `.gitignore` → **Commit changes**.

Check the front page lists `bot.py`, `cyfer.py` and so on directly. If it
shows a single folder instead, the files went one level too deep — delete
the repo and upload again.

### 2. Make sure the VPS has git

```
git --version
```

If that says "command not found":

```
apt update && apt install git -y
```

### 3. Download the code onto the VPS

Replace `YOURNAME` with your GitHub username:

```
cd /root && git clone https://github.com/YOURNAME/cyfer-bot.git
```

That makes `/root/cyfer-bot/` with everything in it, already linked to
this repo.

### 4. Move the bot into it

```
bash /root/cyfer-bot/migrate.sh
```

This copies your settings and trade history across from the old folder,
works out which Python your bot uses, sets up the new service, stops the
old bot, and starts the new one. If the new one doesn't come up, it puts
the old one back automatically.

It copies rather than moves, so the old folder stays untouched as a backup.

### 5. Tidy up

Once the new bot has run fine for a day:

- Delete `migrate.sh` from this repo — it's a one-off, and the only file
  that names the old setup.
- Run the cleanup line `migrate.sh` printed at the end, which removes the
  old folder and the old service.

---

## Commands

| Command | Does |
| :-- | :-- |
| `!version` | Which code is running, broker, data source, AI on/off |
| `!update` | Shows what's waiting on GitHub; changes nothing |
| `!update yes` | Pulls, checks, restarts |
| `./update.sh` | The same, from the VPS console |

---

## Worth knowing

**Anyone who can post in the Discord channel can run `!update`.** It only
ever pulls from this repo, so the real risk is someone getting into this
GitHub account. Keep the channel private and turn on two-factor login
here.

**Don't edit files directly on the VPS.** `!update` replaces them with
whatever's here. Edit here instead — every file has a pencil icon.
`!version` warns you if it spots edits made on the VPS.

**If `.env` ever appears in this repo, stop.** It holds your tokens.
Regenerate your OANDA token, Discord token and Groq key straight away,
then delete the repo. `.gitignore` exists to stop that happening — never
remove it.
