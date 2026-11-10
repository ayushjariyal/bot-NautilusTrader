"""
Milestone 3A: prove that Bybit TESTNET public market data reaches NautilusTrader.

What it does
------------
Builds a data-only TradingNode (the same Bybit data client config as main.py, NO
execution client, NO API keys) with a small Actor that:
  1. counts the BTC option contracts the data client loaded over REST,
  2. picks ONE sample BTC option (nearest expiry at least 24 h away, middle strike),
  3. subscribes to its quotes and option greeks over the public WebSocket,
  4. after --run-seconds, prints a report and exits with code 0 (PASS) or 1 (FAIL).

The sample choice is deliberately simple. Proper ATM selection is Milestone 5.

Everything printed is REAL Bybit Testnet data. Nothing is simulated. No order can be
placed: the node has no execution client.

How to run:
    .venv\\Scripts\\python.exe scripts\\check_testnet_market_data.py --run-seconds 20

PASS criteria: at least one BTC option loaded, and at least one quote and one greeks
update received for the sample option. A quote with bid=0/ask=0 still counts as
"received"; it means the order book was empty at that moment (common on Testnet).
"""

import argparse
import os
import sys

# Make the project root importable when this file is run as a script.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nautilus_trader.adapters.bybit import BYBIT  # noqa: E402
from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory  # noqa: E402
from nautilus_trader.common.actor import Actor  # noqa: E402
from nautilus_trader.config import ActorConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.config import TradingNodeConfig  # noqa: E402
from nautilus_trader.live.node import TradingNode  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.model.identifiers import TraderId  # noqa: E402

from config.bybit_clients import assert_testnet_only  # noqa: E402
from config.bybit_clients import build_data_client_config  # noqa: E402
from config.settings import SettingsError  # noqa: E402
from config.settings import load_settings  # noqa: E402
from strategies.option_chain import contracts_from_instruments  # noqa: E402
from strategies.option_chain import filter_contracts  # noqa: E402
from strategies.option_chain import group_by_expiry  # noqa: E402


MINIMUM_HOURS_TO_EXPIRY = 24


class MarketDataProbeConfig(ActorConfig, frozen=True):
    """Settings for the probe. underlying: which options to look at."""

    underlying: str = "BTC"


class MarketDataProbe(Actor):
    """
    A data-only component (an Actor: like a Strategy, but it cannot trade).

    It records what it receives so main() can print a report after the node stops.
    """

    def __init__(self, config: MarketDataProbeConfig):
        super().__init__(config)
        self.underlying_symbol = config.underlying
        self.btc_option_count = 0
        self.sample_instrument_id = None
        self.quotes_received = 0
        self.greek_updates_received = 0
        self.two_sided_quotes_received = 0
        self.first_quote = None
        self.last_quote = None
        self.last_underlying_price = None

    def on_start(self):
        """
        Runs after the data client connected and loaded instruments into the cache.
        Finds BTC options, picks a sample, and subscribes to its live data.
        """
        now_ns = self.clock.timestamp_ns()

        # Same discovery helpers as the strategy (strategies/option_chain.py): they turn
        # instruments into plain OptionContract objects (this is where the
        # Currency-vs-str pitfall from guide 03A is handled, once).
        all_contracts = contracts_from_instruments(self.cache.instruments())
        btc_contracts = filter_contracts(all_contracts, self.underlying_symbol, now_ns, min_hours_to_expiry=0)
        self.btc_option_count = len(btc_contracts)
        self.log.info(f"BTC options loaded over REST: {self.btc_option_count}")

        candidates = filter_contracts(btc_contracts, self.underlying_symbol, now_ns, MINIMUM_HOURS_TO_EXPIRY)
        if not candidates:
            self.log.error(f"No BTC option expires {MINIMUM_HOURS_TO_EXPIRY} h or more from now")
            return

        # Nearest expiry first; within it, the middle strike (proper ATM is Milestone 5).
        groups = group_by_expiry(candidates)
        nearest_expiry_contracts = list(groups.values())[0]
        nearest_expiry_contracts.sort(key=self.strike_of)
        sample = nearest_expiry_contracts[len(nearest_expiry_contracts) // 2]

        self.sample_instrument_id = InstrumentId.from_str(sample.instrument_id)
        self.log.info(f"Sample option: {self.sample_instrument_id}")

        # One Bybit "ticker" WebSocket subscription feeds BOTH of these
        # (verified in adapters/bybit/data.py, 1.231.0).
        self.subscribe_quote_ticks(self.sample_instrument_id)
        self.subscribe_option_greeks(self.sample_instrument_id)

    @staticmethod
    def strike_of(contract):
        """Sort key: the contract's strike (a Decimal)."""
        return contract.strike

    def on_quote_tick(self, tick):
        """Called by the DataEngine for every quote of the sample option."""
        self.quotes_received += 1
        if self.first_quote is None:
            self.first_quote = tick
        self.last_quote = tick

        has_bid = tick.bid_price.as_double() > 0
        has_ask = tick.ask_price.as_double() > 0
        if has_bid and has_ask:
            self.two_sided_quotes_received += 1

    def on_option_greeks(self, option_greeks):
        """
        Called for every greeks update. underlying_price is Bybit's per-expiry reference
        price for the underlying (used for ATM selection in Milestone 5).
        """
        self.greek_updates_received += 1
        self.last_underlying_price = option_greeks.underlying_price

    def on_stop(self):
        if self.sample_instrument_id is not None:
            self.unsubscribe_quote_ticks(self.sample_instrument_id)
            self.unsubscribe_option_greeks(self.sample_instrument_id)

    def passed(self):
        """PASS criteria described in the module docstring."""
        if self.btc_option_count == 0:
            return False
        if self.quotes_received == 0:
            return False
        if self.greek_updates_received == 0:
            return False
        return True

    def report_lines(self):
        """The final report, one line each. Printed by main() after the node stops."""
        lines = [
            f"BTC options loaded (REST):         {self.btc_option_count}",
            f"Sample option:                     {self.sample_instrument_id if self.sample_instrument_id else 'none'}",
            f"Quotes received (WebSocket):       {self.quotes_received}",
            f"  of which two-sided (bid and ask > 0): {self.two_sided_quotes_received}",
            f"Greeks updates received:           {self.greek_updates_received}",
            f"Last underlying reference price:   {self.last_underlying_price}",
        ]
        if self.last_quote is not None:
            quote = self.last_quote
            lines.append(
                f"Last quote: bid {quote.bid_price} x {quote.bid_size} | "
                f"ask {quote.ask_price} x {quote.ask_size}"
            )
        if self.passed():
            lines.append("RESULT: PASS - Bybit Testnet market data reaches NautilusTrader")
        else:
            lines.append("RESULT: FAIL - see counts above")
        return lines


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Check Bybit Testnet market data")
    parser.add_argument("--run-seconds", type=int, default=20)
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)

    try:
        settings = load_settings()
        node_config = TradingNodeConfig(
            trader_id=TraderId("DATACHECK-001"),
            logging=LoggingConfig(log_level=arguments.log_level),
            data_clients={BYBIT: build_data_client_config(settings)},
            exec_clients={},
        )
        assert_testnet_only(node_config)
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    node = TradingNode(config=node_config)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)

    probe = MarketDataProbe(MarketDataProbeConfig())
    node.trader.add_actor(probe)
    node.build()

    node.get_event_loop().call_later(arguments.run_seconds, node.stop)
    try:
        node.run()
    finally:
        node.dispose()

    print("=== Bybit TESTNET market data check (real data, no orders) ===")
    for line in probe.report_lines():
        print(line)

    if probe.passed():
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
