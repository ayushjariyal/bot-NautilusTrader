"""
Unit tests for Milestone 1: the development environment.

These tests run on every `pytest` call. They guard against two easy mistakes:
  * running the tests with the wrong Python (for example the system Python 3.14 or 3.8
    instead of the project's .venv), and
  * accidentally upgrading NautilusTrader to a version whose API this project does not use.

No network connection is needed.
"""

import sys

import nautilus_trader


def test_python_version_is_supported_by_nautilus_trader():
    # NautilusTrader 1.231.0 publishes wheels for Python 3.12, 3.13 and 3.14 only.
    current = sys.version_info[:2]
    assert (3, 12) <= current < (3, 15)


def test_running_inside_virtual_environment():
    # Inside a venv, sys.prefix points at .venv while sys.base_prefix points at the
    # Python installation the venv was created from.
    assert sys.prefix != sys.base_prefix


def test_nautilus_trader_version_is_pinned_release():
    # Every API used in this project was checked against this exact release.
    assert nautilus_trader.__version__ == "1.231.0"


def test_bybit_adapter_exposes_option_product_type():
    # Options support is the foundation of the whole assignment, so check it exists.
    from nautilus_trader.adapters.bybit import BybitProductType

    assert hasattr(BybitProductType, "OPTION")


def test_bybit_adapter_exposes_testnet_environment():
    # The project refuses to run anywhere except TESTNET, so that value must exist.
    from nautilus_trader.adapters.bybit import BybitEnvironment

    assert hasattr(BybitEnvironment, "TESTNET")
