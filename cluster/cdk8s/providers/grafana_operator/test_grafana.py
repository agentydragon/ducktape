import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.providers.grafana_operator.grafana import Grafana


def test_external_sets_external_reference_not_managed_fields() -> None:
    chart = Cdk8sTesting.chart()
    Grafana.external(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="grafana"),
        url="http://grafana-service.monitoring.svc.cluster.local:3000",
        tenant_namespace="flux-system",
        use_kube_auth=True,
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    spec = manifest["spec"]
    assert spec["external"] == {
        "url": "http://grafana-service.monitoring.svc.cluster.local:3000",
        "tenantNamespace": "flux-system",
    }
    assert spec["client"] == {"useKubeAuth": True}
    assert "config" not in spec
    assert "deployment" not in spec


if __name__ == "__main__":
    pytest_bazel.main()
