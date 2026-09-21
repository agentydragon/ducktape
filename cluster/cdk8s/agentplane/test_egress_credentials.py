"""Each proxy can read only the credentials approved for its environment."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import STAGING_NAMESPACE, TESTING_NAMESPACE, EgressCredentials


def test_real_credentials_have_only_staging_readers_and_exact_source_access() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredentials(
        chart, "credentials", namespace=STAGING_NAMESPACE, proxy_namespace="agentplane-staging", include_forgejo=True
    )
    objects = Cdk8sTesting.synth(chart)
    bindings = [obj for obj in objects if obj["kind"] == "RoleBinding"]
    assert len(bindings) == 2
    assert {(subject["namespace"], subject["name"]) for binding in bindings for subject in binding["subjects"]} == {
        ("agentplane-staging", "agentplane-egress"),
        (STAGING_NAMESPACE, "external-creds-reader"),
    }

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

    secrets = [obj for obj in objects if obj["kind"] == "ExternalSecret"]
    assert {(obj["metadata"]["namespace"], obj["metadata"]["name"]) for obj in secrets} == {
        (STAGING_NAMESPACE, "agentplane-github-pat"),
        (STAGING_NAMESPACE, "haku-forgejo-git"),
    }
    forgejo = next(obj for obj in secrets if obj["metadata"]["name"] == "haku-forgejo-git")
    assert forgejo["spec"]["data"] == [
        {"secretKey": "password", "remoteRef": {"key": "haku-forgejo-git", "property": "password"}}
    ]
    assert not any(obj["kind"] == "Secret" for obj in objects)


def test_testing_gets_github_only_and_no_forgejo_source_grant() -> None:
    chart = Cdk8sTesting.chart()
    EgressCredentials(
        chart, "credentials", namespace=TESTING_NAMESPACE, proxy_namespace="agentplane-testing", include_forgejo=False
    )
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
