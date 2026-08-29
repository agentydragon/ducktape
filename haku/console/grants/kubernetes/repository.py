"""PostgreSQL persistence for the temporary Kubernetes grant domain."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import KubernetesGrantRow
from haku.console.grants.envelope import (
    GrantNotFoundError,
    GrantOwnershipError,
    match_replayed_grant_set,
    request_principal_clause,
)
from haku.console.grants.kubernetes.models import Grant, GrantScope, GrantSpec, Rule
from haku.console.grants.principal import (
    GrantPrincipal,
    RequestPrincipal,
    grant_principal_column_values,
    grant_principal_from_columns,
)
from haku.console.grants.provenance import SourceToolFilter, assert_owner_principal_and_source

# Grant creation moved to the shared `grants` server (#4918); the source ToolCall provenance is
# pinned to it. Stored audit rows from before the cutover keep their old `server_id` and are never
# re-created, so this only governs newly minted grants.
_CREATE_GRANT_TOOL = SourceToolFilter(server_id="grants", tool_name="create_grant")


def _row_spec(row: KubernetesGrantRow) -> str:
    return GrantSpec(scope=row.scope, rules=tuple(row.rules)).model_dump_json()


def _not_ended() -> ColumnElement[bool]:
    return KubernetesGrantRow.ended_at.is_(None)


class PostgresGrantRepository:
    """Small transactional repository with explicit lifecycle ownership and applicability."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _row_to_model(row: KubernetesGrantRow) -> Grant:
        return Grant(
            grant_id=row.grant_id,
            owner_agent_id=row.owner_agent_id,
            principal=grant_principal_from_columns(
                row.principal_kind,
                agent_id=row.principal_agent_id,
                session_id=row.principal_session_id,
                access_profile_id=row.principal_access_profile_id,
            ),
            source_tool_call_id=row.source_tool_call_id,
            scope=row.scope,
            rules=tuple(row.rules),
            created_at=row.created_at,
            expires_at=row.expires_at,
            ended_at=row.ended_at,
            end_reason=row.end_reason,
        )

    async def create(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: GrantScope,
        rules: Sequence[Rule],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> Grant:
        grants = await self.create_many(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=(GrantSpec(scope=scope, rules=tuple(rules)),),
            created_at=created_at,
            expires_at=expires_at,
        )
        return grants[0]

    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[GrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[Grant, ...]:
        grants = tuple(grants)
        if not grants:
            raise ValueError("grants must not be empty")
        async with self._sessions.begin() as session:
            await assert_owner_principal_and_source(
                session,
                owner_agent_id=owner_agent_id,
                grant_principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
                source_tool=_CREATE_GRANT_TOOL,
            )
            existing = (
                await session.scalars(
                    select(KubernetesGrantRow)
                    .where(
                        KubernetesGrantRow.owner_agent_id == owner_agent_id,
                        KubernetesGrantRow.source_tool_call_id == source_tool_call_id,
                    )
                    .order_by(KubernetesGrantRow.grant_id)
                )
            ).all()
            if existing:
                replayed = match_replayed_grant_set(
                    existing,
                    grant_principal=grant_principal,
                    specs=[grant.model_dump_json() for grant in grants],
                    row_spec=_row_spec,
                )
                return tuple(self._row_to_model(row) for row in replayed)
            principal_agent_id, principal_session_id, principal_access_profile_id = grant_principal_column_values(
                grant_principal
            )
            rows = [
                KubernetesGrantRow(
                    grant_id=uuid4(),
                    owner_agent_id=owner_agent_id,
                    principal_kind=grant_principal.kind,
                    principal_agent_id=principal_agent_id,
                    principal_session_id=principal_session_id,
                    principal_access_profile_id=principal_access_profile_id,
                    source_tool_call_id=source_tool_call_id,
                    scope=grant.scope,
                    rules=list(grant.rules),
                    created_at=created_at,
                    expires_at=expires_at,
                    ended_at=None,
                    end_reason=None,
                )
                for grant in grants
            ]
            session.add_all(rows)
            await session.flush()
            return tuple(self._row_to_model(row) for row in rows)

    async def list(
        self, *, owner_agent_id: UUID, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[Grant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(KubernetesGrantRow.owner_agent_id == owner_agent_id)
            if not include_terminal:
                statement = statement.where(_not_ended(), KubernetesGrantRow.expires_at > now)
            rows = (
                await session.scalars(
                    statement.order_by(KubernetesGrantRow.created_at.desc(), KubernetesGrantRow.grant_id)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> Grant:
        async with self._sessions() as session:
            row = await session.scalar(select(KubernetesGrantRow).where(KubernetesGrantRow.grant_id == grant_id))
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise GrantOwnershipError(str(grant_id))
            return self._row_to_model(row)

    async def end(
        self, *, owner_agent_ids: frozenset[UUID], grant_id: UUID, reason: str | None, now: datetime.datetime
    ) -> Grant:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(KubernetesGrantRow).where(KubernetesGrantRow.grant_id == grant_id).with_for_update()
            )
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id not in owner_agent_ids:
                raise GrantOwnershipError(str(grant_id))
            # Only a still-active grant records an end action: an already-ended one keeps its
            # facts, and an expired one stays expired by derivation rather than being relabeled.
            if row.ended_at is None and row.expires_at > now:
                row.ended_at = now
                row.end_reason = reason
                await session.flush()
            return self._row_to_model(row)

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[Grant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(
                request_principal_clause(KubernetesGrantRow, request_principal)
            )
            if not include_terminal:
                statement = statement.where(_not_ended(), KubernetesGrantRow.expires_at > now)
            rows = (
                await session.scalars(
                    statement.order_by(KubernetesGrantRow.created_at.desc(), KubernetesGrantRow.grant_id)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[Grant, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(KubernetesGrantRow)
                    .where(
                        request_principal_clause(KubernetesGrantRow, request_principal),
                        _not_ended(),
                        KubernetesGrantRow.expires_at > now,
                    )
                    .order_by(KubernetesGrantRow.expires_at, KubernetesGrantRow.created_at)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)
