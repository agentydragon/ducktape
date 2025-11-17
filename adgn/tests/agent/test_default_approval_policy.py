"""Test default approval policy via container evaluator."""

import pytest

from adgn.agent.policies.policy_types import ApprovalDecision, PolicyRequest
from adgn.mcp._shared.constants import RESOURCES_SERVER_NAME, UI_SERVER_NAME
from adgn.mcp._shared.naming import build_mcp_function


def _make_policy_for_decision(decision_enum: str) -> str:
    return (
        "from adgn.agent.policies.policy_types import ApprovalDecision, PolicyRequest\n"
        "from adgn.agent.policies.scaffold import run\n"
        + (
            f"""
def decide(ctx: PolicyRequest):
    return ({decision_enum}, 'ok')
if __name__ == '__main__':
    raise SystemExit(run(decide))
"""
        )
    )


@pytest.mark.requires_docker
async def test_ui_tools_allowed(policy_evaluator):
    ui_tools = [
        PolicyRequest(name=build_mcp_function(UI_SERVER_NAME, "send_message"), arguments={}),
        PolicyRequest(name=build_mcp_function(UI_SERVER_NAME, "end_turn"), arguments={}),
    ]
    for ctx in ui_tools:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ALLOW


@pytest.mark.requires_docker
async def test_resource_operations_allowed(policy_evaluator):
    resource_ops = [
        PolicyRequest(name=build_mcp_function(RESOURCES_SERVER_NAME, "read"), arguments={}),
        PolicyRequest(name=build_mcp_function(RESOURCES_SERVER_NAME, "list"), arguments={}),
    ]
    for ctx in resource_ops:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ALLOW


@pytest.mark.requires_docker
async def test_other_tools_require_approval(policy_evaluator):
    other_tools = [
        PolicyRequest(name=build_mcp_function("echo", "echo"), arguments={}),
        PolicyRequest(name=build_mcp_function("some_server", "some_tool"), arguments={}),
    ]
    for ctx in other_tools:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ASK


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
