"""Static scrape targets for Talos' etcd metrics: a headless Service with a hand-managed
EndpointSlice naming every control plane's Nebula IP, and the ServiceMonitor over it.

etcd runs outside Kubernetes (`listen-metrics-urls` on the node's Nebula IP, set by
cluster/terraform/main/ovh-nodes.tf), so no selector can find it; the slice is rendered
from the mesh roster instead.
"""

from __future__ import annotations

from cdk8s_plus_34 import Protocol, Service, ServicePort, k8s
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsRelabelings,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.metadata import metadata
from cluster.scripts.nebula_mesh import Mesh

NAMESPACE = "monitoring"
_NAME = "talos-etcd-metrics"
_LABELS = {"app.kubernetes.io/name": _NAME, "app.kubernetes.io/part-of": NAMESPACE}
_PORT_NAME = "metrics"
_PORT = 2381


class TalosEtcdMetrics(Construct):
    def __init__(self, scope: Construct, id: str, mesh: Mesh) -> None:
        super().__init__(scope, id)
        Service(
            self,
            "service",
            metadata=metadata(_NAME, NAMESPACE, labels=_LABELS),
            cluster_ip="None",
            ports=[ServicePort(name=_PORT_NAME, port=_PORT, target_port=_PORT, protocol=Protocol.TCP)],
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
            ports=[k8s.EndpointPort(name=_PORT_NAME, port=_PORT, protocol="TCP")],
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
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata("talos-etcd", NAMESPACE, labels=_LABELS),
            spec=ServiceMonitorSpec(
                namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=[NAMESPACE]),
                selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": _NAME}),
                endpoints=[
                    ServiceMonitorSpecEndpoints(
                        port=_PORT_NAME,
                        path="/metrics",
                        interval="30s",
                        scrape_timeout="10s",
                        relabelings=[
                            ServiceMonitorSpecEndpointsRelabelings(
                                source_labels=["__meta_kubernetes_endpointslice_endpoint_hostname"], target_label="node"
                            )
                        ],
                    )
                ],
            ),
        )
