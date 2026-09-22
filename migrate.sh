#!/usr/bin/env bash
#
# One-off: move the bot to its new name.
#
#   bash /root/cyfer-bot/migrate.sh
#
# Moves you from the old folder and service to /root/cyfer-bot and the
# 'cyferbot' service. This is the only file in the project that names the
# old setup — its whole job is to move you off it. Once it has run
# successfully, delete it from GitHub.
#
# WHAT IT DOES
#   1. Copies .env and positions.db across from the old folder. COPIES, so
#      the old folder stays exactly as it was, as a backup.
#   2. Reads the old service file to work out which Python the bot used,
#      rather than guessing.
#   3. Writes the new service.
#   4. Stops the old bot BEFORE starting the new one. Two copies running at
#      once would each scan, each believe it had the whole risk budget, and
#      each place orders on the same account.
#   5. If the new one doesn't come up cleanly, puts the old one back.

set -euo pipefail

OLD_DIR="${OLD_DIR:-/root/tjr-discord-bot}"
OLD_SVC="${OLD_SVC:-tjrbot}"
NEW_DIR="${NEW_DIR:-/root/cyfer-bot}"
NEW_SVC="${NEW_SVC:-cyferbot}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
CHECK_IMPORTS="${CHECK_IMPORTS:-import discord, requests, dotenv}"
SETTLE="${SETTLE:-8}"

say()  { echo "  $*"; }
step() { echo; echo "=== $* ==="; }
die()  { echo; echo "STOPPED: $*"; exit 1; }

# --- preflight ---------------------------------------------------------
step "Checking"
[ "$(id -u)" = "0" ]     || die "run this as root"
[ -d "$NEW_DIR/.git" ]   || die "$NEW_DIR isn't a git clone yet — do the git clone step first"
[ -f "$NEW_DIR/bot.py" ] || die "$NEW_DIR has no bot.py — the GitHub upload may have gone one folder too deep"
[ -d "$OLD_DIR" ]        || die "can't find the old bot at $OLD_DIR"

if systemctl is-active --quiet "$NEW_SVC" 2>/dev/null; then
    say "$NEW_SVC is already running — this has already been done. Nothing to do."
    exit 0
fi
say "moving from  $OLD_DIR  (service '$OLD_SVC')"
say "        to   $NEW_DIR  (service '$NEW_SVC')"

# --- settings and history ---------------------------------------------
step "Copying your settings and trade history"
if [ -f "$NEW_DIR/.env" ]; then
    say ".env is already in the new folder — leaving it alone"
elif [ -f "$OLD_DIR/.env" ]; then
    cp -p "$OLD_DIR/.env" "$NEW_DIR/.env"
    chmod 600 "$NEW_DIR/.env"
    say ".env copied — your Discord token comes with it"
else
    die "there's no .env in $OLD_DIR, so there are no settings to bring across"
fi

if [ -f "$NEW_DIR/positions.db" ]; then
    say "positions.db is already in the new folder — leaving it alone"
elif [ -f "$OLD_DIR/positions.db" ]; then
    cp -p "$OLD_DIR/positions.db" "$NEW_DIR/positions.db"
    say "positions.db copied — your trade history comes with it"
else
    say "no positions.db yet — fine, a fresh one is made on first start"
fi

# --- which python -----------------------------------------------------
step "Working out which Python the bot uses"
OLD_UNIT="$(systemctl show -p FragmentPath --value "$OLD_SVC" 2>/dev/null || true)"
{ [ -n "$OLD_UNIT" ] && [ -f "$OLD_UNIT" ]; } || OLD_UNIT="$UNIT_DIR/$OLD_SVC.service"
[ -f "$OLD_UNIT" ] || die "can't find the old service file ($OLD_UNIT)"
say "read $OLD_UNIT"

EXEC_LINE="$(grep -E '^[[:space:]]*ExecStart=' "$OLD_UNIT" | head -1 \
             | sed -E 's/^[[:space:]]*ExecStart=//; s/^[-@+!:]+//')"
INTERP="$(echo "$EXEC_LINE" | awk '{print $1}')"
RUN_AS="$(grep -E '^[[:space:]]*User=' "$OLD_UNIT" | head -1 \
          | sed -E 's/^[[:space:]]*User=//' || true)"

