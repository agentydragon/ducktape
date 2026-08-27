"""PostgreSQL lifecycle and provenance tests for temporary HTTP grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import default_agent_binding, insert_approved_tool_call, insert_live_session
from haku.console.database_schema import HttpGrantRow
from haku.console.grant_principal import AgentGrantPrincipal, RequestPrincipal, SessionGrantPrincipal
from haku.console.http_grant_models import (
    HttpGrantSourceError,
    HttpGrantSpec,
    HttpGrantStatus,
    HttpMethod,
    HttpOrigin,
    HttpScheme,
)
from haku.console.http_grant_repository import PostgresHttpGrantRepository

_NOW = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_SPEC = HttpGrantSpec(
    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443), methods=frozenset({HttpMethod.GET})
)
_OTHER_SPEC = HttpGrantSpec(
    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=8443),
    methods=frozenset({HttpMethod.GET, HttpMethod.POST}),
    path_regex="/v1/.*",
    credential_handle="github-bot",
)

_insert_http_source = partial(insert_approved_tool_call, server_id="http_grants")


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
                grants=(_SPEC,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert grant.status is HttpGrantStatus.ACTIVE
            assert grant.spec == _SPEC
            assert (await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id, now=_NOW)) == grant
            assert await repository.active_for_request_principal(
                request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None), now=_NOW
            ) == (grant,)

            released = await repository.release(
                owner_agent_id=agent_id,
                grant_id=grant.grant_id,
                reason="no longer needed",
                now=_NOW + timedelta(minutes=1),
            )
            assert released.status is HttpGrantStatus.RELEASED
            assert released.released_at == _NOW + timedelta(minutes=1)
            assert released.revoked_at is None
            assert (
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None),
                    now=_NOW + timedelta(minutes=2),
                )
                == ()
            )

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
        repository = PostgresHttpGrantRepository(sessions)

        async def exercise() -> None:
            (grant,) = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(_SPEC,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            past_expiry = _NOW + timedelta(minutes=10)
            # No sweeper ran, yet every read past the bound derives EXPIRED and excludes it.
            expired = await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id, now=past_expiry)
            assert expired.status is HttpGrantStatus.EXPIRED
            assert expired.released_at is None
            assert expired.revoked_at is None
            assert expired.end_reason is None
            assert (
                await repository.active_for_request_principal(
                    request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None),
                    now=past_expiry,
                )
                == ()
            )
            # A late release does not relabel the lease: expiry had already won.
            late = await repository.release(
                owner_agent_id=agent_id, grant_id=grant.grant_id, reason="too late", now=past_expiry
            )
            assert late.status is HttpGrantStatus.EXPIRED
            assert late.released_at is None
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
        repository = PostgresHttpGrantRepository(sessions)

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

            with pytest.raises(HttpGrantSourceError, match="already created a different"):
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
        session_source = client.portal.call(
            partial(_insert_http_source, sessions, binding_id=binding_id, now=_NOW, session_id=session_id)
        )
        repository = PostgresHttpGrantRepository(sessions)

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
            with pytest.raises(HttpGrantSourceError, match="durable source ToolCall principal"):
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
        repository = PostgresHttpGrantRepository(sessions)

        async def exercise() -> None:
            for source_tool_call_id in [auto_approved, "tc_never_recorded"]:
                with pytest.raises(HttpGrantSourceError, match="manually approved"):
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
        repository = PostgresHttpGrantRepository(sessions)

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
                text(
                    "UPDATE http_grants SET released_at = :n, revoked_at = :n, end_reason = 'both' "
                    "WHERE grant_id = :grant_id"
                ),
                text("UPDATE http_grants SET released_at = :n WHERE grant_id = :grant_id"),
                text("UPDATE http_grants SET end_reason = 'reason without an end action' WHERE grant_id = :grant_id"),
                text("UPDATE http_grants SET released_at = :n, end_reason = '  ' WHERE grant_id = :grant_id"),
            ]:
                with pytest.raises(IntegrityError, match="ck_http_grants_end_shape"):
                    async with sessions.begin() as session:
                        await session.execute(statement, {"n": _NOW + timedelta(minutes=1), "grant_id": grant.grant_id})

        client.portal.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
