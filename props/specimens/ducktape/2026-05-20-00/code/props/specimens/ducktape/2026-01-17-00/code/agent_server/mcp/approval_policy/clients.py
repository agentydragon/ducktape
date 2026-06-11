from __future__ import annotations

from agent_server.mcp.approval_policy.engine import DecideProposalArgs, SetPolicyTextArgs
from agent_server.policies.policy_types import PolicyRequest, PolicyResponse
from mcp_infra.stubs.server_stubs import ServerStub


class PolicyReaderStub(ServerStub):
    """Typed stub for the approval policy reader MCP server."""

    async def evaluate_policy(self, input: PolicyRequest) -> PolicyResponse:
        raise NotImplementedError  # Auto-wired at runtime


class PolicyApproverStub(ServerStub):
    """Typed stub for the approval policy approver MCP server."""

    async def set_policy_text(self, input: SetPolicyTextArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime

    async def decide_proposal(self, input: DecideProposalArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime
