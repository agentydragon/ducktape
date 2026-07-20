"""Tests for replica-coordinated background OAuth association refresh."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, Mock

import pytest_bazel
from sqlalchemy import create_engine

from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.database_schema import McpOperatorOAuthAssociation, OperatorAuthentikToken, ProviderConnection
from haku.console.mcp_config import McpServerEntry, NoCredential, RemoteMcpBackend
from haku.console.oauth_association_maintenance import OAuthAssociationMaintenance
from haku.console.provider_connection_registry import ProviderConnectionKind


async def test_refreshes_every_expiring_association_and_isolates_failures(migrated_db_url: str, caplog) -> None:
    engine = create_engine(migrated_db_url)
    sessions = console_sessions(migrated_db_url)
    operator_id = operator_identity_store(migrated_db_url).resolve_configured_external_user_key(
        "background-refresh-operator"
    )
    now = datetime.datetime.now(datetime.UTC)
    with sessions.begin() as session:
        session.add_all(
            [
                McpOperatorOAuthAssociation(
                    server_id="remote",
                    operator_id=operator_id,
                    created_at=now,
                    updated_at=now,
                    client_id="client",
                    token_endpoint="https://auth.test/token",
                    access_token="remote-old",
                    refresh_token="remote-refresh",
                    token_type="Bearer",
                    token_expires_at=now,
                ),
                ProviderConnection(
                    operator_id=operator_id,
                    connection_name="google_mail",
                    provider_name="google_mail",
                    provider=ProviderConnectionKind.GOOGLE,
                    created_at=now,
                    updated_at=now,
                    access_token="provider-old",
                    refresh_token="provider-refresh",
                    token_type="Bearer",
                    token_expires_at=now,
                ),
                OperatorAuthentikToken(
                    operator_id=operator_id,
                    created_at=now,
                    updated_at=now,
                    access_token="authentik-old",
                    refresh_token="authentik-refresh",
                    token_type="Bearer",
                    token_expires_at=now,
                ),
            ]
        )

    oauth_store = Mock()
    oauth_store.access_token_for = AsyncMock(side_effect=RuntimeError("remote refresh failed"))
    provider_store = Mock()
    provider_store.access_token_for = AsyncMock(return_value="provider-new")
    authentik_store = Mock()
    authentik_store.access_token_for = AsyncMock(return_value="authentik-new")
    maintenance = OAuthAssociationMaintenance(
        engine,
        sessions,
        servers=[
            McpServerEntry(id="remote", backend=RemoteMcpBackend(url="https://remote.test/mcp", auth=NoCredential()))
        ],
        oauth_store=oauth_store,
        provider_store=provider_store,
        authentik_store=authentik_store,
        refresh_authentik_tokens=True,
    )
    try:
        await maintenance.refresh_once()
    finally:
        engine.dispose()

    oauth_store.access_token_for.assert_awaited_once()
    assert oauth_store.access_token_for.await_args.kwargs["operator_id"] == operator_id
    provider_store.access_token_for.assert_awaited_once_with(connection="google_mail", operator_id=operator_id)
    authentik_store.access_token_for.assert_awaited_once_with(operator_id=operator_id)
    assert "Background OAuth refresh failed for MCP server 'remote'" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
