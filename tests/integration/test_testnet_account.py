"""
Integration test: the authenticated Bybit TESTNET execution connection.

Skipped by default (run with: pytest -m integration), and ALSO skipped when no Testnet
API credentials are configured. It runs scripts/check_testnet_account.py, which reads
the account, positions and open orders and never places an order.
"""

import os
import subprocess
import sys

import pytest

from config.settings import DEFAULT_DOTENV_PATH
from config.settings import load_dotenv_file


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "check_testnet_account.py")


def credentials_configured():
    """True if both credentials are available from the environment or the .env file."""
    environment = dict(os.environ)
    load_dotenv_file(DEFAULT_DOTENV_PATH, environment)
    key = environment.get("BYBIT_TESTNET_API_KEY", "").strip()
    secret = environment.get("BYBIT_TESTNET_API_SECRET", "").strip()
    return key != "" and secret != ""


@pytest.mark.integration
def test_testnet_account_connection():
    if not credentials_configured():
        pytest.skip("No Bybit Testnet API credentials configured in .env")

    result = subprocess.run(
        [sys.executable, SCRIPT, "--run-seconds", "20"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=200,
    )

    assert "RESULT: PASS" in result.stdout, result.stdout[-3000:]
    assert result.returncode == 0
