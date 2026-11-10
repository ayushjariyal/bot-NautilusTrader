"""
Test helpers that build REAL NautilusTrader instrument objects (CryptoOption) without
any network connection.

Defaults copy the contract specification observed on Bybit Testnet on 2026-11-08:
USDT-settled and quoted, multiplier 1, tick 5, quantity step 0.01, min 0.01, max 10000.
These objects are SIMULATED test data, not Bybit data.
"""

import datetime

from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.currencies import ETH
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


CURRENCIES = {"BTC": BTC, "ETH": ETH, "USDT": USDT, "USDC": USDC}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# A fixed "now" for tests: 2026-11-08 12:00 UTC.
TEST_NOW = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.timezone.utc)
TEST_NOW_NS = int(TEST_NOW.timestamp()) * 1_000_000_000


def expiry_at(days_from_now, hour=8):
    """A datetime `days_from_now` days after TEST_NOW, at `hour`:00 UTC (Bybit: 08:00)."""
    day = TEST_NOW + datetime.timedelta(days=days_from_now)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def make_option(
    strike,
    kind="CALL",
    expiry=None,
    underlying="BTC",
    settlement="USDT",
    quote="USDT",
    multiplier="1",
    tick="5",
    step="0.01",
    min_quantity="0.01",
    max_quantity="10000",
):
    """
    Build one CryptoOption. `strike` may be an int or str; `kind` is "CALL" or "PUT";
    `expiry` is a timezone-aware datetime (default: 19 days after TEST_NOW, 08:00 UTC).
    """
    if expiry is None:
        expiry = expiry_at(19)

    date_code = f"{expiry.day}{MONTHS[expiry.month - 1]}{expiry.year % 100:02d}"
    kind_letter = "C" if kind == "CALL" else "P"
    raw = f"{underlying}-{date_code}-{strike}-{kind_letter}-{settlement}"
    option_kind = OptionKind.CALL if kind == "CALL" else OptionKind.PUT

    size_precision = len(step.split(".")[1]) if "." in step else 0
    price_precision = len(tick.split(".")[1]) if "." in tick else 0

    return CryptoOption(
        instrument_id=InstrumentId.from_str(f"{raw}-OPTION.BYBIT"),
        raw_symbol=Symbol(raw),
        underlying=CURRENCIES[underlying],
        quote_currency=CURRENCIES[quote],
        settlement_currency=CURRENCIES[settlement],
        is_inverse=False,
        option_kind=option_kind,
        strike_price=Price.from_str(str(strike)),
        activation_ns=0,
        expiration_ns=int(expiry.timestamp()) * 1_000_000_000,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(tick),
        size_increment=Quantity.from_str(step),
        ts_event=0,
        ts_init=0,
        multiplier=Quantity.from_str(multiplier),
        lot_size=Quantity.from_str(step),
        max_quantity=Quantity.from_str(max_quantity),
        min_quantity=Quantity.from_str(min_quantity),
    )


def make_chain(strikes, expiry=None, **kwargs):
    """A call AND a put for every strike in `strikes`, all with the same expiry."""
    instruments = []
    for strike in strikes:
        instruments.append(make_option(strike, "CALL", expiry, **kwargs))
        instruments.append(make_option(strike, "PUT", expiry, **kwargs))
    return instruments
