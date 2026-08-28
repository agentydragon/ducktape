"""HTTP contract of the internal egress decide endpoint: auth, verdict wire shape, fail-closed."""

from __future__ import annotations

import datetime
from typing import Any

import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from haku.console.grants.http.decide_routes import router
from haku.console.grants.http.decide_service import HttpDecideUnavailableError
from haku.egress.decision import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    DecideAllowed,
    DecideDenied,
    DecideRequest,
    DecisionSource,
    GrantScope,
    PlaceholderSubstitution,
)

_VALID_UNTIL = datetime.datetime(2026, 8, 27, 12, 30, tzinfo=datetime.UTC)
_PROXY_TOKEN = "proxy-identity-token"
_FENCE = "agent-fence-credential"

_BODY = {
    "fence_credential": _FENCE,
    "request": {"method": "GET", "scheme": "https", "host": "api.example", "port": 443, "path": "/api/items?x=1"},
    "resolved_ips": ["192.0.2.10", "2001:db8::10"],
    "upstream_ip": "192.0.2.10",
}


class _FakeDecideService:
    """Recording double for the route's two service calls (the real evaluation has its own tests)."""

    def __init__(self, decision: DecideAllowed | DecideDenied | Exception) -> None:
        self.decision = decision
        self.authenticated: list[str] = []
        self.decided: list[DecideRequest] = []

    def authenticate_proxy(self, authorization: str) -> bool:
        self.authenticated.append(authorization)
        return authorization == f"Bearer {_PROXY_TOKEN}"

    async def decide(self, request: DecideRequest) -> DecideAllowed | DecideDenied:
        self.decided.append(request)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def _client(service: _FakeDecideService | None = None) -> TestClient:
    app = FastAPI()
    if service is not None:
        app.state.http_decide = service
    app.include_router(router)
    return TestClient(app)


def _post(client: TestClient, body: dict[str, Any] | None = None, token: str | None = _PROXY_TOKEN) -> Any:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post("/api/internal/http/decide", json=body if body is not None else _BODY, headers=headers)


def test_endpoint_requires_bearer() -> None:
    service = _FakeDecideService(DecideDenied(reason="unreached"))
    with _client(service) as client:
        response = _post(client, token=None)
    assert response.status_code == 401
    assert service.decided == []


def test_endpoint_rejects_a_wrong_bearer() -> None:
    service = _FakeDecideService(DecideDenied(reason="unreached"))
    with _client(service) as client:
        response = _post(client, token="not-the-proxy-token")
    assert response.status_code == 401
    assert service.decided == []


def test_endpoint_is_unavailable_when_not_wired() -> None:
    with _client() as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "HTTP egress decision is not configured"


def test_allow_wire_shape_carries_provenance_lifetime_and_substitutions() -> None:
    service = _FakeDecideService(
        DecideAllowed(
            source=DecisionSource.GRANT,
            decision_id="grant:50000000-0000-4000-8000-000000000005",
            valid_until=_VALID_UNTIL,
            substitutions=[
                PlaceholderSubstitution(
                    placeholder="github-token-placeholder",
                    value="ghp-real-value",
                    match_headers=frozenset({"authorization"}),
                )
            ],
        )
    )
    with _client(service) as client:
        response = _post(client)
    assert response.status_code == 200
    # This response is the one wire a real credential value travels on: localhost, to the proxy.
    assert response.json() == {
        "allowed": True,
        "source": "grant",
        "decision_id": "grant:50000000-0000-4000-8000-000000000005",
        "valid_until": "2026-08-27T12:30:00Z",
        "substitutions": [
            {"placeholder": "github-token-placeholder", "value": "ghp-real-value", "match_headers": ["authorization"]}
        ],
    }
    (decided,) = service.decided
    assert decided.request.path == "/api/items?x=1"
    assert decided.fence_credential.get_secret_value() == _FENCE


def test_standing_allow_wire_shape_omits_the_absent_deadline() -> None:
    # A standing-policy admission has no deadline (None), so the field stays off the wire; the
    # client-side default restores None on parse (haku/egress/test_decision.py).
    service = _FakeDecideService(DecideAllowed(source=DecisionSource.STANDING, decision_id="standing:haku-github-api"))
    with _client(service) as client:
        response = _post(client)
    assert response.status_code == 200
    assert response.json() == {
        "allowed": True,
        "source": "standing",
        "decision_id": "standing:haku-github-api",
        "substitutions": [],
    }


def test_deny_wire_shape_carries_reason_and_canonical_grant_scope() -> None:
    service = _FakeDecideService(
        DecideDenied(
            reason="no active HTTP grant covers the request",
            grant_scope=GrantScope(scheme="https", host="api.example", port=443),
        )
    )
    with _client(service) as client:
        response = _post(client)
    assert response.status_code == 200
    assert response.json() == {
        "allowed": False,
        "source": "none",
        "reason": "no active HTTP grant covers the request",
        "grant_scope": {"scheme": "https", "host": "api.example", "port": 443},
    }


def test_deny_without_grant_scope_omits_the_field() -> None:
    service = _FakeDecideService(DecideDenied(reason="unknown fence credential"))
    with _client(service) as client:
        response = _post(client)
    assert response.status_code == 200
    assert response.json() == {"allowed": False, "source": "none", "reason": "unknown fence credential"}


def test_connect_admission_has_no_path() -> None:
    service = _FakeDecideService(
        DecideAllowed(source=DecisionSource.GRANT, decision_id="grant:x", valid_until=_VALID_UNTIL)
    )
    body = dict(_BODY, request={"method": "CONNECT", "scheme": None, "host": "api.example", "port": 443, "path": None})
    with _client(service) as client:
        response = _post(client, body=body)
    assert response.status_code == 200
    (decided,) = service.decided
    assert (decided.request.method, decided.request.scheme, decided.request.path) == ("CONNECT", None, None)


def test_authority_failure_is_a_503_never_an_allow() -> None:
    service = _FakeDecideService(HttpDecideUnavailableError("HTTP grant authority is unavailable"))
    with _client(service) as client:
        response = _post(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "HTTP grant authority is unavailable"


def test_malformed_body_is_rejected_before_evaluation() -> None:
    service = _FakeDecideService(DecideDenied(reason="unreached"))
    incoherent = dict(_BODY, upstream_ip="198.51.100.9")  # pinned address outside the resolved set
    with _client(service) as client:
        missing_field = _post(client, body={"fence_credential": _FENCE})
        unresolved_pin = _post(client, body=incoherent)
    assert missing_field.status_code == 422
    assert unresolved_pin.status_code == 422
    assert service.decided == []


def test_verdict_and_auth_failure_responses_never_echo_the_fence_credential() -> None:
    # 422 validation errors echo the rejected input back to its localhost sender by FastAPI
    # convention; verdicts and auth failures are what reach logs and surfaces, and stay clean.
    service = _FakeDecideService(DecideDenied(reason="unknown fence credential"))
    with _client(service) as client:
        responses = [_post(client), _post(client, token="wrong"), _post(client, token=None)]
    assert all(_FENCE not in response.text for response in responses)


if __name__ == "__main__":
    pytest_bazel.main()
