"""
Entry checks: is the order quantity valid, and is the market for both legs good enough to
sell the straddle right now?

Why these checks exist
----------------------
A short straddle is entered by SELLING both legs, so each leg needs a buyer: a bid with
enough size. Later we will value the position at the ASK (the price we would pay to buy
it back, as the assignment requires). That creates a gap from the very first second:

    entry premium  = (call bid + put bid) x quantity x multiplier   (what we receive)
    buyback cost   = (call ask + put ask) x quantity x multiplier   (what closing costs)

    initial gap %  = (buyback cost - entry premium) / entry premium x 100

The stop-loss (Milestone 9) fires when the buyback cost is 1% above the entry premium.
If the initial gap is already 1% or more, the stop-loss would fire IMMEDIATELY after
entry, even if the market does not move at all. Example with made-up prices:

    call bid 3000 / ask 3030, put bid 2800 / ask 2828
    entry 5800, buyback 5858 -> gap = 58 / 5800 = 1.0%  -> instant stop-loss.

So entry is allowed only when the gap is below the stop-loss percentage, in addition to
each leg's own spread limit (max_spread_pct, default 0.5%).

Who uses this module: strategies/btc_straddle.py, before entering (Milestone 7) and in
the status line (Milestone 6).
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LiquidityReport:
    """
    Result of the entry checks for both legs.

    Fields
    ------
    ok (bool): True only if there are no problems at all.
    problems (tuple[str]): every failed check, prefixed with the leg ("CALL: ...").
    call_spread_pct, put_spread_pct (Decimal or None): each leg's spread in % of mid.
    entry_premium (Decimal or None): USDT received if both legs are sold at the bid.
    buyback_cost (Decimal or None): USDT needed to buy both back at the ask.
    initial_gap_pct (Decimal or None): (buyback - entry) / entry x 100.
    """

    ok: bool
    problems: tuple
    call_spread_pct: Decimal | None
    put_spread_pct: Decimal | None
    entry_premium: Decimal | None
    buyback_cost: Decimal | None
    initial_gap_pct: Decimal | None


def validate_order_quantity(quantity, contract):
    """
    Check an order quantity against one contract's rules (Bybit Testnet: min 0.01,
    step 0.01, max 10000, 2 decimals).

    Parameters: quantity (Decimal); contract (OptionContract).
    Returns: problem text (str), or None if the quantity is valid.
    """
    if quantity <= 0:
        return f"quantity {quantity} must be positive"
    if quantity < contract.min_quantity:
        return f"quantity {quantity} is below the minimum {contract.min_quantity}"
    if contract.max_quantity is not None and quantity > contract.max_quantity:
        return f"quantity {quantity} is above the maximum {contract.max_quantity}"
    if quantity % contract.lot_step != 0:
        return f"quantity {quantity} is not a multiple of the step {contract.lot_step}"

    # Count decimals: Decimal("0.010").normalize() -> 0.01 -> exponent -2 -> 2 decimals.
    exponent = quantity.normalize().as_tuple().exponent
    decimals = 0
    if exponent < 0:
        decimals = -exponent
    if decimals > contract.size_precision:
        return f"quantity {quantity} has more than {contract.size_precision} decimals"
    return None


def check_leg(leg_name, quote, quantity, max_spread_pct, max_quote_age_seconds, now_ns):
    """
    Check one leg's quote. Returns a list of problem texts (empty = this leg is fine).

    Parameters
    ----------
    leg_name : str            "CALL" or "PUT", used to prefix messages
    quote : LegQuote or None  latest quote of the leg
    quantity : Decimal        the order quantity we want to trade
    max_spread_pct : Decimal  largest acceptable spread (% of mid)
    max_quote_age_seconds : float  older quotes count as stale
    now_ns : int              current time (strategy clock)
    """
    if quote is None:
        return [f"{leg_name}: no quote received yet"]

    problems = []

    age = quote.age_seconds(now_ns)
    if age > max_quote_age_seconds:
        problems.append(f"{leg_name}: quote is stale ({age:.1f} s old, limit {max_quote_age_seconds} s)")

    if not quote.has_bid():
        problems.append(f"{leg_name}: no bid (nobody to sell to)")
    elif quote.bid_size < quantity:
        problems.append(f"{leg_name}: bid size {quote.bid_size} is smaller than the order quantity {quantity}")

    if not quote.has_ask():
        problems.append(f"{leg_name}: no ask (buyback cannot be valued)")
    elif quote.ask_size < quantity:
        problems.append(f"{leg_name}: ask size {quote.ask_size} is smaller than the order quantity {quantity}")

    if quote.is_two_sided():
        if quote.ask <= quote.bid:
            problems.append(f"{leg_name}: crossed or locked quote (bid {quote.bid} >= ask {quote.ask})")
        else:
            spread_pct = quote.spread_pct()
            if spread_pct > max_spread_pct:
                problems.append(f"{leg_name}: spread {spread_pct:.2f}% is above the limit {max_spread_pct}%")

    return problems


def check_entry_liquidity(
    call_quote,
    put_quote,
    quantity,
    multiplier,
    max_spread_pct,
    max_quote_age_seconds,
    stop_loss_pct,
    now_ns,
):
    """
    Run every entry check for the straddle and compute the premium figures.

    Parameters: call_quote, put_quote (LegQuote or None); quantity (Decimal, the same for
    both legs); multiplier (Decimal, the same for both legs, checked in Milestone 4);
    max_spread_pct, stop_loss_pct (Decimal or float, percent); max_quote_age_seconds
    (float); now_ns (int).
    Returns: LiquidityReport.
    """
    max_spread = Decimal(str(max_spread_pct))
    stop_loss = Decimal(str(stop_loss_pct))

    problems = []
    problems.extend(check_leg("CALL", call_quote, quantity, max_spread, max_quote_age_seconds, now_ns))
    problems.extend(check_leg("PUT", put_quote, quantity, max_spread, max_quote_age_seconds, now_ns))

    call_spread_pct = None
    if call_quote is not None:
        call_spread_pct = call_quote.spread_pct()
    put_spread_pct = None
    if put_quote is not None:
        put_spread_pct = put_quote.spread_pct()

    entry_premium = None
    buyback_cost = None
    initial_gap_pct = None
    both_two_sided = (
        call_quote is not None
        and put_quote is not None
        and call_quote.is_two_sided()
        and put_quote.is_two_sided()
    )
    if both_two_sided:
        entry_premium = (call_quote.bid + put_quote.bid) * quantity * multiplier
        buyback_cost = (call_quote.ask + put_quote.ask) * quantity * multiplier
        if entry_premium > 0:
            initial_gap_pct = (buyback_cost - entry_premium) / entry_premium * Decimal("100")
            if initial_gap_pct >= stop_loss:
                problems.append(
                    f"STRADDLE: buyback at ask is {initial_gap_pct:.2f}% above the entry at bid; "
                    f"the {stop_loss}% stop-loss would trigger immediately"
                )

    return LiquidityReport(
        ok=len(problems) == 0,
        problems=tuple(problems),
        call_spread_pct=call_spread_pct,
        put_spread_pct=put_spread_pct,
        entry_premium=entry_premium,
        buyback_cost=buyback_cost,
        initial_gap_pct=initial_gap_pct,
    )


def describe_report(report):
    """Lines for the strategy log: the verdict, the premium figures, then each problem."""
    if report.ok:
        verdict = "Entry check: PASS"
    else:
        verdict = f"Entry check: FAIL ({len(report.problems)} problems)"
    lines = [verdict]

    if report.entry_premium is not None:
        lines.append(
            f"  entry premium at bid {report.entry_premium} USDT | buyback at ask "
            f"{report.buyback_cost} USDT | initial gap {report.initial_gap_pct:.2f}%"
        )
    for problem in report.problems:
        lines.append(f"  - {problem}")
    return lines
