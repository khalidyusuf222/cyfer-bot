"""
Tests for the OANDA adapter and the pair maths.

WHAT IS AND ISN'T COVERED
-------------------------
Everything here runs offline. The parsing, the order payload, the level
validation and the sizing are all pure functions fed handmade API
responses, so every branch is exercised without credentials.

The HTTP calls themselves are NOT tested — they can't be from here. The
first real order will be the first time those run. That is the reason for
starting on a practice account.
"""

import os

import oanda
import pairs


# ---------------------------------------------------------------------------
# Environment safety
# ---------------------------------------------------------------------------

def test_practice_is_the_default():
    """Anything but exactly 'live' must mean practice."""
    for value in (None, "", "practice", "PRACTICE", "demo", "nonsense",
                  "Live-ish", " live x"):
        if value is None:
            os.environ.pop("OANDA_ENV", None)
        else:
            os.environ["OANDA_ENV"] = value
        assert not oanda.is_live(), value
    os.environ["OANDA_ENV"] = "practice"
    print("PASS  anything but exactly 'live' means practice")


def test_live_needs_the_exact_word():
    os.environ["OANDA_ENV"] = "live"
    assert oanda.is_live()
    assert oanda._host() == oanda.LIVE_HOST
    os.environ["OANDA_ENV"] = "practice"
    assert oanda._host() == oanda.PRACTICE_HOST
    print("PASS  live and practice are different hosts entirely")


def test_live_uses_separate_credentials():
    """A practice token must not be able to reach the live account."""
    os.environ["OANDA_TOKEN"] = "practice-token"
    os.environ.pop("OANDA_LIVE_TOKEN", None)
    os.environ["OANDA_ENV"] = "live"
    try:
        oanda._token()
    except oanda.OandaError as e:
        assert "OANDA_LIVE_TOKEN" in str(e)
        os.environ["OANDA_ENV"] = "practice"
        print("PASS  live mode demands its own token, not the practice one")
        return
    os.environ["OANDA_ENV"] = "practice"
    raise AssertionError("live mode accepted the practice token")


# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------

def test_timeframe_mapping():
    for ours, theirs in (("5Min", "M5"), ("1Hour", "H1"),
                         ("4Hour", "H4"), ("1Day", "D")):
        assert oanda.granularity(ours) == theirs, ours
    print("PASS  our timeframe names map to OANDA granularities")


def test_unknown_timeframe_raises_rather_than_guessing():
    try:
        oanda.granularity("3Min")
    except oanda.MarketError as e:
        assert "Unsupported" in str(e)
        print("PASS  an unsupported timeframe raises instead of guessing")
        return
    raise AssertionError("accepted a timeframe OANDA doesn't have")


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------

def candle(o, h, l, c, complete=True, t="2026-09-15T10:00:00Z"):
    return {"complete": complete, "volume": 100, "time": t,
            "mid": {"o": str(o), "h": str(h), "l": str(l), "c": str(c)}}


def test_candles_parse_from_strings():
    """OANDA sends every price as a string."""
    bars = oanda.parse_candles({"candles": [candle(1.1000, 1.1050, 1.0990,
                                                   1.1020)]})
    assert len(bars) == 1
    b = bars[0]
    assert (b.open, b.high, b.low, b.close) == (1.1000, 1.1050, 1.0990, 1.1020)
    print("PASS  string prices parse to floats")


def test_incomplete_candle_is_dropped():
    """
    The newest candle is still forming — its close changes every tick.
    Including it means the strategy reads a different chart every minute.
    """
    payload = {"candles": [candle(1.10, 1.11, 1.09, 1.105),
                           candle(1.105, 1.12, 1.10, 1.115, complete=False)]}
    assert len(oanda.parse_candles(payload)) == 1
    assert len(oanda.parse_candles(payload, include_incomplete=True)) == 2
    print("PASS  the still-forming candle is dropped by default")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def pricing(sym="EUR_USD", bid="1.10000", ask="1.10012", tradeable=True):
    return {"prices": [{"instrument": sym, "tradeable": tradeable,
                        "bids": [{"price": bid}], "asks": [{"price": ask}]}]}


