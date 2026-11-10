"""
Unit tests for strategies/option_chain.py (Milestone 4: option discovery).

The instruments are real NautilusTrader CryptoOption objects built by tests/factories.py
with Bybit Testnet's contract specs. They are simulated test data; no network is used.
"""

import dataclasses
from decimal import Decimal

import pytest
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from strategies.option_chain import CALL
from strategies.option_chain import PUT
from strategies.option_chain import OptionContract
from strategies.option_chain import contract_from_instrument
from strategies.option_chain import contracts_from_instruments
from strategies.option_chain import filter_contracts
from strategies.option_chain import find_call_put_pairs
from strategies.option_chain import group_by_expiry
from strategies.option_chain import pair_problem
from strategies.option_chain import summarize_chain
from tests.factories import TEST_NOW_NS
from tests.factories import expiry_at
from tests.factories import make_chain
from tests.factories import make_option


# --------------------------------------------------------------------------------------
# Converting instruments
# --------------------------------------------------------------------------------------
def test_contract_from_instrument_extracts_all_fields():
    instrument = make_option(76000, "CALL", expiry_at(19))
    contract = contract_from_instrument(instrument)

    assert contract.instrument_id == "BTC-27NOV26-76000-C-USDT-OPTION.BYBIT"
    assert contract.underlying == "BTC"
    assert contract.option_kind == CALL
    assert contract.strike == Decimal("76000")
    assert contract.expiry_ns == instrument.expiration_ns
    assert contract.settlement_currency == "USDT"
    assert contract.quote_currency == "USDT"
    assert contract.multiplier == Decimal("1")
    assert contract.tick_size == Decimal("5")
    assert contract.lot_step == Decimal("0.01")
    assert contract.min_quantity == Decimal("0.01")
    assert contract.max_quantity == Decimal("10000")
    assert contract.price_precision == 0
    assert contract.size_precision == 2
    assert contract.is_inverse is False


def test_contract_values_are_plain_python_types():
    # The whole point: no NautilusTrader Price/Currency/enum objects leak through.
    contract = contract_from_instrument(make_option(76000, "PUT"))
    assert isinstance(contract.underlying, str)
    assert isinstance(contract.option_kind, str)
    assert isinstance(contract.strike, Decimal)
    assert isinstance(contract.multiplier, Decimal)
    assert contract.option_kind == PUT


def test_non_option_instrument_is_skipped():
    spot = TestInstrumentProvider.btcusdt_binance()
    assert contract_from_instrument(spot) is None
    contracts = contracts_from_instruments([spot, make_option(76000)])
    assert len(contracts) == 1
    assert contracts[0].option_kind == CALL


def test_expiry_utc_and_hours_to_expiry():
    contract = contract_from_instrument(make_option(76000, expiry=expiry_at(1)))
    # TEST_NOW is 12:00 UTC; expiry is next day 08:00 UTC -> 20 hours.
    assert contract.expiry_utc().hour == 8
    assert contract.hours_to_expiry(TEST_NOW_NS) == 20


# --------------------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------------------
def test_filter_keeps_only_requested_underlying():
    contracts = contracts_from_instruments([make_option(76000, underlying="BTC"), make_option(2800, underlying="ETH")])
    kept = filter_contracts(contracts, "BTC", TEST_NOW_NS, min_hours_to_expiry=24)
    assert [contract.underlying for contract in kept] == ["BTC"]


def test_filter_drops_expired_and_soon_expiring_contracts():
    contracts = contracts_from_instruments(
        [
            make_option(76000, expiry=expiry_at(-1)),  # already expired
            make_option(76000, expiry=expiry_at(1)),   # 20 h left: too soon
            make_option(76000, expiry=expiry_at(2)),   # 44 h left: kept
        ]
    )
    kept = filter_contracts(contracts, "BTC", TEST_NOW_NS, min_hours_to_expiry=24)
    assert len(kept) == 1
    assert kept[0].hours_to_expiry(TEST_NOW_NS) == 44


def test_filter_keeps_contract_exactly_at_the_limit():
    contracts = contracts_from_instruments([make_option(76000, expiry=expiry_at(1))])  # 20 h
    assert len(filter_contracts(contracts, "BTC", TEST_NOW_NS, min_hours_to_expiry=20)) == 1


