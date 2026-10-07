"""Operator approval and history routes for MCP tool calls stored by haku-console.

The request protocol and catalog reflection are retired. This module keeps the REST approval and
history API plus execution of already-approved rows while the ledger drains.
"""

from __future__ import annotations

import datetime
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Never, TypeVar, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp.client import Client
from mcp import types as mcp_types
from pydantic import BaseModel, Field
from sqlalchemy import and_, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    CredentialBinding,
    McpToolCall,
    McpToolCallPrincipal,
    Operator,
)
from haku.console.identity.agent import CredentialBindingStatus
from haku.console.identity.authorization import lock_active_agent_binding
from haku.console.identity.operator_auth import OperatorActorDep
from haku.console.identity.operator_identity import OperatorStatus
from haku.console.mcp.execution import McpExecutionContext, mcp_execution_request_meta
from haku.console.mcp.tool_call_service import (
    ToolCallApplicationService,
    ToolCallExecutionAuthorization,
    ToolCallNotFoundError,
    ToolCallPageCursor,
    ToolCallStateConflictError,
)
from haku.console.mcp_config import InProcessServers, McpServerEntry, McpServerNotFoundError, _in_process_server
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    AgentToolCallCaller,
    ApprovalDecisionRequest,
    OperatorToolCallCaller,
    SubmitToolCallRequest,
    ToolCallCaller,
    ToolCallPayloadField,
    ToolCallRecord,
    ToolCallStatus,
)

# Operator-only routes (approvals, decisions, and audit history). app.py guards this router with
# `require_operator`.
router = APIRouter(tags=["mcp-approval"])


class PendingApprovalsResponse(BaseModel):
    approvals: list[ToolCallRecord] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque position to pass back as `cursor` for the next page, or null once this page is "
            "the last one. Its encoding is the server's; a client only echoes it back."
        ),
    )


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
    session_id: UUID | None


type _ResolvedToolCallPrincipal = _OperatorToolCallPrincipal | _AgentToolCallPrincipal

_SelectRow = TypeVar("_SelectRow", bound=tuple[Any, ...])


