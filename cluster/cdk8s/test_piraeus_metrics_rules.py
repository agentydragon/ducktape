"""Check online/offline handling of a roaming node's Piraeus alerts."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles

from cluster.cdk8s import piraeus_metrics


def test_roaming_piraeus_alerts(scratch_chart: Chart, runfiles: Runfiles, promtool: Path, tmp_path: Path) -> None:
    piraeus_metrics.add_thin_pool_monitoring(
        scratch_chart,
        namespace="piraeus-datastore",
        node_selector={"kubernetes.io/hostname": "rugged"},
        volume_group="piraeus_ssd",
        thin_pool="data",
        storage_pool="ssd",
        name="piraeus-lvm-metrics-rugged",
        only_when_node_ready="rugged",
    )
    rule = next(obj for obj in Cdk8sTesting.synth(scratch_chart) if obj["kind"] == "PrometheusRule")
    (tmp_path / "piraeus.yaml").write_text(yaml.safe_dump(rule["spec"]))
    (tmp_path / "tests.yaml").write_text(
        (runfiles.root() / "ducktape/cluster/cdk8s/testdata/piraeus_roaming_rules.yaml").read_text()
    )
    subprocess.run([promtool, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)


if __name__ == "__main__":
    pytest_bazel.main()