def test_group_by_expiry_orders_nearest_first():
    contracts = contracts_from_instruments(
        [make_option(76000, expiry=expiry_at(30)), make_option(76000, expiry=expiry_at(5)), make_option(77000, expiry=expiry_at(5))]
    )
    groups = group_by_expiry(contracts)
    expiries = list(groups.keys())
    assert expiries == sorted(expiries)
    assert len(groups[expiries[0]]) == 2


# --------------------------------------------------------------------------------------
# Call/put pairs
# --------------------------------------------------------------------------------------
def test_pairs_found_for_every_strike_with_call_and_put():
    contracts = contracts_from_instruments(make_chain([75000, 76000, 77000]))
    pairs = find_call_put_pairs(contracts)
    assert [pair.strike for pair in pairs] == [Decimal("75000"), Decimal("76000"), Decimal("77000")]
    for pair in pairs:
        assert pair.call.option_kind == CALL
        assert pair.put.option_kind == PUT
        assert pair.call.strike == pair.put.strike
        assert pair.call.expiry_ns == pair.put.expiry_ns


def test_call_without_put_is_not_paired():
    contracts = contracts_from_instruments([make_option(76000, "CALL"), make_option(77000, "CALL"), make_option(77000, "PUT")])
    pairs = find_call_put_pairs(contracts)
    assert [pair.strike for pair in pairs] == [Decimal("77000")]


def test_same_strike_different_expiry_is_not_paired():
    contracts = contracts_from_instruments(
        [make_option(76000, "CALL", expiry_at(5)), make_option(76000, "PUT", expiry_at(12))]
    )
    assert find_call_put_pairs(contracts) == []


def test_pairs_sorted_by_expiry_then_strike():
    contracts = contracts_from_instruments(make_chain([77000, 76000], expiry_at(12)) + make_chain([76000], expiry_at(5)))
    pairs = find_call_put_pairs(contracts)
    order = [(pair.expiry_ns, pair.strike) for pair in pairs]
    assert order == sorted(order)
    assert len(pairs) == 3


def test_incompatible_multiplier_is_not_paired():
    contracts = contracts_from_instruments([make_option(76000, "CALL", multiplier="1"), make_option(76000, "PUT", multiplier="10")])
    assert find_call_put_pairs(contracts) == []
    call, put = contracts
    assert pair_problem(call, put) == "different multiplier"


def test_incompatible_settlement_is_not_paired():
    contracts = contracts_from_instruments(
        [make_option(76000, "CALL", settlement="USDT"), make_option(76000, "PUT", settlement="USDC", quote="USDC")]
    )
    assert find_call_put_pairs(contracts) == []
    assert pair_problem(contracts[0], contracts[1]) == "different settlement currency"


def test_pair_problem_reports_none_for_compatible_legs():
    call, put = contracts_from_instruments(make_chain([76000]))
    assert pair_problem(call, put) is None


def test_pair_problem_detects_swapped_kinds():
    call, put = contracts_from_instruments(make_chain([76000]))
    assert "is not a call" in pair_problem(put, call)


def test_empty_input_gives_no_pairs():
    assert find_call_put_pairs([]) == []


# --------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------
def test_summary_counts_contracts_and_pairs():
    all_contracts = contracts_from_instruments(make_chain([75000, 76000], expiry_at(5)) + [make_option(76000, expiry=expiry_at(1))])
    usable = filter_contracts(all_contracts, "BTC", TEST_NOW_NS, 24)
    pairs = find_call_put_pairs(usable)
    lines = summarize_chain(all_contracts, usable, pairs, "BTC", 24)
    assert lines[0] == "Option contracts loaded: 5"
    assert lines[1] == "BTC contracts expiring in >= 24 h: 4"
    assert lines[2] == "Call/put pairs usable for a straddle: 2"
    assert "4 contracts, 2 pairs" in lines[3]


def test_option_contract_is_immutable():
    contract = contract_from_instrument(make_option(76000))
    assert isinstance(contract, OptionContract)
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.strike = Decimal("1")
