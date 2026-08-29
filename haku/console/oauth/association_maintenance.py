"""Background refresh for persisted Operator OAuth associations."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Text, cast as sql_cast, literal, or_, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.database_schema import (
    McpOperatorOAuthAssociation,
    OAuthTokenState,
    Operator,
    OperatorAuthentikToken,
    ProviderConnection,
)
from haku.console.identity.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.identity.operator_identity import OperatorStatus
from haku.console.mcp.operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.mcp_config import McpServerEntry
from haku.console.oauth.provider_connection import PostgresProviderConnectionStore
from haku.console.oauth.token_support import REFRESH_SKEW

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL = datetime.timedelta(seconds=30)
_REFRESH_ADVISORY_LOCK = 0x48414B554F415554


class AssociationKind(StrEnum):
    REMOTE_MCP = "remote_mcp"
    PROVIDER = "provider"
    OPERATOR_LOGIN = "operator_login"


@dataclass(frozen=True, slots=True)
class _RefreshTarget:
    kind: AssociationKind
    name: str | None
    operator_id: UUID


class AssociationMaintenance:
    """Refresh expiring OAuth rows without requiring foreground tool traffic."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        *,
        servers: list[McpServerEntry],
        oauth_store: PostgresMcpOperatorOAuthStore,
        provider_store: PostgresProviderConnectionStore,
        authentik_store: PostgresAuthentikOperatorTokenStore,
        refresh_authentik_tokens: bool,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._servers = {server.id: server for server in servers}
        self._oauth_store = oauth_store
        self._provider_store = provider_store
        self._authentik_store = authentik_store
        self._refresh_authentik_tokens = refresh_authentik_tokens

    async def _candidates(self) -> list[_RefreshTarget]:
        refresh_before = datetime.datetime.now(datetime.UTC) + REFRESH_SKEW
        refreshable = (
            Operator.status == OperatorStatus.ACTIVE,
            OAuthTokenState.refresh_token.is_not(None),
            OAuthTokenState.token_expires_at.is_not(None),
            OAuthTokenState.token_expires_at <= refresh_before,
            or_(
                OAuthTokenState.refresh_failure_action.is_(None),
                (
                    (OAuthTokenState.refresh_failure_action == "retrying")
                    & (
                        OAuthTokenState.refresh_retry_at.is_(None)
                        | (OAuthTokenState.refresh_retry_at <= datetime.datetime.now(datetime.UTC))
                    )
                ),
            ),
        )
        candidates = [
            select(
                literal(AssociationKind.REMOTE_MCP).label("kind"),
                McpOperatorOAuthAssociation.server_id.label("name"),
                OAuthTokenState.operator_id,
            )
            .join(OAuthTokenState, McpOperatorOAuthAssociation.token_state_id == OAuthTokenState.token_state_id)
            .join(Operator, OAuthTokenState.operator_id == Operator.operator_id)
            .where(*refreshable),
            select(
                literal(AssociationKind.PROVIDER).label("kind"),
                ProviderConnection.connection_name.label("name"),
                OAuthTokenState.operator_id,
            )
            .join(OAuthTokenState, ProviderConnection.token_state_id == OAuthTokenState.token_state_id)
            .join(Operator, OAuthTokenState.operator_id == Operator.operator_id)
            .where(*refreshable),
        ]
        if self._refresh_authentik_tokens:
            candidates.append(
                select(
                    literal(AssociationKind.OPERATOR_LOGIN).label("kind"),
                    sql_cast(literal(None), Text).label("name"),
                    OAuthTokenState.operator_id,
                )
                .join(OperatorAuthentikToken, OperatorAuthentikToken.token_state_id == OAuthTokenState.token_state_id)
                .join(Operator, OAuthTokenState.operator_id == Operator.operator_id)
                .where(*refreshable)
            )
        async with self._sessions.begin() as session:
            rows = (await session.execute(union_all(*candidates))).tuples()
            return [
                _RefreshTarget(kind=AssociationKind(kind), name=name, operator_id=operator_id)
                for kind, name, operator_id in rows
            ]

    async def _refresh(self, target: _RefreshTarget) -> None:
        try:
            match target.kind:
                case AssociationKind.REMOTE_MCP:
                    assert target.name is not None
                    if (server := self._servers.get(target.name)) is None:
                        logger.warning(
                            "Cannot background-refresh OAuth association for removed MCP server %r (%s)",
                            target.name,
                            target.operator_id,
                        )
                        return
                    await self._oauth_store.access_token_for(server=server, operator_id=target.operator_id)
                case AssociationKind.PROVIDER:
                    assert target.name is not None
                    await self._provider_store.access_token_for(connection=target.name, operator_id=target.operator_id)
                case AssociationKind.OPERATOR_LOGIN:
                    await self._authentik_store.access_token_for(operator_id=target.operator_id)
        except Exception:
            logger.exception(
                "Background OAuth refresh failed for %s association %r (%s)",
                target.kind,
                target.name,
                target.operator_id,
            )

    async def refresh_once(self) -> None:
        """Refresh one snapshot of candidates when this replica wins the database lock."""
        async with self._engine.connect() as leader:
            if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK}):
                return
            try:
                async with asyncio.TaskGroup() as tasks:
                    for target in await self._candidates():
                        tasks.create_task(self._refresh(target))
            finally:
                if not await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK}):
                    logger.error("OAuth association refresh advisory lock was not held at release")

    async def _run(self, interval: datetime.timedelta) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("OAuth association background refresh sweep failed")
            await asyncio.sleep(interval.total_seconds())

    @asynccontextmanager
    async def run(self, interval: datetime.timedelta = DEFAULT_REFRESH_INTERVAL) -> AsyncIterator[None]:
        """Run refresh sweeps until application shutdown."""
        task = asyncio.create_task(self._run(interval), name="oauth-association-refresh")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