def test_mid_price_sits_between_bid_and_ask():
    mid = oanda.parse_prices(pricing())["EUR_USD"]
    assert abs(mid - 1.10006) < 1e-9, mid
    print("PASS  mid price is the midpoint of bid and ask")


def test_spread_is_reported_in_pips():
    """[BOOK p12-13] The spread is the broker's fee and a real cost."""
    spread = oanda.parse_spreads(pricing())["EUR_USD"]
    assert abs(spread - 1.2) < 1e-6, spread
    print(f"PASS  spread reads as {spread:.1f} pips, not a raw decimal")


def test_yen_spread_uses_the_right_pip():
    """[BOOK p11] A yen pip is the second decimal, not the fourth."""
    spread = oanda.parse_spreads(
        pricing("USD_JPY", "155.000", "155.020"))["USD_JPY"]
    assert abs(spread - 2.0) < 1e-6, spread
    print("PASS  yen pairs measure spread in 0.01 pips")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def test_order_attaches_stop_and_target_on_fill():
    """
    [BOOK p47] The stop goes on WITH the order. stopLossOnFill means it
    exists at the broker from the instant the trade opens — it survives
    this bot dying.
    """
    p = pairs.parse("EUR_USD")
    body = oanda.build_order(p, 2531, 1.0950, 1.1100)["order"]

    assert body["type"] == "MARKET"
    assert body["instrument"] == "EUR_USD"
    assert body["units"] == "2531"
    assert body["stopLossOnFill"]["price"] == "1.09500"
    assert body["takeProfitOnFill"]["price"] == "1.11000"
    assert body["stopLossOnFill"]["timeInForce"] == "GTC"
    print("PASS  stop and target are attached to the order itself")


def test_negative_units_mean_a_short():
    """[BOOK p10] Selling the pair is selling the base currency."""
    body = oanda.build_order(pairs.parse("GBP_USD"), -1500, 1.2600,
                             1.2400)["order"]
    assert body["units"] == "-1500"
    print("PASS  a short is expressed as negative units")


def test_yen_prices_are_rounded_to_three_decimals():
    body = oanda.build_order(pairs.parse("USD_JPY"), 1000, 154.5012,
                             156.4987)["order"]
    assert body["stopLossOnFill"]["price"] == "154.501"
    assert body["takeProfitOnFill"]["price"] == "156.499"
    print("PASS  yen pairs are priced to 3 decimals, majors to 5")


def test_zero_units_is_refused():
    try:
        oanda.build_order(pairs.parse("EUR_USD"), 0, 1.09, 1.11)
    except oanda.OandaError:
        print("PASS  an order for zero units is refused")
        return
    raise AssertionError("built an order for zero units")


# ---------------------------------------------------------------------------
# Level validation — the check that was missing before
# ---------------------------------------------------------------------------

def test_long_with_stop_above_entry_is_refused():
    """
    The exact failure that closed a trade in 59 seconds: the stop ended up
    above the entry, so it triggered instantly.
    """
    problems = oanda.validate_levels(1000, entry=1.1000, stop=1.1050,
                                     target=1.1100)
    assert any("closes the moment it opens" in p for p in problems), problems
    print("PASS  a long with the stop above entry is refused")


def test_short_levels_are_mirrored():
    ok = oanda.validate_levels(-1000, 1.1000, 1.1050, 1.0900)
    bad = oanda.validate_levels(-1000, 1.1000, 1.0950, 1.0900)
    assert ok == []
    assert any("not above entry" in p for p in bad), bad
    print("PASS  a short needs its stop above and target below")


def test_valid_long_passes_cleanly():
    assert oanda.validate_levels(2531, 1.1000, 1.0950, 1.1100) == []
    print("PASS  a correctly arranged long passes validation")


# ---------------------------------------------------------------------------
# Reading trades back
# ---------------------------------------------------------------------------

