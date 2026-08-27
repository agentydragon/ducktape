"""Service lifecycle contracts independent of PostgreSQL transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from haku.console.grant_principal import (
    AgentGrantPrincipal,
    RequestPrincipal,
    SessionGrantPrincipal,
    grant_principal_applies_to,
)
from haku.console.kubernetes_grant_models import (
    KubernetesGrant,
    KubernetesGrantNotFoundError,
    KubernetesGrantSpec,
    KubernetesGrantStatus,
    KubernetesNamespacesGrantScope,
    KubernetesRule,
)
from haku.console.kubernetes_grant_service import KubernetesGrantService

_NOW = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_OTHER_AGENT = UUID("10000000-0000-4000-8000-000000000002")
_GRANT_PRINCIPAL = AgentGrantPrincipal(agent_id=_AGENT)
_SCOPE = KubernetesNamespacesGrantScope(namespaces=("default", "diagnostics"))
_DEFAULT_SCOPE = KubernetesNamespacesGrantScope(namespaces=("default",))


def _rule(verb: str = "get") -> KubernetesRule:
    return KubernetesRule(api_groups=("",), resources=("pods",), verbs=(verb,))


class FakeRepository:
    def __init__(self) -> None:
        self.grants: dict[UUID, KubernetesGrant] = {}
        self.expire_calls: list[tuple[UUID | None, datetime]] = []
        self.release_calls: list[tuple[UUID, UUID, str, datetime]] = []
        self.revoke_source_calls: list[tuple[UUID, str, str, datetime]] = []

    async def create(
        self, *, owner_agent_id, grant_principal, source_tool_call_id, scope, rules, created_at, expires_at
    ):
        created = await self.create_many(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=(KubernetesGrantSpec(scope=scope, rules=tuple(rules)),),
            created_at=created_at,
            expires_at=expires_at,
        )
        return created[0]

    async def create_many(
        self, *, owner_agent_id, grant_principal, source_tool_call_id, grants, created_at, expires_at
    ):
        created = tuple(
            KubernetesGrant(
                grant_id=uuid4(),
                owner_agent_id=owner_agent_id,
                principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
                scope=grant.scope,
                rules=grant.rules,
                status=KubernetesGrantStatus.ACTIVE,
                created_at=created_at,
                expires_at=expires_at,
            )
            for grant in grants
        )
        self.grants.update((grant.grant_id, grant) for grant in created)
        return created

    async def expire(self, *, owner_agent_id=None, now):
        self.expire_calls.append((owner_agent_id, now))
        expired = 0
        for grant_id, grant in tuple(self.grants.items()):
            if (
                grant.status is KubernetesGrantStatus.ACTIVE
                and grant.expires_at <= now
                and (owner_agent_id is None or grant.owner_agent_id == owner_agent_id)
            ):
                self.grants[grant_id] = grant.model_copy(
                    update={"status": KubernetesGrantStatus.EXPIRED, "ended_at": now, "end_reason": "expired"}
                )
                expired += 1
        return expired

    async def list(self, *, owner_agent_id, include_terminal=True):
        return tuple(g for g in self.grants.values() if g.owner_agent_id == owner_agent_id)

    async def list_for_request_principal(self, *, request_principal, include_terminal=True):
        return tuple(
            grant
            for grant in self.grants.values()
            if grant_principal_applies_to(grant.principal, request_principal)
            and (include_terminal or grant.status is KubernetesGrantStatus.ACTIVE)
        )

    async def get(self, *, owner_agent_id, grant_id):
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        return grant

    async def active_for_request_principal(self, *, request_principal, now):
        return tuple(
            g
            for g in self.grants.values()
            if grant_principal_applies_to(g.principal, request_principal)
            and g.status is KubernetesGrantStatus.ACTIVE
            and g.expires_at > now
        )

    async def release(self, *, owner_agent_id, grant_id, reason, ended_at):
        self.release_calls.append((owner_agent_id, grant_id, reason, ended_at))
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        released = grant.model_copy(
            update={"status": KubernetesGrantStatus.RELEASED, "ended_at": ended_at, "end_reason": reason}
        )
        self.grants[grant_id] = released
        return released

    async def revoke(self, **kwargs):
        raise AssertionError("not used by this test")

    async def revoke_source(self, *, owner_agent_id, source_tool_call_id, reason, ended_at):
        self.revoke_source_calls.append((owner_agent_id, source_tool_call_id, reason, ended_at))
        return tuple(
            grant
            for grant in self.grants.values()
            if grant.owner_agent_id == owner_agent_id and grant.source_tool_call_id == source_tool_call_id
        )


@pytest.mark.asyncio
async def test_create_and_match_require_the_explicit_agent_id() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grant = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=5),
    )

    assert grant.owner_agent_id == _AGENT
    assert grant.principal == _GRANT_PRINCIPAL
    assert (
        await service.match_request(
            request_principal=RequestPrincipal(agent_id=_AGENT, session_id=None, access_profile_id=None),
            required_scope=_DEFAULT_SCOPE,
            required_rules=(_rule(),),
        )
    ).allowed
    assert not (
        await service.match_request(
            request_principal=RequestPrincipal(agent_id=_OTHER_AGENT, session_id=None, access_profile_id=None),
            required_scope=_DEFAULT_SCOPE,
            required_rules=(_rule(),),
        )
    ).allowed
    assert repo.expire_calls == []


@pytest.mark.asyncio
async def test_principal_lifecycle_inherits_agent_grants_without_crossing_sessions() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    session_a, session_b = uuid4(), uuid4()
    agent_grant = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=AgentGrantPrincipal(agent_id=_AGENT),
        source_tool_call_id="tool-call-agent",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=5),
    )
    session_grant = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=SessionGrantPrincipal(session_id=session_a),
        source_tool_call_id="tool-call-session",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=5),
    )

    request_principal_a = RequestPrincipal(agent_id=_AGENT, session_id=session_a, access_profile_id=None)
    assert set(await service.list_applicable_grants(request_principal=request_principal_a)) == {
        agent_grant,
        session_grant,
    }
    assert (
        await service.get_applicable_grant(request_principal=request_principal_a, grant_id=session_grant.grant_id)
        == session_grant
    )

    request_principal_b = RequestPrincipal(agent_id=_AGENT, session_id=session_b, access_profile_id=None)
    assert await service.list_applicable_grants(request_principal=request_principal_b) == (agent_grant,)
    with pytest.raises(KubernetesGrantNotFoundError):
        await service.get_applicable_grant(request_principal=request_principal_b, grant_id=session_grant.grant_id)


@pytest.mark.asyncio
async def test_create_many_uses_one_source_and_shared_timestamps() -> None:
    repo = FakeRepository()
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=clock)
    expires_at = _NOW + timedelta(minutes=5)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(
            KubernetesGrantSpec(scope=_SCOPE, rules=(_rule(),)),
            KubernetesGrantSpec(scope=_DEFAULT_SCOPE, rules=(_rule("list"),)),
        ),
        expires_at=expires_at,
    )

    assert len(grants) == 2
    assert {grant.source_tool_call_id for grant in grants} == {"tool-call-1"}
    assert {grant.created_at for grant in grants} == {_NOW}
    assert {grant.expires_at for grant in grants} == {expires_at}
    assert clock_calls == 1


@pytest.mark.asyncio
async def test_create_many_enforces_the_tool_batch_limit_in_the_service() -> None:
    service = KubernetesGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="at most 32 grants"):
        await service.create_grants(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id="tool-call-1",
            grants=tuple(KubernetesGrantSpec(scope=_SCOPE, rules=(_rule(),)) for _ in range(33)),
            expires_at=_NOW + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_release_many_is_bounded_sequential_and_uses_one_timestamp() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(
            KubernetesGrantSpec(scope=_SCOPE, rules=(_rule(),)),
            KubernetesGrantSpec(scope=_DEFAULT_SCOPE, rules=(_rule("list"),)),
        ),
        expires_at=_NOW + timedelta(minutes=5),
    )

    released = await service.release_grants(
        owner_agent_id=_AGENT, grant_ids=[grants[1].grant_id, grants[0].grant_id], reason="probe complete"
    )

    assert [grant.grant_id for grant in released] == [grants[1].grant_id, grants[0].grant_id]
    assert repo.release_calls == [
        (_AGENT, grants[1].grant_id, "probe complete", _NOW),
        (_AGENT, grants[0].grant_id, "probe complete", _NOW),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grant_ids", "message"),
    [
        ((), "must not be empty"),
        ((UUID(int=1),) * 2, "must not contain duplicates"),
        (tuple(UUID(int=value) for value in range(1, 34)), "at most 32 grants"),
    ],
)
async def test_release_many_rejects_invalid_lists(grant_ids, message) -> None:
    service = KubernetesGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=grant_ids)


@pytest.mark.asyncio
async def test_release_many_rejects_a_blank_reason_before_mutating() -> None:
    service = KubernetesGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="reason must not be empty"):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=[UUID(int=1)], reason="   ")


@pytest.mark.asyncio
async def test_release_many_keeps_earlier_releases_when_a_later_item_fails() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grant = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=5),
    )

    with pytest.raises(KeyError):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=[grant.grant_id, UUID(int=9)])

    assert repo.grants[grant.grant_id].status is KubernetesGrantStatus.RELEASED


@pytest.mark.asyncio
async def test_revoke_grant_set_uses_source_tool_call_as_lifecycle_unit() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    await service.revoke_grant_set(
        owner_agent_id=_AGENT, source_tool_call_id="tool-call-1", reason="operator ended probe"
    )

    assert repo.revoke_source_calls == [(_AGENT, "tool-call-1", "operator ended probe", _NOW)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_tool_call_id", "rules", "expires_at", "message"),
    [
        ("", (_rule(),), _NOW + timedelta(minutes=5), "source_tool_call_id must not be empty"),
        ("tool-call-1", (), _NOW + timedelta(minutes=5), "rules must not be empty"),
        ("tool-call-1", (_rule(),), _NOW.replace(tzinfo=None), "expires_at must be timezone-aware"),
        ("tool-call-1", (_rule(),), _NOW, "expires_at must be in the future"),
        ("tool-call-1", (_rule(),), _NOW + timedelta(hours=2), "configured grant lifetime"),
    ],
)
async def test_create_rejects_invalid_input(source_tool_call_id, rules, expires_at, message) -> None:
    service = KubernetesGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.create_grant(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id=source_tool_call_id,
            scope=_SCOPE,
            rules=rules,
            expires_at=expires_at,
        )


@pytest.mark.asyncio
async def test_match_ignores_expired_rows_without_writing() -> None:
    repo = FakeRepository()
    grant = KubernetesGrant(
        grant_id=uuid4(),
        owner_agent_id=_AGENT,
        principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        scope=_SCOPE,
        rules=(_rule(),),
        status=KubernetesGrantStatus.ACTIVE,
        created_at=_NOW - timedelta(minutes=10),
        expires_at=_NOW - timedelta(minutes=1),
    )
    repo.grants[grant.grant_id] = grant
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    assert not (
        await service.match_request(
            request_principal=RequestPrincipal(agent_id=_AGENT, session_id=None, access_profile_id=None),
            required_scope=_DEFAULT_SCOPE,
            required_rules=(_rule(),),
        )
    ).allowed
    assert repo.expire_calls == []


@pytest.mark.asyncio
async def test_get_uses_one_timestamp_when_expiring_a_grant() -> None:
    repo = FakeRepository()
    grant = KubernetesGrant(
        grant_id=uuid4(),
        owner_agent_id=_AGENT,
        principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        scope=_SCOPE,
        rules=(_rule(),),
        status=KubernetesGrantStatus.ACTIVE,
        created_at=_NOW - timedelta(minutes=10),
        expires_at=_NOW - timedelta(minutes=1),
    )
    repo.grants[grant.grant_id] = grant
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=clock)

    result = await service.get_grant(owner_agent_id=_AGENT, grant_id=grant.grant_id)

    assert result.status is KubernetesGrantStatus.EXPIRED
    assert repo.expire_calls == [(_AGENT, _NOW)]
    assert clock_calls == 1


@pytest.mark.asyncio
async def test_match_returns_the_earliest_expiration_bound() -> None:
    repo = FakeRepository()
    service = KubernetesGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    first = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=10),
    )
    second = await service.create_grant(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-2",
        scope=_SCOPE,
        rules=(_rule(),),
        expires_at=_NOW + timedelta(minutes=2),
    )

    decision = await service.match_request(
        request_principal=RequestPrincipal(agent_id=_AGENT, session_id=None, access_profile_id=None),
        required_scope=_DEFAULT_SCOPE,
        required_rules=(_rule(),),
    )

    assert decision.allowed
    assert decision.grant_id == second.grant_id
    assert decision.expires_at == second.expires_at
    assert decision.grant_id != first.grant_id


if __name__ == "__main__":
    pytest_bazel.main()
