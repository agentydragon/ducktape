"""Tests for replica-coordinated background OAuth association refresh."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, Mock

import pytest_bazel

from haku.console.database_schema import McpOperatorOAuthAssociation, OperatorAuthentikToken, ProviderConnection
from haku.console.mcp_config import McpServerEntry, NoCredential, RemoteMcpBackend
from haku.console.oauth.association_maintenance import AssociationMaintenance
from haku.console.oauth.provider_connection_registry import ProviderConnectionKind
from haku.console.oauth.token_state import new_token_state


async def test_refreshes_every_expiring_association_and_isolates_failures(
    migrated_engine, migrated_sessions, migrated_identity_store, caplog
) -> None:
    engine = migrated_engine
    sessions = migrated_sessions
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("background-refresh-operator")
    now = datetime.datetime.now(datetime.UTC)
    async with sessions.begin() as session:
        session.add_all(
            [
                McpOperatorOAuthAssociation(
                    server_id="remote",
                    operator_id=operator_id,
                    created_at=now,
                    client_id="client",
                    token_endpoint="https://auth.test/token",
                    token_state=new_token_state(
                        operator_id=operator_id,
                        access_token="remote-old",
                        refresh_token="remote-refresh",
                        token_type="Bearer",
                        scope=None,
                        expires_at=now,
                        now=now,
                    ),
                ),
                ProviderConnection(
                    operator_id=operator_id,
                    connection_name="google_mail",
                    provider_name="google_mail",
                    provider=ProviderConnectionKind.GOOGLE,
                    created_at=now,
                    token_state=new_token_state(
                        operator_id=operator_id,
                        access_token="provider-old",
                        refresh_token="provider-refresh",
                        token_type="Bearer",
                        scope=None,
                        expires_at=now,
                        now=now,
                    ),
                ),
                OperatorAuthentikToken(
                    operator_id=operator_id,
                    created_at=now,
                    token_state=new_token_state(
                        operator_id=operator_id,
                        access_token="authentik-old",
                        refresh_token="authentik-refresh",
                        token_type="Bearer",
                        scope=None,
                        expires_at=now,
                        now=now,
                    ),
                ),
            ]
        )

    oauth_store = Mock()
    oauth_store.access_token_for = AsyncMock(side_effect=RuntimeError("remote refresh failed"))
    provider_store = Mock()
    provider_store.access_token_for = AsyncMock(return_value="provider-new")
    authentik_store = Mock()
    authentik_store.access_token_for = AsyncMock(return_value="authentik-new")
    maintenance = AssociationMaintenance(
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
    await maintenance.refresh_once()

    oauth_store.access_token_for.assert_awaited_once()
    assert oauth_store.access_token_for.await_args.kwargs["operator_id"] == operator_id
    provider_store.access_token_for.assert_awaited_once_with(connection="google_mail", operator_id=operator_id)
    authentik_store.access_token_for.assert_awaited_once_with(operator_id=operator_id)
    assert "Background OAuth refresh failed for remote_mcp association 'remote'" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
