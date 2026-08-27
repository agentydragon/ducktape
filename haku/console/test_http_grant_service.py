"""Service lifecycle and matcher contracts independent of PostgreSQL transport."""

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
from haku.console.http_grant_models import HttpGrant, HttpGrantNotFoundError, HttpGrantStatus, HttpOrigin, HttpScheme
from haku.console.http_grant_service import HttpGrantService

_NOW = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_OTHER_AGENT = UUID("10000000-0000-4000-8000-000000000002")
_GRANT_PRINCIPAL = AgentGrantPrincipal(agent_id=_AGENT)
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443)
_OTHER_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=443)


def _request_principal(agent_id: UUID = _AGENT, session_id: UUID | None = None) -> RequestPrincipal:
    return RequestPrincipal(agent_id=agent_id, session_id=session_id, access_profile_id=None)


class FakeRepository:
    def __init__(self) -> None:
        self.grants: dict[UUID, HttpGrant] = {}
        self.expire_calls: list[tuple[UUID | None, datetime]] = []
        self.release_calls: list[tuple[UUID, UUID, str, datetime]] = []
        self.revoke_source_calls: list[tuple[UUID, str, str, datetime]] = []

    async def create_many(
        self, *, owner_agent_id, grant_principal, source_tool_call_id, origins, created_at, expires_at
    ):
        created = tuple(
            HttpGrant(
                grant_id=uuid4(),
                owner_agent_id=owner_agent_id,
                principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
                origin=origin,
                status=HttpGrantStatus.ACTIVE,
                created_at=created_at,
                expires_at=expires_at,
            )
            for origin in origins
        )
        self.grants.update((grant.grant_id, grant) for grant in created)
        return created

    async def expire(self, *, owner_agent_id=None, now):
        self.expire_calls.append((owner_agent_id, now))
        expired = 0
        for grant_id, grant in tuple(self.grants.items()):
            if (
                grant.status is HttpGrantStatus.ACTIVE
                and grant.expires_at <= now
                and (owner_agent_id is None or grant.owner_agent_id == owner_agent_id)
            ):
                self.grants[grant_id] = grant.model_copy(
                    update={"status": HttpGrantStatus.EXPIRED, "ended_at": now, "end_reason": "expired"}
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
            and (include_terminal or grant.status is HttpGrantStatus.ACTIVE)
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
            and g.status is HttpGrantStatus.ACTIVE
            and g.expires_at > now
        )

    async def release(self, *, owner_agent_id, grant_id, reason, ended_at):
        self.release_calls.append((owner_agent_id, grant_id, reason, ended_at))
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        released = grant.model_copy(
            update={"status": HttpGrantStatus.RELEASED, "ended_at": ended_at, "end_reason": reason}
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
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    assert grant.owner_agent_id == _AGENT
    assert grant.principal == _GRANT_PRINCIPAL
    assert (await service.match_request(request_principal=_request_principal(), origin=_ORIGIN)).allowed
    assert not (
        await service.match_request(request_principal=_request_principal(agent_id=_OTHER_AGENT), origin=_ORIGIN)
    ).allowed
    assert repo.expire_calls == []


@pytest.mark.asyncio
async def test_match_covers_only_the_exact_origin() -> None:
    service = HttpGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    for origin in (
        _ORIGIN.model_copy(update={"port": 8443}),
        _ORIGIN.model_copy(update={"scheme": HttpScheme.HTTP}),
        _ORIGIN.model_copy(update={"host": "sub." + _ORIGIN.host}),
        _OTHER_ORIGIN,
    ):
        decision = await service.match_request(request_principal=_request_principal(), origin=origin)
        assert not decision.allowed
        assert decision.grant_id is None


@pytest.mark.asyncio
async def test_principal_lifecycle_inherits_agent_grants_without_crossing_sessions() -> None:
    repo = FakeRepository()
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    session_a, session_b = uuid4(), uuid4()
    (agent_grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=AgentGrantPrincipal(agent_id=_AGENT),
        source_tool_call_id="tool-call-agent",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=5),
    )
    (session_grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=SessionGrantPrincipal(session_id=session_a),
        source_tool_call_id="tool-call-session",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    request_principal_a = _request_principal(session_id=session_a)
    assert set(await service.list_applicable_grants(request_principal=request_principal_a)) == {
        agent_grant,
        session_grant,
    }
    assert (
        await service.get_applicable_grant(request_principal=request_principal_a, grant_id=session_grant.grant_id)
        == session_grant
    )

    request_principal_b = _request_principal(session_id=session_b)
    assert await service.list_applicable_grants(request_principal=request_principal_b) == (agent_grant,)
    with pytest.raises(HttpGrantNotFoundError):
        await service.get_applicable_grant(request_principal=request_principal_b, grant_id=session_grant.grant_id)


@pytest.mark.asyncio
async def test_create_many_uses_one_source_and_shared_timestamps() -> None:
    repo = FakeRepository()
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=clock)
    expires_at = _NOW + timedelta(minutes=5)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN, _OTHER_ORIGIN),
        expires_at=expires_at,
    )

    assert len(grants) == 2
    assert {grant.source_tool_call_id for grant in grants} == {"tool-call-1"}
    assert {grant.created_at for grant in grants} == {_NOW}
    assert {grant.expires_at for grant in grants} == {expires_at}
    assert clock_calls == 1


