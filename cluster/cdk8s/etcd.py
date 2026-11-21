"""Static scrape targets for Talos' etcd metrics: a headless Service with a hand-managed
EndpointSlice naming every control plane's Nebula IP, and its ServiceMonitors.

etcd runs outside Kubernetes (`listen-metrics-urls` on the node's Nebula IP, set by
cluster/terraform/main/ovh-nodes.tf), so no selector can find it; the slice is rendered
from the mesh roster instead.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Service, k8s
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsMetricRelabelings,
    ServiceMonitorSpecEndpointsMetricRelabelingsAction,
    ServiceMonitorSpecEndpointsRelabelings,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.service_ref import Port
from cluster.scripts.nebula_mesh import Mesh

NAME = "etcd-monitoring"
NAMESPACE = "monitoring"
_NAME = "talos-etcd-metrics"
_LABELS = {"app.kubernetes.io/name": _NAME, "app.kubernetes.io/part-of": NAMESPACE}
_METRICS = Port(name="metrics", number=2381)
_FAST_MONITOR = "talos-etcd-fast"


class TalosEtcdMetrics(Construct):
    def __init__(self, scope: Construct, id: str, mesh: Mesh) -> None:
        super().__init__(scope, id)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
            cluster_ip="None",
            ports=[_METRICS.service_port()],
        )
        k8s.KubeEndpointSlice(
            self,
            "endpoints",
            metadata=k8s.ObjectMeta(
                name=_NAME,
                namespace=NAMESPACE,
                labels={
                    "kubernetes.io/service-name": _NAME,
                    # Anything but endpointslice-controller.k8s.io keeps the control plane's
                    # controller from reconciling (emptying) a slice whose Service has no selector.
                    "endpointslice.kubernetes.io/managed-by": "ducktape-static",
                },
            ),
            address_type="IPv4",
            ports=[k8s.EndpointPort(name=_METRICS.name, port=_METRICS.number, protocol="TCP")],
            endpoints=[
                k8s.Endpoint(
                    addresses=[host.nebula_ip],
                    hostname=name,
                    node_name=name,
                    conditions=k8s.EndpointConditions(ready=True),
                )
                for name, host in mesh.control_planes().items()
            ],
        )
        namespace_selector = ServiceMonitorSpecNamespaceSelector(match_names=[NAMESPACE])
        selector = ServiceMonitorSpecSelector(match_labels=_LABELS)
        node_label = ServiceMonitorSpecEndpointsRelabelings(
            source_labels=["__meta_kubernetes_endpointslice_endpoint_hostname"], target_label="node"
        )
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name="talos-etcd", namespace=NAMESPACE, labels=_LABELS),
            spec=ServiceMonitorSpec(
                namespace_selector=namespace_selector,
                selector=selector,
                endpoints=[
                    ServiceMonitorSpecEndpoints(
                        port=_METRICS.name, path="/metrics", scrape_timeout="10s", relabelings=[node_label]
                    )
                ],
            ),
        )
        ServiceMonitor(
            self,
            "fast-servicemonitor",
            metadata=ApiObjectMetadata(name=_FAST_MONITOR, namespace=NAMESPACE),
            spec=ServiceMonitorSpec(
                namespace_selector=namespace_selector,
                selector=selector,
                endpoints=[
                    ServiceMonitorSpecEndpoints(
                        port=_METRICS.name,
                        path="/metrics",
                        # One-minute scrapes straddled a six-second follower apply wait in #18601.
                        interval="2s",
                        scrape_timeout="1s",
                        relabelings=[
                            node_label,
                            ServiceMonitorSpecEndpointsRelabelings(target_label="job", replacement=_FAST_MONITOR),
                        ],
                        metric_relabelings=[
                            ServiceMonitorSpecEndpointsMetricRelabelings(
                                action=ServiceMonitorSpecEndpointsMetricRelabelingsAction.KEEP,
                                source_labels=["__name__"],
                                regex="etcd_server_(proposals_(committed|applied)_total|has_leader|is_leader)",
                            ),
                            # Preserve the series matched by existing queries, including jobless selectors.
                            ServiceMonitorSpecEndpointsMetricRelabelings(
                                source_labels=["__name__"],
                                regex="etcd_(.+)",
                                target_label="__name__",
                                replacement="etcd_fast_$1",
                            ),
                        ],
                    )
                ],
            ),
        )


def chart(app: App, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    TalosEtcdMetrics(chart, "etcd", mesh)
    return chart
