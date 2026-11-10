# BTC Options ATM Short Straddle (NautilusTrader 1.231.0 + Bybit Testnet)

An intern assignment, Levels 1 and 2: sell one at-the-money (ATM) BTC call and one ATM BTC
put with the same strike and expiry on **Bybit Testnet**, track the orders and positions,
and close both legs automatically at a **1% stop-loss** or **2% take-profit** on the
combined buyback cost (valued at the ask).

Detailed learning guides (one PDF per milestone, with the real code, line-by-line
explanations and interview preparation) are in `../learning-material/`. Guide 11 has the
complete execution flow, the answers to the assignment's ten walkthrough questions, and
the final version of every file.

## Status: what is verified

| Area | Status |
|---|---|
| Bybit Testnet public market data (instruments, quotes, greeks) | **Verified live** (no API keys needed) |
| Testnet-only guard, dry run, order-submission switch | **Verified live** and unit-tested |
| Option discovery, ATM selection, leg quotes, liquidity check | **Verified live** (the entry check correctly refuses Testnet's empty books) |
| Entry orders, fills, positions, stop-loss / take-profit exits, recovery, ERROR | **Verified in NautilusTrader's simulated exchange** (backtest engine) |
| Authenticated execution client | Built and tested offline; a fake key is correctly rejected by Testnet (HTTP 401) |
| Real Testnet orders | **Not yet verified**: needs API keys, test coins, and two-sided Testnet order books |

The application defaults to **dry run**: orders are logged, never sent.

## The strategy

1. **Discover** every BTC option on Bybit and pair calls with puts of the same expiry
   and strike (contracts expiring within 24 h are ignored).
2. **Select ATM**: nearest expiry; Bybit's per-expiry underlying reference price
   (`OptionGreeks.underlying_price`); the strike closest to it.
3. **Check liquidity** on both legs: bid and ask present (a 0 price means "no quote"),
   sizes cover the order, spread <= 0.5%, quote fresh (<= 10 s), and the initial
   ask-vs-bid gap below the 1% stop-loss (otherwise the stop-loss would fire at once).
4. **Enter**: SELL LIMIT IOC at the bid on both legs, 0.01 each, through a single order
   gateway (dry run: logged only).
5. **Track** every order event and both positions; verify them against what was filled.
6. **Level 2**: on every quote, buyback cost = (call ask + put ask) x quantity; exit at
   >= entry premium x 1.01 (stop-loss) or <= x 0.98 (take-profit) with BUY LIMIT IOC
   **reduce-only** orders; CLOSED only when both positions are confirmed flat.
7. **Robustness**: a state machine; recovery when only one leg (or part) fills; order
   timeouts; an ERROR state that stops trading; a stale-data watchdog; safe shutdown.

## Terminology

| Term | Meaning |
|---|---|
| Call / Put | Option that gains when BTC rises / falls (for the buyer) |
| Strike / Expiry | Fixed price in the contract / the date it ends (Bybit: 08:00 UTC) |
| ATM | The strike nearest the current BTC reference price |
| Premium | The option's price; the seller receives it |
| Short straddle | Sell an ATM call and an ATM put, same strike and expiry |
| Bid / Ask / Spread | Best buy price / best sell price / ask - bid |
| Buyback cost | What closing the short straddle costs now, at the ask |
| Reduce-only | An order that can only shrink a position, never open a new one |
| IOC | Immediate-Or-Cancel: fill what is available now, cancel the rest |

## Architecture

```text
main.py
  load_settings() -> TESTNET guard, DRY_RUN, ENABLE_ORDER_SUBMISSION, API keys (.env)
  build_node_config() -> Bybit data client (+ execution client if keys exist)
  TradingNode ---------------------------------------------------------------
    MessageBus | Cache | DataEngine | RiskEngine | ExecutionEngine | Portfolio
    Bybit data client  (REST: instruments; WebSocket: option tickers)
    Bybit exec client  (REST + private/trade WebSocket; only with API keys)
    Trader
      BTCStraddleStrategy (strategies/btc_straddle.py)
        option_chain -> instrument_selector -> market_data / liquidity_check
        order_gateway -> leg_tracker -> position_manager
        premium_calculator -> risk_manager -> recovery_planner
        straddle_state (state machine)
```

```text
SELECTING -> WAITING_FOR_ENTRY -> ENTRY_SUBMITTED -> ENTERED -> EXITING -> CLOSED
    |               |                  |               ^
    |               v                  v               |
    |        DRY_RUN_COMPLETE      RECOVERING ---------+----> CLOSED (nothing matched)
    v
SELECTION_FAILED / EXISTING_POSITIONS_FOUND          (ERROR from any non-final state)
```

Only `strategies/btc_straddle.py`, `main.py` and the `config/` and `scripts/` files use
NautilusTrader directly; every decision rule is plain Python and unit-tested.

## Project structure

| Path | Responsibility |
|---|---|
| `main.py` | Entry point: settings, node config, build, run, dispose |
| `config/settings.py` | Settings from env / `.env`; TESTNET-only guard; safety switches; credentials (never printed) |
| `config/bybit_clients.py` | Bybit data and execution client configs; final TESTNET check |
| `strategies/btc_straddle.py` | The strategy: wires everything to NautilusTrader events |
| `strategies/option_chain.py` | Contracts from instruments, filtering, call/put pairs, tick rounding |
| `strategies/instrument_selector.py` | Reference price validation and ATM selection |
| `strategies/liquidity_check.py` | Quantity validation and entry checks (incl. the stop-loss gap) |
| `strategies/premium_calculator.py` | Entry premium and ask-side buyback cost (USDT) |
| `strategies/risk_manager.py` | 1% / 2% levels and HOLD / STOP_LOSS / TAKE_PROFIT |
| `data/market_data.py` | LegQuote: clean view of a cached quote (0 = no quote) |
| `execution/order_gateway.py` | The only place orders are submitted; dry run logs instead |
| `execution/leg_tracker.py` | Per-order lifecycle, fills, average price; two-leg outcome |
| `execution/position_manager.py` | Net leg positions from the Cache and their verification |
| `execution/recovery_planner.py` | What to buy back after an incomplete entry |
| `execution/straddle_state.py` | States and allowed transitions |
| `scripts/verify_installation.py` | Environment check (offline) |
| `scripts/check_testnet_market_data.py` | Live public-data check (no keys) |
| `scripts/check_testnet_account.py` | Authenticated account check (needs keys; never orders) |
| `scripts/simulate_entry.py` | SIMULATED demos: dry-run, hold, stop-loss, take-profit, partial-exit, one-leg-denied |
| `tests/unit/` | 265 offline unit tests |
| `tests/integration/` | Real Testnet tests (opt-in) |
| `tests/factories.py` | Builds simulated CryptoOption objects |

## Installation (Windows, PowerShell)

```powershell
cd btc-options-straddle
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env
.\.venv\Scripts\python.exe scripts\verify_installation.py
```

NautilusTrader is pinned to **1.231.0** (version 2.0 changes the core API). For an
identical environment install `requirements-lock.txt`.

## Configuration

### Environment variables (`.env`; a variable set in the terminal wins)

| Variable | Default | Meaning |
|---|---|---|
| `BYBIT_ENVIRONMENT` | **required** | Must be `TESTNET`; anything else (or missing) stops the app before it connects (the Bybit adapter would otherwise default to MAINNET) |
| `DRY_RUN` | `true` | Log orders, never send them (always wins) |
| `ENABLE_ORDER_SUBMISSION` | `false` | Orders are sent only if `DRY_RUN=false` AND this is `true` |
| `BYBIT_TESTNET_API_KEY` / `BYBIT_TESTNET_API_SECRET` | none | Testnet keys (read + trade, **no withdrawal**); set both or neither |

| Credentials | DRY_RUN | ENABLE_ORDER_SUBMISSION | Result |
|---|---|---|---|
| not set | any | false | Market data only; no execution client; orders impossible |
| not set | false | true | Refused at start-up |
| set | true | any | Execution client connects; orders logged only |
| set | false | true | Orders sent to **Testnet** |

### Strategy settings (`BTCStraddleConfig`)

| Setting | Default | Meaning |
|---|---|---|
| `underlying` | `BTC` | Options underlying |
| `min_hours_to_expiry` | `24.0` | Ignore contracts expiring sooner |
| `max_atm_distance_pct` | `2.0` | Reject ATM if the nearest strike is further away |
| `reference_price_timeout_seconds` | `30` | Wait for the reference price |
| `order_quantity` | `"0.01"` | Quantity of each leg |
| `max_spread_pct` | `0.5` | Largest spread per leg for entry |
| `max_quote_age_seconds` | `10.0` | Older quotes are stale |
| `stop_loss_pct` / `take_profit_pct` | `1.0` / `2.0` | Level 2 thresholds |
| `exit_price_offset_ticks` | `2` | Buyback limit = ask + 2 ticks |
| `max_exit_attempts` | `5` | Reduce-only rounds (exit, recovery) before ERROR |
| `order_timeout_seconds` | `20` | Orders without a final status are canceled / recovered |
| `max_unvalued_seconds` | `60` | Alert when the open straddle cannot be valued |
| `submit_orders` | `False` | Set by main.py from the settings; False = dry run |
| `status_log_interval_seconds` | `10` | Status line frequency |

## Running

```powershell
.\.venv\Scripts\python.exe main.py --run-seconds 60   # stops by itself
.\.venv\Scripts\python.exe main.py                    # runs until Ctrl+C
```

Example (real Testnet, dry run, no keys):

```text
Order mode: DRY RUN (orders are logged, never sent)
API credentials: not set: market data only, no execution client
BybitInstrumentProvider: Loaded 646 instruments
Call/put pairs usable for a straddle: 289
Reference price: 73722.59709671
Nearest strike: 73750 (distance 27.40290329 USDT = 0.037%)
State: SELECTING -> WAITING_FOR_ENTRY (ATM strike 73750 selected)
Status #1: [WAITING_FOR_ENTRY] ATM strike 73750 (reference 73722.59709671), 22 leg quote updates
  CALL BTC-12NOV26-73750-C-USDT-OPTION.BYBIT: bid - | ask - | spread n/a | age 0.3 s
  PUT  BTC-12NOV26-73750-P-USDT-OPTION.BYBIT: bid - | ask - | spread n/a | age 0.3 s
  Entry check: FAIL (4 problems)
    - CALL: no bid (nobody to sell to)
    ...
```

### Simulated demos (no Bybit; made-up quotes; NautilusTrader's simulated exchange)

```powershell
.\.venv\Scripts\python.exe scripts\simulate_entry.py --mode dry-run
.\.venv\Scripts\python.exe scripts\simulate_entry.py --mode fills --scenario stop-loss
.\.venv\Scripts\python.exe scripts\simulate_entry.py --mode fills --scenario take-profit
.\.venv\Scripts\python.exe scripts\simulate_entry.py --mode fills --scenario partial-exit
.\.venv\Scripts\python.exe scripts\simulate_entry.py --mode fills --scenario one-leg-denied
```

| Scenario | Outcome |
|---|---|
| dry-run | Two `DRY RUN: would submit` lines; nothing sent |
| hold | ENTERED; premium 23.50 USDT; positions verified |
| stop-loss | Buyback 23.75 >= 23.735 -> CLOSED, result -0.25 USDT |
| take-profit | Buyback 22.50 <= 23.03 -> CLOSED, result +1.00 USDT |
| partial-exit | Exit needs 2 rounds -> CLOSED, result -0.50 USDT |
| one-leg-denied | (fault injection) put denied, call filled -> RECOVERING -> buy back call -> CLOSED |

Results match NautilusTrader's realized PnL of the closed positions.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                  # 265 offline unit tests
.\.venv\Scripts\python.exe -m pytest -m integration   # real Testnet (account test skips without keys)
```

## Testnet integration

1. Create a Testnet account at testnet.bybit.com (Unified Trading Account); get test USDT
   ("Request Test Coins" on the Assets page, if available).
2. Create an API key (system-generated, Read-Write, Unified Trading orders/positions,
   **no withdrawal**); put it in `.env`.
3. `scripts\check_testnet_account.py` should print `RESULT: PASS` and the balance.
4. To trade on Testnet: `DRY_RUN=false`, `ENABLE_ORDER_SUBMISSION=true`, then `main.py`.
   An entry happens only when both ATM legs have tight two-sided quotes.

## Known limitations

- Real Testnet orders are not yet verified (no keys / test coins; empty ATM books).
- Bybit Testnet option books are often empty (quotes of 0/0); the entry check then
  refuses to trade, by design.
- Fees are not included in the 1% / 2% thresholds (the assignment defines them on
  premiums; Testnet instruments report fee rates of 0).
- NautilusTrader 1.231.0's RiskEngine skips notional and free-balance checks for MARGIN
  accounts (Bybit options use one): `risk/engine.pyx`, `if account.is_margin_account:
  return True`. Price/quantity precision and min/max quantity are still checked per order.
- In ERROR, the strategy stops trading: an open position is then not protected by the
  stop-loss until a person acts.
- Positions are not closed automatically on shutdown; the next start detects them and
  opens nothing new.
- Harmless warnings inside NautilusTrader (pandas `Timestamp.utcnow` deprecation; an
  unawaited coroutine when the execution client is built in a test without running)
  are filtered in `pytest.ini`.

## Risk considerations

- A short straddle has **unlimited loss potential** if BTC moves far in either direction;
  the stop-loss is evaluated on quotes and executed with IOC orders, so in a fast or empty
  market the actual exit can be worse than the 1% level.
- A naked single leg (incomplete entry) is closed immediately by the recovery logic.
- This project never connects to Mainnet (two independent checks) and never uses real
  money. Never give API keys withdrawal permission; never commit `.env`.
