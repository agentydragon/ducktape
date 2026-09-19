"""The destination HTTP adapter accepts one bearer and ignores forged identity metadata."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_bazel
from fastapi import HTTPException, Request
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    WorkloadPrincipal,
    WorkloadPrincipalResolver,
)

TOKEN = "workload-token"
NAMESPACE = "sandboxes"
SUBJECT = f"system:serviceaccount:{NAMESPACE}:runner"
PRINCIPAL = WorkloadPrincipal(NAMESPACE, "runner", SUBJECT, "pod-a", "pod-uid")


def authenticator() -> tuple[WorkloadPrincipalAuthenticator, AsyncMock]:
    create_token_review = AsyncMock(
        return_value=k8s_client.V1TokenReview(
            spec=k8s_client.V1TokenReviewSpec(token=TOKEN, audiences=["agentplane-egress"]),
            status=k8s_client.V1TokenReviewStatus(
                authenticated=True,
                audiences=["agentplane-egress"],
                user=k8s_client.V1UserInfo(
                    username=SUBJECT, extra={POD_NAME_CLAIM: [PRINCIPAL.pod_name], POD_UID_CLAIM: [PRINCIPAL.pod_uid]}
                ),
            ),
        )
    )
    return (
        WorkloadPrincipalAuthenticator(
            WorkloadPrincipalResolver(
                authentication=cast(AuthenticationV1Api, SimpleNamespace(create_token_review=create_token_review)),
                audience="agentplane-egress",
                allowed_service_account_namespaces=(NAMESPACE,),
            )
        ),
        create_token_review,
    )


def request(*headers: tuple[bytes, bytes], body: bytes = b"") -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": list(headers)}, receive)


@pytest.mark.parametrize(
    "headers",
    [
        (),
        ((b"authorization", b"Basic abc"),),
        ((b"authorization", b"Bearer"),),
        ((b"authorization", b"Bearer one two"),),
        ((b"authorization", b"Bearer one, Bearer two"),),
        ((b"authorization", b"Bearer one"), (b"authorization", b"Bearer two")),
    ],
)
async def test_missing_malformed_or_multiple_bearers_fail_closed(headers: tuple[tuple[bytes, bytes], ...]) -> None:
    dependency, authentication = authenticator()

    with pytest.raises(HTTPException) as rejected:
        await dependency(request(*headers))

    assert (rejected.value.status_code, rejected.value.detail) == (401, "invalid workload bearer")
    authentication.assert_not_awaited()


async def test_forged_identity_metadata_cannot_affect_result() -> None:
    dependency, authentication = authenticator()
    forged = request(
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (b"x-sandbox-name", b"forged"),
        (b"x-sandbox-uid", b"forged"),
        (b"x-pod-name", b"forged"),
        (b"x-agent-id", b"forged"),
        body=b'{"sandbox_name":"forged","sandbox_uid":"forged"}',
    )

    principal = await dependency(forged)

    assert principal == PRINCIPAL
    authentication.assert_awaited_once()
    sent = authentication.await_args
    assert sent is not None
    assert sent.args[0].spec.token == TOKEN


if __name__ == "__main__":
    pytest_bazel.main()