def test_closed_trade_reports_profit_directly():
    """
    OANDA gives realizedPL in the account currency. No reconstructing the
    result from which bracket leg happened to fill.
    """
    t = oanda.parse_trade({"trade": {
        "id": "1234", "state": "CLOSED", "instrument": "EUR_USD",
        "initialUnits": "2531", "price": "1.10000",
        "averageClosePrice": "1.11000", "realizedPL": "19.98",
        "unrealizedPL": "0", "closeTime": "2026-09-15T14:00:00Z"}})

    assert t.is_closed
    assert t.realised_pl == 19.98
    assert t.close_price == 1.11
    print("PASS  a closed trade reports its profit directly")


def test_open_trade_has_no_close_price():
    t = oanda.parse_trade({"trade": {
        "id": "1", "state": "OPEN", "instrument": "EUR_USD",
        "initialUnits": "1000", "price": "1.1000",
        "realizedPL": "0", "unrealizedPL": "3.20"}})
    assert not t.is_closed
    assert t.close_price is None
    assert t.unrealised_pl == 3.20
    print("PASS  an open trade has unrealised profit and no close price")


# ---------------------------------------------------------------------------
# Sizing — the reason for the switch
# ---------------------------------------------------------------------------

def test_sizing_is_exact_where_shares_were_lumpy():
    """
    A £1,000 account risking 1% sizes a forex trade to the unit. The same
    budget on a $762 share rounds, and the rounding is a real sizing error.
    """
    p = pairs.parse("EUR_USD")
    s = pairs.size_position(p, entry=1.1000, stop=1.0950,
                            risk_account_ccy=10.0,
                            quote_to_account_rate=0.79, target=1.1100)

    assert s.units == 2531, s.units
    assert abs(s.risk_account - 10.0) < 0.01, s.risk_account
    assert abs(s.stop_pips - 50) < 0.01
    assert abs(s.reward_account - 20.0) < 0.05, s.reward_account
    print(f"PASS  {s.units:,} units risks £{s.risk_account:.2f} to make "
          f"£{s.reward_account:.2f}")


def test_sizing_rounds_down_so_risk_is_never_exceeded():
    p = pairs.parse("EUR_USD")
    s = pairs.size_position(p, 1.1000, 1.0950, 10.0, 0.79)
    assert s.risk_account <= 10.0 + 1e-9, s.risk_account
    print("PASS  units round DOWN — the risk budget is a ceiling")


def test_reward_to_risk_survives_sizing():
    """A 2:1 plan must still be 2:1 after the units are worked out."""
    p = pairs.parse("EUR_USD")
    s = pairs.size_position(p, 1.1000, 1.0950, 10.0, 0.79, target=1.1100)
    assert abs(s.reward_account / s.risk_account - 2.0) < 0.01
    print("PASS  2:1 on the chart is still 2:1 in the account")


def test_tight_stop_warns_about_the_spread():
    """[BOOK p8] A stop inside the spread gets taken out by the spread."""
    p = pairs.parse("EUR_USD")
    s = pairs.size_position(p, 1.1000, 1.09980, 10.0, 0.79)
    assert any("spread" in n for n in s.notes), s.notes
    print("PASS  a 2-pip stop is flagged against the cost of the spread")


