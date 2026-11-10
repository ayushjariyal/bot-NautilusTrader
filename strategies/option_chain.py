"""
Option discovery: turn NautilusTrader instruments into plain contract descriptions and
find the call/put pairs that can form a straddle.

Why a separate, plain-Python module
-----------------------------------
NautilusTrader instruments use special types: the strike is a Price, the underlying is
a Currency object, the option kind is an OptionKind enum. Comparing those carelessly
causes bugs (for example Currency != "BTC" raises TypeError, see guide 03A). So we
convert each option ONCE into an OptionContract made of ordinary Python values:
str for names, Decimal for every number (exact decimal arithmetic, no float rounding),
int for timestamps. Everything after that point is plain Python and can be unit-tested
without an exchange connection.

Facts observed on Bybit Testnet (2026-11-08, 644 contracts):
  all BTC, USDT-settled and USDT-quoted, multiplier 1, tick 5 USDT, quantity step 0.01,
  minimum 0.01, maximum 10000, expiries at 08:00 UTC. The instruments report fees and
  margin as 0 (the adapter does not fill them), so nothing here relies on those fields.

Who uses this module
--------------------
strategies/btc_straddle.py calls contracts_from_instruments(), filter_contracts() and
find_call_put_pairs() in on_start(). Milestone 5 (instrument_selector.py) picks the ATM
pair from the result. scripts/check_testnet_market_data.py reuses the same functions.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal


NANOSECONDS_PER_HOUR = 3600 * 1_000_000_000

CALL = "CALL"
PUT = "PUT"


@dataclass(frozen=True)
class OptionContract:
    """
    One option contract, described with plain Python values.

    Fields (type: meaning)
    ----------------------
    instrument_id (str): NautilusTrader ID, e.g. "BTC-27NOV26-76000-C-USDT-OPTION.BYBIT".
        The strategy converts it back with InstrumentId.from_str() to subscribe or trade.
    underlying (str): asset the option is on, e.g. "BTC".
    option_kind (str): "CALL" or "PUT".
    strike (Decimal): strike price in the quote currency, e.g. Decimal("76000").
    expiry_ns (int): expiry time as nanoseconds since 1970-01-01 UTC.
    settlement_currency (str): currency profits/losses are paid in, e.g. "USDT".
    quote_currency (str): currency the premium is quoted in, e.g. "USDT".
    multiplier (Decimal): underlying units per 1.0 of quantity (Bybit: 1, so a quantity
        of 0.01 is an option on 0.01 BTC).
    tick_size (Decimal): smallest price step (Bybit: 5 USDT).
    lot_step (Decimal): smallest quantity step (Bybit: 0.01).
    min_quantity (Decimal): smallest order quantity allowed (Bybit: 0.01).
    max_quantity (Decimal or None): largest order quantity, None if not published.
    price_precision (int): decimals in a price (Bybit: 0).
    size_precision (int): decimals in a quantity (Bybit: 2).
    is_inverse (bool): True if the contract is settled in the underlying (Bybit: False).
    """

    instrument_id: str
    underlying: str
    option_kind: str
    strike: Decimal
    expiry_ns: int
    settlement_currency: str
    quote_currency: str
    multiplier: Decimal
    tick_size: Decimal
    lot_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal | None
    price_precision: int
    size_precision: int
    is_inverse: bool

    def expiry_utc(self):
        """Expiry as a timezone-aware datetime (UTC), for logging and display."""
        seconds = self.expiry_ns / 1_000_000_000
        return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)

    def hours_to_expiry(self, now_ns):
        """Hours from now_ns until expiry (negative if already expired). Returns float."""
        return (self.expiry_ns - now_ns) / NANOSECONDS_PER_HOUR


@dataclass(frozen=True)
class StraddlePair:
    """
    A call and a put with the same expiry and strike, and compatible contract specs.
    This is exactly what a straddle needs; Milestone 5 picks the pair nearest ATM.
    """

    expiry_ns: int
    strike: Decimal
    call: OptionContract
    put: OptionContract


def contract_from_instrument(instrument):
    """
    Convert one NautilusTrader instrument into an OptionContract.

    Parameters: instrument - any NautilusTrader instrument (from cache.instruments()).
    Returns: OptionContract, or None if the instrument is not an option.

    How: option instruments (CryptoOption) have an `option_kind` attribute; other
    instruments do not. Price/Quantity values are converted with as_decimal(), Currency
    objects with .code, and the OptionKind enum with .name ("CALL" / "PUT").
    """
    if not hasattr(instrument, "option_kind"):
        return None

    max_quantity = None
    if instrument.max_quantity is not None:
        max_quantity = instrument.max_quantity.as_decimal()

    min_quantity = instrument.size_increment.as_decimal()
    if instrument.min_quantity is not None:
        min_quantity = instrument.min_quantity.as_decimal()

    return OptionContract(
        instrument_id=str(instrument.id),
        underlying=instrument.underlying.code,
        option_kind=instrument.option_kind.name,
        strike=instrument.strike_price.as_decimal(),
        expiry_ns=int(instrument.expiration_ns),
        settlement_currency=instrument.settlement_currency.code,
        quote_currency=instrument.quote_currency.code,
        multiplier=instrument.multiplier.as_decimal(),
        tick_size=instrument.price_increment.as_decimal(),
        lot_step=instrument.size_increment.as_decimal(),
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        is_inverse=bool(instrument.is_inverse),
    )


def contracts_from_instruments(instruments):
    """
    Convert every option in `instruments`; non-options are skipped.

    Parameters: instruments (iterable of NautilusTrader instruments).
    Returns: list[OptionContract].
    """
    contracts = []
    for instrument in instruments:
        contract = contract_from_instrument(instrument)
        if contract is not None:
            contracts.append(contract)
    return contracts


def filter_contracts(contracts, underlying, now_ns, min_hours_to_expiry):
    """
    Keep only contracts on `underlying` that expire at least `min_hours_to_expiry` from now.

    Why a minimum time to expiry: an option expiring within hours can settle while the
    strategy is running, and its price swings violently near expiry. The strategy
    config sets the limit (default 24 hours).

    Parameters: contracts (list[OptionContract]); underlying (str, e.g. "BTC");
    now_ns (int, current time in ns); min_hours_to_expiry (float).
    Returns: list[OptionContract] in the original order.
    """
    kept = []
    for contract in contracts:
        if contract.underlying != underlying:
            continue
        if contract.hours_to_expiry(now_ns) < min_hours_to_expiry:
            continue
        kept.append(contract)
    return kept


def group_by_expiry(contracts):
    """
    Group contracts by expiry.

    Returns: dict {expiry_ns: list[OptionContract]} with keys in ascending order
    (nearest expiry first; Python dicts keep insertion order).
    """
    groups = {}
    for contract in contracts:
        if contract.expiry_ns not in groups:
            groups[contract.expiry_ns] = []
        groups[contract.expiry_ns].append(contract)

    ordered = {}
    for expiry_ns in sorted(groups.keys()):
        ordered[expiry_ns] = groups[expiry_ns]
    return ordered


def pair_problem(call, put):
    """
    Explain why a call and a put cannot be traded together as one straddle.

    A straddle compares and adds the premiums of both legs with the SAME quantity, so
    the legs must share expiry, strike, currencies and contract sizes.

    Parameters: call, put (OptionContract).
    Returns: str describing the first problem found, or None if they are compatible.
    """
    if call.option_kind != CALL:
        return f"{call.instrument_id} is not a call"
    if put.option_kind != PUT:
        return f"{put.instrument_id} is not a put"
    if call.expiry_ns != put.expiry_ns:
        return "different expiry"
    if call.strike != put.strike:
        return "different strike"
    if call.underlying != put.underlying:
        return "different underlying"
    if call.settlement_currency != put.settlement_currency:
        return "different settlement currency"
    if call.quote_currency != put.quote_currency:
        return "different quote currency"
    if call.multiplier != put.multiplier:
        return "different multiplier"
    if call.lot_step != put.lot_step:
        return "different quantity step"
    if call.min_quantity != put.min_quantity:
        return "different minimum quantity"
    if call.is_inverse != put.is_inverse:
        return "one leg is inverse, the other is not"
    return None


def find_call_put_pairs(contracts):
    """
    Find every (expiry, strike) that has both a call and a compatible put.

    Parameters: contracts (list[OptionContract]), usually already filtered.
    Returns: list[StraddlePair] sorted by expiry, then strike.

    How: index calls and puts by the key (expiry_ns, strike); every key present in
    both indexes is a candidate; pair_problem() rejects incompatible candidates.
    """
    calls_by_key = {}
    puts_by_key = {}
    for contract in contracts:
        key = (contract.expiry_ns, contract.strike)
        if contract.option_kind == CALL:
            calls_by_key[key] = contract
        elif contract.option_kind == PUT:
            puts_by_key[key] = contract

    pairs = []
    for key in sorted(calls_by_key.keys()):
        if key not in puts_by_key:
            continue
        call = calls_by_key[key]
        put = puts_by_key[key]
        if pair_problem(call, put) is not None:
            continue
        expiry_ns, strike = key
        pairs.append(StraddlePair(expiry_ns=expiry_ns, strike=strike, call=call, put=put))
    return pairs


def summarize_chain(all_contracts, usable_contracts, pairs, underlying, min_hours_to_expiry):
    """
    Human-readable summary for the strategy log.

    Parameters: all_contracts (every option loaded), usable_contracts (after filtering),
    pairs (list[StraddlePair]), underlying (str), min_hours_to_expiry (float).
    Returns: list[str].
    """
    lines = [
        f"Option contracts loaded: {len(all_contracts)}",
        f"{underlying} contracts expiring in >= {min_hours_to_expiry:g} h: {len(usable_contracts)}",
        f"Call/put pairs usable for a straddle: {len(pairs)}",
    ]

    pair_count_by_expiry = {}
    for pair in pairs:
        if pair.expiry_ns not in pair_count_by_expiry:
            pair_count_by_expiry[pair.expiry_ns] = 0
        pair_count_by_expiry[pair.expiry_ns] += 1

    for expiry_ns, contracts in group_by_expiry(usable_contracts).items():
        expiry_text = contracts[0].expiry_utc().strftime("%Y-%m-%d %H:%M UTC")
        pair_count = pair_count_by_expiry.get(expiry_ns, 0)
        lines.append(f"  expiry {expiry_text}: {len(contracts)} contracts, {pair_count} pairs")
    return lines


def round_down_to_tick(price, tick_size):
    """
    Round a price DOWN to a multiple of the contract's tick size.

    Bybit rejects prices that are not multiples of the tick (5 USDT on Testnet options).
    NautilusTrader's instrument.make_price() only fixes the number of decimals, not the
    tick, so we round ourselves. For a SELL order at the bid, rounding down can only make
    the order more willing to trade (a lower ask price still matches the bid).

    Parameters: price, tick_size (Decimal). Returns: Decimal.
    Example: round_down_to_tick(Decimal("1203"), Decimal("5")) -> Decimal("1200").
    """
    steps = (price / tick_size).to_integral_value(rounding="ROUND_FLOOR")
    return steps * tick_size


def round_up_to_tick(price, tick_size):
    """
    Round a price UP to a multiple of the tick size.

    Used for BUY orders (the buyback): rounding up keeps the limit price at or above the
    ask, so the order can still trade. Example: round_up_to_tick(Decimal("1221"), 5) -> 1225.
    Parameters: price, tick_size (Decimal). Returns: Decimal.
    """
    steps = (price / tick_size).to_integral_value(rounding="ROUND_CEILING")
    return steps * tick_size


def describe_contract(contract):
    """One-line contract specification for logs, e.g. multiplier, tick and lot sizes."""
    return (
        f"{contract.instrument_id}: {contract.option_kind} strike={contract.strike} "
        f"expiry={contract.expiry_utc().strftime('%Y-%m-%d %H:%M UTC')} "
        f"settle={contract.settlement_currency} quote={contract.quote_currency} "
        f"multiplier={contract.multiplier} tick={contract.tick_size} step={contract.lot_step} "
        f"min_qty={contract.min_quantity} max_qty={contract.max_quantity}"
    )
