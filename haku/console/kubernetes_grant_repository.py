"""PostgreSQL persistence for the temporary Kubernetes grant domain."""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agents.models import AgentStatus
from haku.console.chat_models import SessionStatus
from haku.console.database_schema import (
    Agent,
    CredentialBinding,
    KubernetesGrantRow,
    McpToolCall,
    McpToolCallPrincipal,
    Session,
)
from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.console.kubernetes_grant_models import (
    KubernetesGrant,
    KubernetesGrantNotFoundError,
    KubernetesGrantOwnershipError,
    KubernetesGrantScope,
    KubernetesGrantSourceError,
    KubernetesGrantSpec,
    KubernetesGrantStatus,
    KubernetesRule,
)
from haku.console.tool_calls import ToolCallStatus


class PostgresKubernetesGrantRepository:
    """Small transactional repository with explicit lifecycle ownership and applicability."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _row_to_model(row: KubernetesGrantRow) -> KubernetesGrant:
        match row.principal_kind:
            case GrantPrincipalKind.AGENT:
                if row.principal_agent_id is None:
                    raise RuntimeError("Agent-principal Kubernetes grant is missing its Agent")
                grant_principal: GrantPrincipal = AgentGrantPrincipal(agent_id=row.principal_agent_id)
            case GrantPrincipalKind.SESSION:
                if row.principal_session_id is None:
                    raise RuntimeError("session-principal Kubernetes grant is missing its session")
                grant_principal = SessionGrantPrincipal(session_id=row.principal_session_id)
        return KubernetesGrant(
            grant_id=row.grant_id,
            owner_agent_id=row.owner_agent_id,
            principal=grant_principal,
            source_tool_call_id=row.source_tool_call_id,
            scope=row.scope,
            rules=tuple(row.rules),
            status=row.status,
            created_at=row.created_at,
            expires_at=row.expires_at,
            ended_at=row.ended_at,
            end_reason=row.end_reason,
        )

    async def _assert_owner_principal_and_source(
        self, session: AsyncSession, *, owner_agent_id: UUID, grant_principal: GrantPrincipal, source_tool_call_id: str
    ) -> None:
        agent = await session.scalar(select(Agent).where(Agent.agent_id == owner_agent_id))
        if agent is None or agent.status in (AgentStatus.ABANDONED, AgentStatus.DELETED):
            raise KubernetesGrantOwnershipError(f"Agent {owner_agent_id} is not eligible for a Kubernetes grant")
        source = await session.scalar(
            select(McpToolCallPrincipal)
            .join(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .join(McpToolCall, McpToolCall.tool_call_id == McpToolCallPrincipal.tool_call_id)
            .where(
                McpToolCallPrincipal.tool_call_id == source_tool_call_id,
                CredentialBinding.agent_id == owner_agent_id,
                McpToolCall.server_id == "kubernetes",
                McpToolCall.tool_name == "create_grant",
                or_(McpToolCall.status == ToolCallStatus.RUNNING, McpToolCall.status == ToolCallStatus.OK),
                McpToolCall.approved_at.is_not(None),
                McpToolCall.approval_policy_id.is_(None),
            )
            .with_for_update(of=McpToolCall)
        )
        if source is None:
            raise KubernetesGrantSourceError(
                "source_tool_call_id must identify a manually approved kubernetes/create_grant call "
                "authenticated by the lifecycle owner"
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
                        Session.status.in_((SessionStatus.READY, SessionStatus.RESPONDING)),
                        Session.lease_expires_at > datetime.datetime.now(datetime.UTC),
                    )
                    .with_for_update()
                )
                valid_principal = live_session is not None
        if not valid_principal:
            raise KubernetesGrantSourceError("grant principal does not match the durable source ToolCall principal")

    async def _lock_owned_source(
        self, session: AsyncSession, *, owner_agent_id: UUID, source_tool_call_id: str
    ) -> None:
        source = await session.scalar(
            select(McpToolCall)
            .join(McpToolCallPrincipal, McpToolCallPrincipal.tool_call_id == McpToolCall.tool_call_id)
            .join(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
            .where(McpToolCall.tool_call_id == source_tool_call_id, CredentialBinding.agent_id == owner_agent_id)
            .with_for_update(of=McpToolCall)
        )
        if source is None:
            raise KubernetesGrantNotFoundError(source_tool_call_id)

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
            await self._assert_owner_principal_and_source(
                session,
                owner_agent_id=owner_agent_id,
                grant_principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
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
                if any(self._row_to_model(row).principal != grant_principal for row in existing):
                    raise KubernetesGrantSourceError(
                        "source_tool_call_id already created a different Kubernetes grant principal"
                    )
                requested_specs = Counter(grant.model_dump_json() for grant in grants)
                existing_specs = Counter(
                    KubernetesGrantSpec(scope=row.scope, rules=tuple(row.rules)).model_dump_json() for row in existing
                )
                if existing_specs != requested_specs:
                    raise KubernetesGrantSourceError(
                        "source_tool_call_id already created a different Kubernetes grant set"
                    )
                rows_by_spec: dict[str, list[KubernetesGrantRow]] = {}
                for row in existing:
                    key = KubernetesGrantSpec(scope=row.scope, rules=tuple(row.rules)).model_dump_json()
                    rows_by_spec.setdefault(key, []).append(row)
                return tuple(self._row_to_model(rows_by_spec[grant.model_dump_json()].pop()) for grant in grants)
            rows = [
                KubernetesGrantRow(
                    grant_id=uuid4(),
                    owner_agent_id=owner_agent_id,
                    principal_kind=grant_principal.kind,
                    principal_agent_id=(
                        grant_principal.agent_id if isinstance(grant_principal, AgentGrantPrincipal) else None
                    ),
                    principal_session_id=(
                        grant_principal.session_id if isinstance(grant_principal, SessionGrantPrincipal) else None
                    ),
                    source_tool_call_id=source_tool_call_id,
                    scope=grant.scope,
                    rules=list(grant.rules),
                    status=KubernetesGrantStatus.ACTIVE,
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

    async def list(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[KubernetesGrant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(KubernetesGrantRow.owner_agent_id == owner_agent_id)
            if not include_terminal:
                statement = statement.where(KubernetesGrantRow.status == KubernetesGrantStatus.ACTIVE)
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
                raise KubernetesGrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise KubernetesGrantOwnershipError(str(grant_id))
            return self._row_to_model(row)

    async def _end(
        self,
        *,
        owner_agent_id: UUID,
        grant_id: UUID,
        status: KubernetesGrantStatus,
        reason: str,
        ended_at: datetime.datetime,
    ) -> KubernetesGrant:
        if not reason.strip():
            raise ValueError("grant end reason must not be empty")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(KubernetesGrantRow).where(KubernetesGrantRow.grant_id == grant_id).with_for_update()
            )
            if row is None:
                raise KubernetesGrantNotFoundError(str(grant_id))
            if row.owner_agent_id != owner_agent_id:
                raise KubernetesGrantOwnershipError(str(grant_id))
            if row.status is KubernetesGrantStatus.ACTIVE:
                # Expiration wins over a late release/revocation attempt. This prevents a caller
                # racing the expiry sweep from reviving the meaning of an already-expired lease.
                row.status = KubernetesGrantStatus.EXPIRED if ended_at >= row.expires_at else status
                row.ended_at = ended_at
                row.end_reason = "expired" if row.status is KubernetesGrantStatus.EXPIRED else reason.strip()
                await session.flush()
            return self._row_to_model(row)

    async def release(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant:
        return await self._end(
            owner_agent_id=owner_agent_id,
            grant_id=grant_id,
            status=KubernetesGrantStatus.RELEASED,
            reason=reason,
            ended_at=ended_at,
        )

    async def revoke(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant:
        return await self._end(
            owner_agent_id=owner_agent_id,
            grant_id=grant_id,
            status=KubernetesGrantStatus.REVOKED,
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
            await self._lock_owned_source(
                session, owner_agent_id=owner_agent_id, source_tool_call_id=source_tool_call_id
            )
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
                raise KubernetesGrantNotFoundError(source_tool_call_id)
            for row in rows:
                if row.status is not KubernetesGrantStatus.ACTIVE:
                    continue
                row.status = (
                    KubernetesGrantStatus.EXPIRED if ended_at >= row.expires_at else KubernetesGrantStatus.REVOKED
                )
                row.ended_at = ended_at
                row.end_reason = "expired" if row.status is KubernetesGrantStatus.EXPIRED else reason
            await session.flush()
            return tuple(self._row_to_model(row) for row in rows)

    async def expire(self, *, now: datetime.datetime, owner_agent_id: UUID | None = None) -> int:
        where = [KubernetesGrantRow.status == KubernetesGrantStatus.ACTIVE, KubernetesGrantRow.expires_at <= now]
        if owner_agent_id is not None:
            where.append(KubernetesGrantRow.owner_agent_id == owner_agent_id)
        async with self._sessions.begin() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(KubernetesGrantRow)
                    .where(*where)
                    .values(status=KubernetesGrantStatus.EXPIRED, ended_at=now, end_reason="expired")
                ),
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _request_principal_clause(request_principal: RequestPrincipal):
        grant_principals = [
            and_(
                KubernetesGrantRow.principal_kind == GrantPrincipalKind.AGENT,
                KubernetesGrantRow.principal_agent_id == request_principal.agent_id,
            )
        ]
        if request_principal.session_id is not None:
            grant_principals.append(
                and_(
                    KubernetesGrantRow.owner_agent_id == request_principal.agent_id,
                    KubernetesGrantRow.principal_kind == GrantPrincipalKind.SESSION,
                    KubernetesGrantRow.principal_session_id == request_principal.session_id,
                )
            )
        return or_(*grant_principals)

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[KubernetesGrant, ...]:
        async with self._sessions() as session:
            statement = select(KubernetesGrantRow).where(self._request_principal_clause(request_principal))
            if not include_terminal:
                statement = statement.where(KubernetesGrantRow.status == KubernetesGrantStatus.ACTIVE)
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
                        self._request_principal_clause(request_principal),
                        KubernetesGrantRow.status == KubernetesGrantStatus.ACTIVE,
                        KubernetesGrantRow.expires_at > now,
                    )
                    .order_by(KubernetesGrantRow.expires_at, KubernetesGrantRow.created_at)
                )
            ).all()
            return tuple(self._row_to_model(row) for row in rows)
