"""
End-to-end offline tests of Level 2 exits (Milestone 9) inside NautilusTrader's
BacktestEngine.

EVERYTHING HERE IS SIMULATED: made-up quotes, orders filled by NautilusTrader's simulated
exchange. The entry happens at t+2 s on tight quotes (bids 1200 / 1150 -> entry premium
23.50 USDT; stop-loss level 23.735, take-profit level 23.03); later quotes move the asks.
"""

from decimal import Decimal

from execution.position_manager import FLAT
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.unit.test_strategy_market_data import CALL_LEG
from tests.unit.test_strategy_market_data import PUT_LEG
from tests.unit.test_strategy_market_data import greeks_at
from tests.unit.test_strategy_market_data import quote_at
from tests.unit.test_strategy_market_data import run_with_data


def entry_quotes():
    """t+2 s: tight quotes -> entry at bids 1200 / 1150 (buyback at asks = 23.60, +0.43%)."""
    return [quote_at(2, CALL_LEG, "1200", "1205"), quote_at(2, PUT_LEG, "1150", "1155")]


def repeat_quotes(start, end, call_bid, call_ask, put_bid, put_ask, size="0.50"):
    """The same quotes for both legs every 2 s from `start` to `end` (seconds)."""
    data = []
    for second in range(start, end, 2):
        data.append(quote_at(second, CALL_LEG, call_bid, call_ask, size, size))
        data.append(quote_at(second, PUT_LEG, put_bid, put_ask, size, size))
    return data


def run_scenario(later_quotes, seconds=25):
    config = BTCStraddleConfig(submit_orders=True)
    data = [greeks_at(1, 73748.24)] + entry_quotes() + later_quotes
    return run_with_data(data, seconds=seconds, config=config)


def exit_orders(engine):
    return [order for order in engine.cache.orders() if "STRADDLE_EXIT" in order.tags]


def test_stable_market_holds_the_position():
    engine, strategy = run_scenario(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"))
    try:
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED
        assert strategy.entry_premium == Decimal("23.50")
        assert strategy.last_exit_decision.action == "HOLD"
        assert exit_orders(engine) == []
    finally:
        engine.dispose()


def test_stop_loss_closes_both_legs_and_confirms_flat():
    # t+6 s: the CALL quote arrives first (ask 1220). At that moment the put's latest ask
    # in the Cache is still 1155, so buyback = (1220 + 1155) x 0.01 = 23.75 >= 23.735:
    # the stop-loss fires on the call's tick, before the put's new quote arrives.
    engine, strategy = run_scenario(repeat_quotes(6, 24, "1215", "1220", "1160", "1165"))
    try:
        assert strategy.exit_reason == "STOP_LOSS"
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED
        call_position, put_position = strategy.current_leg_positions()
        assert call_position.side == FLAT
        assert put_position.side == FLAT
        # Simulated fills at the asks seen at that moment: 1220 + 1155 -> 23.75 USDT paid.
        assert strategy.actual_exit_cost() == Decimal("23.75")
        assert strategy.entry_premium - strategy.actual_exit_cost() == Decimal("-0.25")
    finally:
        engine.dispose()


def test_exit_orders_are_reduce_only_ioc_buys_with_price_offset():
    engine, strategy = run_scenario(repeat_quotes(6, 24, "1215", "1220", "1160", "1165"))
    try:
        orders = exit_orders(engine)
        assert len(orders) == 2  # one per leg, exactly once
        for order in orders:
            assert order.side.name == "BUY"
            assert order.is_reduce_only is True
            assert order.time_in_force.name == "IOC"
        prices = sorted(order.price.as_decimal() for order in orders)
        # ask + 2 ticks of 5 USDT, using the asks at trigger time (see the test above):
        # put 1155 + 10 = 1165, call 1220 + 10 = 1230.
        assert prices == [Decimal("1165"), Decimal("1230")]
    finally:
        engine.dispose()


def test_take_profit_closes_both_legs():
    # t+6 s: asks fall to 1150 / 1100 -> buyback 22.50 <= 23.03 (-4.26%).
    engine, strategy = run_scenario(repeat_quotes(6, 24, "1145", "1150", "1095", "1100"))
    try:
        assert strategy.exit_reason == "TAKE_PROFIT"
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED
        assert strategy.entry_premium - strategy.actual_exit_cost() == Decimal("1.00")
    finally:
        engine.dispose()


def test_no_duplicate_exit_while_many_quotes_keep_triggering():
    # Nine more stop-loss-level quote rounds after the trigger must not add exit orders.
    engine, strategy = run_scenario(repeat_quotes(6, 24, "1215", "1220", "1160", "1165"))
    try:
        assert strategy.exit_attempts == 1
        assert len(exit_orders(engine)) == 2
    finally:
        engine.dispose()


def test_partial_exit_fill_is_completed_by_a_second_round():
    # Trade 0.02 per leg; after the trigger the call's ask only offers 0.01 (sizes must
    # use the contract's 2 decimals, so 0.01 is the smallest possible partial).
    config = BTCStraddleConfig(submit_orders=True, order_quantity="0.02")
    data = [greeks_at(1, 73748.24)] + entry_quotes() + repeat_quotes(6, 24, "1215", "1220", "1160", "1165", size="0.01")
    engine, strategy = run_with_data(data, seconds=25, config=config)
    try:
        # Round 1: call filled 0.01 of 0.02 (IOC remainder canceled), put filled 0.02
        # (its latest quote at trigger time still offered 0.50).
        # Round 2: sized from the ACTUAL remaining position: BUY 0.01 call only.
        assert strategy.exit_attempts == 2
        assert len(exit_orders(engine)) == 3
        first_call, first_put, second_call = strategy.all_exit_trackers
        assert first_call.filled_quantity == Decimal("0.01")
        assert first_call.status == "CANCELED"
        assert first_put.filled_quantity == Decimal("0.02")
        assert second_call.target_quantity == Decimal("0.01")
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED
    finally:
        engine.dispose()


def test_missing_ask_does_not_trigger_an_exit():
    # t+6 s: both asks disappear (empty side = 0). The buyback cannot be valued: HOLD.
    engine, strategy = run_scenario(repeat_quotes(6, 24, "1200", "0", "1150", "0"))
    try:
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED
        assert strategy.last_exit_decision.action == "HOLD"
        assert "no ask" in strategy.last_exit_decision.reason
        assert exit_orders(engine) == []
    finally:
        engine.dispose()
