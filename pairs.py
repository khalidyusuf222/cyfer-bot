"""
Currency pairs — pips, units, and position sizing.

WHY THIS FILE EXISTS
--------------------
Shares are lumpy. You cannot buy 0.4 of one inside a bracket order, so a
£10 risk on a $760 share either rounds to zero or balloons into a position
many times the account. That is what produced a $231,000 position on a
$100,000 paper account.

Currency units divide down to 1. A £1,000 account risking 1% can size a
EUR/USD trade exactly, every time, with no rounding and no minimum. That
is the whole reason for switching markets.

SOURCE
------
Pip definitions and the base/quote convention are from the Cyfer Academy
guide, Part II. Page references are to that document.

Everything here is pure arithmetic. No network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ===========================================================================
# Pairs
# ===========================================================================

# [BOOK p6] The majors are built from the largest economies' currencies.
# [BOOK p7] A "major" pair always includes USD plus one other major.
MAJORS = ("USD", "EUR", "JPY", "GBP", "CHF")

# [BOOK p6] Tied to their countries' commodity exports.
COMMODITY = ("AUD", "NZD", "CAD")


@dataclass(frozen=True)
class Pair:
    """
    One tradeable pair.

    [BOOK p10] The first currency listed is the BASE — the one you are
    buying or selling. The second is the QUOTE — the one you pay or
    receive. GBP/USD at 1.2500 means one pound costs 1.25 dollars.
    """
    base: str
    quote: str

    @property
    def symbol(self) -> str:
        """OANDA's format: EUR_USD."""
        return f"{self.base}_{self.quote}"

    @property
    def display(self) -> str:
        """The way the book writes it: EUR/USD."""
        return f"{self.base}/{self.quote}"

    @property
    def pip(self) -> float:
        """
        [BOOK p11] A pip is the fourth decimal place — 0.0001 — for most
        pairs. Yen pairs are the exception: quoted to two decimals, so a
        pip is 0.01.
        """
        return 0.01 if self.quote == "JPY" else 0.0001

    @property
    def displayed_decimals(self) -> int:
        return 3 if self.quote == "JPY" else 5

    @property
    def kind(self) -> str:
        """[BOOK p7] major, minor (no USD), or exotic."""
        both_major = self.base in MAJORS and self.quote in MAJORS
        both_known = (self.base in MAJORS + COMMODITY
                      and self.quote in MAJORS + COMMODITY)
        if "USD" in (self.base, self.quote) and both_known:
            return "major"
        if both_known:
            return "minor"
        return "exotic"

    def pips_between(self, a: float, b: float) -> float:
        """How many pips apart two prices are."""
        return abs(a - b) / self.pip

    def round_price(self, price: float) -> float:
        return round(price, self.displayed_decimals)


def parse(text: str) -> Pair:
    """Accept EUR_USD, EUR/USD, eurusd — all the ways it gets typed."""
    t = text.strip().upper().replace("/", "_").replace("-", "_")
    if "_" in t:
        base, quote = t.split("_", 1)
    elif len(t) == 6:
        base, quote = t[:3], t[3:]
    else:
        raise ValueError(f"Can't read '{text}' as a currency pair.")
    if len(base) != 3 or len(quote) != 3:
        raise ValueError(f"Can't read '{text}' as a currency pair.")
    return Pair(base, quote)


# ===========================================================================
# Position sizing  [BOOK p14, p53]
# ===========================================================================

@dataclass(frozen=True)
class Size:
    units: int
    risk_quote: float          # what's at stake, in the QUOTE currency
    risk_account: float        # the same, in the account's currency
    stop_distance: float       # in price terms
    stop_pips: float
    reward_account: float
    notes: list[str]

    @property
    def lots(self) -> float:
        """
        [BOOK p14] 100,000 units is a standard lot, 10,000 a mini,
        1,000 a micro. Shown for readability — orders are placed in units.
        """
        return self.units / 100_000


