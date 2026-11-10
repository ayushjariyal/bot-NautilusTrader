"""
Offline tests of the strategy's ATM selection flow (Milestone 5).

The real BTCStraddleStrategy runs inside NautilusTrader's BacktestEngine with SIMULATED
option instruments (tests/factories.py). The backtest has no Bybit ticker feed, so no
greeks arrive by themselves; this lets us test two situations:
  * the timeout path (nothing arrives -> selection fails cleanly), and
  * the selection path, by handing the strategy OptionGreeks objects ourselves.
No network, no orders.
"""

import datetime
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import OptionGreeks
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money

from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.factories import TEST_NOW
from tests.factories import expiry_at
from tests.factories import make_chain


STRIKES = [73000, 73250, 73500, 73750, 74000, 74250, 74500]


def run_strategy(instruments, config=None, minutes=1):
    """Run the strategy for `minutes` of simulated time; return (engine, strategy)."""
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
    engine.run(start=TEST_NOW, end=TEST_NOW + datetime.timedelta(minutes=minutes))
    return engine, strategy


def make_greeks(instrument_id_text, underlying_price):
    """A simulated OptionGreeks update carrying a reference price."""
    return OptionGreeks(
        instrument_id=InstrumentId.from_str(instrument_id_text),
        delta=0.5,
        gamma=0.0002,
        vega=40.0,
        theta=-90.0,
        rho=0.0,
        mark_iv=0.40,
        bid_iv=None,
        ask_iv=None,
        underlying_price=underlying_price,
        open_interest=None,
        ts_event=0,
        ts_init=0,
    )


def test_start_chooses_nearest_expiry_and_middle_reference_contract():
    instruments = make_chain(STRIKES, expiry_at(4)) + make_chain(STRIKES, expiry_at(12))
    engine, strategy = run_strategy(instruments, minutes=0)
    try:
        assert strategy.target_expiry_ns == make_chain([73750], expiry_at(4))[0].expiration_ns
    finally:
        engine.dispose()


def test_timeout_when_no_reference_price_arrives():
    # 1 minute of simulated time > 30 s timeout, and no greeks are ever delivered.
    engine, strategy = run_strategy(make_chain(STRIKES, expiry_at(4)), minutes=1)
    try:
        assert strategy.atm_selection is None
        assert strategy.reference_instrument_id is None  # unsubscribed after the timeout
        # Since Milestone 10 the timeout moves the state machine to SELECTION_FAILED.
        assert strategy.entry_state == BTCStraddleStrategy.SELECTION_FAILED
        assert "[SELECTION_FAILED]" in strategy.describe_status()
    finally:
        engine.dispose()


def test_greeks_from_reference_contract_complete_the_selection():
    engine, strategy = run_strategy(make_chain(STRIKES, expiry_at(4)), minutes=0)
    try:
        # Pretend we are still waiting (the short run already stopped the strategy).
        strategy.reference_instrument_id = InstrumentId.from_str("BTC-12NOV26-73750-C-USDT-OPTION.BYBIT")
        strategy.on_option_greeks(make_greeks("BTC-12NOV26-73750-C-USDT-OPTION.BYBIT", 73722.59709671))

        selection = strategy.atm_selection
        assert selection.is_valid()
        assert selection.pair.strike == Decimal("73750")
        assert strategy.reference_instrument_id is None
        assert "ATM strike 73750" in strategy.describe_status()
    finally:
        engine.dispose()


def test_greeks_from_other_contracts_are_ignored():
    engine, strategy = run_strategy(make_chain(STRIKES, expiry_at(4)), minutes=0)
    try:
        strategy.reference_instrument_id = InstrumentId.from_str("BTC-12NOV26-73750-C-USDT-OPTION.BYBIT")
        strategy.on_option_greeks(make_greeks("BTC-12NOV26-74000-P-USDT-OPTION.BYBIT", 73722.0))
        assert strategy.atm_selection is None
    finally:
        engine.dispose()


def test_update_without_price_is_skipped_and_next_valid_one_is_used():
    engine, strategy = run_strategy(make_chain(STRIKES, expiry_at(4)), minutes=0)
    try:
        reference = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
        strategy.reference_instrument_id = InstrumentId.from_str(reference)

        strategy.on_option_greeks(make_greeks(reference, None))
        assert strategy.atm_selection is None
        assert strategy.rejected_reference_updates == 1

        strategy.on_option_greeks(make_greeks(reference, 74130.0))
        assert strategy.atm_selection.pair.strike == Decimal("74250")
    finally:
        engine.dispose()


def test_sparse_chain_is_rejected():
    engine, strategy = run_strategy(make_chain([60000, 90000], expiry_at(4)), minutes=0)
    try:
        selection = strategy.complete_atm_selection(73722.0)
        assert selection.is_valid() is False
        assert "ATM selection failed" in strategy.describe_status()
    finally:
        engine.dispose()
