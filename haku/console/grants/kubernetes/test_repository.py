"""PostgreSQL lifecycle and provenance tests for temporary Kubernetes grants."""

from __future__ import annotations

import json
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
from haku.console.database_schema import KubernetesGrantRow
from haku.console.grants.envelope import GrantNotFoundError, GrantSourceError, GrantStatus
from haku.console.grants.kubernetes.models import (
    KubernetesAllNamespacesGrantScope,
    KubernetesClusterGrantScope,
    KubernetesGrantSpec,
    KubernetesNamespacesGrantScope,
    KubernetesNonResourceGrantScope,
    KubernetesRule,
)
from haku.console.grants.kubernetes.repository import PostgresKubernetesGrantRepository
from haku.console.grants.principal import (
    AgentGrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)

# Relative: KubernetesGrant.status is computed against the live clock, so windows anchor to it.
_NOW = datetime.now(UTC)
_RULE = KubernetesRule(api_groups=("",), resources=("pods",), verbs=("get",))
_SCOPE = KubernetesNamespacesGrantScope(namespaces=("diagnostics", "public-coder-agent"))
_CLUSTER_RULE = KubernetesRule(api_groups=("",), resources=("nodes",), verbs=("get",))
_NON_RESOURCE_RULE = KubernetesRule(non_resource_urls=("/version",), verbs=("get",))
_RAW_GRANT_INSERT = text(
    """
    INSERT INTO kubernetes_grants (
        grant_id, owner_agent_id, principal_kind, principal_agent_id, principal_session_id,
        source_tool_call_id, scope, rules, created_at, expires_at
    ) VALUES (
        :grant_id, :owner_agent_id, :principal_kind, :principal_agent_id, :principal_session_id,
        :source_tool_call_id, CAST(:scope AS jsonb), CAST(:rules AS jsonb),
        :created_at, :expires_at
    )
    """
)


async def _insert_raw_grant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    source_tool_call_id: str,
    scope: dict[str, object],
    rule: KubernetesRule,
    principal_kind: GrantPrincipalKind = GrantPrincipalKind.AGENT,
    principal_agent_id: UUID | None = None,
    principal_session_id: UUID | None = None,
) -> None:
    async with sessions.begin() as session:
        await session.execute(
            _RAW_GRANT_INSERT,
            {
                "grant_id": uuid4(),
                "owner_agent_id": agent_id,
                "principal_kind": principal_kind,
                "principal_agent_id": (
                    agent_id
                    if principal_kind is GrantPrincipalKind.AGENT and principal_agent_id is None
                    else principal_agent_id
                ),
                "principal_session_id": principal_session_id,
                "source_tool_call_id": source_tool_call_id,
                "scope": json.dumps(scope),
                "rules": json.dumps([rule.model_dump(mode="json")]),
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
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresKubernetesGrantRepository(sessions)

        async def exercise() -> None:
            grant = await repository.create(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                scope=_SCOPE,
                rules=(_RULE,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert grant.status is GrantStatus.ACTIVE
            assert (await repository.get(owner_agent_id=agent_id, grant_id=grant.grant_id)) == grant
            assert await repository.active_for_request_principal(
                request_principal=RequestPrincipal(agent_id=agent_id, session_id=None, access_profile_id=None), now=_NOW
            ) == (grant,)

            released = await repository.release(
                owner_agent_id=agent_id,
                grant_id=grant.grant_id,
                reason="no longer needed",
                now=_NOW + timedelta(minutes=1),
            )
            assert released.status is GrantStatus.RELEASED
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
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresKubernetesGrantRepository(sessions)

        async def exercise() -> None:
            grants = await repository.create_many(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                grants=(
                    KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),
                    KubernetesGrantSpec(scope=KubernetesClusterGrantScope(), rules=(_CLUSTER_RULE,)),
                ),
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
                grants=(
                    KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),
                    KubernetesGrantSpec(scope=KubernetesClusterGrantScope(), rules=(_CLUSTER_RULE,)),
                ),
                created_at=_NOW + timedelta(seconds=10),
                expires_at=_NOW + timedelta(minutes=10),
            )
            assert tuple(grant.grant_id for grant in retried) == tuple(grant.grant_id for grant in grants)
            assert {grant.created_at for grant in retried} == {_NOW}
            assert {grant.expires_at for grant in retried} == {_NOW + timedelta(minutes=5)}

            with pytest.raises(GrantNotFoundError):
                await repository.revoke_source(
                    owner_agent_id=uuid4(),
                    source_tool_call_id=source_tool_call_id,
                    reason="must not cross Agent ownership",
                    now=_NOW + timedelta(seconds=20),
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
                reason="first scope no longer needed",
                now=_NOW + timedelta(seconds=30),
            )
            assert released_first.status is GrantStatus.RELEASED

            revoked = await repository.revoke_source(
                owner_agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                reason="operator ended probe",
                now=_NOW + timedelta(minutes=1),
            )
            assert {grant.grant_id for grant in revoked} == {grant.grant_id for grant in grants}
            by_id = {grant.grant_id: grant for grant in revoked}
            assert by_id[grants[0].grant_id].status is GrantStatus.RELEASED
            assert by_id[grants[0].grant_id].end_reason == "first scope no longer needed"
            assert by_id[grants[1].grant_id].status is GrantStatus.REVOKED
            assert by_id[grants[1].grant_id].revoked_at == _NOW + timedelta(minutes=1)
            assert by_id[grants[1].grant_id].end_reason == "operator ended probe"

            repeated = await repository.revoke_source(
                owner_agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                reason="different retry reason",
                now=_NOW + timedelta(minutes=2),
            )
            assert repeated == revoked

            with pytest.raises(GrantSourceError, match="already created a different"):
                await repository.create_many(
                    owner_agent_id=agent_id,
                    grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                    source_tool_call_id=source_tool_call_id,
                    grants=(KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),),
                    created_at=_NOW + timedelta(seconds=10),
                    expires_at=_NOW + timedelta(minutes=10),
                )

            async with sessions() as session:
                rows = (
                    await session.scalars(
                        select(KubernetesGrantRow).where(KubernetesGrantRow.source_tool_call_id == source_tool_call_id)
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
        agent_source = client.portal.call(partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW))
        session_source = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW, session_id=session_id)
        )
        repository = PostgresKubernetesGrantRepository(sessions)

        async def exercise() -> None:
            agent_grant = await repository.create(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=agent_source,
                scope=_SCOPE,
                rules=(_RULE,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            session_grant = await repository.create(
                owner_agent_id=agent_id,
                grant_principal=SessionGrantPrincipal(session_id=session_id),
                source_tool_call_id=session_source,
                scope=_SCOPE,
                rules=(_RULE,),
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
            ended_source = await insert_approved_tool_call(
                sessions, binding_id=binding_id, now=_NOW, session_id=session_id
            )
            with pytest.raises(GrantSourceError, match="durable source ToolCall principal"):
                await repository.create(
                    owner_agent_id=agent_id,
                    grant_principal=SessionGrantPrincipal(session_id=session_id),
                    source_tool_call_id=ended_source,
                    scope=_SCOPE,
                    rules=(_RULE,),
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=5),
                )

        client.portal.call(exercise)


@pytest.mark.parametrize(
    ("scope", "rule"),
    [
        (KubernetesAllNamespacesGrantScope(), _RULE),
        (KubernetesClusterGrantScope(), _CLUSTER_RULE),
        (KubernetesNonResourceGrantScope(), _NON_RESOURCE_RULE),
    ],
)
def test_repository_persists_canonical_non_exact_scope_shapes(
    make_client: Any,
    scope: KubernetesAllNamespacesGrantScope | KubernetesClusterGrantScope | KubernetesNonResourceGrantScope,
    rule: KubernetesRule,
) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )
        repository = PostgresKubernetesGrantRepository(sessions)

        async def exercise() -> None:
            grant = await repository.create(
                owner_agent_id=agent_id,
                grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                source_tool_call_id=source_tool_call_id,
                scope=scope,
                rules=(rule,),
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=5),
            )
            assert grant.scope == scope

        client.portal.call(exercise)


