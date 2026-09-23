"""
The bot's side of `!update yes`.

[FIX 2026-09-23] update.sh used to run as a child of the bot. Restarting
the service kills every process in the bot's cgroup (the group systemd
stops together), so the script died with it: the check that the new code
came up, and the rollback if it didn't, never ran.

Now the bot hands update.sh to systemd as its own unit (`systemd-run`), so
it outlives the restart. The script writes one word to `update.status` as
it goes, and its output to `update.log`. Whichever bot ends up running
afterwards (the new code, or the old code after a rollback) reads those on
startup and posts the result to Discord.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).parent
STATUS_FILE = HERE / "update.status"
LOG_FILE = HERE / "update.log"
UNIT = "cyferbot-update"

# Written by update.sh while it works.
IN_PROGRESS = {"running", "restarting", "rollingback"}

# Finished, before any restart. The bot that started the update is still
# alive and reports these itself.
BEFORE_RESTART = {"uptodate", "refused", "error"}

# Finished, after a restart. Only the next bot to boot can report these.
AFTER_RESTART = {"ok", "rolledback", "failed", "interrupted"}

FINAL = BEFORE_RESTART | AFTER_RESTART


def launch_command(bot_dir: Path = HERE) -> list[str]:
    """systemd-run line that starts update.sh as its own unit.

    --collect removes the unit when it ends, even on failure, so the next
    `!update yes` can reuse the name. While one update runs, a second one
    fails with "unit already exists", which is what we want.
    """
    return ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
            f"--setenv=BOT_DIR={bot_dir}",
            f"--setenv=HOME={os.environ.get('HOME', '/root')}",
            "bash", str(bot_dir / "update.sh")]


def read_status(path: Path = STATUS_FILE) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def clear(path: Path = STATUS_FILE) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def log_tail(path: Path = LOG_FILE, chars: int = 1500) -> str:
    try:
        return path.read_text(errors="replace").strip()[-chars:]
    except OSError:
        return "(no update log found)"


def describe(status: str) -> tuple[str, str, str]:
    """(title, one-line explanation, embed kind) for a finished update."""
    return {
        "ok": ("Updated",
               "The new code started and stayed up.", "good"),
        "uptodate": ("Already up to date",
                     "Nothing new to pull.", "info"),
        "refused": ("Update refused — still on the old code",
                    "The new code has an error, so nothing was restarted and "
                    "the files were put back. The bot will boot into the old "
                    "version after a reboot too.", "urgent"),
        "rolledback": ("Update rolled back",
                       "The new code didn't stay up, so the previous version "
                       "was put back and restarted. That's what's running "
                       "now.", "urgent"),
        "failed": ("Update failed",
                   "Something went wrong and the bot may not be on the "
                   "version you expect. Check `!version`.", "urgent"),
        "interrupted": ("Update cut off partway",
                        "The update was stopped before it could check the "
                        "new code stayed up. `!version` shows which code "
                        "is running.", "warn"),
        "error": ("Update failed",
                  "update.sh stopped before restarting anything. The bot is "
                  "still on the old code.", "urgent"),
    }.get(status, ("Update finished", f"Status: `{status}`", "info"))
