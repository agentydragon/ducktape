"""HTTP contract for Operator inspection and revocation of Kubernetes grants."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest_bazel
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from haku.console.grants.envelope import GrantNotFoundError
from haku.console.grants.kubernetes.models import Grant, NamespacesGrantScope, Rule
from haku.console.grants.kubernetes.routes import router
from haku.console.grants.principal import AgentGrantPrincipal
from haku.console.identity import operator_auth
from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.enrollment import OperatorAgent
from haku.console.identity.operator_auth import require_operator_mutation_origin
from haku.console.tool_call_actor import OperatorActor

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

OPERATOR_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("30000000-0000-4000-8000-000000000003")
OTHER_AGENT_ID = UUID("40000000-0000-4000-8000-000000000004")
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


def _grant(*, revoked: bool = False) -> Grant:
    return Grant(
        grant_id=GRANT_ID,
        owner_agent_id=AGENT_ID,
        principal=AgentGrantPrincipal(agent_id=AGENT_ID),
        source_tool_call_id="tc_0123456789abcdef01234567",
        scope=NamespacesGrantScope(namespaces={"public-coder-agent"}),
        rules=(Rule(api_groups={""}, resources={"pods/log"}, verbs={"get"}),),
        created_at=NOW - datetime.timedelta(minutes=5),
        expires_at=NOW + datetime.timedelta(minutes=25),
        revoked_at=NOW if revoked else None,
        end_reason="operator reason" if revoked else None,
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
    revoked: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    revoked_sets: list[tuple[UUID, str, str]] = field(default_factory=list)

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[Grant, ...]:
        self.listed.append((owner_agent_id, include_terminal))
        return (self.current,) if owner_agent_id == AGENT_ID else ()

    async def revoke_grant(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str) -> Grant:
        self.revoked.append((owner_agent_id, grant_id, reason))
        if owner_agent_id != AGENT_ID or grant_id != GRANT_ID:
            raise GrantNotFoundError(str(grant_id))
        self.current = _grant(revoked=True)
        return self.current

    async def revoke_grant_set(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str
    ) -> tuple[Grant, ...]:
        if owner_agent_id != AGENT_ID or source_tool_call_id != self.current.source_tool_call_id:
            raise GrantNotFoundError(source_tool_call_id)
        self.revoked_sets.append((owner_agent_id, source_tool_call_id, reason))
        self.current = _grant(revoked=True)
        return (self.current,)


def _client() -> tuple[TestClient, _FakeAgentService, _FakeGrantService]:
    app = FastAPI()
    agents = _FakeAgentService()
    grants = _FakeGrantService()
    app.state.agent_enrollment_service = agents
    app.state.kubernetes_grants = grants
    app.state.settings = SimpleNamespace(public_base_url="https://haku.test")
    app.include_router(router, dependencies=[Depends(require_operator_mutation_origin)])
    app.dependency_overrides[operator_auth._operator_actor] = lambda: OperatorActor(operator_id=OPERATOR_ID)
    return TestClient(app, base_url="https://haku.test"), agents, grants


def test_lists_only_the_authenticated_operators_agents_with_provenance() -> None:
    client, agents, grants = _client()

    response = client.get("/api/kubernetes-grants")

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
                    "scope": {"kind": "namespaces", "namespaces": ["public-coder-agent"]},
                    "rules": [
                        {
                            "api_groups": [""],
                            "resources": ["pods/log"],
                            "verbs": ["get"],
                            "resource_names": [],
                            "non_resource_urls": [],
                        }
                    ],
                    "status": "active",
                    "created_at": _wire(NOW - datetime.timedelta(minutes=5)),
                    "expires_at": _wire(NOW + datetime.timedelta(minutes=25)),
                    "released_at": None,
                    "revoked_at": None,
                    "end_reason": None,
                },
            }
        ]
    }


def test_revoke_requires_a_non_blank_reason_and_owned_agent() -> None:
    client, _agents, grants = _client()
    path = f"/api/kubernetes-grants/{AGENT_ID}/{GRANT_ID}/revoke"

    assert client.post(path, json={"reason": "risk"}).status_code == 403
    headers = {"Origin": "https://haku.test"}
    assert client.post(path, json={"reason": "   "}, headers=headers).status_code == 422
    assert (
        client.post(
            f"/api/kubernetes-grants/{OTHER_AGENT_ID}/{GRANT_ID}/revoke", json={"reason": "risk"}, headers=headers
        ).status_code
        == 404
    )

    response = client.post(path, json={"reason": "  pilot complete  "}, headers=headers)

    assert response.status_code == 200
    assert grants.revoked == [(AGENT_ID, GRANT_ID, "pilot complete")]
    assert response.json()["grant"]["status"] == "revoked"


def test_revoke_source_set_requires_reason_and_owned_agent() -> None:
    client, _agents, grants = _client()
    source = "tc_0123456789abcdef01234567"
    path = f"/api/kubernetes-grants/{AGENT_ID}/source/{source}/revoke"
    headers = {"Origin": "https://haku.test"}

    assert client.post(path, json={"reason": "risk"}).status_code == 403
    assert client.post(path, json={"reason": "   "}, headers=headers).status_code == 422
    assert (
        client.post(
            f"/api/kubernetes-grants/{OTHER_AGENT_ID}/source/{source}/revoke", json={"reason": "risk"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/kubernetes-grants/{AGENT_ID}/source/tc_unknown/revoke", json={"reason": "risk"}, headers=headers
        ).status_code
        == 404
    )

    response = client.post(path, json={"reason": "  pilot complete  "}, headers=headers)

    assert response.status_code == 200
    assert grants.revoked_sets == [(AGENT_ID, source, "pilot complete")]
    assert [item["grant"]["status"] for item in response.json()["grants"]] == ["revoked"]


if __name__ == "__main__":
    pytest_bazel.main()
