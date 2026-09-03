"""The identity path against the fake API server: TokenReview, live Pod, Sandbox owner, cache."""

from __future__ import annotations

import base64
import json
import time

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api

from x.agentplane.egress.conftest import AUDIENCE, POD_A_IP, POD_A_UID, POD_B_IP, SANDBOX_A, SANDBOX_B, TOKEN_A, TOKEN_B
from x.agentplane.egress.identity import IdentityRejectedError, PodIdentity, PodIdentityVerifier, token_expiry
from x.agentplane.egress.policy import DenyReason
from x.agentplane.egress.testing.fake_apiserver import NAMESPACE, FakeApiServer, TokenVerdict, pod_for


@pytest.fixture
def verifier(api_client: ApiClient) -> PodIdentityVerifier:
    return PodIdentityVerifier(
        authentication=AuthenticationV1Api(api_client),
        core_v1=CoreV1Api(api_client),
        namespace=NAMESPACE,
        audience=AUDIENCE,
        cache_seconds=60,
    )


def jwt_with_expiry(expiry: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


async def test_good_token_from_its_pod(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    identity = await verifier.identify(TOKEN_A, POD_A_IP)
    sandbox_uid = fake.objects["sandboxes"][SANDBOX_A]["metadata"]["uid"]
    assert identity == PodIdentity(
        pod_name=SANDBOX_A, pod_uid=POD_A_UID, pod_ip=POD_A_IP, sandbox_name=SANDBOX_A, sandbox_uid=sandbox_uid
    )


async def test_verdict_cached_but_source_checked_every_time(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    await verifier.identify(TOKEN_A, POD_A_IP)
    await verifier.identify(TOKEN_A, POD_A_IP)
    assert (fake.token_reviews, fake.pod_reads) == (1, 1)
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify(TOKEN_A, POD_B_IP)
    assert rejected.value.reason is DenyReason.POD_MISMATCH
    assert fake.token_reviews == 1


async def test_cache_bounded_by_token_expiry(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    """A token about to expire is not kept past its life: the next call reviews it again."""
    token = jwt_with_expiry(time.time() - 1)
    fake.tokens[token] = fake.tokens[TOKEN_A]
    await verifier.identify(token, POD_A_IP)
    await verifier.identify(token, POD_A_IP)
    assert fake.token_reviews == 2


def test_token_expiry_parses_jwt_and_ignores_opaque() -> None:
    assert token_expiry(jwt_with_expiry(1_800_000_000)) is not None
    assert token_expiry("opaque-token") is None
    assert token_expiry("a.b.c") is None


async def test_copied_token_from_another_pod(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    """Pod B's token presented from Pod A's address: valid token, wrong source."""
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify(TOKEN_B, POD_A_IP)
    assert rejected.value.reason is DenyReason.POD_MISMATCH


async def test_unknown_token(verifier: PodIdentityVerifier) -> None:
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("not-a-token", POD_A_IP)
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_wrong_audience(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    fake.tokens["other-aud"] = TokenVerdict(
        username=f"system:serviceaccount:{NAMESPACE}:sandbox",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
        audiences=("someone-else",),
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("other-aud", POD_A_IP)
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_other_namespace_service_account(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    fake.tokens["elsewhere"] = TokenVerdict(
        username="system:serviceaccount:elsewhere:sandbox", pod_name=SANDBOX_A, pod_uid=POD_A_UID, audiences=(AUDIENCE,)
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("elsewhere", POD_A_IP)
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_replaced_pod(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    """The token names the old Pod UID; the live Pod under that name is a new one."""
    fake.pods[SANDBOX_A] = pod_for(fake, SANDBOX_A, pod_uid="pod-a-uid-2", ip=POD_A_IP)
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify(TOKEN_A, POD_A_IP)
    assert rejected.value.reason is DenyReason.POD_MISMATCH


async def test_gone_pod(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    del fake.pods[SANDBOX_A]
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify(TOKEN_A, POD_A_IP)
    assert rejected.value.reason is DenyReason.POD_MISMATCH


async def test_pod_without_sandbox_owner(fake: FakeApiServer, verifier: PodIdentityVerifier) -> None:
    fake.pods[SANDBOX_B]["metadata"]["ownerReferences"] = []
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify(TOKEN_B, POD_B_IP)
    assert rejected.value.reason is DenyReason.SANDBOX_UNKNOWN


if __name__ == "__main__":
    pytest_bazel.main()
