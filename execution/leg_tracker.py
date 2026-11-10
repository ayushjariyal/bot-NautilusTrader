"""
Per-leg order tracking: what actually happened to each leg's order.

Why track it ourselves when NautilusTrader's Order object already has filled_qty and
avg_px? Two reasons:
  1. The straddle needs a combined view of TWO orders: are both complete, did one fail,
     how much of each actually filled? That logic (summarize_entry) is plain Python and
     unit-testable without an exchange.
  2. We record every fill (quantity AND price) ourselves and the strategy cross-checks
     the result against NautilusTrader's Order after each event. A mismatch would mean
     we missed or double-counted an event, and is logged as a warning.

Order lifecycle seen by a leg (a SELL LIMIT IOC on Bybit):
    NEW -> SUBMITTED -> ACCEPTED -> FILLED                       (fully filled)
    NEW -> SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED -> CANCELED  (IOC: rest canceled)
    NEW -> SUBMITTED -> ACCEPTED -> CANCELED                      (IOC: nothing filled)
    NEW -> SUBMITTED -> REJECTED                                  (Bybit refused it)
    NEW -> DENIED                                                 (NautilusTrader's
                                                                   RiskEngine refused it)
A fill can arrive before the "accepted" event on a fast exchange, so fills are accepted
from SUBMITTED too.

Who uses this file: strategies/btc_straddle.py (order event handlers).
"""

from dataclasses import dataclass
from decimal import Decimal


NEW = "NEW"
SUBMITTED = "SUBMITTED"
ACCEPTED = "ACCEPTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
REJECTED = "REJECTED"
DENIED = "DENIED"
CANCELED = "CANCELED"
EXPIRED = "EXPIRED"

TERMINAL_STATUSES = (FILLED, REJECTED, DENIED, CANCELED, EXPIRED)


class LegOrderTracker:
    """
    Tracks one leg's order.

    Attributes
    ----------
    leg_name (str): "CALL" or "PUT".
    instrument_id (str): the option traded.
    target_quantity (Decimal): quantity we asked for.
    client_order_id (str or None): NautilusTrader's ID for our order, set on submission.
    status (str): one of the constants above.
    filled_quantity (Decimal): total quantity filled so far.
    filled_notional (Decimal): sum of (fill quantity x fill price), for the average.
    fill_count (int): number of fill events.
    reason (str or None): rejection/denial reason from Bybit or the RiskEngine.
    overfill_detected (bool): True if fills ever exceeded the target (should never happen).
    """

    def __init__(self, leg_name, instrument_id, target_quantity):
        self.leg_name = leg_name
        self.instrument_id = str(instrument_id)
        self.target_quantity = Decimal(target_quantity)
        self.client_order_id = None
        self.status = NEW
        self.filled_quantity = Decimal("0")
        self.filled_notional = Decimal("0")
        self.fill_count = 0
        self.reason = None
        self.overfill_detected = False

    # --- events ------------------------------------------------------------------------
    def mark_submitted(self, client_order_id):
        """The order was handed to NautilusTrader (gateway returned SUBMITTED)."""
        self.client_order_id = str(client_order_id)
        if self.status == NEW:
            self.status = SUBMITTED

    def on_accepted(self):
        """Bybit acknowledged the order. Ignored if a fill already moved us further."""
        if self.status in (NEW, SUBMITTED):
            self.status = ACCEPTED

    def on_filled(self, quantity, price):
        """
        Record one fill (an order can be filled in several pieces).

        Parameters: quantity, price (Decimal) from OrderFilled.last_qty / last_px.
        """
        quantity = Decimal(quantity)
        price = Decimal(price)

        self.fill_count += 1
        self.filled_quantity += quantity
        self.filled_notional += quantity * price

        if self.filled_quantity > self.target_quantity:
            self.overfill_detected = True

        if self.filled_quantity >= self.target_quantity:
            self.status = FILLED
        else:
            self.status = PARTIALLY_FILLED

    def on_rejected(self, reason):
        """Bybit refused the order (e.g. insufficient margin)."""
        self.status = REJECTED
        self.reason = str(reason)

    def on_denied(self, reason):
        """NautilusTrader's RiskEngine refused the order before it left."""
        self.status = DENIED
        self.reason = str(reason)

    def on_canceled(self):
        """
        The order was canceled. For an IOC order this is normal: the unfilled remainder
        is canceled immediately. Any quantity filled before stays filled.
        """
        if self.status != FILLED:
            self.status = CANCELED

    def on_expired(self):
        if self.status != FILLED:
            self.status = EXPIRED

    # --- questions ---------------------------------------------------------------------
    def is_terminal(self):
        """True when no further fills can happen for this order."""
        return self.status in TERMINAL_STATUSES

    def is_fully_filled(self):
        return self.filled_quantity >= self.target_quantity

    def remaining_quantity(self):
        """Target minus filled, never below zero."""
        remaining = self.target_quantity - self.filled_quantity
        if remaining < 0:
            return Decimal("0")
        return remaining

    def average_price(self):
        """Volume-weighted average fill price, or None if nothing filled."""
        if self.filled_quantity == 0:
            return None
        return self.filled_notional / self.filled_quantity

    def describe(self):
        """One line for the log."""
        average = self.average_price()
        average_text = "-" if average is None else f"{average}"
        text = (
            f"{self.leg_name} {self.status}: filled {self.filled_quantity}/{self.target_quantity} "
            f"avg {average_text} ({self.fill_count} fills)"
        )
        if self.reason:
            text += f" reason: {self.reason}"
        return text


# Outcomes of the two-leg entry.
ENTRY_PENDING = "PENDING"            # at least one leg can still fill
ENTRY_COMPLETE = "COMPLETE"          # both legs fully filled
ENTRY_INCOMPLETE = "INCOMPLETE"      # both legs finished, at least one not fully filled


@dataclass(frozen=True)
class EntryOutcome:
    """
    Combined result of both legs' entry orders.

    status: ENTRY_PENDING, ENTRY_COMPLETE or ENTRY_INCOMPLETE.
    call_filled, put_filled (Decimal): executed quantities.
    matched_quantity (Decimal): min of the two = the quantity that forms a real straddle.
    """

    status: str
    call_filled: Decimal
    put_filled: Decimal
    matched_quantity: Decimal


def summarize_entry(call_tracker, put_tracker):
    """
    Decide where the two-leg entry stands.

    Returns: EntryOutcome. INCOMPLETE is handed to Milestone 10's recovery, which closes
    any unmatched quantity (e.g. call filled 0.01, put filled 0 -> close the call).
    """
    matched = min(call_tracker.filled_quantity, put_tracker.filled_quantity)

    if call_tracker.is_fully_filled() and put_tracker.is_fully_filled():
        status = ENTRY_COMPLETE
    elif call_tracker.is_terminal() and put_tracker.is_terminal():
        status = ENTRY_INCOMPLETE
    else:
        status = ENTRY_PENDING

    return EntryOutcome(
        status=status,
        call_filled=call_tracker.filled_quantity,
        put_filled=put_tracker.filled_quantity,
        matched_quantity=matched,
    )