# "python3 bot.py" rather than a full path -> find it on PATH
case "$INTERP" in
    /*) ;;
    *)  INTERP="$(command -v "$INTERP" || true)" ;;
esac
[ -n "$INTERP" ] || die "couldn't work out the Python from: $EXEC_LINE"

case "$INTERP" in
    "$OLD_DIR"/*)
        # A private Python (a "virtual environment") inside the OLD folder.
        # It can't be reused: a venv has its own location baked into it,
        # and the point is to stop depending on the old folder at all.
        say "the old bot used a private Python inside its own folder"
        say "building a fresh one in the new folder (about a minute)"
        BASE="$(command -v python3)"
        "$BASE" -m venv "$NEW_DIR/venv" \
            || die "couldn't create one — run: apt install python3-venv -y, then run this again"
        PY="$NEW_DIR/venv/bin/python3"
        "$PY" -m pip install --quiet --upgrade pip
        "$PY" -m pip install --quiet -r "$NEW_DIR/requirements.txt" \
            || die "installing the bot's packages failed"
        ;;
    *)
        PY="$INTERP"
        say "the old bot used $PY — reusing it"
        ;;
esac

step "Checking the bot's packages are there"
if ! "$PY" -c "$CHECK_IMPORTS" 2>/dev/null; then
    say "some are missing — installing them"
    case "$PY" in
        "$NEW_DIR"/venv/*) "$PY" -m pip install --quiet -r "$NEW_DIR/requirements.txt" ;;
        *) "$PY" -m pip install --quiet -r "$NEW_DIR/requirements.txt" --break-system-packages ;;
    esac
    "$PY" -c "$CHECK_IMPORTS" || die "the packages still won't load"
fi
say "all present"

# --- the new service --------------------------------------------------
step "Writing the new service"
NEW_UNIT="$UNIT_DIR/$NEW_SVC.service"
{
    echo "[Unit]"
    echo "Description=Cyfer forex bot"
    echo "After=network-online.target"
    echo "Wants=network-online.target"
    echo
    echo "[Service]"
    echo "Type=simple"
    if [ -n "$RUN_AS" ]; then echo "User=$RUN_AS"; fi
    echo "WorkingDirectory=$NEW_DIR"
    echo "ExecStart=$PY $NEW_DIR/bot.py"
    echo "Restart=always"
    echo "RestartSec=10"
    echo
    echo "[Install]"
    echo "WantedBy=multi-user.target"
} > "$NEW_UNIT"
say "wrote $NEW_UNIT"

# --- the switch -------------------------------------------------------
step "Switching over"
systemctl stop "$OLD_SVC" 2>/dev/null || true
systemctl disable --quiet "$OLD_SVC" 2>/dev/null || true
say "old bot stopped, and won't come back on a reboot"

systemctl daemon-reload
systemctl enable --quiet "$NEW_SVC"
systemctl start "$NEW_SVC"
say "new bot starting — waiting ${SETTLE}s to make sure it stays up"
sleep "$SETTLE"

# "active" alone isn't enough: a bot that crashes on login and gets
# restarted by systemd can happen to look active at the moment we check.
RESTARTS="$(systemctl show -p NRestarts --value "$NEW_SVC" 2>/dev/null || echo 0)"
if systemctl is-active --quiet "$NEW_SVC" && [ "${RESTARTS:-0}" = "0" ]; then
    say "new bot is up and has stayed up"
else
    echo
    echo "The new bot didn't start cleanly. Last 25 lines of its log:"
    echo "----------------------------------------------------------------"
    journalctl -u "$NEW_SVC" -n 25 --no-pager 2>/dev/null || true
    echo "----------------------------------------------------------------"
    echo
    echo "Putting the old one back so you're not left without a bot."
    systemctl stop "$NEW_SVC" 2>/dev/null || true
    systemctl disable --quiet "$NEW_SVC" 2>/dev/null || true
    systemctl enable --quiet "$OLD_SVC" 2>/dev/null || true
    systemctl start "$OLD_SVC" 2>/dev/null || true
    die "old bot restored and running. Send Claude the log lines above."
fi

step "Done"
say "The bot now lives in $NEW_DIR and runs as '$NEW_SVC'."
say "The old folder is still at $OLD_DIR — nothing in it was touched."
echo
say "Leave it for a day as a backup. Once the new one's been fine, remove"
say "the old setup for good with:"
echo
say "    rm -rf $OLD_DIR $UNIT_DIR/$OLD_SVC.service && systemctl daemon-reload"
