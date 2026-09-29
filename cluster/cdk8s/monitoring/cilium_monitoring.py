"""ServiceMonitors for the Cilium agent and Hubble's flow metrics in kube-system."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import HostNetworkServiceRef, Pods, Port

NAME = "cilium-monitoring"
NAMESPACE = "monitoring"
# The agent DaemonSet's Pods, on the host network. The Cilium chart
# (cluster/terraform/main/cilium-values.yaml) renders both metrics Services in front of them.
_AGENT = Pods(namespace="kube-system", labels=(("app.kubernetes.io/name", "cilium-agent"),))
_AGENT_METRICS = HostNetworkServiceRef(name="cilium-agent", port=Port(name="metrics", number=9962), pods=_AGENT)
# Hubble flow metrics, enabled by `hubble.metrics` in cilium-values.yaml. The chart creates
# this headless Service only when that list is non-empty.
_HUBBLE_METRICS = HostNetworkServiceRef(
    name="hubble-metrics", port=Port(name="hubble-metrics", number=9965), pods=_AGENT
)


def _labels(name: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": NAMESPACE}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "cilium-agent",
        metadata=ApiObjectMetadata(name="cilium-agent", namespace=NAMESPACE, labels=_labels("cilium-agent")),
        namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=[_AGENT_METRICS.pods.namespace]),
        selector=ServiceMonitorSpecSelector(match_labels=_AGENT_METRICS.labels),
        endpoints=[
            Endpoint.plain(
                port=_AGENT_METRICS.port.name, scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="10s"
            )
        ],
    )
    ServiceMonitor(
        chart,
        "hubble",
        metadata=ApiObjectMetadata(name="hubble", namespace=NAMESPACE, labels=_labels("hubble")),
        namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=[_HUBBLE_METRICS.pods.namespace]),
        # The chart labels this Service `k8s-app: hubble`, which the agent Pods behind it do not
        # carry, so `_HUBBLE_METRICS.labels` would not select it.
        selector=ServiceMonitorSpecSelector(match_labels={"k8s-app": "hubble"}),
        endpoints=[
            Endpoint.plain(
                port=_HUBBLE_METRICS.port.name, scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="10s"
            )
        ],
    )
    return chart
