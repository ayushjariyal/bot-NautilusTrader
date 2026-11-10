"""
Offline tests of Milestone 10 (recovery, ERROR, watchdog, shutdown) inside NautilusTrader's
BacktestEngine.

EVERYTHING HERE IS SIMULATED: made-up quotes and NautilusTrader's simulated exchange.

How "one leg refused" is produced: a test-only subclass (PutDeniedStrategy) gives the PUT
entry order a quantity with 3 decimals (0.010) while the contract allows 2. NautilusTrader's
RiskEngine then really DENIES the put ("quantity ... invalid (precision 3 > 2)") while the
call goes through and fills. (A first attempt used RiskEngineConfig.max_notional_per_order,
but in 1.231.0 the RiskEngine skips notional and balance checks for MARGIN accounts, which
is what Bybit options use: risk/engine.pyx, "if account.is_margin_account: return True".)
"""

import datetime
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Quantity

from execution.leg_tracker import LegOrderTracker
from execution.position_manager import FLAT
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.factories import TEST_NOW
from tests.factories import expiry_at
from tests.factories import make_chain
from tests.unit.test_strategy_exits import entry_quotes
from tests.unit.test_strategy_exits import repeat_quotes
from tests.unit.test_strategy_market_data import CALL_LEG
from tests.unit.test_strategy_market_data import PUT_LEG
from tests.unit.test_strategy_market_data import STRIKES
from tests.unit.test_strategy_market_data import greeks_at
from tests.unit.test_strategy_market_data import run_with_data


class PutDeniedStrategy(BTCStraddleStrategy):
    """TEST-ONLY fault injection: the put entry order gets an invalid quantity precision."""

    def build_entry_order(self, leg_name, instrument_id, bid_price):
        order, description = super().build_entry_order(leg_name, instrument_id, bid_price)
        if leg_name != "PUT":
            return order, description
        bad_order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=Quantity.from_str("0.010"),  # 3 decimals: the RiskEngine denies it
            price=order.price,
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            tags=["STRADDLE_ENTRY", leg_name],
        )
        return bad_order, description + " [TEST: invalid precision]"


