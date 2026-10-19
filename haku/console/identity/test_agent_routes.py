"""HTTP contract for Operator-authenticated Agent management APIs."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest_bazel
from fastapi import Depends, FastAPI

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx
from fastapi.testclient import TestClient

from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.agent_routes import router
from haku.console.identity.enrollment import OperatorAgent
from haku.console.identity.operator_auth import OperatorSession, operator_session, require_operator_mutation_origin

OPERATOR_ID = UUID("20000000-0000-4000-8000-000000000002")
IDENTITY_ID = UUID("30000000-0000-4000-8000-000000000003")
AGENT_ID = UUID("40000000-0000-4000-8000-000000000004")
NOW = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=datetime.UTC)


def _session() -> OperatorSession:
    return OperatorSession(
        operator_id=OPERATOR_ID,
        identity_id=IDENTITY_ID,
        username="Rai",
        browser_session_id="browser-session",
        expires_at=NOW + datetime.timedelta(hours=1),
    )


def _agent() -> OperatorAgent:
    return OperatorAgent(
        agent_id=AGENT_ID,
        display_name="Kitchen Claude",
        status=AgentStatus.ACTIVE,
        credential_kind=CredentialKind.OAUTH,
        credential_status=CredentialBindingStatus.ACTIVE,
        created_at=NOW - datetime.timedelta(days=2),
        activated_at=NOW - datetime.timedelta(days=2),
        last_seen_at=NOW - datetime.timedelta(minutes=4),
        access_profile_id="haku_v1",
    )


@dataclass
class _FakeAgentService:
    agents: tuple[OperatorAgent, ...] = field(default_factory=lambda: (_agent(),))
    listed_operator_ids: list[UUID] = field(default_factory=list)
    access_profile_updates: list[tuple[UUID, UUID, str]] = field(default_factory=list)

    def available_access_profiles(self) -> tuple[str, ...]:
        return ("no_auto_approval", "haku_v1")

    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]:
        self.listed_operator_ids.append(operator_id)
        return self.agents

    async def set_access_profile(self, *, operator_id: UUID, agent_id: UUID, access_profile_id: str) -> OperatorAgent:
        self.access_profile_updates.append((operator_id, agent_id, access_profile_id))
        return self.agents[0]


def _client(*, authenticated: bool = True) -> tuple[TestClient, _FakeAgentService]:
    app = FastAPI()
    fake = _FakeAgentService()
    app.state.agent_enrollment_service = fake
    app.state.settings = SimpleNamespace(public_base_url="https://haku.test")
    app.include_router(router, dependencies=[Depends(require_operator_mutation_origin)])

    def operator_session_override() -> OperatorSession | None:
        return _session() if authenticated else None

    app.dependency_overrides[operator_session] = operator_session_override
    return TestClient(app, base_url="https://haku.test"), fake


def test_settings_lists_operator_agents_without_claiming_live_connection_state() -> None:
    client, service = _client()
    response = client.get("/api/agent-enrollment/agents")

    assert response.status_code == 200
    assert service.listed_operator_ids == [OPERATOR_ID]
    assert response.json() == {
        "agents": [
            {
                "agent_id": str(AGENT_ID),
                "display_name": "Kitchen Claude",
                "status": "active",
                "credential_kind": "oauth",
                "credential_status": "active",
                "created_at": "2026-07-18T12:00:00Z",
                "activated_at": "2026-07-18T12:00:00Z",
                "last_seen_at": "2026-07-20T11:56:00Z",
                "access_profile_id": "haku_v1",
            }
        ],
        "access_profiles": ["no_auto_approval", "haku_v1"],
    }


def test_agent_list_requires_an_operator_session() -> None:
    client, service = _client(authenticated=False)
    response = client.get("/api/agent-enrollment/agents")

    assert response.status_code == 401
    assert service.listed_operator_ids == []


def test_settings_updates_an_agents_access_profile() -> None:
    client, service = _client()
    response = client.put(
        f"/api/agent-enrollment/agents/{AGENT_ID}/access-profile",
        json={"access_profile_id": "haku_v1"},
        headers={"Origin": "https://haku.test"},
    )

    assert response.status_code == 200
    assert service.access_profile_updates == [(OPERATOR_ID, AGENT_ID, "haku_v1")]
    assert response.json()["access_profile_id"] == "haku_v1"


def test_settings_access_profile_update_requires_an_explicit_assignment() -> None:
    client, service = _client()
    response = client.put(
        f"/api/agent-enrollment/agents/{AGENT_ID}/access-profile", json={}, headers={"Origin": "https://haku.test"}
    )

    assert response.status_code == 422
    assert service.access_profile_updates == []


if __name__ == "__main__":
    pytest_bazel.main()
