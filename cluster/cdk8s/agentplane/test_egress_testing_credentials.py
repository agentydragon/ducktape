"""Testing copies in only the GitHub bot PAT, beside the ServiceAccount the copy authenticates as."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import GITHUB_PAT_SECRET, TESTING_NAMESPACE
from cluster.cdk8s.agentplane.egress_testing_credentials import add_testing_egress_credentials


def test_only_the_github_pat_is_copied_and_its_reader_exists() -> None:
    chart = Cdk8sTesting.chart()
    add_testing_egress_credentials(chart, credentials_namespace=TESTING_NAMESPACE)
    objects = Cdk8sTesting.synth(chart)
    secrets = [obj for obj in objects if obj["kind"] == "ExternalSecret"]
    assert [(obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in secrets] == [
        (TESTING_NAMESPACE, GITHUB_PAT_SECRET)
    ]
    # cluster/k8s/external-secrets/config/external-creds-secret-store.yaml authenticates as
    # `external-creds-reader` in the consuming namespace; without it ESO cannot read the PAT.
    assert secrets[0]["spec"]["secretStoreRef"]["name"] == "kubernetes-external-creds-secret-store"
    assert [
        (obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in objects if obj["kind"] == "ServiceAccount"
    ] == [(TESTING_NAMESPACE, "external-creds-reader")]


if __name__ == "__main__":
    pytest_bazel.main()
