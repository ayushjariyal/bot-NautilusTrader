"""
Premium maths for the straddle, in USDT, with consistent quantities.

The assignment's definitions (Level 2):
  * "calculate the combined premium using both option legs, with consistent contract
    quantities"
  * "Use ask-side quotes for the buyback valuation"

So:
    combined entry premium = (call entry price x Q + put entry price x Q) x multiplier
    combined buyback cost  = (call ASK x Q + put ASK x Q) x multiplier

where Q is the SAME quantity for both legs (the straddle's size) and the multiplier is
the same for both legs (checked in Milestone 4; Bybit: 1). Entry prices are the ACTUAL
average fill prices, not the quotes seen before the order.

Units: Bybit quotes option prices in USDT per 1 BTC of underlying, quantities are in BTC
(0.01 = an option on 0.01 BTC), the multiplier is 1, so price x quantity x multiplier is
USDT. Both values above are therefore USDT and can be compared directly.

Fees are not included: the assignment defines the thresholds on premiums, and Bybit
Testnet instruments report fee rates of 0 (guide 04).

Who uses this module: strategies/risk_manager.py and strategies/btc_straddle.py.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PremiumValue:
    """
    A combined straddle value, or the reason it cannot be computed.

    value (Decimal or None): USDT amount; None if it cannot be computed.
    problem (str or None): why value is None.
    """

    value: Decimal | None
    problem: str | None


def straddle_quantity(call_quantity, put_quantity):
    """
    The straddle's size: both legs must hold the same quantity.

    Parameters: call_quantity, put_quantity (Decimal).
    Returns: PremiumValue whose value is the common quantity, or a problem if the legs
    differ (an unbalanced position is not a straddle; Milestone 10 repairs it first).
    """
    if call_quantity <= 0 or put_quantity <= 0:
        return PremiumValue(None, f"no straddle position (call {call_quantity}, put {put_quantity})")
    if call_quantity != put_quantity:
        return PremiumValue(None, f"legs have different quantities (call {call_quantity}, put {put_quantity})")
    return PremiumValue(call_quantity, None)


def combined_entry_premium(call_price, put_price, quantity, multiplier):
    """
    USDT received when the straddle was sold, from the actual average fill prices.

    Example: call 1200, put 1150, quantity 0.01, multiplier 1 -> 23.50 USDT.
    """
    return (call_price * quantity + put_price * quantity) * multiplier


def combined_buyback_cost(call_quote, put_quote, quantity, multiplier, now_ns, max_quote_age_seconds):
    """
    USDT needed to buy both legs back NOW, valued at the ASK of each leg.

    Parameters: call_quote, put_quote (LegQuote or None); quantity, multiplier (Decimal);
    now_ns (int); max_quote_age_seconds (float).
    Returns: PremiumValue. No value (and a problem text) when a quote is missing, has no
    ask, or is stale: the risk manager must not act on a value it cannot trust.
    """
    for leg_name, quote in (("CALL", call_quote), ("PUT", put_quote)):
        if quote is None:
            return PremiumValue(None, f"{leg_name}: no quote")
        if not quote.has_ask():
            return PremiumValue(None, f"{leg_name}: no ask to value the buyback")
        age = quote.age_seconds(now_ns)
        if age > max_quote_age_seconds:
            return PremiumValue(None, f"{leg_name}: ask is stale ({age:.1f} s old)")

    cost = (call_quote.ask * quantity + put_quote.ask * quantity) * multiplier
    return PremiumValue(cost, None)


def percent_change(entry_value, current_value):
    """(current - entry) / entry x 100. Positive = the buyback got more expensive."""
    return (current_value - entry_value) / entry_value * Decimal("100")
