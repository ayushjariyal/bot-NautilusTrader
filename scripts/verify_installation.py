"""
Milestone 1: check that the Python environment is ready for this project.

What this script does
---------------------
Part 1 - Version check
    Confirms we are running inside the project's virtual environment with a supported
    Python version and exactly NautilusTrader 1.231.0.

Part 2 - Import check
    Imports every NautilusTrader class that later milestones depend on. If NautilusTrader
    ever renames or removes one of them, we find out here, in one place, instead of in the
    middle of a trading session.

Part 3 - Offline engine smoke test
    Runs a tiny backtest: a Strategy receives 30 generated quote ticks and a clock timer.
    This proves that NautilusTrader's compiled Rust/Cython core loads and that the
    event loop delivers events to a strategy on this machine.
    It is modelled on the official example
    examples/backtest/example_02_use_clock_timer (v1.231.0). The official example reads a
    CSV file that only exists in the GitHub repository, not in the pip package, so we
    generate a few quotes ourselves instead.

    IMPORTANT: the quotes in Part 3 are SIMULATED. They are not Bybit data. No network
    connection is opened and no order is placed.

How to run
----------
    .venv\\Scripts\\python.exe scripts\\verify_installation.py

Output
------
Prints a PASS/FAIL line for each check and exits with code 0 if everything passed,
or 1 if anything failed. Nothing else in the project imports this file.
"""

import importlib
import sys
from decimal import Decimal


# The exact versions this project is written against.
# Later milestones rely on classes that only exist (with these names) in this release.
REQUIRED_NAUTILUS_VERSION = "1.231.0"

# NautilusTrader 1.231.0 publishes wheels for Python 3.12, 3.13 and 3.14.
MINIMUM_PYTHON = (3, 12)
MAXIMUM_PYTHON_EXCLUSIVE = (3, 15)

# Every (module, class name) pair that later milestones import.
# Each entry says which milestone needs it, so a failure here tells us what would break.
REQUIRED_IMPORTS = [
    # Milestone 2: the live trading node and the strategy base class.
    ("nautilus_trader.live.node", "TradingNode"),
    ("nautilus_trader.config", "TradingNodeConfig"),
    ("nautilus_trader.config", "StrategyConfig"),
    ("nautilus_trader.config", "LoggingConfig"),
    ("nautilus_trader.config", "InstrumentProviderConfig"),
    ("nautilus_trader.trading.strategy", "Strategy"),
    # Milestone 3A / 3B: the official Bybit adapter.
    ("nautilus_trader.adapters.bybit", "BYBIT"),
    ("nautilus_trader.adapters.bybit", "BybitDataClientConfig"),
    ("nautilus_trader.adapters.bybit", "BybitExecClientConfig"),
    ("nautilus_trader.adapters.bybit", "BybitLiveDataClientFactory"),
    ("nautilus_trader.adapters.bybit", "BybitLiveExecClientFactory"),
    ("nautilus_trader.adapters.bybit", "BybitEnvironment"),
    ("nautilus_trader.adapters.bybit", "BybitProductType"),
    # Milestones 4-5: option contracts and their metadata.
    ("nautilus_trader.model.instruments", "CryptoOption"),
    ("nautilus_trader.model.enums", "OptionKind"),
    # Milestone 6: market data events.
    ("nautilus_trader.model.data", "QuoteTick"),
    ("nautilus_trader.model.data", "OptionGreeks"),
    # Milestones 7-10: orders and positions.
    ("nautilus_trader.model.enums", "OrderSide"),
    ("nautilus_trader.model.enums", "TimeInForce"),
    ("nautilus_trader.model.objects", "Price"),
    ("nautilus_trader.model.objects", "Quantity"),
]

# Number of simulated quotes fed into the smoke test (one every 10 seconds = 5 minutes).
SMOKE_TEST_QUOTE_COUNT = 30


def print_result(passed, label, detail):
    """
    Print one line of the report in a consistent format.

    Parameters: passed (bool) - did the check succeed; label (str) - short name of the
    check; detail (str) - extra information shown after the label.
    Returns nothing. Called by every check function below.
    """
    if passed:
        status = "PASS"
    else:
        status = "FAIL"
    print(f"[{status}] {label}: {detail}")


def check_python_version():
    """
    Check that the running Python is within the range NautilusTrader 1.231.0 supports.

    Returns True if the version is supported, otherwise False.
    """
    current = sys.version_info[:2]
    version_text = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    is_supported = MINIMUM_PYTHON <= current < MAXIMUM_PYTHON_EXCLUSIVE
    print_result(is_supported, "Python version", version_text)
    return is_supported


def check_virtual_environment():
    """
    Check that we are running inside a virtual environment, not the system Python.

    A virtual environment keeps this project's packages separate from every other
    Python project on the machine. Inside a venv, sys.prefix (the active environment)
    differs from sys.base_prefix (the Python installation it was created from).

    Returns True if running inside a venv, otherwise False.
    """
    in_venv = sys.prefix != sys.base_prefix
    print_result(in_venv, "Virtual environment", sys.prefix)
    return in_venv


def check_nautilus_version():
    """
    Check that exactly the pinned NautilusTrader version is installed.

    Returns True on an exact match, otherwise False (including when it is not installed).
    """
    try:
        import nautilus_trader
    except ImportError as error:
        print_result(False, "NautilusTrader installed", str(error))
        return False

    installed = nautilus_trader.__version__
    matches = installed == REQUIRED_NAUTILUS_VERSION
    detail = f"installed {installed}, required {REQUIRED_NAUTILUS_VERSION}"
    print_result(matches, "NautilusTrader version", detail)
    return matches


