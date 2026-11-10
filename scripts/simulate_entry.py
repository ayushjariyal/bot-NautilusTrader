"""
Demonstrate the strategy's entry (Level 1) and exits (Level 2) OFFLINE, in
NautilusTrader's backtest engine.

EVERYTHING HERE IS SIMULATED: the option contracts, the greeks and the quotes are made
up (tests/factories.py style), and with --mode fills the orders are filled by
NautilusTrader's simulated exchange, NOT by Bybit. Use it to see the strategy's full
order flow and logs while real Testnet trading is not available (no API keys / test
coins, empty Testnet order books).

Modes:
  --mode dry-run   submit_orders=False: the gateway logs "DRY RUN: would submit ..."
  --mode fills     submit_orders=True: orders go to the SIMULATED exchange and fill

Scenarios (what the made-up quotes do after the entry at t+2 s; entry premium 23.50 USDT,
stop-loss at a buyback cost of 23.735, take-profit at 23.03):
  --scenario hold          quotes stay where they were: the position is held
  --scenario stop-loss     asks rise (call 1220): buyback 23.75 -> STOP_LOSS, close both
  --scenario take-profit   asks fall (1150 / 1100): buyback 22.50 -> TAKE_PROFIT
  --scenario partial-exit  quantity 0.02, the call's ask only offers 0.01 after the
                           trigger: the exit needs a second round
  --scenario one-leg-denied
                           FAULT INJECTION: the put entry order is given an invalid
                           quantity precision, so NautilusTrader's RiskEngine DENIES it
                           while the call fills -> RECOVERING buys the naked call back

How to run:
    .venv\\Scripts\\python.exe scripts\\simulate_entry.py --mode dry-run
    .venv\\Scripts\\python.exe scripts\\simulate_entry.py --mode fills --scenario stop-loss
"""

import argparse
import datetime
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.backtest.engine import BacktestEngine  # noqa: E402
from nautilus_trader.config import BacktestEngineConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USDT  # noqa: E402
from nautilus_trader.model.data import OptionGreeks  # noqa: E402
from nautilus_trader.model.data import QuoteTick  # noqa: E402
from nautilus_trader.model.enums import AccountType  # noqa: E402
from nautilus_trader.model.enums import OmsType  # noqa: E402
from nautilus_trader.model.enums import OrderSide  # noqa: E402
from nautilus_trader.model.enums import TimeInForce  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.model.identifiers import TraderId  # noqa: E402
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.model.objects import Price  # noqa: E402
from nautilus_trader.model.objects import Quantity  # noqa: E402

from strategies.btc_straddle import BTCStraddleConfig  # noqa: E402
from strategies.btc_straddle import BTCStraddleStrategy  # noqa: E402
from tests.factories import TEST_NOW  # noqa: E402
from tests.factories import TEST_NOW_NS  # noqa: E402
from tests.factories import expiry_at  # noqa: E402
from tests.factories import make_chain  # noqa: E402


SECOND = 1_000_000_000
STRIKES = [73000, 73250, 73500, 73750, 74000, 74250, 74500]
REFERENCE = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
CALL_LEG = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
PUT_LEG = "BTC-12NOV26-73750-P-USDT-OPTION.BYBIT"


def simulated_greeks(seconds, underlying_price):
    ts = TEST_NOW_NS + seconds * SECOND
    return OptionGreeks(
        instrument_id=InstrumentId.from_str(REFERENCE),
        delta=0.5, gamma=0.0002, vega=40.0, theta=-90.0, rho=0.0,
        mark_iv=0.4, bid_iv=None, ask_iv=None,
        underlying_price=underlying_price, open_interest=None,
        ts_event=ts, ts_init=ts,
    )


def simulated_quote(seconds, leg, bid, ask, size="0.50"):
    ts = TEST_NOW_NS + seconds * SECOND
    return QuoteTick(
        instrument_id=InstrumentId.from_str(leg),
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str(size),
        ask_size=Quantity.from_str(size),
        ts_event=ts,
        ts_init=ts,
    )


