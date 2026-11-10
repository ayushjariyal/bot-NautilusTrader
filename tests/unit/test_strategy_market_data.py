"""
End-to-end offline test of Milestones 5 + 6 inside NautilusTrader's BacktestEngine.

The engine replays a time-ordered stream of SIMULATED data:
  t+1 s   OptionGreeks for the reference contract (carries the reference price)
  t+2 s.. QuoteTicks for the two ATM legs
The real strategy selects the ATM pair from the greeks, subscribes to both legs, and its
status timer runs the entry check on the quotes in the Cache. No network, no orders.
"""

import datetime
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import OptionGreeks
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy
from tests.factories import TEST_NOW
from tests.factories import TEST_NOW_NS
from tests.factories import expiry_at
from tests.factories import make_chain


SECOND = 1_000_000_000
STRIKES = [73000, 73250, 73500, 73750, 74000, 74250, 74500]
REFERENCE_CONTRACT = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"  # middle strike of STRIKES
CALL_LEG = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
PUT_LEG = "BTC-12NOV26-73750-P-USDT-OPTION.BYBIT"


def greeks_at(seconds, underlying_price):
    ts = TEST_NOW_NS + seconds * SECOND
    return OptionGreeks(
        instrument_id=InstrumentId.from_str(REFERENCE_CONTRACT),
        delta=0.5, gamma=0.0002, vega=40.0, theta=-90.0, rho=0.0,
        mark_iv=0.4, bid_iv=None, ask_iv=None,
        underlying_price=underlying_price, open_interest=None,
        ts_event=ts, ts_init=ts,
    )


def quote_at(seconds, leg, bid, ask, bid_size="0.50", ask_size="0.50"):
    ts = TEST_NOW_NS + seconds * SECOND
    return QuoteTick(
        instrument_id=InstrumentId.from_str(leg),
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str(bid_size),
        ask_size=Quantity.from_str(ask_size),
        ts_event=ts,
        ts_init=ts,
    )


def run_with_data(data, seconds=25, config=None):
    """Run the strategy over `data` for `seconds`; returns (engine, strategy)."""
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
    engine.add_data(data)

    strategy = BTCStraddleStrategy(config or BTCStraddleConfig())
    engine.add_strategy(strategy)
    engine.run(start=TEST_NOW, end=TEST_NOW + datetime.timedelta(seconds=seconds))
    return engine, strategy


def liquid_quotes():
    """Tight, deep quotes for both legs every 2 s (spreads well under 0.5%)."""
    data = []
    for second in range(2, 24, 2):
        data.append(quote_at(second, CALL_LEG, "1200", "1205"))
        data.append(quote_at(second, PUT_LEG, "1150", "1155"))
    return data


def test_greeks_in_the_stream_select_the_atm_pair_and_legs_are_watched():
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + liquid_quotes())
    try:
        assert strategy.atm_selection.is_valid()
        assert strategy.atm_selection.pair.strike == Decimal("73750")
        # 11 quotes per leg were delivered to on_quote_tick after the subscription.
        assert strategy.quote_updates_by_leg[CALL_LEG] == 11
        assert strategy.quote_updates_by_leg[PUT_LEG] == 11
    finally:
        engine.dispose()


def test_entry_check_passes_on_tight_liquid_quotes():
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + liquid_quotes())
    try:
        report = strategy.last_liquidity_report  # set by the status timer at t+10 s and t+20 s
        assert report is not None
        assert report.ok, report.problems
        # (1200 + 1150) x 0.01 = 23.50 USDT received at the bid.
        assert report.entry_premium == Decimal("23.50")
        assert strategy.entry_allowed() is True
    finally:
        engine.dispose()


def test_entry_check_fails_on_testnet_like_empty_books():
    empty = []
    for second in range(2, 24, 2):
        empty.append(quote_at(second, CALL_LEG, "0", "0", "0.00", "0.00"))
        empty.append(quote_at(second, PUT_LEG, "0", "0", "0.00", "0.00"))
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + empty)
    try:
        assert strategy.last_liquidity_report.ok is False
        assert len(strategy.last_liquidity_report.problems) == 4
        assert strategy.entry_allowed() is False
    finally:
        engine.dispose()


def test_stale_quotes_block_entry():
    # Quotes stop after t+2 s; at the t+20 s status check they are 18 s old (> 10 s).
    # The put's only quote is too wide to enter on (since Milestone 7 a passing check at
    # t+2 s would enter immediately), so the strategy is still waiting at t+20 s.
    data = [greeks_at(1, 73748.24), quote_at(2, CALL_LEG, "1200", "1205"), quote_at(2, PUT_LEG, "1000", "1300")]
    engine, strategy = run_with_data(data)
    try:
        problems = strategy.last_liquidity_report.problems
        assert any("stale" in problem for problem in problems)
        assert strategy.entry_allowed() is False
    finally:
        engine.dispose()


def test_invalid_quantity_blocks_entry_even_with_good_quotes():
    config = BTCStraddleConfig(order_quantity="0.015")
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + liquid_quotes(), config=config)
    try:
        assert "not a multiple of the step" in strategy.quantity_problem
        assert strategy.entry_allowed() is False
    finally:
        engine.dispose()


def test_legs_are_unsubscribed_on_stop():
    engine, strategy = run_with_data([greeks_at(1, 73748.24)] + liquid_quotes())
    try:
        # The run ended, so on_stop() has already been called.
        assert strategy.call_instrument_id is None
        assert strategy.put_instrument_id is None
    finally:
        engine.dispose()
