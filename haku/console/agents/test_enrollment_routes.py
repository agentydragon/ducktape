"""HTTP contract for the Operator-authenticated Agent enrollment ceremony."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from haku.console.agents.enrollment import (
    AgentNameUnavailableError,
    CreateAgentDecision,
    DenyEnrollmentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserSession,
    EnrollmentDecision,
    EnrollmentDecisionResult,
    EnrollmentDenied,
    EnrollmentPage,
    OperatorAgent,
    ReconnectableAgent,
    ReconnectAgentDecision,
)
from haku.console.agents.enrollment_routes import _operator_session, entry_router, operator_router
from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.operator_auth import OperatorSession

INTERACTION_ID = UUID("10000000-0000-4000-8000-000000000001")
OPERATOR_ID = UUID("20000000-0000-4000-8000-000000000002")
IDENTITY_ID = UUID("30000000-0000-4000-8000-000000000003")
AGENT_ID = UUID("40000000-0000-4000-8000-000000000004")
ENTRY_PATH = f"/auth/agent-enrollment/{INTERACTION_ID}"
API_PATH = f"/api/agent-enrollment/{INTERACTION_ID}"
SPA_PATH = f"/_console/settings/agents/enroll/{INTERACTION_ID}"
FORM_TOKEN = "form-token-0123456789abcdef0123456789"
NOW = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=datetime.UTC)


def _session() -> OperatorSession:
    return OperatorSession(
        operator_id=OPERATOR_ID, identity_id=IDENTITY_ID, username="Rai", browser_session_id="browser-session"
    )


def _page() -> EnrollmentPage:
    return EnrollmentPage(
        client_software="Claude.ai",
        redirect_host="claude.ai",
        requested_scopes=("openid", "offline_access"),
        suggested_agent_name="Claude",
        reconnectable_agents=(ReconnectableAgent(agent_id=AGENT_ID, display_name="Kitchen Claude"),),
        form_token=FORM_TOKEN,
        upstream_authorization_url="https://auth.example.test/authorize?opaque=1",
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
    )


@dataclass
class _FakeEnrollmentService:
    page: EnrollmentPage = field(default_factory=_page)
    agents: tuple[OperatorAgent, ...] = field(default_factory=lambda: (_agent(),))
    result: EnrollmentDecisionResult = field(
        default_factory=lambda: EnrollmentAllowed("https://auth.example.test/authorize?opaque=1")
    )
    decision_error: Exception | None = None
    listed_operator_ids: list[UUID] = field(default_factory=list)
    opens: list[dict[str, object]] = field(default_factory=list)
    decisions: list[dict[str, object]] = field(default_factory=list)

    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]:
        self.listed_operator_ids.append(operator_id)
        return self.agents

    async def open_interaction(
        self,
        *,
        interaction_id: UUID,
        browser_nonce: str | None,
        interaction_cookie: str | None,
        browser: EnrollmentBrowserSession,
    ) -> EnrollmentPage:
        self.opens.append(
            {
                "interaction_id": interaction_id,
                "browser_nonce": browser_nonce,
                "interaction_cookie": interaction_cookie,
                "browser": browser,
            }
        )
        return self.page

    async def decide(
        self,
        *,
        interaction_id: UUID,
        browser: EnrollmentBrowserSession,
        interaction_cookie: str,
        decision: EnrollmentDecision,
    ) -> EnrollmentDecisionResult:
        self.decisions.append(
            {
                "interaction_id": interaction_id,
                "browser": browser,
                "interaction_cookie": interaction_cookie,
                "decision": decision,
            }
        )
        if self.decision_error is not None:
            raise self.decision_error
        return self.result


def _client(
    *, authenticated: bool = True, service: _FakeEnrollmentService | None = None
) -> tuple[TestClient, _FakeEnrollmentService]:
    app = FastAPI()
    fake = service or _FakeEnrollmentService()
    app.state.agent_enrollment_service = fake
    app.state.settings = SimpleNamespace(public_base_url="https://haku.test")
    app.include_router(operator_router)
    app.include_router(entry_router)

    def operator_session_override() -> OperatorSession | None:
        return _session() if authenticated else None

    app.dependency_overrides[_operator_session] = operator_session_override
    return TestClient(app, base_url="https://haku.test"), fake


def _enter(client: TestClient) -> None:
    response = client.get(f"{ENTRY_PATH}?browser_nonce=browser-secret", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == SPA_PATH


def test_unauthenticated_entry_continues_only_through_the_exact_local_interaction() -> None:
    client, service = _client(authenticated=False)
    response = client.get(f"{ENTRY_PATH}?browser_nonce=browser-secret", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/login?return_to=%2Fauth%2Fagent-enrollment%2F10000000-0000-4000-8000-000000000001"
        "%3Fbrowser_nonce%3Dbrowser-secret"
    )
    assert service.opens == []


def test_entry_binds_browser_then_redirects_to_the_settings_enrollment_route() -> None:
    client, service = _client()
    response = client.get(f"{ENTRY_PATH}?browser_nonce=browser-secret", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == SPA_PATH
    assert service.opens == [
        {
            "interaction_id": INTERACTION_ID,
            "browser_nonce": "browser-secret",
            "interaction_cookie": None,
            "browser": EnrollmentBrowserSession(
                operator_id=OPERATOR_ID,
                identity_id=IDENTITY_ID,
                browser_session_id="browser-session",
                display_name="Rai",
            ),
        }
    ]
    cookie = response.headers["set-cookie"]
    assert f"haku_agent_enrollment={FORM_TOKEN}" in cookie
    assert f"Path={API_PATH}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie


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
            }
        ]
    }


def test_bound_settings_route_returns_the_typed_enrollment_view() -> None:
    client, service = _client()
    _enter(client)

    response = client.get(API_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "operator_display_name": "Rai",
        "client_software": "Claude.ai",
        "redirect_host": "claude.ai",
        "requested_scopes": ["openid", "offline_access"],
        "suggested_agent_name": "Claude",
        "reconnectable_agents": [{"agent_id": str(AGENT_ID), "display_name": "Kitchen Claude"}],
        "form_token": FORM_TOKEN,
    }
    assert service.opens[-1]["browser_nonce"] is None
    assert service.opens[-1]["interaction_cookie"] == FORM_TOKEN


def test_create_reconnect_and_deny_use_one_discriminated_json_endpoint() -> None:
    client, service = _client()
    _enter(client)

    create = client.post(
        f"{API_PATH}/decision",
        json={"kind": "create", "form_token": FORM_TOKEN, "display_name": "Kitchen Claude"},
        headers={"Origin": "https://haku.test"},
    )
    assert create.status_code == 200
    assert create.json() == {"status": "continue", "authorization_url": "https://auth.example.test/authorize?opaque=1"}
    assert service.decisions[0]["decision"] == CreateAgentDecision(form_token=FORM_TOKEN, display_name="Kitchen Claude")
    assert 'haku_agent_enrollment=""' in create.headers["set-cookie"]

    _enter(client)
    reconnect = client.post(
        f"{API_PATH}/decision",
        json={"kind": "reconnect", "form_token": FORM_TOKEN, "agent_id": str(AGENT_ID)},
        headers={"Origin": "https://haku.test"},
    )
    assert reconnect.status_code == 200
    assert service.decisions[1]["decision"] == ReconnectAgentDecision(form_token=FORM_TOKEN, agent_id=AGENT_ID)

    _enter(client)
    service.result = EnrollmentDenied()
    denied = client.post(
        f"{API_PATH}/decision", json={"kind": "deny", "form_token": FORM_TOKEN}, headers={"Origin": "https://haku.test"}
    )
    assert denied.status_code == 200
    assert denied.json() == {"status": "denied"}
    assert service.decisions[2]["decision"] == DenyEnrollmentDecision(form_token=FORM_TOKEN)


def test_decision_rejects_invalid_origin_or_missing_browser_binding() -> None:
    client, service = _client()
    _enter(client)
    body = {"kind": "create", "form_token": FORM_TOKEN, "display_name": "Claude"}

    for origin in (None, "null", "https://evil.test"):
        headers = {} if origin is None else {"Origin": origin}
        rejected = client.post(f"{API_PATH}/decision", json=body, headers=headers)
        assert rejected.status_code == 403
        assert rejected.json() == {"detail": "invalid Agent enrollment origin"}
        assert service.decisions == []

    client.cookies.clear()
    missing_cookie = client.post(f"{API_PATH}/decision", json=body, headers={"Origin": "https://haku.test"})
    assert missing_cookie.status_code == 403
    assert missing_cookie.json() == {"detail": "missing Agent enrollment browser binding"}
    assert service.decisions == []


def test_correctable_name_conflict_is_json_and_leaves_the_interaction_bound() -> None:
    service = _FakeEnrollmentService(decision_error=AgentNameUnavailableError("Agent name is already reserved."))
    client, service = _client(service=service)
    _enter(client)

    response = client.post(
        f"{API_PATH}/decision",
        json={"kind": "create", "form_token": FORM_TOKEN, "display_name": "Kitchen Claude"},
        headers={"Origin": "https://haku.test"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Agent name is already reserved."}
    assert client.cookies.get("haku_agent_enrollment", path=API_PATH) == FORM_TOKEN


if __name__ == "__main__":
    pytest_bazel.main()
