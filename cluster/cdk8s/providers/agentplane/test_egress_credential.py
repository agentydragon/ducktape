from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting

from cluster.cdk8s.providers.agentplane.egress_credential import Source

_TARGETS = [
    EgressCredentialSpecTargets(header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD)
]


def test_source_secret_ref(scratch_chart: Chart) -> None:
    EgressCredential(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-credential"),
        spec=EgressCredentialSpec(
            description="test", source=Source.secret_ref(name="my-secret", key="token"), targets=_TARGETS
        ),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert manifest["spec"]["source"] == {"secretRef": {"name": "my-secret", "key": "token"}}
