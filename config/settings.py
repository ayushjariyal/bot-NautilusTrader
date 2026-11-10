"""
Application settings, read from environment variables (optionally from a local .env file).

Why this file exists
--------------------
Three safety rules from the project plan are enforced here, before NautilusTrader starts:

  1. TESTNET only. NautilusTrader's Bybit adapter silently uses MAINNET when no
     environment is given (adapters/bybit/config.py: `environment or MAINNET`).
     So BYBIT_ENVIRONMENT must be set, and set to exactly TESTNET, or we refuse to start.
  2. DRY_RUN defaults to true. In dry-run mode the strategy never submits orders.
  3. ENABLE_ORDER_SUBMISSION defaults to false. Orders are sent only when DRY_RUN is
     false AND ENABLE_ORDER_SUBMISSION is true (and the environment is TESTNET).

  4. API credentials are optional. Without them the app runs market-data only (no
     execution client, so no order is possible). Enabling order submission without
     credentials is an error. The secret is never printed: it is excluded from the
     settings' repr and only a masked form of the key is ever logged.

Environment variables
---------------------
  BYBIT_ENVIRONMENT         required, must be "TESTNET"
  DRY_RUN                   optional, default "true"
  ENABLE_ORDER_SUBMISSION   optional, default "false"
  BYBIT_TESTNET_API_KEY     optional (Milestone 3B), set together with the secret
  BYBIT_TESTNET_API_SECRET  optional (Milestone 3B), set together with the key

Who uses this file
------------------
main.py and the scripts/ call load_settings() first.
config/bybit_clients.py turns the result into NautilusTrader client configs.
"""

import os
from dataclasses import dataclass
from dataclasses import field


# The only environment this project will ever connect to.
ALLOWED_ENVIRONMENT = "TESTNET"

# Words accepted for true/false settings. Anything else is an error, so a typo such as
# "ture" can never silently switch a safety setting.
TRUE_WORDS = ("true", "1", "yes", "on")
FALSE_WORDS = ("false", "0", "no", "off")

# Default location of the optional .env file: the project root (next to main.py).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOTENV_PATH = os.path.join(PROJECT_ROOT, ".env")


class SettingsError(Exception):
    """Raised when a setting is missing or unsafe. main.py prints it and exits."""


@dataclass(frozen=True)
class AppSettings:
    """
    Validated settings. frozen=True: values cannot change after start-up.

    Fields
    ------
    bybit_environment : str
        Always "TESTNET" (load_settings refuses anything else).
    dry_run : bool
        True = calculate and log decisions, but never send an order.
    enable_order_submission : bool
        Must be True (together with dry_run False) before any order is sent.
    api_key, api_secret : str or None
        Bybit TESTNET API credentials, or None when not configured.
        repr=False keeps them out of printed/logged representations of this object.
    """

    bybit_environment: str
    dry_run: bool
    enable_order_submission: bool
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)

    def has_credentials(self):
        """
        True when both the API key and secret are present.
        Decides whether main.py adds the Bybit execution client.
        """
        if not self.api_key:
            return False
        if not self.api_secret:
            return False
        return True

    def masked_api_key(self):
        """
        The API key with all but its last 4 characters hidden, e.g. "****Xy9Q".
        Safe to log; lets you check WHICH key is in use without revealing it.
        """
        if not self.api_key:
            return "not set"
        if len(self.api_key) <= 4:
            return "****"
        return "****" + self.api_key[-4:]

    def orders_allowed(self):
        """
        True only when real Testnet orders may be sent.

        Dry-run always wins: if dry_run is True, this returns False no matter what
        enable_order_submission says. Used by the order gateway (Milestone 7).
        """
        if self.dry_run:
            return False
        if not self.enable_order_submission:
            return False
        if self.bybit_environment != ALLOWED_ENVIRONMENT:
            return False
        return True

    def describe_safety(self):
        """
        Human-readable summary of the safety settings, one line each.
        main.py logs these lines at start-up so every run states its mode clearly.
        """
        if self.orders_allowed():
            mode = "LIVE TESTNET ORDERS ENABLED"
        elif self.dry_run:
            mode = "DRY RUN (orders are logged, never sent)"
        else:
            mode = "ORDERS DISABLED (ENABLE_ORDER_SUBMISSION is false)"

        if self.has_credentials():
            credentials = f"set (key {self.masked_api_key()}): execution client enabled"
        else:
            credentials = "not set: market data only, no execution client"

        return [
            f"Bybit environment: {self.bybit_environment}",
            f"DRY_RUN={self.dry_run}  ENABLE_ORDER_SUBMISSION={self.enable_order_submission}",
            f"Order mode: {mode}",
            f"API credentials: {credentials}",
        ]


