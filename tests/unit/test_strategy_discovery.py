"""
Offline test of the strategy's option discovery (Milestone 4).

A BacktestEngine (NautilusTrader's in-memory simulation) is loaded with SIMULATED
option instruments from tests/factories.py and our real BTCStraddleStrategy. Running the
engine calls on_start(), which performs discovery exactly as it does live. No network,
no orders.
"""

import datetime
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money

from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.factories import TEST_NOW
from tests.factories import expiry_at
from tests.factories import make_chain
from tests.factories import make_option


def run_discovery(instruments, config=None):
    """Load `instruments` into a backtest engine, run the strategy's on_start, return it."""
    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=TraderId("TEST-001"), logging=LoggingConfig(bypass_logging=True))
    )
    engine.add_venue(
        venue=Venue("BYBIT"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
    )
    for instrument in instruments:
        engine.add_instrument(instrument)

    strategy = BTCStraddleStrategy(config or BTCStraddleConfig())
    engine.add_strategy(strategy)

    # An explicit time window is required: without market data the engine has no natural
    # end, and the strategy's repeating status timer would advance simulated time forever
    # (the first version of this test hung for that reason). Starting at TEST_NOW also
    # makes the strategy's clock read TEST_NOW, so the expiry filter behaves realistically.
    engine.run(start=TEST_NOW, end=TEST_NOW + datetime.timedelta(minutes=1))
    engine.dispose()
    return strategy


def test_strategy_discovers_pairs_on_start():
    instruments = make_chain([75000, 76000, 77000], expiry_at(19)) + [make_option(78000, "CALL", expiry_at(19))]
    strategy = run_discovery(instruments)

    assert len(strategy.usable_contracts) == 7
    assert [pair.strike for pair in strategy.straddle_pairs] == [Decimal("75000"), Decimal("76000"), Decimal("77000")]


def test_strategy_skips_contracts_expiring_within_24_hours():
    # Expiry tomorrow 08:00 UTC is 20 h after TEST_NOW: below the default 24 h minimum.
    instruments = make_chain([76000], expiry_at(1)) + make_chain([76000], expiry_at(2))
    strategy = run_discovery(instruments)
    assert len(strategy.straddle_pairs) == 1
    assert strategy.straddle_pairs[0].expiry_ns == make_option(76000, expiry=expiry_at(2)).expiration_ns


def test_strategy_ignores_other_underlyings():
    instruments = make_chain([76000], expiry_at(19)) + make_chain([2800], expiry_at(19), underlying="ETH")
    strategy = run_discovery(instruments)
    assert len(strategy.straddle_pairs) == 1
    assert strategy.straddle_pairs[0].call.underlying == "BTC"


def test_strategy_reports_zero_pairs_when_only_calls_exist():
    instruments = [make_option(76000, "CALL"), make_option(77000, "CALL")]
    strategy = run_discovery(instruments)
    assert strategy.straddle_pairs == []


def test_on_reset_clears_discovery_results():
    strategy = run_discovery(make_chain([76000]))
    assert len(strategy.straddle_pairs) == 1
    strategy.on_reset()
    assert strategy.straddle_pairs == []
    assert strategy.usable_contracts == []
