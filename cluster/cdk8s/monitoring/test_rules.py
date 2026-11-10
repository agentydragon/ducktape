"""Exercise the PrometheusRule expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles

from cluster.cdk8s.monitoring import rules


@pytest.mark.parametrize(
    ("rule_files", "tests"),
    [
        pytest.param(
            {"github-quota-alerts": "github-quota.yaml", "roaming-node-alerts": "roaming-node.yaml"},
            "github_quota_rules.yaml",
            id="github-quota",
        ),
        pytest.param({"disk-usage-rules": "disk-usage.yaml"}, "disk_usage_rules.yaml", id="disk-usage"),
        pytest.param({"io-pressure-alerts": "io-pressure.yaml"}, "io_pressure_rules.yaml", id="io-pressure"),
        pytest.param({"roaming-node-alerts": "roaming-node.yaml"}, "alloy_roaming_rules.yaml", id="alloy-roaming"),
    ],
)
def test_prometheus_rules(
    runfiles: Runfiles, promtool: Path, scratch_app: App, tmp_path: Path, rule_files: dict[str, str], tests: str
) -> None:
    manifests = {manifest["metadata"]["name"]: manifest for manifest in Cdk8sTesting.synth(rules.chart(scratch_app))}
    for manifest_name, rule_file in rule_files.items():
        (tmp_path / rule_file).write_text(yaml.safe_dump(manifests[manifest_name]["spec"]))
    (tmp_path / "tests.yaml").write_text(
        (runfiles.root() / f"ducktape/cluster/cdk8s/monitoring/testdata/{tests}").read_text()
    )
    subprocess.run([promtool, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)


if __name__ == "__main__":
    pytest_bazel.main()
