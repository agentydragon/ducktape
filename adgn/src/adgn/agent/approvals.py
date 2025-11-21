from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
import logging
import uuid

from docker import DockerClient
from pydantic import BaseModel

from adgn.agent.handler import AbortTurnDecision, ContinueDecision, DenyContinueDecision
from adgn.agent.models.policy_error import PolicyError
from adgn.agent.persist import Persistence
from adgn.agent.policy_eval.runner import run_policy_source
from adgn.agent.types import AgentID, ToolCall
from adgn.mcp._shared.constants import (
    AGENTS_POLICY_STATE_URI_FMT,
    APPROVAL_POLICY_PROPOSALS_INDEX_URI,
    APPROVAL_POLICY_RESOURCE_URI,
    UI_SERVER_NAME,
)
from adgn.mcp._shared.naming import build_mcp_function

# build_mcp_function is used for self_check payload construction

logger = logging.getLogger(__name__)


class PolicyValidationError(Exception):
    def __init__(self, message: str, details: PolicyError | None = None) -> None:
        super().__init__(message)
        self.details: PolicyError | None = details


# Policy source is executed only inside the container evaluator


# TODO(approval-policy follow-ups)
# - Resource operations are exposed as MCP tools (e.g., resources_list,
#   resources_read) and are gated by the Policy Gateway middleware.
#   The default policy allows RESOURCES server ops; tighten policy as needed.
# - Policy sandboxing: Execute user policy code under a stricter sandbox. Today
#   we execute with standard Python builtins and require explicit imports; future
#   hardening may restrict imports or isolate execution.
# - Persistence/versioning UX: Persistence exists (SQLite) for policy IDs and
#   proposals, but richer history/metadata and rollback tools could improve UX.
# - Multi-user/editor UX: Add conflict prevention and richer notifications for
#   concurrent edits/approvals (e.g., optimistic locking, better UI affordances).


class TurnAbortRequested(Exception):  # noqa: N818
    # TODO: Reconsider whether signalling turn abort via exceptions is the best approach
    def __init__(self, call_id: str, reason: str = "approval_denied", context: dict | None = None) -> None:
        self.call_id = call_id
        self.reason = reason
        self.context = context or {}
        super().__init__(f"Turn abort requested: {reason} (call_id={call_id})")


@dataclass
class PendingApproval:
    """Pending approval with tool call and future."""

    tool_call: ToolCall
    future: asyncio.Future[ContinueDecision | DenyContinueDecision | AbortTurnDecision]


class ApprovalHub:
    """In-process rendezvous for pending approval/decision events.

    - await_decision(call_id, request) -> Decision waits until resolve() is called
    - resolve(call_id, decision) resolves the pending decision
    - set_notifier(notifier) installs a callback for approval state changes
    """

    def __init__(self, notifier: Callable[[], None] | None = None) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._notifier = notifier

    def set_notifier(self, notifier: Callable[[], None]) -> None:
        """Install/replace the out-of-band notifier for approval state changes.

        Contract: notifier() is sync and non-blocking (may schedule async work).
        """
        self._notifier = notifier

    async def await_decision(
        self, call_id: str, tool_call: ToolCall
    ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
        async with self._lock:
            pending = self._pending.get(call_id)
            if pending is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)
            else:
                fut = pending.future
        if self._notifier:
            self._notifier()
        return await fut

    def resolve(self, call_id: str, decision: ContinueDecision | DenyContinueDecision | AbortTurnDecision) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)
        if self._notifier:
            self._notifier()

    @property
    def pending(self) -> dict[str, ToolCall]:
        """Public view of pending approval tool calls (immutable contract by convention)."""
        return {call_id: p.tool_call for call_id, p in self._pending.items()}


# ---- Approval Policy Engine (decoupled, in-memory; optional) ----


class WellKnownTools(StrEnum):
    SEND_MESSAGE = "send_message"
    END_TURN = "end_turn"
    SANDBOX_EXEC = "sandbox_exec"  # adgn.mcp.seatbelt_exec.server


def load_default_policy_source() -> str:
    """Load the packaged default approval policy source code as text."""
    return resources.files("adgn.agent.policies").joinpath("default_policy.py").read_text(encoding="utf-8")


