import pytest_bazel
from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting

from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source

_TARGETS = [
    EgressCredentialSpecTargets(header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD)
]


def test_source_secret_ref() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredential(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.secret_ref(name="my-secret", key="token").to_spec(),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["source"] == {"secretRef": {"name": "my-secret", "key": "token"}}


def test_source_authenticated_workload_token() -> None:
    # The schema's `maxProperties: 0` branch: selecting it means an explicit empty object, not an
    # absent field.
    chart = Cdk8sTesting.chart()
    EgressCredential(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.authenticated_workload_token().to_spec(),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["source"] == {"authenticatedWorkloadToken": {}}


def test_source_projected_workload_token() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredential(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        description="test",
        source=Source.projected_workload_token(audience="https://example.com").to_spec(),
        targets=_TARGETS,
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["source"] == {"projectedWorkloadToken": {"audience": "https://example.com"}}


if __name__ == "__main__":
    pytest_bazel.main()
