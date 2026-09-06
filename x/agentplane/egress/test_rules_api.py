"""The agent API is an ordinary independently authenticated destination."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api

from x.agentplane.egress.conftest import AUDIENCE, SANDBOX_A, SANDBOX_B, TOKEN_A
from x.agentplane.egress.policy import Index
from x.agentplane.egress.resources import ObjectMeta, Sandbox
from x.agentplane.egress.rules_api import HOST, PATH, URL, RulesProjection, create_rules_app
from x.agentplane.egress.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SANDBOX = Sandbox(metadata=ObjectMeta(name=SANDBOX_A, uid=f"uid-sandboxes-{SANDBOX_A}"))


def client(api_client: ApiClient, index: Index) -> httpx.AsyncClient:
    authenticator = SandboxPrincipalAuthenticator(
        SandboxPrincipalResolver(
            authentication=AuthenticationV1Api(api_client),
            core_v1=CoreV1Api(api_client),
            audience=AUDIENCE,
            namespaces=frozenset({SANDBOX_NAMESPACE}),
        )
    )
    app = create_rules_app(authenticator, RulesProjection(index, clock=lambda: NOW))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")


async def test_api_independently_tokenreviews_authorization_and_returns_redacted_rules(
    fake: FakeApiServer, api_client: ApiClient
) -> None:
    index = Index(sandboxes={SANDBOX_A: SANDBOX})
    before = (fake.token_reviews, fake.pod_reads)

    async with client(api_client, index) as api:
        response = await api.get(PATH, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert f"http://{HOST}{PATH}" == URL
    assert response.status_code == 200
    assert response.json() == {"sandbox": SANDBOX_A, "policies": []}
    assert (fake.token_reviews, fake.pod_reads) == (before[0] + 1, before[1] + 1)
    assert TOKEN_A not in response.text


async def test_direct_api_without_or_with_forged_authorization_fails_closed(
    fake: FakeApiServer, api_client: ApiClient
) -> None:
    index = Index(sandboxes={SANDBOX_A: SANDBOX})

    async with client(api_client, index) as api:
        missing = await api.get(PATH)
        forged = await api.get(PATH, headers={"Authorization": "Bearer forged"})

    assert (missing.status_code, forged.status_code) == (401, 401)
    assert missing.json() == forged.json() == {"detail": "invalid workload bearer"}
    assert "forged" not in forged.text
    assert fake.token_reviews == 1, "missing Authorization must not reach TokenReview"


async def test_headers_body_and_proxy_auth_cannot_select_another_sandbox(api_client: ApiClient) -> None:
    index = Index(sandboxes={SANDBOX_A: SANDBOX})

    async with client(api_client, index) as api:
        response = await api.request(
            "GET",
            PATH,
            headers={
                "Authorization": f"Bearer {TOKEN_A}",
                "Proxy-Authorization": "Bearer forged-hop",
                "X-Agentplane-Sandbox": SANDBOX_B,
                "X-Sandbox-UID": "forged-uid",
                "Content-Type": "application/json",
            },
            content=json.dumps({"sandbox": SANDBOX_B, "sandbox_uid": "forged-uid"}),
        )

    assert response.status_code == 200
    assert response.json()["sandbox"] == SANDBOX_A
    assert all(value not in response.text for value in ("forged-hop", "forged-uid", SANDBOX_B))


async def test_stale_or_replaced_sandbox_fails_after_live_principal_resolution(api_client: ApiClient) -> None:
    index = Index(sandboxes={SANDBOX_A: SANDBOX.model_copy(update={"metadata": ObjectMeta(name=SANDBOX_A, uid="new")})})

    async with client(api_client, index) as api:
        response = await api.get(PATH, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid workload bearer"}


@pytest.mark.parametrize("path", ["/", "/decisions", "/healthz"])
async def test_api_exposes_no_other_route(api_client: ApiClient, path: str) -> None:
    async with client(api_client, Index(sandboxes={SANDBOX_A: SANDBOX})) as api:
        response = await api.get(path, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert response.status_code == 404


async def test_api_does_not_require_sandbox_source_address(fake: FakeApiServer, api_client: ApiClient) -> None:
    fake.pods[SANDBOX_A]["status"]["podIP"] = "10.99.0.42"
    async with client(api_client, Index(sandboxes={SANDBOX_A: SANDBOX})) as api:
        response = await api.get(PATH, headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert response.status_code == 200


if __name__ == "__main__":
    pytest_bazel.main()