# Quotes after the entry, per scenario: (call bid, call ask, put bid, put ask, size).
SCENARIO_QUOTES = {
    "hold": ("1200", "1205", "1150", "1155", "0.50"),
    "stop-loss": ("1215", "1220", "1160", "1165", "0.50"),
    "take-profit": ("1145", "1150", "1095", "1100", "0.50"),
    "partial-exit": ("1215", "1220", "1160", "1165", "0.01"),
    "one-leg-denied": ("1200", "1205", "1150", "1155", "0.50"),
}


class PutDeniedStrategy(BTCStraddleStrategy):
    """
    DEMO FAULT INJECTION (scenario one-leg-denied only): the put entry order gets a
    quantity with 3 decimals, which the contract does not allow, so NautilusTrader's
    RiskEngine denies it. Everything else is the normal strategy.
    """

    def build_entry_order(self, leg_name, instrument_id, bid_price):
        order, description = super().build_entry_order(leg_name, instrument_id, bid_price)
        if leg_name != "PUT":
            return order, description
        bad_order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=Quantity.from_str("0.010"),
            price=order.price,
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            tags=["STRADDLE_ENTRY", leg_name],
        )
        return bad_order, description + " [DEMO FAULT: invalid quantity precision]"


def build_data(scenario):
    """
    Reference price at t+1 s, tight quotes at t+2 s (entry), then the scenario's quotes
    for both legs every 2 s from t+6 s.
    """
    data = [simulated_greeks(1, 73748.24)]
    data.append(simulated_quote(2, CALL_LEG, "1200", "1205"))
    data.append(simulated_quote(2, PUT_LEG, "1150", "1155"))
    call_bid, call_ask, put_bid, put_ask, size = SCENARIO_QUOTES[scenario]
    for second in range(6, 24, 2):
        data.append(simulated_quote(second, CALL_LEG, call_bid, call_ask, size))
        data.append(simulated_quote(second, PUT_LEG, put_bid, put_ask, size))
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="SIMULATED Level 1 entry demo (backtest engine)")
    parser.add_argument("--mode", choices=["dry-run", "fills"], default="dry-run")
    parser.add_argument("--scenario", choices=sorted(SCENARIO_QUOTES.keys()), default="hold")
    arguments = parser.parse_args(argv)

    print(f"=== SIMULATED demo, mode={arguments.mode}, scenario={arguments.scenario} (no Bybit connection) ===")

    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=TraderId("SIM-001"), logging=LoggingConfig(log_level="INFO"))
    )
    engine.add_venue(
        venue=Venue("BYBIT"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
    )
    for instrument in make_chain(STRIKES, expiry_at(4)):
        engine.add_instrument(instrument)
    engine.add_data(build_data(arguments.scenario))

    quantity = "0.01"
    if arguments.scenario == "partial-exit":
        quantity = "0.02"
    config = BTCStraddleConfig(submit_orders=(arguments.mode == "fills"), order_quantity=quantity)
    if arguments.scenario == "one-leg-denied":
        strategy = PutDeniedStrategy(config)
    else:
        strategy = BTCStraddleStrategy(config)
    engine.add_strategy(strategy)
    engine.run(start=TEST_NOW, end=TEST_NOW + datetime.timedelta(seconds=25))

    print("=== Result ===")
    print(f"Final state: {strategy.entry_state}")
    for old_state, new_state, reason in strategy.state_machine.history:
        print(f"  {old_state} -> {new_state}: {reason}")
    if strategy.call_entry is not None:
        print(strategy.call_entry.describe())
        print(strategy.put_entry.describe())
    premium = strategy.actual_entry_premium()
    if premium is None:
        print("Actual entry premium: none (nothing was filled)")
    else:
        print(f"Actual entry premium: {premium} USDT")
    for tracker in strategy.all_recovery_trackers:
        print(f"  recovery {tracker.describe()}")
    if strategy.exit_reason is not None:
        print(f"Exit reason: {strategy.exit_reason}, exit rounds: {strategy.exit_attempts}")
        for tracker in strategy.all_exit_trackers:
            print(f"  exit {tracker.describe()}")
        exit_cost = strategy.actual_exit_cost()
        print(f"Buyback cost paid: {exit_cost} USDT, result: {strategy.entry_premium - exit_cost:+} USDT (before fees)")
    for position in engine.cache.positions():
        state = "CLOSED" if position.is_closed else "OPEN"
        print(f"Position ({state}): {position.instrument_id} {position.side.name} {position.quantity} realized PnL {position.realized_pnl}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
