"""Small builders for the `CiliumNetworkPolicySpecEgress` shapes repeated across every
Agentplane construct: a single TCP port to a namespace+name-selected endpoint, and the
namespace+name label selector itself.
"""

from __future__ import annotations

from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
)

# kube-dns's own selector -- every environment's DNS egress rule selects it the same way.
KUBE_DNS_LABELS = {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}


def endpoint_labels(namespace: str, name: str) -> dict[str, str]:
    return {"k8s:io.kubernetes.pod.namespace": namespace, "app.kubernetes.io/name": name}


def tcp_egress_to(match_labels: dict[str, str], port: int) -> CiliumNetworkPolicySpecEgress:
    return CiliumNetworkPolicySpecEgress(
        to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=match_labels)],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port=str(port), protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ]
            )
        ],
    )
