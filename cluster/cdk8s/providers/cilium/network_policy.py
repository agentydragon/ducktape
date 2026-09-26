"""Ergonomic wrapper for Cilium's `CiliumNetworkPolicy`: a class named after the kind, plus
`EgressRule`/`IngressRule`, grouping the CRD's real alternative peer shapes
(toEndpoints/toEntities/toCIDR/toFQDNs; fromEndpoints/the reserved `ingress` identity) the way
cdk8s-plus groups `Volume.from_config_map`/`.from_secret`/... under one type. No ducktape
namespace, secret name, hostname or topology fact lives here -- every value that would need one
is a required parameter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from cdk8s import ApiObjectMetadata
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy as _CiliumNetworkPolicy,
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
# Cilium's reserved identities (toEntities' real enum); add another member the day a second
# caller needs it -- the CRD also defines ingress/init/health/unmanaged/none/all.
_EGRESS_ENTITIES = {
    "world": CiliumNetworkPolicySpecEgressToEntities.WORLD,
    "cluster": CiliumNetworkPolicySpecEgressToEntities.CLUSTER,
    "remote-node": CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
    "host": CiliumNetworkPolicySpecEgressToEntities.HOST,
    "kube-apiserver": CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER,
}


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
    """A wildcard makes a Cilium pattern; anything else is an exact name -- `PortRuleDNS`'s two
    real alternatives (`matchPattern`/`matchName`)."""
    if "*" in name:
        return CiliumNetworkPolicySpecEgressToPortsRulesDns(match_pattern=name)
    return CiliumNetworkPolicySpecEgressToPortsRulesDns(match_name=name)


def _fqdn_matcher(host: str) -> CiliumNetworkPolicySpecEgressToFqdNs:
    """Same two alternatives as `_dns_matcher`, for a `toFQDNs` entry rather than a DNS-rule name."""
    if "*" in host:
        return CiliumNetworkPolicySpecEgressToFqdNs(match_pattern=host)
    return CiliumNetworkPolicySpecEgressToFqdNs(match_name=host)


def _egress_to_fqdns(
    hosts: Sequence[str], *, port: int, server_names: Sequence[str] | None
) -> CiliumNetworkPolicySpecEgress:
    return CiliumNetworkPolicySpecEgress(
        to_fqd_ns=[_fqdn_matcher(host) for host in hosts],
        to_ports=[_egress_ports([port], "TCP", server_names=server_names)],
    )


def _proxies_dns(rule: CiliumNetworkPolicySpecEgress) -> bool:
    return any(port_rule.rules is not None and port_rule.rules.dns for port_rule in rule.to_ports or [])


class EgressRule:
    """A `CiliumNetworkPolicy` egress rule's peer: `toEndpoints`/`toEntities`/`toCIDR`/`toFQDNs`
    are Cilium's real alternative ways to name who a rule allows outbound traffic to. `toPorts`
    stays a separate module function (`dns_egress`) rather than a fifth factory here: it is a port
    rule, not a peer shape, and every peer shape below combines with it the same way.
    """

    @staticmethod
    def to_endpoints(
        labels: dict[str, str], *ports: int, server_names: Sequence[str] | None = None
    ) -> CiliumNetworkPolicySpecEgress:
        """TCP `ports` on Pods matching `labels`; `server_names` pins the TLS SNI."""
        return CiliumNetworkPolicySpecEgress(
            to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=labels)],
            to_ports=[_egress_ports(ports, "TCP", server_names=server_names)],
        )

    @staticmethod
    def to_entities(
        *entities: Entity, ports: Sequence[int] = (), server_names: Sequence[str] | None = None
    ) -> CiliumNetworkPolicySpecEgress:
        """Cilium's named identities, on TCP `ports` or on any port when none are given."""
        return CiliumNetworkPolicySpecEgress(
            to_entities=[_EGRESS_ENTITIES[entity] for entity in entities],
            to_ports=[_egress_ports(ports, "TCP", server_names=server_names)] if ports else None,
        )

    @staticmethod
    def to_cidrs(*cidrs: str, ports: Sequence[int]) -> CiliumNetworkPolicySpecEgress:
        """TCP `ports` on external IP ranges that do not receive a Cilium entity identity."""
        return CiliumNetworkPolicySpecEgress(to_cidr=list(cidrs), to_ports=[_egress_ports(ports, "TCP")])

    @staticmethod
    def to_fqdns(*hosts: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
        """`hosts` over TLS on `port`, with the SNI pinned to the same names: a toFQDNs match alone
        admits any SNI to an address one of them resolved to."""
        return _egress_to_fqdns(hosts, port=port, server_names=hosts)


class IngressRule:
    """A `CiliumNetworkPolicy` ingress rule's peer, grouped the same way as `EgressRule`."""

    @staticmethod
    def from_endpoints(*sources: dict[str, str], ports: Sequence[int]) -> CiliumNetworkPolicySpecIngress:
        """TCP `ports` from Pods matching any of `sources`."""
        return CiliumNetworkPolicySpecIngress(
            from_endpoints=[CiliumNetworkPolicySpecIngressFromEndpoints(match_labels=source) for source in sources],
            to_ports=[_ingress_ports(ports)],
        )

    @staticmethod
    def from_gateway(*ports: int) -> CiliumNetworkPolicySpecIngress:
        """TCP `ports` from the reserved `ingress` identity: any Cilium Gateway API/Ingress
        implementation running its Envoy with hostNetwork gives its egress to a backend Pod this
        identity, not the Pod's own."""
        return CiliumNetworkPolicySpecIngress(
            from_entities=[CiliumNetworkPolicySpecIngressFromEntities.INGRESS], to_ports=[_ingress_ports(ports)]
        )


def deny_all_egress() -> list[CiliumNetworkPolicySpecEgressDeny]:
    return [CiliumNetworkPolicySpecEgressDeny(to_entities=[CiliumNetworkPolicySpecEgressDenyToEntities.ALL])]


def dns_allowlist(*names: str) -> list[str]:
    """The matchers a DNS rule carries for `names`: patterns first, then exact names, each
    sorted, so the rule is stable under regrouping the toFQDNs side."""
    unique = set(names)
    return sorted(name for name in unique if "*" in name) + sorted(name for name in unique if "*" not in name)


def dns_egress(
    labels: dict[str, str], *, protocols: Sequence[Protocol] = ("UDP", "TCP"), resolves: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """Port 53 on Pods matching `labels`. `resolves` adds the DNS-aware rule that lets Cilium
    observe the answers a toFQDNs rule in the same policy needs, and bounds the query names to
    those matchers (`"*"` for any)."""
    return CiliumNetworkPolicySpecEgress(
        to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=labels)],
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
    dns_labels: dict[str, str], *groups: Sequence[str], resolves_also: Sequence[str] = (), port: int = 443
) -> list[CiliumNetworkPolicySpecEgress]:
    """An FQDN allowlist's two halves from one host list, resolved through the DNS server matching
    `dns_labels`: the DNS rule admitting exactly the names `groups` hold plus `resolves_also`, then
    one toFQDNs rule per group on TCP `port`.

    The DNS rule is not redundant with toFQDNs. The DNS proxy matches the query name before
    any destination identity exists, so it is the only layer that can fence a name resolving
    to a node IP, which toFQDNs cannot select; and Cilium has no way to share one list between
    the two rule kinds, so deriving both here is what keeps them equal. Both halves bound the
    selected Pod's own resolution and connections, not those of the workloads behind a proxy.
    SNI is not pinned: serverNames cannot carry the patterns a group may hold.
    """
    hosts = [host for group in groups for host in group]
    return [
        dns_egress(dns_labels, protocols=["ANY"], resolves=dns_allowlist(*hosts, *resolves_also)),
        *(_egress_to_fqdns(group, port=port, server_names=None) for group in groups),
    ]


