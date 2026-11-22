"""Check online/offline handling of a roaming node's Piraeus alerts."""

import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s import piraeus_metrics
from cluster.cdk8s.testing.promtool import PromtoolRules


def test_roaming_piraeus_alerts(scratch_chart: Chart, promtool: PromtoolRules) -> None:
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
    promtool("ducktape/cluster/cdk8s/testdata/piraeus_roaming_rules.yaml", {"piraeus.yaml": rule["spec"]})


if __name__ == "__main__":
    pytest_bazel.main()
