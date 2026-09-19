"""The proxy's door: what a TokenReview verdict becomes on the way to a client, and what it never reads."""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api

from x.agentplane.egress.conftest import (
    AUDIENCE,
    POD_A_UID,
    POD_B_UID,
    PROJECTED_AUDIENCE,
    PROJECTED_TOKEN_A,
    PROJECTED_TOKEN_B,
    SANDBOX_A,
    SANDBOX_B,
    SUBJECT_A,
    TOKEN_A,
    TOKEN_B,
)
from x.agentplane.egress.identity import IdentityRejectedError, ProjectedTokenVerifier, WorkloadIdentityVerifier
from x.agentplane.egress.policy import DenyReason
from x.agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, TokenVerdict
from x.agentplane.workload_auth.principal import WorkloadPrincipal, WorkloadPrincipalResolver


@pytest.fixture
def verifier(api_client: ApiClient) -> WorkloadIdentityVerifier:
    return WorkloadIdentityVerifier(
        WorkloadPrincipalResolver(
            authentication=AuthenticationV1Api(api_client),
            audience=AUDIENCE,
            allowed_service_account_namespaces=frozenset({SANDBOX_NAMESPACE}),
        )
    )


async def test_a_good_token_is_the_service_account_its_pod_runs_as(verifier: WorkloadIdentityVerifier) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert identity == WorkloadPrincipal(
        namespace=SANDBOX_NAMESPACE,
        service_account_name=SANDBOX_A,
        service_account_subject=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{SANDBOX_A}",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
    )
    assert identity.account == SUBJECT_A


async def test_unknown_token(verifier: WorkloadIdentityVerifier) -> None:
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("not-a-token")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_wrong_audience(fake: FakeApiServer, verifier: WorkloadIdentityVerifier) -> None:
    fake.tokens["other-aud"] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
        audiences=("someone-else",),
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("other-aud")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_other_namespace_service_account(fake: FakeApiServer, verifier: WorkloadIdentityVerifier) -> None:
    fake.tokens["elsewhere"] = TokenVerdict(
        username="system:serviceaccount:elsewhere:sandbox", pod_name=SANDBOX_A, pod_uid=POD_A_UID, audiences=(AUDIENCE,)
    )
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("elsewhere")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_the_proxy_never_reads_a_pod(fake: FakeApiServer, verifier: WorkloadIdentityVerifier) -> None:
    """The deployment holds no `pods` grant, so a read here would 403 in every namespace rather than
    fail a test. A replaced or deleted Pod is the API server's to catch, in the TokenReview itself."""
    del fake.pods[SANDBOX_A]
    assert (await verifier.identify(TOKEN_A)).account == SUBJECT_A
    assert fake.pod_reads == 0


async def test_pod_no_sandbox_owns_is_still_its_service_account(
    fake: FakeApiServer, verifier: WorkloadIdentityVerifier
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


@pytest.fixture
def projected(api_client: ApiClient) -> ProjectedTokenVerifier:
    return ProjectedTokenVerifier(
        resolvers={
            PROJECTED_AUDIENCE: WorkloadPrincipalResolver(
                authentication=AuthenticationV1Api(api_client),
                audience=PROJECTED_AUDIENCE,
                allowed_service_account_namespaces=frozenset({SANDBOX_NAMESPACE}),
            )
        }
    )


async def test_a_projected_token_for_the_presenting_pod_is_kept(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier
) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified({PROJECTED_AUDIENCE: PROJECTED_TOKEN_A}, identity) == {
        PROJECTED_AUDIENCE: PROJECTED_TOKEN_A
    }


async def test_another_pods_projected_token_is_dropped(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier
) -> None:
    """The check the substitution rests on: a caller that somehow holds another Pod's token for this
    audience cannot have the proxy spend it, even though the hop it arrived on authenticated."""
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified({PROJECTED_AUDIENCE: PROJECTED_TOKEN_B}, identity) == {}


async def test_a_token_for_an_unconfigured_audience_is_dropped(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier
) -> None:
    """Which audiences may be substituted for is the deployment's to list, so one it never named is
    refused whatever the token proves."""
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified({"https://unlisted.test.invalid": PROJECTED_TOKEN_A}, identity) == {}


async def test_a_hop_bearer_presented_as_a_projected_token_is_dropped(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier
) -> None:
    """Audiences do not substitute for one another: the proxy's own bearer proves the same Pod and is
    still refused for a destination that validates a different audience."""
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified({PROJECTED_AUDIENCE: TOKEN_A}, identity) == {}


if __name__ == "__main__":
    pytest_bazel.main()