def size_position(pair: Pair,
                  entry: float,
                  stop: float,
                  risk_account_ccy: float,
                  quote_to_account_rate: float,
                  target: Optional[float] = None,
                  min_units: int = 1,
                  max_units: int = 10_000_000) -> Size:
    """
    How many units to trade so that being stopped out costs exactly the
    risk budget.

        units = risk_in_quote_currency / stop_distance_in_price

    Worked through for EUR/USD, a £1,000 account risking 1%, entry 1.1000,
    stop 1.0950, and USD/GBP at 0.79:

        risk in GBP       £10
        risk in USD       10 / 0.79            = $12.66
        stop distance     1.1000 - 1.0950      = 0.0050  (50 pips)
        units             12.66 / 0.0050       = 2,532

    2,532 units of EUR. Not a round lot, and it does not need to be — which
    is exactly the point. The same account could not have bought one share
    of SPY without breaching its risk limit.

    quote_to_account_rate converts one unit of the QUOTE currency into the
    account currency. For a GBP account trading EUR/USD, that's USD→GBP.
    """
    notes: list[str] = []

    if entry <= 0:
        raise ValueError("Entry price must be positive.")
    if stop == entry:
        raise ValueError("Stop cannot equal entry — that is zero risk.")
    if risk_account_ccy <= 0:
        raise ValueError("Risk budget must be positive.")
    if quote_to_account_rate <= 0:
        raise ValueError("Quote-to-account rate must be positive.")

    stop_distance = abs(entry - stop)
    risk_quote = risk_account_ccy / quote_to_account_rate
    raw_units = risk_quote / stop_distance

    units = int(raw_units)
    if units < min_units:
        notes.append(
            f"Sizing wanted {raw_units:.2f} units, below the {min_units}-unit "
            f"minimum. Rounded up — this trade risks slightly more than "
            f"budgeted.")
        units = min_units
    if units > max_units:
        notes.append(f"Capped at {max_units:,} units.")
        units = max_units

    actual_risk_quote = units * stop_distance
    actual_risk_account = actual_risk_quote * quote_to_account_rate

    reward_account = 0.0
    if target is not None:
        reward_account = units * abs(target - entry) * quote_to_account_rate

    stop_pips = pair.pips_between(entry, stop)
    if stop_pips < 5:
        notes.append(
            f"Stop is only {stop_pips:.1f} pips away. [BOOK p8] The spread "
            f"is a real cost, and a stop this tight can be taken out by it "
            f"alone.")

    return Size(units=units,
                risk_quote=actual_risk_quote,
                risk_account=actual_risk_account,
                stop_distance=stop_distance,
                stop_pips=stop_pips,
                reward_account=reward_account,
                notes=notes)


def describe_size(pair: Pair, s: Size, symbol: str = "£") -> str:
    """One human-readable line for Discord."""
    u = s.units
    if u >= 100_000:
        lot_note = f" ({u / 100_000:.2f} standard lots)"
    elif u >= 10_000:
        lot_note = f" ({u / 10_000:.2f} mini lots)"
    elif u >= 1_000:
        lot_note = f" ({u / 1_000:.2f} micro lots)"
    else:
        lot_note = ""

    return (f"**{s.units:,} units** of {pair.base}{lot_note}\n"
            f"Stop {s.stop_pips:.1f} pips away · "
            f"risking {symbol}{s.risk_account:,.2f}"
            + (f" to make {symbol}{s.reward_account:,.2f}"
               if s.reward_account else ""))


# ===========================================================================
# The watchlist
# ===========================================================================

# [BOOK p8] EUR/USD is the most liquid pair, with the narrowest spreads.
# [BOOK p9] The dollar is on one side of about 85% of all trades.
#
# [CHOICE] Kept to four majors. Every one is liquid enough that the spread
# is small, which matters more at a 1% risk budget than having choice.
DEFAULT_PAIRS = ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD")


def watchlist() -> list[Pair]:
    return [parse(p) for p in DEFAULT_PAIRS]
