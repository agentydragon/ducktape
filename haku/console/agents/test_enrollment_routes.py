"""HTTP contract for the Operator-authenticated Agent enrollment ceremony."""

from __future__ import annotations

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
    ReconnectableAgent,
    ReconnectAgentDecision,
)
from haku.console.agents.enrollment_routes import _operator_session, router
from haku.console.operator_auth import OperatorSession

INTERACTION_ID = UUID("10000000-0000-4000-8000-000000000001")
OPERATOR_ID = UUID("20000000-0000-4000-8000-000000000002")
IDENTITY_ID = UUID("30000000-0000-4000-8000-000000000003")
AGENT_ID = UUID("40000000-0000-4000-8000-000000000004")
BASE_PATH = f"/auth/agent-enrollment/{INTERACTION_ID}"
FORM_TOKEN = "form-token-0123456789abcdef0123456789"


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


@dataclass
class _FakeEnrollmentService:
    page: EnrollmentPage = field(default_factory=_page)
    result: EnrollmentDecisionResult = field(
        default_factory=lambda: EnrollmentAllowed("https://auth.example.test/authorize?opaque=1")
    )
    decision_error: Exception | None = None
    opens: list[dict[str, object]] = field(default_factory=list)
    decisions: list[dict[str, object]] = field(default_factory=list)

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
    app.include_router(router)

    def operator_session_override() -> OperatorSession | None:
        return _session() if authenticated else None

    app.dependency_overrides[_operator_session] = operator_session_override
    return TestClient(app, base_url="https://haku.test"), fake


def _open(client: TestClient) -> None:
    response = client.get(f"{BASE_PATH}?browser_nonce=browser-secret")
    assert response.status_code == 200


def test_unauthenticated_get_continues_only_through_the_exact_local_interaction() -> None:
    client, service = _client(authenticated=False)
    response = client.get(f"{BASE_PATH}?browser_nonce=browser-secret", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/login?return_to=%2Fauth%2Fagent-enrollment%2F10000000-0000-4000-8000-000000000001"
        "%3Fbrowser_nonce%3Dbrowser-secret"
    )
    assert service.opens == []


def test_get_binds_the_browser_and_sets_a_path_scoped_http_only_cookie() -> None:
    client, service = _client()
    response = client.get(f"{BASE_PATH}?browser_nonce=browser-secret")

    assert response.status_code == 200
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
    assert f"Path={BASE_PATH}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie
    assert f'action="{BASE_PATH}/new"' in response.text
    assert f'action="{BASE_PATH}/reconnect"' in response.text
    assert f'action="{BASE_PATH}/deny"' in response.text
    assert "form-action 'self' https://auth.example.test" in response.headers["content-security-policy"]


def test_create_reconnect_and_deny_are_separate_typed_endpoints() -> None:
    client, service = _client()
    _open(client)

    create = client.post(
        f"{BASE_PATH}/new",
        data={"form_token": FORM_TOKEN, "agent_name": "Kitchen Claude"},
        headers={"Origin": "https://haku.test"},
        follow_redirects=False,
    )
    assert create.status_code == 200
    assert "Agent approved" in create.text
    assert 'href="https://auth.example.test/authorize?opaque=1"' in create.text
    assert 'haku_agent_enrollment=""' in create.headers["set-cookie"]
    assert service.decisions[0]["interaction_cookie"] == FORM_TOKEN
    assert service.decisions[0]["decision"] == CreateAgentDecision(form_token=FORM_TOKEN, display_name="Kitchen Claude")

    # A terminal response clears the path-scoped cookie; reopen this fake interaction for each
    # endpoint so the test exercises the browser boundary just like a distinct interaction.
    _open(client)
    reconnect = client.post(
        f"{BASE_PATH}/reconnect",
        data={"form_token": FORM_TOKEN, "agent_id": str(AGENT_ID)},
        headers={"Origin": "https://haku.test"},
        follow_redirects=False,
    )
    assert reconnect.status_code == 200
    assert "Agent approved" in reconnect.text
    assert service.decisions[1]["decision"] == ReconnectAgentDecision(form_token=FORM_TOKEN, agent_id=AGENT_ID)

    _open(client)
    service.result = EnrollmentDenied()
    denied = client.post(f"{BASE_PATH}/deny", data={"form_token": FORM_TOKEN}, headers={"Origin": "https://haku.test"})
    assert denied.status_code == 200
    assert service.decisions[2]["decision"] == DenyEnrollmentDecision(form_token=FORM_TOKEN)


def test_post_rejects_invalid_origin_or_missing_browser_binding_before_deciding() -> None:
    client, service = _client()
    _open(client)

    for origin in (None, "null", "https://evil.test"):
        headers = {} if origin is None else {"Origin": origin}
        rejected = client.post(
            f"{BASE_PATH}/new", data={"form_token": FORM_TOKEN, "agent_name": "Claude"}, headers=headers
        )
        assert rejected.status_code == 403
        assert rejected.json() == {"detail": "invalid Agent enrollment origin"}
        assert service.decisions == []

    client.cookies.clear()
    missing_cookie = client.post(
        f"{BASE_PATH}/new",
        data={"form_token": FORM_TOKEN, "agent_name": "Claude"},
        headers={"Origin": "https://haku.test"},
    )
    assert missing_cookie.status_code == 403
    assert service.decisions == []


def test_correctable_name_conflict_rerenders_the_same_bound_interaction() -> None:
    service = _FakeEnrollmentService(decision_error=AgentNameUnavailableError("Agent name is already reserved."))
    client, service = _client(service=service)
    _open(client)

    response = client.post(
        f"{BASE_PATH}/new",
        data={"form_token": FORM_TOKEN, "agent_name": "Kitchen Claude"},
        headers={"Origin": "https://haku.test"},
    )

    assert response.status_code == 409
    assert "Agent name is already reserved." in response.text
    assert service.opens[-1]["browser_nonce"] is None
    assert service.opens[-1]["interaction_cookie"] == FORM_TOKEN
    assert f"haku_agent_enrollment={FORM_TOKEN}" in response.headers["set-cookie"]


if __name__ == "__main__":
    pytest_bazel.main()
