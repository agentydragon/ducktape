"""Operator-approved MCP tool calls owned by haku-console.

This module contains the FastAPI/wire adapter plus the current Postgres repository,
MCP executor, and metadata-reflection adapters. `ToolCallApplicationService` owns the
actor-scoped lifecycle: calls run immediately only when reviewed policy matches; all
others wait for an operator decision in trusted console chrome. The connected-server
catalog lives in `mcp_config`; operator OAuth account linkage lives in
`mcp_operator_oauth`.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never, TypeVar, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp.client import Client
from mcp import types as mcp_types
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from haku.console.agents.models import AgentStatus, CredentialBindingStatus
from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    CredentialBinding,
    McpToolCall,
    McpToolCallPrincipal,
    Operator,
)
from haku.console.mcp_config import (
    InProcessBackend,
    InProcessServers,
    McpServerEntry,
    McpServerNotFoundError,
    NoCredential,
    OperatorConnectionCredential,
    OperatorLoginIdentityCredential,
    RemoteServerOAuthAuth,
    StaticBearerAuth,
    _credential_token,
    _transport,
)
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.mcp_reflection_cache import ReflectionCache, ReflectionCacheKey
from haku.console.operator_auth import OperatorActorDep
from haku.console.operator_identity import OperatorStatus
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_call_service import (
    AuthentikOperatorTokenStore,
    BackendAccountNotConnectedError,
    ProviderConnectionTokenStore,
    ToolCallApplicationService,
    ToolCallNotFoundError,
    ToolCallStateConflictError,
    backend_auth_for_operator,
)
from haku.console.tool_calls import (
    AgentToolCallCaller,
    ApprovalDecisionRequest,
    OperatorToolCallCaller,
    SubmitToolCallRequest,
    ToolCallCaller,
    ToolCallRecord,
    ToolCallStatus,
)

logger = logging.getLogger(__name__)

# Operator-only routes (reflection, approvals, decisions, and audit history). app.py guards this router with
# `require_operator`.
router = APIRouter(tags=["mcp-approval"])


@dataclass(frozen=True)
class DegradedReflection:
    """A downstream server's tools couldn't be reflected right now — the *reason*, not a response
    shape. `server_metadata_response` is the only place this becomes the `degraded` API shape."""

    failure_stage: Literal["credential_resolution", "tool_discovery"]
    degraded_reason: str


# What one reflection attempt actually produced: the real upstream `mcp.types.Tool` objects
# (already validated by the MCP client), or why there aren't any. Internal consumers building
# proxy tools (`_build_proxy_tool`) read the real upstream type directly — no separate mirror to
# keep in sync as the proxy needs more of what the upstream tool declares.
type ServerReflection = list[mcp_types.Tool] | DegradedReflection


class ToolMetadata(BaseModel):
    """The curated, snake_case, wire-stable projection of one reflected tool — used only for the
    `get_mcp_server_status`/`list_mcp_servers` API response, never as internal plumbing. Deliberately
    narrower than `mcp.types.Tool`: that type allows arbitrary extra fields from the upstream server
    (`model_config = ConfigDict(extra="allow")`) and uses camelCase, neither of which the console's
    own API contract should inherit unfiltered from an untrusted third-party MCP server."""

    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotations: mcp_types.ToolAnnotations | None = None
    icons: list[mcp_types.Icon] | None = None


class AliveServerState(BaseModel):
    status: Literal["alive"] = "alive"
    tools: list[ToolMetadata] = Field(default_factory=list)


class DegradedServerState(BaseModel):
    status: Literal["degraded"] = "degraded"
    failure_stage: Literal["credential_resolution", "tool_discovery"]
    degraded_reason: str


type ServerState = Annotated[AliveServerState | DegradedServerState, Field(discriminator="status")]


class ServerMetadata(BaseModel):
    """The curated `get_mcp_server_status` response: identity (`server_id`/`title`) once, wrapping
    whichever state reflection produced — never duplicated across an alive/degraded variant pair."""

    server_id: str
    title: str
    state: ServerState


def _tool_metadata(tool: mcp_types.Tool) -> ToolMetadata:
    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
    return ToolMetadata(
        name=tool.name,
        title=tool.title,
        description=tool.description,
        input_schema=schema,
        output_schema=tool.outputSchema,
        annotations=tool.annotations,
        icons=tool.icons,
    )


def server_metadata_response(server_id: str, reflection: ServerReflection) -> ServerMetadata:
    """Project a raw reflection result into the curated API response shape. The only caller is
    `get_mcp_server_status`; every other reflection consumer works with `ServerReflection` directly."""
    state: ServerState = (
        DegradedServerState(failure_stage=reflection.failure_stage, degraded_reason=reflection.degraded_reason)
        if isinstance(reflection, DegradedReflection)
        else AliveServerState(tools=[_tool_metadata(tool) for tool in reflection])
    )
    return ServerMetadata(server_id=server_id, title=server_id, state=state)


class PendingApprovalsResponse(BaseModel):
    approvals: list[ToolCallRecord] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    tool_call: ToolCallRecord


@dataclass(frozen=True, slots=True)
class _OperatorToolCallPrincipal:
    operator_id: UUID


@dataclass(frozen=True, slots=True)
class _AgentToolCallPrincipal:
    binding_id: UUID
    agent_id: UUID
    operator_id: UUID
    display_name: str


type _ResolvedToolCallPrincipal = _OperatorToolCallPrincipal | _AgentToolCallPrincipal

_SelectRow = TypeVar("_SelectRow", bound=tuple[Any, ...])


class PostgresToolCallLedger:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Migrations are applied once at startup (haku.console.database_migrate.apply_migrations), not
        # here — constructing a ledger neither connects nor mutates schema. The engine/sessionmaker is
        # created once in create_app and shared across every store.
        self._sessions = sessions

    def submit(
        self,
        *,
        server: McpServerEntry,
        req: SubmitToolCallRequest,
        actor: ToolCallActor,
        auto_approval_policy_id: str | None = None,
        auto_approval_evaluation: str | None = None,
        auto_denial_reason: str | None = None,
    ) -> ToolCallRecord:
        with self._sessions.begin() as session:
            tool_call_id = f"tc_{secrets.token_hex(12)}"
            match actor:
                case AgentActor():
                    display_name = self._require_active_agent_binding(session, actor)
                    caller: ToolCallCaller = AgentToolCallCaller(agent_id=actor.agent_id, display_name=display_name)
                    principal = McpToolCallPrincipal(
                        tool_call_id=tool_call_id, operator_id=None, binding_id=actor.binding_id
                    )
                case OperatorActor():
                    self._require_active_operator(session, actor.operator_id)
                    caller = OperatorToolCallCaller()
                    principal = McpToolCallPrincipal(
                        tool_call_id=tool_call_id, operator_id=actor.operator_id, binding_id=None
                    )
                case _:
                    raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")
            now = datetime.datetime.now(datetime.UTC)
            if auto_denial_reason is not None:
                # Born-denied (schema-invalid on an owned in-process server): a full audit row
                # that never passes through PENDING_APPROVAL and never reaches the queue.
                assert auto_approval_policy_id is None, "a call cannot be both auto-approved and auto-denied"
                status = ToolCallStatus.DENIED
            elif auto_approval_policy_id is not None:
                status = ToolCallStatus.RUNNING
            else:
                status = ToolCallStatus.PENDING_APPROVAL
            record = ToolCallRecord(
                tool_call_id=tool_call_id,
                server_id=server.id,
                tool_name=req.tool_name,
                caller=caller,
                status=status,
                created_at=now,
                updated_at=now,
                arguments=req.arguments,
                rationale=req.rationale,
                title=req.title,
                denial_reason=auto_denial_reason,
                approval_policy_id=auto_approval_policy_id,
                auto_approval_evaluation=auto_approval_evaluation,
                approved_at=now if auto_approval_policy_id is not None else None,
            )
            session.add(self._row_from_record(record))
            session.flush()
            session.add(principal)
            return record

    def get(self, tool_call_id: str, *, actor: ToolCallActor) -> ToolCallRecord:
        with self._sessions.begin() as session:
            stmt = self._record_projection_stmt(actor).where(McpToolCall.tool_call_id == tool_call_id)
            projection = session.execute(stmt).tuples().first()
            if projection is None:
                raise ToolCallNotFoundError("tool call not found")
            return self._record_from_projection(*projection)

    def list_tool_calls(
        self,
        *,
        actor: ToolCallActor,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        auto_approved: bool | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ToolCallRecord]:
        with self._sessions.begin() as session:
            stmt = self._record_projection_stmt(actor)
            if since is not None:
                stmt = stmt.where(McpToolCall.updated_at > since)
            if statuses:
                stmt = stmt.where(McpToolCall.status.in_(statuses))
            if auto_approved is not None:
                # A call carries `approval_policy_id` only when the reviewed auto-approval
                # decision let it through at submission time (`submit`, below); it is never set
                # or cleared afterward.
                condition = McpToolCall.approval_policy_id.isnot(None)
                stmt = stmt.where(condition if auto_approved else ~condition)
            # `newest_first` makes `limit` keep the most recent calls (the audit/history
            # view wants those); the default ascending order stays the queue-friendly
            # oldest-first for pending-approval reads.
            order = McpToolCall.created_at.desc() if newest_first else McpToolCall.created_at
            projections = session.execute(stmt.order_by(order).limit(limit)).tuples().all()
            return [self._record_from_projection(*projection) for projection in projections]

    def mark_running(self, tool_call_id: str, *, actor: OperatorActor) -> ToolCallRecord:
        operator = self._require_operator_actor(actor)
        with self._sessions.begin() as session:
            row, principal = self._lock_pending(session, tool_call_id, operator)
            self._require_executable_principal(session, principal, operator.operator_id)
            row.status = ToolCallStatus.RUNNING
            row.updated_at = row.approved_at = datetime.datetime.now(datetime.UTC)
            return self._record_from_principal(row, principal)

    def deny(self, tool_call_id: str, reason: str | None, *, actor: OperatorActor) -> ToolCallRecord:
        operator = self._require_operator_actor(actor)
        with self._sessions.begin() as session:
            row, principal = self._lock_pending(session, tool_call_id, operator)
            row.status = ToolCallStatus.DENIED
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.denial_reason = reason
            return self._record_from_principal(row, principal)

    def withdraw(self, tool_call_id: str, reason: str | None, *, actor: AgentActor) -> ToolCallRecord:
        """Retract the Agent's own still-pending call.

        Scoped at the Agent rather than the exact credential binding (unlike `finish` /
        `authorize_execution`, which gate external execution against the binding that queued the
        work): withdrawal only moves a call toward a terminal state, and an Agent that reconnected
        under a successor binding must still be able to clear its predecessor's ask out of the
        operator's queue.
        """
        agent = self._require_agent_actor(actor)
        with self._sessions.begin() as session:
            row, principal = self._lock_pending(session, tool_call_id, agent)
            self._require_active_agent_binding(session, agent)
            row.status = ToolCallStatus.WITHDRAWN
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.withdrawal_reason = reason
            return self._record_from_principal(row, principal)

    def finish(
        self, tool_call_id: str, *, actor: ToolCallActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord:
        if (result is None) == (error is None):
            raise ValueError("finish requires exactly one of result or error")
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id, actor)
            principal = self._principal(session, tool_call_id)
            if isinstance(actor, AgentActor) and (
                not isinstance(principal, _AgentToolCallPrincipal) or principal.binding_id != actor.binding_id
            ):
                raise ToolCallStateConflictError("tool call was not submitted by this credential binding")
            current = self._record_from_principal(row, principal)
            if current.status != ToolCallStatus.RUNNING:
                raise ToolCallStateConflictError(f"tool call is not running; status={current.status}")
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            row.status = status
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.result_json = result
            row.error = error
            return self._record_from_principal(row, principal)

    def authorize_execution(self, tool_call_id: str, *, actor: ToolCallActor) -> UUID:
        """Revalidate the exact durable principal immediately before external execution."""
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id, actor)
            if row.status is not ToolCallStatus.RUNNING:
                raise ToolCallStateConflictError(f"tool call is not running; status={row.status}")
            principal = self._principal(session, tool_call_id)
            match actor:
                case AgentActor():
                    if not isinstance(principal, _AgentToolCallPrincipal) or principal.binding_id != actor.binding_id:
                        raise ToolCallStateConflictError("tool call was not submitted by this credential binding")
                    self._require_active_agent_binding(session, actor)
                    return actor.operator_id
                case OperatorActor():
                    return self._require_executable_principal(session, principal, actor.operator_id)
                case _:
                    raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    # The annotations already say which actor each exit belongs to; these re-check it at runtime
    # because the ledger is reachable from adapters that resolve an actor dynamically. Each caller
    # uses the narrowed value, so neither is a bare assertion.
    @staticmethod
    def _require_operator_actor(actor: ToolCallActor) -> OperatorActor:
        match actor:
            case OperatorActor():
                return actor
            case _:
                raise TypeError(f"operator actor required, got {type(actor).__name__}")

    @staticmethod
    def _require_agent_actor(actor: ToolCallActor) -> AgentActor:
        match actor:
            case AgentActor():
                return actor
            case _:
                raise TypeError(f"agent actor required, got {type(actor).__name__}")

    def _lock_pending(
        self, session: Session, tool_call_id: str, actor: ToolCallActor
    ) -> tuple[McpToolCall, _ResolvedToolCallPrincipal]:
        """Lock the actor's call and assert it is still pending, for one of its three exits.

        The `SELECT ... FOR UPDATE` in `_row_by_tool_call_id` is what serializes operator-approve
        against agent-withdraw: the loser re-reads the committed row and fails the check below
        naming the winner's status. Each caller keeps its own actor type, so approve/deny stay
        operator verbs and withdraw stays the requester's own.
        """
        row = self._row_by_tool_call_id(session, tool_call_id, actor)
        principal = self._principal(session, tool_call_id)
        record = self._record_from_principal(row, principal)
        if record.status != ToolCallStatus.PENDING_APPROVAL:
            raise ToolCallStateConflictError(f"tool call is not pending approval; status={record.status}")
        return row, principal

    def _row_by_tool_call_id(self, session: Session, tool_call_id: str, actor: ToolCallActor) -> McpToolCall:
        stmt = self._scope_to_actor(select(McpToolCall).where(McpToolCall.tool_call_id == tool_call_id), actor)
        row = session.scalars(stmt.with_for_update(of=McpToolCall)).first()
        if row is None:
            raise ToolCallNotFoundError("tool call not found")
        return row

    @staticmethod
    def _scope_to_actor(stmt: Select[_SelectRow], actor: ToolCallActor) -> Select[_SelectRow]:
        """Apply the one canonical tool-call ownership predicate to reads and locked writes."""
        stmt = (
            stmt.join(McpToolCallPrincipal, McpToolCallPrincipal.tool_call_id == McpToolCall.tool_call_id)
            .outerjoin(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .outerjoin(Agent, Agent.agent_id == CredentialBinding.agent_id)
        )
        match actor:
            case AgentActor(agent_id=agent_id):
                return stmt.where(Agent.agent_id == agent_id)
            case OperatorActor(operator_id=operator_id):
                return stmt.where(
                    or_(McpToolCallPrincipal.operator_id == operator_id, Agent.owner_operator_id == operator_id)
                )
            case _:
                raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    @classmethod
    def _record_projection_stmt(
        cls, actor: ToolCallActor
    ) -> Select[tuple[McpToolCall, McpToolCallPrincipal, UUID, UUID, str]]:
        """Select a scoped call together with the exact durable principal used to render it."""
        return cls._scope_to_actor(
            select(
                McpToolCall,
                McpToolCallPrincipal,
                CredentialBinding.agent_id,
                Agent.owner_operator_id,
                AgentNameReservation.display_name,
            ),
            actor,
        ).outerjoin(
            AgentNameReservation,
            and_(
                AgentNameReservation.agent_id == Agent.agent_id,
                AgentNameReservation.reservation_id == Agent.current_name_reservation_id,
            ),
        )

    @staticmethod
    def _row_from_record(record: ToolCallRecord) -> McpToolCall:
        return McpToolCall(
            tool_call_id=record.tool_call_id,
            server_id=record.server_id,
            tool_name=record.tool_name,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            arguments_json=record.arguments,
            rationale=record.rationale,
            title=record.title,
            result_json=record.result,
            error=record.error,
            denial_reason=record.denial_reason,
            withdrawal_reason=record.withdrawal_reason,
            approval_policy_id=record.approval_policy_id,
            auto_approval_evaluation=record.auto_approval_evaluation,
            approved_at=record.approved_at,
        )

    @classmethod
    def _record_from_projection(
        cls,
        row: McpToolCall,
        principal_row: McpToolCallPrincipal,
        agent_id: UUID | None,
        operator_id: UUID | None,
        display_name: str | None,
    ) -> ToolCallRecord:
        principal = cls._resolve_principal(
            row.tool_call_id, principal_row, agent_id=agent_id, operator_id=operator_id, display_name=display_name
        )
        return cls._record_from_principal(row, principal)

    @staticmethod
    def _record_from_principal(row: McpToolCall, principal: _ResolvedToolCallPrincipal) -> ToolCallRecord:
        caller: ToolCallCaller
        if isinstance(principal, _OperatorToolCallPrincipal):
            caller = OperatorToolCallCaller()
        else:
            caller = AgentToolCallCaller(agent_id=principal.agent_id, display_name=principal.display_name)
        return ToolCallRecord(
            tool_call_id=row.tool_call_id,
            server_id=row.server_id,
            tool_name=row.tool_name,
            caller=caller,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            arguments=row.arguments_json,
            rationale=row.rationale,
            title=row.title,
            result=row.result_json,
            error=row.error,
            denial_reason=row.denial_reason,
            withdrawal_reason=row.withdrawal_reason,
            approval_policy_id=row.approval_policy_id,
            auto_approval_evaluation=row.auto_approval_evaluation,
            approved_at=row.approved_at,
        )

    @staticmethod
    def _principal(session: Session, tool_call_id: str) -> _ResolvedToolCallPrincipal:
        result = session.execute(
            select(
                McpToolCallPrincipal,
                CredentialBinding.agent_id,
                Agent.owner_operator_id,
                AgentNameReservation.display_name,
            )
            .outerjoin(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .outerjoin(Agent, Agent.agent_id == CredentialBinding.agent_id)
            .outerjoin(
                AgentNameReservation,
                and_(
                    AgentNameReservation.agent_id == Agent.agent_id,
                    AgentNameReservation.reservation_id == Agent.current_name_reservation_id,
                ),
            )
            .where(McpToolCallPrincipal.tool_call_id == tool_call_id)
        ).first()
        if result is None:
            raise RuntimeError(f"tool call {tool_call_id!r} has no durable principal")
        row, agent_id, operator_id, display_name = result
        return PostgresToolCallLedger._resolve_principal(
            tool_call_id, row, agent_id=agent_id, operator_id=operator_id, display_name=display_name
        )

    @staticmethod
    def _resolve_principal(
        tool_call_id: str,
        row: McpToolCallPrincipal,
        *,
        agent_id: UUID | None,
        operator_id: UUID | None,
        display_name: str | None,
    ) -> _ResolvedToolCallPrincipal:
        if row.operator_id is not None:
            if row.binding_id is not None:
                raise RuntimeError(f"tool call {tool_call_id!r} has contradictory principal variants")
            return _OperatorToolCallPrincipal(operator_id=row.operator_id)
        if row.binding_id is None or agent_id is None or operator_id is None or display_name is None:
            raise RuntimeError(f"tool call {tool_call_id!r} has an incomplete agent principal")
        return _AgentToolCallPrincipal(
            binding_id=row.binding_id, agent_id=agent_id, operator_id=operator_id, display_name=display_name
        )

    @staticmethod
    def _require_active_operator(session: Session, operator_id: UUID) -> None:
        found = session.scalar(
            select(Operator.operator_id)
            .where(Operator.operator_id == operator_id, Operator.status == OperatorStatus.ACTIVE)
            .with_for_update()
        )
        if found is None:
            raise ToolCallStateConflictError("operator is not active")

    @staticmethod
    def _require_active_agent_binding(session: Session, actor: AgentActor) -> str:
        display_name = session.scalar(
            select(AgentNameReservation.display_name)
            .select_from(CredentialBinding)
            .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
            .join(Operator, Operator.operator_id == Agent.owner_operator_id)
            .join(
                AgentNameReservation,
                and_(
                    AgentNameReservation.agent_id == Agent.agent_id,
                    AgentNameReservation.reservation_id == Agent.current_name_reservation_id,
                ),
            )
            .where(
                CredentialBinding.binding_id == actor.binding_id,
                CredentialBinding.agent_id == actor.agent_id,
                CredentialBinding.status == CredentialBindingStatus.ACTIVE,
                Agent.agent_id == actor.agent_id,
                Agent.owner_operator_id == actor.operator_id,
                Agent.status == AgentStatus.ACTIVE,
                Operator.operator_id == actor.operator_id,
                Operator.status == OperatorStatus.ACTIVE,
            )
            .with_for_update()
        )
        if display_name is None:
            raise ToolCallStateConflictError("agent credential binding is not active")
        return display_name

    def _require_executable_principal(
        self, session: Session, principal: _ResolvedToolCallPrincipal, operator_id: UUID
    ) -> UUID:
        if isinstance(principal, _OperatorToolCallPrincipal):
            if principal.operator_id != operator_id:
                raise ToolCallNotFoundError("tool call not found")
            self._require_active_operator(session, operator_id)
            return operator_id
        if principal.operator_id != operator_id:
            raise ToolCallNotFoundError("tool call not found")
        self._require_active_agent_binding(
            session,
            AgentActor(agent_id=principal.agent_id, operator_id=principal.operator_id, binding_id=principal.binding_id),
        )
        return operator_id


# TODO(naming): client-side dispatcher over fastmcp.client.Client, not a FastMCP Proxy/Provider
#   (those are server-side concepts). Revisit the name against FastMCP terminology.
class McpServerClient:
    """Reaches a configured MCP server (in-process or remote) for tool execution and metadata
    reflection, sharing one in-process registry and transport/Client lifecycle. The two operations
    differ only in call and error policy — `execute` raises on tool error; `metadata` degrades on
    transport error so one unreachable server can't break the capabilities listing."""

    def __init__(
        self,
        in_process_servers: InProcessServers | None = None,
        catalog_cache_ttl_seconds: float = 0.0,
        # A TTL of 0 still collapses concurrent reflections of the same server; only reuse across
        # requests is disabled. See `mcp_reflection_cache`.
    ) -> None:
        self._in_process = in_process_servers or {}
        self._catalogs = ReflectionCache(catalog_cache_ttl_seconds)

    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        transport, transport_auth = _transport(server, self._in_process, auth_token)
        async with Client(transport, auth=transport_auth) as client:
            result = await client.call_tool_mcp(tool_name, arguments)
        if result.isError:
            raise RuntimeError(_mcp_error_message(result))
        return _mcp_result_to_json(result)

    async def metadata(self, server: McpServerEntry, auth_token: str | None) -> ServerReflection:
        try:
            # A raise propagates out of the cache, so only successful catalogs are ever stored and
            # a recovered server is retried on the next listing rather than staying degraded.
            return await self._catalogs.reflect(
                _reflection_cache_key(server, auth_token), lambda: self._reflect(server, auth_token)
            )
        except Exception as e:
            logger.warning("MCP tool discovery failed for %s", server.id, exc_info=True)
            return DegradedReflection(failure_stage="tool_discovery", degraded_reason=str(e))

    async def _reflect(self, server: McpServerEntry, auth_token: str | None) -> list[mcp_types.Tool]:
        transport, transport_auth = _transport(server, self._in_process, auth_token)
        async with Client(transport, auth=transport_auth) as client:
            tools: list[mcp_types.Tool] = await client.list_tools()
            return tools


