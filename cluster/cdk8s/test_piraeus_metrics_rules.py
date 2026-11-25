"""Run the deployed Piraeus alert rule against a roaming node going offline and online."""

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.testing.manifests import find_resource
from cluster.cdk8s.testing.promtool import PromtoolRules

pytest_plugins = ("cluster.cdk8s.testing.promtool",)


def test_roaming_piraeus_alerts(runfiles: Runfiles, promtool: PromtoolRules) -> None:
    path = runfiles.root() / "ducktape/cluster/generated/piraeus/cluster/cluster.k8s.yaml"
    rule = find_resource(yaml.safe_load_all(path.read_text()), "PrometheusRule", "piraeus-lvm-metrics-rugged")
    promtool("ducktape/cluster/cdk8s/testdata/piraeus_roaming_rules.yaml", {"piraeus.yaml": rule["spec"]})


if __name__ == "__main__":
    pytest_bazel.main()
