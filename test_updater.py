"""
Tests for updater.py and update.sh. Plain functions, run with:

    python3 test_updater.py

The update.sh tests run the real script against a throwaway git repo, with
fake `systemctl` and `journalctl` commands, so nothing touches a real
service and nothing needs the network.
"""

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import updater

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# updater.py
# ---------------------------------------------------------------------------

def test_launch_runs_update_sh_as_its_own_unit():
    cmd = updater.launch_command(Path("/root/cyfer-bot"))
    assert cmd[0] == "systemd-run"
    assert "--unit=cyferbot-update" in cmd
    assert "--collect" in cmd
    assert "--setenv=BOT_DIR=/root/cyfer-bot" in cmd
    assert cmd[-2:] == ["bash", "/root/cyfer-bot/update.sh"]


def test_every_final_status_has_a_message():
    for st in updater.FINAL:
        title, why, kind = updater.describe(st)
        assert title and why, st
        assert title != "Update finished", st


def test_status_groups_dont_overlap():
    assert not updater.IN_PROGRESS & updater.FINAL
    assert not updater.BEFORE_RESTART & updater.AFTER_RESTART


def test_status_words_match_update_sh():
    code = "\n".join(line for line in
                     (HERE / "update.sh").read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    written = set(re.findall(r"\bstatus (\w+)", code))
    known = updater.IN_PROGRESS | updater.FINAL
    assert written <= known, written - known
    assert known <= written, known - written


def test_read_and_clear_status():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "update.status"
        assert updater.read_status(p) is None
        p.write_text("ok\n")
        assert updater.read_status(p) == "ok"
        updater.clear(p)
        assert not p.exists()
        updater.clear(p)                  # already gone: no error


def test_log_tail_keeps_the_end():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "update.log"
        assert "no update log" in updater.log_tail(p)
        p.write_text("x" * 3000 + "END")
        tail = updater.log_tail(p, chars=10)
        assert tail.endswith("END") and len(tail) == 10


def test_update_status_is_not_committed():
    ignored = (HERE / ".gitignore").read_text().split()
    assert "update.status" in ignored
    assert "*.log" in ignored


# ---------------------------------------------------------------------------
# update.sh, run for real against a throwaway repo
# ---------------------------------------------------------------------------

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# Pretends to be systemctl. The "bot" crashes whenever crash_me.py is in
# the bot folder, so a commit adding that file is a bad update.
echo "$*" >> "$FAKE_LOG"
case "$1" in
    restart) exit 0 ;;
    is-active) [ ! -e "$BOT_DIR/crash_me.py" ] ;;
    show) echo 0 ;;
esac
"""

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


class _Sandbox:
    """A bare 'GitHub' repo, the bot's clone of it, and a second clone to
    push new commits from."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.origin = self.root / "origin.git"
        self.bot = self.root / "bot"
        self.dev = self.root / "dev"
        self.bin = self.root / "bin"
        self.calls = self.root / "systemctl.log"

        _git(self.root, "init", "--bare", "-b", "main", str(self.origin))
        _git(self.root, "clone", str(self.origin), str(self.dev))
        shutil.copy(HERE / "update.sh", self.dev / "update.sh")
        (self.dev / "app.py").write_text("x = 1\n")
        _git(self.dev, "add", ".")
        _git(self.dev, "commit", "-m", "first")
        _git(self.dev, "push", "origin", "HEAD:main")
        _git(self.root, "clone", str(self.origin), str(self.bot))

        self.bin.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL),
                           ("journalctl", "#!/usr/bin/env bash\necho log\n")):
            f = self.bin / name
            f.write_text(body)
            f.chmod(0o755)

    def push(self, name, text, msg):
        (self.dev / name).write_text(text)
        _git(self.dev, "add", ".")
        _git(self.dev, "commit", "-m", msg)
        _git(self.dev, "push", "origin", "HEAD:main")

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.bot,
                              capture_output=True, text=True).stdout.strip()

    def run(self):
        env = {**os.environ, "BOT_DIR": str(self.bot), "SERVICE": "fakebot",
               "SETTLE_SECONDS": "0", "FAKE_LOG": str(self.calls),
               "PATH": f"{self.bin}:{os.environ['PATH']}"}
        r = subprocess.run(["bash", str(self.bot / "update.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        status = (self.bot / "update.status").read_text().strip()
        return r.returncode, status

    def restarts(self):
        if not self.calls.exists():
            return 0
        return self.calls.read_text().count("restart fakebot")

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def test_update_sh_parses():
    r = subprocess.run(["bash", "-n", str(HERE / "update.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_nothing_new_means_uptodate_and_no_restart():
    sb = _Sandbox()
    try:
        rc, st = sb.run()
        assert (rc, st) == (0, "uptodate")
        assert sb.restarts() == 0
        assert "Already up to date" in (sb.bot / "update.log").read_text()
    finally:
        sb.close()


def test_good_update_restarts_and_reports_ok():
    sb = _Sandbox()
    try:
        sb.push("app.py", "x = 2\n", "good change")
        rc, st = sb.run()
        assert (rc, st) == (0, "ok")
        assert sb.restarts() == 1
        assert (sb.bot / "app.py").read_text() == "x = 2\n"
    finally:
        sb.close()


def test_syntax_error_is_refused_and_files_put_back():
    sb = _Sandbox()
    try:
        before = sb.head()
        sb.push("app.py", "def broken(:\n", "bad syntax")
        rc, st = sb.run()
        assert (rc, st) == (1, "refused")
        assert sb.restarts() == 0
        assert sb.head() == before
    finally:
        sb.close()


def test_code_that_wont_stay_up_is_rolled_back():
    sb = _Sandbox()
    try:
        before = sb.head()
        sb.push("crash_me.py", "x = 1\n", "parses but crashes")
        rc, st = sb.run()
        assert (rc, st) == (1, "rolledback")
        assert sb.restarts() == 2          # the new code, then the old
        assert sb.head() == before
        assert not (sb.bot / "crash_me.py").exists()
    finally:
        sb.close()


def test_killed_midway_reports_interrupted():
    # What an older bot's restart does to this script: systemd sends
    # SIGTERM to every process in the group, the script included.
    sb = _Sandbox()
    try:
        sb.push("app.py", "x = 2\n", "good change")
        env = {**os.environ, "BOT_DIR": str(sb.bot), "SERVICE": "fakebot",
               "SETTLE_SECONDS": "30", "FAKE_LOG": str(sb.calls),
               "PATH": f"{sb.bin}:{os.environ['PATH']}"}
        p = subprocess.Popen(["bash", str(sb.bot / "update.sh")], env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
        status = sb.bot / "update.status"
        for _ in range(100):
            if updater.read_status(status) == "restarting":
                break
            time.sleep(0.1)
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)
        assert updater.read_status(status) == "interrupted"
    finally:
        sb.close()


def test_unlinked_branch_reports_error_not_running():
    sb = _Sandbox()
    try:
        _git(sb.bot, "branch", "--unset-upstream")
        rc, st = sb.run()
        assert (rc, st) == (1, "error")
        assert sb.restarts() == 0
    finally:
        sb.close()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"All {len(tests)} tests passed.")
