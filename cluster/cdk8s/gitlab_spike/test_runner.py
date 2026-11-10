"""The runner manager and jobs have distinct, correctly selected network policies."""

import tomllib

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.gitlab_spike import runner


def test_manager_and_job_policies_select_distinct_workloads(scratch_app: App) -> None:
    resources = Cdk8sTesting.synth(runner.chart(scratch_app))
    helm = next(resource for resource in resources if resource["kind"] == "HelmRelease")
    values = helm["spec"]["values"]
    manager_service_account = values["serviceAccount"]["name"]
    (config,) = tomllib.loads(values["runners"]["config"])["runners"]
    job = config["kubernetes"]

    policies = {
        resource["metadata"]["name"]: resource["spec"]
        for resource in resources
        if resource["kind"] == "CiliumNetworkPolicy"
    }
    manager = policies[f"{runner.NAME}-manager"]
    jobs = policies[f"{runner.NAME}-jobs"]
    assert manager["endpointSelector"]["matchLabels"] == {
        "io.cilium.k8s.policy.serviceaccount": manager_service_account
    }
    assert job["service_account"] != manager_service_account
    assert jobs["endpointSelector"]["matchLabels"].items() <= job["pod_labels"].items()
    assert any("kube-apiserver" in rule.get("toEntities", []) for rule in manager["egress"])
    assert all("kube-apiserver" not in rule.get("toEntities", []) for rule in jobs["egress"])


if __name__ == "__main__":
    pytest_bazel.main()