def test_repository_rejects_wrong_or_auto_approved_source(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        wrong_tool = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW, tool_name="list_grants")
        )
        auto_approved = client.portal.call(
            partial(
                insert_approved_tool_call,
                sessions,
                binding_id=binding_id,
                now=_NOW,
                approval_policy_id="unsafe-test-policy",
            )
        )
        repository = PostgresKubernetesGrantRepository(sessions)

        async def rejected(source_tool_call_id: str) -> None:
            with pytest.raises(GrantSourceError):
                await repository.create(
                    owner_agent_id=agent_id,
                    grant_principal=AgentGrantPrincipal(agent_id=agent_id),
                    source_tool_call_id=source_tool_call_id,
                    scope=_SCOPE,
                    rules=(_RULE,),
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=5),
                )

        client.portal.call(rejected, wrong_tool)
        client.portal.call(rejected, auto_approved)


def test_database_rejects_grants_with_invalid_source_provenance(make_client: Any) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        wrong_tool = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW, tool_name="list_grants")
        )

        async def rejected() -> None:
            with pytest.raises(IntegrityError, match="invalid Kubernetes grant source provenance"):
                async with sessions.begin() as session:
                    session.add(
                        KubernetesGrantRow(
                            grant_id=uuid4(),
                            owner_agent_id=agent_id,
                            principal_kind=GrantPrincipalKind.AGENT,
                            principal_agent_id=agent_id,
                            principal_session_id=None,
                            source_tool_call_id=wrong_tool,
                            scope=_SCOPE,
                            rules=[_RULE],
                            created_at=_NOW,
                            expires_at=_NOW + timedelta(minutes=5),
                            end_reason=None,
                        )
                    )

        client.portal.call(rejected)


@pytest.mark.parametrize(
    ("scope", "rule"),
    [
        ({"kind": "all_namespaces"}, _RULE),
        ({"kind": "cluster"}, _CLUSTER_RULE),
        ({"kind": "non_resource"}, _NON_RESOURCE_RULE),
    ],
)
def test_database_accepts_canonical_non_exact_scope_shape(
    make_client: Any, scope: dict[str, object], rule: KubernetesRule
) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )

        client.portal.call(
            partial(
                _insert_raw_grant,
                sessions,
                agent_id=agent_id,
                source_tool_call_id=source_tool_call_id,
                scope=scope,
                rule=rule,
            )
        )


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"kind": "unknown"},
        {"kind": "namespaces"},
        {"kind": "namespaces", "namespaces": []},
        {"kind": "namespaces", "namespaces": "default"},
        {"kind": "all_namespaces", "namespaces": []},
        {"kind": "cluster", "namespaces": []},
        {"kind": "non_resource", "namespaces": []},
    ],
)
def test_database_rejects_invalid_scope_shape(make_client: Any, scope: dict[str, object]) -> None:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        source_tool_call_id = client.portal.call(
            partial(insert_approved_tool_call, sessions, binding_id=binding_id, now=_NOW)
        )

        async def rejected() -> None:
            with pytest.raises(IntegrityError, match="ck_kubernetes_grants_scope_shape"):
                await _insert_raw_grant(
                    sessions, agent_id=agent_id, source_tool_call_id=source_tool_call_id, scope=scope, rule=_RULE
                )

        client.portal.call(rejected)


if __name__ == "__main__":
    pytest_bazel.main()
