"""
Integration test: real Bybit TESTNET public market data (no API keys, no orders).

Skipped by default. Run explicitly with:
    .venv\\Scripts\\python.exe -m pytest -m integration -v

It runs scripts/check_testnet_market_data.py in a separate process (NautilusTrader's
logger can only be initialised once per process, and a separate process also mirrors
how you run it by hand) and checks its PASS result.
"""

import os
import subprocess
import sys

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "check_testnet_market_data.py")


@pytest.mark.integration
def test_testnet_market_data_reaches_nautilus_trader():
    environment = dict(os.environ)
    environment["BYBIT_ENVIRONMENT"] = "TESTNET"

    result = subprocess.run(
        [sys.executable, SCRIPT, "--run-seconds", "15"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=150,
    )

    assert "RESULT: PASS" in result.stdout, result.stdout[-3000:] + result.stderr[-3000:]
    assert result.returncode == 0
