"""
Milestone 3B: prove that the authenticated Bybit TESTNET execution connection works.

What it does
------------
Builds a TradingNode with BOTH Bybit clients (market data + execution) using your
Testnet API credentials from .env, waits for NautilusTrader's start-up reconciliation,
then reports what the execution client learned from Bybit:
  * the account (Unified Trading Account) and its balances,
  * open option positions,
  * open orders.

It NEVER places, changes or cancels an order. The component that runs the check is an
Actor, and Actors have no order-submission methods at all.

How to run:
    .venv\\Scripts\\python.exe scripts\\check_testnet_account.py --run-seconds 20

Exit codes: 0 = PASS (account received), 1 = FAIL, 2 = configuration error,
            3 = SKIPPED (no credentials configured yet).
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.bybit import BYBIT  # noqa: E402
from nautilus_trader.adapters.bybit import BYBIT_VENUE  # noqa: E402
from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory  # noqa: E402
from nautilus_trader.adapters.bybit import BybitLiveExecClientFactory  # noqa: E402
from nautilus_trader.common.actor import Actor  # noqa: E402
from nautilus_trader.config import ActorConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.config import TradingNodeConfig  # noqa: E402
from nautilus_trader.live.node import TradingNode  # noqa: E402
from nautilus_trader.model.identifiers import TraderId  # noqa: E402

from config.bybit_clients import assert_testnet_only  # noqa: E402
from config.bybit_clients import build_data_client_config  # noqa: E402
from config.bybit_clients import build_exec_client_config  # noqa: E402
from config.settings import SettingsError  # noqa: E402
from config.settings import load_settings  # noqa: E402


EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG_ERROR = 2
EXIT_SKIPPED = 3


class AccountProbe(Actor):
    """
    Read-only component: after start-up (which happens AFTER reconciliation), it copies
    the account, positions and orders that the execution client put into the Cache.
    """

    def __init__(self):
        super().__init__(ActorConfig())
        self.account_found = False
        self.balance_lines = []
        self.position_lines = []
        self.open_order_lines = []

    def on_start(self):
        account = self.cache.account_for_venue(BYBIT_VENUE)
        if account is None:
            self.log.error("No Bybit account in the cache: the execution client did not report one")
            return

        self.account_found = True
        self.log.info(f"Account {account.id} type={account.type}")

        # balances(): dict of Currency -> AccountBalance(total, locked, free).
        for currency, balance in account.balances().items():
            line = f"{currency.code}: total={balance.total} locked={balance.locked} free={balance.free}"
            self.balance_lines.append(line)

        for position in self.cache.positions_open(venue=BYBIT_VENUE):
            line = f"{position.instrument_id}: side={position.side} quantity={position.quantity} avg_open={position.avg_px_open}"
            self.position_lines.append(line)

        for order in self.cache.orders_open(venue=BYBIT_VENUE):
            line = f"{order.instrument_id}: {order.side} {order.quantity} @ {order.price} status={order.status}"
            self.open_order_lines.append(line)

    def on_stop(self):
        """Nothing to clean up (no subscriptions, no timers); defined to make that explicit."""
        return

    def report_lines(self):
        lines = [f"Account received from Bybit: {self.account_found}"]
        lines.append(f"Balances ({len(self.balance_lines)}):")
        for line in self.balance_lines:
            lines.append(f"  {line}")
        lines.append(f"Open positions ({len(self.position_lines)}):")
        for line in self.position_lines:
            lines.append(f"  {line}")
        lines.append(f"Open orders ({len(self.open_order_lines)}):")
        for line in self.open_order_lines:
            lines.append(f"  {line}")
        if self.account_found:
            lines.append("RESULT: PASS - authenticated Bybit Testnet connection works (no orders placed)")
        else:
            lines.append("RESULT: FAIL - no account received; check API key permissions and the log")
            lines.append(
                "  Hint: 'HTTP status code 401' in the log means Bybit rejected the key/secret "
                "(typo, deleted key, or a MAINNET key used on TESTNET)."
            )
        return lines


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Check the authenticated Bybit Testnet connection")
    parser.add_argument("--run-seconds", type=int, default=20)
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)

    try:
        settings = load_settings()
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not settings.has_credentials():
        print("SKIPPED: BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET are not set in .env.")
        return EXIT_SKIPPED

    try:
        node_config = TradingNodeConfig(
            trader_id=TraderId("ACCTCHECK-001"),
            logging=LoggingConfig(log_level=arguments.log_level),
            data_clients={BYBIT: build_data_client_config(settings)},
            exec_clients={BYBIT: build_exec_client_config(settings)},
        )
        assert_testnet_only(node_config)
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    print(f"Using Testnet API key {settings.masked_api_key()}")

    node = TradingNode(config=node_config)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)

    probe = AccountProbe()
    node.trader.add_actor(probe)
    node.build()

    node.get_event_loop().call_later(arguments.run_seconds, node.stop)
    try:
        node.run()
    finally:
        node.dispose()

    print("=== Bybit TESTNET account check (real data, no orders) ===")
    for line in probe.report_lines():
        print(line)

    if probe.account_found:
        return EXIT_PASS
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
