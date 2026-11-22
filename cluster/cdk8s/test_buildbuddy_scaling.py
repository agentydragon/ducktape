"""Exercise the KVM worker's KEDA query against executor and scrape failures."""

import pytest_bazel
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s import buildbuddy_kvm_worker
from cluster.cdk8s.testing.promtool import PromtoolRules


def test_demand_query(scratch_app: App, promtool: PromtoolRules) -> None:
    chart = buildbuddy_kvm_worker.autoscaler_chart(scratch_app)
    worker_chart = buildbuddy_kvm_worker.chart(scratch_app)
    (scaled_object,) = Cdk8sTesting.synth(chart)
    alert_rule = next(obj for obj in Cdk8sTesting.synth(worker_chart) if obj["kind"] == "PrometheusRule")
    spec = scaled_object["spec"]
    query = spec["triggers"][0]["metadata"]["query"]
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
