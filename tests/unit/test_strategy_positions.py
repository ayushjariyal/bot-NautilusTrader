"""
Offline tests of position tracking (Milestone 8) with REAL NautilusTrader positions
created by the backtest engine's SIMULATED exchange (made-up quotes, simulated fills).
"""

from decimal import Decimal

from execution.position_manager import SHORT
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.unit.test_strategy_market_data import CALL_LEG
from tests.unit.test_strategy_market_data import PUT_LEG
from tests.unit.test_strategy_market_data import greeks_at
from tests.unit.test_strategy_market_data import liquid_quotes
from tests.unit.test_strategy_market_data import run_with_data


def run_filled_entry():
    config = BTCStraddleConfig(submit_orders=True)
    return run_with_data([greeks_at(1, 73748.24)] + liquid_quotes(), config=config)


def test_positions_after_entry_are_two_shorts_with_entry_prices():
    engine, strategy = run_filled_entry()
    try:
        call_position, put_position = strategy.current_leg_positions()
        assert call_position.instrument_id == CALL_LEG
        assert call_position.side == SHORT
        assert call_position.quantity == Decimal("0.01")
        assert call_position.average_open_price == Decimal("1200.0")
        assert put_position.instrument_id == PUT_LEG
        assert put_position.side == SHORT
        assert put_position.average_open_price == Decimal("1150.0")
    finally:
        engine.dispose()


def test_position_opened_events_were_received_for_both_legs():
    engine, strategy = run_filled_entry()
    try:
        assert strategy.position_events == 2
    finally:
        engine.dispose()


def test_status_timer_verified_positions_during_the_run():
    engine, strategy = run_filled_entry()
    try:
        # The status timer (t+10 s, t+20 s) ran verify_positions() while ENTERED.
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED
        assert strategy.position_problems == []
        assert strategy.verify_positions() == []
    finally:
        engine.dispose()


def test_mismatch_between_tracker_and_cache_is_detected():
    engine, strategy = run_filled_entry()
    try:
        # Pretend the strategy believes the straddle is 0.02 per leg (since Milestone 10
        # positions are verified against expected_leg_quantity, the straddle's size).
        strategy.expected_leg_quantity = Decimal("0.02")
        problems = strategy.verify_positions()
        assert "CALL: expected SHORT 0.02, found SHORT 0.01" in problems
        assert "PUT: expected SHORT 0.02, found SHORT 0.01" in problems
        assert "STRADDLE: legs are unbalanced" not in " ".join(problems)  # cache legs are equal
    finally:
        engine.dispose()


def test_existing_positions_block_a_new_straddle():
    # Simulates a restart: the Cache already holds the two short legs (as reconciliation
    # would load them from Bybit). check_existing_positions() must find them.
    engine, strategy = run_filled_entry()
    try:
        assert strategy.check_existing_positions() is True
        assert len(strategy.existing_positions) == 2
        for leg in strategy.existing_positions:
            assert leg.side == SHORT
            assert leg.quantity == Decimal("0.01")
    finally:
        engine.dispose()


def test_clean_start_has_no_existing_positions():
    config = BTCStraddleConfig(submit_orders=False)
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + liquid_quotes(), config=config)
    try:
        assert strategy.existing_positions == []
        assert strategy.entry_state == BTCStraddleStrategy.DRY_RUN_COMPLETE
    finally:
        engine.dispose()
