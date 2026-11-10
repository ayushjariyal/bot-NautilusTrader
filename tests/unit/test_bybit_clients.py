"""
Unit tests for config/bybit_clients.py: the Bybit data and execution client configs and
the final Testnet-only check on a finished TradingNodeConfig. No network is used; the
credentials below are fake strings.
"""

import pytest
from nautilus_trader.adapters.bybit import BybitDataClientConfig
from nautilus_trader.adapters.bybit import BybitEnvironment
from nautilus_trader.adapters.bybit import BybitExecClientConfig
from nautilus_trader.adapters.bybit import BybitProductType
from nautilus_trader.config import TradingNodeConfig

from config.bybit_clients import assert_testnet_only
from config.bybit_clients import build_data_client_config
from config.bybit_clients import build_exec_client_config
from config.bybit_clients import to_bybit_environment
from config.settings import AppSettings
from config.settings import SettingsError


SAFE_SETTINGS = AppSettings("TESTNET", dry_run=True, enable_order_submission=False)


def test_to_bybit_environment_returns_testnet():
    assert to_bybit_environment(SAFE_SETTINGS) == BybitEnvironment.TESTNET


def test_to_bybit_environment_refuses_other_values():
    with pytest.raises(SettingsError):
        to_bybit_environment(AppSettings("MAINNET", True, False))


def test_data_client_config_is_testnet_options_without_keys():
    config = build_data_client_config(SAFE_SETTINGS)
    assert config.environment == BybitEnvironment.TESTNET
    assert config.product_types == [BybitProductType.OPTION]
    assert config.instrument_provider.load_all is True
    assert config.api_key is None
    assert config.api_secret is None


def test_assert_testnet_only_accepts_our_node_config():
    node_config = TradingNodeConfig(data_clients={"BYBIT": build_data_client_config(SAFE_SETTINGS)})
    assert_testnet_only(node_config)


def test_assert_testnet_only_refuses_client_without_environment():
    # The adapter treats a missing environment as MAINNET, so we must refuse it.
    node_config = TradingNodeConfig(data_clients={"BYBIT": BybitDataClientConfig()})
    with pytest.raises(SettingsError, match="not TESTNET"):
        assert_testnet_only(node_config)


@pytest.mark.parametrize("environment", [BybitEnvironment.MAINNET, BybitEnvironment.DEMO])
def test_assert_testnet_only_refuses_mainnet_and_demo_data_clients(environment):
    node_config = TradingNodeConfig(data_clients={"BYBIT": BybitDataClientConfig(environment=environment)})
    with pytest.raises(SettingsError):
        assert_testnet_only(node_config)


def test_assert_testnet_only_also_checks_execution_clients():
    node_config = TradingNodeConfig(
        data_clients={"BYBIT": build_data_client_config(SAFE_SETTINGS)},
        exec_clients={"BYBIT": BybitExecClientConfig(environment=BybitEnvironment.MAINNET)},
    )
    with pytest.raises(SettingsError, match="execution client"):
        assert_testnet_only(node_config)


# --------------------------------------------------------------------------------------
# Execution client (Milestone 3B)
# --------------------------------------------------------------------------------------
SETTINGS_WITH_KEYS = AppSettings(
    "TESTNET", dry_run=True, enable_order_submission=False, api_key="TESTKEY1234abcd", api_secret="TESTSECRET"
)


def test_exec_client_config_is_testnet_options_with_keys():
    config = build_exec_client_config(SETTINGS_WITH_KEYS)
    assert config.environment == BybitEnvironment.TESTNET
    assert config.product_types == [BybitProductType.OPTION]
    assert config.api_key == "TESTKEY1234abcd"
    assert config.api_secret == "TESTSECRET"


def test_exec_client_config_requires_credentials():
    with pytest.raises(SettingsError, match="no Testnet API credentials"):
        build_exec_client_config(SAFE_SETTINGS)


def test_assert_testnet_only_accepts_both_of_our_clients():
    node_config = TradingNodeConfig(
        data_clients={"BYBIT": build_data_client_config(SETTINGS_WITH_KEYS)},
        exec_clients={"BYBIT": build_exec_client_config(SETTINGS_WITH_KEYS)},
    )
    assert_testnet_only(node_config)
