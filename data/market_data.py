"""
Leg quotes: a clean, plain-Python view of the latest bid/ask of one option.

Where quotes live
-----------------
We do NOT keep our own quote store. NautilusTrader's Cache already keeps the latest
QuoteTick of every subscribed instrument, and the DataEngine updates it BEFORE calling
the strategy's on_quote_tick() (verified in data/engine.pyx, guide 02). So the strategy
reads self.cache.quote_tick(instrument_id) and converts it here.

Why a conversion is needed
--------------------------
On Bybit Testnet, an empty side of the order book arrives as price 0 and size 0
(observed in Milestone 3A: "bid 0 x 0.00 | ask 0 x 0.00"). A price of 0 is NOT a price:
treating it as one would make buying back an option look free. LegQuote stores a missing
side as None, so every later calculation must handle "no price" explicitly.

Who uses this module: strategies/btc_straddle.py (reading the legs' quotes),
strategies/liquidity_check.py (entry checks), and from Milestone 9 the risk manager.
"""

from dataclasses import dataclass
from decimal import Decimal


NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class LegQuote:
    """
    The latest top-of-book quote for one option leg.

    Fields
    ------
    instrument_id (str): the option, e.g. "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT".
    bid (Decimal or None): best price a buyer pays; None if nobody is bidding.
    ask (Decimal or None): best price a seller accepts; None if nobody is offering.
    bid_size (Decimal or None): quantity available at the bid; None with no bid.
    ask_size (Decimal or None): quantity available at the ask; None with no ask.
    received_ns (int): when NautilusTrader received the quote (QuoteTick.ts_init, local
        clock), used to detect stale data.
    """

    instrument_id: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    received_ns: int

    def has_bid(self):
        return self.bid is not None

    def has_ask(self):
        return self.ask is not None

    def is_two_sided(self):
        """True when both a bid and an ask exist."""
        return self.has_bid() and self.has_ask()

    def age_seconds(self, now_ns):
        """Seconds since the quote was received (float)."""
        return (now_ns - self.received_ns) / NANOSECONDS_PER_SECOND

    def spread(self):
        """ask - bid in USDT, or None if a side is missing."""
        if not self.is_two_sided():
            return None
        return self.ask - self.bid

    def mid(self):
        """(bid + ask) / 2, or None if a side is missing."""
        if not self.is_two_sided():
            return None
        return (self.bid + self.ask) / Decimal("2")

    def spread_pct(self):
        """
        Spread as a percentage of the mid price, or None if a side is missing or the mid
        is not positive. Example: bid 3000, ask 3030 -> spread 30 / mid 3015 = 0.995%.
        """
        mid = self.mid()
        if mid is None or mid <= 0:
            return None
        return self.spread() / mid * Decimal("100")


def side_or_none(price, size):
    """
    Turn one side of the book into (price, size), or (None, None) if the side is empty.

    Parameters: price, size (Decimal). A side counts as empty when its price OR its size
    is zero or negative (Bybit's way of saying "nothing here").
    Returns: tuple (Decimal or None, Decimal or None).
    """
    if price <= 0:
        return None, None
    if size <= 0:
        return None, None
    return price, size


def leg_quote_from_values(instrument_id, bid, ask, bid_size, ask_size, received_ns):
    """
    Build a LegQuote from raw numbers, applying the "zero means missing" rule.

    Parameters: instrument_id (str); bid, ask, bid_size, ask_size (Decimal or anything
    Decimal() accepts); received_ns (int).
    Returns: LegQuote.
    """
    clean_bid, clean_bid_size = side_or_none(Decimal(str(bid)), Decimal(str(bid_size)))
    clean_ask, clean_ask_size = side_or_none(Decimal(str(ask)), Decimal(str(ask_size)))
    return LegQuote(
        instrument_id=str(instrument_id),
        bid=clean_bid,
        ask=clean_ask,
        bid_size=clean_bid_size,
        ask_size=clean_ask_size,
        received_ns=int(received_ns),
    )


def leg_quote_from_tick(tick):
    """
    Convert a NautilusTrader QuoteTick (from cache.quote_tick()) into a LegQuote.

    Parameters: tick (QuoteTick or None).
    Returns: LegQuote, or None if there is no tick yet.
    """
    if tick is None:
        return None
    return leg_quote_from_values(
        instrument_id=tick.instrument_id,
        bid=tick.bid_price.as_decimal(),
        ask=tick.ask_price.as_decimal(),
        bid_size=tick.bid_size.as_decimal(),
        ask_size=tick.ask_size.as_decimal(),
        received_ns=tick.ts_init,
    )


def describe_quote(quote, now_ns):
    """
    One short text describing a quote for the status log, e.g.
    "bid 1000 x 0.02 | ask 2500 x 0.01 | spread 85.71% | age 0.4 s".
    """
    if quote is None:
        return "no quote received yet"

    if quote.has_bid():
        bid_text = f"bid {quote.bid} x {quote.bid_size}"
    else:
        bid_text = "bid -"
    if quote.has_ask():
        ask_text = f"ask {quote.ask} x {quote.ask_size}"
    else:
        ask_text = "ask -"

    spread_pct = quote.spread_pct()
    if spread_pct is None:
        spread_text = "spread n/a"
    else:
        spread_text = f"spread {spread_pct:.2f}%"

    return f"{bid_text} | {ask_text} | {spread_text} | age {quote.age_seconds(now_ns):.1f} s"
