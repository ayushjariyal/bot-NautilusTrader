"""
Recovery planning: given the ACTUAL short quantities of both legs, what must be closed so
that what remains is either a balanced straddle or nothing at all?

Assignment walkthrough question 10: "What happens when one leg gets executed but the
other leg does not?" A lone short call (or put) is a naked option position with
unlimited (call) or large (put) risk and none of the straddle's balance. The rule:

    matched quantity = min(call short quantity, put short quantity)
    close on the call = call quantity - matched
    close on the put  = put quantity  - matched

Examples:
    call 0.01, put 0     -> close 0.01 call; nothing remains (GO_FLAT)
    call 0.02, put 0.01  -> close 0.01 call; a 0.01 straddle remains (KEEP_STRADDLE)
    call 0.01, put 0.01  -> nothing to close (KEEP_STRADDLE)
    call 0,    put 0     -> nothing to close, nothing remains (GO_FLAT)

The quantities to close must be valid order quantities (>= the contract minimum and a
multiple of the step). Fills on Bybit are always in whole steps, so this holds in
practice; if it ever does not, the plan reports a problem and the strategy goes to ERROR
instead of guessing.

Plain Python; used by strategies/btc_straddle.py in the RECOVERING state.
"""

from dataclasses import dataclass
from decimal import Decimal


KEEP_STRADDLE = "KEEP_STRADDLE"
GO_FLAT = "GO_FLAT"


@dataclass(frozen=True)
class RecoveryPlan:
    """
    matched_quantity (Decimal): the balanced straddle that remains after recovery.
    close_call (Decimal): call quantity to buy back (0 = nothing to do on the call).
    close_put (Decimal): put quantity to buy back.
    outcome (str): KEEP_STRADDLE if matched > 0, otherwise GO_FLAT.
    problem (str or None): why the plan cannot be executed safely.
    """

    matched_quantity: Decimal
    close_call: Decimal
    close_put: Decimal
    outcome: str
    problem: str | None

    def needs_orders(self):
        """True if anything has to be bought back."""
        return self.close_call > 0 or self.close_put > 0


def is_valid_close_quantity(quantity, min_quantity, lot_step):
    """True if `quantity` can be sent as an order (0 counts as 'nothing to send')."""
    if quantity == 0:
        return True
    if quantity < min_quantity:
        return False
    if quantity % lot_step != 0:
        return False
    return True


def plan_recovery(call_short_quantity, put_short_quantity, min_quantity, lot_step):
    """
    Build the recovery plan from the actual short quantities.

    Parameters: call_short_quantity, put_short_quantity (Decimal, >= 0, from the Cache);
    min_quantity, lot_step (Decimal, contract specs; identical for both legs).
    Returns: RecoveryPlan.
    """
    matched = min(call_short_quantity, put_short_quantity)
    close_call = call_short_quantity - matched
    close_put = put_short_quantity - matched

    outcome = GO_FLAT
    if matched > 0:
        outcome = KEEP_STRADDLE

    problem = None
    for leg_name, quantity in (("CALL", close_call), ("PUT", close_put)):
        if not is_valid_close_quantity(quantity, min_quantity, lot_step):
            problem = (
                f"{leg_name}: unmatched quantity {quantity} cannot be traded "
                f"(minimum {min_quantity}, step {lot_step})"
            )
            break

    if matched > 0 and not is_valid_close_quantity(matched, min_quantity, lot_step):
        problem = f"matched quantity {matched} is not a valid straddle size"

    return RecoveryPlan(
        matched_quantity=matched,
        close_call=close_call,
        close_put=close_put,
        outcome=outcome,
        problem=problem,
    )


def describe_plan(plan):
    """One line for the log."""
    text = (
        f"Recovery plan: matched straddle {plan.matched_quantity}, close call {plan.close_call}, "
        f"close put {plan.close_put} -> {plan.outcome}"
    )
    if plan.problem is not None:
        text += f" (PROBLEM: {plan.problem})"
    return text
