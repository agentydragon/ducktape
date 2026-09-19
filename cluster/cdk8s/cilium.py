"""One call per CiliumNetworkPolicy rule. The generated `cilium_crds` structs spell a single
TCP port out over a dozen lines; these name the shapes the generators repeat.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from cdk8s import ApiObjectMetadata
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEgressDenyToEntities,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToFqdNs,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEgressToPortsRules,
    CiliumNetworkPolicySpecEgressToPortsRulesDns,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct

Protocol = Literal["TCP", "UDP", "ANY"]
Entity = Literal["world", "cluster", "remote-node", "host", "kube-apiserver"]
_ENTITIES = {
    "world": CiliumNetworkPolicySpecEgressToEntities.WORLD,
    "cluster": CiliumNetworkPolicySpecEgressToEntities.CLUSTER,
    "remote-node": CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
    "host": CiliumNetworkPolicySpecEgressToEntities.HOST,
    "kube-apiserver": CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER,
}

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


def network_policy(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    selector: dict[str, str] | CiliumNetworkPolicySpecEndpointSelector,
    ingress: Sequence[CiliumNetworkPolicySpecIngress] | None = None,
    egress: Sequence[CiliumNetworkPolicySpecEgress] | None = None,
    egress_deny: Sequence[CiliumNetworkPolicySpecEgressDeny] | None = None,
) -> CiliumNetworkPolicy:
    if isinstance(selector, dict):
        selector = CiliumNetworkPolicySpecEndpointSelector(match_labels=selector)
    return CiliumNetworkPolicy(
        scope,
        id,
        metadata=metadata,
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=selector,
            ingress=list(ingress) if ingress is not None else None,
            egress=list(egress) if egress is not None else None,
            egress_deny=list(egress_deny) if egress_deny is not None else None,
        ),
    )


def _ingress_ports(ports: Sequence[int]) -> CiliumNetworkPolicySpecIngressToPorts:
    return CiliumNetworkPolicySpecIngressToPorts(
        ports=[
            CiliumNetworkPolicySpecIngressToPortsPorts(
                port=str(port), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
            )
            for port in ports
        ]
    )


def _egress_ports(
    ports: Sequence[int], protocol: Protocol, *, server_names: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgressToPorts:
    return CiliumNetworkPolicySpecEgressToPorts(
        ports=[
            CiliumNetworkPolicySpecEgressToPortsPorts(
                port=str(port), protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol[protocol]
            )
            for port in ports
        ],
        server_names=list(server_names) if server_names else None,
    )


def _dns_matcher(name: str) -> CiliumNetworkPolicySpecEgressToPortsRulesDns:
    """A wildcard makes a Cilium pattern; anything else is an exact name."""
    if "*" in name:
        return CiliumNetworkPolicySpecEgressToPortsRulesDns(match_pattern=name)
    return CiliumNetworkPolicySpecEgressToPortsRulesDns(match_name=name)


def _fqdn_matcher(host: str) -> CiliumNetworkPolicySpecEgressToFqdNs:
    if "*" in host:
        return CiliumNetworkPolicySpecEgressToFqdNs(match_pattern=host)
    return CiliumNetworkPolicySpecEgressToFqdNs(match_name=host)


def ingress_from(*sources: dict[str, str], ports: Sequence[int]) -> CiliumNetworkPolicySpecIngress:
    """TCP `ports` from Pods matching any of `sources`."""
    return CiliumNetworkPolicySpecIngress(
        from_endpoints=[CiliumNetworkPolicySpecIngressFromEndpoints(match_labels=source) for source in sources],
        to_ports=[_ingress_ports(ports)],
    )


def ingress_from_gateway(*ports: int) -> CiliumNetworkPolicySpecIngress:
    """TCP `ports` from the Gateway: cilium-envoy is hostNetwork and its egress to a backend Pod
    carries the reserved:ingress identity, not a Pod's."""
    return CiliumNetworkPolicySpecIngress(
        from_entities=[CiliumNetworkPolicySpecIngressFromEntities.INGRESS], to_ports=[_ingress_ports(ports)]
    )


