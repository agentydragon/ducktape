"""Smoke test for the cdk8s-generated ServiceMonitor bindings (cdk8s_import.bzl).

Confirms the generated jsii package actually loads and synthesizes under Bazel, and
that its typed fields round-trip the same shape cluster/litellm_constructs.py emits.
"""

from pathlib import Path

import pytest_bazel
from cdk8s import App, Chart
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecSelector,
)


def test_servicemonitor_synthesizes_with_bearer_token_secret(tmp_path: Path) -> None:
    app = App(outdir=str(tmp_path))
    chart = Chart(app, "test", disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "servicemonitor",
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "litellm"}),
            endpoints=[
                ServiceMonitorSpecEndpoints(
                    port="http",
                    path="/metrics",
                    interval="15s",
                    scrape_timeout="10s",
                    bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(
                        name="litellm-master-key", key="api-key"
                    ),
                )
            ],
        ),
    )
    app.synth()
    (manifest_path,) = tmp_path.glob("*.k8s.yaml")
    manifest = manifest_path.read_text()

    assert "kind: ServiceMonitor" in manifest
    assert "bearerTokenSecret" in manifest


if __name__ == "__main__":
    pytest_bazel.main()
