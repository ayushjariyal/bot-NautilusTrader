"""
Unit tests for data/market_data.py: LegQuote and the "zero means missing" rule.
Quotes here are SIMULATED. No network is used.
"""

from decimal import Decimal

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from data.market_data import describe_quote
from data.market_data import leg_quote_from_tick
from data.market_data import leg_quote_from_values


LEG = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
SECOND = 1_000_000_000


def make_tick(bid, ask, bid_size, ask_size, ts_ns=0):
    return QuoteTick(
        instrument_id=InstrumentId.from_str(LEG),
        bid_price=Price.from_str(str(bid)),
        ask_price=Price.from_str(str(ask)),
        bid_size=Quantity.from_str(str(bid_size)),
        ask_size=Quantity.from_str(str(ask_size)),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def test_two_sided_quote_keeps_prices_and_sizes():
    quote = leg_quote_from_values(LEG, "3000", "3030", "0.50", "0.20", 0)
    assert quote.bid == Decimal("3000")
    assert quote.ask == Decimal("3030")
    assert quote.bid_size == Decimal("0.50")
    assert quote.ask_size == Decimal("0.20")
    assert quote.is_two_sided()


def test_zero_price_means_no_quote_on_that_side():
    # Exactly what Bybit Testnet sends for an empty book (observed in Milestone 3A).
    quote = leg_quote_from_values(LEG, "0", "0", "0", "0", 0)
    assert quote.bid is None
    assert quote.ask is None
    assert quote.bid_size is None
    assert quote.is_two_sided() is False


def test_zero_size_also_means_no_quote():
    quote = leg_quote_from_values(LEG, "3000", "3030", "0", "0.10", 0)
    assert quote.has_bid() is False
    assert quote.has_ask() is True


def test_one_sided_quote_has_no_spread_or_mid():
    quote = leg_quote_from_values(LEG, "0", "2500", "0", "0.01", 0)
    assert quote.spread() is None
    assert quote.mid() is None
    assert quote.spread_pct() is None


def test_spread_and_mid_calculations():
    quote = leg_quote_from_values(LEG, "3000", "3030", "1", "1", 0)
    assert quote.spread() == Decimal("30")
    assert quote.mid() == Decimal("3015")
    assert round(quote.spread_pct(), 3) == Decimal("0.995")


def test_age_seconds():
    quote = leg_quote_from_values(LEG, "3000", "3030", "1", "1", received_ns=10 * SECOND)
    assert quote.age_seconds(now_ns=12 * SECOND) == 2.0


def test_conversion_from_real_quote_tick_object():
    tick = make_tick(1000, 2500, "0.02", "0.01", ts_ns=5 * SECOND)
    quote = leg_quote_from_tick(tick)
    assert quote.instrument_id == LEG
    assert quote.bid == Decimal("1000")
    assert quote.ask == Decimal("2500")
    assert quote.received_ns == 5 * SECOND


def test_conversion_of_empty_book_tick():
    quote = leg_quote_from_tick(make_tick(0, 0, "0.00", "0.00"))
    assert quote.is_two_sided() is False


def test_no_tick_gives_none():
    assert leg_quote_from_tick(None) is None


def test_describe_quote_texts():
    now = 1 * SECOND
    assert describe_quote(None, now) == "no quote received yet"
    empty = leg_quote_from_values(LEG, "0", "0", "0", "0", 0)
    assert describe_quote(empty, now) == "bid - | ask - | spread n/a | age 1.0 s"
    full = leg_quote_from_values(LEG, "3000", "3030", "0.5", "0.2", 0)
    assert describe_quote(full, now) == "bid 3000 x 0.5 | ask 3030 x 0.2 | spread 1.00% | age 1.0 s"
