"""This cluster's own CiliumNetworkPolicy facts and recipes, layered on the generic wrapper in
`cluster.cdk8s.providers.cilium.network_policy`: which labels reach this cluster's kube-dns and
Authentik, and the node-IP/SNI workaround this cluster's hostNetwork Gateway needs for egress to
its own public hostnames.
"""

from __future__ import annotations

from collections.abc import Sequence

from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress

from cluster.cdk8s.providers.cilium.network_policy import (
    EgressRule,
    Entity,
    Protocol,
    dns_egress as _dns_egress,
    fqdn_fence as _fqdn_fence,
)

# kube-dns's own selector -- every environment's DNS egress rule selects it the same way.
KUBE_DNS_LABELS = {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}
# Authentik's server Pods, reached through the Gateway Service with the client's original SNI
# (cluster/docs/cilium_network_policy.md § Egress through the Gateway Service).
AUTHENTIK_SERVER_LABELS = {
    "k8s:io.kubernetes.pod.namespace": "authentik",
    "app.kubernetes.io/name": "authentik",
    "app.kubernetes.io/instance": "authentik",
    "app.kubernetes.io/component": "server",
}


def endpoint_labels(namespace: str, name: str) -> dict[str, str]:
    return {"k8s:io.kubernetes.pod.namespace": namespace, "app.kubernetes.io/name": name}


def egress_via_gateway(*server_names: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """A public origin the hostNetwork Gateway serves. It resolves to node IPs, which FQDN and
    CIDR selectors cannot match with the cluster's Cilium configuration; TLS SNI narrows the
    node:443 rule to that origin, and TLS stays end-to-end (no terminatingTLS secret, no MITM)."""
    return EgressRule.to_entities(Entity.REMOTE_NODE, Entity.HOST, ports=[port], server_names=server_names)


def dns_egress(
    *, protocols: Sequence[Protocol] = ("UDP", "TCP"), resolves: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """kube-dns on port 53. `resolves` adds the DNS-aware rule that lets Cilium observe the
    answers a toFQDNs rule in the same policy needs, and bounds the query names to those
    matchers (`"*"` for any)."""
    return _dns_egress(KUBE_DNS_LABELS, protocols=protocols, resolves=resolves)


def fqdn_fence(
    *groups: Sequence[str], resolves_also: Sequence[str] = (), port: int = 443
) -> list[CiliumNetworkPolicySpecEgress]:
    """An FQDN allowlist's two halves from one host list: the DNS rule admitting exactly the
    names `groups` hold plus `resolves_also`, then one toFQDNs rule per group on TCP `port`.

    The DNS rule is not redundant with toFQDNs. The DNS proxy matches the query name before
    any destination identity exists, so it is the only layer that can fence a name resolving
    to a node IP, which toFQDNs cannot select (cluster/docs/cilium_network_policy.md); and
    Cilium has no way to share one list between the two rule kinds, so deriving both here is
    what keeps them equal. Both halves bound the selected Pod's own resolution and
    connections, not those of the workloads behind a proxy. SNI is not pinned: serverNames
    cannot carry the patterns a group may hold.
    """
    return _fqdn_fence(KUBE_DNS_LABELS, *groups, resolves_also=resolves_also, port=port)
