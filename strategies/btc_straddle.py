"""
The BTC ATM short straddle strategy (final version, Milestone 10).

Level 1: find the BTC options, select the ATM call and put, check liquidity, sell both
legs, track orders and positions. Level 2: close both legs at a 1% stop-loss or 2%
take-profit on the combined buyback cost (ask side). Milestone 10: one explicit state
machine, recovery from incomplete entries, order timeouts, an ERROR state, a stale-data
watchdog and safe shutdown.

State machine (execution/straddle_state.py; every change goes through set_state()):

    SELECTING -> WAITING_FOR_ENTRY -> ENTRY_SUBMITTED -> ENTERED -> EXITING -> CLOSED
        |               |                  |               ^
        |               v                  v               |
        |        DRY_RUN_COMPLETE      RECOVERING ---------+----> CLOSED
        v                                                   (nothing matched)
    SELECTION_FAILED / EXISTING_POSITIONS_FOUND
    (ERROR can be reached from any non-final state)

Event flow:
    on_start()            discover_options(); check_existing_positions(); start_atm_selection()
    on_option_greeks()    complete_atm_selection() -> start_leg_market_data()
    on_quote_tick()       WAITING_FOR_ENTRY: entry check -> enter_straddle()
                          ENTERED: check_risk_and_exit() -> start_exit()
                          EXITING / RECOVERING waiting for an ask: continue_closing()
    on_order_*()          update the LegOrderTrackers -> after_order_event():
                          ENTRY_SUBMITTED -> check_entry_progress()
                          RECOVERING      -> advance_recovery()
                          EXITING         -> check_exit_progress()
    on_order_timeout()    orders stuck without a final status: recover / cancel / ERROR
    on_status_timer()     quotes, positions, risk, position health, stale-data watchdog
    on_stop()             cancel our working orders; warn if positions stay open

Who uses this file: main.py creates one BTCStraddleStrategy from a BTCStraddleConfig and
hands it to the TradingNode's Trader, which then calls the on_* methods below.
"""

import datetime
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from data.market_data import describe_quote
from data.market_data import leg_quote_from_tick
from execution import straddle_state
from execution.leg_tracker import ENTRY_COMPLETE
from execution.leg_tracker import ENTRY_INCOMPLETE
from execution.leg_tracker import LegOrderTracker
from execution.leg_tracker import summarize_entry
from execution.order_gateway import SUBMITTED
from execution.order_gateway import OrderGateway
from execution.position_manager import FLAT
from execution.position_manager import LONG
from execution.position_manager import SHORT
from execution.position_manager import check_straddle_positions
from execution.position_manager import describe_leg_position
from execution.position_manager import net_leg_position
from execution.recovery_planner import KEEP_STRADDLE
from execution.recovery_planner import describe_plan
from execution.recovery_planner import plan_recovery
from execution.straddle_state import StraddleStateMachine
from strategies.instrument_selector import choose_reference_contract
from strategies.instrument_selector import choose_target_expiry
from strategies.instrument_selector import describe_selection
from strategies.instrument_selector import pairs_for_expiry
from strategies.instrument_selector import parse_reference_price
from strategies.instrument_selector import select_atm_pair
from strategies.liquidity_check import check_entry_liquidity
from strategies.liquidity_check import describe_report
from strategies.liquidity_check import validate_order_quantity
from strategies.option_chain import contracts_from_instruments
from strategies.option_chain import describe_contract
from strategies.option_chain import filter_contracts
from strategies.option_chain import find_call_put_pairs
from strategies.option_chain import round_down_to_tick
from strategies.option_chain import round_up_to_tick
from strategies.option_chain import summarize_chain
from strategies.premium_calculator import PremiumValue
from strategies.premium_calculator import combined_buyback_cost
from strategies.premium_calculator import combined_entry_premium
from strategies.premium_calculator import straddle_quantity
from strategies.risk_manager import HOLD
from strategies.risk_manager import evaluate_exit
from strategies.risk_manager import risk_levels


# Results of send_reduce_only_round().
ROUND_SENT = "SENT"
ROUND_WAITING_FOR_QUOTE = "WAITING_FOR_QUOTE"
ROUND_LIMIT_REACHED = "LIMIT_REACHED"
ROUND_NOTHING_TO_DO = "NOTHING_TO_DO"

# Purposes of a reduce-only round.
EXIT = "EXIT"
RECOVERY = "RECOVERY"


class BTCStraddleConfig(StrategyConfig, frozen=True):
    """
    Settings for the straddle strategy.

    NautilusTrader configs are immutable "structs" (frozen=True): once created, a value
    cannot be changed by accident while the strategy is running.

    Fields
    ------
    underlying : str
        The asset whose options we trade ("BTC").
    min_hours_to_expiry : float
        Ignore contracts expiring sooner than this (default 24 hours).
    max_atm_distance_pct : float
        Refuse the ATM selection if even the nearest strike is further than this from
        the reference price (percent).
    reference_price_timeout_seconds : int
        How long to wait for the first valid reference price.
    order_quantity : str
        Quantity of EACH leg, as text so it stays an exact Decimal ("0.01" = an option
        on 0.01 BTC). Both legs always use the same quantity.
    max_spread_pct : float
        Largest acceptable bid-ask spread per leg for entry (% of mid).
    max_quote_age_seconds : float
        A quote older than this is stale: it blocks entry and cannot value the buyback.
    stop_loss_pct : float
        Level 2 stop-loss (1%); also refuses an entry that would start beyond it.
    take_profit_pct : float
        Level 2 take-profit (2%).
    exit_price_offset_ticks : int
        Buy-back limit price = ask + this many ticks (rounded up to the tick).
    max_exit_attempts : int
        Rounds of reduce-only orders allowed for an exit, and separately for a recovery,
        before the strategy gives up and goes to ERROR.
    order_timeout_seconds : int
        If our orders still have no final status after this long (for example during a
        disconnection), they are canceled; if that does not resolve them, ERROR.
    max_unvalued_seconds : int
        If the open straddle cannot be valued (no ask, stale quotes) for longer than this,
        every status line logs an error: the position is not being monitored.
    submit_orders : bool
        True ONLY when real Testnet orders are allowed (main.py: settings.orders_allowed()).
        Default False: the order gateway logs orders instead of submitting them.
    status_log_interval_seconds : int
        How often the strategy logs its status.
    """

    underlying: str = "BTC"
    min_hours_to_expiry: float = 24.0
    max_atm_distance_pct: float = 2.0
    reference_price_timeout_seconds: int = 30
    order_quantity: str = "0.01"
    max_spread_pct: float = 0.5
    max_quote_age_seconds: float = 10.0
    stop_loss_pct: float = 1.0
    take_profit_pct: float = 2.0
    exit_price_offset_ticks: int = 2
    max_exit_attempts: int = 5
    order_timeout_seconds: int = 20
    max_unvalued_seconds: int = 60
    submit_orders: bool = False
    status_log_interval_seconds: int = 10


