"""The identity path against the fake API server: TokenReview, live Pod, ServiceAccount, cache."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api

from x.agentplane.egress.conftest import (
    AUDIENCE,
    POD_A_UID,
    POD_B_UID,
    SANDBOX_A,
    SANDBOX_B,
    SUBJECT_A,
    TOKEN_A,
    TOKEN_B,
)
from x.agentplane.egress.identity import IdentityRejectedError, PodIdentityVerifier, token_expiry
from x.agentplane.egress.policy import DenyReason
from x.agentplane.egress.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, TokenVerdict
from x.agentplane.sandbox_auth.principal import WorkloadPrincipal


@pytest.fixture
def verifier(api_client: ApiClient) -> PodIdentityVerifier:
    return PodIdentityVerifier(
        authentication=AuthenticationV1Api(api_client),
        namespaces=frozenset({SANDBOX_NAMESPACE}),
        audience=AUDIENCE,
        cache_seconds=60,
    )


def jwt_with_expiry(expiry: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


async def test_a_good_token_is_the_service_account_its_pod_runs_as(verifier: PodIdentityVerifier) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert identity == WorkloadPrincipal(
        namespace=SANDBOX_NAMESPACE,
        service_account_name=SANDBOX_A,
        service_account_subject=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{SANDBOX_A}",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
    )
    assert identity.account == SUBJECT_A


async def test_a_verdict_is_reused_without_reviewing_the_token_again(
    fake: FakeApiServer, verifier: PodIdentityVerifier
) -> None:
    assert await verifier.identify(TOKEN_A) == await verifier.identify(TOKEN_A)
    assert (fake.token_reviews, fake.pod_reads) == (1, 0)


async def test_cache_bounded_by_token_expiry(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    """A token about to expire is not kept past its life: the next call reviews it again."""
    token = jwt_with_expiry(time.time() - 1)
    fake.tokens[token] = fake.tokens[TOKEN_A]
    await verifier.identify(token)
    await verifier.identify(token)
    assert fake.token_reviews == 2


def test_token_expiry_parses_jwt_and_ignores_opaque() -> None:
    assert token_expiry(jwt_with_expiry(1_800_000_000)) is not None
    assert token_expiry("opaque-token") is None
    assert token_expiry("a.b.c") is None


async def test_unknown_token(verifier: PodIdentityVerifier) -> None:
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("not-a-token")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_wrong_audience(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    fake.tokens["other-aud"] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
        audiences=("someone-else",),
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("other-aud")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_other_namespace_service_account(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    fake.tokens["elsewhere"] = TokenVerdict(
        username="system:serviceaccount:elsewhere:sandbox", pod_name=SANDBOX_A, pod_uid=POD_A_UID, audiences=(AUDIENCE,)
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("elsewhere")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_the_proxy_never_reads_a_pod(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    """The deployment holds no `pods` grant, so a read here would 403 in every namespace rather than
    fail a test. A replaced or deleted Pod is the API server's to catch, in the TokenReview itself;
    `sandbox_auth` covers that where the object is still read, for the callers that want its owner."""
    del fake.pods[SANDBOX_A]
    assert (await verifier.identify(TOKEN_A)).account == SUBJECT_A
    assert fake.pod_reads == 0


async def test_pod_no_sandbox_owns_is_still_its_service_account(
    fake: FakeApiServer, verifier: PodIdentityVerifier
) -> None:
    """Nothing here reads the Pod's owner: a Deployment's Pod authenticates exactly as a sandbox's
    does, and whether it may reach anything is decided by whether a binding names that account."""
    fake.pods[SANDBOX_B]["metadata"]["ownerReferences"] = []
    fake.tokens[TOKEN_B] = replace(
        fake.tokens[TOKEN_B], username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:test-workload-sa"
    )
    identity = await verifier.identify(TOKEN_B)
    assert identity == WorkloadPrincipal(
        namespace=SANDBOX_NAMESPACE,
        service_account_name="test-workload-sa",
        service_account_subject=f"system:serviceaccount:{SANDBOX_NAMESPACE}:test-workload-sa",
        pod_name=SANDBOX_B,
        pod_uid=POD_B_UID,
    )


if __name__ == "__main__":
    pytest_bazel.main()
