"""Exercise the deployed platform monitoring rules with upstream promtool."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.testing.promtool import PromtoolRules


@pytest.fixture(scope="module")
def rule_files(runfiles: Runfiles) -> dict[str, dict[str, Any]]:
    path = runfiles.root() / "ducktape/cluster/generated/platform-monitoring/platform-monitoring.k8s.yaml"
    return {
        f"{doc['metadata']['name']}.yaml": doc["spec"]
        for doc in yaml.safe_load_all(path.read_text())
        if doc["kind"] == "PrometheusRule"
    }


@pytest.mark.parametrize(
    "case", ["client_certificate", "github_quota", "disk_usage", "io_pressure", "alloy_roaming", "roaming_node"]
)
def test_prometheus_rules(promtool: PromtoolRules, rule_files: dict[str, dict[str, Any]], case: str) -> None:
    promtool(f"ducktape/cluster/cdk8s/monitoring/testdata/{case}_rules.yaml", rule_files)


if __name__ == "__main__":
    pytest_bazel.main()
