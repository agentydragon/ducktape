"""The agent API is an ordinary independently authenticated destination."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_bazel

from agentplane.egress.conftest import RULES_HOST, SANDBOX_A, SANDBOX_B, TOKEN_A
from agentplane.egress.policy import Index
from agentplane.egress.rules_api import PATH, RulesProjection, create_rules_app
from agentplane.kubernetes_watch import Freshness
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SUBJECT_A = {"namespace": SANDBOX_NAMESPACE, "name": SANDBOX_A}


@pytest.fixture
async def api(workload_resolver: WorkloadPrincipalResolver) -> AsyncIterator[httpx.AsyncClient]:
    """The rules app over an empty index: the subject's identity is the whole answer."""
    app = create_rules_app(
        WorkloadPrincipalAuthenticator(workload_resolver),
        RulesProjection(Index(freshness=Freshness(stale_after_seconds=180)), clock=lambda: NOW),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{RULES_HOST}") as client:
        yield client


async def test_api_independently_tokenreviews_authorization_and_returns_redacted_rules(
    fake: FakeApiServer, api: httpx.AsyncClient
) -> None:
    before = (fake.token_reviews, fake.pod_reads)

    response = await api.get(PATH, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert response.status_code == 200
    assert response.json() == {"subject": SUBJECT_A, "policies": []}
    assert (fake.token_reviews, fake.pod_reads) == (before[0] + 1, before[1])
    assert TOKEN_A not in response.text


async def test_direct_api_without_or_with_forged_authorization_fails_closed(
    fake: FakeApiServer, api: httpx.AsyncClient
) -> None:
    missing = await api.get(PATH)
    forged = await api.get(PATH, headers={"Authorization": "Bearer forged"})

    assert (missing.status_code, forged.status_code) == (401, 401)
    assert missing.json() == forged.json() == {"detail": "invalid workload bearer"}
    assert "forged" not in forged.text
    assert fake.token_reviews == 1, "missing Authorization must not reach TokenReview"


async def test_headers_body_and_proxy_auth_cannot_select_another_subject(api: httpx.AsyncClient) -> None:
    response = await api.request(
        "GET",
        PATH,
        headers={
            "Authorization": f"Bearer {TOKEN_A}",
            "Proxy-Authorization": "Bearer forged-hop",
            "X-Agentplane-Subject": SANDBOX_B,
            "Content-Type": "application/json",
        },
        content=json.dumps({"namespace": SANDBOX_NAMESPACE, "name": SANDBOX_B}),
    )

    assert response.status_code == 200
    assert response.json()["subject"] == SUBJECT_A
    assert all(value not in response.text for value in ("forged-hop", SANDBOX_B))


@pytest.mark.parametrize("path", ["/", "/decisions", "/healthz"])
async def test_api_exposes_no_other_route(api: httpx.AsyncClient, path: str) -> None:
    response = await api.get(path, headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert response.status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
