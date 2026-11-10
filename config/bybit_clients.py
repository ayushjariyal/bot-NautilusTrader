"""
Builds the NautilusTrader configs for the official Bybit adapter.

Milestone 3A: the market data client (public data, no API keys needed).
Milestone 3B: the execution client (needs Testnet API credentials). It sends orders,
receives order updates and fills over the private WebSocket, and reports the account
balance and positions used for reconciliation at start-up.

Two layers of protection against Mainnet
----------------------------------------
1. to_bybit_environment() only ever returns BybitEnvironment.TESTNET.
2. assert_testnet_only() re-checks the FINISHED TradingNodeConfig just before the node
   is built: every Bybit data and execution client must say TESTNET. This catches a
   mistake made anywhere else (for example a client config added by hand), because a
   client config without an environment means MAINNET inside the adapter.

Who uses this file: main.py and scripts/check_testnet_market_data.py.
"""

from nautilus_trader.adapters.bybit import BybitDataClientConfig
from nautilus_trader.adapters.bybit import BybitEnvironment
from nautilus_trader.adapters.bybit import BybitExecClientConfig
from nautilus_trader.adapters.bybit import BybitProductType
from nautilus_trader.config import InstrumentProviderConfig

from config.settings import ALLOWED_ENVIRONMENT
from config.settings import SettingsError


def to_bybit_environment(settings):
    """
    Convert our validated settings into the adapter's environment enum.

    Parameters: settings (AppSettings).
    Returns: BybitEnvironment.TESTNET.
    Raises: SettingsError for anything else (defence in depth; load_settings already
    refuses other values).
    """
    if settings.bybit_environment != ALLOWED_ENVIRONMENT:
        raise SettingsError(f"Refusing environment {settings.bybit_environment!r}")
    return BybitEnvironment.TESTNET


def build_data_client_config(settings):
    """
    Describe the Bybit market data connection.

    What each setting means:
      environment     TESTNET: REST https://api-testnet.bybit.com and public WebSocket
                      wss://stream-testnet.bybit.com/v5/public/option (verified in 1.231.0).
      product_types   [OPTION]: only option contracts are loaded and streamed. The adapter
                      default is LINEAR (perpetuals), which would load no options at all.
      instrument_provider load_all=True: when the client connects it downloads every
                      option contract definition over REST and puts them in the Cache.
                      Milestone 4 filters those down to BTC.
      api_key / api_secret  None: public market data needs no credentials. (If the
                      BYBIT_TESTNET_API_KEY variables exist, the adapter may pick them up
                      for its REST calls; that is harmless for public data.)

    Parameters: settings (AppSettings).
    Returns: BybitDataClientConfig. main.py puts it into TradingNodeConfig.data_clients.
    """
    return BybitDataClientConfig(
        environment=to_bybit_environment(settings),
        product_types=[BybitProductType.OPTION],
        instrument_provider=InstrumentProviderConfig(load_all=True),
        api_key=None,
        api_secret=None,
    )


def build_exec_client_config(settings):
    """
    Describe the Bybit execution (trading) connection.

    What each setting means:
      environment     TESTNET: REST https://api-testnet.bybit.com, private WebSocket
                      wss://stream-testnet.bybit.com/v5/private (order and position
                      updates) and trade WebSocket .../v5/trade (order submission).
                      URLs verified in 1.231.0.
      product_types   [OPTION]: the client trades and reconciles option orders/positions.
                      With derivatives only, the adapter's factory uses a MARGIN account
                      (Unified Trading Account) and NETTING positions (one per contract).
      instrument_provider load_all=True: the execution client also needs the contract
                      definitions to interpret order reports and fills.
      api_key / api_secret  from settings (the .env file). Never logged by our code.

    Parameters: settings (AppSettings) with credentials.
    Returns: BybitExecClientConfig. main.py puts it into TradingNodeConfig.exec_clients.
    Raises: SettingsError if credentials are missing.

    Note: building this config does NOT send orders. Orders are sent only by the
    strategy, through the order gateway, which checks settings.orders_allowed().
    """
    if not settings.has_credentials():
        raise SettingsError("Cannot build the Bybit execution client: no Testnet API credentials.")

    return BybitExecClientConfig(
        environment=to_bybit_environment(settings),
        product_types=[BybitProductType.OPTION],
        instrument_provider=InstrumentProviderConfig(load_all=True),
        api_key=settings.api_key,
        api_secret=settings.api_secret,
    )


def assert_testnet_only(node_config):
    """
    Final check on a finished TradingNodeConfig: every client must be TESTNET.

    Parameters: node_config (TradingNodeConfig).
    Returns: nothing if safe.
    Raises: SettingsError naming the first client that is not TESTNET (including clients
    whose environment is missing, which the adapter would treat as MAINNET).
    """
    all_clients = []
    for name, client_config in node_config.data_clients.items():
        all_clients.append(("data", name, client_config))
    for name, client_config in node_config.exec_clients.items():
        all_clients.append(("execution", name, client_config))

    for kind, name, client_config in all_clients:
        environment = getattr(client_config, "environment", None)
        if environment != BybitEnvironment.TESTNET:
            raise SettingsError(
                f"The {kind} client {name!r} is configured for {environment!r}, not TESTNET. "
                "Refusing to start."
            )
