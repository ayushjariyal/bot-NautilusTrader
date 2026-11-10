"""
Unit tests for execution/recovery_planner.py (assignment question 10).
"""

from decimal import Decimal

import pytest

from execution.recovery_planner import GO_FLAT
from execution.recovery_planner import KEEP_STRADDLE
from execution.recovery_planner import describe_plan
from execution.recovery_planner import plan_recovery


MIN = Decimal("0.01")
STEP = Decimal("0.01")


def plan(call, put):
    return plan_recovery(Decimal(call), Decimal(put), MIN, STEP)


@pytest.mark.parametrize(
    "call, put, matched, close_call, close_put, outcome",
    [
        ("0.01", "0", "0", "0.01", "0", GO_FLAT),            # call filled, put not
        ("0", "0.01", "0", "0", "0.01", GO_FLAT),            # put filled, call not
        ("0.02", "0.01", "0.01", "0.01", "0", KEEP_STRADDLE),  # partial: keep 0.01 straddle
        ("0.01", "0.03", "0.01", "0", "0.02", KEEP_STRADDLE),
        ("0.01", "0.01", "0.01", "0", "0", KEEP_STRADDLE),   # already balanced
        ("0", "0", "0", "0", "0", GO_FLAT),                  # nothing filled at all
    ],
)
def test_plans(call, put, matched, close_call, close_put, outcome):
    result = plan(call, put)
    assert result.matched_quantity == Decimal(matched)
    assert result.close_call == Decimal(close_call)
    assert result.close_put == Decimal(close_put)
    assert result.outcome == outcome
    assert result.problem is None


def test_needs_orders():
    assert plan("0.01", "0").needs_orders() is True
    assert plan("0.01", "0.01").needs_orders() is False
    assert plan("0", "0").needs_orders() is False


def test_untradeable_remainder_is_a_problem():
    # Cannot happen with Bybit's 0.01 fills, but must never be guessed at.
    result = plan_recovery(Decimal("0.015"), Decimal("0.01"), MIN, STEP)
    assert result.problem is not None
    assert "cannot be traded" in result.problem


def test_describe_plan():
    # Decimal keeps the precision of the subtraction: 0.01 - 0.01 is written "0.00".
    assert describe_plan(plan("0.02", "0.01")) == (
        "Recovery plan: matched straddle 0.01, close call 0.01, close put 0.00 -> KEEP_STRADDLE"
    )
