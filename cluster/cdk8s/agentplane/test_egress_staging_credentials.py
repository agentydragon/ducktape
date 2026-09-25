"""Staging's Forgejo credential reaches its credentials namespace through exact source access."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import STAGING_NAMESPACE
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials


def test_forgejo_password_has_one_reader_and_exact_source_access() -> None:
    chart = Cdk8sTesting.chart()
    add_staging_egress_credentials(chart, namespace="agentplane-staging", credentials_namespace=STAGING_NAMESPACE)
    objects = Cdk8sTesting.synth(chart)
    binding = next(obj for obj in objects if obj["kind"] == "RoleBinding")
    assert binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "external-creds-reader", "namespace": STAGING_NAMESPACE}
    ]

    source_role = next(
        obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["namespace"] == "haku-sandbox"
    )
    assert source_role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["haku-forgejo-git"], "verbs": ["get"]}
    ]
    store = next(obj for obj in objects if obj["kind"] == "ClusterSecretStore")
    assert store["spec"]["conditions"] == [{"namespaces": [STAGING_NAMESPACE]}]
    provider = store["spec"]["provider"]["kubernetes"]
    assert provider["remoteNamespace"] == "haku-sandbox"
    assert provider["auth"]["serviceAccount"] == {"name": "external-creds-reader"}

    secrets = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "ExternalSecret"}
    assert {obj["metadata"]["namespace"] for obj in secrets.values()} == {STAGING_NAMESPACE}
    assert set(secrets) == {
        "agentplane-github-pat",
        "haku-forgejo-git",
        "grocy-sf-readonly",
        "home-assistant-readonly",
        "activitywatch-read-token",
    }
    assert secrets["haku-forgejo-git"]["spec"]["data"] == [
        {"secretKey": "password", "remoteRef": {"key": "haku-forgejo-git", "property": "password"}}
    ]
    assert not any(obj["kind"] == "Secret" for obj in objects)


if __name__ == "__main__":
    pytest_bazel.main()
