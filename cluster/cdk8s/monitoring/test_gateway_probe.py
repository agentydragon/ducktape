"""Exercise the gateway probe's alert expressions with upstream promtool."""

import pytest_bazel
from cdk8s import App
from more_itertools import one

from cluster.cdk8s.monitoring import gateway_probe
from cluster.cdk8s.testing.promtool import PromtoolRules
from cluster.scripts.nebula_mesh import Host, Mesh

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


def test_gateway_probe_rules(promtool: PromtoolRules, scratch_app: App) -> None:
    manifest = one(
        manifest
        for manifest in gateway_probe.chart(scratch_app, _MESH).to_json()
        if manifest["kind"] == "PrometheusRule"
    )
    promtool(
        "ducktape/cluster/cdk8s/monitoring/testdata/gateway_probe_rules.yaml", {"gateway-probe.yaml": manifest["spec"]}
    )


if __name__ == "__main__":
    pytest_bazel.main()
