"""
ATM selection: choose the call/put pair whose strike is nearest to the BTC reference price.

The reference price
-------------------
Bybit sends, with every option ticker update, an "underlying price" for that option's
expiry. NautilusTrader delivers it as OptionGreeks.underlying_price (an optional float).
Bybit documents it as the per-expiry reference used for ATM determination, so it is the
"BTC underlying reference price" the assignment asks for. Because it is per expiry, the
strategy reads it from a contract of the SAME expiry it is going to trade.

Selection rules (all in this file, all plain Python, all unit-tested)
---------------------------------------------------------------------
1. Target expiry: the nearest expiry among the usable pairs (Milestone 4 already removed
   contracts expiring within min_hours_to_expiry).
2. Reference price must be present, finite and positive.
3. ATM pair: the smallest |strike - reference price|. On an exact tie (reference exactly
   halfway between two strikes) the LOWER strike is chosen, so the result is always the
   same for the same input.
4. Safety check: if even the nearest strike is more than max_distance_pct away from the
   reference price, the chain is too sparse to call anything "at the money": no pair is
   selected.

Who uses this file: strategies/btc_straddle.py (start_atm_selection and
complete_atm_selection). Milestone 6 subscribes to the two legs of the selected pair.
"""

import math
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AtmSelection:
    """
    The result of an ATM selection attempt.

    Fields
    ------
    pair (StraddlePair or None): the nearest pair; None if none could be considered.
    reference_price (Decimal or None): the BTC reference price used.
    distance (Decimal or None): |strike - reference_price| in USDT.
    distance_pct (Decimal or None): distance as a percentage of the reference price.
    problem (str or None): why the selection is NOT usable; None when it is.
    """

    pair: object
    reference_price: Decimal | None
    distance: Decimal | None
    distance_pct: Decimal | None
    problem: str | None

    def is_valid(self):
        """True when a pair was selected and no problem was found."""
        if self.problem is not None:
            return False
        if self.pair is None:
            return False
        return True


def parse_reference_price(raw_value):
    """
    Validate the reference price from an OptionGreeks update.

    Parameters: raw_value - OptionGreeks.underlying_price (float or None).
    Returns: (Decimal, None) when valid, or (None, problem text) when not.

    Why Decimal(str(raw_value)): converting the float's text keeps the digits Bybit sent
    (76046.970079) instead of the float's binary approximation.
    """
    if raw_value is None:
        return None, "the update carried no underlying price"

    try:
        as_float = float(raw_value)
    except (TypeError, ValueError):
        return None, f"underlying price {raw_value!r} is not a number"

    if math.isnan(as_float) or math.isinf(as_float):
        return None, f"underlying price {raw_value!r} is not finite"
    if as_float <= 0:
        return None, f"underlying price {raw_value!r} is not positive"

    return Decimal(str(raw_value)), None


def choose_target_expiry(pairs):
    """
    Pick the expiry to trade: the nearest one among the given pairs.

    Parameters: pairs (list[StraddlePair]).
    Returns: int expiry_ns, or None if there are no pairs.
    """
    if len(pairs) == 0:
        return None

    nearest = pairs[0].expiry_ns
    for pair in pairs:
        if pair.expiry_ns < nearest:
            nearest = pair.expiry_ns
    return nearest


def pairs_for_expiry(pairs, expiry_ns):
    """
    Keep only the pairs of one expiry, sorted by strike.

    Returns: list[StraddlePair].
    """
    selected = []
    for pair in pairs:
        if pair.expiry_ns == expiry_ns:
            selected.append(pair)
    selected.sort(key=strike_of_pair)
    return selected


def strike_of_pair(pair):
    """Sort key: the pair's strike (Decimal)."""
    return pair.strike


def choose_reference_contract(expiry_pairs):
    """
    Choose which contract to read the reference price from.

    Every contract of one expiry carries the same per-expiry underlying price, so any
    would do. We take the call of the middle-strike pair: it is near the money, where
    Bybit's tickers are most reliably updated.

    Parameters: expiry_pairs (list[StraddlePair]) of ONE expiry, sorted by strike.
    Returns: OptionContract, or None if the list is empty.
    """
    if len(expiry_pairs) == 0:
        return None
    middle_pair = expiry_pairs[len(expiry_pairs) // 2]
    return middle_pair.call


def select_atm_pair(expiry_pairs, reference_price, max_distance_pct):
    """
    Choose the pair whose strike is nearest to the reference price.

    Parameters
    ----------
    expiry_pairs : list[StraddlePair]
        Pairs of ONE expiry (mixing expiries would compare different reference prices).
    reference_price : Decimal
        Validated reference price (see parse_reference_price).
    max_distance_pct : Decimal or float
        Largest acceptable distance between the chosen strike and the reference price,
        in percent of the reference price.

    Returns: AtmSelection. Check .is_valid() before using .pair.
    """
    if len(expiry_pairs) == 0:
        return AtmSelection(None, reference_price, None, None, "no call/put pairs to choose from")

    first_expiry = expiry_pairs[0].expiry_ns
    for pair in expiry_pairs:
        if pair.expiry_ns != first_expiry:
            return AtmSelection(None, reference_price, None, None, "pairs from different expiries were given")

    if reference_price is None or reference_price <= 0:
        return AtmSelection(None, reference_price, None, None, "no valid reference price")

    best_pair = None
    best_distance = None
    for pair in expiry_pairs:
        distance = abs(pair.strike - reference_price)

        is_better = False
        if best_pair is None:
            is_better = True
        elif distance < best_distance:
            is_better = True
        elif distance == best_distance and pair.strike < best_pair.strike:
            # Exact tie: prefer the lower strike so the result is deterministic.
            is_better = True

        if is_better:
            best_pair = pair
            best_distance = distance

    distance_pct = best_distance / reference_price * Decimal("100")
    limit = Decimal(str(max_distance_pct))

    problem = None
    if distance_pct > limit:
        problem = (
            f"nearest strike {best_pair.strike} is {distance_pct:.2f}% from the reference "
            f"price {reference_price} (limit {limit}%)"
        )

    return AtmSelection(best_pair, reference_price, best_distance, distance_pct, problem)


def describe_selection(selection):
    """
    Human-readable lines describing an AtmSelection, for the strategy log.
    Returns: list[str].
    """
    if selection.pair is None:
        return [f"ATM selection failed: {selection.problem}"]

    pair = selection.pair
    lines = [
        f"Reference price: {selection.reference_price}",
        f"Nearest strike: {pair.strike} (distance {selection.distance} USDT = {selection.distance_pct:.3f}%)",
        f"  CALL leg: {pair.call.instrument_id}",
        f"  PUT  leg: {pair.put.instrument_id}",
        f"  Expiry:   {pair.call.expiry_utc().strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if selection.is_valid():
        lines.append("ATM selection: OK")
    else:
        lines.append(f"ATM selection REJECTED: {selection.problem}")
    return lines
