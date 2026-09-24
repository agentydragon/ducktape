"""Revision 0132 drops the remote-MCP operator OAuth tables together with the token states they owned."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest_bazel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.conftest import operator_id
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import OAuthTokenState, ProviderConnection
from haku.console.oauth.provider_connection_registry import ProviderConnectionKind
from haku.console.oauth.token_state import new_token_state


def _provider_connection(operator: UUID, name: str, now: datetime.datetime) -> ProviderConnection:
    return ProviderConnection(
        operator_id=operator,
        connection_name=name,
        provider_name="google",
        provider=ProviderConnectionKind.GOOGLE,
        created_at=now,
        token_state=new_token_state(
            operator_id=operator,
            access_token=f"{name}-access",
            refresh_token=f"{name}-refresh",
            token_type="Bearer",
            scope=None,
            expires_at=None,
            now=now,
        ),
    )


async def test_upgrade_deletes_association_token_states_and_keeps_other_owners(db_url: str) -> None:
    apply_migrations(db_url, "0131")
    engine = create_async_engine(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        operator = await operator_id(sessions, "migration-operator")
        now = datetime.datetime.now(datetime.UTC)
        async with sessions.begin() as session:
            provider = _provider_connection(operator, "google_mail", now)
            association_token = new_token_state(
                operator_id=operator,
                access_token="remote-access",
                refresh_token="remote-refresh",
                token_type="Bearer",
                scope=None,
                expires_at=None,
                now=now,
            )
            session.add_all([provider, association_token])
            await session.flush()
            await session.execute(
                text(
                    """
                    INSERT INTO mcp_operator_oauth_associations
                        (server_id, operator_id, association_id, created_at, client_id, token_endpoint, token_state_id)
                    VALUES ('remote', :operator, gen_random_uuid(), :now, 'client', 'https://auth.test/token', :token)
                    """
                ),
                {"operator": operator, "now": now, "token": association_token.token_state_id},
            )
            provider_token_id = provider.token_state_id

        apply_migrations(db_url)

        async with sessions.begin() as session:
            assert set(await session.scalars(select(OAuthTokenState.token_state_id))) == {provider_token_id}
            assert await session.scalar(text("SELECT to_regclass('mcp_operator_oauth_associations')")) is None
            assert await session.scalar(text("SELECT to_regclass('mcp_operator_oauth_flows')")) is None
            # The deferred ownership check still runs at commit, now without the dropped table.
            session.add(_provider_connection(operator, "google_calendar", now))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
