"""The proxy's door: what a TokenReview verdict becomes on the way to a client, and what it never reads."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.egress.conftest import (
    AUDIENCE,
    POD_A_UID,
    PROJECTED_AUDIENCE,
    PROJECTED_TOKEN_A,
    PROJECTED_TOKEN_B,
    SANDBOX_A,
    SUBJECT_A,
    TOKEN_A,
)
from agentplane.egress.identity import IdentityRejectedError, ProjectedTokenVerifier, WorkloadIdentityVerifier
from agentplane.egress.policy import DenyReason
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipal


async def test_a_good_token_is_the_service_account_its_pod_runs_as(verifier: WorkloadIdentityVerifier) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert identity == WorkloadPrincipal(
        namespace=SANDBOX_NAMESPACE,
        service_account_name=SANDBOX_A,
        service_account_subject=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{SANDBOX_A}",
        pod_name=SANDBOX_A,
        pod_uid=POD_A_UID,
        service_account_uid=f"uid-system:serviceaccount:{SANDBOX_NAMESPACE}:{SANDBOX_A}",
    )
    assert identity.account == SUBJECT_A


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param(None, id="unknown-token"),
        pytest.param(
            TokenVerdict(
                username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox",
                pod_name=SANDBOX_A,
                pod_uid=POD_A_UID,
                audiences=("someone-else",),
            ),
            id="wrong-audience",
        ),
        pytest.param(
            TokenVerdict(
                username="system:serviceaccount:elsewhere:sandbox",
                pod_name=SANDBOX_A,
                pod_uid=POD_A_UID,
                audiences=(AUDIENCE,),
            ),
            id="other-namespace-service-account",
        ),
    ],
)
async def test_a_token_review_the_proxy_does_not_accept_is_rejected(
    fake: FakeApiServer, verifier: WorkloadIdentityVerifier, verdict: TokenVerdict | None
) -> None:
    if verdict is not None:
        fake.tokens["test-token"] = verdict
    with pytest.raises(IdentityRejectedError) as rejected:
        await verifier.identify("test-token")
    assert rejected.value.reason is DenyReason.TOKEN_REJECTED


async def test_the_proxy_never_reads_a_pod(fake: FakeApiServer, verifier: WorkloadIdentityVerifier) -> None:
    """The deployment holds no `pods` grant, so a read here would 403 in every namespace rather than
    fail a test. A replaced or deleted Pod is the API server's to catch, in the TokenReview itself."""
    del fake.pods[SANDBOX_A]
    assert (await verifier.identify(TOKEN_A)).account == SUBJECT_A
    assert fake.pod_reads == 0


async def test_a_projected_token_for_the_presenting_pod_is_kept(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier
) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified({PROJECTED_AUDIENCE: PROJECTED_TOKEN_A}, identity) == {
        PROJECTED_AUDIENCE: PROJECTED_TOKEN_A
    }


@pytest.mark.parametrize(
    "offered",
    [
        # The check the substitution rests on: a caller that somehow holds another Pod's token for
        # this audience cannot have the proxy spend it, even though the hop it arrived on authenticated.
        pytest.param({PROJECTED_AUDIENCE: PROJECTED_TOKEN_B}, id="another-pods-token"),
        # Which audiences may be substituted for is the deployment's to list, so one it never named
        # is refused whatever the token proves.
        pytest.param({"https://unlisted.test.invalid": PROJECTED_TOKEN_A}, id="unconfigured-audience"),
        # Audiences do not substitute for one another: the proxy's own bearer proves the same Pod and
        # is still refused for a destination that validates a different audience.
        pytest.param({PROJECTED_AUDIENCE: TOKEN_A}, id="hop-bearer"),
    ],
)
async def test_a_projected_token_not_proven_for_the_presenting_pod_is_dropped(
    projected: ProjectedTokenVerifier, verifier: WorkloadIdentityVerifier, offered: dict[str, str]
) -> None:
    identity = await verifier.identify(TOKEN_A)
    assert await projected.verified(offered, identity) == {}


if __name__ == "__main__":
    pytest_bazel.main()
