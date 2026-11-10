"""
Unit tests for config/settings.py: the Testnet-only guard and the two safety switches.

Every test passes its own dictionary as the environment and dotenv_path=None, so the
results never depend on the real machine's environment variables or .env file.
"""

import pytest

from config.settings import AppSettings
from config.settings import SettingsError
from config.settings import load_dotenv_file
from config.settings import load_settings
from config.settings import parse_bool
from config.settings import parse_environment


# --------------------------------------------------------------------------------------
# BYBIT_ENVIRONMENT guard
# --------------------------------------------------------------------------------------
def test_missing_environment_is_refused():
    with pytest.raises(SettingsError, match="not set"):
        parse_environment(None)


def test_empty_environment_is_refused():
    with pytest.raises(SettingsError, match="not set"):
        parse_environment("   ")


@pytest.mark.parametrize("value", ["MAINNET", "mainnet", "DEMO", "LIVE", "PROD", "TESTNETX"])
def test_non_testnet_environments_are_refused(value):
    with pytest.raises(SettingsError, match="not allowed"):
        parse_environment(value)


@pytest.mark.parametrize("value", ["TESTNET", "testnet", "  Testnet  "])
def test_testnet_is_accepted_case_insensitively(value):
    assert parse_environment(value) == "TESTNET"


# --------------------------------------------------------------------------------------
# true/false parsing
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_parse_bool_true_words(value):
    assert parse_bool("X", value, default=False) is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_parse_bool_false_words(value):
    assert parse_bool("X", value, default=True) is False


def test_parse_bool_uses_default_when_missing():
    assert parse_bool("X", None, default=True) is True
    assert parse_bool("X", "", default=False) is False


def test_parse_bool_rejects_typos():
    # A typo must never silently flip a safety switch.
    with pytest.raises(SettingsError):
        parse_bool("DRY_RUN", "ture", default=True)


# --------------------------------------------------------------------------------------
# load_settings defaults and the order permission rule
# --------------------------------------------------------------------------------------
def test_defaults_are_safe():
    settings = load_settings({"BYBIT_ENVIRONMENT": "TESTNET"}, dotenv_path=None)
    assert settings.bybit_environment == "TESTNET"
    assert settings.dry_run is True
    assert settings.enable_order_submission is False
    assert settings.orders_allowed() is False


def test_load_settings_refuses_missing_environment():
    with pytest.raises(SettingsError):
        load_settings({}, dotenv_path=None)


@pytest.mark.parametrize(
    "dry_run, enable_orders, expected",
    [
        (True, False, False),   # default: dry run
        (True, True, False),    # dry run ALWAYS wins
        (False, False, False),  # not dry run, but submission not enabled
        (False, True, True),    # the only combination that allows orders
    ],
)
def test_orders_allowed_truth_table(dry_run, enable_orders, expected):
    settings = AppSettings("TESTNET", dry_run, enable_orders)
    assert settings.orders_allowed() is expected


def test_orders_never_allowed_outside_testnet():
    # Defence in depth: even if an AppSettings were built by hand with another value.
    settings = AppSettings("MAINNET", False, True)
    assert settings.orders_allowed() is False


def test_describe_safety_states_the_mode():
    lines = AppSettings("TESTNET", True, False).describe_safety()
    assert "DRY RUN" in lines[2]
    lines = AppSettings("TESTNET", False, False).describe_safety()
    assert "ORDERS DISABLED" in lines[2]
    lines = AppSettings("TESTNET", False, True).describe_safety()
    assert "LIVE TESTNET ORDERS ENABLED" in lines[2]


# --------------------------------------------------------------------------------------
# .env reader
# --------------------------------------------------------------------------------------
def test_dotenv_file_is_read(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment line\n"
        "\n"
        "BYBIT_ENVIRONMENT=TESTNET\n"
        "DRY_RUN = \"false\"\n"
        "not a setting line\n",
        encoding="utf-8",
    )
    environ = {}
    added = load_dotenv_file(str(dotenv), environ)
    assert added == 2
    assert environ == {"BYBIT_ENVIRONMENT": "TESTNET", "DRY_RUN": "false"}