def load_dotenv_file(path, environ):
    """
    Copy KEY=VALUE lines from a .env file into `environ`.

    Variables that are already set in `environ` are NOT overwritten, so a value set in
    the terminal always wins over the file. A missing file is not an error (the file is
    optional). We use this small reader instead of an extra package because the format
    we need is simple.

    Parameters: path (str); environ (dict-like, usually os.environ).
    Returns: int, how many variables were added.
    """
    if not os.path.exists(path):
        return 0

    added_count = 0
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()

            # Skip blank lines and comments.
            if line == "" or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # Allow optional quotes: KEY="value" or KEY='value'.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            if key in environ:
                continue
            environ[key] = value
            added_count += 1

    return added_count


def parse_environment(raw_value):
    """
    Validate BYBIT_ENVIRONMENT.

    Parameters: raw_value (str or None) - the variable's text, None if it is not set.
    Returns: "TESTNET".
    Raises: SettingsError if missing, empty, or anything other than TESTNET.
    """
    if raw_value is None or raw_value.strip() == "":
        raise SettingsError(
            "BYBIT_ENVIRONMENT is not set. Set it to TESTNET (for example in the .env "
            "file). Refusing to start: the Bybit adapter would otherwise default to MAINNET."
        )

    normalised = raw_value.strip().upper()
    if normalised != ALLOWED_ENVIRONMENT:
        raise SettingsError(
            f"BYBIT_ENVIRONMENT={raw_value!r} is not allowed. This project only runs on "
            f"{ALLOWED_ENVIRONMENT}."
        )
    return normalised


def parse_bool(name, raw_value, default):
    """
    Turn a true/false environment variable into a bool.

    Parameters: name (str) - variable name, used in error messages;
    raw_value (str or None); default (bool) - used when the variable is not set.
    Returns: bool.
    Raises: SettingsError for unrecognised text.
    """
    if raw_value is None or raw_value.strip() == "":
        return default

    word = raw_value.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise SettingsError(f"{name}={raw_value!r} is not a valid true/false value.")


def load_settings(environ=None, dotenv_path=DEFAULT_DOTENV_PATH):
    """
    Read and validate all settings.

    Parameters
    ----------
    environ : dict-like or None
        Where to read variables from. None means the real os.environ. Tests pass a
        plain dict so they never depend on the machine's environment.
    dotenv_path : str or None
        .env file to read first. None skips the file (tests do this).

    Returns: AppSettings.
    Raises: SettingsError if anything is missing or unsafe.
    """
    if environ is None:
        environ = os.environ

    if dotenv_path is not None:
        load_dotenv_file(dotenv_path, environ)

    environment = parse_environment(environ.get("BYBIT_ENVIRONMENT"))
    dry_run = parse_bool("DRY_RUN", environ.get("DRY_RUN"), default=True)
    enable_orders = parse_bool(
        "ENABLE_ORDER_SUBMISSION", environ.get("ENABLE_ORDER_SUBMISSION"), default=False
    )
    api_key = parse_optional_secret(environ.get("BYBIT_TESTNET_API_KEY"))
    api_secret = parse_optional_secret(environ.get("BYBIT_TESTNET_API_SECRET"))

    settings = AppSettings(
        bybit_environment=environment,
        dry_run=dry_run,
        enable_order_submission=enable_orders,
        api_key=api_key,
        api_secret=api_secret,
    )
    validate_credentials(settings)
    return settings


def parse_optional_secret(raw_value):
    """
    Clean an optional credential: strip spaces; treat an empty value as "not set".

    Parameters: raw_value (str or None). Returns: str or None.
    """
    if raw_value is None:
        return None
    cleaned = raw_value.strip()
    if cleaned == "":
        return None
    return cleaned


def validate_credentials(settings):
    """
    Check the credentials make sense together with the safety switches.

    Rules:
      * key and secret must be set together (one without the other is a mistake);
      * order submission cannot be enabled without credentials.
    Raises SettingsError; the messages never include the credential values.
    """
    has_key = settings.api_key is not None
    has_secret = settings.api_secret is not None

    if has_key and not has_secret:
        raise SettingsError("BYBIT_TESTNET_API_KEY is set but BYBIT_TESTNET_API_SECRET is missing.")
    if has_secret and not has_key:
        raise SettingsError("BYBIT_TESTNET_API_SECRET is set but BYBIT_TESTNET_API_KEY is missing.")

    if settings.orders_allowed() and not settings.has_credentials():
        raise SettingsError(
            "Order submission is enabled (DRY_RUN=false, ENABLE_ORDER_SUBMISSION=true) "
            "but no Testnet API credentials are set."
        )
