"""HTTP contract for Operator inspection of HTTP egress grants."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest_bazel
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from haku.console.grants.catalog import (
    ConfigFileGrantSource,
    DatabaseGrantSource,
    Grant as CatalogGrant,
    GrantPrincipalSubject,
    GrantValidity,
    HttpCoverage,
)
from haku.console.grants.envelope import GrantStatus
from haku.console.grants.http.models import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    Grant,
    GrantSpec,
    HttpMethod,
    HttpOrigin,
    HttpRequestCoverage,
    HttpScheme,
)
from haku.console.grants.http.routes import router
from haku.console.grants.principal import AgentGrantPrincipal
from haku.console.identity import operator_auth
from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.enrollment import OperatorAgent
from haku.console.identity.operator_auth import require_operator_mutation_origin
from haku.console.tool_call_actor import OperatorActor

OPERATOR_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("30000000-0000-4000-8000-000000000003")
GRANT_ID = UUID("50000000-0000-4000-8000-000000000005")
# Relative: the serialized status is computed against the live clock. Whole seconds, so the
# expected wire timestamps below serialize without microseconds.
NOW = datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def _agent() -> OperatorAgent:
    return OperatorAgent(
        agent_id=AGENT_ID,
        display_name="Public Coder",
        status=AgentStatus.ACTIVE,
        credential_kind=CredentialKind.STATIC,
        credential_status=CredentialBindingStatus.ACTIVE,
        created_at=NOW - datetime.timedelta(days=2),
        activated_at=NOW - datetime.timedelta(days=2),
        last_seen_at=NOW - datetime.timedelta(minutes=1),
        access_profile_id="public-coder",
    )


def _wire(instant: datetime.datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _grant() -> Grant:
    return Grant(
        grant_id=GRANT_ID,
        owner_agent_id=AGENT_ID,
        principal=AgentGrantPrincipal(agent_id=AGENT_ID),
        source_tool_call_id="tc_0123456789abcdef01234567",
        spec=GrantSpec(
            origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443),
            coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}), path_regex="/api/.*"),
        ),
        created_at=NOW - datetime.timedelta(minutes=5),
        expires_at=NOW + datetime.timedelta(hours=2),
    )


@dataclass
class _FakeAgentService:
    listed_operator_ids: list[UUID] = field(default_factory=list)

    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]:
        self.listed_operator_ids.append(operator_id)
        return (_agent(),)


@dataclass
class _FakeGrantService:
    current: Grant = field(default_factory=_grant)
    listed: list[tuple[UUID, bool]] = field(default_factory=list)

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[Grant, ...]:
        self.listed.append((owner_agent_id, include_terminal))
        return (self.current,) if owner_agent_id == AGENT_ID else ()


@dataclass
class _FakeCatalog:
    grants: _FakeGrantService
    listed: list[tuple[UUID, str | None]] = field(default_factory=list)

    async def list_http_for_agent(self, *, agent_id: UUID, access_profile_id: str | None) -> tuple[CatalogGrant, ...]:
        self.listed.append((agent_id, access_profile_id))
        if agent_id != AGENT_ID:
            return ()
        return (self._config_grant(), self.describe_http_grant(self.grants.current))

    @staticmethod
    def _config_grant() -> CatalogGrant:
        return CatalogGrant(
            source=ConfigFileGrantSource(entry_id="grocy-config"),
            subject=GrantPrincipalSubject(principal=AgentGrantPrincipal(agent_id=AGENT_ID)),
            coverage=HttpCoverage(
                origins=frozenset({HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443)}),
                coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET}), path_regex="/api/.*"),
                credential_handles=frozenset(),
                allow_prohibited_address=False,
            ),
            validity=GrantValidity(ends_at=None, status=GrantStatus.ACTIVE),
        )

    @staticmethod
    def describe_http_grant(grant: Grant) -> CatalogGrant:
        return CatalogGrant(
            source=DatabaseGrantSource(
                id=grant.grant_id, tool_call_id=grant.source_tool_call_id, created_at=grant.created_at
            ),
            subject=GrantPrincipalSubject(principal=grant.principal),
            coverage=HttpCoverage(
                origins=frozenset({grant.spec.origin}),
                coverage=grant.spec.coverage,
                credential_handles=frozenset(),
                allow_prohibited_address=grant.spec.allow_prohibited_address,
            ),
            validity=GrantValidity(
                ends_at=grant.expires_at,
                status=grant.status,
                ended_at=grant.released_at or grant.revoked_at,
                end_reason=grant.end_reason,
            ),
        )


def _client() -> tuple[TestClient, _FakeAgentService, _FakeGrantService, _FakeCatalog]:
    app = FastAPI()
    agents = _FakeAgentService()
    grants = _FakeGrantService()
    app.state.agent_enrollment_service = agents
    app.state.http_grants = grants
    app.state.grant_catalog = _FakeCatalog(grants)
    app.state.settings = SimpleNamespace(public_base_url="https://haku.test")
    app.include_router(router, dependencies=[Depends(require_operator_mutation_origin)])
    app.dependency_overrides[operator_auth._operator_actor] = lambda: OperatorActor(operator_id=OPERATOR_ID)
    return TestClient(app, base_url="https://haku.test"), agents, grants, app.state.grant_catalog


def test_lists_only_the_authenticated_operators_agents_with_provenance() -> None:
    client, agents, grants, catalog = _client()

    response = client.get("/api/http-grants")

    assert response.status_code == 200
    assert agents.listed_operator_ids == [OPERATOR_ID]
    assert grants.listed == []
    assert catalog.listed == [(AGENT_ID, "public-coder")]
    record, config_record = response.json()["grants"]
    assert record["agent_id"] == str(AGENT_ID)
    assert record["agent_display_name"] == "Public Coder"
    assert record["grant"]["source"] == {
        "kind": "database",
        "id": str(GRANT_ID),
        "tool_call_id": "tc_0123456789abcdef01234567",
        "created_at": _wire(NOW - datetime.timedelta(minutes=5)),
    }
    assert record["grant"]["coverage"]["kind"] == "http"
    assert record["grant"]["validity"] == {
        "ends_at": _wire(NOW + datetime.timedelta(hours=2)),
        "status": "active",
        "ended_at": None,
        "end_reason": None,
    }
    assert config_record["grant"]["source"] == {"kind": "config_file", "entry_id": "grocy-config"}
    assert config_record["grant"]["validity"] == {
        "ends_at": None,
        "status": "active",
        "ended_at": None,
        "end_reason": None,
    }


if __name__ == "__main__":
    pytest_bazel.main()
