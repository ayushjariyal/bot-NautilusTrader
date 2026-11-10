"""
Unit tests for execution/leg_tracker.py: one leg's order lifecycle and the combined
two-leg entry outcome (including the "one leg filled, the other not" cases).
Events are simulated by calling the tracker methods directly.
"""

from decimal import Decimal

from execution.leg_tracker import ACCEPTED
from execution.leg_tracker import CANCELED
from execution.leg_tracker import DENIED
from execution.leg_tracker import ENTRY_COMPLETE
from execution.leg_tracker import ENTRY_INCOMPLETE
from execution.leg_tracker import ENTRY_PENDING
from execution.leg_tracker import FILLED
from execution.leg_tracker import NEW
from execution.leg_tracker import PARTIALLY_FILLED
from execution.leg_tracker import REJECTED
from execution.leg_tracker import SUBMITTED
from execution.leg_tracker import LegOrderTracker
from execution.leg_tracker import summarize_entry


QTY = Decimal("0.03")


def submitted_leg(name="CALL"):
    tracker = LegOrderTracker(name, f"BTC-12NOV26-73750-{name[0]}-USDT-OPTION.BYBIT", QTY)
    tracker.mark_submitted(f"O-{name}")
    return tracker


def test_new_tracker_state():
    tracker = LegOrderTracker("CALL", "X", "0.01")
    assert tracker.status == NEW
    assert tracker.filled_quantity == 0
    assert tracker.average_price() is None
    assert tracker.is_terminal() is False


def test_full_fill_lifecycle():
    tracker = submitted_leg()
    assert tracker.status == SUBMITTED
    tracker.on_accepted()
    assert tracker.status == ACCEPTED
    tracker.on_filled(Decimal("0.03"), Decimal("1200"))
    assert tracker.status == FILLED
    assert tracker.is_terminal()
    assert tracker.average_price() == Decimal("1200")
    assert tracker.remaining_quantity() == 0


def test_partial_fills_weighted_average_price():
    tracker = submitted_leg()
    tracker.on_filled(Decimal("0.01"), Decimal("1200"))
    assert tracker.status == PARTIALLY_FILLED
    assert tracker.is_terminal() is False
    tracker.on_filled(Decimal("0.02"), Decimal("1185"))
    # (0.01 x 1200 + 0.02 x 1185) / 0.03 = 1190
    assert tracker.average_price() == Decimal("1190")
    assert tracker.fill_count == 2
    assert tracker.status == FILLED


def test_ioc_partial_fill_then_cancel_keeps_the_filled_part():
    tracker = submitted_leg()
    tracker.on_accepted()
    tracker.on_filled(Decimal("0.01"), Decimal("1200"))
    tracker.on_canceled()
    assert tracker.status == CANCELED
    assert tracker.is_terminal()
    assert tracker.filled_quantity == Decimal("0.01")
    assert tracker.remaining_quantity() == Decimal("0.02")


def test_fill_before_accepted_event_is_handled():
    tracker = submitted_leg()
    tracker.on_filled(Decimal("0.03"), Decimal("1200"))
    tracker.on_accepted()  # late acknowledgement must not move the status back
    assert tracker.status == FILLED


def test_cancel_after_full_fill_does_not_change_status():
    tracker = submitted_leg()
    tracker.on_filled(QTY, Decimal("1200"))
    tracker.on_canceled()
    assert tracker.status == FILLED


def test_rejection_and_denial_record_the_reason():
    rejected = submitted_leg()
    rejected.on_rejected("Insufficient margin")
    assert rejected.status == REJECTED
    assert rejected.is_terminal()
    assert "Insufficient margin" in rejected.describe()

    denied = LegOrderTracker("PUT", "X", QTY)
    denied.on_denied("QUANTITY_BELOW_MINIMUM")
    assert denied.status == DENIED
    assert denied.reason == "QUANTITY_BELOW_MINIMUM"


def test_overfill_is_flagged():
    tracker = submitted_leg()
    tracker.on_filled(Decimal("0.04"), Decimal("1200"))
    assert tracker.overfill_detected is True


# --------------------------------------------------------------------------------------
# Combined entry outcome (assignment question 10: one leg executed, the other not)
# --------------------------------------------------------------------------------------
def test_both_filled_is_complete():
    call, put = submitted_leg("CALL"), submitted_leg("PUT")
    call.on_filled(QTY, Decimal("1200"))
    put.on_filled(QTY, Decimal("1150"))
    outcome = summarize_entry(call, put)
    assert outcome.status == ENTRY_COMPLETE
    assert outcome.matched_quantity == QTY


def test_one_leg_still_working_is_pending():
    call, put = submitted_leg("CALL"), submitted_leg("PUT")
    call.on_filled(QTY, Decimal("1200"))
    put.on_accepted()
    assert summarize_entry(call, put).status == ENTRY_PENDING


def test_call_filled_put_rejected_is_incomplete_with_zero_matched():
    call, put = submitted_leg("CALL"), submitted_leg("PUT")
    call.on_filled(QTY, Decimal("1200"))
    put.on_rejected("Insufficient margin")
    outcome = summarize_entry(call, put)
    assert outcome.status == ENTRY_INCOMPLETE
    assert outcome.call_filled == QTY
    assert outcome.put_filled == 0
    assert outcome.matched_quantity == 0


def test_partial_fills_on_both_legs_give_the_smaller_matched_quantity():
    call, put = submitted_leg("CALL"), submitted_leg("PUT")
    call.on_filled(Decimal("0.02"), Decimal("1200"))
    call.on_canceled()
    put.on_filled(Decimal("0.01"), Decimal("1150"))
    put.on_canceled()
    outcome = summarize_entry(call, put)
    assert outcome.status == ENTRY_INCOMPLETE
    assert outcome.matched_quantity == Decimal("0.01")


def test_nothing_filled_on_either_leg_is_incomplete():
    call, put = submitted_leg("CALL"), submitted_leg("PUT")
    call.on_canceled()
    put.on_canceled()
    outcome = summarize_entry(call, put)
    assert outcome.status == ENTRY_INCOMPLETE
    assert outcome.matched_quantity == 0