def test_zero_risk_is_refused():
    p = pairs.parse("EUR_USD")
    for bad in (lambda: pairs.size_position(p, 1.1, 1.1, 10, 0.79),
                lambda: pairs.size_position(p, 1.1, 1.09, 0, 0.79),
                lambda: pairs.size_position(p, 0, 1.09, 10, 0.79),
                lambda: pairs.size_position(p, 1.1, 1.09, 10, 0)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("accepted an impossible sizing input")
    print("PASS  impossible sizing inputs are refused")


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------

def test_pair_parsing_accepts_every_spelling():
    for text in ("EUR_USD", "EUR/USD", "eurusd", "eur-usd", " EUR/usd "):
        assert pairs.parse(text).symbol == "EUR_USD", text
    print("PASS  pairs parse from any common spelling")


def test_pip_sizes():
    """[BOOK p11] 0.0001 for most, 0.01 for yen."""
    assert pairs.parse("EUR_USD").pip == 0.0001
    assert pairs.parse("USD_JPY").pip == 0.01
    assert pairs.parse("EUR_JPY").pip == 0.01
    print("PASS  yen pairs use a 0.01 pip, everything else 0.0001")


def test_pair_classification():
    """[BOOK p7] Majors include USD; minors are major-crosses without it."""
    assert pairs.parse("EUR_USD").kind == "major"
    assert pairs.parse("EUR_GBP").kind == "minor"
    assert pairs.parse("USD_TRY").kind == "exotic"
    print("PASS  majors, minors and exotics are classified correctly")


def test_watchlist_is_all_liquid_majors():
    wl = pairs.watchlist()
    assert len(wl) >= 3
    assert all(p.kind == "major" for p in wl), [p.display for p in wl]
    print(f"PASS  watchlist is {len(wl)} majors: "
          f"{', '.join(p.display for p in wl)}")


# ---------------------------------------------------------------------------
# When OANDA's servers fail
# ---------------------------------------------------------------------------

HTML_504 = ('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8"/>\n'
            '<title>Internal Server Error</title>\n</head></html>')


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload
        self.ok = 200 <= status < 300
        self.content = text.encode() if text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _fake_get(responses, calls):
    def fake(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return responses.pop(0)
    return fake


def test_an_html_error_page_becomes_one_sentence():
    msg = oanda._explain(_Resp(504, HTML_504))
    assert "<" not in msg and "timed out" in msg and "OANDA's side" in msg, msg
    j = oanda._explain(_Resp(400, '{"errorMessage":"Invalid value"}',
                             {"errorMessage": "Invalid value"}))
    assert j == "Invalid value", j
    print(f"PASS  HTML 504 page → \"{msg[:48]}…\"")


def test_a_read_is_retried_after_a_504():
    from unittest.mock import patch
    os.environ.setdefault("OANDA_TOKEN", "test-token")
    calls = []
    responses = [_Resp(504, HTML_504), _Resp(200, "{}", {"candles": []})]
    with patch("oanda.requests.get", _fake_get(responses, calls)), \
         patch("oanda.time.sleep", lambda s: None):
        out = oanda._get("/v3/instruments/EUR_USD/candles", retries=2)
    assert out == {"candles": []} and len(calls) == 2, (out, len(calls))
    print("PASS  a 504 is retried and the second attempt's answer is used")


def test_without_retries_a_504_is_a_readable_server_error():
    from unittest.mock import patch
    os.environ.setdefault("OANDA_TOKEN", "test-token")
    calls = []
    with patch("oanda.requests.get", _fake_get([_Resp(504, HTML_504)], calls)):
        try:
            oanda._get("/v3/x")
        except oanda.OandaServerError as e:
            text = str(e)
        else:
            raise AssertionError("no error raised")
    assert len(calls) == 1 and "<" not in text and "504" in text, text
    print("PASS  the live scan doesn't retry, and the error has no HTML in it")


def test_a_rejected_token_is_never_retried():
    from unittest.mock import patch
    os.environ.setdefault("OANDA_TOKEN", "test-token")
    calls = []
    responses = [_Resp(401, "{}", {}), _Resp(200, "{}", {})]
    with patch("oanda.requests.get", _fake_get(responses, calls)), \
         patch("oanda.time.sleep", lambda s: None):
        try:
            oanda._get("/v3/x", retries=3)
        except oanda.OandaError as e:
            assert not isinstance(e, oanda.OandaServerError)
        else:
            raise AssertionError("no error raised")
    assert len(calls) == 1, len(calls)
    print("PASS  a bad token fails once — asking again can't fix it")


if __name__ == "__main__":
    os.environ.setdefault("OANDA_TOKEN", "test")
    os.environ.setdefault("OANDA_ACCOUNT", "001-001-1234567-001")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} tests passed.")
