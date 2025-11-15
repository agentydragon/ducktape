import asyncio
from datetime import UTC, datetime
from importlib import resources
import logging
import uuid

from fastmcp.server.context import ServerSession
from jinja2 import Template
from pydantic import BaseModel

from adgn.agent.approvals import ApprovalPolicyEngine
from adgn.agent.models.proposal_status import ProposalStatus

# Persistence API (SQLite-backed implementation injected by container)
from adgn.agent.policies.policy_types import PolicyRequest, PolicyResponse
from adgn.agent.policy_eval.container import ContainerPolicyEvaluator, run_policy_source
from adgn.mcp._shared.constants import (
    APPROVAL_POLICY_PROPOSALS_INDEX_URI,
    APPROVAL_POLICY_RESOURCE_URI,
    APPROVAL_POLICY_SERVER_NAME_APPROVER,
    APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    APPROVAL_POLICY_SERVER_NAME_READER,
    RUNTIME_EXEC_TOOL_NAME,
    RUNTIME_SERVER_NAME,
    UI_SERVER_NAME,
)
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp._shared.uris import (
    approval_policy_proposal_item_uri,
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

    # Use shared constant for proposals index URI (broadcast mapping)

    # proposal item URI helper removed; use approval_policy_proposal_item_uri directly

    def __init__(
        self,
        engine: ApprovalPolicyEngine,
        *,
        name: str = APPROVAL_POLICY_SERVER_NAME_READER,
    ) -> None:
        super().__init__(name=name, instructions=_load_instructions())
        self._engine = engine
        # Required backend context must come from the engine
        self._agent_id = engine.agent_id
        self._persistence = engine.persistence
        self._docker = engine.docker_client
        # Broadcast coordination for deterministic waits (tests)
        self._broadcast_version: int = 0
        self._broadcast_cond: asyncio.Condition = asyncio.Condition()

        # Bridge engine notifications → MCP protocol resource updates
        def _notify(uri: str) -> None:
            # Fire-and-forget; schedule broadcast and signal completion to waiters
            logger.debug("engine notify uri=%s", uri)
            asyncio.create_task(self._broadcast_and_signal(uri))

        # Install notifier hook on the engine (required wiring)
        self._engine.set_notifier(_notify)

        # Register resources only (no proposer/admin tools here)
        self._register_resources()

        # Protocol-level resource subscriptions: acknowledge subscribe/unsubscribe
        # and maintain a minimal per-session index. Notifications are broadcast
        # by the server regardless of subscriptions, but handlers ensure that
        # capability gating reflects true support and calls succeed.
        self._session_subscriptions: dict[ServerSession, set[str]] = {}
        ll = self._mcp_server

        @ll.subscribe_resource()
        async def _subscribe(uri):  # type: ignore[no-redef]
            ctx = ll.request_context
            sess = ctx.session
            self._session_subscriptions.setdefault(sess, set()).add(str(uri))
            return None

        @ll.unsubscribe_resource()
        async def _unsubscribe(uri):  # type: ignore[no-redef]
            ctx = ll.request_context
            sess = ctx.session
            try:
                self._session_subscriptions.get(sess, set()).discard(str(uri))
            finally:
                # Do not error if unknown; protocol allows idempotent unsubscribe
                return None

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

        @self.resource(
            APPROVAL_POLICY_PROPOSALS_INDEX_URI + "/{id}",
            name="proposal",
            mime_type="text/x-python",
        )
        async def proposal_item(id: str) -> str:
            if (got := await self._persistence.get_policy_proposal(self._agent_id, id)) is None:
                raise KeyError(id)
            return got.content

        @self.flat_model()
        async def decide(input: PolicyRequest) -> PolicyResponse:  # type: ignore[unused-ignore]
            """Evaluate a policy decision for a single tool call via Docker-backed evaluator."""
            evaluator = ContainerPolicyEvaluator(
                agent_id=self._agent_id,
                docker_client=self._docker,
                engine=self._engine,
            )
            # Pass through input directly; it's already a PolicyRequest
            return await evaluator.decide(input)

    async def wait_for_broadcast(
        self, since_version: int | None = None, timeout: float | None = None
    ) -> int:
        """Await the next completed broadcast and return the new version.

        If since_version is provided, waits until a strictly higher version occurs.
        """
        target = (since_version or 0) + 1
        async with self._broadcast_cond:
            if timeout is None:
                await self._broadcast_cond.wait_for(lambda: self._broadcast_version >= target)
            else:
                await asyncio.wait_for(
                    self._broadcast_cond.wait_for(lambda: self._broadcast_version >= target),
                    timeout=timeout,
                )
            return self._broadcast_version

    # No nested IO models; see module-level CreateProposalArgs/ProposalDescriptor


## Legacy attach_approval_policy helper removed; use explicit attach_* helpers


async def attach_approval_policy_readonly(
    comp: Compositor,
    engine: ApprovalPolicyEngine,
    *,
    name: str = APPROVAL_POLICY_SERVER_NAME_READER,
    init_timeout_secs: float | None = None,
) -> ApprovalPolicyServer:
    """Attach the approval policy readonly server (resources only; no proposer tools)."""
    server = ApprovalPolicyServer(
        engine,
        name=name,
    )
    await comp.mount_inproc(name, server)
    return server


class ApprovalPolicyProposerServer(NotifyingFastMCP):
    """Proposer-only MCP server: create/withdraw proposals (no resources).

    Uses the readonly server to broadcast resource updates.
    """

    def __init__(
        self,
        *,
        engine: ApprovalPolicyEngine,
        name: str = APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    ) -> None:
        super().__init__(name=name, instructions=None)
        self._engine = engine
        self._agent_id = engine.agent_id
        self._persistence = engine.persistence
        self._docker = engine.docker_client

        @self.flat_model()
        async def create_proposal(input: CreateProposalArgs) -> ProposalDescriptor:  # type: ignore[unused-ignore]
            """Create a new policy proposal and return its descriptor."""
            if self._docker is not None:
                run_policy_source(
                    docker_client=self._docker,
                    source=input.content,
                    input_payload={
                        "name": build_mcp_function(UI_SERVER_NAME, "send_message"),
                        "arguments": {},
                    },
                )
            new_id = uuid.uuid4().hex
            await self._persistence.create_policy_proposal(
                self._agent_id, proposal_id=new_id, content=input.content
            )
            self._engine.notify_resource(approval_policy_proposal_item_uri(new_id))
            self._engine.notify_proposals_changed()
            return ProposalDescriptor(
                id=new_id,
                status=ProposalStatus.PENDING,
                created_at=datetime.now(UTC),
                decided_at=None,
            )

        @self.flat_model()
        async def withdraw_proposal(input: WithdrawProposalArgs) -> bool:  # type: ignore[unused-ignore]
            """Withdraw a pending policy proposal by id."""
            pid = input.id
            await self._persistence.delete_policy_proposal(self._agent_id, pid)
            self._engine.notify_resource(approval_policy_proposal_item_uri(pid))
            self._engine.notify_proposals_changed()
            return True


async def attach_approval_policy_proposer(
    comp: Compositor,
    engine: ApprovalPolicyEngine,
    *,
    name: str = APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    init_timeout_secs: float | None = None,
) -> ApprovalPolicyProposerServer:
    server = ApprovalPolicyProposerServer(
        engine=engine,
        name=name,
    )
    await comp.mount_inproc(name, server)
    return server


class ApprovalPolicyAdminServer(NotifyingFastMCP):
    """Admin-only MCP server: approve/reject proposals; may set policy text directly.

    Uses the readonly server to broadcast resource updates.
    """

    def __init__(
        self,
        *,
        engine: ApprovalPolicyEngine,
        name: str = APPROVAL_POLICY_SERVER_NAME_APPROVER,
    ) -> None:
        super().__init__(name=name, instructions=None)
        self._engine = engine
        self._agent_id = engine.agent_id
        self._persistence = engine.persistence
        self._docker = engine.docker_client

        @self.flat_model()
        async def approve_proposal(input: ApproveProposalArgs) -> bool:  # type: ignore[unused-ignore]
            """Approve a pending policy proposal by id (activates policy)."""
            got = await self._persistence.get_policy_proposal(self._agent_id, input.id)
            if got is None:
                raise KeyError(input.id)
            # Self-check the proposal program before activation
            if self._docker is not None:
                run_policy_source(
                    docker_client=self._docker,
                    source=got.content,
                    input_payload={
                        "name": build_mcp_function(UI_SERVER_NAME, "send_message"),
                        "arguments": {},
                    },
                )
            # Activate policy in engine (notifies via engine)
            self._engine.set_policy(got.content)
            await self._persistence.approve_policy_proposal(self._agent_id, input.id)
            self._engine.notify_resource(approval_policy_proposal_item_uri(input.id))
            self._engine.notify_proposals_changed()
            return True

        @self.flat_model()
        async def reject_proposal(input: RejectProposalArgs) -> bool:  # type: ignore[unused-ignore]
            """Reject a pending policy proposal by id."""
            await self._persistence.reject_policy_proposal(self._agent_id, input.id)
            self._engine.notify_resource(approval_policy_proposal_item_uri(input.id))
            self._engine.notify_proposals_changed()
            return True

        @self.flat_model()
        async def set_policy_text(input: SetPolicyTextArgs) -> bool:  # type: ignore[unused-ignore]
            """Directly set active policy text after self-check."""
            # Self-check program using engine's docker client
            self._engine.self_check(input.source)
            self._engine.set_policy(input.source)
            return True


async def attach_approval_policy_admin(
    comp: Compositor,
    engine: ApprovalPolicyEngine,
    *,
    name: str = APPROVAL_POLICY_SERVER_NAME_APPROVER,
    init_timeout_secs: float | None = None,
) -> ApprovalPolicyAdminServer:
    server = ApprovalPolicyAdminServer(
        engine=engine,
        name=name,
    )
    await comp.mount_inproc(name, server)
    return server


class SetPolicyTextArgs(BaseModel):
    """Direct policy set input for admin endpoint.

    Uses field name 'source' to distinguish from proposal 'content'.
    """

    source: str
