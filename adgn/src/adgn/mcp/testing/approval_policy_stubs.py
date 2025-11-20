"""Typed stubs for approval_policy MCP servers."""

from adgn.agent.policies.policy_types import PolicyRequest, PolicyResponse
from adgn.mcp.approval_policy.server import (
    ApproveProposalArgs,
    CreateProposalArgs,
    ProposalDescriptor,
    ProposalDetail,
    RejectProposalArgs,
    ReloadPolicyArgs,
    SetPolicyTextArgs,
    ValidatePolicyArgs,
    ValidationResult,
    WithdrawProposalArgs,
)
from adgn.mcp.stubs.server_stubs import ServerStub


class ApprovalPolicyServerStub(ServerStub):
    """Typed stub for approval policy reader server operations (resources + decide tool)."""

    async def decide(self, input: PolicyRequest) -> PolicyResponse:
        raise NotImplementedError  # Auto-wired at runtime


class ApprovalPolicyProposerServerStub(ServerStub):
    """Typed stub for approval policy proposer server operations."""

    async def create_proposal(self, input: CreateProposalArgs) -> ProposalDescriptor:
        raise NotImplementedError  # Auto-wired at runtime

    async def withdraw_proposal(self, input: WithdrawProposalArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime


class ApprovalPolicyAdminServerStub(ServerStub):
    """Typed stub for approval policy admin server operations."""

    async def approve_proposal(self, input: ApproveProposalArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime

    async def reject_proposal(self, input: RejectProposalArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime

    async def set_policy_text(self, input: SetPolicyTextArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime

    async def validate_policy(self, input: ValidatePolicyArgs) -> ValidationResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def reload_policy(self, input: ReloadPolicyArgs) -> None:
        raise NotImplementedError  # Auto-wired at runtime
