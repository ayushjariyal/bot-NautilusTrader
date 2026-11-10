"""
Unit tests for strategies/liquidity_check.py: quantity validation and the entry checks,
including the "immediate stop-loss" gap. Quotes are SIMULATED. No network is used.
"""

from decimal import Decimal

import pytest

from data.market_data import leg_quote_from_values
from strategies.liquidity_check import check_entry_liquidity
from strategies.liquidity_check import check_leg
from strategies.liquidity_check import describe_report
from strategies.liquidity_check import validate_order_quantity
from strategies.option_chain import contract_from_instrument
from tests.factories import make_option


SECOND = 1_000_000_000
NOW = 100 * SECOND
QTY = Decimal("0.01")
CONTRACT = contract_from_instrument(make_option(73750))  # Testnet specs: min 0.01, step 0.01


def quote(bid, ask, bid_size="1", ask_size="1", age_seconds=0):
    return leg_quote_from_values("LEG", bid, ask, bid_size, ask_size, NOW - age_seconds * SECOND)


def run_check(call_quote, put_quote, quantity=QTY, max_spread=0.5, max_age=10.0, stop_loss=1.0):
    return check_entry_liquidity(call_quote, put_quote, quantity, Decimal("1"), max_spread, max_age, stop_loss, NOW)


# --------------------------------------------------------------------------------------
# Order quantity
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["0.01", "0.05", "1", "1.00", "10000"])
def test_valid_quantities(value):
    assert validate_order_quantity(Decimal(value), CONTRACT) is None


@pytest.mark.parametrize(
    "value, words",
    [
        ("0", "must be positive"),
        ("-0.01", "must be positive"),
        ("0.005", "below the minimum"),
        ("10000.01", "above the maximum"),
        ("0.015", "not a multiple of the step"),
    ],
)
def test_invalid_quantities(value, words):
    assert words in validate_order_quantity(Decimal(value), CONTRACT)


# --------------------------------------------------------------------------------------
# One leg
# --------------------------------------------------------------------------------------
def test_good_leg_has_no_problems():
    assert check_leg("CALL", quote("3000", "3010"), QTY, Decimal("0.5"), 10.0, NOW) == []


def test_missing_quote():
    assert check_leg("CALL", None, QTY, Decimal("0.5"), 10.0, NOW) == ["CALL: no quote received yet"]


def test_empty_book_reports_both_sides():
    problems = check_leg("PUT", quote("0", "0", "0", "0"), QTY, Decimal("0.5"), 10.0, NOW)
    assert problems == ["PUT: no bid (nobody to sell to)", "PUT: no ask (buyback cannot be valued)"]


def test_insufficient_size_on_both_sides():
    problems = check_leg("CALL", quote("3000", "3010", bid_size="0.005", ask_size="0.005"), QTY, Decimal("0.5"), 10.0, NOW)
    assert any("bid size 0.005 is smaller" in problem for problem in problems)
    assert any("ask size 0.005 is smaller" in problem for problem in problems)


def test_wide_spread_is_rejected():
    # The real Testnet book seen in Milestone 3A: bid 1000 / ask 2500 -> 85.7% spread.
    problems = check_leg("PUT", quote("1000", "2500"), QTY, Decimal("0.5"), 10.0, NOW)
    assert len(problems) == 1
    assert "spread 85.71% is above the limit 0.5%" in problems[0]


def test_crossed_quote_is_rejected():
    problems = check_leg("CALL", quote("3010", "3000"), QTY, Decimal("0.5"), 10.0, NOW)
    assert "crossed or locked" in problems[0]


def test_stale_quote_is_rejected():
    problems = check_leg("CALL", quote("3000", "3010", age_seconds=11), QTY, Decimal("0.5"), 10.0, NOW)
    assert "stale (11.0 s old" in problems[0]


# --------------------------------------------------------------------------------------
# Whole straddle
# --------------------------------------------------------------------------------------
def test_tight_liquid_market_passes():
    report = run_check(quote("3000", "3010"), quote("2800", "2808"))
    assert report.ok
    assert report.problems == ()
    # Premiums use quantity x multiplier: (3000 + 2800) x 0.01 x 1 = 58 USDT.
    assert report.entry_premium == Decimal("58.00")
    assert report.buyback_cost == Decimal("58.18")
    assert round(report.initial_gap_pct, 3) == Decimal("0.310")


def test_gap_equal_to_stop_loss_blocks_entry():
    # The example from the module docstring: entry 5800 x qty, buyback 5858 x qty = +1.0%.
    # Spreads of about 1% are allowed here (max_spread=2) to isolate the straddle-level check.
    report = run_check(quote("3000", "3030"), quote("2800", "2828"), max_spread=2.0)
    assert report.ok is False
    assert report.initial_gap_pct == Decimal("1")
    assert any("stop-loss would trigger immediately" in problem for problem in report.problems)


def test_gap_just_below_stop_loss_is_allowed():
    report = run_check(quote("3000", "3025"), quote("2800", "2825"), max_spread=2.0)
    assert report.initial_gap_pct < Decimal("1")
    assert report.ok


def test_one_bad_leg_fails_the_straddle():
    report = run_check(quote("3000", "3010"), quote("0", "0", "0", "0"))
    assert report.ok is False
    assert report.entry_premium is None
    assert all(problem.startswith("PUT:") for problem in report.problems)


def test_real_testnet_empty_books_fail_with_four_problems():
    # What the live run on 2026-11-10 showed for both ATM legs.
    empty = quote("0", "0", "0", "0")
    report = run_check(empty, empty)
    assert len(report.problems) == 4


def test_describe_report_lines():
    passed = describe_report(run_check(quote("3000", "3010"), quote("2800", "2808")))
    assert passed[0] == "Entry check: PASS"
    assert "initial gap 0.31%" in passed[1]
    failed = describe_report(run_check(None, None))
    assert failed[0] == "Entry check: FAIL (2 problems)"
    assert failed[1] == "  - CALL: no quote received yet"
