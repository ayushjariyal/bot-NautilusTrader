"""
Entry point: builds and runs the NautilusTrader TradingNode with the straddle strategy.

Milestone 3B version: the node always has the Bybit TESTNET market data client (public
data, no API keys). When Testnet API credentials are configured it ALSO gets the Bybit
execution client (account, orders, positions). Without credentials it runs in
market-data-only mode, where no order can be sent.

How to run (from the btc-options-straddle folder; needs BYBIT_ENVIRONMENT=TESTNET,
for example in the .env file):
    .venv\\Scripts\\python.exe main.py --run-seconds 30
    .venv\\Scripts\\python.exe main.py            (runs until you press Ctrl+C)

Start-up sequence:
    1. parse_arguments()       read command-line options
    2. load_settings()         read .env / environment; refuse anything but TESTNET
    3. build_node_config()     describe the system we want (TradingNodeConfig)
    4. assert_testnet_only()   re-check every client config says TESTNET
    5. create_node()           TradingNode(config) creates the kernel (message bus, cache,
                               engines, portfolio, trader); the Bybit data and execution
                               client factories are registered under the name "BYBIT"
    6. trader.add_strategy()   register our strategy with the Trader
    7. node.build()            create the Bybit clients from their factories
    8. node.run()              connect, load instruments, start the strategy; blocks
    9. node.dispose()          release resources, always, even after an error
"""

import argparse
import asyncio
import sys

from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory
from nautilus_trader.adapters.bybit import BybitLiveExecClientFactory
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from config.bybit_clients import assert_testnet_only
from config.bybit_clients import build_data_client_config
from config.bybit_clients import build_exec_client_config
from config.settings import SettingsError
from config.settings import load_settings
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy


# Identifies this trading system in logs and in generated order IDs.
# Format required by NautilusTrader: NAME-TAG.
TRADER_ID = "STRADDLE-001"


def parse_arguments(argv=None):
    """
    Read command-line options.

    Parameters: argv (list[str] or None) - arguments to parse; None means the real
    command line. Tests pass their own list.
    Returns: argparse.Namespace with .run_seconds (int) and .log_level (str).
    """
    parser = argparse.ArgumentParser(description="BTC ATM short straddle (NautilusTrader)")
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=0,
        help="Stop automatically after this many seconds. 0 = run until Ctrl+C.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="How much NautilusTrader logs to the terminal.",
    )
    return parser.parse_args(argv)


def build_node_config(log_level, settings):
    """
    Describe the trading system we want NautilusTrader to build.

    A TradingNodeConfig is only a description; nothing starts until TradingNode is created
    and run. Anything not given here uses NautilusTrader's defaults (an in-memory cache,
    default engine settings, default timeouts).

    Parameters: log_level (str) - e.g. "INFO"; settings (AppSettings) - validated settings.
    Returns: TradingNodeConfig.
    Used by: main() and the unit tests.
    """
    # Execution client only when credentials exist. Without it the node cannot send
    # orders at all, which is the safest possible mode.
    exec_clients = {}
    if settings.has_credentials():
        exec_clients[BYBIT] = build_exec_client_config(settings)

    # Keep the Cache in line with Bybit WHILE running (start-up reconciliation is on by
    # default). Only useful with an execution client, so only enabled with credentials.
    #   open_check_interval_secs=10:     compare open orders with Bybit every 10 s
    #   position_check_interval_secs=30: compare positions with Bybit every 30 s and fetch
    #                                    missing fills if they differ
    # (Documented values: 5-10 s and 30-60 s respectively, nautilus_trader/live/config.py.)
    exec_engine = LiveExecEngineConfig()
    if settings.has_credentials():
        exec_engine = LiveExecEngineConfig(
            reconciliation=True,
            open_check_interval_secs=10.0,
            position_check_interval_secs=30.0,
        )

    config = TradingNodeConfig(
        trader_id=TraderId(TRADER_ID),
        logging=LoggingConfig(log_level=log_level),
        # Exchange connections, keyed by client name. The name "BYBIT" must match the
        # name used in add_*_client_factory() so build() knows which factory to use.
        #   data_clients - market data feeds (Bybit Testnet options data, public)
        #   exec_clients - orders, fills, account and positions (needs API credentials)
        data_clients={BYBIT: build_data_client_config(settings)},
        exec_clients=exec_clients,
        exec_engine=exec_engine,
    )
    return config


