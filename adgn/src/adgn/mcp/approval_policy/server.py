import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from importlib import resources
import logging

from fastmcp.server.context import ServerSession
from jinja2 import Template
from pydantic import AnyUrl, BaseModel

from adgn.agent.approvals import ApprovalPolicyEngine
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.policies.policy_types import PolicyRequest, PolicyResponse
from adgn.agent.policy_eval.container import ContainerPolicyEvaluator
from adgn.mcp._shared.constants import (
    APPROVAL_POLICY_PROPOSALS_INDEX_URI,
    APPROVAL_POLICY_RESOURCE_URI,
    APPROVAL_POLICY_SERVER_NAME_APPROVER,
    APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    APPROVAL_POLICY_SERVER_NAME_READER,
    RUNTIME_EXEC_TOOL_NAME,
    RUNTIME_SERVER_NAME,
)
from adgn.mcp.compositor.server import Compositor
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

# IO types unified: use engine-level PolicyRequest and PolicyResponse

logger = logging.getLogger(__name__)


class CreateProposalArgs(BaseModel):
    content: str


class WithdrawProposalArgs(BaseModel):
    id: str


class ProposalDescriptor(BaseModel):
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None


class ApproveProposalArgs(BaseModel):
    id: str


class RejectProposalArgs(BaseModel):
    id: str


# IO types unified; see PolicyRequest/PolicyResponse in adgn.agent.approvals


def _load_instructions() -> str:
    """Load and render instructions with embedded shared constants via Jinja2."""
    raw = resources.files(__package__).joinpath("instructions.j2.md").read_text(encoding="utf-8")
    tmpl = Template(raw)
    rendered = tmpl.render(
        RUNTIME_SERVER_NAME=RUNTIME_SERVER_NAME,
        RUNTIME_EXEC_TOOL_NAME=RUNTIME_EXEC_TOOL_NAME,
        TRUSTED_POLICY_PATH=None,
        TRUSTED_POLICY_URL=APPROVAL_POLICY_RESOURCE_URI,
    )
    return str(rendered)


class ApprovalPolicyServer(NotifyingFastMCP):
    """MCP facade over ApprovalPolicyEngine with protocol notifications.

    Exposes a deterministic waiter for tests via wait_for_broadcast(since_version).
    Proposals are authored inside the runtime container and surfaced to the UI via
    the backend snapshot.
    """

    def __init__(self, engine: ApprovalPolicyEngine, *, name: str = APPROVAL_POLICY_SERVER_NAME_READER) -> None:
        super().__init__(name=name, instructions=_load_instructions())
        self._engine = engine
        # Broadcast coordination for deterministic waits (tests)
        self._broadcast_version: int = 0
        self._broadcast_cond: asyncio.Condition = asyncio.Condition()

        # Bridge engine notifications → MCP protocol resource updates
        def _notify(uri: str) -> None:
            # Fire-and-forget; schedule broadcast and signal completion to waiters
            logger.debug("engine notify uri=%s", uri)
            task = asyncio.create_task(self._broadcast_and_signal(uri))
            task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)

        # Install notifier hook on the engine (required wiring)
        self._engine.set_notifier(_notify)

        # Register resources only (no proposer/admin tools here)
        self._register_resources()

        # Protocol-level resource subscriptions: acknowledge subscribe/unsubscribe
        # and maintain a minimal per-session index. Notifications are broadcast
        # by the server regardless of subscriptions, but handlers ensure that
        # capability gating reflects true support and calls succeed.
        self._session_subscriptions: defaultdict[ServerSession, set[AnyUrl]] = defaultdict(set)
        mcp_server = self._mcp_server

        def _subscriptions() -> set[AnyUrl]:
            """Return subscription set for current session context."""
            return self._session_subscriptions[mcp_server.request_context.session]

        @mcp_server.subscribe_resource()
        async def _subscribe(uri: AnyUrl):
            _subscriptions().add(uri)

        @mcp_server.unsubscribe_resource()
        async def _unsubscribe(uri: AnyUrl):
            _subscriptions().discard(uri)
            # Do not error if unknown; protocol allows idempotent unsubscribe

        # Do not expose a server-local "list subscriptions" resource; the
        # aggregator (resources server) provides a single index for the UI.

    async def _broadcast_and_signal(self, uri: str) -> None:
        if uri == APPROVAL_POLICY_PROPOSALS_INDEX_URI:
            await self.broadcast_resource_list_changed()
        else:
            await self.broadcast_resource_updated(uri)
        async with self._broadcast_cond:
            self._broadcast_version += 1
            self._broadcast_cond.notify_all()

    def _register_resources(self) -> None:
        # Resources for agents: active policy, proposals index and items
        @self.resource(APPROVAL_POLICY_RESOURCE_URI, name="policy.py", mime_type="text/x-python")
        def active_policy() -> str:
            # Single source of truth: engine
            content, _version = self._engine.get_policy()
            return content

        @self.resource(APPROVAL_POLICY_PROPOSALS_INDEX_URI + "/{id}", name="proposal", mime_type="text/x-python")
        async def proposal_item(id: str) -> str:
            if (got := await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)) is None:
                raise KeyError(id)
            return got.content

        @self.flat_model()
        async def decide(input: PolicyRequest) -> PolicyResponse:
            """Evaluate a policy decision for a single tool call via Docker-backed evaluator."""
            evaluator = ContainerPolicyEvaluator(
                agent_id=self._engine.agent_id, docker_client=self._engine.docker_client, engine=self._engine
            )
            # Pass through input directly; it's already a PolicyRequest
            return await evaluator.decide(input)

    async def wait_for_broadcast(self, since_version: int | None = None) -> int:
        """Await the next completed broadcast and return the new version.

        If since_version is provided, waits until a strictly higher version occurs.
        Use asyncio.timeout() around this call to add a timeout.
        """
        target = (since_version or 0) + 1
        async with self._broadcast_cond:
            await self._broadcast_cond.wait_for(lambda: self._broadcast_version >= target)
            return self._broadcast_version

    # No nested IO models; see module-level CreateProposalArgs/ProposalDescriptor


