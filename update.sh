#!/usr/bin/env bash
#
# One-command update.
#
#   ./update.sh
#
# Pulls the latest code from GitHub, shows what changed, restarts the bot,
# checks it stays up, and puts the old code back if it doesn't.
#
# Your .env and positions.db are NOT touched — both are in .gitignore, so
# git does not know they exist and will never overwrite them.
#
# [FIX 2026-09-23] `!update yes` starts this with systemd-run, as its own
# unit (cyferbot-update). It used to run inside the bot's own process
# group, so restarting the bot killed this script too, and the check and
# rollback at the bottom never ran. Its output: journalctl -u cyferbot-update
#
# Progress goes into update.status as one word, and the output into
# update.log. The bot reads both when it starts and posts the result.

set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/cyfer-bot}"
SERVICE="${SERVICE:-cyferbot}"
# How long the restarted bot must stay up to count as working.
SETTLE_SECONDS="${SETTLE_SECONDS:-20}"

cd "$BOT_DIR"

STATUS_FILE="$BOT_DIR/update.status"
status() { echo "$1" > "$STATUS_FILE"; }

exec > >(tee "$BOT_DIR/update.log") 2>&1
status running

# If anything below dies unexpectedly, say so instead of leaving the bot
# waiting on "running" forever.
trap 'case "$(cat "$STATUS_FILE" 2>/dev/null)" in
        running) status error ;;
        restarting|rollingback) status failed ;;
      esac' EXIT
# Killed from outside: that's what happens when an older bot, still running
# this as its own child, restarts itself. The result is unknown, not failed.
trap 'status interrupted; exit 1' TERM HUP INT

if [ ! -d .git ]; then
    echo "ERROR: $BOT_DIR is not a git repository yet."
    echo "Run the one-time setup first — see UPDATING.md."
    exit 1
fi

echo "=== Before ==="
git log -1 --format='%h  %s  (%cr)' 2>/dev/null || echo "(no commits yet)"

echo
echo "=== Fetching ==="
git fetch origin --quiet

LOCAL="$(git rev-parse HEAD)"
if ! REMOTE="$(git rev-parse '@{u}' 2>/dev/null)"; then
    echo "ERROR: this branch isn't linked to GitHub."
    echo "Run:  git branch -u origin/main"
    exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Already up to date. Nothing to do."
    status uptodate
    exit 0
fi

echo
echo "=== What's changing ==="
git diff --stat "$LOCAL" "$REMOTE"

echo
echo "=== Updating ==="
# --hard because the VPS is never where you edit code. Anything changed
# here by hand is a mistake, and silently keeping it is how the running
# code drifts away from what you think is running.
git reset --hard '@{u}' --quiet
git log -1 --format='Now on: %h  %s'

echo
echo "=== Checking the new code before restarting ==="
# Only files git actually tracks. Anything else in the folder — a stray file
# copied in by hand, a leftover from testing — isn't part of the bot and must
# not be able to block an update, or pass one.
if git ls-files -z '*.py' | xargs -0 python3 -c "
import ast, sys
bad = []
for f in sys.argv[1:]:
    try:
        ast.parse(open(f).read())
    except SyntaxError as e:
        bad.append(f'{f}: {e}')
if bad:
    print('\n'.join(bad))
    sys.exit(1)
print(f'all {len(sys.argv) - 1} files parse')
"; then
    echo
else
    # Put the files back. Without this, the running process is fine (the old
    # code is already loaded in memory) but the DISK now holds the broken
    # version — so the next reboot, crash or restart for any reason loads it
    # and the bot crash-loops without anyone having touched it.
    git reset --hard "$LOCAL" --quiet
    echo
    echo "REFUSING TO RESTART — the new code has a syntax error."
    echo "Rolled the files back to $(git log -1 --format='%h  %s')."
    echo "The bot is still running, and will still boot, on the old version."
    echo "Tell Claude."
    status refused
    exit 1
fi

# True if the service is running and hasn't crashed and been restarted by
# systemd in the last SETTLE_SECONDS. A bot that dies on startup can still
# look "active" for a moment, which is why a single quick check isn't enough.
came_up() {
    local before after
    before="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || true)"
    sleep "$SETTLE_SECONDS"
    systemctl is-active --quiet "$SERVICE" || return 1
    after="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || true)"
    [ "$before" = "$after" ]
}

echo "=== Restarting ==="
status restarting
if systemctl restart "$SERVICE" && came_up; then
    echo "Running, and still up after $SETTLE_SECONDS seconds."
    status ok
    echo
    echo "Done. Check Discord for the startup message."
    exit 0
fi

echo "THE NEW CODE DIDN'T STAY UP. Last 20 lines:"
journalctl -u "$SERVICE" -n 20 --no-pager || true

echo
echo "=== Rolling back ==="
status rollingback
git reset --hard "$LOCAL" --quiet
echo "Files put back to $(git log -1 --format='%h  %s')."
if systemctl restart "$SERVICE" && came_up; then
    echo "The previous version is running again. Tell Claude."
    status rolledback
else
    echo "THE PREVIOUS VERSION DIDN'T COME UP EITHER."
    echo "Check with: journalctl -u $SERVICE -n 50 --no-pager"
    status failed
fi
exit 1
