"""PostgreSQL persistence for the temporary HTTP egress grant domain."""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agents.models import AgentStatus
from haku.console.database_schema import (
    Agent,
    CredentialBinding,
    HttpGrantRow,
    McpToolCall,
    McpToolCallPrincipal,
    Session,
)
from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    grant_principal_column_values,
    grant_principal_from_columns,
)
from haku.console.http_grant_models import (
    HttpGrant,
    HttpGrantNotFoundError,
    HttpGrantOwnershipError,
    HttpGrantSourceError,
    HttpGrantSpec,
    HttpOrigin,
    HttpRequestCoverage,
    derive_status,
)
from haku.console.tool_calls import ToolCallStatus


def _row_to_model(row: HttpGrantRow, *, now: datetime.datetime) -> HttpGrant:
    return HttpGrant(
        grant_id=row.grant_id,
        owner_agent_id=row.owner_agent_id,
        principal=grant_principal_from_columns(
            row.principal_kind, agent_id=row.principal_agent_id, session_id=row.principal_session_id
        ),
        source_tool_call_id=row.source_tool_call_id,
        spec=HttpGrantSpec(
            origin=HttpOrigin(scheme=row.scheme, host=row.host, port=row.port),
            coverage=HttpRequestCoverage(methods=row.methods, path_regex=row.path_regex),
            credential_handle=row.credential_handle,
        ),
        status=derive_status(
            released_at=row.released_at, revoked_at=row.revoked_at, expires_at=row.expires_at, now=now
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        released_at=row.released_at,
        revoked_at=row.revoked_at,
        end_reason=row.end_reason,
    )


def _not_ended() -> ColumnElement[bool]:
    return and_(HttpGrantRow.released_at.is_(None), HttpGrantRow.revoked_at.is_(None))


class PostgresHttpGrantRepository:
    """Small transactional repository with explicit lifecycle ownership and applicability."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _assert_owner_principal_and_source(
        self, session: AsyncSession, *, owner_agent_id: UUID, grant_principal: GrantPrincipal, source_tool_call_id: str
    ) -> None:
        agent = await session.scalar(select(Agent).where(Agent.agent_id == owner_agent_id))
        if agent is None or agent.status in (AgentStatus.ABANDONED, AgentStatus.DELETED):
            raise HttpGrantOwnershipError(f"Agent {owner_agent_id} is not eligible for an HTTP grant")
        source = await session.scalar(
            select(McpToolCallPrincipal)
            .join(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .join(McpToolCall, McpToolCall.tool_call_id == McpToolCallPrincipal.tool_call_id)
            .where(
                McpToolCallPrincipal.tool_call_id == source_tool_call_id,
                CredentialBinding.agent_id == owner_agent_id,
                or_(McpToolCall.status == ToolCallStatus.RUNNING, McpToolCall.status == ToolCallStatus.OK),
                McpToolCall.approved_at.is_not(None),
                McpToolCall.approval_policy_id.is_(None),
            )
            .with_for_update(of=McpToolCall)
        )
        if source is None:
            raise HttpGrantSourceError(
                "source_tool_call_id must identify a manually approved call authenticated by the lifecycle owner"
            )
        if isinstance(grant_principal, AgentGrantPrincipal):
            valid_principal = grant_principal.agent_id == owner_agent_id
        else:
            valid_principal = source.session_id is not None and grant_principal.session_id == source.session_id
            if valid_principal:
                live_session = await session.scalar(
                    select(Session)
                    .where(
                        Session.session_id == grant_principal.session_id,
                        Session.agent_binding_id == source.binding_id,
                        Session.ended_at.is_(None),
                        Session.close_requested_at.is_(None),
                        Session.bridge_connected_at.is_not(None),
                        Session.lease_expires_at > datetime.datetime.now(datetime.UTC),
                    )
                    .with_for_update()
                )
                valid_principal = live_session is not None
        if not valid_principal:
            raise HttpGrantSourceError("grant principal does not match the durable source ToolCall principal")

    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[HttpGrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]:
        grants = tuple(grants)
        if not grants:
            raise ValueError("grants must not be empty")
        async with self._sessions.begin() as session:
            await self._assert_owner_principal_and_source(
                session,
                owner_agent_id=owner_agent_id,
                grant_principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
            )
            existing = (
                await session.scalars(
                    select(HttpGrantRow)
                    .where(
                        HttpGrantRow.owner_agent_id == owner_agent_id,
                        HttpGrantRow.source_tool_call_id == source_tool_call_id,
                    )
                    .order_by(HttpGrantRow.grant_id)
                )
            ).all()
            if existing:
                models = {row.grant_id: _row_to_model(row, now=created_at) for row in existing}
                if any(model.principal != grant_principal for model in models.values()):
                    raise HttpGrantSourceError("source_tool_call_id already created a different HTTP grant principal")
                requested_specs = Counter(spec.model_dump_json() for spec in grants)
                existing_specs = Counter(model.spec.model_dump_json() for model in models.values())
                if existing_specs != requested_specs:
                    raise HttpGrantSourceError("source_tool_call_id already created a different HTTP grant set")
                rows_by_spec: dict[str, list[HttpGrant]] = {}
                for model in models.values():
                    rows_by_spec.setdefault(model.spec.model_dump_json(), []).append(model)
                return tuple(rows_by_spec[spec.model_dump_json()].pop() for spec in grants)
            principal_agent_id, principal_session_id = grant_principal_column_values(grant_principal)
            rows = [
                HttpGrantRow(
                    grant_id=uuid4(),
                    owner_agent_id=owner_agent_id,
                    principal_kind=grant_principal.kind,
                    principal_agent_id=principal_agent_id,
                    principal_session_id=principal_session_id,
                    source_tool_call_id=source_tool_call_id,
                    scheme=spec.origin.scheme,
                    host=spec.origin.host,
                    port=spec.origin.port,
                    methods=spec.coverage.methods,
                    path_regex=spec.coverage.path_regex,
                    credential_handle=spec.credential_handle,
                    created_at=created_at,
                    expires_at=expires_at,
                    released_at=None,
                    revoked_at=None,
                    end_reason=None,
                )
                for spec in grants
            ]
            session.add_all(rows)
            await session.flush()
            return tuple(_row_to_model(row, now=created_at) for row in rows)

    async def list(
        self, *, owner_agent_id: UUID, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        async with self._sessions() as session:
            statement = select(HttpGrantRow).where(HttpGrantRow.owner_agent_id == owner_agent_id)
            if not include_terminal:
                statement = statement.where(_not_ended(), HttpGrantRow.expires_at > now)
            rows = (
                await session.scalars(statement.order_by(HttpGrantRow.created_at.desc(), HttpGrantRow.grant_id))
            ).all()
            return tuple(_row_to_model(row, now=now) for row in rows)

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID, now: datetime.datetime) -> HttpGrant:
        async with self._sessions() as session:
            row = await session.scalar(select(HttpGrantRow).where(HttpGrantRow.grant_id == grant_id))
            if row is None:
                raise HttpGrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise HttpGrantOwnershipError(str(grant_id))
            return _row_to_model(row, now=now)

    async def _end(
        self, *, owner_agent_id: UUID, grant_id: UUID, release: bool, reason: str, now: datetime.datetime
    ) -> HttpGrant:
        reason = reason.strip()
        if not reason:
            raise ValueError("grant end reason must not be empty")
        async with self._sessions.begin() as session:
            row = await session.scalar(select(HttpGrantRow).where(HttpGrantRow.grant_id == grant_id).with_for_update())
            if row is None:
                raise HttpGrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise HttpGrantOwnershipError(str(grant_id))
            # Only a still-active grant records an end action: an already-ended one keeps its
            # facts, and an expired one stays expired by derivation rather than being relabeled.
            if row.released_at is None and row.revoked_at is None and row.expires_at > now:
                if release:
                    row.released_at = now
                else:
                    row.revoked_at = now
                row.end_reason = reason
                await session.flush()
            return _row_to_model(row, now=now)

    async def release(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        return await self._end(owner_agent_id=owner_agent_id, grant_id=grant_id, release=True, reason=reason, now=now)

    async def revoke(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        return await self._end(owner_agent_id=owner_agent_id, grant_id=grant_id, release=False, reason=reason, now=now)

    @staticmethod
    def _request_principal_clause(request_principal: RequestPrincipal) -> ColumnElement[bool]:
        grant_principals = [
            and_(
                HttpGrantRow.principal_kind == GrantPrincipalKind.AGENT,
                HttpGrantRow.principal_agent_id == request_principal.agent_id,
            )
        ]
        if request_principal.session_id is not None:
            grant_principals.append(
                and_(
                    HttpGrantRow.owner_agent_id == request_principal.agent_id,
                    HttpGrantRow.principal_kind == GrantPrincipalKind.SESSION,
                    HttpGrantRow.principal_session_id == request_principal.session_id,
                )
            )
        return or_(*grant_principals)

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        async with self._sessions() as session:
            statement = select(HttpGrantRow).where(self._request_principal_clause(request_principal))
            if not include_terminal:
                statement = statement.where(_not_ended(), HttpGrantRow.expires_at > now)
            rows = (
                await session.scalars(statement.order_by(HttpGrantRow.created_at.desc(), HttpGrantRow.grant_id))
            ).all()
            return tuple(_row_to_model(row, now=now) for row in rows)

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[HttpGrant, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(HttpGrantRow)
                    .where(
                        self._request_principal_clause(request_principal), _not_ended(), HttpGrantRow.expires_at > now
                    )
                    .order_by(HttpGrantRow.expires_at, HttpGrantRow.created_at)
                )
            ).all()
            return tuple(_row_to_model(row, now=now) for row in rows)
