"""Service lifecycle and matcher contracts independent of PostgreSQL transport."""

from __future__ import annotations

import datetime
from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from haku.console.grants.envelope import GrantNotFoundError, GrantStatus, derive_status
from haku.console.grants.http.models import Grant, GrantSpec, HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.grants.http.service import GrantService
from haku.console.grants.principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    RequestPrincipal,
    SessionGrantPrincipal,
    grant_principal_applies_to,
)

_NOW = datetime.datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_OTHER_AGENT = UUID("10000000-0000-4000-8000-000000000002")
_GRANT_PRINCIPAL = AgentGrantPrincipal(agent_id=_AGENT)
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443)
_OTHER_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=443)
_SPEC = GrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))
_OTHER_SPEC = GrantSpec(origin=_OTHER_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))


def _request_principal(agent_id: UUID = _AGENT, session_id: UUID | None = None) -> RequestPrincipal:
    return RequestPrincipal(agent_id=agent_id, session_id=session_id, access_profile_id=None)


def _grant(
    *,
    spec: GrantSpec = _SPEC,
    principal: GrantPrincipal = _GRANT_PRINCIPAL,
    created_at: datetime.datetime = _NOW,
    expires_at: datetime.datetime,
    released_at: datetime.datetime | None = None,
    revoked_at: datetime.datetime | None = None,
    end_reason: str | None = None,
) -> Grant:
    return Grant(
        grant_id=uuid4(),
        owner_agent_id=_AGENT,
        principal=principal,
        source_tool_call_id="tool-call-preexisting",
        spec=spec,
        created_at=created_at,
        expires_at=expires_at,
        released_at=released_at,
        revoked_at=revoked_at,
        end_reason=end_reason,
    )


class FakeRepository:
    """Facts-holding in-memory double: like the real store, status is derived at read time."""

    def __init__(self) -> None:
        self.grants: dict[UUID, Grant] = {}
        self.release_calls: list[tuple[UUID, UUID, str, datetime.datetime]] = []
        self.revoke_calls: list[tuple[UUID, UUID, str, datetime.datetime]] = []

    @staticmethod
    def _active(grant: Grant, now: datetime.datetime) -> bool:
        return (
            derive_status(
                released_at=grant.released_at, revoked_at=grant.revoked_at, expires_at=grant.expires_at, now=now
            )
            is GrantStatus.ACTIVE
        )

    async def create_many(
        self, *, owner_agent_id, grant_principal, source_tool_call_id, grants, created_at, expires_at
    ):
        created = tuple(
            Grant(
                grant_id=uuid4(),
                owner_agent_id=owner_agent_id,
                principal=grant_principal,
                source_tool_call_id=source_tool_call_id,
                spec=spec,
                created_at=created_at,
                expires_at=expires_at,
            )
            for spec in grants
        )
        self.grants.update((grant.grant_id, grant) for grant in created)
        return created

    async def list(self, *, owner_agent_id, now, include_terminal=True):
        return tuple(grant for grant in self.grants.values() if grant.owner_agent_id == owner_agent_id)

    async def list_for_request_principal(self, *, request_principal, now, include_terminal=True):
        return tuple(
            grant
            for grant in self.grants.values()
            if grant_principal_applies_to(grant.principal, request_principal)
            and (include_terminal or self._active(grant, now))
        )

    async def get(self, *, owner_agent_id, grant_id):
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        return grant

    async def active_for_request_principal(self, *, request_principal, now):
        return tuple(
            grant
            for grant in self.grants.values()
            if grant_principal_applies_to(grant.principal, request_principal) and self._active(grant, now)
        )

    async def release(self, *, owner_agent_id, grant_id, reason, now):
        self.release_calls.append((owner_agent_id, grant_id, reason, now))
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        self.grants[grant_id] = grant.model_copy(update={"released_at": now, "end_reason": reason})
        return self.grants[grant_id]

    async def revoke(self, *, owner_agent_id, grant_id, reason, now):
        self.revoke_calls.append((owner_agent_id, grant_id, reason, now))
        grant = self.grants[grant_id]
        assert grant.owner_agent_id == owner_agent_id
        self.grants[grant_id] = grant.model_copy(update={"revoked_at": now, "end_reason": reason})
        return self.grants[grant_id]


async def test_create_and_match_require_the_explicit_agent_id() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    assert grant.owner_agent_id == _AGENT
    assert grant.principal == _GRANT_PRINCIPAL
    assert (
        await service.match_request(
            request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="/"
        )
    ).allowed
    assert not (
        await service.match_request(
            request_principal=_request_principal(agent_id=_OTHER_AGENT), method=HttpMethod.GET, origin=_ORIGIN, path="/"
        )
    ).allowed


