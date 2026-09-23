"""The shared construct gives only the egress proxy access and copies in no credential; each
environment's own module decides which credentials it gets."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import TESTING_NAMESPACE, EgressCredentials


def test_only_the_proxy_reads_and_nothing_is_copied() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredentials(chart, "credentials", namespace=TESTING_NAMESPACE, proxy_namespace="agentplane-testing")
    objects = Cdk8sTesting.synth(chart)
    assert not any(obj["kind"] in {"ExternalSecret", "ClusterSecretStore", "ServiceAccount"} for obj in objects)
    bindings = [obj for obj in objects if obj["kind"] == "RoleBinding"]
    assert len(bindings) == 1
    assert bindings[0]["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "agentplane-egress", "namespace": "agentplane-testing"}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
