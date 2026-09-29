"""
Tests for bot.py as a whole. Plain functions, run with:

    python3 test_bot.py

bot.py can't be exercised without Discord, but it can be LOADED. A command
name clashing with another command's alias only fails at import, which on
the server means the bot won't start. This catches that before a push.
"""

import os

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_CHANNEL_ID", "1")


def test_bot_loads_and_every_command_name_is_unique():
    import bot                                  # raises on any clash
    names = []
    for c in bot.bot.commands:
        names += [c.name, *c.aliases]
    assert len(names) == len(set(names)), sorted(n for n in names
                                                 if names.count(n) > 1)
    for needed in ("whynot", "backtest", "update", "chart", "auto"):
        assert bot.bot.get_command(needed) is not None, needed
    print(f"PASS  bot.py loads; {len(bot.bot.commands)} commands, "
          f"{len(names)} names, no clashes")


def test_whynot_tally_counts_what_blocked_a_scan():
    import bot, cyfer
    bot._why["pairs"].clear()
    sig = cyfer.Signal("EUR_USD", "bullish", entry=1.1, stop=1.09,
                       target=1.12, level=cyfer.Level(1.095, "support", 3),
                       trend_ok=True)              # no trigger candle
    bot._tally("EUR_USD", sig)
    bot._tally("EUR_USD", None)
    p = bot._why["pairs"]["EUR_USD"]
    assert p["scans"] == 2 and p["qualified"] == 0 and p["near"] == 1, p
    assert p["blocked"]["trigger candle"] == 1, p["blocked"]
    assert p["blocked"]["not enough data"] == 1, p["blocked"]
    print("PASS  !whynot counts each scan and names what was missing")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