## Legacy attach_approval_policy helper removed; use explicit attach_* helpers


async def attach_approval_policy_readonly(
    comp: Compositor, engine: ApprovalPolicyEngine, *, name: str = APPROVAL_POLICY_SERVER_NAME_READER
) -> ApprovalPolicyServer:
    """Attach the approval policy readonly server (resources only; no proposer tools)."""
    server = ApprovalPolicyServer(engine, name=name)
    await comp.mount_inproc(name, server)
    return server


class ApprovalPolicyProposerServer(NotifyingFastMCP):
    """Proposer-only MCP server: create/withdraw proposals (no resources).

    Uses the readonly server to broadcast resource updates.
    """

    def __init__(self, *, engine: ApprovalPolicyEngine, name: str = APPROVAL_POLICY_SERVER_NAME_PROPOSER) -> None:
        super().__init__(name=name, instructions=None)
        self._engine = engine

        @self.flat_model()
        async def create_proposal(input: CreateProposalArgs) -> ProposalDescriptor:
            """Create a new policy proposal and return its descriptor."""
            new_id = await self._engine.create_proposal(input.content)
            return ProposalDescriptor(
                id=new_id, status=ProposalStatus.PENDING, created_at=datetime.now(UTC), decided_at=None
            )

        @self.flat_model()
        async def withdraw_proposal(input: WithdrawProposalArgs) -> None:
            """Withdraw a pending policy proposal by id."""
            await self._engine.withdraw_proposal(input.id)


async def attach_approval_policy_proposer(
    comp: Compositor, engine: ApprovalPolicyEngine, *, name: str = APPROVAL_POLICY_SERVER_NAME_PROPOSER
) -> ApprovalPolicyProposerServer:
    server = ApprovalPolicyProposerServer(engine=engine, name=name)
    await comp.mount_inproc(name, server)
    return server


class ApprovalPolicyAdminServer(NotifyingFastMCP):
    """Admin-only MCP server: approve/reject proposals; may set policy text directly.

    Uses the readonly server to broadcast resource updates.
    """

    def __init__(self, *, engine: ApprovalPolicyEngine, name: str = APPROVAL_POLICY_SERVER_NAME_APPROVER) -> None:
        super().__init__(name=name, instructions=None)
        self._engine = engine

        @self.flat_model()
        async def approve_proposal(input: ApproveProposalArgs) -> None:
            """Approve a pending policy proposal by id (activates policy)."""
            await self._engine.approve_proposal(input.id)

        @self.flat_model()
        async def reject_proposal(input: RejectProposalArgs) -> None:
            """Reject a pending policy proposal by id."""
            await self._engine.reject_proposal(input.id)

        @self.flat_model()
        async def set_policy_text(input: SetPolicyTextArgs) -> None:
            """Directly set active policy text after self-check."""
            # Self-check program using engine's docker client
            self._engine.self_check(input.source)
            self._engine.set_policy(input.source)


async def attach_approval_policy_admin(
    comp: Compositor, engine: ApprovalPolicyEngine, *, name: str = APPROVAL_POLICY_SERVER_NAME_APPROVER
) -> ApprovalPolicyAdminServer:
    server = ApprovalPolicyAdminServer(engine=engine, name=name)
    await comp.mount_inproc(name, server)
    return server


class SetPolicyTextArgs(BaseModel):
    """Direct policy set input for admin endpoint.

    Uses field name 'source' to distinguish from proposal 'content'.
    """

    source: str
