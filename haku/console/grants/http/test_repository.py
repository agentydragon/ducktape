"""PostgreSQL lifecycle and provenance tests for temporary HTTP grants."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import (
    DEFAULT_ACCESS_PROFILE_ID,
    default_agent_binding,
    insert_approved_tool_call,
    insert_live_session,
)
from haku.console.database_schema import HttpGrantRow
from haku.console.grants.envelope import GrantSourceError, GrantStatus
from haku.console.grants.http.models import GrantSpec, HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.grants.http.repository import PostgresGrantRepository
from haku.console.grants.principal import (
    AccessProfileGrantPrincipal,
    AgentGrantPrincipal,
    RequestPrincipal,
    SessionGrantPrincipal,
)

# Relative: Grant.status is computed against the live clock, so windows anchor to it.
_NOW = datetime.now(UTC)
_SPEC = GrantSpec(
    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443),
    coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
)
_OTHER_SPEC = GrantSpec(
    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=8443),
    coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET, HttpMethod.POST}), path_regex="/v1/.*"),
    credential_handle="github-bot",
)

_insert_http_source = partial(insert_approved_tool_call, server_id="grants")


@dataclass(frozen=True, slots=True)
class _RepositoryClient:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    agent_id: UUID
    binding_id: UUID

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)


@pytest.fixture
def repository_client(make_client: Any) -> Iterator[_RepositoryClient]:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _RepositoryClient(client=client, sessions=sessions, agent_id=agent_id, binding_id=binding_id)


def test_repository_enforces_source_provenance_and_lifecycle(repository_client: _RepositoryClient) -> None:
    source_tool_call_id = repository_client.call(
        partial(_insert_http_source, repository_client.sessions, binding_id=repository_client.binding_id, now=_NOW)
    )
    repository = PostgresGrantRepository(repository_client.sessions)

    async def exercise() -> None:
        (grant,) = await repository.create_many(
            owner_agent_id=repository_client.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=repository_client.agent_id),
            source_tool_call_id=source_tool_call_id,
            grants=(_SPEC,),
            created_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
        assert grant.status is GrantStatus.ACTIVE
        assert grant.spec == _SPEC
        assert (await repository.get(owner_agent_id=repository_client.agent_id, grant_id=grant.grant_id)) == grant
        assert await repository.active_for_request_principal(
            request_principal=RequestPrincipal(
                agent_id=repository_client.agent_id, session_id=None, access_profile_id=None
            ),
            now=_NOW,
        ) == (grant,)

        ended = await repository.end(
            owner_agent_ids=frozenset({repository_client.agent_id}),
            grant_id=grant.grant_id,
            reason="no longer needed",
            now=_NOW + timedelta(minutes=1),
        )
        assert ended.status is GrantStatus.ENDED
        assert ended.ended_at == _NOW + timedelta(minutes=1)
        assert (
            await repository.active_for_request_principal(
                request_principal=RequestPrincipal(
                    agent_id=repository_client.agent_id, session_id=None, access_profile_id=None
                ),
                now=_NOW + timedelta(minutes=2),
            )
            == ()
        )

    repository_client.call(exercise)


def test_repository_persists_permanent_grants(repository_client: _RepositoryClient) -> None:
    source_tool_call_id = repository_client.call(
        partial(_insert_http_source, repository_client.sessions, binding_id=repository_client.binding_id, now=_NOW)
    )
    repository = PostgresGrantRepository(repository_client.sessions)

    async def exercise() -> None:
        (grant,) = await repository.create_many(
            owner_agent_id=repository_client.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=repository_client.agent_id),
            source_tool_call_id=source_tool_call_id,
            grants=(_SPEC,),
            created_at=_NOW,
            expires_at=None,
        )
        principal = RequestPrincipal(agent_id=repository_client.agent_id, session_id=None, access_profile_id=None)
        assert grant.expires_at is None
        assert await repository.active_for_request_principal(
            request_principal=principal, now=_NOW + timedelta(days=365)
        ) == (grant,)
        assert (
            await repository.end(
                owner_agent_ids=frozenset({repository_client.agent_id}), grant_id=grant.grant_id, reason=None, now=_NOW
            )
        ).status is GrantStatus.ENDED

    repository_client.call(exercise)


def test_repository_persists_an_access_profile_principal(repository_client: _RepositoryClient) -> None:
    source_tool_call_id = repository_client.call(
        partial(_insert_http_source, repository_client.sessions, binding_id=repository_client.binding_id, now=_NOW)
    )
    repository = PostgresGrantRepository(repository_client.sessions)

    async def exercise() -> None:
        (grant,) = await repository.create_many(
            owner_agent_id=repository_client.agent_id,
            grant_principal=AccessProfileGrantPrincipal(access_profile_id=DEFAULT_ACCESS_PROFILE_ID),
            source_tool_call_id=source_tool_call_id,
            grants=(_SPEC,),
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        )
        assert grant.principal == AccessProfileGrantPrincipal(access_profile_id=DEFAULT_ACCESS_PROFILE_ID)
        assert await repository.active_for_request_principal(
            request_principal=RequestPrincipal(
                agent_id=uuid4(), session_id=None, access_profile_id=DEFAULT_ACCESS_PROFILE_ID
            ),
            now=_NOW,
        ) == (grant,)

    repository_client.call(exercise)


def test_repository_persists_the_allow_prohibited_address_flag(make_client: Any) -> None:
    """The capability column round-trips through create and a fresh read; the default-false shape is
    covered by the specs above, whose equality holds only if the reread flag matches."""
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresGrantRepository(sessions)
        flagged = GrantSpec(
            origin=HttpOrigin(scheme=HttpScheme.HTTP, host="gateway.internal.example", port=4000),
            coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.POST})),
            allow_prohibited_address=True,
        )

        async def exercise() -> None:
            (grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(flagged,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(hours=1),
            )
            assert grant.spec == flagged
            reread = await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id)
            assert reread.spec.allow_prohibited_address is True

        client.portal.call(exercise)


def test_expiry_is_derived_and_ending_an_expired_grant_records_nothing(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresGrantRepository(sessions)

        async def exercise() -> None:
            # A lease whose whole window is behind the clock: written directly at this layer,
            # since the window bound is the service's validation, not the store's.
            (grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(_SPEC,),
                created_at=_NOW - timedelta(hours=2),
                expires_at=_NOW - timedelta(hours=1),
            )
            past_expiry = _NOW
            # No sweeper ran, yet every read past the bound derives EXPIRED and excludes it.
            expired = await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id)
            assert expired.status is GrantStatus.EXPIRED
            assert expired.ended_at is None
            assert expired.end_reason is None
            assert (
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None),
                    now=past_expiry,
                )
                == ()
            )
            # A late end does not relabel the lease: expiry had already won.
            late = await repository.end(
                owner_agent_ids=frozenset({agent_id}), grant_id=grant.grant_id, reason="too late", now=past_expiry
            )
            assert late.status is GrantStatus.EXPIRED
            assert late.ended_at is None
            assert late.end_reason is None

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
        repository = PostgresGrantRepository(sessions)

        async def exercise() -> None:
            grants = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(_SPEC, _OTHER_SPEC),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert len(grants) == 2
            assert len({grant.grant_id for grant in grants}) == 2
            assert [grant.spec for grant in grants] == [_SPEC, _OTHER_SPEC]
            assert {grant.source_tool_call_id for grant in grants} == {source_tool_call_id}
            assert {grant.created_at for grant in grants} == {_NOW}
            assert {grant.expires_at for grant in grants} == {_NOW + timedelta(minutes=5)}

            retried = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(_SPEC, _OTHER_SPEC),
                created_at=_NOW + timedelta(seconds=10),
                expires_at=_NOW + timedelta(minutes=10),
            )
            assert tuple(grant.grant_id for grant in retried) == tuple(grant.grant_id for grant in grants)
            assert {grant.created_at for grant in retried} == {_NOW}
            assert {grant.expires_at for grant in retried} == {_NOW + timedelta(minutes=5)}

            with pytest.raises(GrantSourceError, match="already created a different"):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                    source_tool_call_id=source_tool_call_id,
                    grants=(_SPEC,),
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
        # The source Agent may request a grant for another live session; the operator approval
        # on the source ToolCall, not source-session identity equality, authorizes that choice.
        session_source = client.portal.call(partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW))
        repository = PostgresGrantRepository(sessions)

        async def exercise() -> None:
            (agent_grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=agent_source,
                grants=(_SPEC,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            (session_grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=SessionGrantPrincipal(session_id=session_id),
                source_tool_call_id=session_source,
                grants=(_SPEC,),
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
                    request_principal=RequestPrincipal(
                        agent_id=agent_id, session_id=session_id, access_profile_id=None
                    ),
                    now=_NOW,
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
            with pytest.raises(GrantSourceError, match="live session"):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=SessionGrantPrincipal(session_id=session_id),
                    source_tool_call_id=ended_source,
                    grants=(_SPEC,),
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=5),
                )

        client.portal.call(exercise)


def test_repository_rejects_auto_approved_and_foreign_sources(make_client: Any) -> None:
    """Provenance requires a manually approved call authenticated by the owner — the exact tool
    name is deliberately not pinned, so any of the owner's manually approved calls qualifies."""

    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        auto_approved = client.portal.call(
            partial(
                _insert_http_source, sessions, binding_id=binding_id, now=_NOW, approval_policy_id="unsafe-test-policy"
            )
        )
        repository = PostgresGrantRepository(sessions)

        async def exercise() -> None:
            for source_tool_call_id in [auto_approved, "tc_never_recorded"]:
                with pytest.raises(GrantSourceError, match="manually approved"):
                    await repository.create_many(
                        owner_agent_id=agent_id,
                        grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                        source_tool_call_id=source_tool_call_id,
                        grants=(_SPEC,),
                        created_at=_NOW,
                        expires_at=_NOW + timedelta(minutes=5),
                    )

        client.portal.call(exercise)


def test_database_holds_the_end_fact_shape(make_client: Any) -> None:
    """The one retained DB-side invariant family: at most one end action, reason iff ended."""

    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresGrantRepository(sessions)

        async def exercise() -> None:
            (grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(_SPEC,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            for statement in [
                text("UPDATE http_grants SET ended_at = :n, end_reason = '  ' WHERE grant_id = :grant_id"),
                text("UPDATE http_grants SET end_reason = 'reason without an end action' WHERE grant_id = :grant_id"),
            ]:
                with pytest.raises(IntegrityError, match="ck_http_grants_end_shape"):
                    async with sessions.begin() as session:
                        await session.execute(statement, {"n": _NOW + timedelta(minutes=1), "grant_id": grant.grant_id})

        client.portal.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
