"""
Level 2 risk rules: 1% stop-loss and 2% take-profit on the combined buyback cost.

Assignment wording:
  Stop-Loss:   "If the combined premium required to buy back both options increases by
                1% from the combined entry premium, ... close both positions."
  Take-Profit: "If the combined premium required to buy back both options decreases by
                2% from the combined entry premium, ... buy back and close both positions."

For a SHORT straddle a rising buyback cost is a loss, a falling one is a profit:

    stop-loss level   = entry premium x (1 + 1/100)    buyback cost >= level -> STOP_LOSS
    take-profit level = entry premium x (1 - 2/100)    buyback cost <= level -> TAKE_PROFIT
    otherwise                                                               -> HOLD

Example: entry 23.50 USDT -> stop-loss at 23.735, take-profit at 23.03.

If the buyback cost cannot be computed (missing ask, stale quote, unbalanced legs), the
decision is HOLD with the reason, and the strategy logs a warning. Acting on data we
cannot trust (for example an empty ask that looks like a free buyback) could close the
position for no real reason; waiting for the next valid quote is the safer default.
(Milestone 10 adds the handling of a feed that stays broken.)

Plain Python; used by strategies/btc_straddle.py on every leg quote while ENTERED.
"""

from dataclasses import dataclass
from decimal import Decimal

from strategies.premium_calculator import percent_change


HOLD = "HOLD"
STOP_LOSS = "STOP_LOSS"
TAKE_PROFIT = "TAKE_PROFIT"


@dataclass(frozen=True)
class RiskLevels:
    """The two trigger levels for one entry premium (USDT)."""

    entry_premium: Decimal
    stop_loss_level: Decimal
    take_profit_level: Decimal


@dataclass(frozen=True)
class ExitDecision:
    """
    action: HOLD, STOP_LOSS or TAKE_PROFIT.
    buyback_cost (Decimal or None): the value the decision was based on.
    change_pct (Decimal or None): buyback cost vs entry premium, in %.
    reason (str): human-readable explanation for the log.
    """

    action: str
    buyback_cost: Decimal | None
    change_pct: Decimal | None
    reason: str


def risk_levels(entry_premium, stop_loss_pct, take_profit_pct):
    """
    Compute the stop-loss and take-profit levels for an entry premium.

    Parameters: entry_premium (Decimal, USDT); stop_loss_pct, take_profit_pct (percent,
    e.g. 1.0 and 2.0). Returns: RiskLevels.
    """
    stop_loss = Decimal(str(stop_loss_pct))
    take_profit = Decimal(str(take_profit_pct))
    hundred = Decimal("100")
    return RiskLevels(
        entry_premium=entry_premium,
        stop_loss_level=entry_premium * (hundred + stop_loss) / hundred,
        take_profit_level=entry_premium * (hundred - take_profit) / hundred,
    )


def evaluate_exit(levels, buyback):
    """
    Decide whether to exit.

    Parameters: levels (RiskLevels); buyback (PremiumValue from combined_buyback_cost).
    Returns: ExitDecision.
    """
    if buyback.value is None:
        return ExitDecision(HOLD, None, None, f"cannot value the buyback: {buyback.problem}")

    cost = buyback.value
    change = percent_change(levels.entry_premium, cost)

    if cost >= levels.stop_loss_level:
        return ExitDecision(
            STOP_LOSS,
            cost,
            change,
            f"buyback cost {cost} >= stop-loss level {levels.stop_loss_level} ({change:+.2f}%)",
        )
    if cost <= levels.take_profit_level:
        return ExitDecision(
            TAKE_PROFIT,
            cost,
            change,
            f"buyback cost {cost} <= take-profit level {levels.take_profit_level} ({change:+.2f}%)",
        )
    return ExitDecision(
        HOLD,
        cost,
        change,
        f"buyback cost {cost} ({change:+.2f}%) between take-profit {levels.take_profit_level} "
        f"and stop-loss {levels.stop_loss_level}",
    )