class NetworkPolicy(_CiliumNetworkPolicy):
    """Cilium's `CiliumNetworkPolicy`, following cdk8s-plus's own construction pattern: a class
    named after the kind, constructed as `NetworkPolicy(scope, id, ...)`. A `dict[str, str]`
    `selector` is a plain-labels shorthand for `CiliumNetworkPolicySpecEndpointSelector`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        selector: dict[str, str] | CiliumNetworkPolicySpecEndpointSelector,
        ingress: Sequence[CiliumNetworkPolicySpecIngress] | None = None,
        egress: Sequence[CiliumNetworkPolicySpecEgress] | None = None,
        egress_deny: Sequence[CiliumNetworkPolicySpecEgressDeny] | None = None,
    ) -> None:
        if isinstance(selector, dict):
            selector = CiliumNetworkPolicySpecEndpointSelector(match_labels=selector)
        # Cilium matches toFQDNs only against answers its DNS proxy saw, and the proxy sees only the
        # queries a DNS rule covers. Without one, the toFQDNs rules admit nothing, and connections to
        # those hosts time out instead of failing.
        if egress is not None and any(rule.to_fqd_ns for rule in egress) and not any(map(_proxies_dns, egress)):
            raise ValueError(f"toFQDNs egress needs a DNS rule, dns_egress(...), in the same policy: {id=}")
        super().__init__(
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
