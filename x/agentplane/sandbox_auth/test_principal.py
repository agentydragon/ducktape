"""Destination workload authentication from TokenReview through the live Sandbox owner."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api

from x.agentplane.sandbox_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    RejectionReason,
    SandboxPrincipal,
    SandboxPrincipalRejectedError,
    SandboxPrincipalResolver,
)

AUDIENCE = "agentplane-egress"
NAMESPACE = "sandboxes"
TOKEN = "opaque-workload-token"
SUBJECT = f"system:serviceaccount:{NAMESPACE}:runner"
POD_NAME = "sandbox-a-pod"
POD_UID = "pod-a-uid"
SANDBOX_NAME = "sandbox-a"
SANDBOX_UID = "sandbox-a-uid"


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


def pod(
    *,
    namespace: str = NAMESPACE,
    name: str = POD_NAME,
    uid: str = POD_UID,
    owners: list[k8s_client.V1OwnerReference] | None = None,
) -> k8s_client.V1Pod:
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            namespace=namespace,
            name=name,
            uid=uid,
            owner_references=(
                owners
                if owners is not None
                else [
                    k8s_client.V1OwnerReference(
                        api_version="agents.x-k8s.io/v1beta1",
                        kind="Sandbox",
                        name=SANDBOX_NAME,
                        uid=SANDBOX_UID,
                        controller=True,
                    )
                ]
            ),
        ),
        status=k8s_client.V1PodStatus(pod_ip="10.0.0.4"),
    )


def resolver(
    token_reviews: Mapping[str, k8s_client.V1TokenReview],
    pods: Mapping[tuple[str, str], k8s_client.V1Pod | Exception],
    *,
    namespaces: frozenset[str] = frozenset({NAMESPACE}),
) -> tuple[SandboxPrincipalResolver, AsyncMock, AsyncMock]:
    async def create_token_review(body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        return token_reviews[body.spec.token]

    async def read_namespaced_pod(name: str, namespace: str) -> k8s_client.V1Pod:
        result = pods[(namespace, name)]
        if isinstance(result, Exception):
            raise result
        return result

    create_token_review_mock = AsyncMock(side_effect=create_token_review)
    read_namespaced_pod_mock = AsyncMock(side_effect=read_namespaced_pod)
    return (
        SandboxPrincipalResolver(
            authentication=cast(AuthenticationV1Api, SimpleNamespace(create_token_review=create_token_review_mock)),
            core_v1=cast(CoreV1Api, SimpleNamespace(read_namespaced_pod=read_namespaced_pod_mock)),
            audience=AUDIENCE,
            namespaces=namespaces,
        ),
        create_token_review_mock,
        read_namespaced_pod_mock,
    )


async def test_valid_bearer_resolves_exact_sandbox_principal() -> None:
    subject_resolver, authentication, core_v1 = resolver({TOKEN: review()}, {(NAMESPACE, POD_NAME): pod()})

    principal = await subject_resolver.resolve(TOKEN)

    assert principal == SandboxPrincipal(
        namespace=NAMESPACE,
        service_account_name="runner",
        service_account_subject=SUBJECT,
        pod_name=POD_NAME,
        pod_uid=POD_UID,
        sandbox_name=SANDBOX_NAME,
        sandbox_uid=SANDBOX_UID,
    )
    authentication.assert_awaited_once()
    sent = authentication.await_args
    assert sent is not None
    body = sent.args[0]
    assert body.spec.audiences == [AUDIENCE]
    core_v1.assert_awaited_once_with(POD_NAME, NAMESPACE)


async def test_same_service_account_two_pods_resolve_different_sandboxes() -> None:
    token_b, pod_b, pod_b_uid, sandbox_b, sandbox_b_uid = (
        "other-token",
        "sandbox-b-pod",
        "pod-b-uid",
        "sandbox-b",
        "sb-b-uid",
    )
    subject_resolver, _, _ = resolver(
        {TOKEN: review(), token_b: review(pod_name=pod_b, pod_uid=pod_b_uid)},
        {
            (NAMESPACE, POD_NAME): pod(),
            (NAMESPACE, pod_b): pod(
                name=pod_b,
                uid=pod_b_uid,
                owners=[
                    k8s_client.V1OwnerReference(
                        api_version="agents.x-k8s.io/v1beta1",
                        kind="Sandbox",
                        name=sandbox_b,
                        uid=sandbox_b_uid,
                        controller=True,
                    )
                ],
            ),
        },
    )

    first, second = await subject_resolver.resolve(TOKEN), await subject_resolver.resolve(token_b)

    assert first.service_account_subject == second.service_account_subject == SUBJECT
    assert (first.pod_uid, first.sandbox_uid) != (second.pod_uid, second.sandbox_uid)
    assert (second.pod_name, second.sandbox_name) == (pod_b, sandbox_b)


async def test_distinct_service_accounts_in_scope_work() -> None:
    other_subject = f"system:serviceaccount:{NAMESPACE}:specialist"
    subject_resolver, _, _ = resolver({TOKEN: review(subject=other_subject)}, {(NAMESPACE, POD_NAME): pod()})

    principal = await subject_resolver.resolve(TOKEN)

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
    subject_resolver, _, core_v1 = resolver({TOKEN: bad_review}, {})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve(TOKEN)

    assert rejected.value.reason is reason
    core_v1.assert_not_awaited()


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
    subject_resolver, _, core_v1 = resolver({TOKEN: review(extra=extra)}, {})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve(TOKEN)

    assert rejected.value.reason is RejectionReason.TOKEN_REJECTED
    core_v1.assert_not_awaited()


@pytest.mark.parametrize(
    "live_pod",
    [
        k8s_client.ApiException(status=404, reason="Not Found"),
        pod(uid="replacement-uid"),
        pod(name="different-name"),
        pod(namespace="different-namespace"),
        k8s_client.V1Pod(metadata=None),
    ],
)
async def test_deleted_replaced_or_incomplete_pod_is_rejected(live_pod: k8s_client.V1Pod | Exception) -> None:
    subject_resolver, _, _ = resolver({TOKEN: review()}, {(NAMESPACE, POD_NAME): live_pod})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve(TOKEN)

    assert rejected.value.reason is RejectionReason.POD_MISMATCH


@pytest.mark.parametrize(
    "owners",
    [
        [],
        [
            k8s_client.V1OwnerReference(
                api_version="apps/v1", kind="Deployment", name="wrong", uid="wrong-uid", controller=True
            )
        ],
        [
            k8s_client.V1OwnerReference(
                api_version="agents.x-k8s.io/v1beta1", kind="Sandbox", name="one", uid="one-uid", controller=True
            ),
            k8s_client.V1OwnerReference(
                api_version="agents.x-k8s.io/v1beta1", kind="Sandbox", name="two", uid="two-uid", controller=True
            ),
        ],
        [
            k8s_client.V1OwnerReference(
                api_version="agents.x-k8s.io/v1beta1", kind="Sandbox", name="not-controller", uid="uid"
            )
        ],
        [
            k8s_client.V1OwnerReference(
                api_version="agents.x-k8s.io/v1beta1", kind="Sandbox", name="", uid="", controller=True
            )
        ],
    ],
)
async def test_requires_exactly_one_controller_sandbox_owner(owners: list[k8s_client.V1OwnerReference]) -> None:
    subject_resolver, _, _ = resolver({TOKEN: review()}, {(NAMESPACE, POD_NAME): pod(owners=owners)})

    with pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve(TOKEN)

    assert rejected.value.reason is RejectionReason.SANDBOX_UNKNOWN


async def test_bearer_never_appears_in_principal_error_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "secret-bearer-must-not-escape"
    subject_resolver, _, _ = resolver({secret: review(audiences=("wrong",))}, {})

    with caplog.at_level(logging.DEBUG), pytest.raises(SandboxPrincipalRejectedError) as rejected:
        await subject_resolver.resolve(secret)

    assert secret not in str(rejected.value)
    assert secret not in repr(rejected.value)
    assert secret not in caplog.text
    assert secret not in repr(
        SandboxPrincipal(NAMESPACE, "runner", SUBJECT, POD_NAME, POD_UID, SANDBOX_NAME, SANDBOX_UID)
    )


if __name__ == "__main__":
    pytest_bazel.main()
