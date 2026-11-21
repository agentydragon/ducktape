"""Exercise the PrometheusRule expressions with upstream promtool."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from python.runfiles import Runfiles

from cluster.cdk8s.monitoring import rules


@pytest.fixture(scope="module")
def rendered_rules(scratch_app_factory: Callable[[], App]) -> dict[str, dict[str, Any]]:
    app = scratch_app_factory()
    return {manifest["metadata"]["name"]: manifest for manifest in Cdk8sTesting.synth(rules.chart(app))}


@pytest.mark.parametrize(
    ("rule_files", "tests"),
    [
        pytest.param(
            {"client-certificate-expiry-alerts": "client-cert.yaml"},
            "client_certificate_rules.yaml",
            id="client-certificate-expiry",
        ),
        pytest.param(
            {"github-quota-alerts": "github-quota.yaml", "roaming-node-alerts": "roaming-node.yaml"},
            "github_quota_rules.yaml",
            id="github-quota",
        ),
        pytest.param({"disk-usage-rules": "disk-usage.yaml"}, "disk_usage_rules.yaml", id="disk-usage"),
        pytest.param({"io-pressure-alerts": "io-pressure.yaml"}, "io_pressure_rules.yaml", id="io-pressure"),
        pytest.param({"roaming-node-alerts": "roaming-node.yaml"}, "alloy_roaming_rules.yaml", id="alloy-roaming"),
        pytest.param({"roaming-node-alerts": "roaming-node.yaml"}, "roaming_node_rules.yaml", id="roaming-node"),
    ],
)
def test_prometheus_rules(
    runfiles: Runfiles,
    promtool: Path,
    rendered_rules: dict[str, dict[str, Any]],
    tmp_path: Path,
    rule_files: dict[str, str],
    tests: str,
) -> None:
    for manifest_name, rule_file in rule_files.items():
        (tmp_path / rule_file).write_text(yaml.safe_dump(rendered_rules[manifest_name]["spec"]))
    (tmp_path / "tests.yaml").write_text(
        (runfiles.root() / f"ducktape/cluster/cdk8s/monitoring/testdata/{tests}").read_text()
    )
    subprocess.run([promtool, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)


if __name__ == "__main__":
    pytest_bazel.main()
