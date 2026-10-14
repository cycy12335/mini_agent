from decimal import Decimal
import json

import pytest

from mini_agent.actions import TOOL_SCHEMAS
from mini_agent.adapters.http_api import HTTPAdapter, HTTPResult, ModelError
from mini_agent.api_config import APIConfig
from mini_agent.costs import CostBudget
from mini_agent.models import ToolError


def config(**values):
    return APIConfig("https://api.deepseek.com/responses", "deepseek-flash", **values)


def test_full_run_reservation_and_retry_uncertainty():
    budget = CostBudget(config(max_output_tokens=1000))
    # 101024 maximum input tokens plus 1000 output tokens = CNY 0.210048.
    first = budget.reserve(b"x" * 100000, 1000)
    budget.reserve(b"x" * 100000, 1000)
    with pytest.raises(ToolError, match="no request sent"):
        budget.reserve(b"x" * 100000, 1000)
    assert budget.used == Decimal("0.420096")
    budget.settle(first, 100, 50)
    assert budget.used == Decimal("0.210648")
    assert budget.summary()["uncertain_requests"] == 1


def test_cost_gate_blocks_before_transport():
    class NeverCalled:
        def post(self, *args):
            pytest.fail("Over-budget HTTP request was sent")
    model = HTTPAdapter(config(max_run_cost_cny=.001), key="fake-unit-test-key",
                        authorized=True, transport=NeverCalled())
    with pytest.raises(ToolError) as error:
        model.next_action([], TOOL_SCHEMAS)
    assert error.value.code == "MODEL_COST_LIMIT" and model.requests == 0


def test_uncertain_transport_retry_must_fit_remaining_budget():
    class Broken:
        calls = 0
        def post(self, *args):
            self.calls += 1
            raise ModelError("MODEL_NETWORK_ERROR", "unknown outcome")
    transport = Broken()
    model = HTTPAdapter(config(max_run_cost_cny=.10), key="fake-unit-test-key",
                        authorized=True, transport=transport)
    with pytest.raises(ToolError) as error:
        model.next_action([], TOOL_SCHEMAS)
    assert error.value.code == "MODEL_COST_LIMIT" and transport.calls == 1
    assert model.cost_summary()["uncertain_requests"] == 1
    assert Decimal(model.cost_summary()["accounted_upper_estimate_cny"]) <= Decimal(".10")


def test_missing_usage_keeps_full_reservation_across_actions():
    class NoUsage:
        calls = 0
        def post(self, *args):
            self.calls += 1
            data = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": '{"type":"final","message":"done"}'}]}]}
            return HTTPResult(200, json.dumps(data).encode())
    transport = NoUsage()
    model = HTTPAdapter(config(max_run_cost_cny=.10), key="fake-unit-test-key",
                        authorized=True, transport=transport)
    assert model.next_action([], TOOL_SCHEMAS)
    with pytest.raises(ToolError) as error:
        model.next_action([], TOOL_SCHEMAS)
    assert error.value.code == "MODEL_COST_LIMIT" and transport.calls == 1


def test_reported_usage_exceeding_estimate_stops_without_hiding_charge():
    budget = CostBudget(config())
    reservation = budget.reserve(b"small", 100)
    with pytest.raises(ToolError) as error:
        budget.settle(reservation, 300000, 100)
    assert error.value.code == "MODEL_COST_ESTIMATE_EXCEEDED"
    assert budget.summary()["billing_verified"] is False
    assert Decimal(budget.summary()["accounted_upper_estimate_cny"]) > Decimal(".5")


def test_unreliable_estimate_disables_run_even_with_remaining_money():
    budget = CostBudget(config())
    reservation = budget.reserve(b"small", 10)
    with pytest.raises(ToolError):
        budget.settle(reservation, 2000, 10)
    assert budget.used < budget.limit
    with pytest.raises(ToolError):
        budget.reserve(b"another", 10)
    assert len(budget.attempts) == 1


@pytest.mark.parametrize("value", [0, -.5, .51, True, "0.5", float("inf"), float("nan")])
def test_cannot_raise_or_disable_authorized_ceiling(value):
    with pytest.raises(ValueError):
        config(max_run_cost_cny=value)


def test_unknown_provider_requires_verified_unit_prices():
    budget = CostBudget(APIConfig("https://example.com/responses", "other-model"))
    with pytest.raises(ToolError) as error:
        budget.reserve(b"input", 100)
    assert error.value.code == "MODEL_PRICING_UNKNOWN"
    assert not budget.attempts


def test_cache_hits_are_conservatively_priced_as_misses():
    budget = CostBudget(config())
    reservation = budget.reserve(b"x" * 2000, 1000)
    budget.settle(reservation, 1000, 100)
    assert budget.used == Decimal("0.0028")


def test_successful_usage_settles_even_when_action_envelope_is_rejected():
    class Incomplete:
        def post(self, *args):
            return HTTPResult(200, json.dumps({"status": "incomplete", "usage": {"input_tokens": 50, "output_tokens": 60}}).encode())
    model = HTTPAdapter(config(), key="fake-unit-test-key", authorized=True, transport=Incomplete())
    with pytest.raises(ModelError):
        model.next_action([], TOOL_SCHEMAS)
    assert model.cost_summary()["uncertain_requests"] == 0
    assert Decimal(model.cost_summary()["accounted_upper_estimate_cny"]) == Decimal(".00058")
