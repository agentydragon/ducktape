"""PostgreSQL lifecycle and provenance tests for temporary exact-origin HTTP grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import default_agent_binding, insert_approved_tool_call, insert_live_session
from haku.console.database_schema import HttpGrantRow
from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.console.http_grant_models import (
    HttpGrantNotFoundError,
    HttpGrantSourceError,
    HttpGrantStatus,
    HttpOrigin,
    HttpScheme,
)
from haku.console.http_grant_repository import PostgresHttpGrantRepository

_NOW = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443)
_OTHER_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=8443)
_RAW_GRANT_INSERT = text(
    """
    INSERT INTO http_grants (
        grant_id, owner_agent_id, principal_kind, principal_agent_id, principal_session_id,
        source_tool_call_id, scheme, host, port, status, created_at, expires_at, ended_at, end_reason
    ) VALUES (
        :grant_id, :owner_agent_id, 'agent', :owner_agent_id, NULL,
        :source_tool_call_id, :scheme, :host, :port, 'active', :created_at, :expires_at, NULL, NULL
    )
    """
)

_insert_http_source = partial(insert_approved_tool_call, server_id="http")


async def _insert_raw_grant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    source_tool_call_id: str,
    scheme: str,
    host: str,
    port: int,
) -> None:
    async with sessions.begin() as session:
        await session.execute(
            _RAW_GRANT_INSERT,
            {
                "grant_id": uuid4(),
                "owner_agent_id": agent_id,
                "source_tool_call_id": source_tool_call_id,
                "scheme": scheme,
                "host": host,
                "port": port,
                "created_at": _NOW,
                "expires_at": _NOW + timedelta(minutes=5),
            },
        )


def test_repository_enforces_source_provenance_and_lifecycle(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresHttpGrantRepository(sessions)

        async def exercise() -> None:
            (grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                origins=(_ORIGIN,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert grant.status is HttpGrantStatus.ACTIVE
            assert grant.origin == _ORIGIN
            assert (await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id)) == grant
            assert await repository.active_for_request_principal(
                request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None), now=_NOW
            ) == (grant,)

            released = await repository.release(
                owner_agent_id=agent_id,
                grant_id=grant.grant_id,
                reason="no longer needed",
                ended_at=_NOW + timedelta(minutes=1),
            )
            assert released.status is HttpGrantStatus.RELEASED
            assert (
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None),
                    now=_NOW,
                )
                == ()
            )

        client.portal.call(exercise)


def test_repository_atomically_creates_multiple_grants_from_one_source(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresHttpGrantRepository(sessions)

        async def exercise() -> None:
            grants = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                origins=(_ORIGIN, _OTHER_ORIGIN),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert len(grants) == 2
            assert len({grant.grant_id for grant in grants}) == 2
            assert {grant.source_tool_call_id for grant in grants} == {source_tool_call_id}
            assert {grant.created_at for grant in grants} == {_NOW}
            assert {grant.expires_at for grant in grants} == {_NOW + timedelta(minutes=5)}

            retried = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                origins=(_ORIGIN, _OTHER_ORIGIN),
                created_at=_NOW + timedelta(seconds=10),
                expires_at=_NOW + timedelta(minutes=10),
            )
            assert tuple(grant.grant_id for grant in retried) == tuple(grant.grant_id for grant in grants)
            assert {grant.created_at for grant in retried} == {_NOW}
            assert {grant.expires_at for grant in retried} == {_NOW + timedelta(minutes=5)}

            with pytest.raises(HttpGrantNotFoundError):
                await repository.revoke_source(
                    owner_agent_id=uuid4(),
                    source_tool_call_id=source_tool_call_id,
                    reason="must not cross Agent ownership",
                    ended_at=_NOW + timedelta(seconds=20),
                )
            assert (
                len(
                    await repository.active_for_request_principal(
                        request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None),
                        now=_NOW,
                    )
                )
                == 2
            )

            released_first = await repository.release(
                owner_agent_id=agent_id,
                grant_id=grants[0].grant_id,
                reason="first origin no longer needed",
                ended_at=_NOW + timedelta(seconds=30),
            )
            assert released_first.status is HttpGrantStatus.RELEASED

            revoked = await repository.revoke_source(
                owner_agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                reason="operator ended probe",
                ended_at=_NOW + timedelta(minutes=1),
            )
            assert {grant.grant_id for grant in revoked} == {grant.grant_id for grant in grants}
            by_id = {grant.grant_id: grant for grant in revoked}
            assert by_id[grants[0].grant_id].status is HttpGrantStatus.RELEASED
            assert by_id[grants[0].grant_id].end_reason == "first origin no longer needed"
            assert by_id[grants[1].grant_id].status is HttpGrantStatus.REVOKED
            assert by_id[grants[1].grant_id].ended_at == _NOW + timedelta(minutes=1)
            assert by_id[grants[1].grant_id].end_reason == "operator ended probe"

            repeated = await repository.revoke_source(
                owner_agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                reason="different retry reason",
                ended_at=_NOW + timedelta(minutes=2),
            )
            assert repeated == revoked

            with pytest.raises(HttpGrantSourceError, match="already created a different"):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                    source_tool_call_id=source_tool_call_id,
                    origins=(_ORIGIN,),
                    created_at=_NOW + timedelta(seconds=10),
                    expires_at=_NOW + timedelta(minutes=10),
                )

            async with sessions() as session:
                rows = (
                    await session.scalars(
                        select(HttpGrantRow).where(HttpGrantRow.source_tool_call_id == source_tool_call_id)
                    )
                ).all()
            assert len(rows) == 2

        client.portal.call(exercise)


def test_repository_matches_agent_and_exact_session_principals(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        session_id = client.portal.call(partial(insert_live_session, sessions, binding_id=binding_id, now=_NOW))
        agent_source = client.portal.call(partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW))
        session_source = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW, session_id=session_id)
        )
        repository = PostgresHttpGrantRepository(sessions)

        async def exercise() -> None:
            (agent_grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=agent_source,
                origins=(_ORIGIN,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            (session_grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=SessionGrantPrincipal(session_id=session_id),
                source_tool_call_id=session_source,
                origins=(_ORIGIN,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )

            assert await repository.active_for_request_principal(
                request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None), now=_NOW
            ) == (agent_grant,)
            assert set(
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(
                        agent_id=agent_id, session_id=session_id, access_profile_id=None
                    ),
                    now=_NOW,
                )
            ) == {agent_grant, session_grant}
            assert set(
                await repository.list_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=agent_id, session_id=session_id, access_profile_id=None)
                )
            ) == {agent_grant, session_grant}
            assert await repository.active_for_request_principal(
                request_principal=RequestPrincipal(agent_id=agent_id, session_id=uuid4(), access_profile_id=None),
                now=_NOW,
            ) == (agent_grant,)
            assert (
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=uuid4(), session_id=session_id, access_profile_id=None),
                    now=_NOW,
                )
                == ()
            )

            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE sessions SET ended_at = now(), error = 'runner failed' WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id},
                )
            ended_source = await _insert_http_source(sessions, binding_id=binding_id, now=_NOW, session_id=session_id)
            with pytest.raises(HttpGrantSourceError, match="durable source ToolCall principal"):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=SessionGrantPrincipal(session_id=session_id),
                    source_tool_call_id=ended_source,
                    origins=(_ORIGIN,),
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=5),
                )

        client.portal.call(exercise)


def test_repository_rejects_wrong_tool_auto_approved_or_cross_domain_source(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        wrong_tool = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW, tool_name="list_grants")
        )
        auto_approved = client.portal.call(
            partial(
                _insert_http_source, sessions, binding_id=binding_id, now=_NOW, approval_policy_id="unsafe-test-policy"
            )
        )
        # A manually approved kubernetes/create_grant must not mint HTTP authority: the
        # provenance is typed per domain, not merely "some approved grant creation".
        kubernetes_source = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresHttpGrantRepository(sessions)

        async def rejected(source_tool_call_id: str) -> None:
            with pytest.raises(HttpGrantSourceError):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                    source_tool_call_id=source_tool_call_id,
                    origins=(_ORIGIN,),
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=5),
                )

        client.portal.call(rejected, wrong_tool)
        client.portal.call(rejected, auto_approved)
        client.portal.call(rejected, kubernetes_source)


def test_database_rejects_grants_with_invalid_source_provenance(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        wrong_tool = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW, tool_name="list_grants")
        )

        async def rejected() -> None:
            with pytest.raises(IntegrityError, match="invalid HTTP grant source provenance"):
                async with sessions.begin() as session:
                    session.add(
                        HttpGrantRow(
                            grant_id=uuid4(),
                            owner_agent_id=agent_id,
                            principal_kind=GrantPrincipalKind.AGENT,
                            principal_agent_id=agent_id,
                            principal_session_id=None,
                            source_tool_call_id=wrong_tool,
                            scheme=_ORIGIN.scheme,
                            host=_ORIGIN.host,
                            port=_ORIGIN.port,
                            status=HttpGrantStatus.ACTIVE,
                            created_at=_NOW,
                            expires_at=_NOW + timedelta(minutes=5),
                            ended_at=None,
                            end_reason=None,
                        )
                    )

        client.portal.call(rejected)


def test_database_accepts_canonical_origin_shape(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )

        client.portal.call(
            partial(
                _insert_raw_grant,
                sessions,
                agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                scheme="https",
                host="xn--bcher-kva.example",
                port=443,
            )
        )


@pytest.mark.parametrize(
    ("scheme", "host", "port"),
    [
        ("ftp", "example.com", 21),
        ("https", "Example.com", 443),
        ("https", "example.com.", 443),
        ("https", "*.example.com", 443),
        ("https", "example.com/path", 443),
        ("https", "", 443),
        ("https", "-bad.example", 443),
        ("https", "example..com", 443),
        ("https", "example.com", 0),
        ("https", "example.com", 65_536),
    ],
)
def test_database_rejects_non_canonical_origin_shape(make_client: Any, scheme: str, host: str, port: int) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )

        async def rejected() -> None:
            with pytest.raises(IntegrityError, match="ck_http_grants_origin_shape"):
                await _insert_raw_grant(
                    sessions,
                    agent_id=agent_id,
                    source_tool_call_id=source_tool_call_id,
                    scheme=scheme,
                    host=host,
                    port=port,
                )

        client.portal.call(rejected)


if __name__ == "__main__":
    pytest_bazel.main()
