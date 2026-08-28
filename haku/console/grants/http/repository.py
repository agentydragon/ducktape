"""PostgreSQL persistence for the temporary HTTP egress grant domain."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import HttpGrantRow
from haku.console.grants.envelope import (
    GrantNotFoundError,
    GrantOwnershipError,
    match_replayed_grant_set,
    request_principal_clause,
)
from haku.console.grants.http.models import HttpGrant, HttpGrantSpec, HttpOrigin, HttpRequestCoverage
from haku.console.grants.principal import (
    GrantPrincipal,
    RequestPrincipal,
    grant_principal_column_values,
    grant_principal_from_columns,
)
from haku.console.grants.provenance import assert_owner_principal_and_source


def _row_spec(row: HttpGrantRow) -> HttpGrantSpec:
    return HttpGrantSpec(
        origin=HttpOrigin(scheme=row.scheme, host=row.host, port=row.port),
        coverage=HttpRequestCoverage(methods=row.methods, path_regex=row.path_regex),
        credential_handle=row.credential_handle,
        allow_prohibited_address=row.allow_prohibited_address,
    )


def _row_to_model(row: HttpGrantRow) -> HttpGrant:
    return HttpGrant(
        grant_id=row.grant_id,
        owner_agent_id=row.owner_agent_id,
        principal=grant_principal_from_columns(
            row.principal_kind, agent_id=row.principal_agent_id, session_id=row.principal_session_id
        ),
        source_tool_call_id=row.source_tool_call_id,
        spec=_row_spec(row),
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
            await assert_owner_principal_and_source(
                session,
                owner_agent_id=owner_agent_id,
                grant_principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
                source_tool=None,
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
                replayed = match_replayed_grant_set(
                    existing,
                    grant_principal=grant_principal,
                    specs=[spec.model_dump_json() for spec in grants],
                    row_spec=lambda row: _row_spec(row).model_dump_json(),
                )
                return tuple(_row_to_model(row) for row in replayed)
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
                    allow_prohibited_address=spec.allow_prohibited_address,
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
            return tuple(_row_to_model(row) for row in rows)

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
            return tuple(_row_to_model(row) for row in rows)

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> HttpGrant:
        async with self._sessions() as session:
            row = await session.scalar(select(HttpGrantRow).where(HttpGrantRow.grant_id == grant_id))
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise GrantOwnershipError(str(grant_id))
            return _row_to_model(row)

    async def _end(
        self, *, owner_agent_id: UUID, grant_id: UUID, release: bool, reason: str, now: datetime.datetime
    ) -> HttpGrant:
        reason = reason.strip()
        if not reason:
            raise ValueError("grant end reason must not be empty")
        async with self._sessions.begin() as session:
            row = await session.scalar(select(HttpGrantRow).where(HttpGrantRow.grant_id == grant_id).with_for_update())
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise GrantOwnershipError(str(grant_id))
            # Only a still-active grant records an end action: an already-ended one keeps its
            # facts, and an expired one stays expired by derivation rather than being relabeled.
            if row.released_at is None and row.revoked_at is None and row.expires_at > now:
                if release:
                    row.released_at = now
                else:
                    row.revoked_at = now
                row.end_reason = reason
                await session.flush()
            return _row_to_model(row)

    async def release(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        return await self._end(owner_agent_id=owner_agent_id, grant_id=grant_id, release=True, reason=reason, now=now)

    async def revoke(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        return await self._end(owner_agent_id=owner_agent_id, grant_id=grant_id, release=False, reason=reason, now=now)

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        async with self._sessions() as session:
            statement = select(HttpGrantRow).where(request_principal_clause(HttpGrantRow, request_principal))
            if not include_terminal:
                statement = statement.where(_not_ended(), HttpGrantRow.expires_at > now)
            rows = (
                await session.scalars(statement.order_by(HttpGrantRow.created_at.desc(), HttpGrantRow.grant_id))
            ).all()
            return tuple(_row_to_model(row) for row in rows)

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[HttpGrant, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(HttpGrantRow)
                    .where(
                        request_principal_clause(HttpGrantRow, request_principal),
                        _not_ended(),
                        HttpGrantRow.expires_at > now,
                    )
                    .order_by(HttpGrantRow.expires_at, HttpGrantRow.created_at)
                )
            ).all()
            return tuple(_row_to_model(row) for row in rows)
