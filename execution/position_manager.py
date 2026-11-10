"""
Position tracking: a plain-Python view of each leg's position and the checks that the
positions are what the strategy expects.

Where positions come from
-------------------------
NautilusTrader's ExecutionEngine builds positions from fills: every OrderFilled updates
the Position of that instrument in the Cache, and emits PositionOpened / PositionChanged
/ PositionClosed events. With Bybit options the venue uses NETTING (one position per
contract), so a SELL of 0.01 opens a SHORT position of 0.01.

How we know the Cache matches Bybit
-----------------------------------
  * At start-up, reconciliation loads Bybit's open orders and positions into the Cache
    BEFORE the strategy starts (guide 03B).
  * While running, the execution engine's periodic position check
    (LiveExecEngineConfig.position_check_interval_secs, enabled in main.py when an
    execution client exists) compares the Cache with Bybit and fetches missing fills
    when they differ.
So the Cache is the best available picture of the exchange, and this module compares it
with what the strategy's own order trackers expect.

Who uses this module: strategies/btc_straddle.py (position events, status line, the
existing-position safety check at start-up). Milestone 9 values these positions.
"""

from dataclasses import dataclass
from decimal import Decimal


SHORT = "SHORT"
LONG = "LONG"
FLAT = "FLAT"


@dataclass(frozen=True)
class LegPosition:
    """
    The net position in one option.

    Fields
    ------
    instrument_id (str): the option.
    side (str): "SHORT", "LONG" or "FLAT".
    quantity (Decimal): absolute size, e.g. 0.01 (always >= 0).
    signed_quantity (Decimal): negative for short, positive for long, 0 when flat.
    average_open_price (Decimal or None): average entry price, None when flat.
    position_count (int): how many open NautilusTrader positions were combined (normally
        0 or 1 with NETTING; more than 1 is reported as a problem).
    """

    instrument_id: str
    side: str
    quantity: Decimal
    signed_quantity: Decimal
    average_open_price: Decimal | None
    position_count: int


def net_leg_position(instrument_id, positions):
    """
    Combine the open NautilusTrader positions of one instrument into one LegPosition.

    Parameters: instrument_id (str or InstrumentId); positions (list of NautilusTrader
    Position objects, e.g. cache.positions_open(instrument_id=...)).
    Returns: LegPosition (FLAT when the list is empty).

    How: each position's signed quantity is built from position.quantity (an exact
    Quantity) and position.side, NOT from position.signed_qty, which NautilusTrader stores
    as a float (tiny binary errors). Signed quantities are added (short = negative). The
    average open price (also a float in NautilusTrader) is the quantity-weighted average
    of the positions on the same side as the net result.
    """
    instrument_text = str(instrument_id)
    if len(positions) == 0:
        return LegPosition(instrument_text, FLAT, Decimal("0"), Decimal("0"), None, 0)

    signed_quantities = []
    net_signed = Decimal("0")
    for position in positions:
        signed = signed_quantity_of(position)
        signed_quantities.append(signed)
        net_signed += signed

    if net_signed < 0:
        side = SHORT
    elif net_signed > 0:
        side = LONG
    else:
        side = FLAT

    weighted_sum = Decimal("0")
    weight = Decimal("0")
    for position, signed in zip(positions, signed_quantities):
        same_side = False
        if side == SHORT and signed < 0:
            same_side = True
        if side == LONG and signed > 0:
            same_side = True
        if same_side:
            size = abs(signed)
            weighted_sum += Decimal(str(position.avg_px_open)) * size
            weight += size

    average = None
    if weight > 0:
        average = weighted_sum / weight

    return LegPosition(
        instrument_id=instrument_text,
        side=side,
        quantity=abs(net_signed),
        signed_quantity=net_signed,
        average_open_price=average,
        position_count=len(positions),
    )


def signed_quantity_of(position):
    """
    Exact signed quantity of one NautilusTrader Position: -quantity when SHORT,
    +quantity when LONG, 0 when FLAT. Returns: Decimal.
    """
    size = position.quantity.as_decimal()
    side_name = position.side.name
    if side_name == SHORT:
        return -size
    if side_name == LONG:
        return size
    return Decimal("0")


def check_leg_position(leg_name, position, expected_quantity):
    """
    Compare one leg's actual position with the quantity our order tracker says was sold.

    Parameters: leg_name ("CALL"/"PUT"); position (LegPosition); expected_quantity
    (Decimal, the leg's filled entry quantity; 0 means we expect no position).
    Returns: list of problem texts (empty = as expected).
    """
    problems = []
    if position.position_count > 1:
        problems.append(f"{leg_name}: {position.position_count} open positions on one contract (expected one, NETTING)")

    if expected_quantity == 0:
        if position.side != FLAT:
            problems.append(f"{leg_name}: unexpected {position.side} position of {position.quantity}")
        return problems

    if position.side != SHORT:
        problems.append(f"{leg_name}: expected SHORT {expected_quantity}, found {position.side} {position.quantity}")
    elif position.quantity != expected_quantity:
        problems.append(f"{leg_name}: expected SHORT {expected_quantity}, found SHORT {position.quantity}")
    return problems


def check_straddle_positions(call_position, put_position, expected_call, expected_put, require_balanced):
    """
    Check both legs, and (for a complete straddle) that the two legs are the same size.

    Parameters: call_position, put_position (LegPosition); expected_call, expected_put
    (Decimal); require_balanced (bool): True once the straddle is ENTERED.
    Returns: list of problem texts (empty = positions confirmed).
    """
    problems = []
    problems.extend(check_leg_position("CALL", call_position, expected_call))
    problems.extend(check_leg_position("PUT", put_position, expected_put))
    if require_balanced and call_position.quantity != put_position.quantity:
        problems.append(
            f"STRADDLE: legs are unbalanced (call {call_position.quantity}, put {put_position.quantity})"
        )
    return problems


def describe_leg_position(leg_name, position):
    """One line for the log, e.g. "CALL BTC-...: SHORT 0.01 @ avg 1200"."""
    if position.side == FLAT:
        return f"{leg_name} {position.instrument_id}: FLAT"
    return (
        f"{leg_name} {position.instrument_id}: {position.side} {position.quantity} "
        f"@ avg {position.average_open_price}"
    )
