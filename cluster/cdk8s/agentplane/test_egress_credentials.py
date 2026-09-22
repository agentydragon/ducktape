"""The shared credentials namespace holds only credentials every environment gets."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import TESTING_NAMESPACE, EgressCredentials


def test_only_the_proxy_reads_and_only_github_is_copied() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredentials(chart, "credentials", namespace=TESTING_NAMESPACE, proxy_namespace="agentplane-testing")
    objects = Cdk8sTesting.synth(chart)
    assert not any(obj["kind"] == "ClusterSecretStore" for obj in objects)
    assert not any(obj["metadata"].get("namespace") == "haku-sandbox" for obj in objects)
    secrets = [obj for obj in objects if obj["kind"] == "ExternalSecret"]
    assert [(obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in secrets] == [
        (TESTING_NAMESPACE, "agentplane-github-pat")
    ]
    bindings = [obj for obj in objects if obj["kind"] == "RoleBinding"]
    assert len(bindings) == 1
    assert bindings[0]["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "agentplane-egress", "namespace": "agentplane-testing"}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
