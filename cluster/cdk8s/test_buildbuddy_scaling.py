"""Exercise the KVM worker's KEDA query against executor and scrape failures."""

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.testing.manifests import find_resource
from cluster.cdk8s.testing.promtool import PromtoolRules

pytest_plugins = ("cluster.cdk8s.testing.promtool",)


def test_demand_query(runfiles: Runfiles, promtool: PromtoolRules) -> None:
    cluster = runfiles.root() / "ducktape/cluster"
    autoscaler = cluster / "generated/buildbuddy-kvm-worker-autoscaler/buildbuddy-kvm-worker-autoscaler.k8s.yaml"
    worker = cluster / "k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml"
    scaled_object = find_resource(yaml.safe_load_all(autoscaler.read_text()), "ScaledObject", "buildbuddy-kvm-worker")
    alert_rule = find_resource(yaml.safe_load_all(worker.read_text()), "PrometheusRule", "buildbuddy-kvm-worker")
    query = scaled_object["spec"]["triggers"][0]["metadata"]["query"]
    promtool(
        "ducktape/cluster/cdk8s/testdata/buildbuddy_scaling_rules.yaml",
        {
            "buildbuddy.yaml": {
                "groups": [
                    {"name": "demand", "rules": [{"record": "buildbuddy_kvm:demand", "expr": query}]},
                    *alert_rule["spec"]["groups"],
                ]
            }
        },
    )


if __name__ == "__main__":
    pytest_bazel.main()
