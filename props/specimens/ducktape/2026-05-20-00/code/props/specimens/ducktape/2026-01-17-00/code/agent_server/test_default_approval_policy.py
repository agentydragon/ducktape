"""Test default approval policy via container evaluator."""

import pytest

from agent_server.policies.policy_types import ApprovalDecision
from agent_server.testing.fixtures import make_policy_request
from mcp_infra.constants import RESOURCES_MOUNT_PREFIX, UI_MOUNT_PREFIX
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME


def _make_policy_for_decision(decision_enum: str) -> str:
    return (
        "from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest\n"
        "from agent_server.policies.scaffold import run\n"
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
@pytest.mark.requires_runtime_image
async def test_ui_tools_allowed(policy_evaluator):
    ui_tools = [make_policy_request(UI_MOUNT_PREFIX, "send_message"), make_policy_request(UI_MOUNT_PREFIX, "end_turn")]
    for ctx in ui_tools:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ALLOW


@pytest.mark.requires_docker
@pytest.mark.requires_runtime_image
async def test_resource_operations_allowed(policy_evaluator):
    resource_ops = [
        make_policy_request(RESOURCES_MOUNT_PREFIX, "read"),
        make_policy_request(RESOURCES_MOUNT_PREFIX, "list"),
    ]
    for ctx in resource_ops:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ALLOW


@pytest.mark.requires_docker
@pytest.mark.requires_runtime_image
async def test_other_tools_require_approval(policy_evaluator):
    other_tools = [
        make_policy_request(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME),
        make_policy_request(MCPMountPrefix("some_server"), "some_tool"),
    ]
    for ctx in other_tools:
        result = await policy_evaluator.decide(ctx)
        assert result.decision is ApprovalDecision.ASK


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
