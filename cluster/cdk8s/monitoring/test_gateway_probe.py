"""Exercise the gateway probe's alert expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.monitoring import gateway_probe
from cluster.scripts.nebula_mesh import Host, Mesh
from util.bazel.runfiles import get_required_path

# testdata/gateway_probe_rules.yaml writes its series for these node names.
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


def test_gateway_probe_rules(tmp_path: Path) -> None:
    manifest = one(
        manifest
        for manifest in Cdk8sTesting.synth(gateway_probe.chart(Cdk8sTesting.app(), _MESH))
        if manifest["kind"] == "PrometheusRule"
    )
    (tmp_path / "gateway-probe.yaml").write_text(yaml.safe_dump(manifest["spec"]))
    (tmp_path / "tests.yaml").write_text(
        get_required_path("_main/cluster/cdk8s/monitoring/testdata/gateway_probe_rules.yaml").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
