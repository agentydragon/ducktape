"""PostgreSQL persistence for the temporary Kubernetes grant domain."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import KubernetesGrantRow
from haku.console.grants.envelope import (
    GrantNotFoundError,
    GrantOwnershipError,
    GrantStatus,
    match_replayed_grant_set,
    request_principal_clause,
)
from haku.console.grants.kubernetes.models import (
    KubernetesGrant,
    KubernetesGrantScope,
    KubernetesGrantSpec,
    KubernetesRule,
)
from haku.console.grants.principal import (
    GrantPrincipal,
    RequestPrincipal,
    grant_principal_column_values,
    grant_principal_from_columns,
)
from haku.console.grants.provenance import SourceToolFilter, assert_owner_principal_and_source, lock_owned_source

_CREATE_GRANT_TOOL = SourceToolFilter(server_id="kubernetes", tool_name="create_grant")


def _row_spec(row: KubernetesGrantRow) -> str:
    return KubernetesGrantSpec(scope=row.scope, rules=tuple(row.rules)).model_dump_json()


class PostgresKubernetesGrantRepository:
    """Small transactional repository with explicit lifecycle ownership and applicability."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _row_to_model(row: KubernetesGrantRow) -> KubernetesGrant:
        return KubernetesGrant(
            grant_id=row.grant_id,
            owner_agent_id=row.owner_agent_id,
            principal=grant_principal_from_columns(
                row.principal_kind, agent_id=row.principal_agent_id, session_id=row.principal_session_id
            ),
            source_tool_call_id=row.source_tool_call_id,
            scope=row.scope,
            rules=tuple(row.rules),
            status=row.status,
            created_at=row.created_at,
            expires_at=row.expires_at,
            released_at=row.released_at,
            revoked_at=row.revoked_at,
            ended_at=row.ended_at,
            end_reason=row.end_reason,
        )

    async def create(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: KubernetesGrantScope,
        rules: Sequence[KubernetesRule],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> KubernetesGrant:
        grants = await self.create_many(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=(KubernetesGrantSpec(scope=scope, rules=tuple(rules)),),
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
        grants: Sequence[KubernetesGrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[KubernetesGrant, ...]:
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
            principal_agent_id, principal_session_id = grant_principal_column_values(grant_principal)
            rows = [
                KubernetesGrantRow(
                    grant_id=uuid4(),
                    owner_agent_id=owner_agent_id,
                    principal_kind=grant_principal.kind,
                    principal_agent_id=principal_agent_id,
                    principal_session_id=principal_session_id,
                    source_tool_call_id=source_tool_call_id,
                    scope=grant.scope,
                    rules=list(grant.rules),
                    status=GrantStatus.ACTIVE,
                    created_at=created_at,
                    expires_at=expires_at,
                    released_at=None,
                    revoked_at=None,
                    ended_at=None,
                    end_reason=None,
                )
                for grant in grants
            ]
            session.add_all(rows)
            await session.flush()
            return tuple(self._row_to_model(row) for row in rows)

    async def list(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[KubernetesGrant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(KubernetesGrantRow.owner_agent_id == owner_agent_id)
            if not include_terminal:
                statement = statement.where(KubernetesGrantRow.status == GrantStatus.ACTIVE)
            rows = (
                await session.scalars(
                    statement.order_by(KubernetesGrantRow.created_at.desc(), KubernetesGrantRow.grant_id)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> KubernetesGrant:
        async with self._sessions() as session:
            row = await session.scalar(select(KubernetesGrantRow).where(KubernetesGrantRow.grant_id == grant_id))
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise GrantOwnershipError(str(grant_id))
            return self._row_to_model(row)

    async def _end(
        self, *, owner_agent_id: UUID, grant_id: UUID, status: GrantStatus, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant:
        if not reason.strip():
            raise ValueError("grant end reason must not be empty")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(KubernetesGrantRow).where(KubernetesGrantRow.grant_id == grant_id).with_for_update()
            )
            if row is None:
                raise GrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise GrantOwnershipError(str(grant_id))
            if row.status is GrantStatus.ACTIVE:
                # Expiration wins over a late release/revocation attempt. This prevents a caller
                # racing the expiry sweep from reviving the meaning of an already-expired lease.
                row.status = GrantStatus.EXPIRED if ended_at >= row.expires_at else status
                row.ended_at = ended_at
                row.end_reason = "expired" if row.status is GrantStatus.EXPIRED else reason.strip()
                # Dual-write the envelope end fact; an expiry relabel records none (expiry is
                # derivational, never a fact).
                if row.status is GrantStatus.RELEASED:
                    row.released_at = ended_at
                elif row.status is GrantStatus.REVOKED:
                    row.revoked_at = ended_at
                await session.flush()
            return self._row_to_model(row)

    async def release(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant:
        return await self._end(
            owner_agent_id=owner_agent_id,
            grant_id=grant_id,
            status=GrantStatus.RELEASED,
            reason=reason,
            ended_at=ended_at,
        )

    async def revoke(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant:
        return await self._end(
            owner_agent_id=owner_agent_id,
            grant_id=grant_id,
            status=GrantStatus.REVOKED,
            reason=reason,
            ended_at=ended_at,
        )

    async def revoke_source(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str, ended_at: datetime.datetime
    ) -> tuple[KubernetesGrant, ...]:
        """Revoke every active grant created by one reviewed source ToolCall."""

        reason = reason.strip()
        if not reason:
            raise ValueError("grant end reason must not be empty")
        async with self._sessions.begin() as session:
            # Serialize source-set lifecycle with create_many(), which locks this same durable
            # ToolCall before reading or inserting the immutable grant set.
            await lock_owned_source(session, owner_agent_id=owner_agent_id, source_tool_call_id=source_tool_call_id)
            rows = (
                await session.scalars(
                    select(KubernetesGrantRow)
                    .where(
                        KubernetesGrantRow.owner_agent_id == owner_agent_id,
                        KubernetesGrantRow.source_tool_call_id == source_tool_call_id,
                    )
                    .order_by(KubernetesGrantRow.grant_id)
                    .with_for_update()
                )
            ).all()
            if not rows:
                raise GrantNotFoundError(source_tool_call_id)
            for row in rows:
                if row.status is not GrantStatus.ACTIVE:
                    continue
                row.status = GrantStatus.EXPIRED if ended_at >= row.expires_at else GrantStatus.REVOKED
                row.ended_at = ended_at
                row.end_reason = "expired" if row.status is GrantStatus.EXPIRED else reason
                if row.status is GrantStatus.REVOKED:
                    row.revoked_at = ended_at
            await session.flush()
            return tuple(self._row_to_model(row) for row in rows)

    async def expire(self, *, now: datetime.datetime, owner_agent_id: UUID | None = None) -> int:
        where = [KubernetesGrantRow.status == GrantStatus.ACTIVE, KubernetesGrantRow.expires_at <= now]
        if owner_agent_id is not None:
            where.append(KubernetesGrantRow.owner_agent_id == owner_agent_id)
        async with self._sessions.begin() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(KubernetesGrantRow)
                    .where(*where)
                    .values(status=GrantStatus.EXPIRED, ended_at=now, end_reason="expired")
                ),
            )
            return int(result.rowcount or 0)

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[KubernetesGrant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(
                request_principal_clause(KubernetesGrantRow, request_principal)
            )
            if not include_terminal:
                statement = statement.where(KubernetesGrantRow.status == GrantStatus.ACTIVE)
            rows = (
                await session.scalars(
                    statement.order_by(KubernetesGrantRow.created_at.desc(), KubernetesGrantRow.grant_id)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[KubernetesGrant, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(KubernetesGrantRow)
                    .where(
                        request_principal_clause(KubernetesGrantRow, request_principal),
                        KubernetesGrantRow.status == GrantStatus.ACTIVE,
                        KubernetesGrantRow.expires_at > now,
                    )
                    .order_by(KubernetesGrantRow.expires_at, KubernetesGrantRow.created_at)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)