def egress_to(
    labels: dict[str, str], *ports: int, server_names: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """TCP `ports` on Pods matching `labels`; `server_names` pins the TLS SNI."""
    return CiliumNetworkPolicySpecEgress(
        to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=labels)],
        to_ports=[_egress_ports(ports, "TCP", server_names=server_names)],
    )


def egress_to_entities(
    *entities: Entity, ports: Sequence[int] = (), server_names: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """Cilium's named identities, on TCP `ports` or on any port when none are given."""
    return CiliumNetworkPolicySpecEgress(
        to_entities=[_ENTITIES[entity] for entity in entities],
        to_ports=[_egress_ports(ports, "TCP", server_names=server_names)] if ports else None,
    )


def egress_to_cidrs(*cidrs: str, ports: Sequence[int]) -> CiliumNetworkPolicySpecEgress:
    """TCP `ports` on external IP ranges that do not receive a Cilium entity identity."""
    return CiliumNetworkPolicySpecEgress(to_cidr=list(cidrs), to_ports=[_egress_ports(ports, "TCP")])


def egress_via_gateway(*server_names: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """A public origin the hostNetwork Gateway serves. It resolves to node IPs, which FQDN and
    CIDR selectors cannot match with the cluster's Cilium configuration; TLS SNI narrows the
    node:443 rule to that origin, and TLS stays end-to-end (no terminatingTLS secret, no MITM)."""
    return egress_to_entities("remote-node", "host", ports=[port], server_names=server_names)


def _egress_to_fqdns(
    hosts: Sequence[str], *, port: int, server_names: Sequence[str] | None
) -> CiliumNetworkPolicySpecEgress:
    return CiliumNetworkPolicySpecEgress(
        to_fqd_ns=[_fqdn_matcher(host) for host in hosts],
        to_ports=[_egress_ports([port], "TCP", server_names=server_names)],
    )


def egress_to_fqdns(*hosts: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """`hosts` over TLS on `port`, with the SNI pinned to the same names: a toFQDNs match alone
    admits any SNI to an address one of them resolved to."""
    return _egress_to_fqdns(hosts, port=port, server_names=hosts)


def dns_allowlist(*names: str) -> list[str]:
    """The matchers a DNS rule carries for `names`: patterns first, then exact names, each
    sorted, so the rule is stable under regrouping the toFQDNs side."""
    unique = set(names)
    return sorted(name for name in unique if "*" in name) + sorted(name for name in unique if "*" not in name)


def dns_egress(
    *, protocols: Sequence[Protocol] = ("UDP", "TCP"), resolves: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """kube-dns on port 53. `resolves` adds the DNS-aware rule that lets Cilium observe the
    answers a toFQDNs rule in the same policy needs, and bounds the query names to those
    matchers (`"*"` for any)."""
    return CiliumNetworkPolicySpecEgress(
        to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=KUBE_DNS_LABELS)],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol[protocol]
                    )
                    for protocol in protocols
                ],
                rules=(
                    CiliumNetworkPolicySpecEgressToPortsRules(dns=[_dns_matcher(name) for name in resolves])
                    if resolves is not None
                    else None
                ),
            )
        ],
    )


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
    hosts = [host for group in groups for host in group]
    return [
        dns_egress(protocols=["ANY"], resolves=dns_allowlist(*hosts, *resolves_also)),
        *(_egress_to_fqdns(group, port=port, server_names=None) for group in groups),
    ]


def deny_all_egress() -> list[CiliumNetworkPolicySpecEgressDeny]:
    return [CiliumNetworkPolicySpecEgressDeny(to_entities=[CiliumNetworkPolicySpecEgressDenyToEntities.ALL])]
