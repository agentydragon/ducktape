"""This cluster's own CiliumNetworkPolicy facts and recipes, layered on the generic wrapper in
`cluster.cdk8s.providers.cilium.network_policy`: which labels reach this cluster's kube-dns and
Authentik, and the node-IP/SNI workaround this cluster's hostNetwork Gateway needs for egress to
its own public hostnames.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
)
from constructs import Construct

from cluster.cdk8s.providers.cilium.network_policy import (
    EgressRule,
    Entity,
    IngressRule,
    NetworkPolicy,
    Protocol,
    deny_all_egress as _deny_all_egress,
    dns_allowlist as _dns_allowlist,
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


def deny_all_egress() -> list[CiliumNetworkPolicySpecEgressDeny]:
    return _deny_all_egress()


def dns_allowlist(*names: str) -> list[str]:
    """The matchers a DNS rule carries for `names`: patterns first, then exact names, each
    sorted, so the rule is stable under regrouping the toFQDNs side."""
    return _dns_allowlist(*names)


def network_policy(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    selector: dict[str, str] | CiliumNetworkPolicySpecEndpointSelector,
    ingress: Sequence[CiliumNetworkPolicySpecIngress] | None = None,
    egress: Sequence[CiliumNetworkPolicySpecEgress] | None = None,
    egress_deny: Sequence[CiliumNetworkPolicySpecEgressDeny] | None = None,
) -> NetworkPolicy:
    return NetworkPolicy(
        scope, id, metadata=metadata, selector=selector, ingress=ingress, egress=egress, egress_deny=egress_deny
    )


def ingress_from(*sources: dict[str, str], ports: Sequence[int]) -> CiliumNetworkPolicySpecIngress:
    """TCP `ports` from Pods matching any of `sources`."""
    return IngressRule.from_endpoints(*sources, ports=ports)


def ingress_from_gateway(*ports: int) -> CiliumNetworkPolicySpecIngress:
    """TCP `ports` from the Gateway: cilium-envoy is hostNetwork and its egress to a backend Pod
    carries the reserved:ingress identity, not a Pod's."""
    return IngressRule.from_gateway(*ports)


def egress_to(
    labels: dict[str, str], *ports: int, server_names: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """TCP `ports` on Pods matching `labels`; `server_names` pins the TLS SNI."""
    return EgressRule.to_endpoints(labels, *ports, server_names=server_names)


def egress_to_entities(
    *entities: Entity, ports: Sequence[int] = (), server_names: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """Cilium's named identities, on TCP `ports` or on any port when none are given."""
    return EgressRule.to_entities(*entities, ports=ports, server_names=server_names)


def egress_to_cidrs(*cidrs: str, ports: Sequence[int]) -> CiliumNetworkPolicySpecEgress:
    """TCP `ports` on external IP ranges that do not receive a Cilium entity identity."""
    return EgressRule.to_cidrs(*cidrs, ports=ports)


def egress_via_gateway(*server_names: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """A public origin the hostNetwork Gateway serves. It resolves to node IPs, which FQDN and
    CIDR selectors cannot match with the cluster's Cilium configuration; TLS SNI narrows the
    node:443 rule to that origin, and TLS stays end-to-end (no terminatingTLS secret, no MITM)."""
    return egress_to_entities("remote-node", "host", ports=[port], server_names=server_names)


def egress_to_fqdns(*hosts: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """`hosts` over TLS on `port`, with the SNI pinned to the same names: a toFQDNs match alone
    admits any SNI to an address one of them resolved to."""
    return EgressRule.to_fqdns(*hosts, port=port)


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
