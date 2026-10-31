"""The real Go Action facade and its operator federation boundary.

The retired ASGI-only checks moved to Go tests alongside the owning behavior:
``TestGeneratedActionFacadeUsesNativeRPCAndRequestBoundBearer``,
``TestCallerGrantsUsesFederatedGeneratedRPCAndReturnsSafeView``,
``TestExchangePostsAssertionAndVerifiesTargetIdentity``,
``TestRefusedRefreshEndsSession``, ``TestOIDCCallbackAndTwoOperatorsWorkAcrossReplicas``, and
``TestConcurrentCrossReplicaCallbacksConsumePendingLoginOnce``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import jwt
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service import action_service_pb2
from agentplane.app.testing.fake_action_service import FakeActionService, served_action_service
from agentplane.app.testing.replication_process import (
    AppProcess,
    GatewayAccess,
    OIDCProviderOptions,
    SandboxesAccess,
    app_process,
)
from agentplane.history_service.client import HistoryServiceClient

ACTION_AUDIENCE = "test-actions"
ACTION_REQUESTS = "/upstream/actions/v1/action-requests"


@dataclass
class FederationReview:
    app: AppProcess
    service: FakeActionService
    provider: OIDCProviderOptions
    issuer: str
    sandbox: Any


@pytest.fixture
def token_renew_before_seconds() -> int:
    return 30


@pytest.fixture
def reject_refresh_token() -> bool:
    return False


@pytest.fixture
async def federation_review(
    db_url: str,
    history_client: HistoryServiceClient,
    sandbox: Any,
    gateway_service: Any,
    app_token_file: Path,
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
                }
            }

        async with app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(sandbox.server.kubeconfig, sandbox.namespace),
            gateway=GatewayAccess(gateway_service.target, app_token_file),
            oidc_provider_options=provider,
            config_for_oidc_issuer=action_federation,
            oidc_env={"AGENTPLANE_OIDC_TOKEN_RENEW_BEFORE_SECONDS": str(token_renew_before_seconds)},
        ) as app:
            assert app.oidc_issuer is not None
            yield FederationReview(app, service, provider, app.oidc_issuer, sandbox)


async def _list_action_requests(review: FederationReview) -> httpx.Response:
    async with httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies) as browser:
        return await browser.get(
            ACTION_REQUESTS, params=[("state", "ACTION_STATE_DECISION_PENDING"), ("limit", "3"), ("afterId", "last-id")]
        )


async def _read_sse_event(lines: Any) -> tuple[str, dict[str, Any]]:
    event = "message"
    data: list[str] = []
    async for line in lines:
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").lstrip())
        elif not line and data:
            value = json.loads("\n".join(data))
            assert isinstance(value, dict), value
            return event, value
    raise AssertionError("live sandbox stream ended before its next event")


def _get_action_policy_calls(review: FederationReview) -> list[Any]:
    return [call for call in review.service.calls if call.method == "GetActionPolicy"]


@asynccontextmanager
async def _live_sandbox_stream(browser: httpx.AsyncClient, name: str) -> AsyncIterator[Any]:
    async with browser.stream("GET", f"/live/sandboxes/{name}", timeout=None) as response:
        assert response.status_code == 200
        yield response.aiter_lines()


def _action_policy_response(*, synced: bool) -> action_service_pb2.GetActionPolicyResponse:
    return action_service_pb2.GetActionPolicyResponse(
        action_policy=action_service_pb2.CallerActionPolicy(synced=synced)
    )


def _action_policy_subjects(review: FederationReview) -> list[tuple[str, str]]:
    subjects = []
    for call in _get_action_policy_calls(review):
        request = cast(action_service_pb2.GetActionPolicyRequest, call.request)
        subjects.append((request.subject.namespace, request.subject.name))
    return subjects


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


async def test_live_action_policy_retries_failure_and_reuses_same_generation(
    federation_review: FederationReview,
) -> None:
    review = federation_review
    review.service.policy = _action_policy_response(synced=True)
    review.service.fail_next_policy_unavailable()
    async with (
        httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies, timeout=None) as browser,
        _live_sandbox_stream(browser, review.sandbox.name) as events,
    ):
        event, failed = await _read_sse_event(events)
        assert event == "snapshot"
        assert failed["sandbox"]["name"] == review.sandbox.name
        assert failed["action_policy"]["code"] == "upstream_request_failed"
        assert len(_get_action_policy_calls(review)) == 1

        await review.sandbox.patch_sandbox({"metadata": {"annotations": {"policy-cache-retry": "1"}}})
        event, retried = await _read_sse_event(events)
        assert event == "snapshot"
        assert retried["action_policy"]["synced"] is True
        assert len(_get_action_policy_calls(review)) == 2

        # A stream skips a frame identical to its last one, so this change must show in the frame.
        wake_on_send = not (await review.sandbox.sandbox())["spec"].get("wakeOnSend", False)
        await review.sandbox.patch_sandbox({"spec": {"wakeOnSend": wake_on_send}})
        event, reused = await _read_sse_event(events)
        assert event == "snapshot"
        assert reused["sandbox"]["wake_on_send"] is wake_on_send
        assert reused["action_policy"]["synced"] is True
        assert len(_get_action_policy_calls(review)) == 2


async def test_live_action_policy_invalidates_on_policy_set_and_binding_events(
    federation_review: FederationReview,
) -> None:
    review = federation_review
    review.service.policy = _action_policy_response(synced=True)
    async with (
        httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies, timeout=None) as browser,
        _live_sandbox_stream(browser, review.sandbox.name) as events,
    ):
        event, initial = await _read_sse_event(events)
        assert event == "snapshot"
        assert initial["action_policy"]["synced"] is True
        assert len(_get_action_policy_calls(review)) == 1

        # A stream skips a frame identical to its last one, so each re-read answers differently.
        review.service.policy = _action_policy_response(synced=False)
        sandbox = await review.sandbox.sandbox()
        group, version = sandbox["apiVersion"].split("/", maxsplit=1)
        await review.sandbox.custom_objects.create_namespaced_custom_object(
            group,
            version,
            review.sandbox.namespace,
            "policysets",
            {
                "apiVersion": sandbox["apiVersion"],
                "kind": "PolicySet",
                "metadata": {"name": "live-action-policy"},
                "spec": {
                    "rules": [
                        {
                            "type": "action_auto_approve",
                            "policy": {"type": "exact_actions", "actions": {"test": ["read"]}},
                        }
                    ]
                },
            },
        )
        event, after_set = await _read_sse_event(events)
        assert event == "snapshot"
        assert after_set["action_policy"]["synced"] is False
        assert len(_get_action_policy_calls(review)) == 2

        review.service.policy = _action_policy_response(synced=True)

        await review.sandbox.custom_objects.create_namespaced_custom_object(
            group,
            version,
            review.sandbox.namespace,
            "policybindings",
            {
                "apiVersion": sandbox["apiVersion"],
                "kind": "PolicyBinding",
                "metadata": {"name": "live-action-binding"},
                "spec": {
                    "subject": {"namespace": review.sandbox.namespace, "name": review.sandbox.name},
                    "policySets": ["live-action-policy"],
                },
            },
        )
        event, after_binding = await _read_sse_event(events)
        assert event == "snapshot"
        assert after_binding["action_policy"]["synced"] is True
        assert len(_get_action_policy_calls(review)) == 3


async def test_live_action_policy_is_scoped_to_each_sandbox_service_account(
    federation_review: FederationReview,
) -> None:
    review = federation_review
    namespace = review.sandbox.namespace
    first_name = review.sandbox.name
    second_name = "other-runner"
    review.service.policy = _action_policy_response(synced=True)
    review.service.policy_by_subject[(namespace, second_name)] = _action_policy_response(synced=False)
    sandbox = await review.sandbox.sandbox()
    group, version = sandbox["apiVersion"].split("/", maxsplit=1)
    await review.sandbox.custom_objects.create_namespaced_custom_object(
        group,
        version,
        namespace,
        "sandboxes",
        {
            "apiVersion": sandbox["apiVersion"],
            "kind": "Sandbox",
            "metadata": {"name": second_name},
            "spec": {"template": "test-template", "running": True},
        },
    )

    async with httpx.AsyncClient(base_url=review.app.url, cookies=review.app.cookies, timeout=None) as browser:
        async with _live_sandbox_stream(browser, first_name) as events:
            event, first = await _read_sse_event(events)
            assert event == "snapshot"
            assert first["sandbox"]["service_account"]["name"] == first_name
            assert first["action_policy"]["synced"] is True

        async with _live_sandbox_stream(browser, second_name) as events:
            while True:
                event, second = await _read_sse_event(events)
                assert event == "snapshot"
                if second["sandbox"] is not None:
                    break
            assert second["sandbox"]["service_account"]["name"] == second_name
            assert second["action_policy"]["synced"] is False

    assert _action_policy_subjects(review) == [(namespace, first_name), (namespace, second_name)]


if __name__ == "__main__":
    pytest_bazel.main()
