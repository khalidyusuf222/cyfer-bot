#!/usr/bin/env bash
#
# One-command update.
#
#   ./update.sh
#
# Pulls the latest code from GitHub, shows what changed, and restarts the
# bot. Replaces the whole download-WinSCP-drag-unpack-restart routine.
#
# Your .env and positions.db are NOT touched — both are in .gitignore, so
# git does not know they exist and will never overwrite them.

set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/cyfer-bot}"
SERVICE="${SERVICE:-cyferbot}"

cd "$BOT_DIR"

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
    exit 1
fi

echo "=== Restarting ==="
systemctl restart "$SERVICE"
sleep 3
systemctl is-active "$SERVICE" >/dev/null && echo "Running." || {
    echo "FAILED TO START. Last 20 lines:"
    journalctl -u "$SERVICE" -n 20 --no-pager
    exit 1
}

echo
echo "Done. Check Discord for the startup message."