@pytest.mark.asyncio
async def test_create_many_enforces_the_tool_batch_limit_in_the_service() -> None:
    service = HttpGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="at most 32 grants"):
        await service.create_grants(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id="tool-call-1",
            origins=tuple(
                HttpOrigin(scheme=HttpScheme.HTTPS, host=f"host{index}.example", port=443) for index in range(33)
            ),
            expires_at=_NOW + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_release_many_is_bounded_sequential_and_uses_one_timestamp() -> None:
    repo = FakeRepository()
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN, _OTHER_ORIGIN),
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
    service = HttpGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=grant_ids)


@pytest.mark.asyncio
async def test_release_many_keeps_earlier_releases_when_a_later_item_fails() -> None:
    repo = FakeRepository()
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    with pytest.raises(KeyError):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=[grant.grant_id, UUID(int=9)])

    assert repo.grants[grant.grant_id].status is HttpGrantStatus.RELEASED


@pytest.mark.asyncio
async def test_revoke_grant_set_uses_source_tool_call_as_lifecycle_unit() -> None:
    repo = FakeRepository()
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    await service.revoke_grant_set(owner_agent_id=_AGENT, source_tool_call_id="tool-call-1", reason="operator ended")

    assert repo.revoke_source_calls == [(_AGENT, "tool-call-1", "operator ended", _NOW)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_tool_call_id", "origins", "expires_at", "message"),
    [
        ("", (_ORIGIN,), _NOW + timedelta(minutes=5), "source_tool_call_id must not be empty"),
        ("tool-call-1", (), _NOW + timedelta(minutes=5), "origins must not be empty"),
        ("tool-call-1", (_ORIGIN,), _NOW.replace(tzinfo=None), "expires_at must be timezone-aware"),
        ("tool-call-1", (_ORIGIN,), _NOW, "expires_at must be in the future"),
        ("tool-call-1", (_ORIGIN,), _NOW + timedelta(hours=2), "configured grant lifetime"),
    ],
)
async def test_create_rejects_invalid_input(source_tool_call_id, origins, expires_at, message) -> None:
    service = HttpGrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.create_grants(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id=source_tool_call_id,
            origins=origins,
            expires_at=expires_at,
        )


@pytest.mark.asyncio
async def test_match_ignores_expired_rows_without_writing() -> None:
    repo = FakeRepository()
    grant = HttpGrant(
        grant_id=uuid4(),
        owner_agent_id=_AGENT,
        principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origin=_ORIGIN,
        status=HttpGrantStatus.ACTIVE,
        created_at=_NOW - timedelta(minutes=10),
        expires_at=_NOW - timedelta(minutes=1),
    )
    repo.grants[grant.grant_id] = grant
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    assert not (await service.match_request(request_principal=_request_principal(), origin=_ORIGIN)).allowed
    assert repo.expire_calls == []


@pytest.mark.asyncio
async def test_get_uses_one_timestamp_when_expiring_a_grant() -> None:
    repo = FakeRepository()
    grant = HttpGrant(
        grant_id=uuid4(),
        owner_agent_id=_AGENT,
        principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origin=_ORIGIN,
        status=HttpGrantStatus.ACTIVE,
        created_at=_NOW - timedelta(minutes=10),
        expires_at=_NOW - timedelta(minutes=1),
    )
    repo.grants[grant.grant_id] = grant
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=clock)

    result = await service.get_grant(owner_agent_id=_AGENT, grant_id=grant.grant_id)

    assert result.status is HttpGrantStatus.EXPIRED
    assert repo.expire_calls == [(_AGENT, _NOW)]
    assert clock_calls == 1


@pytest.mark.asyncio
async def test_match_returns_the_earliest_expiration_bound() -> None:
    repo = FakeRepository()
    service = HttpGrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (first,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=10),
    )
    (second,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-2",
        origins=(_ORIGIN,),
        expires_at=_NOW + timedelta(minutes=2),
    )

    decision = await service.match_request(request_principal=_request_principal(), origin=_ORIGIN)

    assert decision.allowed
    assert decision.grant_id == second.grant_id
    assert decision.expires_at == second.expires_at
    assert decision.grant_id != first.grant_id


if __name__ == "__main__":
    pytest_bazel.main()
