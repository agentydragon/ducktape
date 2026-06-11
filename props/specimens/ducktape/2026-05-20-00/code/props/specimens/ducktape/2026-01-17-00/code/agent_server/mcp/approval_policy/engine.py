"""PolicyEngine: Complete policy subsystem with servers, state, and middleware."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from typing import Any, Final, cast

import aiodocker
import pydantic_core
from fastmcp.client import Client
from fastmcp.resources import FunctionResource, ResourceTemplate
from fastmcp.server.context import ServerSession
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import FunctionTool, ToolResult
from jinja2 import Template
from mcp import McpError, types as mtypes
from mcp.types import ErrorData
from pydantic import AnyUrl, BaseModel

from agent_core.handler import AbortTurnDecision, ContinueDecision
from agent_server.agent_types import AgentID
from agent_server.approvals import ApprovalRequest, ApprovalToolCall
from agent_server.models.proposal_status import ProposalStatus
from agent_server.persist.types import ApprovalOutcome, Persistence
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policy_eval.container import ContainerPolicyEvaluator
from agent_server.policy_eval.runner import run_policy_source
from mcp_infra.constants import RUNTIME_MOUNT_PREFIX, UI_MOUNT_PREFIX
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.mcp_types import SimpleOk
from mcp_infra.naming import build_mcp_function
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

logger = logging.getLogger(__name__)


def _serialize_arguments_json(arguments: dict[str, Any] | None) -> str | None:
    """Serialize tool arguments to JSON string.

    Handles nested Pydantic models (like AnyUrl) via pydantic_core.to_json.
    Returns None for None to match MCP semantics.
    """
    if arguments is None:
        return None
    return pydantic_core.to_json(arguments, fallback=str).decode("utf-8")


# ============================================================================
# Reserved JSON-RPC Error Codes & Messages for Policy Gateway
# ============================================================================
# Policy gateway denials
POLICY_DENIED_ABORT_CODE: Final[int] = -32950
POLICY_DENIED_CONTINUE_CODE: Final[int] = -32951
POLICY_DENIED_ABORT_MSG: Final[str] = "policy_denied"
POLICY_DENIED_CONTINUE_MSG: Final[str] = "policy_denied_continue"

# Policy evaluator errors (when evaluator itself fails/times out)
POLICY_EVALUATOR_ERROR_CODE: Final[int] = -32953
POLICY_EVALUATOR_ERROR_MSG: Final[str] = "policy_evaluator_error"

# Backend misuse protection (prevents backends from spoofing policy denials)
POLICY_BACKEND_RESERVED_MISUSE_CODE: Final[int] = -32952
POLICY_BACKEND_RESERVED_MISUSE_MSG: Final[str] = "policy_backend_reserved_misuse"

# Gateway stamping (placed on error.data to mark origin)
POLICY_GATEWAY_STAMP_KEY: Final[str] = "adgn_policy_gateway"

# ============================================================================
# Tool and Resource Constants
# ============================================================================
# (No constants - use server.tool.name and server.resource.uri for SSOT)


# ---- Enums for consolidated tools ----


class CallDecision(StrEnum):
    """Decision for a pending tool call."""

    APPROVE = "approve"
    DENY_ABORT = "deny_abort"
    DENY_CONTINUE = "deny_continue"


class ProposalDecision(StrEnum):
    """Decision for a policy proposal."""

    APPROVE = "approve"
    REJECT = "reject"


# ---- Pydantic models ----


class CreateProposalArgs(OpenAIStrictModeBaseModel):
    content: str


class WithdrawProposalArgs(OpenAIStrictModeBaseModel):
    id: str


class ProposalDescriptor(BaseModel):
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None


class DecideCallArgs(OpenAIStrictModeBaseModel):
    call_id: str
    decision: CallDecision


class DecideProposalArgs(OpenAIStrictModeBaseModel):
    proposal_id: str
    decision: ProposalDecision


class SetPolicyTextArgs(OpenAIStrictModeBaseModel):
    """Direct policy set input for admin endpoint."""

    source: str


class PendingCallItem(BaseModel):
    """Pending call approval request exposed to UI."""

    call_id: str
    tool_key: str
    args_json: str | None = None


class PendingCallsResponse(BaseModel):
    """Response from pending://calls resource."""

    pending: list[PendingCallItem]


