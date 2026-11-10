"""
End-to-end offline tests of the Level 1 entry (Milestone 7) inside NautilusTrader's
BacktestEngine.

IMPORTANT: everything here is SIMULATED. The quotes are made up, and when orders are
allowed they are filled by NautilusTrader's simulated exchange, not by Bybit. These
tests prove the strategy's order flow and event handling; they are not exchange trades.
"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from execution.leg_tracker import LegOrderTracker
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.unit.test_strategy_market_data import CALL_LEG
from tests.unit.test_strategy_market_data import PUT_LEG
from tests.unit.test_strategy_market_data import greeks_at
from tests.unit.test_strategy_market_data import liquid_quotes
from tests.unit.test_strategy_market_data import run_with_data


def run_entry(submit_orders):
    config = BTCStraddleConfig(submit_orders=submit_orders)
    return run_with_data([greeks_at(1, 73748.24)] + liquid_quotes(), config=config)


def test_dry_run_logs_orders_and_sends_nothing():
    engine, strategy = run_entry(submit_orders=False)
    try:
        assert strategy.entry_state == BTCStraddleStrategy.DRY_RUN_COMPLETE
        assert strategy.order_gateway.dry_run_count == 2
        assert strategy.order_gateway.submitted_count == 0
        # The simulated exchange never saw an order.
        assert engine.cache.orders() == []
        assert strategy.actual_entry_premium() is None  # nothing filled, not "0 USDT"
        descriptions = strategy.order_gateway.dry_run_descriptions
        assert descriptions[0] == f"SELL 0.01 {CALL_LEG} @ 1200 IOC (entry CALL)"
        assert descriptions[1] == f"SELL 0.01 {PUT_LEG} @ 1150 IOC (entry PUT)"
    finally:
        engine.dispose()


def test_simulated_execution_fills_both_legs_and_opens_two_short_positions():
    engine, strategy = run_entry(submit_orders=True)
    try:
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED
        assert strategy.call_entry.is_fully_filled()
        assert strategy.put_entry.is_fully_filled()
        assert strategy.call_entry.average_price() == Decimal("1200")
        assert strategy.put_entry.average_price() == Decimal("1150")
        # (1200 + 1150) x 0.01 x multiplier 1 = 23.50 USDT received.
        assert strategy.actual_entry_premium() == Decimal("23.50")

        orders = engine.cache.orders()
        assert len(orders) == 2
        for order in orders:
            assert order.side.name == "SELL"
            assert order.time_in_force.name == "IOC"
            assert order.is_reduce_only is False
            assert "STRADDLE_ENTRY" in order.tags

        positions = engine.cache.positions()
        assert len(positions) == 2
        for position in positions:
            assert position.side == PositionSide.SHORT
            assert position.quantity.as_decimal() == Decimal("0.01")
    finally:
        engine.dispose()


def test_entry_happens_only_once():
    engine, strategy = run_entry(submit_orders=True)
    try:
        # 11 quote updates per leg arrived, but only one entry (2 orders) was made.
        assert strategy.order_gateway.submitted_count == 2
        assert len(engine.cache.orders()) == 2
    finally:
        engine.dispose()


def fill_event(client_order_id, quantity, price):
    """A minimal stand-in for OrderFilled with real NautilusTrader value types."""
    return SimpleNamespace(
        client_order_id=ClientOrderId(client_order_id),
        last_qty=Quantity.from_str(quantity),
        last_px=Price.from_str(price),
    )


def test_one_leg_filled_other_rejected_starts_recovery():
    # The order EVENTS are delivered by hand (state forced to ENTRY_SUBMITTED; test-only).
    # No real fill exists in the Cache, so after the rejection the recovery finds both
    # legs flat and ends in CLOSED. The full recovery with a REAL filled leg is tested in
    # test_strategy_recovery.py.
    engine, strategy = run_entry(submit_orders=False)
    try:
        strategy.call_entry = LegOrderTracker("CALL", CALL_LEG, Decimal("0.01"))
        strategy.put_entry = LegOrderTracker("PUT", PUT_LEG, Decimal("0.01"))
        strategy.call_entry.mark_submitted("O-CALL-1")
        strategy.put_entry.mark_submitted("O-PUT-1")
        strategy.trackers_by_client_order_id = {"O-CALL-1": strategy.call_entry, "O-PUT-1": strategy.put_entry}
        strategy.state_machine.state = BTCStraddleStrategy.ENTRY_SUBMITTED

        strategy.on_order_filled(fill_event("O-CALL-1", "0.01", "1200"))
        assert strategy.entry_state == BTCStraddleStrategy.ENTRY_SUBMITTED  # put still pending

        strategy.on_order_rejected(SimpleNamespace(client_order_id=ClientOrderId("O-PUT-1"), reason="Insufficient margin"))
        moves = [(old, new) for old, new, _ in strategy.state_machine.history]
        assert (BTCStraddleStrategy.ENTRY_SUBMITTED, BTCStraddleStrategy.RECOVERING) in moves
        assert strategy.put_entry.reason == "Insufficient margin"
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED
    finally:
        engine.dispose()


def test_events_for_unknown_orders_are_ignored():
    engine, strategy = run_entry(submit_orders=False)
    try:
        strategy.on_order_filled(fill_event("O-SOMEONE-ELSE", "0.01", "1200"))
        assert strategy.entry_state == BTCStraddleStrategy.DRY_RUN_COMPLETE
    finally:
        engine.dispose()
