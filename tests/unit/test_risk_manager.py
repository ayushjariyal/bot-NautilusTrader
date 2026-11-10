"""
Unit tests for strategies/premium_calculator.py and strategies/risk_manager.py
(Level 2: combined premium, ask-side buyback valuation, 1% stop-loss, 2% take-profit).
Quotes are SIMULATED. No network.
"""

from decimal import Decimal

import pytest

from data.market_data import leg_quote_from_values
from strategies.premium_calculator import PremiumValue
from strategies.premium_calculator import combined_buyback_cost
from strategies.premium_calculator import combined_entry_premium
from strategies.premium_calculator import percent_change
from strategies.premium_calculator import straddle_quantity
from strategies.risk_manager import HOLD
from strategies.risk_manager import STOP_LOSS
from strategies.risk_manager import TAKE_PROFIT
from strategies.risk_manager import evaluate_exit
from strategies.risk_manager import risk_levels


SECOND = 1_000_000_000
NOW = 100 * SECOND
Q = Decimal("0.01")
ONE = Decimal("1")


def quote(bid, ask, age_seconds=0):
    return leg_quote_from_values("LEG", bid, ask, "1", "1", NOW - age_seconds * SECOND)


# --------------------------------------------------------------------------------------
# Premium calculator
# --------------------------------------------------------------------------------------
def test_entry_premium_uses_both_legs_and_the_same_quantity():
    # (1200 x 0.01 + 1150 x 0.01) x 1 = 23.50 USDT
    assert combined_entry_premium(Decimal("1200"), Decimal("1150"), Q, ONE) == Decimal("23.50")


def test_multiplier_scales_the_premium():
    assert combined_entry_premium(Decimal("1200"), Decimal("1150"), Q, Decimal("10")) == Decimal("235.0")


def test_buyback_cost_uses_the_ask_side():
    value = combined_buyback_cost(quote("1210", "1215"), quote("1155", "1160"), Q, ONE, NOW, 10.0)
    # asks: (1215 + 1160) x 0.01 = 23.75 (the bids would give 23.65)
    assert value.value == Decimal("23.75")
    assert value.problem is None


@pytest.mark.parametrize(
    "call_quote, put_quote, words",
    [
        (None, quote("1150", "1155"), "CALL: no quote"),
        (quote("1200", "0"), quote("1150", "1155"), "CALL: no ask"),
        (quote("1200", "1205"), quote("1150", "1155", age_seconds=30), "PUT: ask is stale"),
    ],
)
def test_buyback_cannot_be_valued_on_bad_data(call_quote, put_quote, words):
    value = combined_buyback_cost(call_quote, put_quote, Q, ONE, NOW, 10.0)
    assert value.value is None
    assert words in value.problem


def test_straddle_quantity_requires_equal_legs():
    assert straddle_quantity(Q, Q).value == Q
    assert "different quantities" in straddle_quantity(Decimal("0.02"), Q).problem
    assert "no straddle position" in straddle_quantity(Q, Decimal("0")).problem


def test_percent_change():
    assert percent_change(Decimal("23.50"), Decimal("23.735")) == Decimal("1")
    assert percent_change(Decimal("23.50"), Decimal("23.03")) == Decimal("-2")


# --------------------------------------------------------------------------------------
# Risk levels and decisions
# --------------------------------------------------------------------------------------
def test_levels_for_the_example_entry():
    levels = risk_levels(Decimal("23.50"), 1.0, 2.0)
    assert levels.stop_loss_level == Decimal("23.735")
    assert levels.take_profit_level == Decimal("23.03")


LEVELS = risk_levels(Decimal("23.50"), 1.0, 2.0)


def test_hold_between_the_levels():
    decision = evaluate_exit(LEVELS, PremiumValue(Decimal("23.60"), None))
    assert decision.action == HOLD
    assert round(decision.change_pct, 2) == Decimal("0.43")


def test_stop_loss_exactly_at_plus_one_percent():
    decision = evaluate_exit(LEVELS, PremiumValue(Decimal("23.735"), None))
    assert decision.action == STOP_LOSS


def test_just_below_stop_loss_holds():
    assert evaluate_exit(LEVELS, PremiumValue(Decimal("23.734"), None)).action == HOLD


def test_take_profit_exactly_at_minus_two_percent():
    assert evaluate_exit(LEVELS, PremiumValue(Decimal("23.03"), None)).action == TAKE_PROFIT


def test_just_above_take_profit_holds():
    assert evaluate_exit(LEVELS, PremiumValue(Decimal("23.031"), None)).action == HOLD


def test_large_moves_trigger_the_right_side():
    assert evaluate_exit(LEVELS, PremiumValue(Decimal("30"), None)).action == STOP_LOSS
    assert evaluate_exit(LEVELS, PremiumValue(Decimal("10"), None)).action == TAKE_PROFIT


def test_unusable_valuation_holds_with_the_reason():
    decision = evaluate_exit(LEVELS, PremiumValue(None, "CALL: no ask to value the buyback"))
    assert decision.action == HOLD
    assert decision.buyback_cost is None
    assert "CALL: no ask" in decision.reason
