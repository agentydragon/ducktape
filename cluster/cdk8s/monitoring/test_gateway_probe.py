"""Exercise the gateway probe's alert expressions with upstream promtool."""

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.monitoring import gateway_probe
from cluster.scripts.nebula_mesh import Host, Mesh
from util.bazel.runfiles import get_required_path

_MESH = Mesh(
    hosts={
        "probe-test-cp": Host(
            nebula_ip="192.0.2.1", endpoint="198.51.100.1:4242", role="control-plane", managed_by="tofu-ovh"
        ),
        "probe-test-worker": Host(
            nebula_ip="192.0.2.2", endpoint="198.51.100.2:4242", role="worker", managed_by="tofu-ovh"
        ),
        # Behind NAT, so outside public DNS: no probe is expected there.
        "probe-test-home": Host(nebula_ip="192.0.2.3", role="worker", managed_by="tofu-home"),
    }
)

_TESTS = dedent(
    """\
    rule_files:
      - gateway-probe.yaml
    evaluation_interval: 1m
    tests:
      - name: an own-node failure and a public node without results alert; the control dial does not
        interval: 1m
        input_series:
          - series: kube_node_info{job="kube-state-metrics",node="probe-test-cp"}
            values: 1+0x20
          - series: kube_node_info{job="kube-state-metrics",node="probe-test-worker"}
            values: 1+0x20
          - series: kube_node_info{job="kube-state-metrics",node="probe-test-home"}
            values: 1+0x20
          - series: probe_success{job="monitoring/gateway-probe",node="probe-test-cp",dial="own_public",instance="198.51.100.1:443"}
            values: 0+0x20
          - series: probe_success{job="monitoring/gateway-probe",node="probe-test-cp",dial="own_nebula",instance="192.0.2.1:443"}
            values: 1+0x20
          - series: probe_success{job="monitoring/gateway-probe",node="probe-test-cp",dial="gateway_service",instance="gw:443"}
            values: 0+0x20
        promql_expr_test:
          - expr: ALERTS{alertname=~"OwnNodeGateway.*",alertstate="firing"}
            eval_time: 20m
            exp_samples:
              - labels: ALERTS{alertname="OwnNodeGatewayHandshakeFailing",alertstate="firing",severity="warning",job="monitoring/gateway-probe",node="probe-test-cp",dial="own_public",instance="198.51.100.1:443"}
                value: 1
              - labels: ALERTS{alertname="OwnNodeGatewayProbeMissing",alertstate="firing",severity="warning",node="probe-test-worker"}
                value: 1
    """
)


def test_gateway_probe_rules(tmp_path: Path) -> None:
    manifest = one(
        manifest
        for manifest in Cdk8sTesting.synth(gateway_probe.chart(Cdk8sTesting.app(), _MESH))
        if manifest["kind"] == "PrometheusRule"
    )
    (tmp_path / "gateway-probe.yaml").write_text(yaml.safe_dump(manifest["spec"]))
    (tmp_path / "tests.yaml").write_text(_TESTS)
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
