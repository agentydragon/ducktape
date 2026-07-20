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
import logging
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never, TypeVar, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi_csrf_protect import CsrfProtect
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
Csrf = Annotated[CsrfProtect, Depends()]


class ToolMetadata(BaseModel):
    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotations: mcp_types.ToolAnnotations | None = None
    icons: list[mcp_types.Icon] | None = None


class ServerMetadataBase(BaseModel):
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)


class AliveServerMetadata(ServerMetadataBase):
    status: Literal["alive"] = "alive"


class DegradedServerMetadata(ServerMetadataBase):
    status: Literal["degraded"] = "degraded"
    failure_stage: Literal["credential_resolution", "tool_discovery"]
    degraded_reason: str


type ServerMetadata = Annotated[AliveServerMetadata | DegradedServerMetadata, Field(discriminator="status")]


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
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ToolCallRecord]:
        with self._sessions.begin() as session:
            stmt = self._record_projection_stmt(actor)
            if since is not None:
                stmt = stmt.where(McpToolCall.updated_at > since)
            if statuses:
                stmt = stmt.where(McpToolCall.status.in_(statuses))
            # `newest_first` makes `limit` keep the most recent calls (the audit/history
            # view wants those); the default ascending order stays the queue-friendly
            # oldest-first for pending-approval reads.
            order = McpToolCall.created_at.desc() if newest_first else McpToolCall.created_at
            projections = session.execute(stmt.order_by(order).limit(limit)).tuples().all()
            return [self._record_from_projection(*projection) for projection in projections]

    def mark_running(self, tool_call_id: str, *, actor: OperatorActor) -> ToolCallRecord:
        return self._transition_pending_approval(tool_call_id, ToolCallStatus.RUNNING, actor=actor)

    def deny(self, tool_call_id: str, reason: str | None, *, actor: OperatorActor) -> ToolCallRecord:
        return self._transition_pending_approval(tool_call_id, ToolCallStatus.DENIED, actor=actor, denial_reason=reason)

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

    def _transition_pending_approval(
        self, tool_call_id: str, status: ToolCallStatus, *, actor: OperatorActor, denial_reason: str | None = None
    ) -> ToolCallRecord:
        operator_id = self._operator_id(actor)
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id, actor)
            principal = self._principal(session, tool_call_id)
            record = self._record_from_principal(row, principal)
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise ToolCallStateConflictError(f"tool call is not pending approval; status={record.status}")
            if status is ToolCallStatus.RUNNING:
                self._require_executable_principal(session, principal, operator_id)
            row.status = status
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.denial_reason = denial_reason
            if status == ToolCallStatus.RUNNING:
                row.approved_at = row.updated_at
            return self._record_from_principal(row, principal)

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

    @staticmethod
    def _operator_id(actor: OperatorActor) -> UUID:
        match actor:
            case OperatorActor(operator_id=operator_id):
                return operator_id
            case _:
                raise TypeError(f"operator actor required, got {type(actor).__name__}")


# TODO(naming): client-side dispatcher over fastmcp.client.Client, not a FastMCP Proxy/Provider
#   (those are server-side concepts). Revisit the name against FastMCP terminology.
class McpServerClient:
    """Reaches a configured MCP server (in-process or remote) for tool execution and metadata
    reflection, sharing one in-process registry and transport/Client lifecycle. The two operations
    differ only in call and error policy — `execute` raises on tool error; `metadata` degrades on
    transport error so one unreachable server can't break the capabilities listing."""

    def __init__(self, in_process_servers: InProcessServers | None = None) -> None:
        self._in_process = in_process_servers or {}

    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        transport, transport_auth = _transport(server, self._in_process, auth_token)
        async with Client(transport, auth=transport_auth) as client:
            result = await client.call_tool_mcp(tool_name, arguments)
        if result.isError:
            raise RuntimeError(_mcp_error_message(result))
        return _mcp_result_to_json(result)

    async def metadata(self, server: McpServerEntry, auth_token: str | None) -> ServerMetadata:
        try:
            transport, transport_auth = _transport(server, self._in_process, auth_token)
            async with Client(transport, auth=transport_auth) as client:
                tools = await client.list_tools()
        except Exception as e:
            logger.warning("MCP tool discovery failed for %s", server.id, exc_info=True)
            return DegradedServerMetadata(
                server_id=server.id, title=server.id, tools=[], failure_stage="tool_discovery", degraded_reason=str(e)
            )
        reflected: list[ToolMetadata] = []
        for tool in tools:
            schema = tool.inputSchema
            if not isinstance(schema, dict):
                schema = {}
            reflected.append(
                ToolMetadata(
                    name=tool.name,
                    title=tool.title,
                    description=tool.description,
                    input_schema=schema,
                    output_schema=tool.outputSchema,
                    annotations=tool.annotations,
                    icons=tool.icons,
                )
            )
        return AliveServerMetadata(server_id=server.id, title=server.id, tools=reflected)


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
) -> ServerMetadata:
    resolution = await _resolve_operator_metadata_auth(
        operator_id=operator_id, server=server, oauth_store=oauth_store, provider_store=provider_store
    )
    if isinstance(resolution, _DegradedAuth):
        return DegradedServerMetadata(
            server_id=server.id,
            title=server.id,
            tools=[],
            failure_stage="credential_resolution",
            degraded_reason=resolution.reason,
        )
    return await metadata_provider.metadata(server, resolution.token)


@router.get("/api/tool-calls")
async def list_tool_calls(
    service: ToolCallServiceDep,
    actor: OperatorActorDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: datetime.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    newest_first: bool = False,
) -> ToolCallListResponse:
    return ToolCallListResponse(
        tool_calls=service.list_tool_calls(
            actor=actor, statuses=status, since=since, limit=limit, newest_first=newest_first
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
    tool_call_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    csrf_protect: Csrf,
    service: ToolCallServiceDep,
    actor: OperatorActorDep,
) -> ApprovalDecisionResponse:
    await csrf_protect.validate_csrf(request)
    try:
        tool_call = await service.decide(tool_call_id=tool_call_id, decision=body, actor=actor)
    except BackendAccountNotConnectedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (McpServerNotFoundError, ToolCallNotFoundError, ToolCallStateConflictError) as error:
        _raise_tool_call_http_error(error)
    return ApprovalDecisionResponse(tool_call=tool_call)