def _reflection_cache_key(server: McpServerEntry, auth_token: str | None) -> ReflectionCacheKey:
    return ReflectionCacheKey(
        server_id=server.id,
        config_fingerprint=_fingerprint(server.model_dump_json()),
        # Digest, not the credential: a cached catalog must belong to exactly the credential that
        # fetched it, but the key itself is ordinary in-memory state and must not hold a bearer.
        credential_fingerprint="unauthenticated" if auth_token is None else _fingerprint(auth_token),
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _mcp_result_to_json(result: mcp_types.CallToolResult) -> dict[str, Any]:
    return cast(dict[str, Any], result.model_dump(mode="json", by_alias=True, exclude_none=True))


def _mcp_error_message(result: mcp_types.CallToolResult) -> str:
    text_blocks = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    return "\n".join(text_blocks) or "MCP tool returned isError=true"


async def _execution_auth(
    server: McpServerEntry,
    operator_id: UUID,
    oauth_store: PostgresMcpOperatorOAuthStore,
    provider_store: ProviderConnectionTokenStore,
    authentik_store: AuthentikOperatorTokenStore,
) -> str | None:
    try:
        return await backend_auth_for_operator(
            server=server,
            operator_id=operator_id,
            oauth_store=oauth_store,
            provider_store=provider_store,
            authentik_store=authentik_store,
        )
    except BackendAccountNotConnectedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _tool_call_service(request: Request) -> ToolCallApplicationService:
    return cast(ToolCallApplicationService, request.app.state.tool_call_service)


ToolCallServiceDep = Annotated[ToolCallApplicationService, Depends(_tool_call_service)]


def _raise_tool_call_http_error(
    error: McpServerNotFoundError | ToolCallNotFoundError | ToolCallStateConflictError,
) -> Never:
    status_code = 409 if isinstance(error, ToolCallStateConflictError) else 404
    raise HTTPException(status_code=status_code, detail=str(error)) from error


@dataclass(frozen=True, slots=True)
class _ResolvedAuth:
    """A reflection auth token (None for a credential-free server) resolved for the acting operator."""

    token: str | None


@dataclass(frozen=True, slots=True)
class _DegradedAuth:
    """Reflection cannot proceed for the acting operator; the server renders degraded with this reason."""

    reason: str


async def _resolve_operator_metadata_auth(
    *,
    operator_id: UUID,
    server: McpServerEntry,
    oauth_store: PostgresMcpOperatorOAuthStore,
    provider_store: ProviderConnectionTokenStore,
) -> _ResolvedAuth | _DegradedAuth:
    """Resolve reflection readiness per the server's backend credential, or a degraded
    reason.

    Deviation from `backend_auth_for_operator` (which dispatches on the same variants): a
    missing operator-linked token or a missing static credential degrades reflection here rather than
    raising.
    """
    credential = server.backend.credential if isinstance(server.backend, InProcessBackend) else server.backend.auth
    match credential:
        case OperatorConnectionCredential(connection=connection):
            if not provider_store.is_provisioned(connection=connection):
                return _DegradedAuth(
                    f"OAuth client for {connection} is not provisioned on this console; "
                    "see the console deployment README."
                )
            if not provider_store.is_connected(connection=connection, operator_id=operator_id):
                return _DegradedAuth(f"Connect your {connection} account in the console to use this server.")
            # The implementation owns its schemas and tools/list invokes no backend operation.
            return _ResolvedAuth(None)
        case RemoteServerOAuthAuth():
            try:
                auth_token = await oauth_store.access_token_for(server=server, operator_id=operator_id)
            except Exception as error:
                logger.warning("MCP credential resolution failed for %s", server.id, exc_info=True)
                return _DegradedAuth(str(error))
            if not auth_token:
                return _DegradedAuth(
                    f"Connect your {server.id} MCP account in the console to reflect this server's tools."
                )
            return _ResolvedAuth(auth_token)
        case OperatorLoginIdentityCredential():
            # Reflection (tools/list) doesn't need the per-host token — the in-process hostexec
            # server lists its tools regardless — so reflect with no token and never degrade here.
            # The operator's identity token is required only at execution (backend_auth_for_operator).
            return _ResolvedAuth(None)
        case StaticBearerAuth(bearer_token_secret=secret):
            try:
                return _ResolvedAuth(_credential_token(server.id, secret))
            except Exception as e:
                return _DegradedAuth(str(e))
        case NoCredential():
            return _ResolvedAuth(None)


async def metadata_for_operator(
    *,
    operator_id: UUID,
    server: McpServerEntry,
    metadata_provider: McpServerClient,
    oauth_store: PostgresMcpOperatorOAuthStore,
    provider_store: ProviderConnectionTokenStore,
) -> ServerReflection:
    resolution = await _resolve_operator_metadata_auth(
        operator_id=operator_id, server=server, oauth_store=oauth_store, provider_store=provider_store
    )
    if isinstance(resolution, _DegradedAuth):
        return DegradedReflection(failure_stage="credential_resolution", degraded_reason=resolution.reason)
    return await metadata_provider.metadata(server, resolution.token)


@router.get("/api/tool-calls")
async def list_tool_calls(
    service: ToolCallServiceDep,
    actor: OperatorActorDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: datetime.datetime | None = None,
    auto_approved: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    newest_first: bool = False,
) -> ToolCallListResponse:
    return ToolCallListResponse(
        tool_calls=service.list_tool_calls(
            actor=actor,
            statuses=status,
            since=since,
            auto_approved=auto_approved,
            limit=limit,
            newest_first=newest_first,
        )
    )


@router.get("/api/tool-calls/{tool_call_id}")
async def get_tool_call(tool_call_id: str, service: ToolCallServiceDep, actor: OperatorActorDep) -> ToolCallRecord:
    try:
        return service.get(tool_call_id, actor=actor)
    except (ToolCallNotFoundError, ToolCallStateConflictError) as error:
        _raise_tool_call_http_error(error)


@router.get("/api/approvals/pending")
async def pending_approvals(service: ToolCallServiceDep, actor: OperatorActorDep) -> PendingApprovalsResponse:
    return PendingApprovalsResponse(approvals=service.pending_approvals(actor=actor))


@router.post("/api/tool-calls/{tool_call_id}/decision")
async def decide_approval(
    tool_call_id: str, body: ApprovalDecisionRequest, service: ToolCallServiceDep, actor: OperatorActorDep
) -> ApprovalDecisionResponse:
    try:
        tool_call = await service.decide(tool_call_id=tool_call_id, decision=body, actor=actor)
    except BackendAccountNotConnectedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (McpServerNotFoundError, ToolCallNotFoundError, ToolCallStateConflictError) as error:
        _raise_tool_call_http_error(error)
    return ApprovalDecisionResponse(tool_call=tool_call)
