"""The runner manager and jobs have distinct, correctly selected network policies."""

import tomllib

import pytest_bazel
from cdk8s import App

from cluster.cdk8s.gitlab_spike import runner
from cluster.cdk8s.testing.manifests import find_resource


def test_manager_and_job_policies_select_distinct_workloads(scratch_app: App) -> None:
    resources = runner.chart(scratch_app).to_json()
    helm = find_resource(resources, "HelmRelease", runner.NAME)
    values = helm["spec"]["values"]
    manager_service_account = values["serviceAccount"]["name"]
    (config,) = tomllib.loads(values["runners"]["config"])["runners"]
    job = config["kubernetes"]

    manager = find_resource(resources, "CiliumNetworkPolicy", f"{runner.NAME}-manager")["spec"]
    jobs = find_resource(resources, "CiliumNetworkPolicy", f"{runner.NAME}-jobs")["spec"]
    assert manager["endpointSelector"]["matchLabels"] == {
        "io.cilium.k8s.policy.serviceaccount": manager_service_account
    }
    assert job["service_account"] != manager_service_account
    assert jobs["endpointSelector"]["matchLabels"] == {"io.cilium.k8s.policy.serviceaccount": job["service_account"]}
    job_account = find_resource(resources, "ServiceAccount", job["service_account"], namespace=job["namespace"])
    assert job_account["automountServiceAccountToken"] is False
    assert job["automount_service_account_token"] is False
    assert any("kube-apiserver" in rule.get("toEntities", []) for rule in manager["egress"])
    assert all("kube-apiserver" not in rule.get("toEntities", []) for rule in jobs["egress"])


if __name__ == "__main__":
    pytest_bazel.main()