class BTCStraddleStrategy(Strategy):
    """
    The BTC ATM short straddle. NautilusTrader calls the on_* methods.
    """

    # Timer names (unique per strategy clock).
    STATUS_TIMER_NAME = "straddle_status"
    REFERENCE_TIMEOUT_NAME = "reference_price_timeout"
    ORDER_TIMEOUT_NAME = "order_timeout"

    # States (see execution/straddle_state.py), repeated here for convenient access.
    SELECTING = straddle_state.SELECTING
    SELECTION_FAILED = straddle_state.SELECTION_FAILED
    EXISTING_POSITIONS_FOUND = straddle_state.EXISTING_POSITIONS_FOUND
    WAITING_FOR_ENTRY = straddle_state.WAITING_FOR_ENTRY
    DRY_RUN_COMPLETE = straddle_state.DRY_RUN_COMPLETE
    ENTRY_SUBMITTED = straddle_state.ENTRY_SUBMITTED
    RECOVERING = straddle_state.RECOVERING
    ENTERED = straddle_state.ENTERED
    EXITING = straddle_state.EXITING
    CLOSED = straddle_state.CLOSED
    ERROR = straddle_state.ERROR

    def __init__(self, config: BTCStraddleConfig):
        # The parent constructor connects this object to NautilusTrader's machinery.
        super().__init__(config)

        # Settings, copied into clearly named attributes. Names the Strategy base class
        # already uses ("greeks", "clock", "cache", ...) are avoided: some are read-only.
        self.underlying_symbol = config.underlying
        self.min_hours_to_expiry = config.min_hours_to_expiry
        self.max_atm_distance_pct = config.max_atm_distance_pct
        self.reference_price_timeout_seconds = config.reference_price_timeout_seconds
        self.order_quantity = Decimal(config.order_quantity)
        self.max_spread_pct = config.max_spread_pct
        self.max_quote_age_seconds = config.max_quote_age_seconds
        self.stop_loss_pct = config.stop_loss_pct
        self.take_profit_pct = config.take_profit_pct
        self.exit_price_offset_ticks = config.exit_price_offset_ticks
        self.max_exit_attempts = config.max_exit_attempts
        self.order_timeout_seconds = config.order_timeout_seconds
        self.max_unvalued_seconds = config.max_unvalued_seconds
        self.submit_orders = config.submit_orders
        self.status_log_interval_seconds = config.status_log_interval_seconds

        self.reset_state()

    def reset_state(self):
        """
        Put every piece of run-time state back to its starting value.
        Used by __init__ and on_reset (so the two can never disagree).
        """
        # The state machine (Milestone 10): the single source of truth for "what now?".
        self.state_machine = StraddleStateMachine(straddle_state.SELECTING)

        # Discovery (Milestone 4).
        self.usable_contracts = []          # list[OptionContract]
        self.straddle_pairs = []            # list[StraddlePair]
        self.btc_option_ids = set()         # set[str]: every BTC option contract

        # ATM selection (Milestone 5).
        self.target_expiry_ns = None        # int or None
        self.reference_instrument_id = None  # InstrumentId while subscribed
        self.atm_selection = None           # AtmSelection or None
        self.rejected_reference_updates = 0

        # Leg market data and entry check (Milestone 6).
        self.call_instrument_id = None      # InstrumentId while subscribed
        self.put_instrument_id = None
        self.quote_updates_by_leg = {}      # dict[str, int]
        self.quantity_problem = None        # str or None
        self.last_liquidity_report = None   # LiquidityReport or None

        # Entry orders (Milestone 7).
        self.order_gateway = None           # OrderGateway (created in on_start)
        self.call_entry = None              # LegOrderTracker or None
        self.put_entry = None
        self.trackers_by_client_order_id = {}  # dict[str, LegOrderTracker]
        self.entry_report = None            # LiquidityReport that allowed the entry

        # Positions (Milestone 8).
        self.existing_positions = []        # list[LegPosition] found at start-up
        self.position_problems = []         # list[str], latest verification
        self.position_events = 0
        self.expected_leg_quantity = None   # Decimal: each leg's size once ENTERED
        self.position_problem_strikes = 0   # consecutive status checks with problems

        # Level 2 (Milestone 9).
        self.entry_premium = None           # Decimal USDT actually received (straddle)
        self.risk_levels_usdt = None        # RiskLevels
        self.last_exit_decision = None      # ExitDecision
        self.exit_reason = None             # STOP_LOSS / TAKE_PROFIT
        self.exit_attempts = 0              # exit rounds sent
        self.all_exit_trackers = []         # every exit order

        # Recovery and robustness (Milestone 10).
        self.recovery_attempts = 0          # recovery rounds sent
        self.all_recovery_trackers = []     # every recovery order
        self.round_trackers = []            # the CURRENT reduce-only round's orders
        self.waiting_for_quote = False      # a round is waiting for a usable ask
        self.cancel_requested = False       # cancels were requested after a timeout
        self.unvalued_since_ns = None       # when the straddle stopped being valuable
        self.monitoring_alert_active = False
        self.stopped_with_open_positions = False
        self.status_updates_logged = 0

    @property
    def entry_state(self):
        """The current state (kept under this name for readability in tests and logs)."""
        return self.state_machine.state

    def set_state(self, new_state, reason):
        """
        Move the state machine to new_state, or refuse if the move is not allowed.

        Every accepted move is logged as "State: A -> B (reason)". A refused move is
        logged as an error and the state stays unchanged: it means the code tried to do
        something the design forbids, which must be visible.
        Returns: bool, True if the move happened.
        """
        old_state = self.state_machine.state
        if self.state_machine.move_to(new_state, reason):
            self.log.info(f"State: {old_state} -> {new_state} ({reason})")
            return True
        self.log.error(f"Refused state change {old_state} -> {new_state} ({reason})")
        return False

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------
    def on_start(self):
        """
        Called once by the Trader when the node starts this strategy. Instruments (and,
        with an execution client, reconciled orders and positions) are already in the
        Cache at this point.
        """
        self.log.info(f"Starting straddle strategy for underlying {self.underlying_symbol}")
        self.order_gateway = OrderGateway(
            submit_function=self.submit_order,
            orders_allowed=self.submit_orders,
            log_info=self.log.info,
            log_warning=self.log.warning,
        )
        self.log.info(f"Order gateway: {self.order_gateway.mode_text()}")
        self.warn_about_risky_settings()
        self.log_framework_overview()
        self.discover_options()

        if self.check_existing_positions():
            self.set_state(self.EXISTING_POSITIONS_FOUND, "BTC option positions already open; no new straddle")
        elif not self.start_atm_selection():
            self.set_state(self.SELECTION_FAILED, "no call/put pairs to select from")

        self.clock.set_timer(
            name=self.STATUS_TIMER_NAME,
            interval=datetime.timedelta(seconds=self.status_log_interval_seconds),
            callback=self.on_status_timer,
        )

    def on_stop(self):
        """
        Called once when the node stops this strategy.

        Safe shutdown:
          * cancel timers and data subscriptions;
          * cancel any of OUR orders still working (normally none: all orders are IOC);
          * open positions are NOT closed automatically. A stop may be caused by a crash
            or an operator, and closing options on thin books in a hurry can be very
            costly. Instead a clear warning lists them; on the next start they are found
            (EXISTING_POSITIONS_FOUND) and no second straddle is opened.
        """
        for timer_name in (self.STATUS_TIMER_NAME, self.REFERENCE_TIMEOUT_NAME, self.ORDER_TIMEOUT_NAME):
            if timer_name in self.clock.timer_names:
                self.clock.cancel_timer(timer_name)

        self.cancel_working_orders()
        self.warn_if_positions_remain()
        self.stop_reference_subscription()
        self.stop_leg_market_data()
        self.log.info(f"Stopped in state {self.entry_state} after {self.status_updates_logged} status updates")

    def on_reset(self):
        """Called if the strategy is reset (for example between backtest runs)."""
        self.reset_state()

    def warn_if_positions_remain(self):
        """Log the open leg positions at shutdown (if any). Sets stopped_with_open_positions."""
        call_position, put_position = self.current_leg_positions()
        if call_position is None:
            return
        if call_position.side == FLAT and put_position.side == FLAT:
            return
        self.stopped_with_open_positions = True
        self.log.warning(
            "Stopping with OPEN positions; they stay open on the exchange. On the next start "
            "they will be detected and no new straddle will be opened:"
        )
        self.log.warning(f"  {describe_leg_position('CALL', call_position)}")
        self.log.warning(f"  {describe_leg_position('PUT', put_position)}")

    def warn_about_risky_settings(self):
        """Warn if the spread limit cannot protect against an immediate stop-loss."""
        if self.max_spread_pct >= self.stop_loss_pct:
            self.log.warning(
                f"max_spread_pct ({self.max_spread_pct}%) >= stop_loss_pct ({self.stop_loss_pct}%): "
                "an entry at the bid could start beyond the stop-loss"
            )

    def log_framework_overview(self):
        """Log what the strategy can reach through the framework (cache, clock)."""
        instrument_count = len(self.cache.instruments())
        self.log.info(f"Clock time (UTC): {self.clock.utc_now().isoformat()}")
        self.log.info(f"Instruments in cache: {instrument_count}")
        self.log.info(f"Open positions in cache: {len(self.cache.positions_open())}")
        if instrument_count == 0:
            self.log.warning("No instruments loaded: check the Bybit data client lines earlier in the log")

    # ----------------------------------------------------------------------------------
    # Milestone 4: discovery
    # ----------------------------------------------------------------------------------
    def discover_options(self):
        """
        Find the BTC option contracts and the call/put pairs usable for a straddle
        (helpers in strategies/option_chain.py). Returns the number of pairs.
        """
        now_ns = self.clock.timestamp_ns()
        all_contracts = contracts_from_instruments(self.cache.instruments())
        self.btc_option_ids = set()
        for contract in all_contracts:
            if contract.underlying == self.underlying_symbol:
                self.btc_option_ids.add(contract.instrument_id)
        self.usable_contracts = filter_contracts(
            all_contracts, self.underlying_symbol, now_ns, self.min_hours_to_expiry
        )
        self.straddle_pairs = find_call_put_pairs(self.usable_contracts)

        summary = summarize_chain(
            all_contracts,
            self.usable_contracts,
            self.straddle_pairs,
            self.underlying_symbol,
            self.min_hours_to_expiry,
        )
        for line in summary:
            self.log.info(line)

        if len(self.straddle_pairs) == 0:
            self.log.warning("No call/put pair found: a straddle cannot be built")
            return 0

        example = self.straddle_pairs[0]
        self.log.info("Example pair (nearest usable expiry, lowest strike):")
        self.log.info(f"  {describe_contract(example.call)}")
        self.log.info(f"  {describe_contract(example.put)}")
        return len(self.straddle_pairs)

    # ----------------------------------------------------------------------------------
    # Milestone 5: ATM selection
    # ----------------------------------------------------------------------------------
    def start_atm_selection(self):
        """
        Choose the target expiry and subscribe to the greeks of one of its contracts to
        receive the reference price; start a timeout alert.
        Returns: bool, False if there is nothing to select from.
        """
        self.target_expiry_ns = choose_target_expiry(self.straddle_pairs)
        if self.target_expiry_ns is None:
            self.log.warning("ATM selection not started: no call/put pairs")
            return False

        expiry_pairs = pairs_for_expiry(self.straddle_pairs, self.target_expiry_ns)
        reference_contract = choose_reference_contract(expiry_pairs)
        self.reference_instrument_id = InstrumentId.from_str(reference_contract.instrument_id)

        expiry_text = reference_contract.expiry_utc().strftime("%Y-%m-%d %H:%M UTC")
        self.log.info(f"Target expiry: {expiry_text} ({len(expiry_pairs)} pairs)")
        self.log.info(f"Waiting for the reference price from {self.reference_instrument_id}")
        self.subscribe_option_greeks(self.reference_instrument_id)

        timeout_at = self.clock.utc_now() + datetime.timedelta(seconds=self.reference_price_timeout_seconds)
        self.clock.set_time_alert(
            name=self.REFERENCE_TIMEOUT_NAME,
            alert_time=timeout_at,
            callback=self.on_reference_timeout,
        )
        return True

    def on_option_greeks(self, option_greeks):
        """While selecting, a greeks update of the reference contract completes the selection."""
        if self.entry_state != self.SELECTING:
            return
        if self.reference_instrument_id is None:
            return
        if option_greeks.instrument_id != self.reference_instrument_id:
            return
        self.complete_atm_selection(option_greeks.underlying_price)

    def complete_atm_selection(self, raw_reference_price):
        """
        Validate the reference price and select the ATM pair.
        Returns: AtmSelection, or None if the price was unusable (keep waiting).
        """
        reference_price, problem = parse_reference_price(raw_reference_price)
        if problem is not None:
            self.rejected_reference_updates += 1
            self.log.warning(f"Ignoring reference price update: {problem}")
            return None

        expiry_pairs = pairs_for_expiry(self.straddle_pairs, self.target_expiry_ns)
        selection = select_atm_pair(expiry_pairs, reference_price, self.max_atm_distance_pct)
        self.atm_selection = selection

        for line in describe_selection(selection):
            if selection.is_valid():
                self.log.info(line)
            else:
                self.log.error(line)

        if self.REFERENCE_TIMEOUT_NAME in self.clock.timer_names:
            self.clock.cancel_timer(self.REFERENCE_TIMEOUT_NAME)
        self.stop_reference_subscription()

        if selection.is_valid():
            self.start_leg_market_data(selection.pair)
            self.set_state(self.WAITING_FOR_ENTRY, f"ATM strike {selection.pair.strike} selected")
        else:
            self.set_state(self.SELECTION_FAILED, f"ATM selection rejected: {selection.problem}")
        return selection

    def on_reference_timeout(self, event):
        """No valid reference price in time: selection fails."""
        if self.entry_state != self.SELECTING:
            return
        self.log.error(
            f"No valid reference price within {self.reference_price_timeout_seconds} s "
            f"({self.rejected_reference_updates} unusable updates): ATM selection failed"
        )
        self.stop_reference_subscription()
        self.set_state(self.SELECTION_FAILED, "reference price timeout")

    def stop_reference_subscription(self):
        """Unsubscribe from the reference contract's greeks, if still subscribed."""
        if self.reference_instrument_id is None:
            return
        self.unsubscribe_option_greeks(self.reference_instrument_id)
        self.reference_instrument_id = None

    # ----------------------------------------------------------------------------------
    # Milestone 6: leg market data and the entry check
    # ----------------------------------------------------------------------------------
    def start_leg_market_data(self, pair):
        """Validate the order quantity for both legs and subscribe to their quotes."""
        self.quantity_problem = None
        for contract in (pair.call, pair.put):
            problem = validate_order_quantity(self.order_quantity, contract)
            if problem is not None:
                self.quantity_problem = f"{contract.option_kind}: {problem}"
                self.log.error(f"Invalid order quantity: {self.quantity_problem}")
                break

        self.call_instrument_id = InstrumentId.from_str(pair.call.instrument_id)
        self.put_instrument_id = InstrumentId.from_str(pair.put.instrument_id)
        self.quote_updates_by_leg = {str(self.call_instrument_id): 0, str(self.put_instrument_id): 0}
        self.subscribe_quote_ticks(self.call_instrument_id)
        self.subscribe_quote_ticks(self.put_instrument_id)
        self.log.info(f"Watching quotes of both legs, order quantity {self.order_quantity} each")

    def on_quote_tick(self, tick):
        """
        Called for every quote of a subscribed leg (the Cache already holds it).
        What happens depends ONLY on the state; in final states (CLOSED, ERROR, ...)
        quotes are just counted.
        """
        key = str(tick.instrument_id)
        if key not in self.quote_updates_by_leg:
            return
        self.quote_updates_by_leg[key] += 1

        state = self.entry_state
        if state == self.WAITING_FOR_ENTRY:
            self.evaluate_entry_conditions()
            if self.entry_allowed():
                self.enter_straddle()
        elif state == self.ENTERED:
            self.check_risk_and_exit()
        elif state in (self.EXITING, self.RECOVERING) and self.waiting_for_quote:
            self.continue_closing()

    def current_leg_quotes(self):
        """Latest quotes of both legs from the Cache, as LegQuote objects (or None)."""
        if self.call_instrument_id is None or self.put_instrument_id is None:
            return None, None
        call_quote = leg_quote_from_tick(self.cache.quote_tick(self.call_instrument_id))
        put_quote = leg_quote_from_tick(self.cache.quote_tick(self.put_instrument_id))
        return call_quote, put_quote

    def evaluate_entry_conditions(self):
        """Run the liquidity entry check on the current quotes. Returns LiquidityReport or None."""
        if self.atm_selection is None or not self.atm_selection.is_valid():
            return None
        if self.call_instrument_id is None:
            return None
        call_quote, put_quote = self.current_leg_quotes()
        report = check_entry_liquidity(
            call_quote=call_quote,
            put_quote=put_quote,
            quantity=self.order_quantity,
            multiplier=self.atm_selection.pair.call.multiplier,
            max_spread_pct=self.max_spread_pct,
            max_quote_age_seconds=self.max_quote_age_seconds,
            stop_loss_pct=self.stop_loss_pct,
            now_ns=self.clock.timestamp_ns(),
        )
        self.last_liquidity_report = report
        return report

    def entry_allowed(self):
        """True only if the quantity is valid AND the latest entry check passed."""
        if self.quantity_problem is not None:
            return False
        if self.last_liquidity_report is None:
            return False
        return self.last_liquidity_report.ok

    def stop_leg_market_data(self):
        """Unsubscribe from both legs' quotes, if subscribed."""
        if self.call_instrument_id is not None:
            self.unsubscribe_quote_ticks(self.call_instrument_id)
            self.call_instrument_id = None
        if self.put_instrument_id is not None:
            self.unsubscribe_quote_ticks(self.put_instrument_id)
            self.put_instrument_id = None

    # ----------------------------------------------------------------------------------
    # Milestone 7: entry orders
    # ----------------------------------------------------------------------------------
    def build_entry_order(self, leg_name, instrument_id, bid_price):
        """
        Create (not send) one SELL LIMIT IOC order at the leg's best bid, price rounded
        down to the tick. reduce_only=False: this order opens a position.
        Returns: (order, description).
        """
        instrument = self.cache.instrument(instrument_id)
        tick = instrument.price_increment.as_decimal()
        limit_price = instrument.make_price(round_down_to_tick(bid_price, tick))
        quantity = instrument.make_qty(self.order_quantity)
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=quantity,
            price=limit_price,
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
            tags=["STRADDLE_ENTRY", leg_name],
        )
        description = f"SELL {quantity} {instrument_id} @ {limit_price} IOC (entry {leg_name})"
        return order, description

    def enter_straddle(self):
        """
        Sell both legs, once.

        Dry run: both orders are only logged by the gateway -> DRY_RUN_COMPLETE.
        Otherwise the state becomes ENTRY_SUBMITTED BEFORE submitting (no double entry),
        both orders are submitted, and an order timeout is armed: if the orders do not
        reach a final status in time (for example during a disconnection), the strategy
        cancels them and recovers from the actual positions.
        """
        call_quote, put_quote = self.current_leg_quotes()
        self.entry_report = self.last_liquidity_report

        call_order, call_text = self.build_entry_order("CALL", self.call_instrument_id, call_quote.bid)
        put_order, put_text = self.build_entry_order("PUT", self.put_instrument_id, put_quote.bid)

        self.call_entry = LegOrderTracker("CALL", self.call_instrument_id, self.order_quantity)
        self.put_entry = LegOrderTracker("PUT", self.put_instrument_id, self.order_quantity)
        self.trackers_by_client_order_id[str(call_order.client_order_id)] = self.call_entry
        self.trackers_by_client_order_id[str(put_order.client_order_id)] = self.put_entry

        self.log.info(
            f"Entering short straddle: expected premium {self.entry_report.entry_premium} USDT "
            f"(initial gap {self.entry_report.initial_gap_pct:.2f}%)"
        )

        if not self.order_gateway.orders_allowed:
            self.order_gateway.submit(call_order, call_text)
            self.order_gateway.submit(put_order, put_text)
            self.set_state(self.DRY_RUN_COMPLETE, "dry run: entry orders logged, not sent")
            return

        self.set_state(self.ENTRY_SUBMITTED, "both entry orders submitted")
        if self.order_gateway.submit(call_order, call_text) == SUBMITTED:
            self.call_entry.mark_submitted(call_order.client_order_id)
        if self.order_gateway.submit(put_order, put_text) == SUBMITTED:
            self.put_entry.mark_submitted(put_order.client_order_id)
        self.arm_order_timeout()

    def tracker_for(self, event):
        """The LegOrderTracker of an order event, or None if the order is not ours."""
        return self.trackers_by_client_order_id.get(str(event.client_order_id))

    def on_order_accepted(self, event):
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_accepted()
        self.log.info(f"Order accepted: {tracker.describe()}")

    def on_order_filled(self, event):
        """
        A (partial) fill: last_qty / last_px describe THIS fill only. The Cache's
        position already includes it (the ExecutionEngine updates positions before
        publishing the fill), so progress checks below see the new position.
        """
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_filled(event.last_qty.as_decimal(), event.last_px.as_decimal())
        self.log.info(f"Order filled: {event.last_qty} @ {event.last_px} -> {tracker.describe()}")
        if tracker.overfill_detected:
            self.log.error(f"OVERFILL on {tracker.leg_name}: filled more than ordered")
        self.cross_check_with_cache(event, tracker)
        self.after_order_event()

    def on_order_rejected(self, event):
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_rejected(event.reason)
        self.log.error(f"Order REJECTED: {tracker.describe()}")
        self.after_order_event()

    def on_order_denied(self, event):
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_denied(event.reason)
        self.log.error(f"Order DENIED by the RiskEngine: {tracker.describe()}")
        self.after_order_event()

    def on_order_canceled(self, event):
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_canceled()
        self.log.info(f"Order canceled: {tracker.describe()}")
        self.after_order_event()

    def on_order_expired(self, event):
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        tracker.on_expired()
        self.log.info(f"Order expired: {tracker.describe()}")
        self.after_order_event()

    def on_order_cancel_rejected(self, event):
        """
        A cancel request was refused (usually because the order had already finished).
        Nothing to change: the order's own final event follows, or the next order
        timeout escalates to ERROR.
        """
        tracker = self.tracker_for(event)
        if tracker is None:
            return
        self.log.warning(f"Cancel rejected for {tracker.leg_name}: {event.reason}")

    def cross_check_with_cache(self, event, tracker):
        """Our filled quantity must equal NautilusTrader's order.filled_qty."""
        order = self.cache.order(event.client_order_id)
        if order is None:
            return
        cache_filled = order.filled_qty.as_decimal()
        if cache_filled != tracker.filled_quantity:
            self.log.warning(
                f"{tracker.leg_name}: tracked fill {tracker.filled_quantity} differs from "
                f"NautilusTrader's order filled_qty {cache_filled}"
            )

    def after_order_event(self):
        """Advance whatever the current state is waiting for."""
        state = self.entry_state
        if state == self.ENTRY_SUBMITTED:
            self.check_entry_progress()
        elif state == self.RECOVERING:
            self.advance_recovery()
        elif state == self.EXITING:
            self.check_exit_progress()

    def check_entry_progress(self):
        """
        After every entry order event.
          COMPLETE   -> ENTERED (stop-loss / take-profit become active)
          INCOMPLETE -> RECOVERING (close unmatched exposure)
          PENDING    -> keep waiting
        """
        outcome = summarize_entry(self.call_entry, self.put_entry)
        if outcome.status == ENTRY_COMPLETE:
            self.cancel_order_timeout()
            self.set_state(self.ENTERED, "both legs fully filled")
            self.activate_straddle(outcome.matched_quantity)
        elif outcome.status == ENTRY_INCOMPLETE:
            self.cancel_order_timeout()
            self.start_recovery(
                f"entry incomplete: call filled {outcome.call_filled}, put filled {outcome.put_filled}"
            )
        return outcome

    def actual_entry_premium(self):
        """
        Premium actually received by the entry fills (both legs, all filled quantity).
        Returns: Decimal, or None if nothing filled.
        """
        if self.call_entry is None or self.put_entry is None:
            return None
        multiplier = self.atm_selection.pair.call.multiplier
        total = Decimal("0")
        any_fill = False
        for tracker in (self.call_entry, self.put_entry):
            average = tracker.average_price()
            if average is not None:
                total += average * tracker.filled_quantity * multiplier
                any_fill = True
        if not any_fill:
            return None
        return total

    def activate_straddle(self, quantity):
        """
        Start Level 2 monitoring for a balanced straddle of `quantity` per leg.

        The entry premium uses the ACTUAL average fill prices and the straddle's
        quantity (after a recovery that is the matched quantity, not everything that was
        sold), so the premium and the buyback are always measured on the same size.
        """
        self.expected_leg_quantity = quantity
        self.entry_premium = combined_entry_premium(
            self.call_entry.average_price(),
            self.put_entry.average_price(),
            quantity,
            self.atm_selection.pair.call.multiplier,
        )
        self.risk_levels_usdt = risk_levels(self.entry_premium, self.stop_loss_pct, self.take_profit_pct)
        self.log.info(
            f"STRADDLE ENTERED: {quantity} per leg, entry premium {self.entry_premium} USDT. "
            f"Stop-loss at buyback cost >= {self.risk_levels_usdt.stop_loss_level} USDT "
            f"(+{self.stop_loss_pct}%), take-profit at <= {self.risk_levels_usdt.take_profit_level} USDT "
            f"(-{self.take_profit_pct}%)"
        )

    # ----------------------------------------------------------------------------------
    # Milestone 8: positions
    # ----------------------------------------------------------------------------------
    def check_existing_positions(self):
        """
        At start-up: are there open positions in any BTC option (e.g. reconciled from
        Bybit after a restart)? If so, no new straddle may be opened.
        Returns: bool. Produces: self.existing_positions.
        """
        positions_by_instrument = {}
        for position in self.cache.positions_open():
            instrument_text = str(position.instrument_id)
            if instrument_text not in self.btc_option_ids:
                continue
            if instrument_text not in positions_by_instrument:
                positions_by_instrument[instrument_text] = []
            positions_by_instrument[instrument_text].append(position)

        self.existing_positions = []
        for instrument_text in sorted(positions_by_instrument.keys()):
            leg = net_leg_position(instrument_text, positions_by_instrument[instrument_text])
            if leg.side != FLAT:
                self.existing_positions.append(leg)

        if len(self.existing_positions) == 0:
            self.log.info("No existing BTC option positions: a new straddle may be opened")
            return False

        self.log.warning(f"Found {len(self.existing_positions)} existing BTC option positions; no new straddle will be opened:")
        for leg in self.existing_positions:
            self.log.warning(f"  {describe_leg_position('EXISTING', leg)}")
        return True

    def leg_instrument_ids(self):
        """InstrumentIds of the two selected legs, or (None, None) before a valid selection."""
        if self.atm_selection is None or not self.atm_selection.is_valid():
            return None, None
        pair = self.atm_selection.pair
        return InstrumentId.from_str(pair.call.instrument_id), InstrumentId.from_str(pair.put.instrument_id)

    def current_leg_positions(self):
        """Both legs' net positions from the Cache, or (None, None) without legs."""
        call_id, put_id = self.leg_instrument_ids()
        if call_id is None:
            return None, None
        call_position = net_leg_position(call_id, self.cache.positions_open(instrument_id=call_id))
        put_position = net_leg_position(put_id, self.cache.positions_open(instrument_id=put_id))
        return call_position, put_position

    def open_order_count_on_legs(self):
        """How many orders (ours or not) are still open on the two legs."""
        call_id, put_id = self.leg_instrument_ids()
        if call_id is None:
            return 0
        return len(self.cache.orders_open(instrument_id=call_id)) + len(self.cache.orders_open(instrument_id=put_id))

    def verify_positions(self):
        """
        While ENTERED: each leg must be SHORT by exactly expected_leg_quantity, and both
        legs equal. Returns the problems (also stored in self.position_problems).
        """
        if self.entry_state != self.ENTERED:
            return []
        call_position, put_position = self.current_leg_positions()
        if call_position is None:
            return []
        self.position_problems = check_straddle_positions(
            call_position,
            put_position,
            expected_call=self.expected_leg_quantity,
            expected_put=self.expected_leg_quantity,
            require_balanced=True,
        )
        return self.position_problems

    def check_position_health(self):
        """
        Called by the status timer while ENTERED. A position that does not match what
        the strategy holds (an unexpected quantity, a missing or extra leg) is something
        it does not understand. One mismatching check may be a timing effect (an event
        still on its way), so ERROR is entered only after TWO consecutive checks.
        Returns: list of problems.
        """
        problems = self.verify_positions()
        if len(problems) == 0:
            self.position_problem_strikes = 0
            return problems

        self.position_problem_strikes += 1
        for problem in problems:
            self.log.warning(f"  Position check ({self.position_problem_strikes}/2): {problem}")
        if self.position_problem_strikes >= 2:
            self.enter_error("unexpected positions: " + "; ".join(problems))
        return problems

    def is_our_leg(self, instrument_id):
        call_id, put_id = self.leg_instrument_ids()
        if call_id is None:
            return False
        return instrument_id == call_id or instrument_id == put_id

    def on_position_opened(self, event):
        if not self.is_our_leg(event.instrument_id):
            return
        self.position_events += 1
        self.log.info(f"Position OPENED: {event.instrument_id} {event.side.name} {event.quantity} @ {event.avg_px_open}")

    def on_position_changed(self, event):
        if not self.is_our_leg(event.instrument_id):
            return
        self.position_events += 1
        self.log.info(f"Position CHANGED: {event.instrument_id} {event.side.name} {event.quantity} @ {event.avg_px_open}")

    def on_position_closed(self, event):
        if not self.is_our_leg(event.instrument_id):
            return
        self.position_events += 1
        self.log.info(f"Position CLOSED: {event.instrument_id} realized PnL {event.realized_pnl}")

    # ----------------------------------------------------------------------------------
    # Milestone 9: Level 2 risk management
    # ----------------------------------------------------------------------------------
    def current_straddle_quantity(self):
        """The straddle's size from the ACTUAL positions (both legs must be equal)."""
        call_position, put_position = self.current_leg_positions()
        if call_position is None:
            return PremiumValue(None, "no legs selected")
        return straddle_quantity(call_position.quantity, put_position.quantity)

    def value_buyback(self):
        """Cost of buying both legs back now at the ASK (PremiumValue)."""
        quantity = self.current_straddle_quantity()
        if quantity.value is None:
            return quantity
        call_quote, put_quote = self.current_leg_quotes()
        return combined_buyback_cost(
            call_quote,
            put_quote,
            quantity.value,
            self.atm_selection.pair.call.multiplier,
            self.clock.timestamp_ns(),
            self.max_quote_age_seconds,
        )

    def check_risk_and_exit(self):
        """
        Every leg quote while ENTERED: stop-loss / take-profit check. Also feeds the
        stale-data watchdog (unvalued_since_ns). Returns the ExitDecision.
        """
        decision = evaluate_exit(self.risk_levels_usdt, self.value_buyback())
        previous = self.last_exit_decision
        self.last_exit_decision = decision

        if decision.buyback_cost is None:
            if self.unvalued_since_ns is None:
                self.unvalued_since_ns = self.clock.timestamp_ns()
            reason_changed = previous is None or previous.reason != decision.reason
            if reason_changed:
                self.log.warning(f"Risk check on hold: {decision.reason}")
            return decision

        self.unvalued_since_ns = None
        if self.monitoring_alert_active:
            self.monitoring_alert_active = False
            self.log.info("Straddle can be valued again: monitoring restored")
        if decision.action == HOLD:
            return decision

        self.start_exit(decision)
        return decision

    def start_exit(self, decision):
        """STOP_LOSS / TAKE_PROFIT: switch to EXITING first, then send the first round."""
        if not self.set_state(self.EXITING, f"{decision.action}: {decision.reason}"):
            return
        self.exit_reason = decision.action
        self.log.warning(f"{decision.action} TRIGGERED: {decision.reason}")
        self.continue_closing()

    def check_exit_progress(self):
        """
        After each exit order event: wait while the round works; CLOSED only when both
        positions are flat and no orders are open; otherwise the next round.
        """
        if self.round_is_working():
            return
        call_position, put_position = self.current_leg_positions()
        both_flat = call_position.side == FLAT and put_position.side == FLAT
        if both_flat and self.open_order_count_on_legs() == 0:
            self.cancel_order_timeout()
            exit_cost = self.actual_exit_cost()
            result = self.entry_premium - exit_cost
            self.set_state(self.CLOSED, f"{self.exit_reason}: both positions confirmed flat")
            self.log.info(
                f"STRADDLE CLOSED ({self.exit_reason}): entry premium {self.entry_premium} USDT, "
                f"buyback cost {exit_cost} USDT, result {result:+} USDT (before fees)"
            )
            return
        if self.exit_attempts > 0:
            self.log.warning(
                f"Exit round {self.exit_attempts} finished but positions remain: "
                f"{describe_leg_position('CALL', call_position)}; {describe_leg_position('PUT', put_position)}"
            )
        self.continue_closing()

    def actual_exit_cost(self):
        """USDT actually paid to buy back, summed over every exit fill."""
        multiplier = self.atm_selection.pair.call.multiplier
        total = Decimal("0")
        for tracker in self.all_exit_trackers:
            average = tracker.average_price()
            if average is not None:
                total += average * tracker.filled_quantity * multiplier
        return total

    # ----------------------------------------------------------------------------------
    # Milestone 10: reduce-only rounds (shared by exits and recovery)
    # ----------------------------------------------------------------------------------
    def continue_closing(self):
        """
        Send the next reduce-only round for the current activity:
          EXITING    -> close everything still short on both legs
          RECOVERING -> advance_recovery() (closes only unmatched quantity)
        """
        if self.entry_state == self.RECOVERING:
            self.advance_recovery()
            return
        if self.entry_state != self.EXITING:
            return

        call_id, put_id = self.leg_instrument_ids()
        call_position, put_position = self.current_leg_positions()
        targets = []
        if call_position.side == SHORT:
            targets.append(("CALL", call_id, call_position.quantity))
        if put_position.side == SHORT:
            targets.append(("PUT", put_id, put_position.quantity))

        if len(targets) == 0:
            self.check_exit_progress()
            return
        result = self.send_reduce_only_round(EXIT, targets)
        if result == ROUND_LIMIT_REACHED:
            self.enter_error(f"exit not complete after {self.exit_attempts} rounds")

    def build_reduce_only_order(self, purpose, leg_name, instrument_id, quantity, ask_price):
        """
        Create one BUY LIMIT IOC reduce-only order (exit or recovery buy-back).
        Price = ask + exit_price_offset_ticks, rounded UP to the tick. reduce_only=True:
        Bybit only reduces the position, so it can never open a new long.
        Returns: (order, description).
        """
        instrument = self.cache.instrument(instrument_id)
        tick = instrument.price_increment.as_decimal()
        raw_price = ask_price + tick * self.exit_price_offset_ticks
        limit_price = instrument.make_price(round_up_to_tick(raw_price, tick))
        order_quantity = instrument.make_qty(quantity)

        if purpose == EXIT:
            tag = "STRADDLE_EXIT"
            detail = f"exit {leg_name}, {self.exit_reason}, attempt {self.exit_attempts}"
        else:
            tag = "STRADDLE_RECOVERY"
            detail = f"recovery {leg_name}, attempt {self.recovery_attempts}"

        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=order_quantity,
            price=limit_price,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            tags=[tag, leg_name],
        )
        description = f"BUY {order_quantity} {instrument_id} @ {limit_price} IOC reduce-only ({detail})"
        return order, description

    def send_reduce_only_round(self, purpose, targets):
        """
        Send one round of reduce-only BUY orders.

        Parameters: purpose (EXIT or RECOVERY); targets (list of (leg name, InstrumentId,
        quantity)), always derived from the ACTUAL positions by the caller.
        Returns: ROUND_SENT, ROUND_WAITING_FOR_QUOTE (a leg has no ask; retried on the
        next quote) or ROUND_LIMIT_REACHED (max_exit_attempts rounds already sent).
        """
        call_quote, put_quote = self.current_leg_quotes()
        quotes = {"CALL": call_quote, "PUT": put_quote}
        for leg_name, _, _ in targets:
            quote = quotes[leg_name]
            if quote is None or not quote.has_ask():
                if not self.waiting_for_quote:
                    self.log.warning(f"{purpose} waiting: {leg_name} has no ask to buy back at")
                self.waiting_for_quote = True
                return ROUND_WAITING_FOR_QUOTE
        self.waiting_for_quote = False

        if purpose == EXIT:
            if self.exit_attempts >= self.max_exit_attempts:
                return ROUND_LIMIT_REACHED
            self.exit_attempts += 1
        else:
            if self.recovery_attempts >= self.max_exit_attempts:
                return ROUND_LIMIT_REACHED
            self.recovery_attempts += 1

        self.round_trackers = []
        self.cancel_requested = False
        for leg_name, instrument_id, quantity in targets:
            order, description = self.build_reduce_only_order(
                purpose, leg_name, instrument_id, quantity, quotes[leg_name].ask
            )
            tracker = LegOrderTracker(leg_name, instrument_id, quantity)
            self.trackers_by_client_order_id[str(order.client_order_id)] = tracker
            self.round_trackers.append(tracker)
            if purpose == EXIT:
                self.all_exit_trackers.append(tracker)
            else:
                self.all_recovery_trackers.append(tracker)
            if self.order_gateway.submit(order, description) == SUBMITTED:
                tracker.mark_submitted(order.client_order_id)
        self.arm_order_timeout()
        return ROUND_SENT

    def round_is_working(self):
        """True while any order of the current reduce-only round can still fill."""
        for tracker in self.round_trackers:
            if not tracker.is_terminal():
                return True
        return False

    # ----------------------------------------------------------------------------------
    # Milestone 10: recovery from an incomplete entry
    # ----------------------------------------------------------------------------------
    def start_recovery(self, reason):
        """
        Enter RECOVERING: cancel any entry order still working, then repair.

        Assignment question 10 (one leg executed, the other not): the unmatched part of
        the filled leg is a naked option. Recovery buys back exactly the unmatched
        quantity with reduce-only orders. What remains is a smaller balanced straddle
        (back to ENTERED) or nothing (CLOSED).
        """
        if not self.set_state(self.RECOVERING, reason):
            return
        self.log.warning(f"RECOVERY started: {reason}")
        self.cancel_working_orders()
        self.advance_recovery()

    def entry_orders_working(self):
        """True while either entry order can still fill."""
        for tracker in (self.call_entry, self.put_entry):
            if tracker is not None and not tracker.is_terminal():
                return True
        return False

    def advance_recovery(self):
        """
        One recovery step, called after every relevant event.

        1. Wait while any entry order or recovery order is still working.
        2. Read the ACTUAL positions (the Cache, kept in line with Bybit). A LONG leg is
           something a short straddle can never produce -> ERROR.
        3. plan_recovery(): matched quantity and what to close on each leg.
        4. Nothing to close: KEEP_STRADDLE -> ENTERED with the matched quantity;
           GO_FLAT -> CLOSED (once no orders are open). Otherwise send a round.
        """
        if self.entry_state != self.RECOVERING:
            return
        if self.entry_orders_working() or self.round_is_working():
            return

        call_position, put_position = self.current_leg_positions()
        if call_position.side == LONG or put_position.side == LONG:
            self.enter_error("a LONG option position appeared during recovery")
            return

        contract = self.atm_selection.pair.call
        plan = plan_recovery(call_position.quantity, put_position.quantity, contract.min_quantity, contract.lot_step)
        self.log.info(describe_plan(plan))
        if plan.problem is not None:
            self.enter_error(plan.problem)
            return

        if not plan.needs_orders():
            if self.open_order_count_on_legs() > 0:
                return
            self.cancel_order_timeout()
            if plan.outcome == KEEP_STRADDLE:
                self.set_state(self.ENTERED, f"recovery done: balanced straddle of {plan.matched_quantity} remains")
                self.activate_straddle(plan.matched_quantity)
            else:
                self.set_state(self.CLOSED, "recovery done: no matched straddle, both legs flat")
                self.log.info("RECOVERY complete: both legs flat, nothing remains open")
            return

        call_id, put_id = self.leg_instrument_ids()
        targets = []
        if plan.close_call > 0:
            targets.append(("CALL", call_id, plan.close_call))
        if plan.close_put > 0:
            targets.append(("PUT", put_id, plan.close_put))
        result = self.send_reduce_only_round(RECOVERY, targets)
        if result == ROUND_LIMIT_REACHED:
            self.enter_error(f"recovery not complete after {self.recovery_attempts} rounds")

    # ----------------------------------------------------------------------------------
    # Milestone 10: order timeouts, cancels and the ERROR state
    # ----------------------------------------------------------------------------------
    def arm_order_timeout(self):
        """(Re)start the one-off order timeout alert."""
        alert_time = self.clock.utc_now() + datetime.timedelta(seconds=self.order_timeout_seconds)
        self.clock.set_time_alert(
            name=self.ORDER_TIMEOUT_NAME,
            alert_time=alert_time,
            callback=self.on_order_timeout,
            override=True,
        )

    def cancel_order_timeout(self):
        if self.ORDER_TIMEOUT_NAME in self.clock.timer_names:
            self.clock.cancel_timer(self.ORDER_TIMEOUT_NAME)

    def working_trackers(self):
        """Our orders that have not reached a final status yet."""
        working = []
        for tracker in (self.call_entry, self.put_entry):
            if tracker is not None and tracker.client_order_id is not None and not tracker.is_terminal():
                working.append(tracker)
        for tracker in self.round_trackers:
            if tracker.client_order_id is not None and not tracker.is_terminal():
                working.append(tracker)
        return working

    def cancel_working_orders(self):
        """
        Ask NautilusTrader to cancel every order of ours that is still open.
        Cancels reduce risk, so they do not go through the order gateway (and in dry run
        no order exists to cancel). Returns: number of cancel requests sent.
        """
        sent = 0
        for tracker in self.working_trackers():
            order = self.cache.order(ClientOrderId(tracker.client_order_id))
            if order is None or not order.is_open:
                continue
            self.log.warning(f"Canceling working {tracker.leg_name} order {tracker.client_order_id}")
            self.cancel_order(order)
            sent += 1
        return sent

    def on_order_timeout(self, event):
        """
        Our orders did not reach a final status in time (lost connection, exchange
        problem...).
          ENTRY_SUBMITTED        -> start_recovery(): cancel the entry orders, then
                                    repair from the actual positions.
          first timeout in a round -> request cancels, wait another period.
          second timeout         -> ERROR: the strategy can no longer be sure what is
                                    open, so it stops sending orders.
        """
        working = self.working_trackers()
        if len(working) == 0:
            return

        state = self.entry_state
        names = ", ".join(tracker.leg_name for tracker in working)
        if state == self.ENTRY_SUBMITTED:
            self.start_recovery(f"entry orders without final status after {self.order_timeout_seconds} s ({names})")
            self.cancel_requested = True
            self.arm_order_timeout()
            return
        if state not in (self.RECOVERING, self.EXITING):
            return
        if not self.cancel_requested:
            self.log.warning(f"Orders without final status after {self.order_timeout_seconds} s ({names}): canceling")
            self.cancel_requested = True
            self.cancel_working_orders()
            self.arm_order_timeout()
            return
        self.enter_error(f"orders still without final status after cancel requests ({names})")

    def enter_error(self, reason):
        """
        Go to ERROR: stop all automatic trading and ask for a human.

        In ERROR the strategy sends no more orders (every order path depends on a
        non-final state), keeps logging the positions on every status line, and says
        what to do. Acting automatically on a situation it does not understand could
        make things worse; a person must look at the account.
        """
        if self.entry_state == self.ERROR:
            return
        self.cancel_order_timeout()
        self.set_state(self.ERROR, reason)
        self.log.error(f"ERROR: {reason}")
        self.log.error("MANUAL ACTION REQUIRED: check and, if needed, close the positions on Bybit Testnet")
        call_position, put_position = self.current_leg_positions()
        if call_position is not None:
            self.log.error(f"  {describe_leg_position('CALL', call_position)}")
            self.log.error(f"  {describe_leg_position('PUT', put_position)}")

    # ----------------------------------------------------------------------------------
    # Monitoring
    # ----------------------------------------------------------------------------------
    def describe_status(self):
        """One-line summary of where the strategy is."""
        state = self.entry_state
        if state == self.EXISTING_POSITIONS_FOUND:
            return f"[{state}] {len(self.existing_positions)} existing BTC option positions; new entries blocked"
        if state == self.SELECTING:
            if self.reference_instrument_id is not None:
                return f"[{state}] {len(self.straddle_pairs)} pairs known, waiting for the reference price"
            return f"[{state}] {len(self.straddle_pairs)} pairs known"
        if self.atm_selection is None or not self.atm_selection.is_valid():
            problem = "no selection"
            if self.atm_selection is not None:
                problem = self.atm_selection.problem
            return f"[{state}] ATM selection failed: {problem}"
        pair = self.atm_selection.pair
        updates = sum(self.quote_updates_by_leg.values())
        return (
            f"[{state}] ATM strike {pair.strike} (reference {self.atm_selection.reference_price}), "
            f"{updates} leg quote updates"
        )

    def check_stale_data(self):
        """
        Stale-data watchdog (status timer, ENTERED only). If the straddle has not been
        valuable for longer than max_unvalued_seconds (no ask, quotes stale: e.g. the
        market data connection dropped), log an error on every status line. No order is
        sent: without prices there is nothing sensible to trade at. Monitoring resumes
        automatically when valid quotes return (NautilusTrader reconnects the WebSocket).
        Returns: seconds without a valuation (0 if valued).
        """
        if self.entry_state != self.ENTERED or self.unvalued_since_ns is None:
            return 0
        seconds = (self.clock.timestamp_ns() - self.unvalued_since_ns) / 1_000_000_000
        if seconds > self.max_unvalued_seconds:
            self.monitoring_alert_active = True
            reason = "unknown"
            if self.last_exit_decision is not None:
                reason = self.last_exit_decision.reason
            self.log.error(f"POSITION NOT MONITORED for {seconds:.0f} s: {reason}")
        return seconds

    def log_positions_and_risk(self):
        """Positions, risk valuation and order trackers, for states with positions."""
        call_position, put_position = self.current_leg_positions()
        if call_position is None:
            return
        self.log.info(f"  {describe_leg_position('CALL', call_position)}")
        self.log.info(f"  {describe_leg_position('PUT', put_position)}")
        if self.entry_state == self.ENTERED and self.last_exit_decision is not None:
            self.log.info(f"  Risk: {self.last_exit_decision.reason}")
        for tracker in self.all_recovery_trackers:
            self.log.info(f"  recovery {tracker.describe()}")
        for tracker in self.all_exit_trackers:
            self.log.info(f"  exit {tracker.describe()}")

    def on_status_timer(self, event):
        """Every status_log_interval_seconds: one status line plus details for the state."""
        self.status_updates_logged += 1
        self.log.info(f"Status #{self.status_updates_logged}: {self.describe_status()}")
        state = self.entry_state

        if state == self.EXISTING_POSITIONS_FOUND:
            for leg in self.existing_positions:
                self.log.info(f"  {describe_leg_position('EXISTING', leg)}")
            return
        if self.call_instrument_id is None:
            return

        now_ns = self.clock.timestamp_ns()
        call_quote, put_quote = self.current_leg_quotes()
        self.log.info(f"  CALL {self.call_instrument_id}: {describe_quote(call_quote, now_ns)}")
        self.log.info(f"  PUT  {self.put_instrument_id}: {describe_quote(put_quote, now_ns)}")

        if state == self.WAITING_FOR_ENTRY:
            report = self.evaluate_entry_conditions()
            if self.quantity_problem is not None:
                self.log.info(f"  Entry check: FAIL - invalid order quantity: {self.quantity_problem}")
            elif report is not None:
                for line in describe_report(report):
                    self.log.info(f"  {line}")
            return

        if self.call_entry is not None:
            self.log.info(f"  entry {self.call_entry.describe()}")
            self.log.info(f"  entry {self.put_entry.describe()}")
        self.log_positions_and_risk()

        if state == self.ENTERED:
            problems = self.check_position_health()
            if len(problems) == 0:
                self.log.info("  Positions verified: match the straddle size")
            self.check_stale_data()
        if state == self.ERROR:
            self.log.error("  ERROR state: no automatic trading; manual action required")
