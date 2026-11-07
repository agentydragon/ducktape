import pytest_bazel
from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting

from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source

_TARGETS = [
    EgressCredentialSpecTargets(header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD)
]


def test_source_secret_ref(scratch_chart: Chart) -> None:
    EgressCredential(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.secret_ref(name="my-secret", key="token"),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert manifest["spec"]["source"] == {"secretRef": {"name": "my-secret", "key": "token"}}


def test_source_authenticated_workload_token(scratch_chart: Chart) -> None:
    # The schema's `maxProperties: 0` branch: selecting it means an explicit empty object, not an
    # absent field.
    EgressCredential(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.authenticated_workload_token(),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert manifest["spec"]["source"] == {"authenticatedWorkloadToken": {}}


def test_source_projected_workload_token(scratch_chart: Chart) -> None:
    EgressCredential(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.projected_workload_token(audience="https://example.com"),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert manifest["spec"]["source"] == {"projectedWorkloadToken": {"audience": "https://example.com"}}


if __name__ == "__main__":
    pytest_bazel.main()