def create_node(node_config):
    """
    Create the TradingNode and register the client factories it needs.

    A factory is an object that knows how to build one kind of client. The node only
    stores client CONFIGS until build() is called; build() then looks up the factory
    registered under the same name ("BYBIT") and asks it to create the client.

    Each node gets a NEW asyncio event loop. Without one, TradingNode falls back to
    asyncio.get_event_loop(), which returns the previous loop even after an earlier
    node's dispose() has closed it; clients that schedule tasks while being built (the
    Bybit execution client does) then fail with "Event loop is closed". It also avoids
    Python's "There is no current event loop" DeprecationWarning.

    Parameters: node_config (TradingNodeConfig), already checked by assert_testnet_only.
    Returns: TradingNode (not built, not running).
    Used by: main() and the offline unit tests.
    """
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    node = TradingNode(config=node_config, loop=event_loop)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    # Registering a factory does not create a client. The execution client is only built
    # if node_config.exec_clients contains a "BYBIT" entry (i.e. credentials exist).
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    return node


def build_strategy(settings=None):
    """
    Create the straddle strategy.

    Parameters: settings (AppSettings or None). The ONLY setting passed on is whether
    orders may really be submitted: settings.orders_allowed() (False if settings is None).
    Everything else uses BTCStraddleConfig's defaults.
    Returns: BTCStraddleStrategy. main() registers it with the node's Trader.
    """
    submit_orders = False
    if settings is not None:
        submit_orders = settings.orders_allowed()
    strategy_config = BTCStraddleConfig(submit_orders=submit_orders)
    return BTCStraddleStrategy(config=strategy_config)


def describe_architecture(node):
    """
    List the main components inside a built TradingNode, one line each.

    This exists for learning: it turns the architecture diagram into something you can
    see in the terminal. It reads the node's kernel, which owns every component.

    Parameters: node (TradingNode) - must already be built.
    Returns: list[str], one human-readable line per component.
    """
    kernel = node.kernel

    data_client_ids = []
    for client_id in kernel.data_engine.registered_clients:
        data_client_ids.append(str(client_id))

    exec_client_ids = []
    for client_id in kernel.exec_engine.registered_clients:
        exec_client_ids.append(str(client_id))

    strategy_ids = []
    for strategy_id in node.trader.strategy_ids():
        strategy_ids.append(str(strategy_id))

    lines = [
        f"TradingNode       trader_id={node.trader_id}",
        f"  MessageBus      {type(kernel.msgbus).__name__}: routes every event and command",
        f"  Cache           {type(kernel.cache).__name__}: instruments, quotes, orders, positions",
        f"  DataEngine      {type(kernel.data_engine).__name__}: data clients = {data_client_ids or 'none'}",
        f"  RiskEngine      {type(kernel.risk_engine).__name__}: checks every order before it leaves",
        f"  ExecEngine      {type(kernel.exec_engine).__name__}: exec clients = {exec_client_ids or 'none'}",
        f"  Portfolio       {type(kernel.portfolio).__name__}: accounts, positions, P&L",
        f"  Trader          strategies = {strategy_ids or 'none'}",
    ]
    return lines


def schedule_automatic_stop(node, run_seconds):
    """
    Ask the node to stop itself after run_seconds (used for demos and tests).

    node.run() blocks while the node's asyncio event loop runs. call_later() schedules
    node.stop() on that same loop, so the node shuts down cleanly from the inside:
    strategies get on_stop(), clients disconnect, then run() returns.

    Parameters: node (TradingNode); run_seconds (int) - 0 or less means "do not schedule".
    Returns nothing.
    """
    if run_seconds <= 0:
        return
    event_loop = node.get_event_loop()
    event_loop.call_later(run_seconds, node.stop)


def main(argv=None):
    """
    Build, run and dispose of the trading node.

    Returns the process exit code (0 = normal exit).
    """
    arguments = parse_arguments(argv)

    # Safety first: stop here, before anything connects, if the settings are unsafe.
    try:
        settings = load_settings()
        node_config = build_node_config(arguments.log_level, settings)
        assert_testnet_only(node_config)
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    node = create_node(node_config)

    strategy = build_strategy(settings)
    node.trader.add_strategy(strategy)

    # build() turns each client config into a real client object using its factory.
    # Nothing connects yet: connections open inside run().
    node.build()

    logger = node.get_logger()
    for line in settings.describe_safety():
        logger.info(line)
    for line in describe_architecture(node):
        logger.info(line)

    schedule_automatic_stop(node, arguments.run_seconds)

    try:
        node.run()
    finally:
        # Always release resources (event loop, logger, open connections), even if
        # run() raised an exception or the user pressed Ctrl+C.
        node.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(main())
