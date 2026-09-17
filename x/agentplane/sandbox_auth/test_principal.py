"""Destination workload authentication: what a Pod-bound bearer proves, and what it must not leak."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api
from multidict import CIMultiDict, CIMultiDictProxy

from x.agentplane.sandbox_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    RejectionReason,
    SandboxPrincipalRejectedError,
    SandboxPrincipalResolver,
    WorkloadPrincipal,
    token_expiry,
)

AUDIENCE = "agentplane-egress"
NAMESPACE = "sandboxes"
TOKEN = "opaque-workload-token"
SUBJECT = f"system:serviceaccount:{NAMESPACE}:runner"
POD_NAME = "sandbox-a-pod"
POD_UID = "pod-a-uid"


def review(
    *,
    subject: str = SUBJECT,
    pod_name: str = POD_NAME,
    pod_uid: str = POD_UID,
    audiences: tuple[str, ...] = (AUDIENCE,),
    authenticated: bool = True,
    extra: Mapping[str, list[str]] | None = None,
) -> k8s_client.V1TokenReview:
    return k8s_client.V1TokenReview(
        spec=k8s_client.V1TokenReviewSpec(token=TOKEN, audiences=[AUDIENCE]),
        status=k8s_client.V1TokenReviewStatus(
            authenticated=authenticated,
            audiences=list(audiences),
            user=k8s_client.V1UserInfo(
                username=subject,
                extra=dict(extra) if extra is not None else {POD_NAME_CLAIM: [pod_name], POD_UID_CLAIM: [pod_uid]},
            ),
        ),
    )


def resolver(
    token_reviews: Mapping[str, k8s_client.V1TokenReview],
    *,
    allowed_service_account_namespaces: frozenset[str] = frozenset({NAMESPACE}),
) -> tuple[SandboxPrincipalResolver, AsyncMock]:
    async def create_token_review(body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        return token_reviews[body.spec.token]

    create_token_review_mock = AsyncMock(side_effect=create_token_review)
    return (
        SandboxPrincipalResolver(
            authentication=cast(AuthenticationV1Api, SimpleNamespace(create_token_review=create_token_review_mock)),
            audience=AUDIENCE,
            allowed_service_account_namespaces=allowed_service_account_namespaces,
        ),
        create_token_review_mock,
    )


def jwt_with_expiry(expiry: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


async def test_a_valid_bearer_is_the_service_account_its_pod_runs_as() -> None:
    subject_resolver, authentication = resolver({TOKEN: review()})

    principal = await subject_resolver.resolve_workload(TOKEN)

    assert principal == WorkloadPrincipal(
        namespace=NAMESPACE,
        service_account_name="runner",
        service_account_subject=SUBJECT,
        pod_name=POD_NAME,
        pod_uid=POD_UID,
    )
    authentication.assert_awaited_once()
    sent = authentication.await_args
    assert sent is not None
    assert sent.args[0].spec.audiences == [AUDIENCE]


async def test_two_pods_on_one_service_account_are_one_caller() -> None:
    """The deliberate consequence of keying on the account: nothing distinguishes the Pods, and
    nothing is meant to. Give a ServiceAccount to one workload if that matters."""
    other = "other-token"
    subject_resolver, _ = resolver({TOKEN: review(), other: review(pod_name="sandbox-b-pod", pod_uid="pod-b-uid")})

    first, second = await subject_resolver.resolve_workload(TOKEN), await subject_resolver.resolve_workload(other)

    assert first.service_account_subject == second.service_account_subject
    assert (first.pod_name, first.pod_uid) != (second.pod_name, second.pod_uid)


async def test_distinct_service_accounts_in_scope_work() -> None:
    other_subject = f"system:serviceaccount:{NAMESPACE}:specialist"
    subject_resolver, _ = resolver({TOKEN: review(subject=other_subject)})

    principal = await subject_resolver.resolve_workload(TOKEN)

    assert (principal.service_account_name, principal.service_account_subject) == ("specialist", other_subject)


@pytest.mark.parametrize(
    ("bad_review", "reason"),
    [
        (review(audiences=("someone-else",)), RejectionReason.TOKEN_REJECTED),
        (review(subject="human@example.com"), RejectionReason.TOKEN_REJECTED),
        (review(subject="system:serviceaccount:elsewhere:runner"), RejectionReason.TOKEN_REJECTED),
        (review(authenticated=False), RejectionReason.TOKEN_REJECTED),
    ],
)
async def test_tokenreview_identity_gates(bad_review: k8s_client.V1TokenReview, reason: RejectionReason) -> None:
    subject_resolver, _ = resolver({TOKEN: bad_review})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve_workload(TOKEN)

    assert rejected.value.reason is reason


@pytest.mark.parametrize(
    "extra",
    [
        {POD_UID_CLAIM: [POD_UID]},
        {POD_NAME_CLAIM: [POD_NAME]},
        {POD_NAME_CLAIM: [], POD_UID_CLAIM: [POD_UID]},
        {POD_NAME_CLAIM: [POD_NAME, "other"], POD_UID_CLAIM: [POD_UID]},
        {POD_NAME_CLAIM: [POD_NAME], POD_UID_CLAIM: [POD_UID, "other"]},
    ],
)
async def test_requires_exactly_one_pod_name_and_uid_claim(extra: Mapping[str, list[str]]) -> None:
    """The claims name the object the API server bound the token to; without exactly one of each
    there is no Pod binding to speak of, whoever the ServiceAccount turns out to be."""
    subject_resolver, _ = resolver({TOKEN: review(extra=extra)})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve_workload(TOKEN)

    assert rejected.value.reason is RejectionReason.TOKEN_REJECTED


async def test_bearer_never_appears_in_principal_error_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "secret-bearer-must-not-escape"
    subject_resolver, _ = resolver({secret: review(audiences=("wrong",))})

    with caplog.at_level(logging.DEBUG), pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve_workload(secret)

    assert secret not in str(rejected.value)
    assert secret not in repr(rejected.value)
    assert secret not in caplog.text
    assert secret not in repr(WorkloadPrincipal(NAMESPACE, "runner", SUBJECT, POD_NAME, POD_UID))


def credential_bearing_api_error(status: int) -> k8s_client.ApiException:
    error = k8s_client.ApiException(status=status, reason=f"credential-bearing-reason: {TOKEN}")
    error.body = f"credential-bearing-body: {TOKEN}".encode()
    error.headers = CIMultiDictProxy(
        CIMultiDict({"Authorization": f"Bearer {TOKEN}", "X-Private": "credential-bearing-header"})
    )
    return error


@pytest.mark.parametrize("status", [0, 403, 503])
async def test_tokenreview_api_failure_logs_only_operation_and_status(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    error = credential_bearing_api_error(status)
    subject_resolver, authentication = resolver({})
    authentication.side_effect = error

    with caplog.at_level(logging.WARNING), pytest.raises(k8s_client.ApiException) as rejected:
        await subject_resolver.resolve_workload(TOKEN)

    assert rejected.value is error
    authentication.assert_awaited_once()
    assert caplog.record_tuples == [
        (
            "x.agentplane.sandbox_auth.principal",
            logging.WARNING,
            f"Kubernetes workload authentication failed: operation=create_token_review status={status}",
        )
    ]
    assert caplog.records[0].exc_info is None
    assert TOKEN not in caplog.text
    assert "credential-bearing" not in caplog.text


async def test_a_verdict_is_reused_without_reviewing_the_token_again() -> None:
    subject_resolver, authentication = resolver({TOKEN: review()})

    assert await subject_resolver.resolve_workload(TOKEN) == await subject_resolver.resolve_workload(TOKEN)

    authentication.assert_awaited_once()


async def test_a_verdict_is_not_kept_past_the_token_expiry() -> None:
    """A bearer already out of life is reviewed every time, so the cache cannot outlive the token."""
    token = jwt_with_expiry(time.time() - 1)
    subject_resolver, authentication = resolver({token: review()})

    await subject_resolver.resolve_workload(token)
    await subject_resolver.resolve_workload(token)

    assert authentication.await_count == 2


async def test_a_refusal_is_reviewed_again() -> None:
    """Nothing keeps a rejection: a bearer bound a moment later must not be refused for a minute."""
    subject_resolver, authentication = resolver({TOKEN: review(authenticated=False)})

    for _ in range(2):
        with pytest.raises(SandboxPrincipalRejectedError):
            await subject_resolver.resolve_workload(TOKEN)

    assert authentication.await_count == 2


def test_token_expiry_parses_jwt_and_ignores_opaque() -> None:
    assert token_expiry(jwt_with_expiry(1_800_000_000)) is not None
    assert token_expiry("opaque-token") is None
    assert token_expiry("a.b.c") is None


if __name__ == "__main__":
    pytest_bazel.main()
