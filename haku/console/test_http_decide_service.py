"""Decide-service contracts: fence-credential binding, canonicalization, grant evaluation."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from datetime import UTC, timedelta
from ipaddress import IPv4Address
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    RequestPrincipal,
    grant_principal_applies_to,
)
from haku.console.http_decide_service import HttpDecideService, HttpDecideUnavailableError
from haku.console.http_grant_models import HttpGrant, HttpGrantSpec, HttpMethod, HttpOrigin, HttpScheme, derive_status
from haku.console.http_grant_service import HttpGrantService
from haku.console.mcp_config import (
    EgressDecideConfig,
    EgressFenceCredentialEntry,
    LoadedEgressDecide,
    LoadedFenceCredential,
    load_egress_decide,
)
from haku.egress.decision import DecideAllowed, DecideDenied, DecideRequest, DecisionSource, RequestMeta

_NOW = datetime.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_OTHER_AGENT = UUID("10000000-0000-4000-8000-000000000002")
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=443)
_PROXY_TOKEN = "proxy-identity-token"
_FENCE = "agent-fence-credential"
_OTHER_FENCE = "other-agent-fence-credential"


class _Repository:
    """Only what decide evaluation reads; grant lifecycle is unreachable from the endpoint."""

    def __init__(self, grants: Sequence[HttpGrant] = ()) -> None:
        self.grants = tuple(grants)
        self.fail: Exception | None = None

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[HttpGrant, ...]:
        if self.fail is not None:
            raise self.fail
        return tuple(
            grant
            for grant in self.grants
            if grant_principal_applies_to(grant.principal, request_principal) and now < grant.expires_at
        )

    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[HttpGrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]:
        raise NotImplementedError

    async def list(
        self, *, owner_agent_id: UUID, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        raise NotImplementedError

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID, now: datetime.datetime) -> HttpGrant:
        raise NotImplementedError

    async def release(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        raise NotImplementedError

    async def revoke(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime) -> HttpGrant:
        raise NotImplementedError

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        raise NotImplementedError


def _grant(*, agent_id: UUID = _AGENT, spec: HttpGrantSpec, expires_at: datetime.datetime) -> HttpGrant:
    return HttpGrant(
        grant_id=uuid4(),
        owner_agent_id=agent_id,
        principal=AgentGrantPrincipal(agent_id=agent_id),
        source_tool_call_id="tool-call-preexisting",
        spec=spec,
        status=derive_status(released_at=None, revoked_at=None, expires_at=expires_at, now=_NOW),
        created_at=_NOW - timedelta(minutes=5),
        expires_at=expires_at,
    )


def _service(grants: Sequence[HttpGrant] = ()) -> tuple[HttpDecideService, _Repository]:
    repository = _Repository(grants)
    return HttpDecideService(
        grants=HttpGrantService(repository, max_lifetime=timedelta(hours=1), clock=lambda: _NOW),
        credentials=LoadedEgressDecide(
            proxy_token=SecretStr(_PROXY_TOKEN),
            fence_credentials=[
                LoadedFenceCredential(agent_id=_AGENT, token=SecretStr(_FENCE)),
                LoadedFenceCredential(agent_id=_OTHER_AGENT, token=SecretStr(_OTHER_FENCE)),
            ],
        ),
    ), repository


def _request(
    *,
    fence_credential: str = _FENCE,
    method: str = "GET",
    scheme: str | None = "https",
    host: str = "api.example",
    port: int = 443,
    path: str | None = "/",
) -> DecideRequest:
    return DecideRequest(
        fence_credential=SecretStr(fence_credential),
        request=RequestMeta(method=method, scheme=scheme, host=host, port=port, path=path),
        resolved_ips=frozenset({IPv4Address("192.0.2.10")}),
        upstream_ip=IPv4Address("192.0.2.10"),
    )


def test_proxy_bearer_must_match_exactly() -> None:
    service, _ = _service()
    assert service.authenticate_proxy(f"Bearer {_PROXY_TOKEN}")
    assert service.authenticate_proxy(f"bearer {_PROXY_TOKEN}")
    for rejected in (
        "",
        _PROXY_TOKEN,
        "Bearer",
        f"Bearer {_PROXY_TOKEN}x",
        f"Basic {_PROXY_TOKEN}",
        f"Bearer {_FENCE}",
    ):
        assert not service.authenticate_proxy(rejected)


async def test_unknown_fence_credential_denies_without_grant_scope() -> None:
    service, _ = _service()

    decision = await service.decide(_request(fence_credential="not-a-configured-credential"))

    assert decision == DecideDenied(reason="unknown fence credential", grant_scope=None)


async def test_grant_admits_only_its_own_agents_fence_credential() -> None:
    spec = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.GET}))
    grant = _grant(spec=spec, expires_at=_NOW + timedelta(minutes=30))
    service, _ = _service([grant])

    allowed = await service.decide(_request())
    assert isinstance(allowed, DecideAllowed)
    assert allowed.source is DecisionSource.GRANT
    assert allowed.decision_id == f"grant:{grant.grant_id}"
    assert allowed.valid_until == grant.expires_at
    assert allowed.substitutions == []

    denied = await service.decide(_request(fence_credential=_OTHER_FENCE))
    assert isinstance(denied, DecideDenied)
    assert denied.grant_scope is not None
    assert (denied.grant_scope.scheme, denied.grant_scope.host, denied.grant_scope.port) == (
        "https",
        "api.example",
        443,
    )


async def test_method_and_path_regex_scope_the_verdict() -> None:
    spec = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.GET}), path_regex="/api/.*")
    service, _ = _service([_grant(spec=spec, expires_at=_NOW + timedelta(minutes=30))])

    allowed = await service.decide(_request(path="/api/items"))
    assert isinstance(allowed, DecideAllowed)

    for miss in (
        _request(method="POST", path="/api/items"),
        _request(path="/elsewhere"),
        _request(host="other.example"),
        _request(port=8443),
        _request(scheme="http", port=80),
    ):
        decision = await service.decide(miss)
        assert isinstance(decision, DecideDenied)
        assert decision.reason == "no active HTTP grant covers the request"


async def test_path_regex_sees_the_query_exactly_as_sent() -> None:
    # Ruled in #4884: coverage regexes evaluate against the path plus query as the proxy sends
    # it, so an exact-path pin does not admit the same path with a query string appended.
    exact = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.GET}), path_regex="/api/items")
    service, _ = _service([_grant(spec=exact, expires_at=_NOW + timedelta(minutes=30))])
    assert isinstance(await service.decide(_request(path="/api/items")), DecideAllowed)
    assert isinstance(await service.decide(_request(path="/api/items?state=open")), DecideDenied)

    prefixed = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.GET}), path_regex="/api/.*")
    service, _ = _service([_grant(spec=prefixed, expires_at=_NOW + timedelta(minutes=30))])
    assert isinstance(await service.decide(_request(path="/api/items?state=open")), DecideAllowed)


async def test_connect_is_admitted_by_any_https_grant_at_the_origin() -> None:
    spec = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.POST}), path_regex="/api/.*")
    grant = _grant(spec=spec, expires_at=_NOW + timedelta(minutes=30))
    service, _ = _service([grant])

    allowed = await service.decide(_request(method="CONNECT", scheme=None, path=None))
    assert isinstance(allowed, DecideAllowed)
    assert allowed.decision_id == f"grant:{grant.grant_id}"

    denied = await service.decide(_request(method="CONNECT", scheme=None, path=None, host="other.example"))
    assert isinstance(denied, DecideDenied)
    assert denied.reason == "no active HTTP grant covers the origin"


async def test_connect_never_matches_http_scheme_grants() -> None:
    # A tunnel transports TLS, so a cleartext-origin grant cannot admit it.
    http_origin = HttpOrigin(scheme=HttpScheme.HTTP, host="api.example", port=80)
    spec = HttpGrantSpec(origin=http_origin, methods=frozenset({HttpMethod.GET}))
    service, _ = _service([_grant(spec=spec, expires_at=_NOW + timedelta(minutes=30))])

    decision = await service.decide(_request(method="CONNECT", scheme=None, path=None, port=80))

    assert isinstance(decision, DecideDenied)


async def test_earliest_expiry_bounds_the_admission() -> None:
    spec = HttpGrantSpec(origin=_ORIGIN, methods=frozenset({HttpMethod.GET}))
    earlier = _grant(spec=spec, expires_at=_NOW + timedelta(minutes=10))
    later = _grant(spec=spec, expires_at=_NOW + timedelta(minutes=50))
    service, _ = _service([later, earlier])

    decision = await service.decide(_request())

    assert isinstance(decision, DecideAllowed)
    assert decision.decision_id == f"grant:{earlier.grant_id}"
    assert decision.valid_until == earlier.expires_at


async def test_ungrantable_metadata_denies_with_a_reason() -> None:
    service, _ = _service()
    for request, reason in [
        (_request(method="CONNECT", scheme=None, path="/tunnel"), "malformed CONNECT metadata"),
        (_request(method="CONNECT", path=None), "malformed CONNECT metadata"),
        (_request(scheme=None), "malformed request metadata"),
        (_request(path=None), "malformed request metadata"),
        (_request(path="relative/path"), "malformed request metadata"),
        (_request(method="TRACE"), "method is not grantable"),
        (_request(method="get"), "method is not grantable"),
        (_request(scheme="ftp"), "origin is not grantable"),
        (_request(host="192.0.2.10"), "origin is not grantable"),
        (_request(method="CONNECT", scheme=None, path=None, host="203.0.113.7"), "origin is not grantable"),
    ]:
        decision = await service.decide(request)
        assert decision == DecideDenied(reason=reason, grant_scope=None), request.request


async def test_grant_authority_failure_raises_unavailable() -> None:
    service, repository = _service()
    repository.fail = RuntimeError("database is down")

    with pytest.raises(HttpDecideUnavailableError):
        await service.decide(_request())


def test_egress_decide_config_requires_distinct_env_references() -> None:
    with pytest.raises(ValueError, match="distinct"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            fence_credentials=[EgressFenceCredentialEntry(agent_id=_AGENT, token_env_var="EGRESS_TOKEN")],
        )


def test_load_egress_decide_reads_env_references_and_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_PROXY_TOKEN",
        fence_credentials=[EgressFenceCredentialEntry(agent_id=_AGENT, token_env_var="EGRESS_FENCE_A")],
    )
    monkeypatch.delenv("EGRESS_PROXY_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="EGRESS_PROXY_TOKEN"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    with pytest.raises(RuntimeError, match="EGRESS_FENCE_A"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_FENCE_A", _PROXY_TOKEN)
    with pytest.raises(RuntimeError, match="duplicate"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_FENCE_A", _FENCE)
    loaded = load_egress_decide(config)
    assert loaded.proxy_token.get_secret_value() == _PROXY_TOKEN
    (credential,) = loaded.fence_credentials
    assert credential.agent_id == _AGENT
    assert credential.token.get_secret_value() == _FENCE


if __name__ == "__main__":
    pytest_bazel.main()
