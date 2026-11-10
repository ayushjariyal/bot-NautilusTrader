"""
Unit tests for execution/order_gateway.py. The "orders" are simple fakes; the submit
function is a fake that records calls. Proves dry-run mode never submits.
"""

from types import SimpleNamespace

from execution.order_gateway import DRY_RUN
from execution.order_gateway import SUBMITTED
from execution.order_gateway import OrderGateway


class Recorder:
    """Collects everything passed to it, to see what the gateway did."""

    def __init__(self):
        self.calls = []

    def __call__(self, item):
        self.calls.append(item)


def make_gateway(orders_allowed):
    submitted = Recorder()
    infos = Recorder()
    warnings = Recorder()
    gateway = OrderGateway(submitted, orders_allowed, infos, warnings)
    return gateway, submitted, infos, warnings


def fake_order(number):
    return SimpleNamespace(client_order_id=f"O-TEST-{number}")


def test_dry_run_never_calls_submit():
    gateway, submitted, infos, warnings = make_gateway(orders_allowed=False)
    for number in range(5):
        assert gateway.submit(fake_order(number), f"SELL order {number}") == DRY_RUN
    assert submitted.calls == []
    assert gateway.dry_run_count == 5
    assert gateway.submitted_count == 0
    assert gateway.dry_run_descriptions[0] == "SELL order 0"
    assert "DRY RUN: would submit SELL order 0" in warnings.calls[0]


def test_allowed_mode_submits_each_order_once():
    gateway, submitted, infos, warnings = make_gateway(orders_allowed=True)
    order = fake_order(1)
    assert gateway.submit(order, "SELL 0.01 CALL") == SUBMITTED
    assert submitted.calls == [order]
    assert gateway.submitted_count == 1
    assert warnings.calls == []
    assert "Submitting SELL 0.01 CALL" in infos.calls[0]


def test_orders_allowed_is_fixed_at_creation():
    gateway, _, _, _ = make_gateway(orders_allowed=0)
    assert gateway.orders_allowed is False
    assert "DRY RUN" in gateway.mode_text()
    live, _, _, _ = make_gateway(orders_allowed=True)
    assert "SUBMIT ENABLED" in live.mode_text()
