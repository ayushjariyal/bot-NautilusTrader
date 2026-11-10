"""
Unit tests for the TradingNode setup (main.py) and the strategy skeleton.

Everything here runs offline. Since Milestone 3A the node has a Bybit data client,
but these tests only BUILD the node; a connection would only open inside node.run(),
which is never called here. No orders exist anywhere.
"""

import msgspec
import pytest
from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit import BybitEnvironment

import main
from config.settings import AppSettings
from config.settings import SettingsError
from strategies.btc_straddle import BTCStraddleConfig
from strategies.btc_straddle import BTCStraddleStrategy


SAFE_SETTINGS = AppSettings("TESTNET", dry_run=True, enable_order_submission=False)


# --------------------------------------------------------------------------------------
# Strategy config
# --------------------------------------------------------------------------------------
def test_config_defaults():
    config = BTCStraddleConfig()
    assert config.underlying == "BTC"
    assert config.status_log_interval_seconds == 10


def test_config_is_immutable():
    # frozen=True: settings cannot be changed by accident while the strategy runs.
    config = BTCStraddleConfig()
    with pytest.raises((AttributeError, TypeError, msgspec.ValidationError)):
        config.underlying = "ETH"


def test_config_accepts_custom_values():
    config = BTCStraddleConfig(underlying="ETH", status_log_interval_seconds=30)
    assert config.underlying == "ETH"
    assert config.status_log_interval_seconds == 30


# --------------------------------------------------------------------------------------
# Strategy object
# --------------------------------------------------------------------------------------
def test_strategy_copies_settings_from_config():
    strategy = BTCStraddleStrategy(BTCStraddleConfig(status_log_interval_seconds=5))
    assert strategy.underlying_symbol == "BTC"
    assert strategy.status_log_interval_seconds == 5
    assert strategy.status_updates_logged == 0


def test_on_reset_clears_status_counter():
    strategy = BTCStraddleStrategy(BTCStraddleConfig())
    strategy.status_updates_logged = 7
    strategy.on_reset()
    assert strategy.status_updates_logged == 0


# --------------------------------------------------------------------------------------
# main.py helpers
# --------------------------------------------------------------------------------------
def test_parse_arguments_defaults():
    arguments = main.parse_arguments([])
    assert arguments.run_seconds == 0
    assert arguments.log_level == "INFO"


def test_parse_arguments_custom_values():
    arguments = main.parse_arguments(["--run-seconds", "15", "--log-level", "DEBUG"])
    assert arguments.run_seconds == 15
    assert arguments.log_level == "DEBUG"


def test_parse_arguments_rejects_unknown_log_level():
    with pytest.raises(SystemExit):
        main.parse_arguments(["--log-level", "LOUD"])


def test_node_config_has_testnet_data_client_and_no_execution_client():
    config = main.build_node_config("WARNING", SAFE_SETTINGS)
    assert str(config.trader_id) == main.TRADER_ID
    assert config.logging.log_level == "WARNING"
    # Without credentials: exactly one data client, on TESTNET, and no execution
    # client, so no order can be sent.
    assert list(config.data_clients.keys()) == [BYBIT]
    assert config.data_clients[BYBIT].environment == BybitEnvironment.TESTNET
    assert config.exec_clients == {}


def test_node_config_adds_testnet_execution_client_when_credentials_exist():
    settings = AppSettings("TESTNET", True, False, api_key="TESTKEY1234abcd", api_secret="TESTSECRET")
    config = main.build_node_config("WARNING", settings)
    assert list(config.exec_clients.keys()) == [BYBIT]
    assert config.exec_clients[BYBIT].environment == BybitEnvironment.TESTNET


def test_node_builds_offline_and_registers_strategy():
    # Builds the real TradingNode and its Bybit data client object, without running it
    # (so nothing connects), and checks the architecture summary.
    node = main.create_node(main.build_node_config("ERROR", SAFE_SETTINGS))
    try:
        strategy = main.build_strategy()
        node.trader.add_strategy(strategy)
        node.build()

        strategy_ids = [str(strategy_id) for strategy_id in node.trader.strategy_ids()]
        assert strategy_ids == ["BTCStraddleStrategy-000"]

        summary = "\n".join(main.describe_architecture(node))
        for component in ["MessageBus", "Cache", "DataEngine", "RiskEngine", "ExecEngine", "Portfolio", "Trader"]:
            assert component in summary
        assert "data clients = ['BYBIT']" in summary
        assert "exec clients = none" in summary
    finally:
        node.dispose()


def test_node_builds_execution_client_offline_with_fake_credentials():
    # build() creates the execution client object but does not connect (that only
    # happens in run()), so fake credentials are safe here and nothing is sent.
    # Note: the Bybit execution client schedules a background task when it is created.
    # Because this test never runs the node, Python reports that task as "never awaited"
    # (RuntimeWarning). That is expected here and does not happen in real runs.
    settings = AppSettings("TESTNET", True, False, api_key="TESTKEY1234abcd", api_secret="TESTSECRET")
    node = main.create_node(main.build_node_config("ERROR", settings))
    try:
        node.build()
        summary = "\n".join(main.describe_architecture(node))
        assert "exec clients = ['BYBIT']" in summary
    finally:
        node.dispose()


def test_main_refuses_to_start_on_settings_error(monkeypatch, capsys):
    # If settings are unsafe, main() must stop BEFORE creating a node (exit code 2).
    def refuse(*args, **kwargs):
        raise SettingsError("BYBIT_ENVIRONMENT is not set")

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("a node was created despite a settings error")

    monkeypatch.setattr(main, "load_settings", refuse)
    monkeypatch.setattr(main, "create_node", must_not_be_called)

    exit_code = main.main(["--run-seconds", "1"])
    assert exit_code == 2
    assert "BYBIT_ENVIRONMENT is not set" in capsys.readouterr().err


def test_periodic_exchange_checks_enabled_only_with_credentials():
    # Milestone 8: with an execution client, the engine re-checks orders and positions
    # against Bybit while running; without one there is nothing to check.
    with_keys = AppSettings("TESTNET", True, False, api_key="TESTKEY1234abcd", api_secret="TESTSECRET")
    config = main.build_node_config("WARNING", with_keys)
    assert config.exec_engine.reconciliation is True
    assert config.exec_engine.open_check_interval_secs == 10.0
    assert config.exec_engine.position_check_interval_secs == 30.0

    without_keys = main.build_node_config("WARNING", SAFE_SETTINGS)
    assert without_keys.exec_engine.position_check_interval_secs is None
