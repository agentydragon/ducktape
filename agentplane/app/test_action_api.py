"""The real Go Action facade and its operator federation boundary.

The retired ASGI-only checks moved to Go tests alongside the owning behavior:
``TestGeneratedActionFacadeUsesNativeRPCAndRequestBoundBearer``,
``TestExchangePostsAssertionAndVerifiesTargetIdentity``,
``TestRefusedRefreshEndsSession``, ``TestOIDCCallbackAndTwoOperatorsWorkAcrossReplicas``, and
``TestConcurrentCrossReplicaCallbacksConsumePendingLoginOnce``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import httpx
import jwt
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service import action_service_pb2
from agentplane.app.testing.fake_action_service import FakeActionService, served_action_service
from agentplane.app.testing.replication_process import AppProcess, OIDCProviderOptions, SandboxesAccess, app_process
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.go_service import WRITER
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer

# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)

ACTION_AUDIENCE = "test-actions"
ACTION_REQUESTS = "/upstream/actions/v1/action-requests"
ACTION_POLICY_SUBJECT_NAMESPACE = "action-federation-tests"
ACTION_POLICY_SUBJECT_NAME = "test-runner"


@dataclass
class FederationReview:
    app: AppProcess
    service: FakeActionService
    provider: OIDCProviderOptions
    issuer: str


@pytest.fixture
def token_renew_before_seconds() -> int:
    return 30


@pytest.fixture
def reject_refresh_token() -> bool:
    return False


@pytest.fixture
def history_writer_namespaces() -> tuple[str, ...]:
    """These tests have no Sandbox writer; retain only the scripted test writer."""
    return (WRITER.namespace,)


@pytest.fixture
async def federation_review(
    db_url: str,
    history_client: HistoryServiceClient,
    api_server: KubernetesApiServer,
    token_renew_before_seconds: int,
    reject_refresh_token: bool,
) -> AsyncIterator[FederationReview]:
    service = FakeActionService()
    provider = OIDCProviderOptions(
        authentik_compatible=True,
        exchange_token_audience=ACTION_AUDIENCE,
        expected_exchange_client_id=ACTION_AUDIENCE,
        reject_refresh=reject_refresh_token,
    )
    async with served_action_service(service) as (_, target):

        def action_federation(issuer: str) -> dict[str, object]:
            token_endpoint = issuer.removesuffix("agentplane/") + "token/"
            jwks_uri = issuer + "jwks/"
            return {
                "action_federation": {
                    "mode": "exchange",
                    "service_url": f"http://{target}",
                    "login_jwks_uri": jwks_uri,
                    "login_token_profile": "authentik",
                    "target": {
                        "issuer": issuer,
                        "audience": ACTION_AUDIENCE,
                        "jwks_uri": jwks_uri,
                        "token_profile": "authentik",
                    },
                    "scope": "openid actions",
                    "token_endpoint": token_endpoint,
                },
                "oidc": {"token_renew_before_seconds": token_renew_before_seconds},
            }

        async with app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(api_server.kubeconfig, ACTION_POLICY_SUBJECT_NAMESPACE),
            gateway=None,
            oidc_provider_options=provider,
            config_for_oidc_issuer=action_federation,
        ) as app:
            assert app.oidc_issuer is not None
            yield FederationReview(app, service, provider, app.oidc_issuer)


async def _list_action_requests(review: FederationReview) -> httpx.Response:
    async with httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies) as browser:
        return await browser.get(
            ACTION_REQUESTS, params=[("state", "ACTION_STATE_DECISION_PENDING"), ("limit", "3"), ("afterId", "last-id")]
        )


async def _expire_login_access_token(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "UPDATE operator_browser_session SET access_token_expires_at = now() - interval '1 second' "
                "WHERE operator_issuer IS NOT NULL"
            )
        )
    assert result.rowcount == 1


async def test_action_facade_exchanges_a_signed_login_and_builds_the_rpc(federation_review: FederationReview) -> None:
    review = federation_review
    assert review.provider.client_assertions == []

    async with httpx.AsyncClient(base_url=review.app.url) as anonymous:
        assert (await anonymous.get(ACTION_REQUESTS)).status_code == 401

    response = await _list_action_requests(review)

    assert response.status_code == 200, response.text
    assert len(review.provider.client_assertions) == 1
    assert len(review.service.calls) == 1
    call = review.service.calls[0]
    request = cast(action_service_pb2.ListActionRequestsRequest, call.request)
    assert call.method == "ListActionRequests"
    assert request.idempotency_key == ""
    assert request.limit == 3
    assert request.after_id == "last-id"
    assert list(request.state) == [action_service_pb2.ACTION_STATE_DECISION_PENDING]
    assert call.bearer_token is not None
    assert call.bearer_token.startswith("Bearer ")

    login_claims = jwt.decode(
        review.provider.client_assertions[0], algorithms=["RS256"], options={"verify_signature": False}
    )
    target_claims = jwt.decode(
        call.bearer_token.removeprefix("Bearer "), algorithms=["RS256"], options={"verify_signature": False}
    )
    assert login_claims["iss"] == review.issuer
    assert login_claims["aud"] == "test-agentplane"
    assert login_claims["sub"] == "test-operator"
    assert target_claims["iss"] == review.issuer
    assert target_claims["aud"] == ACTION_AUDIENCE
    assert target_claims["azp"] == ACTION_AUDIENCE
    assert target_claims["sub"] == login_claims["sub"]
    assert call.bearer_token != f"Bearer {review.provider.client_assertions[0]}"

    async with httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies) as browser:
        invalid = await browser.get(ACTION_REQUESTS, params={"state": "ACTION_STATE_UNKNOWN"})
    assert invalid.status_code == 400


async def test_action_exchange_uses_a_rotated_login_token_once(
    federation_review: FederationReview, engine: AsyncEngine
) -> None:
    review = federation_review
    await _expire_login_access_token(engine)
    for _ in range(2):
        response = await _list_action_requests(review)
        assert response.status_code == 200, response.text

    assert len(review.provider.refresh_tokens) == 1
    assert len(review.provider.issued_refresh_tokens) == 2
    assert review.provider.refresh_tokens == [review.provider.issued_refresh_tokens[0]]
    assert review.provider.issued_refresh_tokens[0] != review.provider.issued_refresh_tokens[1]
    assert len(review.provider.client_assertions) == 2
    assert len(review.service.calls) == 2


@pytest.mark.parametrize("reject_refresh_token", [True])
async def test_refused_refresh_requires_a_new_operator_login(
    federation_review: FederationReview, engine: AsyncEngine
) -> None:
    review = federation_review
    await _expire_login_access_token(engine)
    response = await _list_action_requests(review)

    assert response.status_code == 401, response.text
    assert response.json() == {"detail": {"code": "operator_reauthentication_required"}}
    assert len(review.provider.refresh_tokens) == 1
    assert len(review.provider.issued_refresh_tokens) == 1
    assert review.provider.client_assertions == []
    assert review.service.calls == []
    async with httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies) as browser:
        assert (await browser.get("/auth/me")).status_code == 401


async def test_action_policy_watch_uses_generated_facade(federation_review: FederationReview) -> None:
    review = federation_review
    subject = {"subject.namespace": ACTION_POLICY_SUBJECT_NAMESPACE, "subject.name": ACTION_POLICY_SUBJECT_NAME}
    review.service.policy.action_policy.synced = True
    async with (
        httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies, timeout=None) as browser,
        browser.stream("GET", "/upstream/actions/v1/action-policy:watch", params=subject) as response,
    ):
        assert response.status_code == 200
        lines = response.aiter_lines()
        initial = ParseDict(json.loads(await anext(lines))["result"], action_service_pb2.GetActionPolicyResponse())
        assert initial.action_policy.synced
        assert initial.action_policy.subject.namespace == ACTION_POLICY_SUBJECT_NAMESPACE
        assert initial.action_policy.subject.name == ACTION_POLICY_SUBJECT_NAME
        changed = action_service_pb2.GetActionPolicyResponse()
        changed.action_policy.subject.CopyFrom(initial.action_policy.subject)
        changed.action_policy.synced = False
        await review.service.policy_watch_frames.put(changed)
        async with asyncio.timeout(10):
            frame = ParseDict(json.loads(await anext(lines))["result"], action_service_pb2.GetActionPolicyResponse())
        assert frame == changed
        calls = [call for call in review.service.calls if call.method == "WatchActionPolicy"]
        assert len(calls) == 1
        assert calls[0].bearer_token
        assert calls[0].bearer_token.startswith("Bearer ")
        assert calls[0].request == action_service_pb2.GetActionPolicyRequest(subject=initial.action_policy.subject)
        assert not any(call.method == "GetActionPolicy" for call in review.service.calls)
    async with asyncio.timeout(10):
        await review.service.policy_watch_cancelled.wait()


if __name__ == "__main__":
    pytest_bazel.main()