# ---- Private ApprovalHub (internal to PolicyEngine) ----


class _ApprovalHub:
    """In-process rendezvous for pending approval/decision events.

    Internal to PolicyEngine - not exposed publicly.
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._futures: dict[str, asyncio.Future[ContinueDecision | AbortTurnDecision]] = {}
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()
        self._on_change = on_change

    def _notify_change(self) -> None:
        self._on_change()

    async def await_decision(self, call_id: str, request: ApprovalRequest) -> ContinueDecision | AbortTurnDecision:
        async with self._lock:
            self._requests[call_id] = request
            fut = self._futures.get(call_id)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._futures[call_id] = fut
            self._notify_change()
        return await fut

    def resolve(self, call_id: str, decision: ContinueDecision | AbortTurnDecision) -> None:
        fut = self._futures.pop(call_id, None)
        self._requests.pop(call_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        self._notify_change()

    @property
    def pending(self) -> dict[str, ApprovalRequest]:
        """View of pending approval requests."""
        return self._requests


# ---- Gateway middleware helpers ----


def _raise_if_reserved_code(e: McpError, name: str) -> None:
    """Check if error uses reserved policy codes and raise appropriate error."""
    stamped: bool = False
    error = e.error
    code = error.code  # already int (guaranteed by ErrorData)
    msg = error.message  # already str (guaranteed by ErrorData)
    data = error.data
    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
        stamped = True
    if not stamped:
        for a in e.args:
            if isinstance(a, mtypes.ErrorData):
                data = a.data
                if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                    stamped = True
                    break
            if isinstance(a, dict):
                try:
                    ad = mtypes.ErrorData.model_validate(a)
                    data = ad.data
                    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                        stamped = True
                        break
                except (TypeError, ValueError):
                    # Pydantic validation failed - fall back to raw dict access
                    data = a.get("data")
                    if isinstance(data, dict) and data.get(POLICY_GATEWAY_STAMP_KEY) is True:
                        stamped = True
                        break

    if (
        stamped
        or (code in (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_CONTINUE_CODE, POLICY_EVALUATOR_ERROR_CODE))
        or (msg in (POLICY_DENIED_ABORT_MSG, POLICY_DENIED_CONTINUE_MSG, POLICY_EVALUATOR_ERROR_MSG))
    ):
        raise McpError(
            ErrorData(
                code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
                message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
                data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": code},
            )
        )


_DENIAL_MAP: dict[ApprovalDecision, tuple[int, str]] = {
    ApprovalDecision.DENY_ABORT: (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_ABORT_MSG),
    ApprovalDecision.DENY_CONTINUE: (POLICY_DENIED_CONTINUE_CODE, POLICY_DENIED_CONTINUE_MSG),
}


def _policy_denied_error(decision: ApprovalDecision, name: str, reason: str | None) -> McpError:
    code, msg = _DENIAL_MAP[decision]
    return McpError(
        ErrorData(
            code=code,
            message=msg,
            data={POLICY_GATEWAY_STAMP_KEY: True, "decision": str(decision), "name": name, "reason": reason},
        )
    )


# ---- PolicyGatewayMiddleware (internal to PolicyEngine) ----


class _PolicyGatewayMiddleware(Middleware):
    """Approval-enforcing middleware installed on the agent compositor.

    - Gates tools/call via policy evaluation
    - Denials raise explicit JSON-RPC errors using reserved codes/messages
    - ASK blocks until hub resolves to Continue/Abort
    """

    def __init__(
        self,
        *,
        hub: _ApprovalHub,
        evaluate_policy: Callable[[PolicyRequest], Awaitable[PolicyResponse]],
        record_outcome: Callable[[str, str, ApprovalOutcome], Awaitable[None]] | None = None,
    ) -> None:
        self._hub = hub
        self._evaluate = evaluate_policy
        self._record = record_outcome
        self._inflight: dict[str, str] = {}

    def has_inflight_calls(self) -> bool:
        """Check if there are any tool calls currently in flight."""
        return len(self._inflight) > 0

    def inflight_count(self) -> int:
        """Return the number of tool calls currently in flight."""
        return len(self._inflight)

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments
        tool_key = name

        # Evaluate policy
        logger.warning(f"[POLICY_MW] Evaluating policy for tool: {name}")
        try:
            decision_res = await self._evaluate(
                PolicyRequest(name=name, arguments_json=_serialize_arguments_json(arguments))
            )
            decision = decision_res.decision
            rationale = decision_res.rationale
            logger.warning(f"[POLICY_MW] Decision for {name}: {decision} ({rationale})")
        except Exception as e:
            logger.warning("policy evaluator error", exc_info=e)
            raise McpError(
                ErrorData(
                    code=POLICY_EVALUATOR_ERROR_CODE,
                    message=POLICY_EVALUATOR_ERROR_MSG,
                    data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "reason": f"{type(e).__name__}: {e}"},
                )
            ) from e

        logger.debug("Policy decision: %s → %s (%s)", name, decision, rationale or "")

        if decision is ApprovalDecision.ALLOW:
            if self._record is not None:
                await self._record("pg:" + uuid.uuid4().hex, tool_key, ApprovalOutcome.POLICY_ALLOW)

            call_id = uuid.uuid4().hex
            self._inflight[call_id] = tool_key
            try:
                return await call_next(context)
            except McpError as e:
                _raise_if_reserved_code(e, name)
                raise
            except Exception as e:
                s = str(e)
                if (
                    (POLICY_DENIED_ABORT_MSG in s)
                    or (POLICY_DENIED_CONTINUE_MSG in s)
                    or (POLICY_EVALUATOR_ERROR_MSG in s)
                ):
                    raise McpError(
                        ErrorData(
                            code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
                            message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
                            data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": "unknown"},
                        )
                    ) from e
                raise
            finally:
                self._inflight.pop(call_id, None)

        if decision is ApprovalDecision.DENY_ABORT:
            if self._record is not None:
                await self._record("pg:" + uuid.uuid4().hex, tool_key, ApprovalOutcome.POLICY_DENY_ABORT)
            raise _policy_denied_error(ApprovalDecision.DENY_ABORT, name, rationale)

        if decision is ApprovalDecision.DENY_CONTINUE:
            if self._record is not None:
                await self._record("pg:" + uuid.uuid4().hex, tool_key, ApprovalOutcome.POLICY_DENY_CONTINUE)
            raise _policy_denied_error(ApprovalDecision.DENY_CONTINUE, name, rationale)

        # ASK: block until resolved via hub
        call_id = "pg:" + uuid.uuid4().hex
        req = ApprovalRequest(
            tool_key=tool_key,
            tool_call=ApprovalToolCall(name=name, call_id=call_id, args_json=_serialize_arguments_json(arguments)),
        )
        decision_obj = await self._hub.await_decision(call_id, req)

        if isinstance(decision_obj, ContinueDecision):
            if self._record is not None:
                await self._record(call_id, tool_key, ApprovalOutcome.POLICY_ALLOW)
            try:
                return await call_next(context)
            except McpError as e:
                _raise_if_reserved_code(e, name)
                raise
        if isinstance(decision_obj, AbortTurnDecision):
            if self._record is not None:
                await self._record(call_id, tool_key, ApprovalOutcome.POLICY_DENY_ABORT)
            raise _policy_denied_error(ApprovalDecision.DENY_ABORT, name, decision_obj.reason)

        raise McpError(
            ErrorData(
                code=-32603,
                message="internal_error: unknown approval decision type",
                data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "decision_type": type(decision_obj).__name__},
            )
        )


# ---- Helper to load instructions ----


def _load_instructions(policy_uri: str) -> str:
    """Load and render instructions with embedded shared constants via Jinja2.

    Args:
        policy_uri: URI of the active policy resource (from server.active_policy_resource.uri)
    """
    raw = resources.files(__package__).joinpath("instructions.j2.md").read_text(encoding="utf-8")
    tmpl = Template(raw)
    rendered = tmpl.render(
        RUNTIME_MOUNT_PREFIX=RUNTIME_MOUNT_PREFIX,
        RUNTIME_EXEC_TOOL_NAME=ContainerExecServer.EXEC_TOOL_NAME,
        TRUSTED_POLICY_PATH=None,
        TRUSTED_POLICY_URL=policy_uri,
    )
    return str(rendered)


# ---- Policy Server Classes ----


class PolicyReaderServer(EnhancedFastMCP):
    """Policy reader server: resources + evaluate_policy tool."""

    # Resource attributes (stashed results of @resource decorator - single source of truth for URI access)
    active_policy_resource: FunctionResource
    proposal_item_resource: ResourceTemplate
    pending_calls_resource: FunctionResource

    # Typed tool attribute for canonical pattern compliance
    evaluate_policy_tool: FunctionTool

    def __init__(self, engine: PolicyEngine):
        """Initialize reader server with access to policy engine state.

        Args:
            engine: PolicyEngine instance providing policy state and evaluation logic
        """
        # URI constant for active policy resource (SSOT is server.active_policy_resource.uri after registration)
        policy_uri = "resource://approval-policy/policy.py"

        # Initialize server with instructions rendered using the policy URI
        super().__init__(name="reader", instructions=_load_instructions(policy_uri))

        # Register resources and stash the results
        def active_policy() -> str:
            return engine.get_policy()

        self.active_policy_resource = cast(
            FunctionResource, self.resource(policy_uri, name="policy.py", mime_type="text/x-python")(active_policy)
        )

        async def proposal_item(id: str) -> str:
            if (got := await engine.persistence.get_policy_proposal(engine.agent_id, id)) is None:
                raise KeyError(id)
            return got.content

        self.proposal_item_resource = cast(
            ResourceTemplate,
            self.resource("resource://approval-policy/proposals/{id}", name="proposal", mime_type="text/x-python")(
                proposal_item
            ),
        )

        def pending_calls() -> PendingCallsResponse:
            """List all pending tool call approval requests."""
            return PendingCallsResponse(
                pending=[
                    PendingCallItem(
                        call_id=call_id,
                        tool_key=req.tool_key,
                        args_json=req.tool_call.args_json if req.tool_call else None,
                    )
                    for call_id, req in engine._hub.pending.items()
                ]
            )

        self.pending_calls_resource = cast(
            FunctionResource,
            self.resource("pending://calls", name="pending_calls", mime_type="application/json")(pending_calls),
        )

        # Register tool with typed attribute
        async def evaluate_policy(input: PolicyRequest) -> PolicyResponse:
            """Evaluate a policy decision for a single tool call via Docker-backed evaluator."""
            return await engine._evaluate_policy(input)

        self.evaluate_policy_tool = self.flat_model()(evaluate_policy)


class PolicyProposerServer(EnhancedFastMCP):
    """Policy proposer server: create/withdraw proposal tools."""

    # Typed tool attributes
    create_proposal_tool: FunctionTool
    withdraw_proposal_tool: FunctionTool

    def __init__(self, engine: PolicyEngine):
        """Initialize proposer server with access to policy engine.

        Args:
            engine: PolicyEngine instance for creating/withdrawing proposals
        """
        super().__init__(name="policy_proposer", instructions=None)

        # Register tools with typed attributes
        async def create_proposal(input: CreateProposalArgs) -> ProposalDescriptor:
            """Create a new policy proposal and return its descriptor."""
            new_id = await engine.create_proposal(input.content)
            return ProposalDescriptor(
                id=new_id, status=ProposalStatus.PENDING, created_at=datetime.now(UTC), decided_at=None
            )

        async def withdraw_proposal(input: WithdrawProposalArgs) -> None:
            """Withdraw a pending policy proposal by id."""
            await engine.withdraw_proposal(input.id)

        self.create_proposal_tool = self.flat_model()(create_proposal)
        self.withdraw_proposal_tool = self.flat_model()(withdraw_proposal)


class PolicyAdminServer(EnhancedFastMCP):
    """Policy admin server: decide_call/decide_proposal/set_policy tools."""

    # Typed tool attributes
    decide_call_tool: FunctionTool
    decide_proposal_tool: FunctionTool
    set_policy_tool: FunctionTool

    def __init__(self, engine: PolicyEngine):
        """Initialize admin server with access to policy engine.

        Args:
            engine: PolicyEngine instance for approval decisions and policy management
        """
        super().__init__(name="admin", instructions=None)

        # Register tools with typed attributes
        async def decide_call(input: DecideCallArgs) -> SimpleOk:
            """Approve or deny a pending tool call."""
            call_id = input.call_id
            decision = input.decision

            if decision == CallDecision.APPROVE:
                engine._hub.resolve(call_id, ContinueDecision())
            elif decision == CallDecision.DENY_ABORT:
                engine._hub.resolve(call_id, AbortTurnDecision(reason="user_denied"))
            elif decision == CallDecision.DENY_CONTINUE:
                # Continue without executing - resolve with continue decision
                # The call is skipped but turn continues
                engine._hub.resolve(call_id, ContinueDecision())
            return SimpleOk()

        async def decide_proposal(input: DecideProposalArgs) -> SimpleOk:
            """Approve or reject a policy proposal."""
            proposal_id = input.proposal_id
            decision = input.decision

            if decision == ProposalDecision.APPROVE:
                await engine.approve_proposal(proposal_id)
            elif decision == ProposalDecision.REJECT:
                await engine.reject_proposal(proposal_id)
            return SimpleOk()

        async def set_policy(input: SetPolicyTextArgs) -> SimpleOk:
            """Directly set active policy text after self-check."""
            await engine.self_check(input.source)
            engine.set_policy(input.source)
            return SimpleOk()

        self.decide_call_tool = self.flat_model()(decide_call)
        self.decide_proposal_tool = self.flat_model()(decide_proposal)
        self.set_policy_tool = self.flat_model()(set_policy)


# ---- PolicyEngine ----


class PolicyEngine:
    """Complete policy subsystem - servers, state, and middleware.

    Owns:
    - reader: PolicyReaderServer with evaluate_policy, policy resources, pending://calls
    - proposer: PolicyProposerServer with propose/withdraw tools
    - admin: PolicyAdminServer with decide_call, decide_proposal, set_policy tools
    - _hub: Internal ApprovalHub for pending call coordination
    - _gateway: PolicyGatewayMiddleware to install on compositor
    """

    def __init__(
        self, *, agent_id: AgentID, persistence: Persistence, policy_source: str, docker_client: aiodocker.Docker
    ) -> None:
        # Policy state
        self._policy_source: str = policy_source
        self._policy_version: int = 1

        # Context for policy operations
        self.agent_id: AgentID = agent_id
        self.persistence: Persistence = persistence
        self._docker_client: aiodocker.Docker = docker_client

        # Background task tracking
        self._bg_tasks: set[asyncio.Task] = set()

        # Create hub with on_change callback that broadcasts pending://calls
        self._hub = _ApprovalHub(on_change=self._on_hub_change)

        # Create owned servers (now using extracted server classes)
        self.reader = PolicyReaderServer(self)
        self.proposer = PolicyProposerServer(self)
        self.admin = PolicyAdminServer(self)

        # Protocol-level resource subscriptions on reader
        self._session_subscriptions: defaultdict[ServerSession, set[AnyUrl]] = defaultdict(set)
        mcp_server = self.reader._mcp_server

        def _subscriptions() -> set[AnyUrl]:
            return self._session_subscriptions[mcp_server.request_context.session]

        @mcp_server.subscribe_resource()
        async def _subscribe(uri: AnyUrl):
            _subscriptions().add(uri)

        @mcp_server.unsubscribe_resource()
        async def _unsubscribe(uri: AnyUrl):
            _subscriptions().discard(uri)

        # Create gateway middleware (uses internal evaluate method)
        self._gateway = _PolicyGatewayMiddleware(
            hub=self._hub, evaluate_policy=self._evaluate_policy, record_outcome=self._record_outcome
        )

        # Internal reader client for gateway (created lazily)
        self._reader_client: Client | None = None

    @property
    def gateway(self) -> Middleware:
        """Middleware to install on agent compositor."""
        return self._gateway

    # ---- Internal methods ----

    def _on_hub_change(self) -> None:
        """Called when hub pending list changes - broadcast pending://calls."""
        task = asyncio.create_task(self._broadcast_resource_updated(self.reader.pending_calls_resource.uri))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _evaluate_policy(self, request: PolicyRequest) -> PolicyResponse:
        """Evaluate policy for a tool call via Docker-backed evaluator (uses injected client)."""
        logger.warning(f"[EVAL_START] Starting policy evaluation for {request.name}")
        evaluator = ContainerPolicyEvaluator(agent_id=self.agent_id, docker_client=self._docker_client, engine=self)
        logger.warning("[EVAL_EVALUATOR] Evaluator created, calling decide")
        result = await evaluator.decide(request)
        logger.warning(f"[EVAL_RESULT] Got result: {result.decision}")
        return result

    async def _record_outcome(self, call_id: str, tool_key: str, outcome: ApprovalOutcome) -> None:
        """Record approval outcome to persistence."""
        await self.persistence.record_approval(
            agent_id=self.agent_id, call_id=call_id, tool_key=tool_key, outcome=outcome, decided_at=datetime.now(UTC)
        )

    # ---- Policy state methods ----

    def get_policy(self) -> str:
        """Return current policy source."""
        return self._policy_source

    def set_policy(self, source: str) -> int:
        """Set new policy source and broadcast update."""
        self._policy_source = source
        self._policy_version += 1
        task = asyncio.create_task(self._broadcast_resource_updated(self.reader.active_policy_resource.uri))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return self._policy_version

    def load_policy(self, source: str, *, version: int) -> None:
        """Hydrate policy from persistence without broadcasting."""
        self._policy_source = source
        self._policy_version = version

    async def self_check(self, source: str) -> None:
        """Validate policy source by executing it in Docker (uses injected client)."""
        await run_policy_source(
            docker_client=self._docker_client,
            source=source,
            input_payload=PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, "send_message"), arguments_json=None),
        )

    # ---- Proposal management methods ----

    async def create_proposal(self, content: str) -> str:
        """Create a new policy proposal and return its ID."""
        await self.self_check(content)
        new_id = uuid.uuid4().hex
        await self.persistence.create_policy_proposal(self.agent_id, proposal_id=new_id, content=content)
        self._notify_proposal_change(new_id)
        return new_id

    async def withdraw_proposal(self, proposal_id: str) -> None:
        """Withdraw (delete) a pending policy proposal by ID."""
        await self.persistence.delete_policy_proposal(self.agent_id, proposal_id)
        self._notify_proposal_change(proposal_id)

    async def approve_proposal(self, proposal_id: str) -> None:
        """Approve a pending policy proposal by ID and activate it."""
        got = await self.persistence.get_policy_proposal(self.agent_id, proposal_id)
        if got is None:
            raise KeyError(proposal_id)
        await self.self_check(got.content)
        self.set_policy(got.content)
        await self.persistence.approve_policy_proposal(self.agent_id, proposal_id)
        self._notify_proposal_change(proposal_id)

    async def reject_proposal(self, proposal_id: str) -> None:
        """Reject a pending policy proposal by ID."""
        await self.persistence.reject_policy_proposal(self.agent_id, proposal_id)
        self._notify_proposal_change(proposal_id)

    # ---- Notification helpers ----

    def _notify_proposal_change(self, proposal_id: str) -> None:
        """Notify about a specific proposal change and the proposals index."""
        # Notify specific proposal
        uri = self.reader.proposal_item_resource.uri_template.format(id=proposal_id)
        task = asyncio.create_task(self._broadcast_resource_updated(uri))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        # Notify list changed for proposals collection
        list_task = asyncio.create_task(self._broadcast_list_changed())
        self._bg_tasks.add(list_task)
        list_task.add_done_callback(self._bg_tasks.discard)

    async def _broadcast_resource_updated(self, uri: AnyUrl | str) -> None:
        """Broadcast that a specific resource has been updated."""
        await self.reader.broadcast_resource_updated(uri)

    async def _broadcast_list_changed(self) -> None:
        """Broadcast that a resource list has changed."""
        await self.reader.broadcast_resource_list_changed()
