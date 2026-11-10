"""
Unit tests for strategies/instrument_selector.py (Milestone 5: ATM selection).

Contracts are built from SIMULATED CryptoOption objects (tests/factories.py) with Bybit
Testnet's specs. No network is used.
"""

from decimal import Decimal

import pytest

from strategies.instrument_selector import choose_reference_contract
from strategies.instrument_selector import choose_target_expiry
from strategies.instrument_selector import describe_selection
from strategies.instrument_selector import pairs_for_expiry
from strategies.instrument_selector import parse_reference_price
from strategies.instrument_selector import select_atm_pair
from strategies.option_chain import contracts_from_instruments
from strategies.option_chain import find_call_put_pairs
from tests.factories import expiry_at
from tests.factories import make_chain


def pairs_from_strikes(strikes, days=4):
    """Call/put pairs for the given strikes, all with one expiry."""
    return find_call_put_pairs(contracts_from_instruments(make_chain(strikes, expiry_at(days))))


# Strikes around 73,750 with Bybit's 250-USDT spacing near the money.
STRIKES = [73000, 73250, 73500, 73750, 74000, 74250, 74500]


# --------------------------------------------------------------------------------------
# Reference price validation
# --------------------------------------------------------------------------------------
def test_valid_reference_price_keeps_its_digits():
    price, problem = parse_reference_price(73722.59709671)
    assert problem is None
    assert price == Decimal("73722.59709671")


@pytest.mark.parametrize(
    "raw, expected_words",
    [
        (None, "no underlying price"),
        (float("nan"), "not finite"),
        (float("inf"), "not finite"),
        (0.0, "not positive"),
        (-5.0, "not positive"),
        ("abc", "not a number"),
    ],
)
def test_invalid_reference_prices_are_rejected(raw, expected_words):
    price, problem = parse_reference_price(raw)
    assert price is None
    assert expected_words in problem


# --------------------------------------------------------------------------------------
# Expiry and reference contract
# --------------------------------------------------------------------------------------
def test_target_expiry_is_the_nearest():
    pairs = pairs_from_strikes([73750], days=12) + pairs_from_strikes([73750], days=4) + pairs_from_strikes([73750], days=30)
    assert choose_target_expiry(pairs) == pairs_from_strikes([73750], days=4)[0].expiry_ns


def test_target_expiry_of_empty_list_is_none():
    assert choose_target_expiry([]) is None


def test_pairs_for_expiry_filters_and_sorts_by_strike():
    near = pairs_from_strikes([74000, 73500], days=4)
    far = pairs_from_strikes([73750], days=12)
    selected = pairs_for_expiry(far + near, near[0].expiry_ns)
    assert [pair.strike for pair in selected] == [Decimal("73500"), Decimal("74000")]


def test_reference_contract_is_middle_call():
    contract = choose_reference_contract(pairs_from_strikes(STRIKES))
    assert contract.option_kind == "CALL"
    assert contract.strike == Decimal("73750")


def test_reference_contract_of_empty_list_is_none():
    assert choose_reference_contract([]) is None


# --------------------------------------------------------------------------------------
# ATM selection
# --------------------------------------------------------------------------------------
def test_real_testnet_example_selects_73750():
    # Values from the real Testnet run on 2026-11-10: reference 73722.59709671.
    selection = select_atm_pair(pairs_from_strikes(STRIKES), Decimal("73722.59709671"), 2.0)
    assert selection.is_valid()
    assert selection.pair.strike == Decimal("73750")
    assert selection.distance == Decimal("27.40290329")
    assert selection.pair.call.option_kind == "CALL"
    assert selection.pair.put.option_kind == "PUT"
    assert selection.pair.call.strike == selection.pair.put.strike
    assert selection.pair.call.expiry_ns == selection.pair.put.expiry_ns


def test_reference_below_and_above_pick_nearest():
    pairs = pairs_from_strikes(STRIKES)
    assert select_atm_pair(pairs, Decimal("73600"), 2.0).pair.strike == Decimal("73500")
    assert select_atm_pair(pairs, Decimal("73640"), 2.0).pair.strike == Decimal("73750")


def test_reference_exactly_on_a_strike():
    selection = select_atm_pair(pairs_from_strikes(STRIKES), Decimal("74000"), 2.0)
    assert selection.pair.strike == Decimal("74000")
    assert selection.distance == Decimal("0")


def test_exact_tie_picks_lower_strike():
    # 73625 is exactly halfway between 73500 and 73750.
    selection = select_atm_pair(pairs_from_strikes(STRIKES), Decimal("73625"), 2.0)
    assert selection.pair.strike == Decimal("73500")


def test_reference_far_outside_the_chain_is_rejected():
    # Nearest strike 74500 is about 7.6% below 80,000: too far to be "at the money".
    selection = select_atm_pair(pairs_from_strikes(STRIKES), Decimal("80000"), 2.0)
    assert selection.is_valid() is False
    assert selection.pair.strike == Decimal("74500")
    assert "limit 2.0%" in selection.problem


def test_distance_limit_is_inclusive():
    # 73750 vs 75225: distance 1475 / 75225 = 1.961%, below the 1.97% limit, so it passes.
    selection = select_atm_pair(pairs_from_strikes([73750]), Decimal("75225"), 1.97)
    assert selection.is_valid()


def test_no_pairs_gives_invalid_selection():
    selection = select_atm_pair([], Decimal("73722"), 2.0)
    assert selection.is_valid() is False
    assert selection.pair is None
    assert "no call/put pairs" in selection.problem


def test_mixed_expiries_are_refused():
    pairs = pairs_from_strikes([73750], days=4) + pairs_from_strikes([73750], days=12)
    selection = select_atm_pair(pairs, Decimal("73722"), 2.0)
    assert selection.is_valid() is False
    assert "different expiries" in selection.problem


def test_missing_reference_price_gives_invalid_selection():
    selection = select_atm_pair(pairs_from_strikes(STRIKES), None, 2.0)
    assert selection.is_valid() is False


# --------------------------------------------------------------------------------------
# Description for the log
# --------------------------------------------------------------------------------------
def test_describe_valid_selection():
    lines = describe_selection(select_atm_pair(pairs_from_strikes(STRIKES), Decimal("73722.59709671"), 2.0))
    text = "\n".join(lines)
    assert "Nearest strike: 73750" in text
    # pairs_from_strikes uses expiry_at(4): TEST_NOW (8 Nov) + 4 days = 12 Nov.
    assert "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT" in text
    assert lines[-1] == "ATM selection: OK"


def test_describe_rejected_and_failed_selections():
    rejected = describe_selection(select_atm_pair(pairs_from_strikes(STRIKES), Decimal("80000"), 2.0))
    assert rejected[-1].startswith("ATM selection REJECTED")
    failed = describe_selection(select_atm_pair([], Decimal("73722"), 2.0))
    assert failed == ["ATM selection failed: no call/put pairs to choose from"]
