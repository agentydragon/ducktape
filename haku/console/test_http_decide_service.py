"""Decide-service integration contracts on real PostgreSQL: fence binding, evaluation, fail-closed.

Grants are created through the real repository with real approved-ToolCall provenance, so these
tests exercise the same storage, status derivation, and principal filtering the endpoint uses in
production. Only the authority-failure test points the repository at an unreachable database —
that failure mode is the test's subject.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from haku.console.conftest import default_agent_binding, insert_approved_tool_call
from haku.console.grant_principal import AgentGrantPrincipal
from haku.console.http_decide_config import (
    EgressStandingPolicyEntry,
    LoadedEgressCredential,
    LoadedEgressDecide,
    LoadedFenceCredential,
)
from haku.console.http_decide_service import HttpDecideService, HttpDecideUnavailableError
from haku.console.http_grant_models import HttpGrantSpec, HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.http_grant_repository import PostgresHttpGrantRepository
from haku.console.http_grant_service import HttpGrantService
from haku.egress.decision import (
    DecideAllowed,
    DecideDenied,
    DecideRequest,
    DecisionSource,
    PlaceholderSubstitution,
    RequestMeta,
)

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.example", port=443)
_PROXY_TOKEN = "proxy-identity-token"
_FENCE = "agent-fence-credential"
_OTHER_FENCE = "other-agent-fence-credential"
# Configured fence identity that owns no grants: isolation must come from principal filtering.
_UNGRANTED_AGENT = UUID("10000000-0000-4000-8000-000000000099")
_GITHUB_VALUE = "ghp-real-bot-token"
# Genuinely global unicast addresses (example.com / Cloudflare DNS): the always-prohibited set
# covers the RFC 5737/3849 documentation ranges (they are ``is_private``), so those cannot
# stand in for public resolutions here.
_PUBLIC_V4 = IPv4Address("93.184.216.34")
_PUBLIC_V6 = IPv6Address("2606:4700::1111")

_insert_http_source = partial(insert_approved_tool_call, server_id="http_grants")


def _github_credential(agent_id: UUID, **overrides: Any) -> LoadedEgressCredential:
    fields: dict[str, Any] = {
        "handle": "github-bot",
        "placeholder": "github-token-placeholder",
        "value": SecretStr(_GITHUB_VALUE),
        "match_headers": frozenset({"authorization"}),
        "agent_ids": frozenset({agent_id}),
        "origins": frozenset({_ORIGIN}),
    }
    return LoadedEgressCredential(**{**fields, **overrides})


def _standing_entry(agent_id: UUID, **overrides: Any) -> EgressStandingPolicyEntry:
    """Build a standing entry; ``methods``/``path_regex`` overrides populate the nested coverage."""
    coverage_fields: dict[str, Any] = {"methods": frozenset({HttpMethod.GET})}
    for key in ("methods", "path_regex"):
        if key in overrides:
            coverage_fields[key] = overrides.pop(key)
    fields: dict[str, Any] = {
        "id": "api-standing",
        "agent_ids": frozenset({agent_id}),
        "origins": frozenset({_ORIGIN}),
        "coverage": HttpRequestCoverage(**coverage_fields),
    }
    return EgressStandingPolicyEntry(**{**fields, **overrides})


@dataclass(frozen=True)
class _Harness:
    decide: HttpDecideService
    grants: HttpGrantService
    sessions: async_sessionmaker[AsyncSession]
    agent_id: UUID
    binding_id: UUID


def _harness(
    client: Any,
    *,
    credentials: Callable[[UUID], list[LoadedEgressCredential]] | None = None,
    standing: Callable[[UUID], list[EgressStandingPolicyEntry]] | None = None,
    prohibited_cidrs: frozenset[IPv4Network | IPv6Network] = frozenset(),
) -> _Harness:
    app = cast(FastAPI, client.app)
    sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
    assert client.portal is not None
    agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
    grants = HttpGrantService(
        PostgresHttpGrantRepository(sessions), max_lifetime=timedelta(hours=1), clock=lambda: _NOW
    )
    decide = HttpDecideService(
        grants=grants,
        credentials=LoadedEgressDecide(
            proxy_token=SecretStr(_PROXY_TOKEN),
            fence_credentials=[
                LoadedFenceCredential(agent_id=agent_id, token=SecretStr(_FENCE)),
                LoadedFenceCredential(agent_id=_UNGRANTED_AGENT, token=SecretStr(_OTHER_FENCE)),
            ],
            credentials=credentials(agent_id) if credentials is not None else [],
            standing_policies=standing(agent_id) if standing is not None else [],
        ),
        prohibited_cidrs=prohibited_cidrs,
    )
    return _Harness(decide=decide, grants=grants, sessions=sessions, agent_id=agent_id, binding_id=binding_id)


def _create_grants(client: Any, harness: _Harness, *specs: HttpGrantSpec, expires_at: datetime) -> tuple[UUID, ...]:
    """Create one approved-ToolCall grant set through the real repository; returns grant ids."""
    source_tool_call_id = client.portal.call(
        partial(_insert_http_source, harness.sessions, binding_id=harness.binding_id, now=_NOW)
    )

    async def create() -> tuple[UUID, ...]:
        created = await harness.grants.create_grants(
            owner_agent_id=harness.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=harness.agent_id),
            source_tool_call_id=source_tool_call_id,
            grants=specs,
            expires_at=expires_at,
        )
        return tuple(grant.grant_id for grant in created)

    return cast(tuple[UUID, ...], client.portal.call(create))


def _request(
    *,
    fence_credential: str = _FENCE,
    method: str = "GET",
    scheme: str | None = "https",
    host: str = "api.example",
    port: int = 443,
    path: str | None = "/",
    resolved_ips: frozenset[IPv4Address | IPv6Address] = frozenset({_PUBLIC_V4}),
    upstream_ip: IPv4Address | IPv6Address = _PUBLIC_V4,
) -> DecideRequest:
    return DecideRequest(
        fence_credential=SecretStr(fence_credential),
        request=RequestMeta(method=method, scheme=scheme, host=host, port=port, path=path),
        resolved_ips=resolved_ips,
        upstream_ip=upstream_ip,
    )


def test_proxy_bearer_and_fence_identity_gate_evaluation(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client)
        service = harness.decide

        assert service.authenticate_proxy(f"Bearer {_PROXY_TOKEN}")
        assert service.authenticate_proxy(f"bearer {_PROXY_TOKEN}")
        for rejected in ("", _PROXY_TOKEN, "Bearer", f"Bearer {_PROXY_TOKEN}x", f"Basic {_PROXY_TOKEN}"):
            assert not service.authenticate_proxy(rejected)
        # The Agent-bound fence credential is not the proxy's endpoint bearer.
        assert not service.authenticate_proxy(f"Bearer {_FENCE}")

        decision = client.portal.call(partial(service.decide, _request(fence_credential="not-configured")))
        assert decision == DecideDenied(reason="unknown fence credential", grant_scope=None)


def test_grant_scoped_verdicts_against_stored_grants(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client)
        prefix_spec = HttpGrantSpec(
            origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}), path_regex="/api/.*")
        )
        exact_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="exact.example", port=443)
        exact_spec = HttpGrantSpec(
            origin=exact_origin,
            coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}), path_regex="/api/items"),
        )
        prefix_grant_id, _ = _create_grants(
            client, harness, prefix_spec, exact_spec, expires_at=_NOW + timedelta(minutes=30)
        )
        decide = partial(client.portal.call, harness.decide.decide)

        allowed = decide(_request(path="/api/items?state=open"))
        assert allowed == DecideAllowed(
            source=DecisionSource.GRANT,
            decision_id=f"grant:{prefix_grant_id}",
            valid_until=_NOW + timedelta(minutes=30),
            substitutions=[],
        )

        # Ruled in #4884: the coverage regex sees the path plus query exactly as the proxy sends
        # it, so an exact-path pin does not admit the same path with a query string appended.
        assert decide(_request(host="exact.example", path="/api/items")).allowed
        exact_miss = decide(_request(host="exact.example", path="/api/items?state=open"))
        assert isinstance(exact_miss, DecideDenied)

        for miss in (
            _request(method="POST", path="/api/items"),
            _request(path="/elsewhere"),
            _request(host="other.example", path="/api/items"),
            _request(port=8443, path="/api/items"),
            _request(scheme="http", port=80, path="/api/items"),
            _request(fence_credential=_OTHER_FENCE, path="/api/items"),
        ):
            decision = decide(miss)
            assert isinstance(decision, DecideDenied), miss.request
            assert decision.reason == "no active HTTP grant covers the request"
        denied = decide(_request(fence_credential=_OTHER_FENCE, path="/api/items"))
        assert isinstance(denied, DecideDenied)
        assert denied.grant_scope is not None
        assert (denied.grant_scope.scheme, denied.grant_scope.host, denied.grant_scope.port) == (
            "https",
            "api.example",
            443,
        )


def test_connect_tunnel_admission(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client)
        https_spec = HttpGrantSpec(
            origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.POST}), path_regex="/api/.*")
        )
        cleartext_origin = HttpOrigin(scheme=HttpScheme.HTTP, host="plain.example", port=80)
        cleartext_spec = HttpGrantSpec(
            origin=cleartext_origin, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}))
        )
        https_grant_id, _ = _create_grants(
            client, harness, https_spec, cleartext_spec, expires_at=_NOW + timedelta(minutes=30)
        )
        decide = partial(client.portal.call, harness.decide.decide)

        # Any active https grant at the origin admits the tunnel, whatever its method/path pins.
        allowed = decide(_request(method="CONNECT", scheme=None, path=None))
        assert isinstance(allowed, DecideAllowed)
        assert allowed.decision_id == f"grant:{https_grant_id}"

        unknown = decide(_request(method="CONNECT", scheme=None, path=None, host="other.example"))
        assert isinstance(unknown, DecideDenied)
        assert unknown.reason == "no active HTTP grant covers the origin"

        # A tunnel transports TLS, so a cleartext-origin grant cannot admit one.
        cleartext = decide(_request(method="CONNECT", scheme=None, path=None, host="plain.example", port=80))
        assert isinstance(cleartext, DecideDenied)


def test_standing_allowance_admits_with_provenance_and_no_deadline(make_client: Any) -> None:
    """A standing match allows before any grant is consulted: `standing:<entry id>` provenance,
    no deadline (the policy outlives any admission window; only a redeploy changes it), and the
    same coverage semantics as grants — the path pin sees path plus query exactly as sent."""
    with make_client() as client:
        exact_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="exact.example", port=443)
        harness = _harness(
            client,
            standing=lambda agent_id: [
                _standing_entry(agent_id, path_regex="/api/.*"),
                _standing_entry(
                    agent_id, id="exact-standing", origins=frozenset({exact_origin}), path_regex="/api/items"
                ),
            ],
        )
        decide = partial(client.portal.call, harness.decide.decide)

        allowed = decide(_request(path="/api/items?state=open"))
        assert allowed == DecideAllowed(
            source=DecisionSource.STANDING, decision_id="standing:api-standing", valid_until=None, substitutions=[]
        )

        # Ruled in #4884 for grants and identical here: the regex sees path plus query, so an
        # exact-path pin does not admit the same path with a query string appended.
        assert decide(_request(host="exact.example", path="/api/items")).allowed
        exact_miss = decide(_request(host="exact.example", path="/api/items?state=open"))
        assert isinstance(exact_miss, DecideDenied)

        # No standing match falls through to grants — none exist, so the grant evaluator's clean
        # denial comes back unchanged. The other configured fence identity shares the origin but
        # not the allowance: standing policy is per-Agent.
        for miss in (
            _request(method="POST", path="/api/items"),
            _request(path="/elsewhere"),
            _request(host="other.example", path="/api/items"),
            _request(fence_credential=_OTHER_FENCE, path="/api/items"),
        ):
            decision = decide(miss)
            assert isinstance(decision, DecideDenied), miss.request
            assert decision.reason == "no active HTTP grant covers the request"


def test_standing_wins_over_a_matching_grant(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client, standing=lambda agent_id: [_standing_entry(agent_id)])
        (grant_id,) = _create_grants(
            client,
            harness,
            HttpGrantSpec(
                origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET, HttpMethod.POST}))
            ),
            expires_at=_NOW + timedelta(minutes=30),
        )
        decide = partial(client.portal.call, harness.decide.decide)

        # Both authorities cover GET; standing is evaluated first and provenance says so.
        covered_by_both = decide(_request())
        assert isinstance(covered_by_both, DecideAllowed)
        assert covered_by_both.source is DecisionSource.STANDING
        assert covered_by_both.decision_id == "standing:api-standing"
        assert covered_by_both.valid_until is None

        # Outside standing coverage the grant path is untouched: same verdict it always gave.
        grant_only = decide(_request(method="POST"))
        assert isinstance(grant_only, DecideAllowed)
        assert grant_only.source is DecisionSource.GRANT
        assert grant_only.decision_id == f"grant:{grant_id}"
        assert grant_only.valid_until == _NOW + timedelta(minutes=30)


def test_prohibited_resolved_answer_denies_despite_standing_policy(make_client: Any) -> None:
    # Address validation precedes every authority (#4948): standing policy cannot admit a
    # prohibited resolution any more than a grant can.
    with make_client() as client:
        harness = _harness(client, standing=lambda agent_id: [_standing_entry(agent_id)])
        loopback = IPv4Address("127.0.0.1")

        decision = client.portal.call(
            partial(harness.decide.decide, _request(resolved_ips=frozenset({loopback}), upstream_ip=loopback))
        )

        assert decision == DecideDenied(reason="resolved address 127.0.0.1 is loopback")


def test_standing_allowance_redeems_the_registry_credential(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(
            client,
            credentials=lambda agent_id: [_github_credential(agent_id)],
            standing=lambda agent_id: [_standing_entry(agent_id, credential_handle="github-bot")],
        )

        allowed = client.portal.call(partial(harness.decide.decide, _request(path="/repos/agentydragon/ducktape")))

        assert allowed == DecideAllowed(
            source=DecisionSource.STANDING,
            decision_id="standing:api-standing",
            valid_until=None,
            substitutions=[
                PlaceholderSubstitution(
                    placeholder="github-token-placeholder",
                    value=_GITHUB_VALUE,
                    match_headers=frozenset({"authorization"}),
                )
            ],
        )


def test_overlapping_standing_entries_union_credentials_and_keep_first_provenance(make_client: Any) -> None:
    """Declaration order is the provenance tiebreak; redemption unions every matching entry's
    handle, mirroring how overlapping grants report all their credentials."""
    with make_client() as client:
        harness = _harness(
            client,
            credentials=lambda agent_id: [_github_credential(agent_id)],
            standing=lambda agent_id: [
                _standing_entry(agent_id, id="broad"),
                _standing_entry(agent_id, id="credentialed", credential_handle="github-bot"),
            ],
        )

        allowed = client.portal.call(partial(harness.decide.decide, _request()))

        assert isinstance(allowed, DecideAllowed)
        assert allowed.decision_id == "standing:broad"
        assert [substitution.value for substitution in allowed.substitutions] == [_GITHUB_VALUE]


def test_standing_connect_tunnel_admission(make_client: Any) -> None:
    with make_client() as client:
        cleartext_origin = HttpOrigin(scheme=HttpScheme.HTTP, host="plain.example", port=80)
        harness = _harness(
            client,
            credentials=lambda agent_id: [_github_credential(agent_id)],
            standing=lambda agent_id: [
                # Method and path pins bind each decrypted inner request, not the tunnel itself.
                _standing_entry(agent_id, path_regex="/api/.*", credential_handle="github-bot"),
                _standing_entry(agent_id, id="cleartext", origins=frozenset({cleartext_origin})),
            ],
        )
        decide = partial(client.portal.call, harness.decide.decide)

        allowed = decide(_request(method="CONNECT", scheme=None, path=None))
        assert isinstance(allowed, DecideAllowed)
        assert allowed.source is DecisionSource.STANDING
        assert allowed.decision_id == "standing:api-standing"
        # A tunnel has no inner request yet: nothing to substitute into, even credentialed.
        assert allowed.substitutions == []

        # A tunnel transports TLS, so a cleartext-origin standing entry cannot admit one.
        cleartext = decide(_request(method="CONNECT", scheme=None, path=None, host="plain.example", port=80))
        assert isinstance(cleartext, DecideDenied)

        unknown = decide(_request(method="CONNECT", scheme=None, path=None, host="other.example"))
        assert isinstance(unknown, DecideDenied)


def test_standing_unresolvable_credential_degrades(make_client: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Same #4951 redemption path as grants: a handle the registry does not back for this request
    admits without substitution and warns naming only the handle. Config validation refuses an
    unknown handle at load time, so the not-configured branch is exercised through a directly
    constructed loaded view."""
    locked_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="exact.example", port=443)
    ghost_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="third.example", port=443)
    with make_client() as client:
        harness = _harness(
            client,
            credentials=lambda agent_id: [
                # Assigned to a different Agent than the one whose fence credential decides here.
                _github_credential(_UNGRANTED_AGENT),
                _github_credential(
                    agent_id, handle="origin-locked", placeholder="origin-locked-placeholder"
                ),  # redeemable only at _ORIGIN
            ],
            standing=lambda agent_id: [
                _standing_entry(agent_id, credential_handle="github-bot"),
                _standing_entry(
                    agent_id, id="locked", origins=frozenset({locked_origin}), credential_handle="origin-locked"
                ),
                _standing_entry(agent_id, id="ghost", origins=frozenset({ghost_origin}), credential_handle="ghost"),
            ],
        )
        decide = partial(client.portal.call, harness.decide.decide)

        with caplog.at_level("WARNING"):
            for request, warning in [
                (_request(), "not assigned"),
                (_request(host="exact.example"), "not redeemable"),
                (_request(host="third.example"), "not configured"),
            ]:
                decision = decide(request)
                assert isinstance(decision, DecideAllowed), request.request
                assert decision.source is DecisionSource.STANDING
                assert decision.substitutions == []
                assert warning in caplog.text
        assert _GITHUB_VALUE not in caplog.text


