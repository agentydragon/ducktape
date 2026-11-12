"""Exercise the KVM worker's KEDA query against executor and scrape failures."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles

from cluster.cdk8s import buildbuddy_kvm_worker


def test_demand_query(scratch_app: App, runfiles: Runfiles, promtool: Path, tmp_path: Path) -> None:
    chart = buildbuddy_kvm_worker.autoscaler_chart(scratch_app)
    worker_chart = buildbuddy_kvm_worker.chart(scratch_app)
    (scaled_object,) = Cdk8sTesting.synth(chart)
    alert_rule = next(obj for obj in Cdk8sTesting.synth(worker_chart) if obj["kind"] == "PrometheusRule")
    spec = scaled_object["spec"]
    assert spec["minReplicaCount"] == 1
    assert spec["maxReplicaCount"] == 3
    behavior = spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    assert behavior["scaleUp"]["stabilizationWindowSeconds"] == 120
    assert behavior["scaleUp"]["policies"] == [{"type": "Pods", "value": 1, "periodSeconds": 900}]
    assert behavior["scaleDown"]["stabilizationWindowSeconds"] == 900
    assert behavior["scaleDown"]["policies"] == [{"type": "Pods", "value": 1, "periodSeconds": 300}]
    query = spec["triggers"][0]["metadata"]["query"]
    (tmp_path / "buildbuddy.yaml").write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {"name": "demand", "rules": [{"record": "buildbuddy_kvm:demand", "expr": query}]},
                    *alert_rule["spec"]["groups"],
                ]
            }
        )
    )
    (tmp_path / "tests.yaml").write_text(
        (runfiles.root() / "ducktape/cluster/cdk8s/testdata/buildbuddy_scaling_rules.yaml").read_text()
    )
    subprocess.run([promtool, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)


if __name__ == "__main__":
    pytest_bazel.main()