def run_with_put_denied(later_quotes, seconds=25):
    """
    Like run_with_data, but with PutDeniedStrategy: the entry ends with the call filled and
    the put DENIED by NautilusTrader's RiskEngine.
    """
    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=TraderId("TEST-001"), logging=LoggingConfig(bypass_logging=True))
    )
    engine.add_venue(
        venue=Venue("BYBIT"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
    )
    for instrument in make_chain(STRIKES, expiry_at(4)):
        engine.add_instrument(instrument)
    engine.add_data([greeks_at(1, 73748.24)] + entry_quotes() + later_quotes)
    strategy = PutDeniedStrategy(BTCStraddleConfig(submit_orders=True))
    engine.add_strategy(strategy)
    engine.run(start=TEST_NOW, end=TEST_NOW + datetime.timedelta(seconds=seconds))
    return engine, strategy


def state_moves(strategy):
    return [(old, new) for old, new, _ in strategy.state_machine.history]


def test_call_filled_put_denied_recovery_closes_the_naked_call():
    engine, strategy = run_with_put_denied(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"))
    try:
        assert strategy.call_entry.filled_quantity == Decimal("0.01")
        assert strategy.put_entry.status == "DENIED"
        assert "precision 3 > 2" in strategy.put_entry.reason

        moves = state_moves(strategy)
        assert (BTCStraddleStrategy.ENTRY_SUBMITTED, BTCStraddleStrategy.RECOVERING) in moves
        assert (BTCStraddleStrategy.RECOVERING, BTCStraddleStrategy.CLOSED) in moves
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED

        # Exactly one recovery order: BUY 0.01 call, reduce-only.
        recovery_orders = [order for order in engine.cache.orders() if "STRADDLE_RECOVERY" in order.tags]
        assert len(recovery_orders) == 1
        assert str(recovery_orders[0].instrument_id) == CALL_LEG
        assert recovery_orders[0].side.name == "BUY"
        assert recovery_orders[0].is_reduce_only is True
        assert strategy.recovery_attempts == 1

        call_position, put_position = strategy.current_leg_positions()
        assert call_position.side == FLAT
        assert put_position.side == FLAT
    finally:
        engine.dispose()


def test_recovery_waits_for_an_ask_before_buying_back():
    # After the entry, the call's ask disappears for a while (t+4..t+10), then returns.
    later = repeat_quotes(4, 12, "1200", "0", "1150", "1155") + repeat_quotes(12, 24, "1200", "1205", "1150", "1155")
    engine, strategy = run_with_put_denied(later)
    try:
        assert strategy.entry_state == BTCStraddleStrategy.CLOSED
        assert strategy.recovery_attempts == 1
    finally:
        engine.dispose()


def run_entered(later_quotes, seconds=25, config=None):
    config = config or BTCStraddleConfig(submit_orders=True)
    data = [greeks_at(1, 73748.24)] + entry_quotes() + later_quotes
    return run_with_data(data, seconds=seconds, config=config)


def test_unexpected_positions_on_two_checks_enter_error():
    engine, strategy = run_entered(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"))
    try:
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED
        strategy.expected_leg_quantity = Decimal("0.02")  # the Cache shows 0.01 per leg

        strategy.check_position_health()
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED  # one check may be a race
        strategy.check_position_health()
        assert strategy.entry_state == BTCStraddleStrategy.ERROR

        # ERROR is final: the state machine refuses any further automatic step.
        assert strategy.set_state(BTCStraddleStrategy.EXITING, "test") is False
    finally:
        engine.dispose()


def test_error_state_sends_no_orders_on_new_quotes():
    engine, strategy = run_entered(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"))
    try:
        orders_before = len(engine.cache.orders())
        strategy.enter_error("test")
        # A stop-loss-level quote would trigger an exit while ENTERED; in ERROR it must not.
        strategy.on_quote_tick(engine.cache.quote_tick(strategy.leg_instrument_ids()[0]))
        assert len(engine.cache.orders()) == orders_before
    finally:
        engine.dispose()


def test_stale_data_watchdog_raises_an_alert():
    # After the entry, both asks vanish for the rest of the 80 s run (> 60 s limit).
    later = repeat_quotes(4, 80, "1200", "0", "1150", "0")
    engine, strategy = run_entered(later, seconds=80)
    try:
        assert strategy.entry_state == BTCStraddleStrategy.ENTERED  # no blind orders
        assert strategy.unvalued_since_ns is not None
        assert strategy.monitoring_alert_active is True
    finally:
        engine.dispose()


def test_shutdown_with_open_positions_warns_and_leaves_them_open():
    engine, strategy = run_entered(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"))
    try:
        # The run ended while ENTERED, so on_stop() saw two open short legs.
        assert strategy.stopped_with_open_positions is True
        call_position, put_position = strategy.current_leg_positions()
        assert call_position.side == "SHORT"
        assert put_position.side == "SHORT"
    finally:
        engine.dispose()


def test_order_timeout_escalates_to_error_when_orders_never_finish():
    # Simulates a lost connection: two entry orders that never receive any event.
    engine, strategy = run_entered(repeat_quotes(4, 24, "1200", "1205", "1150", "1155"),
                                   config=BTCStraddleConfig(submit_orders=False))
    try:
        strategy.call_entry = LegOrderTracker("CALL", CALL_LEG, Decimal("0.01"))
        strategy.put_entry = LegOrderTracker("PUT", PUT_LEG, Decimal("0.01"))
        strategy.call_entry.mark_submitted("O-LOST-1")
        strategy.put_entry.mark_submitted("O-LOST-2")
        strategy.state_machine.state = BTCStraddleStrategy.ENTRY_SUBMITTED  # test-only

        strategy.on_order_timeout(None)  # first timeout: recovery starts, cancels requested
        assert strategy.entry_state == BTCStraddleStrategy.RECOVERING
        strategy.on_order_timeout(None)  # still no final status: give up safely
        assert strategy.entry_state == BTCStraddleStrategy.ERROR
    finally:
        engine.dispose()


def test_every_state_change_is_recorded():
    engine, strategy = run_entered(repeat_quotes(6, 24, "1215", "1220", "1160", "1165"))
    try:
        assert state_moves(strategy) == [
            (BTCStraddleStrategy.SELECTING, BTCStraddleStrategy.WAITING_FOR_ENTRY),
            (BTCStraddleStrategy.WAITING_FOR_ENTRY, BTCStraddleStrategy.ENTRY_SUBMITTED),
            (BTCStraddleStrategy.ENTRY_SUBMITTED, BTCStraddleStrategy.ENTERED),
            (BTCStraddleStrategy.ENTERED, BTCStraddleStrategy.EXITING),
            (BTCStraddleStrategy.EXITING, BTCStraddleStrategy.CLOSED),
        ]
        assert strategy.state_machine.refused == []
    finally:
        engine.dispose()