def test_dotenv_does_not_override_existing_variables(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DRY_RUN=false\n", encoding="utf-8")
    environ = {"DRY_RUN": "true"}
    load_dotenv_file(str(dotenv), environ)
    assert environ["DRY_RUN"] == "true"


def test_missing_dotenv_file_is_not_an_error(tmp_path):
    assert load_dotenv_file(str(tmp_path / "missing.env"), {}) == 0


# --------------------------------------------------------------------------------------
# API credentials (Milestone 3B)
# --------------------------------------------------------------------------------------
FAKE_KEY = "TESTKEY1234abcd"
FAKE_SECRET = "TESTSECRET-should-never-be-printed"


def test_credentials_are_optional():
    settings = load_settings({"BYBIT_ENVIRONMENT": "TESTNET"}, dotenv_path=None)
    assert settings.api_key is None
    assert settings.api_secret is None
    assert settings.has_credentials() is False


def test_credentials_are_read_and_stripped():
    settings = load_settings(
        {
            "BYBIT_ENVIRONMENT": "TESTNET",
            "BYBIT_TESTNET_API_KEY": f"  {FAKE_KEY} ",
            "BYBIT_TESTNET_API_SECRET": FAKE_SECRET,
        },
        dotenv_path=None,
    )
    assert settings.api_key == FAKE_KEY
    assert settings.has_credentials() is True


def test_empty_credentials_count_as_not_set():
    settings = load_settings(
        {"BYBIT_ENVIRONMENT": "TESTNET", "BYBIT_TESTNET_API_KEY": "", "BYBIT_TESTNET_API_SECRET": "  "},
        dotenv_path=None,
    )
    assert settings.has_credentials() is False


def test_key_without_secret_is_refused():
    with pytest.raises(SettingsError, match="SECRET is missing"):
        load_settings({"BYBIT_ENVIRONMENT": "TESTNET", "BYBIT_TESTNET_API_KEY": FAKE_KEY}, dotenv_path=None)


def test_secret_without_key_is_refused():
    with pytest.raises(SettingsError, match="API_KEY is missing"):
        load_settings({"BYBIT_ENVIRONMENT": "TESTNET", "BYBIT_TESTNET_API_SECRET": FAKE_SECRET}, dotenv_path=None)


def test_enabling_orders_without_credentials_is_refused():
    environ = {"BYBIT_ENVIRONMENT": "TESTNET", "DRY_RUN": "false", "ENABLE_ORDER_SUBMISSION": "true"}
    with pytest.raises(SettingsError, match="no Testnet API credentials"):
        load_settings(environ, dotenv_path=None)


def test_dry_run_without_credentials_is_fine():
    # Market-data-only dry run is the normal mode while API keys are being set up.
    settings = load_settings({"BYBIT_ENVIRONMENT": "TESTNET", "DRY_RUN": "true"}, dotenv_path=None)
    assert settings.orders_allowed() is False


def test_secret_never_appears_in_repr_or_safety_lines():
    settings = AppSettings("TESTNET", True, False, api_key=FAKE_KEY, api_secret=FAKE_SECRET)
    printed = repr(settings) + str(settings) + "\n".join(settings.describe_safety())
    assert FAKE_SECRET not in printed
    assert FAKE_KEY not in printed


def test_masked_api_key_shows_only_last_four_characters():
    settings = AppSettings("TESTNET", True, False, api_key=FAKE_KEY, api_secret=FAKE_SECRET)
    assert settings.masked_api_key() == "****abcd"
    assert AppSettings("TESTNET", True, False).masked_api_key() == "not set"


def test_describe_safety_reports_credential_mode():
    without = AppSettings("TESTNET", True, False).describe_safety()
    assert "market data only" in without[-1]
    with_keys = AppSettings("TESTNET", True, False, api_key=FAKE_KEY, api_secret=FAKE_SECRET).describe_safety()
    assert "execution client enabled" in with_keys[-1]