def test_credentialed_grant_redeems_the_substitution(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client, credentials=lambda agent_id: [_github_credential(agent_id)])
        spec = HttpGrantSpec(
            origin=_ORIGIN,
            coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
            credential_handle="github-bot",
        )
        (grant_id,) = _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=30))
        decide = partial(client.portal.call, harness.decide.decide)

        allowed = decide(_request(path="/repos/agentydragon/ducktape"))
        assert allowed == DecideAllowed(
            source=DecisionSource.GRANT,
            decision_id=f"grant:{grant_id}",
            valid_until=_NOW + timedelta(minutes=30),
            substitutions=[
                PlaceholderSubstitution(
                    placeholder="github-token-placeholder",
                    value=_GITHUB_VALUE,
                    match_headers=frozenset({"authorization"}),
                )
            ],
        )

        # Substitutions come from every matching grant, while the expiry bound stays the earliest:
        # an additional pure-reachability grant at the origin narrows valid_until, not redemption.
        (reachability_id,) = _create_grants(
            client,
            harness,
            HttpGrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}))),
            expires_at=_NOW + timedelta(minutes=10),
        )
        combined = decide(_request(path="/repos/agentydragon/ducktape"))
        assert isinstance(combined, DecideAllowed)
        assert combined.decision_id == f"grant:{reachability_id}"
        assert combined.valid_until == _NOW + timedelta(minutes=10)
        assert [substitution.value for substitution in combined.substitutions] == [_GITHUB_VALUE]