def check_required_imports():
    """
    Try to import every class listed in REQUIRED_IMPORTS.

    Returns True if all imports succeed. Each failure is printed individually so we can
    see exactly which class is missing.
    """
    all_found = True

    for module_name, class_name in REQUIRED_IMPORTS:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            print_result(False, f"import {module_name}", str(error))
            all_found = False
            continue

        if not hasattr(module, class_name):
            print_result(False, f"{module_name}.{class_name}", "not found in module")
            all_found = False

    found_count = len(REQUIRED_IMPORTS)
    if all_found:
        print_result(True, "Required imports", f"all {found_count} classes found")
    return all_found


def run_offline_smoke_test():
    """
    Run a tiny offline backtest to prove the NautilusTrader engine works on this machine.

    How it works:
      1. Create a BacktestEngine (an in-memory, simulated version of the trading system).
      2. Add a simulated venue and a test instrument (BTCUSDT from NautilusTrader's test kit).
      3. Generate SMOKE_TEST_QUOTE_COUNT simulated QuoteTick objects.
      4. Add a Strategy that counts the quotes it receives and logs a timer every minute.
      5. Run the engine, then check the strategy received every quote.

    Returns True if the strategy received all quotes and at least one timer event.
    """
    # These imports live inside the function so that Part 2 can report a missing class
    # cleanly before we get here.
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.enums import AccountType
    from nautilus_trader.model.enums import OmsType
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.model.objects import Price
    from nautilus_trader.model.objects import Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.trading.strategy import Strategy

    class QuoteCountingStrategy(Strategy):
        """
        A minimal strategy: it subscribes to quotes, counts them, and logs a timer.

        It never places orders. Its only purpose is to show that the engine calls
        on_start, on_quote_tick, the timer callback and on_stop in the right order.
        """

        def __init__(self, instrument_id):
            super().__init__()
            self.instrument_id = instrument_id
            self.quotes_received = 0
            self.timer_events_received = 0

        def on_start(self):
            # Called once when the engine starts this strategy.
            # Subscribing tells the DataEngine to forward this instrument's quotes to us.
            self.subscribe_quote_ticks(self.instrument_id)

            # A repeating timer, as in the official clock-timer example.
            # interval is a datetime.timedelta; callback is called with a TimeEvent.
            import datetime

            self.clock.set_timer(
                name="every_minute",
                interval=datetime.timedelta(minutes=1),
                callback=self.on_timer,
            )

        def on_quote_tick(self, tick):
            # Called by the engine for every quote of a subscribed instrument.
            self.quotes_received += 1

        def on_timer(self, event):
            self.timer_events_received += 1

        def on_stop(self):
            # Called once when the engine stops. Good practice: undo subscriptions.
            self.unsubscribe_quote_ticks(self.instrument_id)

    # Step 1: the engine. log_level ERROR keeps the output short for this check.
    engine_config = BacktestEngineConfig(
        trader_id=TraderId("VERIFY-001"),
        logging=LoggingConfig(log_level="ERROR"),
    )
    engine = BacktestEngine(config=engine_config)

    # Step 2: a simulated venue and a test instrument. Both are fake and local.
    instrument = TestInstrumentProvider.btcusdt_binance()
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(10_000, USDT)],
    )
    engine.add_instrument(instrument)

    # Step 3: simulated quotes, one every 10 seconds, with a bid/ask 1 USDT apart.
    start_time_ns = 1_700_000_000 * 1_000_000_000
    ten_seconds_ns = 10 * 1_000_000_000
    quotes = []
    for index in range(SMOKE_TEST_QUOTE_COUNT):
        timestamp_ns = start_time_ns + index * ten_seconds_ns
        bid = Decimal("75000.00") + Decimal(index)
        ask = bid + Decimal("1.00")
        quote = QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price(bid, precision=instrument.price_precision),
            ask_price=Price(ask, precision=instrument.price_precision),
            bid_size=Quantity(Decimal("1.0"), precision=instrument.size_precision),
            ask_size=Quantity(Decimal("1.0"), precision=instrument.size_precision),
            ts_event=timestamp_ns,
            ts_init=timestamp_ns,
        )
        quotes.append(quote)
    engine.add_data(quotes)

    # Step 4 and 5: add the strategy, run, and inspect what it received.
    strategy = QuoteCountingStrategy(instrument.id)
    engine.add_strategy(strategy)
    engine.run()

    quotes_ok = strategy.quotes_received == SMOKE_TEST_QUOTE_COUNT
    timer_ok = strategy.timer_events_received > 0
    detail = (
        f"strategy received {strategy.quotes_received}/{SMOKE_TEST_QUOTE_COUNT} simulated quotes "
        f"and {strategy.timer_events_received} timer events"
    )
    engine.dispose()

    passed = quotes_ok and timer_ok
    print_result(passed, "Offline engine smoke test", detail)
    return passed


def main():
    """
    Run all checks in order and return the process exit code (0 = success, 1 = failure).
    """
    print("=== Milestone 1: environment verification ===")

    results = []
    results.append(check_python_version())
    results.append(check_virtual_environment())
    nautilus_ok = check_nautilus_version()
    results.append(nautilus_ok)

    # Parts 2 and 3 only make sense if NautilusTrader is installed at the right version.
    if nautilus_ok:
        results.append(check_required_imports())
        results.append(run_offline_smoke_test())

    if all(results):
        print("=== All checks passed ===")
        return 0

    print("=== Some checks FAILED (see above) ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
