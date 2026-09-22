"""
Tests for the data-source switch in data.py.

data.py decides its backend at import time from the environment, so each
case runs in a fresh subprocess with its own environment rather than
fighting the module cache.

The case that matters: an old .env from the shares days carries
DATA_SOURCE=alpaca, and moving the bot to a new folder copies it across.
With a forex broker that would make every scan fail silently.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def source_for(**env) -> tuple[str, str]:
    """Import data.py under this environment; return (source, stderr)."""
    clean = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    clean.update(env)
    r = subprocess.run(
        [sys.executable, "-c", "import data; print(data.SOURCE_NAME)"],
        cwd=HERE, env=clean, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip(), r.stderr


def test_defaults_to_oanda():
    src, _ = source_for()
    assert src == "oanda", src
    print("PASS  with nothing set, prices come from OANDA")


def test_old_env_cannot_point_forex_at_alpaca():
    """The exact state a copied shares-era .env produces."""
    src, err = source_for(BROKER="oanda", DATA_SOURCE="alpaca")
    assert src == "oanda", src
    assert "ignored" in err and "alpaca" in err, err
    print("PASS  BROKER=oanda + DATA_SOURCE=alpaca uses OANDA, and warns")


def test_yahoo_is_overridden_too():
    src, err = source_for(BROKER="oanda", DATA_SOURCE="yahoo")
    assert src == "oanda" and "ignored" in err
    print("PASS  BROKER=oanda + DATA_SOURCE=yahoo uses OANDA, and warns")


def test_no_warning_when_consistent():
    src, err = source_for(BROKER="oanda", DATA_SOURCE="oanda")
    assert src == "oanda" and "ignored" not in err, err
    print("PASS  a consistent setup raises no warning")


def test_shares_setup_is_still_respected():
    """The guard is for forex only — a shares setup keeps its own choice."""
    src, err = source_for(BROKER="alpaca", DATA_SOURCE="yahoo")
    assert src == "yahoo" and "ignored" not in err, (src, err)
    print("PASS  BROKER=alpaca keeps DATA_SOURCE=yahoo untouched")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
