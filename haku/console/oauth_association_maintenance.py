"""Background refresh for persisted Operator OAuth associations."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.database_schema import (
    McpOperatorOAuthAssociation,
    Operator,
    OperatorAuthentikToken,
    ProviderConnection,
)
from haku.console.mcp_config import McpServerEntry
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.oauth_token_support import REFRESH_SKEW
from haku.console.operator_identity import OperatorStatus
from haku.console.provider_connection import PostgresProviderConnectionStore

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL = datetime.timedelta(seconds=30)
# Session-level PostgreSQL advisory lock spelling "HAKUOAUT" in ASCII. It keeps multiple
# interchangeable console replicas from concurrently presenting the same rotating refresh token.
_REFRESH_ADVISORY_LOCK = 0x48414B554F415554


class OAuthAssociationMaintenance:
    """Refresh expiring OAuth rows without requiring foreground tool traffic."""

    def __init__(
        self,
        engine: Engine,
        sessions: sessionmaker[Session],
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

    def _candidates(self) -> tuple[list[tuple[str, UUID]], list[tuple[str, UUID]], list[UUID]]:
        refresh_before = datetime.datetime.now(datetime.UTC) + REFRESH_SKEW
        active_operator = Operator.status == OperatorStatus.ACTIVE
        with self._sessions.begin() as session:
            oauth = list(
                session.execute(
                    select(McpOperatorOAuthAssociation.server_id, McpOperatorOAuthAssociation.operator_id)
                    .join(Operator)
                    .where(active_operator)
                    .where(McpOperatorOAuthAssociation.refresh_token.is_not(None))
                    .where(McpOperatorOAuthAssociation.token_expires_at.is_not(None))
                    .where(McpOperatorOAuthAssociation.token_expires_at <= refresh_before)
                ).tuples()
            )
            providers = list(
                session.execute(
                    select(ProviderConnection.connection_name, ProviderConnection.operator_id)
                    .join(Operator)
                    .where(active_operator)
                    .where(ProviderConnection.refresh_token.is_not(None))
                    .where(ProviderConnection.token_expires_at.is_not(None))
                    .where(ProviderConnection.token_expires_at <= refresh_before)
                ).tuples()
            )
            authentik = (
                list(
                    session.scalars(
                        select(OperatorAuthentikToken.operator_id)
                        .join(Operator)
                        .where(active_operator)
                        .where(OperatorAuthentikToken.refresh_token.is_not(None))
                        .where(OperatorAuthentikToken.token_expires_at.is_not(None))
                        .where(OperatorAuthentikToken.token_expires_at <= refresh_before)
                    )
                )
                if self._refresh_authentik_tokens
                else []
            )
        return oauth, providers, authentik

    async def _refresh_oauth(self, server_id: str, operator_id: UUID) -> None:
        if (server := self._servers.get(server_id)) is None:
            logger.warning(
                "Cannot background-refresh OAuth association for removed MCP server %r (%s)", server_id, operator_id
            )
            return
        try:
            await self._oauth_store.access_token_for(server=server, operator_id=operator_id)
        except Exception:
            logger.exception("Background OAuth refresh failed for MCP server %r (%s)", server_id, operator_id)

    async def _refresh_provider(self, connection: str, operator_id: UUID) -> None:
        try:
            await self._provider_store.access_token_for(connection=connection, operator_id=operator_id)
        except Exception:
            logger.exception("Background OAuth refresh failed for provider connection %r (%s)", connection, operator_id)

    async def _refresh_authentik(self, operator_id: UUID) -> None:
        try:
            await self._authentik_store.access_token_for(operator_id=operator_id)
        except Exception:
            logger.exception("Background OAuth refresh failed for Operator login token (%s)", operator_id)

    async def refresh_once(self) -> None:
        """Refresh one snapshot of candidates when this replica wins the database lock."""
        with self._engine.connect() as leader:
            if not leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK}):
                return
            try:
                oauth, providers, authentik = self._candidates()
                async with asyncio.TaskGroup() as tasks:
                    for server_id, operator_id in oauth:
                        tasks.create_task(self._refresh_oauth(server_id, operator_id))
                    for connection, operator_id in providers:
                        tasks.create_task(self._refresh_provider(connection, operator_id))
                    for operator_id in authentik:
                        tasks.create_task(self._refresh_authentik(operator_id))
            finally:
                if not leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK}):
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
