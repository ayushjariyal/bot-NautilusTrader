"""
The order gateway: the ONLY place in this project where an order is handed to
NautilusTrader for submission.

Why a gateway
-------------
The safety rules (guide 03A) say orders may be sent only when DRY_RUN=false AND
ENABLE_ORDER_SUBMISSION=true on TESTNET. Instead of repeating that check wherever an
order is created, every order goes through OrderGateway.submit(). The decision is made
once, at start-up (settings.orders_allowed(), passed to the strategy as the config flag
submit_orders), and the gateway enforces it for the whole run.

In dry-run (or disabled) mode the gateway NEVER calls the submit function. It only logs
"DRY RUN: would submit ..." and records the description, so you can see exactly what the
strategy would have done. A unit test proves the submit function is never called.

Plain Python: the gateway receives the strategy's submit_order method as a function, so
tests can pass a fake function and count calls.

Who uses this file: strategies/btc_straddle.py (enter_straddle; later exits and recovery).
"""


SUBMITTED = "SUBMITTED"
DRY_RUN = "DRY_RUN"


class OrderGateway:
    """
    Sends orders, or logs them in dry-run mode.

    Parameters
    ----------
    submit_function : callable(order)
        What actually submits an order: the strategy's self.submit_order.
    orders_allowed : bool
        True only when real Testnet orders are permitted (settings.orders_allowed()).
    log_info, log_warning : callable(str)
        Logging functions (the strategy's self.log.info / self.log.warning).
    """

    def __init__(self, submit_function, orders_allowed, log_info, log_warning):
        self.submit_function = submit_function
        self.orders_allowed = bool(orders_allowed)
        self.log_info = log_info
        self.log_warning = log_warning

        # Counters and a record of dry-run orders, for status lines, tests and the guide.
        self.submitted_count = 0
        self.dry_run_count = 0
        self.dry_run_descriptions = []

    def mode_text(self):
        """
        Short description of the gateway's mode, for the start-up log.

        The gateway does not know WHERE orders go: that is decided by the execution client
        the node was built with (Bybit Testnet in main.py, NautilusTrader's simulated
        exchange in the backtest demo). So the text only states whether orders are sent.
        """
        if self.orders_allowed:
            return "SUBMIT ENABLED: orders are sent to the node's execution client"
        return "DRY RUN: orders are logged, never submitted"

    def submit(self, order, description):
        """
        Submit `order`, or log it in dry-run mode.

        Parameters: order (a NautilusTrader Order from order_factory); description (str)
        human-readable summary, e.g. "SELL 0.01 BTC-...-C @ 1200 IOC (entry CALL)".
        Returns: SUBMITTED or DRY_RUN.
        """
        if not self.orders_allowed:
            self.dry_run_count += 1
            self.dry_run_descriptions.append(description)
            self.log_warning(f"DRY RUN: would submit {description} (client order id {order.client_order_id})")
            return DRY_RUN

        self.submitted_count += 1
        self.log_info(f"Submitting {description} (client order id {order.client_order_id})")
        self.submit_function(order)
        return SUBMITTED
