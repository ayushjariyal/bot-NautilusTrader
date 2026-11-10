"""
Unit tests for execution/position_manager.py. Positions are simple stand-ins with the
same fields NautilusTrader's Position uses (quantity: Quantity, side: PositionSide,
avg_px_open: float). No network.
"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.objects import Quantity

from execution.position_manager import FLAT
from execution.position_manager import LONG
from execution.position_manager import SHORT
from execution.position_manager import check_leg_position
from execution.position_manager import check_straddle_positions
from execution.position_manager import describe_leg_position
from execution.position_manager import net_leg_position


CALL = "BTC-12NOV26-73750-C-USDT-OPTION.BYBIT"
PUT = "BTC-12NOV26-73750-P-USDT-OPTION.BYBIT"


def fake_position(side, quantity, average_price):
    return SimpleNamespace(
        side=PositionSide[side],
        quantity=Quantity.from_str(quantity),
        avg_px_open=average_price,
    )


def test_no_positions_is_flat():
    leg = net_leg_position(CALL, [])
    assert leg.side == FLAT
    assert leg.quantity == 0
    assert leg.average_open_price is None
    assert leg.position_count == 0


def test_one_short_position():
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0)])
    assert leg.side == SHORT
    assert leg.quantity == Decimal("0.01")
    assert leg.signed_quantity == Decimal("-0.01")
    assert leg.average_open_price == Decimal("1200.0")


def test_float_noise_does_not_leak_into_quantities():
    # Quantity is exact; NautilusTrader's float signed_qty is deliberately not used.
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.03", 1190.0)])
    assert leg.quantity == Decimal("0.03")


def test_two_positions_are_netted_and_counted():
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0), fake_position("SHORT", "0.02", 1185.0)])
    assert leg.side == SHORT
    assert leg.quantity == Decimal("0.03")
    assert leg.average_open_price == Decimal("1190")
    assert leg.position_count == 2


def test_long_and_short_offset_to_net_long():
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0), fake_position("LONG", "0.03", 1100.0)])
    assert leg.side == LONG
    assert leg.quantity == Decimal("0.02")
    assert leg.average_open_price == Decimal("1100.0")


def test_expected_short_matches():
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0)])
    assert check_leg_position("CALL", leg, Decimal("0.01")) == []


def test_wrong_size_and_wrong_side_are_reported():
    short = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0)])
    assert check_leg_position("CALL", short, Decimal("0.02")) == ["CALL: expected SHORT 0.02, found SHORT 0.01"]
    long = net_leg_position(CALL, [fake_position("LONG", "0.01", 1200.0)])
    assert "found LONG 0.01" in check_leg_position("CALL", long, Decimal("0.01"))[0]
    flat = net_leg_position(CALL, [])
    assert "found FLAT 0" in check_leg_position("CALL", flat, Decimal("0.01"))[0]


def test_unexpected_position_when_none_expected():
    leg = net_leg_position(PUT, [fake_position("SHORT", "0.01", 1150.0)])
    assert check_leg_position("PUT", leg, Decimal("0")) == ["PUT: unexpected SHORT position of 0.01"]


def test_more_than_one_position_per_contract_is_reported():
    leg = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0), fake_position("SHORT", "0.01", 1200.0)])
    problems = check_leg_position("CALL", leg, Decimal("0.02"))
    assert "2 open positions on one contract" in problems[0]


def test_balanced_straddle_passes():
    call = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0)])
    put = net_leg_position(PUT, [fake_position("SHORT", "0.01", 1150.0)])
    assert check_straddle_positions(call, put, Decimal("0.01"), Decimal("0.01"), require_balanced=True) == []


def test_unbalanced_straddle_is_reported_only_when_required():
    call = net_leg_position(CALL, [fake_position("SHORT", "0.02", 1200.0)])
    put = net_leg_position(PUT, [fake_position("SHORT", "0.01", 1150.0)])
    problems = check_straddle_positions(call, put, Decimal("0.02"), Decimal("0.01"), require_balanced=True)
    assert problems == ["STRADDLE: legs are unbalanced (call 0.02, put 0.01)"]
    assert check_straddle_positions(call, put, Decimal("0.02"), Decimal("0.01"), require_balanced=False) == []


def test_describe_leg_position():
    assert describe_leg_position("CALL", net_leg_position(CALL, [])) == f"CALL {CALL}: FLAT"
    short = net_leg_position(CALL, [fake_position("SHORT", "0.01", 1200.0)])
    assert describe_leg_position("CALL", short) == f"CALL {CALL}: SHORT 0.01 @ avg 1200.0"