async def test_match_covers_only_the_exact_origin_method_and_path() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(
            GrantSpec(
                origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}), path_regex="/api/.*")
            ),
        ),
        expires_at=_NOW + timedelta(minutes=5),
    )

    covered = await service.match_request(
        request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="/api/items"
    )
    assert covered.allowed
    for method, origin, path in [
        (HttpMethod.GET, _ORIGIN.model_copy(update={"port": 8443}), "/api/items"),
        (HttpMethod.GET, _ORIGIN.model_copy(update={"scheme": HttpScheme.HTTP}), "/api/items"),
        (HttpMethod.GET, _ORIGIN.model_copy(update={"host": "sub." + _ORIGIN.host}), "/api/items"),
        (HttpMethod.GET, _OTHER_ORIGIN, "/api/items"),
        (HttpMethod.POST, _ORIGIN, "/api/items"),
        (HttpMethod.GET, _ORIGIN, "/outside"),
    ]:
        decision = await service.match_request(
            request_principal=_request_principal(), method=method, origin=origin, path=path
        )
        assert not decision.allowed
        assert decision.reason


async def test_match_requires_an_absolute_path() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="absolute path"):
        await service.match_request(
            request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="api/items"
        )


async def test_tunnel_matches_the_exact_origin_ignoring_method_and_path_coverage() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(
            GrantSpec(
                origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.POST}), path_regex="/api/.*")
            ),
        ),
        expires_at=_NOW + timedelta(minutes=5),
    )

    admitted = await service.match_tunnel(request_principal=_request_principal(), origin=_ORIGIN)
    assert admitted.allowed
    for principal, origin in [
        (_request_principal(agent_id=_OTHER_AGENT), _ORIGIN),
        (_request_principal(), _OTHER_ORIGIN),
        (_request_principal(), _ORIGIN.model_copy(update={"scheme": HttpScheme.HTTP, "port": 80})),
    ]:
        decision = await service.match_tunnel(request_principal=principal, origin=origin)
        assert not decision.allowed
        assert decision.reason == "no active HTTP grant covers the origin"


async def test_tunnel_admission_carries_the_earliest_expiry() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (earlier,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=5),
    )
    await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-2",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=50),
    )

    decision = await service.match_tunnel(request_principal=_request_principal(), origin=_ORIGIN)

    assert decision.allowed
    assert decision.grant_id == earlier.grant_id
    assert decision.expires_at == earlier.expires_at


async def test_match_reports_credential_handles_from_every_matching_grant() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(
            GrantSpec(
                origin=_ORIGIN,
                coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                credential_handle="github-bot",
            ),
        ),
        expires_at=_NOW + timedelta(minutes=50),
    )
    (reachability,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-2",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    request = await service.match_request(
        request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="/"
    )
    tunnel = await service.match_tunnel(request_principal=_request_principal(), origin=_ORIGIN)
    for decision in (request, tunnel):
        assert decision.allowed
        # Redemption is reported from every matching grant while the pure-reachability grant
        # still bounds the admission with the earliest expiry.
        assert decision.credential_handles == frozenset({"github-bot"})
        assert decision.grant_id == reachability.grant_id
        assert decision.expires_at == reachability.expires_at


async def test_principal_lifecycle_inherits_agent_grants_without_crossing_sessions() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    session_a, session_b = uuid4(), uuid4()
    (agent_grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=AgentGrantPrincipal(agent_id=_AGENT),
        source_tool_call_id="tool-call-agent",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=5),
    )
    (session_grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=SessionGrantPrincipal(session_id=session_a),
        source_tool_call_id="tool-call-session",
        grants=(_SPEC,),
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
    with pytest.raises(GrantNotFoundError):
        await service.get_applicable_grant(request_principal=request_principal_b, grant_id=session_grant.grant_id)


async def test_create_many_uses_one_source_and_shared_timestamps() -> None:
    repo = FakeRepository()
    clock_calls = 0

    def clock() -> datetime.datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=clock)
    expires_at = _NOW + timedelta(minutes=5)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC, _OTHER_SPEC),
        expires_at=expires_at,
    )

    assert len(grants) == 2
    assert {grant.source_tool_call_id for grant in grants} == {"tool-call-1"}
    assert {grant.created_at for grant in grants} == {_NOW}
    assert {grant.expires_at for grant in grants} == {expires_at}
    assert clock_calls == 1


