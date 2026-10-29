"""Exercise the PrometheusRule expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s.monitoring import rules
from cluster.cdk8s.testing.scratch import scratch_app
from util.bazel.runfiles import get_required_path


def _promtool_test_rules(app: App, tmp_path: Path, rule_files: dict[str, str], tests: str) -> None:
    """Writes each named PrometheusRule's spec to its rule file, then runs promtool on `tests`."""
    manifests = {manifest["metadata"]["name"]: manifest for manifest in Cdk8sTesting.synth(rules.chart(app))}
    for manifest_name, rule_file in rule_files.items():
        (tmp_path / rule_file).write_text(yaml.safe_dump(manifests[manifest_name]["spec"]))
    (tmp_path / "tests.yaml").write_text(
        get_required_path(f"_main/cluster/cdk8s/monitoring/testdata/{tests}").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


def test_github_quota_rules(tmp_path: Path) -> None:
    _promtool_test_rules(
        scratch_app(tmp_path),
        tmp_path,
        {"github-quota-alerts": "github-quota.yaml", "roaming-node-alerts": "roaming-node.yaml"},
        "github_quota_rules.yaml",
    )


def test_disk_usage_rules(tmp_path: Path) -> None:
    _promtool_test_rules(
        scratch_app(tmp_path), tmp_path, {"disk-usage-rules": "disk-usage.yaml"}, "disk_usage_rules.yaml"
    )


def test_io_pressure_rules(tmp_path: Path) -> None:
    _promtool_test_rules(
        scratch_app(tmp_path), tmp_path, {"io-pressure-alerts": "io-pressure.yaml"}, "io_pressure_rules.yaml"
    )


if __name__ == "__main__":
    pytest_bazel.main()
