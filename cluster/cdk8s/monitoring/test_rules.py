"""Exercise the PrometheusRule expressions with upstream promtool."""

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s.monitoring import rules
from cluster.cdk8s.testing.promtool import PromtoolRules


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
    promtool: PromtoolRules, rendered_rules: dict[str, dict[str, Any]], rule_files: dict[str, str], tests: str
) -> None:
    promtool(
        f"ducktape/cluster/cdk8s/monitoring/testdata/{tests}",
        {rule_file: rendered_rules[manifest_name]["spec"] for manifest_name, rule_file in rule_files.items()},
    )


if __name__ == "__main__":
    pytest_bazel.main()
