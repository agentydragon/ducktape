from __future__ import annotations

from typing import Final

from fastmcp.client import Client

from adgn.agent.policies.policy_types import PolicyRequest, PolicyResponse
from adgn.mcp._shared.client_helpers import call_simple_ok, call_tool_typed
from adgn.mcp._shared.constants import APPROVAL_POLICY_SERVER_NAME_APPROVER, APPROVAL_POLICY_SERVER_NAME_READER
from adgn.mcp.approval_policy.server import ApproveProposalArgs, RejectProposalArgs, SetPolicyTextArgs

READER_SERVER_NAME: Final[str] = APPROVAL_POLICY_SERVER_NAME_READER
APPROVER_SERVER_NAME: Final[str] = APPROVAL_POLICY_SERVER_NAME_APPROVER


class PolicyReaderClient:
    """Typed wrapper for the approval policy reader MCP client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return READER_SERVER_NAME

    @property
    def client(self) -> Client:
        return self._client

    async def decide(self, args: PolicyRequest) -> PolicyResponse:
        return await call_tool_typed(self._client, "decide", args, PolicyResponse)


class PolicyApproverClient:
    """Typed wrapper for the approval policy approver MCP client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return APPROVER_SERVER_NAME

    @property
    def client(self) -> Client:
        return self._client

    async def set_policy_text(self, source: str) -> None:
        """Set active policy text directly after self-check.

        Accepts raw policy source; constructs the server's typed input.
        """
        await call_simple_ok(
            self._client, name="set_policy_text", arguments=SetPolicyTextArgs(source=source).model_dump()
        )

    async def approve_proposal(self, args: ApproveProposalArgs) -> None:
        await call_simple_ok(self._client, name="approve_proposal", arguments=args.model_dump())

    async def reject_proposal(self, args: RejectProposalArgs) -> None:
        await call_simple_ok(self._client, name="reject_proposal", arguments=args.model_dump())
