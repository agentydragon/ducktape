"""Testing copies its GitHub and BuildBuddy credentials into the isolated egress namespace."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import BUILDBUDDY_API_KEY_SECRET, GITHUB_PAT_SECRET, TESTING_NAMESPACE
from cluster.cdk8s.agentplane.egress_testing_credentials import add_testing_egress_credentials


def test_egress_credentials_are_copied_and_their_reader_exists() -> None:
    chart = Cdk8sTesting.chart()
    add_testing_egress_credentials(chart, credentials_namespace=TESTING_NAMESPACE)
    objects = Cdk8sTesting.synth(chart)
    secrets = [obj for obj in objects if obj["kind"] == "ExternalSecret"]
    assert {(obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in secrets} == {
        (TESTING_NAMESPACE, GITHUB_PAT_SECRET),
        (TESTING_NAMESPACE, BUILDBUDDY_API_KEY_SECRET),
    }
    # cluster/cdk8s/external_secrets/config.py's external-creds store authenticates as
    # `external-creds-reader` in the consuming namespace; without it ESO cannot read the source secrets.
    assert all(
        secret["spec"]["secretStoreRef"]["name"] == "kubernetes-external-creds-secret-store" for secret in secrets
    )
    assert [
        (obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in objects if obj["kind"] == "ServiceAccount"
    ] == [(TESTING_NAMESPACE, "external-creds-reader")]


if __name__ == "__main__":
    pytest_bazel.main()