class PostgresToolCallLedger:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        # Migrations run in the image-coupled release Job (haku.console.database_migrate.apply_migrations), not
        # here — constructing a ledger neither connects nor mutates schema. The engine/sessionmaker is
        # created once in create_app and shared across every store.
        self._sessions = sessions

    async def submit(
        self,
        *,
        server: McpServerEntry,
        req: SubmitToolCallRequest,
        actor: RuntimeActor,
        auto_approval_policy_id: str | None = None,
        auto_approval_evaluation: str | None = None,
        auto_denial_reason: str | None = None,
    ) -> ToolCallRecord:
        async with self._sessions.begin() as session:
            tool_call_id = f"tc_{secrets.token_hex(12)}"
            match actor:
                case AgentActor():
                    display_name, _ = await self._require_active_agent_binding(session, actor)
                    caller: ToolCallCaller = AgentToolCallCaller(
                        agent_id=actor.agent_id, display_name=display_name, session_id=None
                    )
                    principal = McpToolCallPrincipal(
                        tool_call_id=tool_call_id, operator_id=None, binding_id=actor.binding_id
                    )
                case OperatorActor():
                    await self._require_active_operator(session, actor.operator_id)
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
                decision_note=auto_denial_reason,
                approval_policy_id=auto_approval_policy_id,
                auto_approval_evaluation=auto_approval_evaluation,
                approved_at=now if auto_approval_policy_id is not None else None,
            )
            session.add(self._row_from_record(record))
            await session.flush()
            session.add(principal)
            return record

    async def get(
        self, tool_call_id: str, *, actor: RuntimeActor, fields: Collection[ToolCallPayloadField] | None = None
    ) -> ToolCallRecord:
        async with self._sessions.begin() as session:
            stmt = self._projection_stmt(actor, fields).where(McpToolCall.tool_call_id == tool_call_id)
            projection = (await session.execute(stmt)).mappings().first()
            if projection is None:
                raise ToolCallNotFoundError("tool call not found")
            return self._record_from_mapping(projection, fields=fields)

    async def list_tool_calls(
        self,
        *,
        actor: RuntimeActor,
        fields: Collection[ToolCallPayloadField] | None = None,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        auto_approved: bool | None = None,
        limit: int = 100,
        newest_first: bool = False,
        cursor: ToolCallPageCursor | None = None,
    ) -> list[ToolCallRecord]:
        async with self._sessions.begin() as session:
            stmt = self._projection_stmt(actor, fields)
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
            # oldest-first for pending-approval reads. `tool_call_id` makes either order total,
            # so a keyset page boundary can't fall inside a group of same-instant calls.
            position = tuple_(McpToolCall.created_at, McpToolCall.tool_call_id)
            order = (
                (McpToolCall.created_at.desc(), McpToolCall.tool_call_id.desc())
                if newest_first
                else (McpToolCall.created_at, McpToolCall.tool_call_id)
            )
            if cursor is not None:
                boundary = tuple_(literal(cursor.created_at), literal(cursor.tool_call_id))
                stmt = stmt.where(position < boundary if newest_first else position > boundary)
            result = await session.execute(stmt.order_by(*order).limit(limit))
            projections = result.mappings().all()
            return [self._record_from_mapping(projection, fields=fields) for projection in projections]

    async def mark_running(
        self, tool_call_id: str, decision_note: str | None = None, *, actor: OperatorActor
    ) -> ToolCallRecord:
        operator = self._require_operator_actor(actor)
        async with self._sessions.begin() as session:
            row, principal = await self._lock_pending(session, tool_call_id, operator)
            await self._require_executable_principal(session, principal, operator.operator_id)
            row.status = ToolCallStatus.RUNNING
            row.updated_at = row.approved_at = datetime.datetime.now(datetime.UTC)
            row.decision_note = decision_note
            row.decision_operator_id = operator.operator_id
            return self._record_from_principal(row, principal)

    async def deny(
        self, tool_call_id: str, decision_note: str | None = None, *, actor: OperatorActor
    ) -> ToolCallRecord:
        operator = self._require_operator_actor(actor)
        async with self._sessions.begin() as session:
            row, principal = await self._lock_pending(session, tool_call_id, operator)
            row.status = ToolCallStatus.DENIED
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.decision_note = decision_note
            row.decision_operator_id = operator.operator_id
            return self._record_from_principal(row, principal)

    async def withdraw(self, tool_call_id: str, reason: str | None, *, actor: AgentActor) -> ToolCallRecord:
        """Retract the Agent's own still-pending call.

        Scoped at the Agent rather than the exact credential binding (unlike `finish` /
        `authorize_execution`, which gate external execution against the binding that queued the
        work): withdrawal only moves a call toward a terminal state, and an Agent that reconnected
        under a successor binding must still be able to clear its predecessor's ask out of the
        operator's queue.
        """
        agent = self._require_agent_actor(actor)
        async with self._sessions.begin() as session:
            row, principal = await self._lock_pending(session, tool_call_id, agent)
            await self._require_active_agent_binding(session, agent)
            row.status = ToolCallStatus.WITHDRAWN
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.withdrawal_reason = reason
            return self._record_from_principal(row, principal)

    async def finish(
        self, tool_call_id: str, *, actor: RuntimeActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord:
        if (result is None) == (error is None):
            raise ValueError("finish requires exactly one of result or error")
        async with self._sessions.begin() as session:
            row = await self._row_by_tool_call_id(session, tool_call_id, actor)
            principal = await self._principal(session, tool_call_id)
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

    async def authorize_execution(self, tool_call_id: str, *, actor: RuntimeActor) -> ToolCallExecutionAuthorization:
        """Revalidate the exact durable principal immediately before external execution."""
        async with self._sessions.begin() as session:
            row = await self._row_by_tool_call_id(session, tool_call_id, actor)
            if row.status is not ToolCallStatus.RUNNING:
                raise ToolCallStateConflictError(f"tool call is not running; status={row.status}")
            principal = await self._principal(session, tool_call_id)
            match actor:
                case AgentActor():
                    if not isinstance(principal, _AgentToolCallPrincipal) or principal.binding_id != actor.binding_id:
                        raise ToolCallStateConflictError("tool call was not submitted by this credential binding")
                    _, access_profile_id = await self._require_active_agent_binding(session, actor)
                    return ToolCallExecutionAuthorization(
                        operator_id=actor.operator_id,
                        caller=AgentActor(
                            agent_id=actor.agent_id,
                            operator_id=actor.operator_id,
                            binding_id=actor.binding_id,
                            access_profile_id=access_profile_id,
                        ),
                    )
                case OperatorActor():
                    return await self._require_executable_principal(session, principal, actor.operator_id)
                case _:
                    raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    # The annotations already say which actor each exit belongs to; these re-check it at runtime
    # because the ledger is reachable from adapters that resolve an actor dynamically. Each caller
    # uses the narrowed value, so neither is a bare assertion.
    @staticmethod
    def _require_operator_actor(actor: RuntimeActor) -> OperatorActor:
        match actor:
            case OperatorActor():
                return actor
            case _:
                raise TypeError(f"operator actor required, got {type(actor).__name__}")

    @staticmethod
    def _require_agent_actor(actor: RuntimeActor) -> AgentActor:
        match actor:
            case AgentActor():
                return actor
            case _:
                raise TypeError(f"agent actor required, got {type(actor).__name__}")

    async def _lock_pending(
        self, session: AsyncSession, tool_call_id: str, actor: RuntimeActor
    ) -> tuple[McpToolCall, _ResolvedToolCallPrincipal]:
        """Lock the actor's call and assert it is still pending, for one of its three exits.

        The `SELECT ... FOR UPDATE` in `_row_by_tool_call_id` is what serializes operator-approve
        against agent-withdraw: the loser re-reads the committed row and fails the check below
        naming the winner's status. Each caller keeps its own actor type, so approve/deny stay
        operator verbs and withdraw stays the requester's own.
        """
        row = await self._row_by_tool_call_id(session, tool_call_id, actor)
        principal = await self._principal(session, tool_call_id)
        record = self._record_from_principal(row, principal)
        if record.status != ToolCallStatus.PENDING_APPROVAL:
            raise ToolCallStateConflictError(f"tool call is not pending approval; status={record.status}")
        return row, principal

    async def _row_by_tool_call_id(self, session: AsyncSession, tool_call_id: str, actor: RuntimeActor) -> McpToolCall:
        stmt = self._scope_to_actor(select(McpToolCall).where(McpToolCall.tool_call_id == tool_call_id), actor)
        row = (await session.scalars(stmt.with_for_update(of=McpToolCall))).first()
        if row is None:
            raise ToolCallNotFoundError("tool call not found")
        return row

    @staticmethod
    def _scope_to_actor(stmt: Select[_SelectRow], actor: RuntimeActor) -> Select[_SelectRow]:
        """Apply the one canonical tool-call ownership predicate to reads and locked writes."""
        stmt = (
            stmt.join(McpToolCallPrincipal, McpToolCallPrincipal.tool_call_id == McpToolCall.tool_call_id)
            .outerjoin(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .outerjoin(Agent, Agent.agent_id == CredentialBinding.agent_id)
        )
        match actor:
            case AgentActor(agent_id=agent_id):
                # Session id is audit attribution, not a new ownership boundary. A replacement
                # session for the same durable Agent can still inspect or withdraw its predecessor's
                # pending calls, matching credential-rotation behavior.
                return stmt.where(Agent.agent_id == agent_id)
            case OperatorActor(operator_id=operator_id):
                return stmt.where(
                    or_(McpToolCallPrincipal.operator_id == operator_id, Agent.owner_operator_id == operator_id)
                )
            case _:
                raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    @staticmethod
    def _selected_fields(fields: Collection[ToolCallPayloadField] | None) -> frozenset[ToolCallPayloadField]:
        """``None`` means the actor-scoped ledger reader (browser/internal callers): every field."""
        return frozenset(ToolCallPayloadField) if fields is None else frozenset(fields)

    @classmethod
    def _projection_stmt(
        cls, actor: RuntimeActor, fields: Collection[ToolCallPayloadField] | None
    ) -> Select[tuple[Any, ...]]:
        """Select one actor-scoped row shape; ``fields`` only controls optional payload columns.

        The principal/agent columns needed to resolve ``caller`` are always selected: the same join
        already scopes the row to the actor, so the columns are free. Whether ``caller`` is attached to
        the returned record is decided in ``_record_from_mapping``, from the same ``fields`` selection.
        """
        selected = cls._selected_fields(fields)
        columns: list[Any] = [
            McpToolCall.tool_call_id.label("tool_call_id"),
            McpToolCall.server_id.label("server_id"),
            McpToolCall.tool_name.label("tool_name"),
            McpToolCall.status.label("status"),
            McpToolCall.created_at.label("created_at"),
            McpToolCall.updated_at.label("updated_at"),
            McpToolCall.title.label("title"),
            McpToolCall.error.label("error"),
            McpToolCall.decision_note.label("decision_note"),
            McpToolCall.decision_operator_id.label("decision_operator_id"),
            McpToolCall.withdrawal_reason.label("withdrawal_reason"),
            McpToolCall.approval_policy_id.label("approval_policy_id"),
            McpToolCall.auto_approval_evaluation.label("auto_approval_evaluation"),
            McpToolCall.approved_at.label("approved_at"),
            McpToolCallPrincipal.operator_id.label("principal_operator_id"),
            McpToolCallPrincipal.binding_id.label("principal_binding_id"),
            McpToolCallPrincipal.session_id.label("principal_session_id"),
            CredentialBinding.agent_id.label("agent_id"),
            Agent.owner_operator_id.label("operator_id"),
            AgentNameReservation.display_name.label("display_name"),
        ]
        payload_columns = {
            ToolCallPayloadField.ARGUMENTS: McpToolCall.arguments_json.label("arguments"),
            ToolCallPayloadField.RATIONALE: McpToolCall.rationale.label("rationale"),
            ToolCallPayloadField.RESULT: McpToolCall.result_json.label("result"),
        }
        columns.extend(column for field, column in payload_columns.items() if field in selected)
        return cls._scope_to_actor(select(*columns), actor).outerjoin(
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
            decision_note=record.decision_note,
            decision_operator_id=record.decision_operator_id,
            withdrawal_reason=record.withdrawal_reason,
            approval_policy_id=record.approval_policy_id,
            auto_approval_evaluation=record.auto_approval_evaluation,
            approved_at=record.approved_at,
        )

    @classmethod
    def _record_from_mapping(
        cls, projection: Mapping[str, Any], *, fields: Collection[ToolCallPayloadField] | None
    ) -> ToolCallRecord:
        principal = cls._resolve_principal(
            projection["tool_call_id"],
            McpToolCallPrincipal(
                tool_call_id=projection["tool_call_id"],
                operator_id=projection["principal_operator_id"],
                binding_id=projection["principal_binding_id"],
                session_id=projection["principal_session_id"],
            ),
            agent_id=projection["agent_id"],
            operator_id=projection["operator_id"],
            display_name=projection["display_name"],
        )
        if isinstance(principal, _OperatorToolCallPrincipal):
            caller: ToolCallCaller = OperatorToolCallCaller()
        else:
            caller = AgentToolCallCaller(
                agent_id=principal.agent_id, display_name=principal.display_name, session_id=principal.session_id
            )
        # `caller` is resolved unconditionally above (its columns are already joined for actor
        # scoping), but it only joins `fields_set` — and so the MCP edge's serialized output — when
        # selected; unlike the payload fields it is never a literal column in `projection`.
        selected_payloads = {field.value for field in ToolCallPayloadField if field.value in projection}
        if ToolCallPayloadField.CALLER in cls._selected_fields(fields):
            selected_payloads.add(ToolCallPayloadField.CALLER)
        fields_set = (
            set(ToolCallRecord.model_fields) - {field.value for field in ToolCallPayloadField} | selected_payloads
        )
        return ToolCallRecord.model_construct(
            _fields_set=fields_set,
            tool_call_id=projection["tool_call_id"],
            server_id=projection["server_id"],
            tool_name=projection["tool_name"],
            caller=caller,
            status=projection["status"],
            created_at=projection["created_at"],
            updated_at=projection["updated_at"],
            title=projection["title"],
            error=projection["error"],
            decision_note=projection["decision_note"],
            decision_operator_id=projection["decision_operator_id"],
            withdrawal_reason=projection["withdrawal_reason"],
            approval_policy_id=projection["approval_policy_id"],
            auto_approval_evaluation=projection["auto_approval_evaluation"],
            approved_at=projection["approved_at"],
            arguments=projection.get("arguments", {}),
            rationale=projection.get("rationale", ""),
            result=projection.get("result"),
        )

    @staticmethod
    def _record_from_principal(row: McpToolCall, principal: _ResolvedToolCallPrincipal) -> ToolCallRecord:
        caller: ToolCallCaller
        if isinstance(principal, _OperatorToolCallPrincipal):
            caller = OperatorToolCallCaller()
        else:
            caller = AgentToolCallCaller(
                agent_id=principal.agent_id, display_name=principal.display_name, session_id=principal.session_id
            )
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
            decision_note=row.decision_note,
            decision_operator_id=row.decision_operator_id,
            withdrawal_reason=row.withdrawal_reason,
            approval_policy_id=row.approval_policy_id,
            auto_approval_evaluation=row.auto_approval_evaluation,
            approved_at=row.approved_at,
        )

    @staticmethod
    async def _principal(session: AsyncSession, tool_call_id: str) -> _ResolvedToolCallPrincipal:
        result = (
            await session.execute(
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
            )
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
            binding_id=row.binding_id,
            agent_id=agent_id,
            operator_id=operator_id,
            display_name=display_name,
            session_id=row.session_id,
        )

    @staticmethod
    async def _require_active_operator(session: AsyncSession, operator_id: UUID) -> None:
        found = await session.scalar(
            select(Operator.operator_id)
            .where(Operator.operator_id == operator_id, Operator.status == OperatorStatus.ACTIVE)
            .with_for_update()
        )
        if found is None:
            raise ToolCallStateConflictError("operator is not active")

    @staticmethod
    async def _require_active_agent_binding(session: AsyncSession, actor: AgentActor) -> tuple[str, str | None]:
        active = await lock_active_agent_binding(
            session, binding_id=actor.binding_id, agent_id=actor.agent_id, operator_id=actor.operator_id, lock=True
        )
        if active is None or active.binding.status is not CredentialBindingStatus.ACTIVE:
            raise ToolCallStateConflictError("agent credential binding is not active")
        return active.display_name, active.agent.access_profile_id

    async def _require_executable_principal(
        self, session: AsyncSession, principal: _ResolvedToolCallPrincipal, operator_id: UUID
    ) -> ToolCallExecutionAuthorization:
        if isinstance(principal, _OperatorToolCallPrincipal):
            if principal.operator_id != operator_id:
                raise ToolCallNotFoundError("tool call not found")
            await self._require_active_operator(session, operator_id)
            return ToolCallExecutionAuthorization(
                operator_id=operator_id, caller=OperatorActor(operator_id=operator_id)
            )
        if principal.operator_id != operator_id:
            raise ToolCallNotFoundError("tool call not found")
        caller = AgentActor(
            agent_id=principal.agent_id, operator_id=principal.operator_id, binding_id=principal.binding_id
        )
        _, access_profile_id = await self._require_active_agent_binding(session, caller)
        caller = AgentActor(
            agent_id=caller.agent_id,
            operator_id=caller.operator_id,
            binding_id=caller.binding_id,
            access_profile_id=access_profile_id,
        )
        return ToolCallExecutionAuthorization(operator_id=operator_id, caller=caller)


class McpServerDispatcher:
    """Execute approved calls through the configured in-process backend."""

    def __init__(self, in_process_servers: InProcessServers) -> None:
        self._in_process = in_process_servers

    async def execute(
        self,
        server: McpServerEntry,
        tool_name: str,
        arguments: dict[str, Any],
        auth_token: str | None,
        execution_context: McpExecutionContext,
    ) -> dict[str, Any]:
        async with Client(_in_process_server(server, self._in_process, auth_token), mode="legacy") as client:
            result = await client.call_tool_mcp(
                tool_name, arguments, meta=mcp_execution_request_meta(execution_context)
            )
        if result.is_error:
            raise RuntimeError(_mcp_error_message(result))
        return _mcp_result_to_json(result)


def _mcp_result_to_json(result: mcp_types.CallToolResult) -> dict[str, Any]:
    return cast(dict[str, Any], result.model_dump(mode="json", by_alias=True, exclude_none=True))


def _mcp_error_message(result: mcp_types.CallToolResult) -> str:
    text_blocks = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    return "\n".join(text_blocks) or "MCP tool returned isError=true"


def _tool_call_service(request: Request) -> ToolCallApplicationService:
    return cast(ToolCallApplicationService, request.app.state.tool_call_service)


ToolCallServiceDep = Annotated[ToolCallApplicationService, Depends(_tool_call_service)]


def _raise_tool_call_http_error(
    error: McpServerNotFoundError | ToolCallNotFoundError | ToolCallStateConflictError,
) -> Never:
    status_code = 409 if isinstance(error, ToolCallStateConflictError) else 404
    raise HTTPException(status_code=status_code, detail=str(error)) from error


@router.get("/api/tool-calls")
async def list_tool_calls(
    *,
    service: ToolCallServiceDep,
    actor: OperatorActorDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: datetime.datetime | None = None,
    auto_approved: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    newest_first: bool = False,
    cursor: Annotated[str | None, Query(description="A `next_cursor` from a previous page.")] = None,
) -> ToolCallListResponse:
    # A record carries its whole arguments and result payload, so a page is worth megabytes at the
    # limit's cap. The history view therefore reads small pages and follows `next_cursor` instead of
    # asking for the maximum up front.
    try:
        page_cursor = ToolCallPageCursor.parse(cursor) if cursor is not None else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    tool_calls = await service.list_tool_calls(
        actor=actor,
        statuses=status,
        since=since,
        auto_approved=auto_approved,
        limit=limit,
        newest_first=newest_first,
        cursor=page_cursor,
    )
    # A short page is the last one; a full page may or may not be, and offering a cursor for the
    # empty page after it costs one request instead of a wrong "no more results".
    next_cursor = ToolCallPageCursor.of(tool_calls[-1]).encode() if len(tool_calls) == limit else None
    return ToolCallListResponse(tool_calls=tool_calls, next_cursor=next_cursor)


@router.get("/api/tool-calls/{tool_call_id}")
async def get_tool_call(tool_call_id: str, service: ToolCallServiceDep, actor: OperatorActorDep) -> ToolCallRecord:
    try:
        return await service.get(tool_call_id, actor=actor)
    except (ToolCallNotFoundError, ToolCallStateConflictError) as error:
        _raise_tool_call_http_error(error)


@router.get("/api/approvals/pending")
async def pending_approvals(service: ToolCallServiceDep, actor: OperatorActorDep) -> PendingApprovalsResponse:
    return PendingApprovalsResponse(approvals=await service.pending_approvals(actor=actor))


@router.post("/api/tool-calls/{tool_call_id}/decision")
async def decide_approval(
    tool_call_id: str, body: ApprovalDecisionRequest, service: ToolCallServiceDep, actor: OperatorActorDep
) -> ApprovalDecisionResponse:
    try:
        tool_call = await service.decide(tool_call_id=tool_call_id, decision=body, actor=actor)
    except (McpServerNotFoundError, ToolCallNotFoundError, ToolCallStateConflictError) as error:
        _raise_tool_call_http_error(error)
    return ApprovalDecisionResponse(tool_call=tool_call)