class ApprovalPolicyEngine:
    """Single source of truth for active policy text/version.

    Validation and execution are delegated to the Docker-backed evaluator. The
    engine stores text and publishes change notifications via the optional
    notifier callback.
    """

    def __init__(
        self,
        notifier: Callable[[str], None] | None = None,
        *,
        docker_client: DockerClient,
        agent_id: AgentID,
        persistence: Persistence,
        policy_source: str,
    ) -> None:
        # DI of initial policy source; caller must pass explicit policy text.
        self._policy_source: str = policy_source
        # In-memory version counter for MCP resource change notifications.
        # Independent of SQL primary key (which is auto-incrementing per agent).
        # This tracks resource versions for the MCP protocol; SQL ID is for persistence.
        self._policy_id: int = 1  # Start at 1 since we have default content
        # Notifier receives a canonical policy resource URI for broadcasts
        self._notify = notifier
        # Public attributes for engine wiring; keep simple access patterns
        self.docker_client: DockerClient = docker_client
        self.agent_id: AgentID = agent_id
        self.persistence: Persistence = persistence

    def set_notifier(self, notifier: Callable[[str], None]) -> None:
        """Install/replace the out-of-band notifier for resource changes.

        Contract: notifier(uri) is sync and non-blocking (may schedule async work).
        """
        self._notify = notifier
        # No runtime volume state

    def get_policy(self) -> tuple[str, int]:
        return self._policy_source, self._policy_id

    async def set_policy(self, source: str) -> int:
        """Store new policy and return its database ID."""
        self._policy_source = source
        # Call persistence to get ACTUAL ID
        self._policy_id = await self.persistence.set_policy(self.agent_id, content=source)
        if self._notify:
            self._notify(APPROVAL_POLICY_RESOURCE_URI)
            # Also notify agent-specific policy state resource
            self._notify(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))
        return self._policy_id

    # Internal load used on startup to hydrate content/id from persistence
    def load_policy(self, source: str, *, policy_id: int) -> None:
        # Hydrate from persistence without executing the code
        self._policy_source = source
        self._policy_id = policy_id

    # No in-engine validation/TEST_CASES; evaluator will surface errors

    # No in-process evaluator construction; policy evaluation happens via MCP reader

    # Public attributes (no properties): docker_client, agent_id, persistence

    def self_check(self, source: str) -> None:
        run_policy_source(
            docker_client=self.docker_client,
            source=source,
            input_payload={"name": build_mcp_function(UI_SERVER_NAME, "send_message"), "arguments": {}},
        )

    def notify_resource(self, uri: str) -> None:
        cb = self._notify
        if cb:
            cb(uri)

    def notify_proposals_changed(self) -> None:
        cb = self._notify
        if cb:
            cb(APPROVAL_POLICY_PROPOSALS_INDEX_URI)

    def notify_proposal_change(self, proposal_id: int) -> None:
        """Notify about a specific proposal change and the proposals index.

        Convenience method that combines notifying about a specific proposal item
        and the proposals index list change.
        """
        self.notify_resource(f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal_id}")
        self.notify_proposals_changed()
        # Also notify agent-specific policy state resource since proposals changed
        self.notify_resource(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))

    async def create_proposal(self, content: str) -> int:
        """Create a new policy proposal and return its ID.

        Validates the proposal content if docker_client is available,
        persists it, and notifies about the change.
        """
        # Self-check proposal program if docker is available
        if self.docker_client is not None:
            self.self_check(content)
        # Create proposal and get actual database-assigned ID
        new_id = await self.persistence.create_policy_proposal(self.agent_id, proposal_id=0, content=content)
        self.notify_proposal_change(new_id)
        return new_id

    async def approve_proposal(self, proposal_id: int) -> None:
        """Approve a pending policy proposal by ID and activate it.

        Retrieves the proposal, validates it, activates it as the current policy,
        marks it approved in persistence, and notifies about the change.
        """
        if (got := await self.persistence.get_policy_proposal(self.agent_id, proposal_id)) is None:
            raise KeyError(str(proposal_id))
        # Self-check the proposal program before activation
        if self.docker_client is not None:
            self.self_check(got.content)
        # Activate policy (notifies via engine's set_policy)
        await self.set_policy(got.content)
        await self.persistence.approve_policy_proposal(self.agent_id, proposal_id)
        self.notify_proposal_change(proposal_id)

    async def reject_proposal(self, proposal_id: int) -> None:
        """Reject a pending policy proposal by ID."""
        await self.persistence.reject_policy_proposal(self.agent_id, proposal_id)
        self.notify_proposal_change(proposal_id)


def make_policy_engine(
    *,
    agent_id: AgentID,
    persistence: Persistence,
    docker_client: DockerClient,
    notifier: Callable[[str], None] | None = None,
    policy_source: str,
) -> ApprovalPolicyEngine:
    """Factory for ApprovalPolicyEngine with required context.

    Centralizes creation for wiring, CLI, and tests without hiding parameters.
    """
    return ApprovalPolicyEngine(
        notifier, docker_client=docker_client, agent_id=agent_id, persistence=persistence, policy_source=policy_source
    )

    # No set_context: engine must be constructed with required context

    # No in-engine tests; proposals/policies are validated by executing in Docker

    # No in-process decide helpers

    # No proposal APIs here; proposals handled by approval policy server/persistence

    # No seatbelt resolution or policy fields here; keep context transport-agnostic

    # Default repr is sufficient; no custom string/repr implementation


# No agent-level approval handler: Policy Gateway middleware enforces approvals.
