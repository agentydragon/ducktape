"""HTTP contract for Operator inspection of HTTP egress grants."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest_bazel
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from haku.console import operator_auth
from haku.console.agents.enrollment import OperatorAgent
from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.grants.http.models import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    HttpGrant,
    HttpGrantSpec,
    HttpMethod,
    HttpOrigin,
    HttpRequestCoverage,
    HttpScheme,
)
from haku.console.grants.http.routes import router
from haku.console.grants.principal import AgentGrantPrincipal
from haku.console.operator_auth import require_operator_mutation_origin
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


def _grant() -> HttpGrant:
    return HttpGrant(
        grant_id=GRANT_ID,
        owner_agent_id=AGENT_ID,
        principal=AgentGrantPrincipal(agent_id=AGENT_ID),
        source_tool_call_id="tc_0123456789abcdef01234567",
        spec=HttpGrantSpec(
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
    current: HttpGrant = field(default_factory=_grant)
    listed: list[tuple[UUID, bool]] = field(default_factory=list)

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[HttpGrant, ...]:
        self.listed.append((owner_agent_id, include_terminal))
        return (self.current,) if owner_agent_id == AGENT_ID else ()


def _client() -> tuple[TestClient, _FakeAgentService, _FakeGrantService]:
    app = FastAPI()
    agents = _FakeAgentService()
    grants = _FakeGrantService()
    app.state.agent_enrollment_service = agents
    app.state.http_grants = grants
    app.state.settings = SimpleNamespace(public_base_url="https://haku.test")
    app.include_router(router, dependencies=[Depends(require_operator_mutation_origin)])
    app.dependency_overrides[operator_auth._operator_actor] = lambda: OperatorActor(operator_id=OPERATOR_ID)
    return TestClient(app, base_url="https://haku.test"), agents, grants


def test_lists_only_the_authenticated_operators_agents_with_provenance() -> None:
    client, agents, grants = _client()

    response = client.get("/api/http-grants")

    assert response.status_code == 200
    assert agents.listed_operator_ids == [OPERATOR_ID]
    assert grants.listed == [(AGENT_ID, True)]
    assert response.json() == {
        "grants": [
            {
                "agent_display_name": "Public Coder",
                "grant": {
                    "grant_id": str(GRANT_ID),
                    "owner_agent_id": str(AGENT_ID),
                    "principal": {"kind": "agent", "agent_id": str(AGENT_ID)},
                    "source_tool_call_id": "tc_0123456789abcdef01234567",
                    "spec": {
                        "origin": {"scheme": "https", "host": "grocy.example", "port": 443},
                        "coverage": {"methods": ["GET"], "path_regex": "/api/.*"},
                        "credential_handle": None,
                    },
                    "status": "active",
                    "created_at": _wire(NOW - datetime.timedelta(minutes=5)),
                    "expires_at": _wire(NOW + datetime.timedelta(hours=2)),
                    "released_at": None,
                    "revoked_at": None,
                    "end_reason": None,
                },
            }
        ]
    }


if __name__ == "__main__":
    pytest_bazel.main()
