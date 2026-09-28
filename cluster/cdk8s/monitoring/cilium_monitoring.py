"""ServiceMonitors for the Cilium agent and Hubble's flow metrics in kube-system."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

NAME = "cilium-monitoring"
NAMESPACE = "monitoring"


def _labels(name: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": NAMESPACE}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "cilium-agent",
        metadata=ApiObjectMetadata(name="cilium-agent", namespace=NAMESPACE, labels=_labels("cilium-agent")),
        namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=["kube-system"]),
        selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "cilium-agent"}),
        endpoints=[Endpoint.plain(port="metrics", scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="10s")],
    )
    # Hubble flow metrics, enabled by `hubble.metrics` in
    # cluster/terraform/main/cilium-values.yaml. The Cilium chart creates the
    # headless `hubble-metrics` Service only when that list is non-empty, so this
    # produces no targets until the corresponding Helm upgrade has been applied.
    #
    # `k8s-app: hubble` also matches hubble-peer/relay/ui; the `hubble-metrics` port
    # name is what narrows the targets to the metrics Service.
    ServiceMonitor(
        chart,
        "hubble",
        metadata=ApiObjectMetadata(name="hubble", namespace=NAMESPACE, labels=_labels("hubble")),
        namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=["kube-system"]),
        selector=ServiceMonitorSpecSelector(match_labels={"k8s-app": "hubble"}),
        endpoints=[
            Endpoint.plain(port="hubble-metrics", scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="10s")
        ],
    )
    return chart