def test_connect_tunnel_admission_carries_no_substitutions(make_client: Any) -> None:
    # A tunnel has no inner request to substitute into; each intercepted request is decided
    # individually and redeems there.
    with make_client() as client:
        harness = _harness(client, credentials=lambda agent_id: [_github_credential(agent_id)])
        spec = HttpGrantSpec(
            origin=_ORIGIN,
            coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
            credential_handle="github-bot",
        )
        _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=30))

        allowed = client.portal.call(partial(harness.decide.decide, _request(method="CONNECT", scheme=None, path=None)))

        assert isinstance(allowed, DecideAllowed)
        assert allowed.substitutions == []


def test_unresolvable_credential_admits_without_substitution(
    make_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Credential redemption is an authority separate from reachability: a handle the deploy
    config does not back for this request yields no substitution — the admission stands, the
    inert placeholder passes through — and the mismatch logs a warning naming only the handle."""
    locked_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="exact.example", port=443)
    ghost_origin = HttpOrigin(scheme=HttpScheme.HTTPS, host="third.example", port=443)
    with make_client() as client:
        harness = _harness(
            client,
            credentials=lambda agent_id: [
                # Assigned to a different Agent than the one whose fence credential decides here.
                _github_credential(_UNGRANTED_AGENT),
                _github_credential(
                    agent_id, handle="origin-locked", placeholder="origin-locked-placeholder"
                ),  # redeemable only at _ORIGIN
            ],
        )
        _create_grants(
            client,
            harness,
            HttpGrantSpec(
                origin=_ORIGIN,
                coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                credential_handle="github-bot",
            ),
            HttpGrantSpec(
                origin=locked_origin,
                coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                credential_handle="origin-locked",
            ),
            HttpGrantSpec(
                origin=ghost_origin,
                coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                credential_handle="ghost",
            ),
            expires_at=_NOW + timedelta(minutes=30),
        )
        decide = partial(client.portal.call, harness.decide.decide)

        with caplog.at_level("WARNING"):
            for request, warning in [
                (_request(), "not assigned"),
                (_request(host="exact.example"), "not redeemable"),
                (_request(host="third.example"), "not configured"),
            ]:
                decision = decide(request)
                assert isinstance(decision, DecideAllowed), request.request
                assert decision.substitutions == []
                assert warning in caplog.text
        assert _GITHUB_VALUE not in caplog.text


def test_earliest_expiry_bounds_the_admission(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client)
        spec = HttpGrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))
        (later_id,) = _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=50))
        (earlier_id,) = _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=10))
        assert later_id != earlier_id

        decision = client.portal.call(partial(harness.decide.decide, _request()))

        assert isinstance(decision, DecideAllowed)
        assert decision.decision_id == f"grant:{earlier_id}"
        assert decision.valid_until == _NOW + timedelta(minutes=10)


def test_ungrantable_metadata_denies_with_a_reason(make_client: Any) -> None:
    with make_client() as client:
        harness = _harness(client)
        decide = partial(client.portal.call, harness.decide.decide)
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
            decision = decide(request)
            assert decision == DecideDenied(reason=reason, grant_scope=None), request.request


def test_prohibited_resolved_answer_denies_each_class(make_client: Any) -> None:
    """Every always-prohibited class denies with no grantable scope, despite a covering grant."""
    with make_client() as client:
        harness = _harness(client)
        spec = HttpGrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))
        _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=30))
        decide = partial(client.portal.call, harness.decide.decide)

        assert decide(_request()).allowed  # the grant does admit a public resolution
        prohibited_answers: list[tuple[IPv4Address | IPv6Address, str]] = [
            (IPv4Address("127.0.0.1"), "loopback"),
            (IPv6Address("::1"), "loopback"),
            (IPv4Address("10.0.0.5"), "in a private range"),
            (IPv4Address("172.16.0.9"), "in a private range"),
            (IPv4Address("192.168.1.1"), "in a private range"),
            (IPv4Address("169.254.169.254"), "link-local"),  # the cloud metadata address
            (IPv6Address("fe80::1"), "link-local"),
            (IPv6Address("fd00::1"), "in a private range"),  # ULA
            (IPv4Address("0.0.0.0"), "unspecified"),
            (IPv6Address("::"), "unspecified"),
            (IPv4Address("224.0.0.1"), "multicast"),
            (IPv6Address("ff02::1"), "multicast"),
            (IPv6Address("::ffff:10.0.0.1"), "in a private range"),  # v4-mapped smuggling of RFC1918
        ]
        for prohibited, class_label in prohibited_answers:
            decision = decide(_request(resolved_ips=frozenset({prohibited}), upstream_ip=prohibited))
            assert decision == DecideDenied(reason=f"resolved address {prohibited} is {class_label}"), prohibited


def test_mixed_public_and_prohibited_answer_denies_whole(make_client: Any) -> None:
    """One prohibited member poisons the whole answer even when the pinned address is public.

    A mixed answer is the DNS-rebinding signature (#4948), so it is refused outright rather
    than filtered to its public members.
    """
    with make_client() as client:
        harness = _harness(client)
        spec = HttpGrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))
        _create_grants(client, harness, spec, expires_at=_NOW + timedelta(minutes=30))

        decision = client.portal.call(
            partial(
                harness.decide.decide,
                _request(resolved_ips=frozenset({_PUBLIC_V4, IPv4Address("10.0.0.5")}), upstream_ip=_PUBLIC_V4),
            )
        )

        assert decision == DecideDenied(reason="resolved address 10.0.0.5 is in a private range")


def test_configured_prohibited_cidrs_extend_the_always_on_classes(make_client: Any) -> None:
    """Deploy CIDRs (cluster service/pod ranges) deny like a prohibited class.

    100.64.0.0/10 and NAT64 space are the realistic examples: globally unrouted yet in no
    always-prohibited class, so only the configured CIDR denies them — the baseline service
    without the config admits the same answers.
    """
    with make_client() as client:
        baseline = _harness(client)
        fenced = _harness(
            client, prohibited_cidrs=frozenset({IPv4Network("100.64.0.0/10"), IPv6Network("64:ff9b::/96")})
        )
        spec = HttpGrantSpec(origin=_ORIGIN, coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})))
        _create_grants(client, fenced, spec, expires_at=_NOW + timedelta(minutes=30))
        cgnat = IPv4Address("100.64.9.9")
        nat64 = IPv6Address("64:ff9b::a00:1")

        for address in (cgnat, nat64):
            allowed = client.portal.call(
                partial(baseline.decide.decide, _request(resolved_ips=frozenset({address}), upstream_ip=address))
            )
            assert allowed.allowed, address
        for address, network in ((cgnat, "100.64.0.0/10"), (nat64, "64:ff9b::/96")):
            denied = client.portal.call(
                partial(fenced.decide.decide, _request(resolved_ips=frozenset({address}), upstream_ip=address))
            )
            assert denied == DecideDenied(reason=f"resolved address {address} is in prohibited range {network}")
        # A public answer — mixed-family included — still admits through the fenced service.
        public = client.portal.call(
            partial(
                fenced.decide.decide, _request(resolved_ips=frozenset({_PUBLIC_V4, _PUBLIC_V6}), upstream_ip=_PUBLIC_V4)
            )
        )
        assert public.allowed


async def test_grant_authority_failure_raises_unavailable() -> None:
    # An unreachable database is the authority failure the service must convert into a refusal;
    # nothing here needs the container.
    unreachable = async_sessionmaker(
        create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:9/unreachable"), expire_on_commit=False
    )
    service = HttpDecideService(
        grants=HttpGrantService(
            PostgresHttpGrantRepository(unreachable), max_lifetime=timedelta(hours=1), clock=lambda: _NOW
        ),
        credentials=LoadedEgressDecide(
            proxy_token=SecretStr(_PROXY_TOKEN),
            fence_credentials=[LoadedFenceCredential(agent_id=_UNGRANTED_AGENT, token=SecretStr(_FENCE))],
        ),
        prohibited_cidrs=frozenset(),
    )

    with pytest.raises(HttpDecideUnavailableError):
        await service.decide(_request())


if __name__ == "__main__":
    pytest_bazel.main()