async def test_create_many_enforces_the_tool_batch_limit_in_the_service() -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="at most 32 grants"):
        await service.create_grants(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id="tool-call-1",
            grants=tuple(
                GrantSpec(
                    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host=f"host{index}.example", port=443),
                    coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                )
                for index in range(33)
            ),
            expires_at=_NOW + timedelta(minutes=5),
        )


async def test_release_many_is_bounded_sequential_and_uses_one_timestamp() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC, _OTHER_SPEC),
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


@pytest.mark.parametrize(
    ("grant_ids", "message"),
    [
        ((), "must not be empty"),
        ((UUID(int=1),) * 2, "must not contain duplicates"),
        (tuple(UUID(int=value) for value in range(1, 34)), "at most 32 grants"),
    ],
)
async def test_end_batches_reject_invalid_lists(grant_ids, message) -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=grant_ids)
    with pytest.raises(ValueError, match=message):
        await service.revoke_grants(owner_agent_id=_AGENT, grant_ids=grant_ids, reason="cleanup")


async def test_release_many_keeps_earlier_releases_when_a_later_item_fails() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (grant,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=5),
    )

    with pytest.raises(KeyError):
        await service.release_grants(owner_agent_id=_AGENT, grant_ids=[grant.grant_id, UUID(int=9)])

    assert repo.grants[grant.grant_id].status is GrantStatus.RELEASED


async def test_revoke_many_is_bounded_sequential_and_uses_one_timestamp() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    grants = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC, _OTHER_SPEC),
        expires_at=_NOW + timedelta(minutes=5),
    )

    revoked = await service.revoke_grants(
        owner_agent_id=_AGENT, grant_ids=[grants[0].grant_id, grants[1].grant_id], reason="operator ended"
    )

    assert all(grant.status is GrantStatus.REVOKED for grant in revoked)
    assert repo.revoke_calls == [
        (_AGENT, grants[0].grant_id, "operator ended", _NOW),
        (_AGENT, grants[1].grant_id, "operator ended", _NOW),
    ]


@pytest.mark.parametrize(
    ("source_tool_call_id", "grants", "expires_at", "message"),
    [
        ("", (_SPEC,), _NOW + timedelta(minutes=5), "source_tool_call_id must not be empty"),
        ("tool-call-1", (), _NOW + timedelta(minutes=5), "grants must not be empty"),
        ("tool-call-1", (_SPEC,), _NOW.replace(tzinfo=None), "expires_at must be timezone-aware"),
        ("tool-call-1", (_SPEC,), _NOW, "expires_at must be in the future"),
        ("tool-call-1", (_SPEC,), _NOW + timedelta(hours=2), "configured grant lifetime"),
    ],
)
async def test_create_rejects_invalid_input(source_tool_call_id, grants, expires_at, message) -> None:
    service = GrantService(FakeRepository(), max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        await service.create_grants(
            owner_agent_id=_AGENT,
            grant_principal=_GRANT_PRINCIPAL,
            source_tool_call_id=source_tool_call_id,
            grants=grants,
            expires_at=expires_at,
        )


async def test_match_and_get_derive_expiry_without_writing() -> None:
    repo = FakeRepository()
    grant = _grant(created_at=_NOW - timedelta(minutes=10), expires_at=_NOW - timedelta(minutes=1))
    repo.grants[grant.grant_id] = grant
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)

    assert not (
        await service.match_request(
            request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="/"
        )
    ).allowed
    fetched = await service.get_grant(owner_agent_id=_AGENT, grant_id=grant.grant_id)
    assert fetched.status is GrantStatus.EXPIRED
    # The stored facts never changed: expiry is derived, not swept.
    assert repo.grants[grant.grant_id].released_at is None
    assert repo.grants[grant.grant_id].revoked_at is None


async def test_match_returns_the_earliest_expiration_bound() -> None:
    repo = FakeRepository()
    service = GrantService(repo, max_lifetime=timedelta(hours=1), clock=lambda: _NOW)
    (first,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-1",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=10),
    )
    (second,) = await service.create_grants(
        owner_agent_id=_AGENT,
        grant_principal=_GRANT_PRINCIPAL,
        source_tool_call_id="tool-call-2",
        grants=(_SPEC,),
        expires_at=_NOW + timedelta(minutes=2),
    )

    decision = await service.match_request(
        request_principal=_request_principal(), method=HttpMethod.GET, origin=_ORIGIN, path="/"
    )

    assert decision.allowed
    assert decision.grant_id == second.grant_id
    assert decision.expires_at == second.expires_at
    assert decision.grant_id != first.grant_id


if __name__ == "__main__":
    pytest_bazel.main()
